from __future__ import annotations

import asyncio
from collections import Counter, deque
from contextvars import ContextVar
from dataclasses import replace

import pytest

from mcp_telegram.config import RuntimeObservationConfig, TelegramRpcSchedulerConfig
from mcp_telegram.daemon import _record_rpc_admission
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from mcp_telegram.telegram_demand import AcquisitionKind, current_demand_token, demand_context
from mcp_telegram.telegram_rpc_consumers import TELEGRAM_DEMAND_CONTRACTS, DemandKind, demand_contract
from mcp_telegram.telegram_rpc_scheduler import (
    LEGACY_DEMAND_KIND_BY_SOURCE,
    RPC_SOURCE_SERVICE_CLASS,
    RpcAdmission,
    RpcAdmissionClosedError,
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    RpcServiceClass,
    RpcTransportReadiness,
    TelegramRpcAdmissionScheduler,
    TelegramRpcScope,
    TelegramRpcSource,
    UnclassifiedTelegramRpcError,
    create_detached_rpc_task,
    current_rpc_scope,
    rpc_scope,
)


def test_entity_profile_refresh_uses_background_service_class() -> None:
    assert RPC_SOURCE_SERVICE_CLASS[TelegramRpcSource.ENTITY_INFO_FOREGROUND] is RpcServiceClass.INTERACTIVE
    assert RPC_SOURCE_SERVICE_CLASS[TelegramRpcSource.ENTITY_INFO_REFRESH] is RpcServiceClass.BACKGROUND


def test_legacy_bridge_is_explicit_complete_and_contract_consistent() -> None:
    assert set(LEGACY_DEMAND_KIND_BY_SOURCE) == set(TelegramRpcSource)
    assert set(LEGACY_DEMAND_KIND_BY_SOURCE.values()) <= set(TELEGRAM_DEMAND_CONTRACTS)
    assert all(
        demand_contract(kind).source is source for source, kind in LEGACY_DEMAND_KIND_BY_SOURCE.items()
    )


def test_legacy_deadline_only_tightens_contract_and_nested_scope_keeps_root_identity() -> None:
    now = 100.0
    contract = demand_contract(DemandKind.MCP_REMOTE_ACQUISITION)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("mcp_telegram.telegram_demand.time.monotonic", lambda: now)
        with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE, deadline=999.0) as root:
            assert root.deadline == now + contract.admission_timeout_seconds
            with rpc_scope(
                TelegramRpcSource.FULL_SYNC,
                deadline=108.0,
                acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
            ) as nested:
                token = current_demand_token()
                assert nested.demand_kind is DemandKind.MCP_REMOTE_ACQUISITION
                assert nested.source is TelegramRpcSource.MCP_INTERACTIVE
                assert nested.service_class is RpcServiceClass.INTERACTIVE
                assert nested.deadline == 108.0
                assert nested.acquisition_kind is AcquisitionKind.ENTITY_LOOKUP
                assert token.kind is nested.demand_kind
                assert token.source is nested.source


class _ControlledLimiter:
    def __init__(self) -> None:
        self._waiters: deque[asyncio.Future[None]] = deque()
        self.acquisitions = 0

    async def acquire(self) -> None:
        self.acquisitions += 1
        future = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        await future

    async def allow_one(self) -> None:
        await _wait_until(lambda: bool(self._waiters))
        self._waiters.popleft().set_result(None)
        await asyncio.sleep(0)


class _FailAfterOneLimiter:
    def __init__(self) -> None:
        self.acquisitions = 0

    async def acquire(self) -> None:
        self.acquisitions += 1
        if self.acquisitions > 1:
            raise RuntimeError("limiter failed")


async def _wait_until(predicate: object, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


def _scope(source: TelegramRpcSource, *, deadline: float | None = None) -> TelegramRpcScope:
    with rpc_scope(source, deadline=deadline):
        return current_rpc_scope()


@pytest.mark.asyncio
async def test_waiting_background_does_not_reserve_next_limiter_token_from_interactive() -> None:
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=TelegramRpcSchedulerConfig(), limiter=limiter)
    background = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC)))
    await _wait_until(lambda: limiter.acquisitions == 1)

    interactive = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.MCP_INTERACTIVE)))
    await _wait_until(lambda: scheduler.queue_depths()[RpcServiceClass.INTERACTIVE] == 1)
    await limiter.allow_one()

    admission = await interactive
    assert admission.service_class is RpcServiceClass.INTERACTIVE
    assert not background.done()

    background.cancel()
    await asyncio.gather(background, return_exceptions=True)
    await scheduler.close()


