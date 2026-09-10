from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

import pytest

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget
from mcp_telegram.telegram_demand_coordinator import (
    CoordinatorState,
    TelegramDemandCoordinator,
    validate_durable_adapters,
)
from mcp_telegram.telegram_rpc_consumers import (
    DURABLE_DEMAND_ORDER,
    TELEGRAM_DEMAND_CONTRACTS,
    DemandKind,
)
from mcp_telegram.telegram_rpc_scheduler import (
    RPC_SOURCE_SERVICE_CLASS,
    RpcAdmissionClosedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcScope,
    TelegramRpcSource,
)


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


async def _noop() -> None:
    return None


class _Adapter:
    def __init__(self, status: DemandStatus | None = None) -> None:
        self.current_status = status
        self.status_calls: list[float] = []
        self.run_calls: list[RpcAttemptBudget] = []
        self.run_error: BaseException | None = None
        self.on_run: Callable[[], Awaitable[None]] = _noop

    def status(self, now: float) -> DemandStatus | None:
        self.status_calls.append(now)
        return self.current_status

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        self.run_calls.append(budget)
        await self.on_run()
        if self.run_error is not None:
            raise self.run_error


def _adapters(statuses: Mapping[DemandKind, DemandStatus] | None = None) -> dict[DemandKind, _Adapter]:
    statuses = statuses or {}
    return {kind: _Adapter(statuses.get(kind)) for kind in DURABLE_DEMAND_ORDER}


def test_startup_uses_explicit_order_and_full_authoritative_scan() -> None:
    adapters = _adapters(
        {
            DemandKind.SCHEDULED_DISCOVERY: DemandStatus(release_at=0),
            DemandKind.ENTITY_PROFILE_REFRESH: DemandStatus(release_at=0),
            DemandKind.DELTA_GAP_FILL: DemandStatus(release_at=0),
        }
    )
    coordinator = TelegramDemandCoordinator(adapters, clock=_Clock())

    assert coordinator.ready_kinds == (
        DemandKind.ENTITY_PROFILE_REFRESH,
        DemandKind.DELTA_GAP_FILL,
        DemandKind.SCHEDULED_DISCOVERY,
    )
    assert all(adapter.status_calls == [100.0] for adapter in adapters.values())


def test_offer_coalesces_and_performs_one_full_scan() -> None:
    clock = _Clock()
    adapters = _adapters()
    coordinator = TelegramDemandCoordinator(adapters, clock=clock)
    target = DemandKind.SCHEDULED_REPAIR
    adapters[target].current_status = DemandStatus(release_at=100)

    assert coordinator.offer(target) is True
    assert [coordinator.offer(target) for _ in range(1_000)] == [False] * 1_000
    assert coordinator.offered_kinds == frozenset({target})
    assert all(len(adapter.status_calls) == 2 for adapter in adapters.values())


@pytest.mark.asyncio
async def test_scan_preserves_fifo_positions_and_tail_rotates_after_slice() -> None:
    clock = _Clock()
    shutdown = asyncio.Event()
    first = DemandKind.ENTITY_PROFILE_REFRESH
    second = DemandKind.DELTA_GAP_FILL
    adapters = _adapters({first: DemandStatus(0), second: DemandStatus(0)})
    coordinator = TelegramDemandCoordinator(adapters, clock=clock)

    async def run_first() -> None:
        adapters[first].current_status = DemandStatus(0)
        shutdown.set()

    adapters[first].on_run = run_first
    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=clock)
    assert coordinator.ready_kinds == (first, second)
    await coordinator.run()
    assert coordinator.ready_kinds == (second, first)


@pytest.mark.asyncio
async def test_run_executes_one_slice_with_contract_budget_and_stops_cleanly() -> None:
    shutdown = asyncio.Event()
    target = DURABLE_DEMAND_ORDER[0]
    adapters = _adapters({target: DemandStatus(0)})

    async def finish() -> None:
        adapters[target].current_status = None
        shutdown.set()

    adapters[target].on_run = finish
    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=_Clock())
    await coordinator.run()

    assert coordinator.state is CoordinatorState.STOPPED
    assert len(adapters[target].run_calls) == 1
    assert adapters[target].run_calls[0].limit == TELEGRAM_DEMAND_CONTRACTS[target].max_rpc_attempts_per_slice


@pytest.mark.asyncio
async def test_single_flight_holds_during_active_slice_and_offer_wakes_waiter() -> None:
    shutdown = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()
    first = DURABLE_DEMAND_ORDER[0]
    second = DURABLE_DEMAND_ORDER[1]
    adapters = _adapters({first: DemandStatus(0), second: DemandStatus(0)})

    async def block() -> None:
        started.set()
        await release.wait()
        adapters[first].current_status = None
        adapters[second].current_status = None

    adapters[first].on_run = block
    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=_Clock())
    task = asyncio.create_task(coordinator.run())
    await started.wait()
    assert coordinator.offer(second) is False
    release.set()
    shutdown.set()
    await task
    assert sum(len(adapter.run_calls) for adapter in adapters.values()) == 1


