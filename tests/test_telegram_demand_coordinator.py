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
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from mcp_telegram.telegram_rpc_consumers import (
    DURABLE_DEMAND_ORDER,
    TELEGRAM_DEMAND_CONTRACTS,
    DemandKind,
)
from mcp_telegram.telegram_rpc_scheduler import (
    RPC_SOURCE_SERVICE_CLASS,
    RpcAdmissionClosedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    TelegramRpcScope,
    TelegramRpcSource,
)
from sqlite_writer_helpers import inline_writer


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


async def _noop() -> None:
    return None


class _Adapter:
    def __init__(self, kind: DemandKind, status: DemandStatus | None = None) -> None:
        self.kind = kind
        self.current_status = status
        self.selected = WorkRevision(WorkKey(kind, 1, kind.value), "r1")
        self.selections = [self.selected]
        self.outcome_kind = SliceOutcomeKind.PROGRESSED
        self.request_methods: dict[WorkRevision, str] = {}
        self.actual_method: str | None = None
        self.status_calls: list[float] = []
        self.run_calls: list[RpcAttemptBudget] = []
        self.run_error: BaseException | None = None
        self.on_run: Callable[[], Awaitable[None]] = _noop

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        self.status_calls.append(now)
        selected = next((revision for revision in self.selections if revision not in excluded), None)
        if self.current_status is None or selected is None:
            return None
        return DemandStatus(
            self.current_status.release_at,
            self.current_status.freshness_deadline,
            selected,
            self.request_methods.get(selected, self.current_status.request_method),
        )

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        self.run_calls.append(budget)
        budget.last_method = self.actual_method
        await self.on_run()
        if self.run_error is not None:
            raise self.run_error
        selected = budget.selected
        assert selected is not None
        if self.outcome_kind in {SliceOutcomeKind.PROGRESSED, SliceOutcomeKind.TERMINAL}:
            return SliceOutcome(selected, self.outcome_kind, f"{selected.revision}-committed")
        return SliceOutcome(selected, self.outcome_kind)


def _adapters(statuses: Mapping[DemandKind, DemandStatus] | None = None) -> dict[DemandKind, _Adapter]:
    statuses = statuses or {}
    return {kind: _Adapter(kind, statuses.get(kind)) for kind in DURABLE_DEMAND_ORDER}


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


def test_offer_coalesces_without_scanning_adapters() -> None:
    clock = _Clock()
    adapters = _adapters()
    coordinator = TelegramDemandCoordinator(adapters, clock=clock)
    target = DemandKind.SCHEDULED_REPAIR
    adapters[target].current_status = DemandStatus(release_at=100)

    assert coordinator.offer(target) is True
    assert [coordinator.offer(target) for _ in range(1_000)] == [False] * 1_000
    assert coordinator.offered_kinds == frozenset({target})
    assert all(len(adapter.status_calls) == 1 for adapter in adapters.values())