@pytest.mark.asyncio
async def test_full_backlog_dispatches_exact_configured_weight_cycle_and_all_classes_progress() -> None:
    policy = TelegramRpcSchedulerConfig(interactive_weight=5, live_sync_weight=3, background_weight=2)
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=policy, limiter=limiter)
    sources = (
        TelegramRpcSource.MCP_INTERACTIVE,
        TelegramRpcSource.REALTIME_EVENT,
        TelegramRpcSource.FULL_SYNC,
    )
    admitted: list[RpcServiceClass] = []

    async def admit(source: TelegramRpcSource) -> None:
        admission = await scheduler.admit(_scope(source))
        admitted.append(admission.service_class)

    tasks = [asyncio.create_task(admit(source)) for source in sources for _ in range(8)]
    await _wait_until(lambda: sum(scheduler.queue_depths().values()) == 24)

    for expected_count in range(1, len(scheduler.fair_cycle) + 1):
        await limiter.allow_one()
        await _wait_until(lambda expected_count=expected_count: len(admitted) == expected_count)

    assert admitted == list(scheduler.fair_cycle)
    assert Counter(admitted) == {
        RpcServiceClass.INTERACTIVE: 5,
        RpcServiceClass.LIVE_SYNC: 3,
        RpcServiceClass.BACKGROUND: 2,
    }

    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await scheduler.close()


@pytest.mark.asyncio
async def test_interactive_admission_stays_within_configured_worst_case_weight_bound() -> None:
    """A late interactive ticket cannot wait behind more than one fair cycle."""
    policy = TelegramRpcSchedulerConfig(
        interactive_weight=1,
        live_sync_weight=5,
        background_weight=4,
        interactive_queue_capacity=64,
        live_sync_queue_capacity=64,
        background_queue_capacity=64,
    )
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=policy, limiter=limiter)
    admitted: list[RpcAdmission] = []

    async def admit(source: TelegramRpcSource) -> RpcAdmission:
        admission = await scheduler.admit(_scope(source))
        admitted.append(admission)
        return admission

    # Keep both non-interactive classes continuously backlogged and use one
    # warmup interactive ticket to position the fair cursor immediately after
    # its sole slot in the configured cycle.
    backlog = [asyncio.create_task(admit(TelegramRpcSource.REALTIME_EVENT)) for _ in range(16)] + [
        asyncio.create_task(admit(TelegramRpcSource.FULL_SYNC)) for _ in range(8)
    ]
    warmup = asyncio.create_task(admit(TelegramRpcSource.MCP_INTERACTIVE))
    await _wait_until(lambda: sum(scheduler.queue_depths().values()) == 25)

    cycle = scheduler.fair_cycle
    interactive_positions = [index for index, item in enumerate(cycle) if item is RpcServiceClass.INTERACTIVE]
    worst_case_slots = max(
        (next_index - index) if next_index > index else (next_index + len(cycle) - index)
        for index, next_index in zip(
            interactive_positions, interactive_positions[1:] + interactive_positions[:1], strict=True
        )
    )
    assert worst_case_slots == len(cycle) == 10

    warmup_slots = cycle.index(RpcServiceClass.INTERACTIVE) + 1
    for expected_count in range(1, warmup_slots):
        await limiter.allow_one()
        await _wait_until(lambda expected_count=expected_count: len(admitted) == expected_count)
    await limiter.allow_one()
    await warmup

    initial_count = len(admitted)
    target = asyncio.create_task(admit(TelegramRpcSource.MCP_INTERACTIVE))
    await _wait_until(lambda: scheduler.queue_depths()[RpcServiceClass.INTERACTIVE] == 1)
    for offset in range(1, worst_case_slots):
        await limiter.allow_one()
        await _wait_until(lambda expected_count=initial_count + offset: len(admitted) == expected_count)
        assert not target.done()
    await limiter.allow_one()
    await target

    assert admitted[-1].service_class is RpcServiceClass.INTERACTIVE
    assert len(admitted) == initial_count + worst_case_slots

    for task in [*backlog, target]:
        if not task.done():
            task.cancel()
    await asyncio.gather(*backlog, target, return_exceptions=True)
    for admission in admitted:
        scheduler.complete(admission)
    await scheduler.close()


