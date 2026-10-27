# pyright: reportAny=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass
from typing import cast

import pytest
from telethon.errors import ServerError
from telethon.tl import functions

from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    demand_context,
)
from mcp_telegram.telegram_rpc import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcGate,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    RpcServiceClass,
    TelegramRpcAdmissionScheduler,
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)
from tests.helpers import reset_rpc_process_policy
from tests.test_telegram_rpc import _gate, _pending_owned_states, _set_sender


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


class _ImmediateLimiter:
    def __init__(self) -> None:
        self.acquisitions = 0

    async def acquire(self) -> None:
        self.acquisitions += 1


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
    limiter: _ControlledLimiter | _ImmediateLimiter,
    policy: TelegramRpcSchedulerConfig,
    events: list[RpcAdmissionEvent],
) -> TelegramRpcGate:
    """Build the guarded-writer fixture and control only scheduling and responses."""
    gate = _gate()
    gate._scheduler_policy = policy
    gate._admission_scheduler._policy = policy
    gate._admission_scheduler._capacities = {
        RpcServiceClass.INTERACTIVE: policy.interactive_queue_capacity,
        RpcServiceClass.LIVE_SYNC: policy.live_sync_queue_capacity,
        RpcServiceClass.BACKGROUND: policy.background_queue_capacity,
    }
    gate._admission_scheduler._fair_cycle = gate._admission_scheduler._build_fair_cycle(
        {
            RpcServiceClass.INTERACTIVE: policy.interactive_weight,
            RpcServiceClass.LIVE_SYNC: policy.live_sync_weight,
            RpcServiceClass.BACKGROUND: policy.background_weight,
        }
    )
    gate._limiter = cast(object, limiter)
    gate._admission_scheduler._limiter = limiter
    gate._admission_scheduler.set_observer(events.append)
    return gate


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    reset_rpc_process_policy()


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

    class _RecordingProvider:
        def send_with_scope(self, request: object, scope: object) -> object:
            sent.append((cast(object, scope).source, request))
            return request

        def send(self, request: object) -> object:
            raise AssertionError(f"scope missing for {request!r}")

    _set_sender(gate, _RecordingProvider())

    async def send(source: TelegramRpcSource, request: object) -> object:
        demand = (
            DemandKind.REALTIME_EVENT_ACQUISITION
            if source is TelegramRpcSource.REALTIME_EVENT
            else DemandKind.MCP_REMOTE_ACQUISITION
            if source is TelegramRpcSource.MCP_INTERACTIVE
            else DemandKind.FULL_SYNC_PAGE
        )
        with demand_context(demand):
            with rpc_scope(source):
                return await gate(functions.updates.GetStateRequest())

    backlog = [asyncio.create_task(send(TelegramRpcSource.REALTIME_EVENT, object())) for _ in range(8)] + [
        asyncio.create_task(send(TelegramRpcSource.FULL_SYNC, object())) for _ in range(8)
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


@pytest.mark.asyncio
async def test_sender_debits_actual_attempts_and_stops_transport_retry_at_slice_bound() -> None:
    limiter = _ImmediateLimiter()
    events: list[RpcAdmissionEvent] = []
    gate = _make_gate(limiter, TelegramRpcSchedulerConfig(), events)
    gate._transient_retry_delays = (0.0,)
    attempts: list[tuple[DemandKind | None, AcquisitionKind | None]] = []

    class _FailingSender:
        def send_with_scope(self, _request: object, scope: object) -> asyncio.Future[object]:
            attempts.append((scope.demand_kind, scope.acquisition_kind))
            result = asyncio.get_running_loop().create_future()
            result.set_exception(ServerError(None, "temporary"))
            return result

        def send(self, _request: object) -> asyncio.Future[object]:
            raise AssertionError("scripted response must retain its admitted scope")

    _set_sender(gate, _FailingSender())
    budget = RpcAttemptBudget(limit=1)
    with demand_context(DemandKind.FULL_SYNC_PAGE):
        with acquisition_context(AcquisitionKind.MESSAGE_HISTORY_PAGE):
            with rpc_attempt_budget(budget):
                with pytest.raises(RpcAttemptBudgetExhaustedError):
                    await gate(functions.updates.GetStateRequest())

    assert attempts == [(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_HISTORY_PAGE)]
    assert budget.attempts == 1
    assert limiter.acquisitions == 1
    assert [event.kind for event in events].count(RpcAdmissionEventKind.DISPATCHED) == 1
    exhausted = [event for event in events if event.reason == "attempt_budget_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0].demand_kind is DemandKind.FULL_SYNC_PAGE
    assert exhausted[0].acquisition_kind is AcquisitionKind.MESSAGE_HISTORY_PAGE
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def _assert_abandoned_terminal_scalar_response_is_consumed_and_releases_once(
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    gate = _make_gate(_ImmediateLimiter(), TelegramRpcSchedulerConfig(), [])
    provider_future: asyncio.Future[object] | None = None

    async def pending_response(_request: object) -> object:
        nonlocal provider_future
        provider_future = asyncio.get_running_loop().create_future()
        return await provider_future

    _set_sender(gate, pending_response)
    complete_calls = 0
    original_complete = gate._admission_scheduler.complete

    def complete_once(admission: object) -> None:
        nonlocal complete_calls
        complete_calls += 1
        original_complete(admission)  # type: ignore[arg-type]

    monkeypatch.setattr(gate._admission_scheduler, "complete", complete_once)
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:

        async def invoke() -> object:
            with demand_context(DemandKind.FULL_SYNC_PAGE):
                with rpc_scope(TelegramRpcSource.FULL_SYNC):
                    return await gate(functions.updates.GetStateRequest())

        caller = asyncio.create_task(invoke())
        await _wait_until(lambda: bool(_pending_owned_states(gate)))
        raw_future = next(iter(_pending_owned_states(gate))).future
        await _wait_until(lambda: provider_future is not None)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        assert provider_future is not None
        if terminal == "success":
            provider_future.set_result("ok")
        else:
            provider_future.set_exception(ServerError(None, "temporary"))

        await _wait_until(lambda: complete_calls == 1)
        assert not _pending_owned_states(gate)
        assert complete_calls == 1
        assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_handler)
        await gate.close_rpc_scheduler()


@pytest.mark.parametrize("terminal", ["success", "server_error"])
@pytest.mark.asyncio
async def test_abandoned_terminal_scalar_response_variants(
    terminal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _assert_abandoned_terminal_scalar_response_is_consumed_and_releases_once(monkeypatch, terminal)


@pytest.mark.asyncio
async def test_scalar_future_remains_owned_until_raw_disconnect_finishes(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _make_gate(_ImmediateLimiter(), TelegramRpcSchedulerConfig(), [])
    disconnect_started = asyncio.Event()
    disconnect_release = asyncio.Event()

    provider_future: asyncio.Future[object] | None = None

    async def pending_response(_request: object) -> object:
        nonlocal provider_future
        provider_future = asyncio.get_running_loop().create_future()
        return await provider_future

    _set_sender(gate, pending_response)
    connection = gate._main_sender._connection
    original_disconnect = connection.disconnect

    async def blocked_disconnect() -> None:
        disconnect_started.set()
        await disconnect_release.wait()
        await original_disconnect()

    monkeypatch.setattr(connection, "disconnect", blocked_disconnect)
    complete_calls = 0
    original_complete = gate._admission_scheduler.complete

    def complete_once(admission: object) -> None:
        nonlocal complete_calls
        complete_calls += 1
        original_complete(admission)  # type: ignore[arg-type]

    monkeypatch.setattr(gate._admission_scheduler, "complete", complete_once)

    async def invoke() -> object:
        with demand_context(DemandKind.FULL_SYNC_PAGE):
            with rpc_scope(TelegramRpcSource.FULL_SYNC):
                return await gate(functions.updates.GetStateRequest())

    caller = asyncio.create_task(invoke())
    await _wait_until(lambda: bool(_pending_owned_states(gate)))
    await _wait_until(lambda: provider_future is not None)
    raw_future = next(iter(_pending_owned_states(gate))).future
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    disconnect = asyncio.create_task(gate._disconnect_main_sender())
    try:
        await disconnect_started.wait()
        assert not raw_future.cancelled()
        assert len(_pending_owned_states(gate)) == 1
        assert sum(gate._admission_scheduler.active_depths().values()) == 1
        assert complete_calls == 0

        disconnect_release.set()
        await disconnect
        await _wait_until(lambda: not _pending_owned_states(gate))
        assert raw_future.cancelled()
        assert complete_calls == 1
        assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    finally:
        disconnect_release.set()
        await asyncio.gather(disconnect, return_exceptions=True)
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [1, 2])
async def test_same_caller_timeouts_cannot_reuse_retained_scalar_capacity(capacity: int) -> None:
    gate = _make_gate(
        _ImmediateLimiter(),
        TelegramRpcSchedulerConfig(background_queue_capacity=capacity),
        [],
    )
    provider_futures: list[asyncio.Future[object]] = []

    class _PendingSender:
        def is_connected(self) -> bool:
            return True

        def _transport_connected(self) -> bool:
            return True

        def send(self, _request: object, *, ordered: bool = False) -> asyncio.Future[object]:
            del ordered
            future = asyncio.get_running_loop().create_future()
            provider_futures.append(future)
            return future

        async def disconnect(self) -> None:
            return None

    _set_sender(gate, _PendingSender())

    async def abandon(value: object) -> None:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                with demand_context(DemandKind.FULL_SYNC_PAGE):
                    with rpc_scope(TelegramRpcSource.FULL_SYNC):
                        await gate(functions.updates.GetStateRequest())

    async def timed_scope_request(value: object) -> object:
        with demand_context(DemandKind.FULL_SYNC_PAGE):
            with rpc_scope(TelegramRpcSource.FULL_SYNC):
                async with asyncio.timeout(0.01):
                    return await gate(functions.updates.GetStateRequest())

    try:
        for value in range(capacity):
            await abandon(value)
        await _wait_until(lambda: len(_pending_owned_states(gate)) == capacity)
        assert gate._admission_scheduler.active_depths()[RpcServiceClass.BACKGROUND] == capacity
        if capacity == 1:
            for value in ("second-timeout-scope", "third-timeout-scope"):
                with pytest.raises(TelegramRpcAdmissionDeferred, match="temporarily busy"):
                    await timed_scope_request(value)
            assert len(provider_futures) == 1
            assert gate._admission_scheduler.active_depths()[RpcServiceClass.BACKGROUND] == 1
        with pytest.raises(TelegramRpcAdmissionDeferred, match="temporarily busy"):
            with demand_context(DemandKind.FULL_SYNC_PAGE):
                with rpc_scope(TelegramRpcSource.FULL_SYNC):
                    await gate(functions.updates.GetStateRequest())
        provider_futures[0].set_result("released")
        await _wait_until(lambda: gate._admission_scheduler.active_depths()[RpcServiceClass.BACKGROUND] == capacity - 1)
        await abandon("replacement")
        assert len(provider_futures) == capacity + 1
        assert gate._admission_scheduler.active_depths()[RpcServiceClass.BACKGROUND] == capacity
    finally:
        await gate._disconnect_main_sender()
        await gate.close_rpc_scheduler()
