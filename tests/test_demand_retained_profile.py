from __future__ import annotations

import asyncio
import time

import pytest

from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget, current_demand_token
from mcp_telegram.telegram_demand_coordinator import CoordinatorState, TelegramDemandCoordinator, _Invocation
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind, WorkRevision
from mcp_telegram.telegram_rpc_consumers import DURABLE_DEMAND_ORDER, DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred
from test_telegram_demand_coordinator import _adapters


async def test_retained_profile_allows_inline_sibling_and_drains_on_shutdown() -> None:
    profile, sibling = DemandKind.ENTITY_PROFILE_REFRESH, DemandKind.DELTA_GAP_FILL
    adapters = _adapters({profile: DemandStatus(0), sibling: DemandStatus(0)})
    started, sibling_done, cancelled, drain = (asyncio.Event() for _ in range(4))
    tasks: dict[str, asyncio.Task[object] | None] = {}
    coordinator = TelegramDemandCoordinator(adapters)

    async def profile_wait() -> None:
        tasks["profile"] = asyncio.current_task()
        assert current_demand_token().owner_task is asyncio.current_task()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await drain.wait()

    async def sibling_work() -> None:
        tasks["sibling"] = asyncio.current_task()
        sibling_done.set()

    adapters[profile].on_run = profile_wait
    adapters[sibling].on_run = sibling_work
    adapters[sibling].outcome_kind = SliceOutcomeKind.NO_PROGRESS
    pump = asyncio.create_task(coordinator.run())
    await asyncio.wait_for(started.wait(), 2)
    for _ in range(12):
        assert not coordinator.offer(profile)
        coordinator.scan()
    await asyncio.wait_for(sibling_done.wait(), 2)
    assert len(adapters[profile].run_calls) == 1
    assert tasks["profile"] is not pump and tasks["sibling"] is pump
    assert coordinator.inspect_work(adapters[profile].selected)["active_for_target"] is True
    coordinator.shutdown()
    await asyncio.wait_for(cancelled.wait(), 2)
    assert coordinator.state is CoordinatorState.STOPPING
    assert not pump.done()
    drain.set()
    await asyncio.wait_for(pump, 2)
    assert coordinator.state is CoordinatorState.STOPPED


async def test_retained_profile_does_not_block_nine_ready_siblings() -> None:
    profile = DemandKind.ENTITY_PROFILE_REFRESH
    siblings = [kind for kind in DURABLE_DEMAND_ORDER if kind is not profile][:9]
    adapters = _adapters({kind: DemandStatus(0) for kind in [profile, *siblings]})
    started, release = asyncio.Event(), asyncio.Event()
    coordinator = TelegramDemandCoordinator(adapters)

    async def wait() -> None:
        started.set()
        await release.wait()

    adapters[profile].on_run = wait
    for kind in siblings:
        adapters[kind].outcome_kind = SliceOutcomeKind.NO_PROGRESS
    pump = asyncio.create_task(coordinator.run())
    await asyncio.wait_for(started.wait(), 2)
    async with asyncio.timeout(2):
        while any(not adapters[kind].run_calls for kind in siblings):
            await asyncio.sleep(0)
    assert len(adapters[profile].run_calls) == 1
    release.set()
    coordinator.shutdown()
    await pump


async def test_active_profile_revision_is_not_pruned_or_replaced() -> None:
    kind = DemandKind.ENTITY_PROFILE_REFRESH
    adapters = _adapters({kind: DemandStatus(0)})
    adapter = adapters[kind]
    started, release = asyncio.Event(), asyncio.Event()
    original = adapter.selected
    coordinator = TelegramDemandCoordinator(adapters)

    async def wait() -> None:
        started.set()
        await release.wait()

    adapter.on_run = wait
    coordinator._start_retained(kind)
    await started.wait()
    adapter.selections = [WorkRevision(original.key, "new")]
    coordinator.scan()
    assert original in coordinator._invocations
    assert len(adapter.run_calls) == 1
    assert coordinator.inspect_work(adapter.selections[0])["active_revision_matches"] is False
    release.set()
    task = coordinator._retained_task
    assert task is not None
    await task
    coordinator._harvest_retained()
    coordinator.scan()
    assert coordinator.statuses[kind].selected == adapter.selections[0]