def test_fair_cycle_normalizes_equivalent_weight_ratio() -> None:
    scheduler = TelegramRpcAdmissionScheduler(
        policy=TelegramRpcSchedulerConfig(interactive_weight=60, live_sync_weight=30, background_weight=10),
        limiter=None,
    )

    assert len(scheduler.fair_cycle) == 10
    assert Counter(scheduler.fair_cycle) == {
        RpcServiceClass.INTERACTIVE: 6,
        RpcServiceClass.LIVE_SYNC: 3,
        RpcServiceClass.BACKGROUND: 1,
    }


@pytest.mark.asyncio
async def test_fifo_is_preserved_within_one_service_class() -> None:
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=TelegramRpcSchedulerConfig(), limiter=limiter)
    completed: list[int] = []

    async def admit(label: int) -> None:
        await scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC))
        completed.append(label)

    tasks = [asyncio.create_task(admit(label)) for label in range(3)]
    await _wait_until(lambda: scheduler.queue_depths()[RpcServiceClass.BACKGROUND] == 3)
    for expected_count in range(1, 4):
        await limiter.allow_one()
        await _wait_until(lambda expected_count=expected_count: len(completed) == expected_count)

    await asyncio.gather(*tasks)
    assert completed == [0, 1, 2]
    await scheduler.close()


@pytest.mark.asyncio
async def test_same_class_sources_rotate_while_each_source_remains_fifo() -> None:
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=TelegramRpcSchedulerConfig(), limiter=limiter)
    admitted: list[str] = []

    async def admit(source: TelegramRpcSource, label: str) -> None:
        await scheduler.admit(_scope(source))
        admitted.append(label)

    tasks = [
        asyncio.create_task(admit(TelegramRpcSource.FULL_SYNC, "full-1")),
        asyncio.create_task(admit(TelegramRpcSource.FULL_SYNC, "full-2")),
        asyncio.create_task(admit(TelegramRpcSource.ACTIVITY_ARCHIVE, "archive-1")),
        asyncio.create_task(admit(TelegramRpcSource.ACTIVITY_ARCHIVE, "archive-2")),
    ]
    await _wait_until(lambda: scheduler.queue_depths()[RpcServiceClass.BACKGROUND] == 4)

    for expected_count in range(1, 5):
        await limiter.allow_one()
        await _wait_until(lambda expected_count=expected_count: len(admitted) == expected_count)

    assert admitted == ["full-1", "archive-1", "full-2", "archive-2"]
    await asyncio.gather(*tasks)
    await scheduler.close()


@pytest.mark.asyncio
async def test_source_bound_rejects_one_producer_without_blocking_its_class_peer() -> None:
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=TelegramRpcSchedulerConfig(), limiter=limiter)
    source_limit = demand_contract(DemandKind.FULL_SYNC_PAGE).source_outstanding_limit
    full_sync = [
        asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC)))
        for _ in range(source_limit)
    ]
    await _wait_until(lambda: scheduler.source_queue_depths()[TelegramRpcSource.FULL_SYNC] == source_limit)

    with pytest.raises(RpcAdmissionSaturatedError, match="source outstanding capacity"):
        await scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC))

    peer = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.ACTIVITY_ARCHIVE)))
    for _ in range(2):
        await limiter.allow_one()
        if peer.done():
            break
    peer_admission = await peer
    assert peer_admission.source is TelegramRpcSource.ACTIVITY_ARCHIVE

    for task in full_sync:
        task.cancel()
    await asyncio.gather(*full_sync, return_exceptions=True)
    scheduler.complete(peer_admission)
    await scheduler.close()


@pytest.mark.asyncio
async def test_scheduler_rejects_caller_overrides_of_registered_transport_policy() -> None:
    scheduler = TelegramRpcAdmissionScheduler(policy=TelegramRpcSchedulerConfig(), limiter=None)
    scope = _scope(TelegramRpcSource.FULL_SYNC)

    with pytest.raises(ValueError, match="source outstanding limit"):
        await scheduler.admit(replace(scope, source_outstanding_limit=999))
    with pytest.raises(ValueError, match="code-owned demand contract"):
        await scheduler.admit(replace(scope, service_class=RpcServiceClass.INTERACTIVE))

    await scheduler.close()