@pytest.mark.asyncio
async def test_offer_wakes_pump_to_scan_and_run_new_state() -> None:
    target = DURABLE_DEMAND_ORDER[0]
    shutdown = asyncio.Event()
    adapters = _adapters()
    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=_Clock())

    async def finish() -> None:
        adapters[target].current_status = None
        shutdown.set()

    adapters[target].on_run = finish
    task = asyncio.create_task(coordinator.run())
    await asyncio.sleep(0)
    adapters[target].current_status = DemandStatus(release_at=100)
    calls_before_offer = len(adapters[target].status_calls)
    assert coordinator.offer(target)
    assert len(adapters[target].status_calls) == calls_before_offer
    await task

    assert len(adapters[target].run_calls) == 1
    assert len(adapters[target].status_calls) > calls_before_offer


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
    assert coordinator.ready_kinds == (second,)


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
async def test_run_yields_to_protocol_task_between_synchronous_slices() -> None:
    target = DURABLE_DEMAND_ORDER[0]
    adapters = _adapters({target: DemandStatus(0)})
    shutdown = asyncio.Event()
    first_slice_done = asyncio.Event()
    protocol_progress = asyncio.Event()
    slice_count = 0
    protocol_progress_at_second_slice = False

    async def local_slice() -> None:
        nonlocal slice_count, protocol_progress_at_second_slice
        slice_count += 1
        if slice_count == 1:
            first_slice_done.set()
        else:
            protocol_progress_at_second_slice = protocol_progress.is_set()
            adapters[target].current_status = None
            shutdown.set()

    async def protocol_task() -> None:
        await first_slice_done.wait()
        protocol_progress.set()

    adapters[target].on_run = local_slice
    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=_Clock())
    protocol = asyncio.create_task(protocol_task())

    await coordinator.run()
    await protocol

    assert protocol_progress_at_second_slice


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
async def test_sqlite_failure_reports_code_without_exception_text(caplog: pytest.LogCaptureFixture) -> None:
    target = DemandKind.BACKFILL_HYDRATION_BATCH
    adapters = _adapters({target: DemandStatus(0)})
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("SELECT * FROM private_failure_context")
    except sqlite3.OperationalError as exc:
        adapters[target].run_error = exc
    finally:
        conn.close()
    coordinator = TelegramDemandCoordinator(adapters, clock=_Clock())
    await coordinator._execute_slice(target)
    assert "sqlite_errorcode=1 sqlite_errorname=SQLITE_ERROR" in caplog.text
    assert "private_failure_context" not in caplog.text


@pytest.mark.asyncio
async def test_attempt_budget_exhaustion_defers_exact_revision() -> None:
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
    assert coordinator.ready_kinds == ()


@pytest.mark.asyncio
async def test_five_same_kind_no_progress_revisions_rotate_without_retrying_one() -> None:
    target = DemandKind.DELTA_GAP_FILL
    adapter = _Adapter(target, DemandStatus(0))
    adapter.selections = [WorkRevision(WorkKey(target, 7, f"peer-{index}"), "r1") for index in range(5)]
    adapter.outcome_kind = SliceOutcomeKind.NO_PROGRESS
    adapters = _adapters()
    adapters[target] = adapter
    coordinator = TelegramDemandCoordinator(adapters, clock=_Clock())

    for _ in range(5):
        coordinator.scan()
        assert coordinator.ready_kinds == (target,)
        await coordinator._execute_slice(target)
        coordinator._active_kind = None

    coordinator.scan()
    assert len(adapter.run_calls) == 5
    assert len({budget.selected for budget in adapter.run_calls}) == 5
    assert coordinator.ready_kinds == ()


def test_method_recovery_hold_skips_to_sibling_revision_of_same_kind() -> None:
    target = DemandKind.DELTA_GAP_FILL
    adapter = _Adapter(target, DemandStatus(0))
    first = WorkRevision(WorkKey(target, 9, "messages.getHistory"), "r1")
    second = WorkRevision(WorkKey(target, 9, "messages.getDialogs"), "r1")
    adapter.selections = [first, second]
    adapter.request_methods = {first: "messages.getHistory", second: "messages.getDialogs"}
    adapters = _adapters()
    adapters[target] = adapter
    recovery_held = True
    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=_Clock(),
        rpc_eligibility=lambda method: (
            TelegramRpcMethodRecoveryPending(method, "g1")
            if recovery_held and method == "messages.getHistory"
            else None
        ),
    )

    coordinator.scan()
    assert coordinator.ready_kinds == (target,), (
        coordinator._excluded,
        coordinator._eligibility_waits,
        coordinator.statuses,
    )
    assert coordinator.statuses[target].selected == second
    recovery_held = False
    coordinator.eligibility_changed("messages.getHistory")
    coordinator.scan()
    assert coordinator.statuses[target].selected == first


