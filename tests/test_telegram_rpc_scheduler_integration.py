# pyright: reportAny=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
import logging
from collections import Counter, deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest
from aiolimiter import AsyncLimiter
from telethon.tl.tlobject import TLRequest

from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.telegram_rpc import TelegramRpcGate, reset_account_cooldown
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    RpcServiceClass,
    RpcTransportReadiness,
    TelegramRpcAdmissionScheduler,
    TelegramRpcSource,
    current_rpc_scope,
    rpc_scope,
)


class _ControlledLimiter:
    def __init__(self) -> None:
        self._waiters: deque[asyncio.Future[None]] = deque()
        self.acquisitions = 0

    async def acquire(self) -> None:
        self.acquisitions += 1
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        await waiter

    async def allow_one(self) -> None:
        await _wait_until(lambda: bool(self._waiters))
        self._waiters.popleft().set_result(None)
        await asyncio.sleep(0)


class _SynchronousSender:
    """Telethon sender seam returning one already-resolved scalar future."""

    def __init__(self, sent: list[tuple[TelegramRpcSource, object]]) -> None:
        self._sent = sent

    def send(self, request: object, *, ordered: bool = False) -> asyncio.Future[object]:
        del ordered
        self._sent.append((current_rpc_scope().source, request))
        result = asyncio.get_running_loop().create_future()
        result.set_result(request)
        return result


class _ScalarRequest(TLRequest):
    CONSTRUCTOR_ID = 0x12345678

    def __init__(self, value: object) -> None:
        self.value = value


@dataclass(frozen=True, slots=True)
class _CircuitStatus:
    open: bool = False

    def detail(self) -> str:
        return "closed-for-test"


async def _wait_until(predicate: object, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


def _worst_case_interactive_slots(scheduler: TelegramRpcAdmissionScheduler) -> int:
    cycle = scheduler.fair_cycle
    interactive_positions = [index for index, item in enumerate(cycle) if item is RpcServiceClass.INTERACTIVE]
    return max(
        (next_index - index) if next_index > index else (next_index + len(cycle) - index)
        for index, next_index in zip(
            interactive_positions, interactive_positions[1:] + interactive_positions[:1], strict=True
        )
    )


async def _release_until_target(
    limiter: _ControlledLimiter,
    target: asyncio.Task[object],
    sent: list[tuple[TelegramRpcSource, object]],
    bound: int,
) -> int:
    released = 0
    while not target.done() and released < bound:
        await limiter.allow_one()
        released += 1
        await _wait_until(lambda expected=released: len(sent) >= expected)
    return released


def _assert_scalar_progress(sent: list[tuple[TelegramRpcSource, object]], events: list[RpcAdmissionEvent]) -> None:
    source_counts = Counter(source for source, _request in sent)
    assert source_counts[TelegramRpcSource.REALTIME_EVENT] >= 1
    assert source_counts[TelegramRpcSource.FULL_SYNC] >= 1
    assert all(not isinstance(request, (list, tuple, dict)) for _source, request in sent)
    assert len([event for event in events if event.kind is RpcAdmissionEventKind.DISPATCHED]) == len(sent)


async def _shutdown_cleanly(gate: TelegramRpcGate, backlog: list[asyncio.Task[object]]) -> None:
    for task in backlog:
        if not task.done():
            task.cancel()
    await asyncio.gather(*backlog, return_exceptions=True)
    await gate.close_rpc_scheduler()

    scheduler = gate._admission_scheduler
    assert scheduler.queue_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert not any(task.get_name() == "telegram-rpc-admission" and not task.done() for task in asyncio.all_tasks())


def _make_gate(
    limiter: _ControlledLimiter,
    policy: TelegramRpcSchedulerConfig,
    events: list[RpcAdmissionEvent],
) -> TelegramRpcGate:
    """Build the production gate with only its network and limiter seams controlled."""
    gate = object.__new__(TelegramRpcGate)
    status = _CircuitStatus()
    gate._rpc_circuit_status = lambda: status
    gate._fallback_wait_seconds = 60
    gate._cooldown_buffer_seconds = 1.0
    gate._transient_retry_delays = ()
    gate._flood_observer = lambda **_kwargs: None
    gate._scheduler_policy = policy
    gate._limiter = cast(AsyncLimiter, limiter)
    gate._loop = None
    gate._request_retries = 0
    gate._raise_last_call_error = True
    gate._flood_waited_requests = {}
    gate._no_updates = False
    gate._log = {"telethon.client.users": logging.getLogger(__name__)}
    gate.flood_sleep_threshold = 0
    gate.session = SimpleNamespace(process_entities=lambda _result: None)
    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=policy,
        limiter=limiter,
        observer=events.append,
        readiness=RpcTransportReadiness(
            probe=gate._scheduler_transport_ready,
            wait=gate._wait_for_scheduler_transport,
        ),
    )
    return gate


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    reset_account_cooldown()


@pytest.mark.asyncio
async def test_gate_shared_scheduler_keeps_interactive_bounded_and_shutdown_clean() -> None:
    policy = TelegramRpcSchedulerConfig(
        interactive_weight=1,
        live_sync_weight=3,
        background_weight=2,
        interactive_queue_capacity=8,
        live_sync_queue_capacity=64,
        background_queue_capacity=64,
        interactive_deadline_seconds=60,
        live_sync_deadline_seconds=60,
        background_deadline_seconds=60,
    )
    limiter = _ControlledLimiter()
    events: list[RpcAdmissionEvent] = []
    gate = _make_gate(limiter, policy, events)
    sent: list[tuple[TelegramRpcSource, object]] = []
    gate._sender = _SynchronousSender(sent)

    async def send(source: TelegramRpcSource, request: object) -> object:
        with rpc_scope(source):
            return await gate(_ScalarRequest(request))

    backlog = [asyncio.create_task(send(TelegramRpcSource.REALTIME_EVENT, object())) for _ in range(12)] + [
        asyncio.create_task(send(TelegramRpcSource.FULL_SYNC, object())) for _ in range(12)
    ]
    await _wait_until(lambda: sum(gate._admission_scheduler.queue_depths().values()) == len(backlog))

    target = asyncio.create_task(send(TelegramRpcSource.MCP_INTERACTIVE, object()))
    await _wait_until(lambda: gate._admission_scheduler.queue_depths()[RpcServiceClass.INTERACTIVE] == 1)

    cycle = gate._admission_scheduler.fair_cycle
    worst_case_slots = _worst_case_interactive_slots(gate._admission_scheduler)
    assert worst_case_slots == len(cycle) == 6

    released = await _release_until_target(limiter, target, sent, worst_case_slots)

    assert target.done()
    assert released <= worst_case_slots
    _assert_scalar_progress(sent, events)
    await _shutdown_cleanly(gate, backlog)