@pytest.mark.asyncio
async def test_nested_attempt_reenters_same_source_without_consuming_another_slot() -> None:
    policy = replace(TelegramRpcSchedulerConfig(), background_queue_capacity=1)
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=policy, limiter=limiter)
    scope = _scope(TelegramRpcSource.FULL_SYNC)
    active_ready = asyncio.Event()
    allow_nested = asyncio.Event()
    nested_done = asyncio.Event()

    async def root_operation() -> None:
        outer = await scheduler.admit(scope)
        active_ready.set()
        await allow_nested.wait()
        inner = await scheduler.admit(scope)
        assert scheduler.source_outstanding_depths()[TelegramRpcSource.FULL_SYNC] == 1
        scheduler.complete(inner)
        scheduler.complete(outer)
        nested_done.set()

    root = asyncio.create_task(root_operation())
    await limiter.allow_one()
    await active_ready.wait()
    allow_nested.set()
    await _wait_until(lambda: scheduler.source_queue_depths()[TelegramRpcSource.FULL_SYNC] == 1)
    await limiter.allow_one()
    await asyncio.wait_for(nested_done.wait(), timeout=1.0)

    await root
    await scheduler.close()


@pytest.mark.asyncio
async def test_background_saturation_is_typed_and_does_not_consume_other_class_capacity() -> None:
    policy = replace(
        TelegramRpcSchedulerConfig(),
        background_queue_capacity=2,
        interactive_queue_capacity=1,
        live_sync_queue_capacity=1,
    )
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=policy, limiter=limiter)
    backgrounds = [asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC))) for _ in range(2)]
    await _wait_until(lambda: scheduler.queue_depths()[RpcServiceClass.BACKGROUND] == 2)

    with pytest.raises(RpcAdmissionSaturatedError) as caught:
        await scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC))
    assert caught.value.service_class is RpcServiceClass.BACKGROUND

    interactive = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.MCP_INTERACTIVE)))
    await limiter.allow_one()
    assert (await interactive).service_class is RpcServiceClass.INTERACTIVE

    for task in backgrounds:
        task.cancel()
    await asyncio.gather(*backgrounds, return_exceptions=True)
    await scheduler.close()


@pytest.mark.asyncio
async def test_active_attempt_counts_toward_capacity_without_burning_another_limiter_token() -> None:
    policy = replace(TelegramRpcSchedulerConfig(), background_queue_capacity=1)
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=policy, limiter=limiter)
    first_task = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC)))
    await limiter.allow_one()
    first = await first_task

    assert scheduler.active_depths()[RpcServiceClass.BACKGROUND] == 1
    assert scheduler.outstanding_depths()[RpcServiceClass.BACKGROUND] == 1
    with pytest.raises(RpcAdmissionSaturatedError, match="outstanding capacity"):
        await scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC))
    assert limiter.acquisitions == 1

    scheduler.complete(first)
    assert scheduler.outstanding_depths()[RpcServiceClass.BACKGROUND] == 0
    await scheduler.close()


@pytest.mark.asyncio
async def test_dispatcher_rechecks_readiness_after_limiter_before_releasing_ticket() -> None:
    ready = True
    ready_event = asyncio.Event()
    ready_event.set()
    limiter = _ControlledLimiter()
    events: list[RpcAdmissionEvent] = []

    async def wait_until_ready() -> None:
        await ready_event.wait()

    scheduler = TelegramRpcAdmissionScheduler(
        policy=TelegramRpcSchedulerConfig(),
        limiter=limiter,
        observer=events.append,
        readiness=RpcTransportReadiness(probe=lambda: ready, wait=wait_until_ready),
    )
    waiting = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC)))
    await _wait_until(lambda: limiter.acquisitions == 1)

    ready = False
    ready_event.clear()
    await limiter.allow_one()
    await asyncio.sleep(0)
    assert not waiting.done()
    assert scheduler.active_depths()[RpcServiceClass.BACKGROUND] == 0
    assert not any(event.kind is RpcAdmissionEventKind.DISPATCHED for event in events)

    ready = True
    ready_event.set()
    await _wait_until(lambda: limiter.acquisitions == 2)
    await limiter.allow_one()
    admission = await waiting
    assert admission.service_class is RpcServiceClass.BACKGROUND
    scheduler.record_dispatch(admission)
    assert [event.kind for event in events].count(RpcAdmissionEventKind.DISPATCHED) == 1
    scheduler.complete(admission)
    await scheduler.close()