def test_finite_gate_deadline_remains_wait_boundary_after_owner_has_no_candidate() -> None:
    target = DemandKind.DELTA_GAP_FILL
    selected = WorkRevision(WorkKey(target, 4, "peer"), "r1")
    adapter = _Adapter(target, DemandStatus(0))
    adapter.selections = [selected]
    adapter.outcome_kind = SliceOutcomeKind.NO_PROGRESS
    adapters = _adapters()
    adapters[target] = adapter
    monotonic = _Clock(10.0)
    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=_Clock(100.0),
        monotonic_clock=monotonic,
        safety_scan_seconds=60,
        rpc_eligibility=lambda _method: TelegramRpcAdmissionDeferred(
            12, method_key="messages.getHistory", generation="g1", eligible_at=22.0, reason="server"
        ),
    )

    assert coordinator.ready_kinds == ()
    assert target not in coordinator.statuses
    assert selected in coordinator._excluded
    assert coordinator._wait_delay(100.0) == 12.0


@pytest.mark.asyncio
async def test_global_floor_does_not_create_invocation_until_first_execution() -> None:
    target = DemandKind.DELTA_GAP_FILL
    selected = WorkRevision(WorkKey(target, 5, "peer"), "r1")
    adapter = _Adapter(target, DemandStatus(0))
    adapter.selections = [selected]
    adapter.outcome_kind = SliceOutcomeKind.NO_PROGRESS
    adapters = _adapters()
    adapters[target] = adapter
    monotonic = _Clock(30.0)
    held = True
    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=_Clock(100.0),
        monotonic_clock=monotonic,
        rpc_eligibility=lambda _method: (
            TelegramRpcAdmissionDeferred(7, eligible_at=37.0, reason="server") if held else None
        ),
    )

    assert coordinator.ready_kinds == ()
    assert coordinator._invocations == {}
    assert coordinator._wait_delay(100.0) == 7.0
    monotonic.value = 37.0
    held = False
    coordinator.scan()
    assert coordinator.ready_kinds == (target,)
    await coordinator._execute_slice(target)
    assert adapter.run_calls[0].selected == selected
    assert adapter.run_calls[0].attempts == 0
    selected_deadline = coordinator._invocations[selected].deadline
    assert selected_deadline is not None and selected_deadline > monotonic.value


@pytest.mark.asyncio
async def test_throttle_catch_uses_current_gate_method_deferral() -> None:
    target = DemandKind.DELTA_GAP_FILL
    adapter = _Adapter(target, DemandStatus(0, request_method="advisory.method"))
    adapter.actual_method = "messages.getHistory"
    adapter.run_error = TelegramRpcThrottled(1)
    adapters = _adapters()
    adapters[target] = adapter
    attempted = False
    prepared: list[tuple[RpcAttemptBudget, str | None]] = []

    def eligibility(
        method: str | None,
    ) -> TelegramRpcThrottled | TelegramRpcMethodRecoveryPending | None:
        if not attempted:
            return None
        return TelegramRpcAdmissionDeferred(9, method_key=method, generation="g2", eligible_at=109.0, reason="server")

    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=_Clock(100),
        monotonic_clock=_Clock(100),
        rpc_eligibility=eligibility,
        prepare_durable_retry=lambda budget, method: prepared.append((budget, method)) or True,
    )
    attempted = True
    await coordinator._execute_slice(target)
    selected = adapter.run_calls[0].selected
    assert selected is not None
    assert prepared == [(adapter.run_calls[0], "messages.getHistory")]
    assert coordinator._revision_methods[selected] == "messages.getHistory"
    assert coordinator._eligibility_waits[selected] == 109.0