async def test_completed_profile_is_not_reported_active_while_sibling_is_busy() -> None:
    profile, sibling = DemandKind.ENTITY_PROFILE_REFRESH, DemandKind.DELTA_GAP_FILL
    adapters = _adapters({profile: DemandStatus(0), sibling: DemandStatus(0)})
    profile_started, profile_release, sibling_started, sibling_release = (asyncio.Event() for _ in range(4))
    coordinator = TelegramDemandCoordinator(adapters)

    async def profile_wait() -> None:
        profile_started.set()
        await profile_release.wait()

    async def sibling_wait() -> None:
        sibling_started.set()
        await sibling_release.wait()

    adapters[profile].on_run = profile_wait
    adapters[sibling].on_run = sibling_wait
    adapters[sibling].outcome_kind = SliceOutcomeKind.NO_PROGRESS
    pump = asyncio.create_task(coordinator.run())
    await asyncio.wait_for(profile_started.wait(), 2)
    await asyncio.wait_for(sibling_started.wait(), 2)
    task = coordinator._retained_task
    assert task is not None
    profile_release.set()
    await task
    assert coordinator.inspect_work(adapters[profile].selected)["active_for_target"] is False
    assert len(adapters[profile].run_calls) == 1
    coordinator.shutdown()
    sibling_release.set()
    await pump


@pytest.mark.parametrize("claimed", [False, True])
async def test_parent_timeout_uses_current_root_physical_evidence(claimed: bool) -> None:
    kind = DemandKind.ENTITY_PROFILE_REFRESH
    adapters = _adapters({kind: DemandStatus(0)})
    adapter = adapters[kind]
    selected = adapter.selected
    budget = RpcAttemptBudget(limit=2, attempts=1, selected=selected)
    called: list[str | None] = []

    def expiry(method: str | None) -> TelegramRpcAdmissionDeferred:
        called.append(method)
        return TelegramRpcAdmissionDeferred(1, eligible_at=time.monotonic() + 1, reason="admission_expired")

    async def wait() -> None:
        if claimed:
            current_demand_token().attempt_evidence.record_dispatch()
            budget.debit()
        await asyncio.Event().wait()

    adapter.on_run = wait
    coordinator = TelegramDemandCoordinator(adapters, admission_expired_deferral=expiry)
    coordinator._invocations[selected] = _Invocation(budget, time.monotonic() + 0.05)
    coordinator._start_retained(kind)
    task = coordinator._retained_task
    assert task is not None
    await task
    assert called == ([] if claimed else [None])
    assert budget.attempts == (2 if claimed else 1)
    assert budget.limit == 2
    assert coordinator.inspect_work(selected)["reason"] == ("deadline_exceeded" if claimed else "admission_deferred")
    coordinator._harvest_retained()


async def test_local_timeout_is_not_parent_admission_expiry() -> None:
    kind = DemandKind.ENTITY_PROFILE_REFRESH
    adapters = _adapters({kind: DemandStatus(0)})
    adapters[kind].run_error = TimeoutError()
    called: list[bool] = []

    def expiry(_: str | None) -> TelegramRpcAdmissionDeferred:
        called.append(True)
        return TelegramRpcAdmissionDeferred(1, eligible_at=time.monotonic() + 1)

    coordinator = TelegramDemandCoordinator(adapters, admission_expired_deferral=expiry)
    coordinator._start_retained(kind)
    task = coordinator._retained_task
    assert task is not None
    await task
    assert called == []
    assert coordinator.inspect_work(adapters[kind].selected)["reason"] == "deadline_exceeded"
    coordinator._harvest_retained()


async def test_sender_expiry_wins_without_parent_factory_or_budget_change() -> None:
    kind = DemandKind.ENTITY_PROFILE_REFRESH
    adapters = _adapters({kind: DemandStatus(0)})
    adapters[kind].run_error = TelegramRpcAdmissionDeferred(
        1, eligible_at=time.monotonic() + 1, reason="admission_expired"
    )
    called: list[bool] = []

    def expiry(_: str | None) -> TelegramRpcAdmissionDeferred:
        called.append(True)
        return TelegramRpcAdmissionDeferred(1, eligible_at=time.monotonic() + 1)

    coordinator = TelegramDemandCoordinator(adapters, admission_expired_deferral=expiry)
    coordinator._start_retained(kind)
    task = coordinator._retained_task
    assert task is not None
    await task
    budget = adapters[kind].run_calls[0]
    assert called == []
    assert budget.attempts == 0 and budget.limit == 1
    assert coordinator.inspect_work(adapters[kind].selected)["reason"] == "admission_deferred"
    coordinator._harvest_retained()