@pytest.mark.asyncio
async def test_cancelled_ticket_is_removed_and_never_dispatched_later() -> None:
    limiter = _ControlledLimiter()
    events: list[RpcAdmissionEvent] = []
    scheduler = TelegramRpcAdmissionScheduler(
        policy=TelegramRpcSchedulerConfig(), limiter=limiter, observer=events.append
    )
    background = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC)))
    await _wait_until(lambda: limiter.acquisitions == 1)
    background.cancel()
    await asyncio.gather(background, return_exceptions=True)
    assert scheduler.queue_depths()[RpcServiceClass.BACKGROUND] == 0

    interactive = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.MCP_INTERACTIVE)))
    await limiter.allow_one()
    admission = await interactive
    scheduler.record_dispatch(admission)

    dispatched_sources = [event.source for event in events if event.kind is RpcAdmissionEventKind.DISPATCHED]
    assert dispatched_sources == [TelegramRpcSource.MCP_INTERACTIVE]
    assert any(event.kind is RpcAdmissionEventKind.CANCELLED for event in events)
    await scheduler.close()


@pytest.mark.asyncio
async def test_fake_clock_expiry_removes_ticket_before_dispatch() -> None:
    now = [100.0]
    limiter = _ControlledLimiter()
    events: list[RpcAdmissionEvent] = []
    scheduler = TelegramRpcAdmissionScheduler(
        policy=TelegramRpcSchedulerConfig(),
        limiter=limiter,
        observer=events.append,
        clock=lambda: now[0],
    )
    waiting = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.REALTIME_EVENT, deadline=105.0)))
    await _wait_until(lambda: limiter.acquisitions == 1)

    now[0] = 106.0
    assert scheduler.expire_due() == 1
    with pytest.raises(RpcAdmissionExpiredError):
        await waiting
    assert scheduler.queue_depths()[RpcServiceClass.LIVE_SYNC] == 0
    assert not any(event.kind is RpcAdmissionEventKind.DISPATCHED for event in events)
    assert [event.wait_seconds for event in events if event.kind is RpcAdmissionEventKind.EXPIRED] == [6.0]
    await scheduler.close()


@pytest.mark.asyncio
async def test_dispatcher_rechecks_deadline_after_queued_observer_returns() -> None:
    now = [100.0]
    limiter = _ControlledLimiter()
    events: list[RpcAdmissionEvent] = []

    def observe(event: RpcAdmissionEvent) -> None:
        events.append(event)
        if event.kind is RpcAdmissionEventKind.QUEUED:
            now[0] = 101.0

    scheduler = TelegramRpcAdmissionScheduler(
        policy=TelegramRpcSchedulerConfig(),
        limiter=limiter,
        observer=observe,
        clock=lambda: now[0],
    )

    with pytest.raises(RpcAdmissionExpiredError):
        await scheduler.admit(_scope(TelegramRpcSource.MCP_INTERACTIVE, deadline=100.5))

    assert limiter.acquisitions == 0
    assert scheduler.queue_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert [event.kind for event in events] == [RpcAdmissionEventKind.QUEUED, RpcAdmissionEventKind.EXPIRED]
    await scheduler.close()


@pytest.mark.asyncio
async def test_cancelled_and_closed_tickets_report_terminal_queue_wait() -> None:
    now = [100.0]
    limiter = _ControlledLimiter()
    events: list[RpcAdmissionEvent] = []
    scheduler = TelegramRpcAdmissionScheduler(
        policy=TelegramRpcSchedulerConfig(),
        limiter=limiter,
        observer=events.append,
        clock=lambda: now[0],
    )
    cancelled = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC)))
    closed = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.REALTIME_EVENT)))
    await _wait_until(lambda: sum(scheduler.queue_depths().values()) == 2)

    now[0] = 104.5
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    now[0] = 107.0
    await scheduler.close()

    terminal_waits = {
        event.kind: event.wait_seconds
        for event in events
        if event.kind in {RpcAdmissionEventKind.CANCELLED, RpcAdmissionEventKind.CLOSED}
    }
    assert terminal_waits == {
        RpcAdmissionEventKind.CANCELLED: 4.5,
        RpcAdmissionEventKind.CLOSED: 7.0,
    }
    with pytest.raises(RpcAdmissionClosedError):
        await closed