@pytest.mark.asyncio
async def test_exception_method_overrides_advisory_method_before_first_send() -> None:
    target = DemandKind.DELTA_GAP_FILL
    adapter = _Adapter(target, DemandStatus(0, request_method="advisory.method"))
    adapter.run_error = TelegramRpcAdmissionDeferred(
        4, method_key="messages.getHistory", generation="g3", eligible_at=104.0, reason="server"
    )
    adapters = _adapters()
    adapters[target] = adapter
    gate_methods: list[str | None] = []
    prepared_methods: list[str | None] = []

    def eligibility(method: str | None) -> TelegramRpcThrottled | TelegramRpcMethodRecoveryPending | None:
        gate_methods.append(method)
        return None

    def prepare(_budget: RpcAttemptBudget, method: str | None) -> bool:
        prepared_methods.append(method)
        return True

    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=_Clock(100),
        rpc_eligibility=eligibility,
        prepare_durable_retry=prepare,
    )
    await coordinator._execute_slice(target)
    selected = adapter.run_calls[0].selected
    assert selected is not None
    assert adapter.run_calls[0].last_method is None
    assert coordinator._revision_methods[selected] == "messages.getHistory"
    assert prepared_methods == ["messages.getHistory"]

    coordinator.eligibility_changed("advisory.method")
    assert selected in coordinator._excluded
    coordinator.eligibility_changed("messages.getHistory")
    assert selected not in coordinator._excluded
    adapter.run_error = None
    adapter.outcome_kind = SliceOutcomeKind.NO_PROGRESS
    adapter.run_calls[0].attempts = adapter.run_calls[0].limit
    coordinator.scan()
    assert gate_methods[-1] == "messages.getHistory"
    await coordinator._execute_slice(target)
    assert prepared_methods[-1] == "messages.getHistory"


@pytest.mark.asyncio
async def test_revision_keeps_budget_and_deadline_until_committed_progress() -> None:
    target = DemandKind.DELTA_GAP_FILL
    adapters = _adapters({target: DemandStatus(0)})
    adapters[target].outcome_kind = SliceOutcomeKind.NO_PROGRESS
    monotonic = _Clock(50.0)
    coordinator = TelegramDemandCoordinator(adapters, clock=_Clock(), monotonic_clock=monotonic)
    coordinator.scan()
    await coordinator._execute_slice(target)
    selected, invocation = next(iter(coordinator._invocations.items()))
    first_budget = invocation.budget
    first_deadline = invocation.deadline
    assert first_deadline is not None
    assert selected == first_budget.selected

    monotonic.value += 1
    adapters[target].selections = [WorkRevision(selected.key, "r2")]
    adapters[target].current_status = DemandStatus(0)
    coordinator.scan()
    await coordinator._execute_slice(target)
    second_budget = adapters[target].run_calls[1]
    assert second_budget.selected is not None
    assert second_budget is not first_budget
    assert second_budget.selected == WorkRevision(selected.key, "r2")
    second_deadline = coordinator._invocations[second_budget.selected].deadline
    assert second_deadline is not None and second_deadline > first_deadline


@pytest.mark.asyncio
async def test_gate_candidate_reentry_reuses_budget_and_renews_deadline() -> None:
    target = DemandKind.DELTA_GAP_FILL
    adapters = _adapters({target: DemandStatus(0, request_method="messages.getHistory")})
    adapters[target].run_error = TelegramRpcAdmissionDeferred(
        2, method_key="messages.getHistory", generation="g1", eligible_at=52.0, reason="server"
    )
    monotonic = _Clock(50.0)
    wall = _Clock(100.0)
    gate_held = False
    prepared: list[tuple[RpcAttemptBudget, str | None]] = []

    def prepare(budget: RpcAttemptBudget, method: str | None) -> bool:
        prepared.append((budget, method))
        return True

    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=wall,
        monotonic_clock=monotonic,
        prepare_durable_retry=prepare,
        rpc_eligibility=lambda method: (
            TelegramRpcAdmissionDeferred(2, method_key=method, generation="g1", eligible_at=52.0, reason="server")
            if gate_held and monotonic.value < 52.0
            else None
        ),
    )
    gate_held = True
    await coordinator._execute_slice(target)
    coordinator._active_kind = None
    wall.value = 10_000.0
    coordinator.scan()
    assert coordinator.ready_kinds == ()
    wall.value = 100.0
    selected, invocation = next(iter(coordinator._invocations.items()))
    budget = invocation.budget
    original_deadline = invocation.deadline
    assert original_deadline is not None
    budget.attempts = budget.limit
    assert prepared == [(budget, "messages.getHistory")]

    monotonic.value = 52.0
    coordinator.scan()
    assert coordinator.ready_kinds == (target,), (
        coordinator._excluded,
        coordinator._eligibility_waits,
        coordinator.statuses,
    )
    assert prepared[-1] == (budget, "messages.getHistory")
    assert selected not in coordinator._excluded
    await coordinator._execute_slice(target)
    assert adapters[target].run_calls == [budget, budget]
    assert invocation.deadline is not None and invocation.deadline > original_deadline


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
        def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
            self.status_calls.append(now)
            raise RuntimeError("status unavailable")

    adapters = _adapters({DURABLE_DEMAND_ORDER[1]: DemandStatus(0)})
    failing = Failing(DURABLE_DEMAND_ORDER[0])
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
    assert coordinator.next_release_at is None