@pytest.mark.asyncio
async def test_unexpected_failure_is_suppressed_and_other_kind_runs() -> None:
    shutdown = asyncio.Event()
    first, second = DURABLE_DEMAND_ORDER[:2]
    adapters = _adapters({first: DemandStatus(0), second: DemandStatus(0)})
    adapters[first].run_error = RuntimeError("domain failure")

    async def second_done() -> None:
        adapters[second].current_status = None
        shutdown.set()

    adapters[second].on_run = second_done
    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=_Clock(), safety_scan_seconds=10)
    await coordinator.run()

    assert len(adapters[first].run_calls) == 1
    assert len(adapters[second].run_calls) == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_from_active_slice() -> None:
    started = asyncio.Event()
    target = DURABLE_DEMAND_ORDER[0]
    adapters = _adapters({target: DemandStatus(0)})

    async def block() -> None:
        started.set()
        await asyncio.Future()

    adapters[target].on_run = block
    coordinator = TelegramDemandCoordinator(adapters, asyncio.Event(), clock=_Clock())
    task = asyncio.create_task(coordinator.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.state is CoordinatorState.STOPPED


@pytest.mark.asyncio
async def test_closed_admission_propagates_until_shutdown_then_exits() -> None:
    target = DURABLE_DEMAND_ORDER[0]
    adapters = _adapters({target: DemandStatus(0)})
    scope = TelegramRpcScope(
        TelegramRpcSource.FACT_HYDRATION_LIVE,
        RPC_SOURCE_SERVICE_CLASS[TelegramRpcSource.FACT_HYDRATION_LIVE],
        None,
        None,
    )
    closed = RpcAdmissionClosedError(scope, "scheduler closed")
    adapters[target].run_error = closed
    coordinator = TelegramDemandCoordinator(adapters, asyncio.Event(), clock=_Clock())

    with pytest.raises(RpcAdmissionClosedError):
        await coordinator.run()
    assert coordinator.state is CoordinatorState.STOPPED

    shutdown = asyncio.Event()
    adapters = _adapters({target: DemandStatus(0)})
    adapters[target].run_error = RpcAdmissionClosedError(scope, "scheduler closed")
    shutdown.set()
    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=_Clock())
    await coordinator.run()
    assert not adapters[target].run_calls


def test_status_failure_isolated_until_safety_scan() -> None:
    class Failing(_Adapter):
        def status(self, now: float) -> DemandStatus | None:
            self.status_calls.append(now)
            raise RuntimeError("status unavailable")

    adapters = _adapters({DURABLE_DEMAND_ORDER[1]: DemandStatus(0)})
    failing = Failing()
    adapters[DURABLE_DEMAND_ORDER[0]] = failing
    coordinator = TelegramDemandCoordinator(adapters, clock=_Clock(), safety_scan_seconds=10)

    assert coordinator.ready_kinds == (DURABLE_DEMAND_ORDER[1],)
    coordinator.scan(now=105)
    assert failing.status_calls == [100.0]
    assert coordinator.ready_kinds == (DURABLE_DEMAND_ORDER[1],)
    coordinator.scan(now=110)
    assert failing.status_calls == [100.0, 110.0]


def test_validation_requires_exact_twenty_adapters() -> None:
    adapters = _adapters()
    adapters.pop(DURABLE_DEMAND_ORDER[0])
    with pytest.raises(RuntimeError, match="coverage mismatch"):
        validate_durable_adapters(adapters)


@pytest.mark.asyncio
async def test_recoverable_transport_outcomes_do_not_kill_siblings() -> None:
    first, second = DURABLE_DEMAND_ORDER[:2]
    shutdown = asyncio.Event()
    adapters = _adapters({first: DemandStatus(0), second: DemandStatus(0)})
    adapters[first].run_error = TelegramRpcAdmissionDeferred(2)

    async def stop_after_first() -> None:
        shutdown.set()

    adapters[first].on_run = stop_after_first
    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=_Clock())

    await coordinator.run()
    assert coordinator.ready_kinds == (second,)

    second_shutdown = asyncio.Event()
    second_adapters = _adapters({second: DemandStatus(0)})
    second_adapters[second].run_error = TelegramRpcThrottled(2)

    async def stop_after_second() -> None:
        second_shutdown.set()

    second_adapters[second].on_run = stop_after_second
    coordinator = TelegramDemandCoordinator(second_adapters, second_shutdown, clock=_Clock())
    await coordinator.run()
    assert coordinator.next_release_at == 102.0