@pytest.mark.asyncio
async def test_close_rejects_waiters_and_leaves_no_dispatcher() -> None:
    limiter = _ControlledLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=TelegramRpcSchedulerConfig(), limiter=limiter)
    waiting = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC)))
    await _wait_until(lambda: limiter.acquisitions == 1)

    await scheduler.close()
    with pytest.raises(RpcAdmissionClosedError):
        await waiting
    assert scheduler.queue_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert not any(task.get_name() == "telegram-rpc-admission" and not task.done() for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_limiter_failure_closes_waiters_and_cancels_active_owner() -> None:
    limiter = _FailAfterOneLimiter()
    scheduler = TelegramRpcAdmissionScheduler(policy=TelegramRpcSchedulerConfig(), limiter=limiter)
    active_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def hold_active() -> None:
        await scheduler.admit(_scope(TelegramRpcSource.MCP_INTERACTIVE))
        active_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    active_owner = asyncio.create_task(hold_active())
    await active_started.wait()
    waiting = asyncio.create_task(scheduler.admit(_scope(TelegramRpcSource.FULL_SYNC)))

    with pytest.raises(RpcAdmissionClosedError, match="limiter failed"):
        await waiting
    await cleanup_started.wait()
    first_close = asyncio.create_task(scheduler.close())
    second_close = asyncio.create_task(scheduler.close())
    await asyncio.sleep(0)
    assert not first_close.done()
    assert not second_close.done()

    release_cleanup.set()
    await asyncio.gather(first_close, second_close)
    await asyncio.gather(active_owner, return_exceptions=True)

    assert active_owner.cancelled()
    assert scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert not any(
        task.get_name() in {"telegram-rpc-admission", "telegram-rpc-shutdown"} and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_detached_task_must_replace_inherited_scope_and_deadline() -> None:
    request_context: ContextVar[str] = ContextVar("request_context", default="clean")

    async def inspect_scope() -> tuple[TelegramRpcScope, str]:
        return current_rpc_scope(), request_context.get()

    with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE):
        request_context.set("caller")
        inherited = asyncio.create_task(inspect_scope())
        explicit = create_detached_rpc_task(
            inspect_scope(),
            source=TelegramRpcSource.ENTITY_INFO_REFRESH,
            timeout_seconds=10.0,
        )

    with pytest.raises(UnclassifiedTelegramRpcError, match="inherited another task"):
        await inherited
    detached_scope, detached_context = await explicit
    assert detached_scope.source is TelegramRpcSource.ENTITY_INFO_REFRESH
    assert detached_scope.deadline is not None
    assert detached_scope.owner_task is explicit
    assert detached_context == "clean"


@pytest.mark.asyncio
async def test_detached_task_can_explicitly_transfer_precise_root_demand() -> None:
    async def inspect_scope() -> TelegramRpcScope:
        return current_rpc_scope()

    with demand_context(DemandKind.SCHEDULED_DISCOVERY) as root:
        detached = create_detached_rpc_task(
            inspect_scope(),
            source=root.source,
            timeout_seconds=10.0,
            demand_token=root,
        )

    scope = await detached
    assert scope.demand_kind is DemandKind.SCHEDULED_DISCOVERY
    assert scope.source is root.source
    assert scope.service_class is root.service_class
    assert scope.owner_task is detached
    assert scope.deadline is not None and scope.deadline <= root.admission_deadline


def test_daemon_observer_forwards_dispatch_event_to_aggregator() -> None:
    class _Recorder:
        def __init__(self) -> None:
            self.rows: list[dict[str, object]] = []

        def record(self, **values: object) -> None:
            self.rows.append(values)

    recorder = _Recorder()
    observer = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    event = RpcAdmissionEvent(
        kind=RpcAdmissionEventKind.DISPATCHED,
        source=TelegramRpcSource.MCP_INTERACTIVE,
        service_class=RpcServiceClass.INTERACTIVE,
        queue_depth=2,
        total_depth=5,
        wait_seconds=0.125,
    )

    _record_rpc_admission(observer, event)
    observer.flush(now=300.0)

    assert recorder.rows[0]["result_count"] == 1