@pytest.mark.asyncio
async def test_account_hold_keeps_coordinator_running_until_shutdown() -> None:
    first, second = DURABLE_DEMAND_ORDER[:2]
    shutdown = asyncio.Event()
    adapters = _adapters({first: DemandStatus(0), second: DemandStatus(0)})
    coordinator = TelegramDemandCoordinator(
        adapters,
        shutdown,
        clock=_Clock(),
        rpc_eligibility=lambda _method: TelegramRpcThrottled(latched=True),
    )
    task = asyncio.create_task(coordinator.run())
    await asyncio.sleep(0)
    assert coordinator.state is CoordinatorState.RUNNING
    assert adapters[first].run_calls == []
    assert adapters[second].run_calls == []
    shutdown.set()
    await task
    assert coordinator.state is CoordinatorState.STOPPED


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
    assert DemandKind.SCHEDULED_DISCOVERY not in coordinator.ready_kinds
    status = discovery.status(clock.value + 1)
    assert status is not None and status.release_at == float(now) and status.selected is not None
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
        writer=inline_writer(conn),
    )
    return conn, client, ScheduledDiscoveryDemandAdapter(reconciler, account_id=42)


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


@pytest.mark.asyncio
async def test_callback_execution_deadline_resumes_sibling_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    import mcp_telegram.telegram_demand_coordinator as module

    target, sibling = DURABLE_DEMAND_ORDER[:2]
    adapters = _adapters({target: DemandStatus(0), sibling: DemandStatus(0)})
    contract = module.demand_contract
    monkeypatch.setattr(
        module,
        "demand_contract",
        lambda kind: replace(
            contract(kind),
            admission_timeout_seconds=0.005,
        ),
    )
    cancelled = asyncio.Event()
    shutdown = asyncio.Event()

    async def slow_slice() -> None:
        try:
            await asyncio.sleep(0.05)
        finally:
            cancelled.set()

    async def sibling_slice() -> None:
        shutdown.set()

    adapters[target].on_run = slow_slice
    adapters[sibling].on_run = sibling_slice
    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=_Clock())
    await asyncio.wait_for(coordinator.run(), 0.5)
    assert cancelled.is_set()
    assert len(adapters[target].run_calls) == 1
    assert len(adapters[sibling].run_calls) == 1
    assert coordinator.state is CoordinatorState.STOPPED


