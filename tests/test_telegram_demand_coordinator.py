from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.linked_chat_fact import linked_chat_fact_owner
from mcp_telegram.own_only import OwnOnlyContext
from mcp_telegram.scheduled_messages import (
    ScheduledDiscoveryDemandAdapter,
    ScheduledMessageReconciler,
    ScheduledReconciliationPolicy,
)
from mcp_telegram.sync_db import _apply_migrations
from mcp_telegram.telegram_demand import (
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
)
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
async def test_attempt_budget_exhaustion_is_deferred_and_suppressed() -> None:
    clock = _Clock()
    target = DemandKind.DELTA_GAP_FILL
    adapters = _adapters({target: DemandStatus(0)})
    adapters[target].run_error = RpcAttemptBudgetExhaustedError("slice attempt budget exhausted")
    observations: list[dict[str, object]] = []

    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=clock,
        safety_scan_seconds=10,
        observer=lambda **fields: observations.append(fields),
    )
    await coordinator._execute_slice(target)
    coordinator._active_kind = None
    coordinator.scan(now=clock.value)

    assert coordinator.ready_kinds == ()
    assert observations[-1]["outcome"] == "deferred"
    assert observations[-1]["reason"] == "attempt_budget_exhausted"

    clock.value += 10
    coordinator.scan(now=clock.value)
    assert coordinator.ready_kinds == (target,)


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


@pytest.mark.asyncio
async def test_suspended_discovery_does_not_spin_coordinator_and_restore_reoffers_due_work() -> None:
    now = 1_800_000_000
    clock = _Clock(float(now))
    conn, client, discovery = _suspended_discovery(now)
    callback_kind = DURABLE_DEMAND_ORDER[0]
    shutdown = asyncio.Event()
    initial = _adapters({callback_kind: DemandStatus(release_at=float(now))})
    adapters: dict[DemandKind, object] = dict(initial)
    adapters[DemandKind.SCHEDULED_DISCOVERY] = discovery
    coordinator = TelegramDemandCoordinator(
        cast(Mapping[DemandKind, DurableDemandAdapter], adapters), shutdown, clock=clock
    )
    callback_adapter = initial[callback_kind]
    callback_offers: list[bool] = []
    callback_adapter.on_run = _restore_link_callback(conn, clock, coordinator, shutdown, callback_offers)

    for _ in range(20):
        coordinator.scan(now=clock.value)
        assert DemandKind.SCHEDULED_DISCOVERY not in coordinator.ready_kinds
    assert callback_adapter.run_calls == []
    assert client.requests == []

    await coordinator.run()

    assert len(callback_adapter.run_calls) == 1
    assert callback_offers == [True]
    assert client.requests == []
    assert DemandKind.SCHEDULED_DISCOVERY in coordinator.ready_kinds
    assert discovery.status(clock.value + 1) == DemandStatus(release_at=0.0, freshness_deadline=float(now))
    conn.close()


class _CoordinatorScheduledClient:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def get_input_entity(self, _dialog_id: int, /) -> object:
        return object()

    async def get_entity(self, _dialog_id: int) -> object:
        return object()

    async def __call__(self, _request: object, **_kwargs: object) -> object:
        self.requests.append(_request)
        return SimpleNamespace(scheduled_messages=[])


def _suspended_discovery(
    now: int,
) -> tuple[sqlite3.Connection, _CoordinatorScheduledClient, ScheduledDiscoveryDemandAdapter]:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    channel_id = -1_000_000_000_900
    conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES(?,'channel',0)", (channel_id,))
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(?,'access_lost')", (channel_id,))
    conn.execute(
        "INSERT INTO linked_chat_fact_state(channel_id,generation,pending_generation,requested_at,retry_at) "
        "VALUES (?,0,0,?,?)",
        (channel_id, now, now + 86_400),
    )
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,repair_due_at,discovery_due_at,updated_at) "
        "VALUES (42,NULL,?,?)",
        (now, now),
    )
    conn.commit()
    client = _CoordinatorScheduledClient()
    reconciler = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        OwnOnlyContext(account_id=42, personal_channel_id=channel_id),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=1),
    )
    return conn, client, ScheduledDiscoveryDemandAdapter(reconciler)


def _restore_link_callback(
    conn: sqlite3.Connection,
    clock: _Clock,
    coordinator: TelegramDemandCoordinator,
    shutdown: asyncio.Event,
    offers: list[bool],
) -> Callable[[], Awaitable[None]]:
    async def restore_link() -> None:
        channel_id = -1_000_000_000_900
        with conn:
            conn.execute("UPDATE synced_dialogs SET status='synced' WHERE dialog_id=?", (channel_id,))
            generation = linked_chat_fact_owner.capture_generation(conn, channel_id)
            assert linked_chat_fact_owner.publish(conn, channel_id, generation, 400, int(clock.value) + 1)
        offers.append(coordinator.offer(DemandKind.SCHEDULED_DISCOVERY))
        shutdown.set()

    return restore_link