@pytest.mark.asyncio
async def test_typed_next_method_survives_older_physical_method_until_a_new_send() -> None:
    target = DemandKind.HOT_ACTIVITY_PAGE
    adapters = _adapters({target: DemandStatus(0, request_method="advisory.A")})
    adapter = adapters[target]
    adapter.actual_method = "physical.A"
    adapter.run_error = TelegramRpcAdmissionDeferred(
        120, method_key="blocked.B", generation="g1", eligible_at=170, reason="method"
    )
    mono = _Clock(50)
    prepared: list[str | None] = []
    checked: list[str | None] = []

    def prepare(_budget: RpcAttemptBudget, method: str | None) -> bool:
        prepared.append(method)
        return method == "blocked.B" and mono.value >= 170

    def eligibility(method: str | None) -> None:
        checked.append(method)
        return

    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=_Clock(),
        monotonic_clock=mono,
        rpc_eligibility=eligibility,
        prepare_durable_retry=prepare,
    )
    await coordinator._execute_slice(target)
    coordinator._active_kind = None
    mono.value = 170
    coordinator.scan()
    assert checked[-1] == "blocked.B"
    adapter.run_error = None
    adapter.outcome_kind = SliceOutcomeKind.NO_PROGRESS
    adapter.actual_method = "physical.C"

    async def sent_new_method() -> None:
        adapter.run_calls[-1].debit()

    adapter.on_run = sent_new_method
    await coordinator._execute_slice(target)
    assert prepared == ["blocked.B", "blocked.B"]
    assert len(adapter.run_calls) == 2 and adapter.run_calls[0] is adapter.run_calls[1]
    assert adapter.selected not in coordinator._revision_methods
    coordinator._handle_throttle(target, adapter.run_calls[-1], TelegramRpcThrottled(1))
    assert checked[-1] == "physical.C"


@pytest.mark.asyncio
async def test_restored_method_floor_preserves_untouched_base_without_starting_deadline() -> None:
    target = DemandKind.HOT_ACTIVITY_PAGE
    adapters = _adapters({target: DemandStatus(0, request_method="restored.method")})
    mono = _Clock(50)
    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=_Clock(),
        monotonic_clock=mono,
        rpc_eligibility=lambda method: (
            TelegramRpcAdmissionDeferred(
                300, method_key=method, generation="restored", eligible_at=350, reason="method"
            )
            if mono.value < 350
            else None
        ),
        prepare_durable_retry=lambda _budget, _method: False,
    )
    selected = adapters[target].selected
    invocation = coordinator._invocations[selected]
    assert invocation.deadline is None and invocation.budget.attempts == 0
    assert not invocation.budget.can_prepare_retry
    mono.value = 350
    coordinator.scan()
    assert coordinator.ready_kinds == (target,)
    await coordinator._execute_slice(target)
    assert len(adapters[target].run_calls) == 1
    assert adapters[target].run_calls[0] is invocation.budget
    assert invocation.deadline is not None and invocation.deadline > mono.value


@pytest.mark.asyncio
@pytest.mark.parametrize("throttled", [False, True])
async def test_renewed_typed_method_wait_survives_no_send_outcome(throttled: bool) -> None:
    target = DemandKind.HOT_ACTIVITY_PAGE
    adapters = _adapters({target: DemandStatus(0, request_method="advisory.A")})
    adapter = adapters[target]
    adapter.actual_method = "physical.A"
    adapter.run_error = TelegramRpcAdmissionDeferred(
        120, method_key="blocked.B", generation="g1", eligible_at=170, reason="method"
    )
    mono = _Clock(50)
    renewed = False

    def eligibility(method: str | None) -> TelegramRpcAdmissionDeferred | None:
        if renewed and method == "blocked.B":
            return TelegramRpcAdmissionDeferred(
                10, method_key=method, generation="g1", eligible_at=180, reason="method"
            )
        return None

    coordinator = TelegramDemandCoordinator(
        adapters,
        clock=_Clock(),
        monotonic_clock=mono,
        rpc_eligibility=eligibility,
        prepare_durable_retry=lambda _budget, method: method == "blocked.B",
    )
    await coordinator._execute_slice(target)
    coordinator._active_kind = None
    mono.value = 170
    coordinator.scan()
    adapter.run_error = TelegramRpcThrottled(10) if throttled else None
    adapter.outcome_kind = SliceOutcomeKind.DEFERRED

    async def renew_hold_without_send() -> None:
        nonlocal renewed
        renewed = True

    adapter.on_run = renew_hold_without_send
    await coordinator._execute_slice(target)
    assert len(adapter.run_calls) == 2
    assert coordinator._revision_methods[adapter.selected] == "blocked.B"
    assert coordinator._eligibility_waits[adapter.selected] == 180
