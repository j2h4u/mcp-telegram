from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from mcp_telegram.self_profile_maintenance import (
    SelfProfileMaintenanceDemandAdapter,
    SelfProfileMaintenanceDependencies,
)
from mcp_telegram.startup_identity import (
    StartupIdentityPhase,
    StartupIdentityState,
    StartupIdentityUnavailableError,
)
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    current_demand_token,
)
from mcp_telegram.telegram_demand_progress import SliceOutcome
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope


async def _unused_get_me() -> object:
    return None


async def _unused_update_profile(me: object) -> None:
    del me


async def _unused_publish_startup_identity(_profile: object, _context: int) -> None:
    pass


@dataclass
class _Cadence:
    current: DemandStatus | None
    statuses: list[float] = field(default_factory=list)
    refreshed_at: list[float] = field(default_factory=list)
    generation: int = 0

    def status(self, now: float) -> DemandStatus | None:
        self.statuses.append(now)
        return self.current

    def business_revision(self) -> str:
        return str(self.generation)

    async def mark_refreshed(self, completed_at: float) -> str:
        self.refreshed_at.append(completed_at)
        self.generation += 1
        return str(self.generation)


async def _run(adapter: SelfProfileMaintenanceDemandAdapter, *, limit: int = 1) -> SliceOutcome:
    status = adapter.status(100.0)
    assert status is not None and status.selected is not None
    return await adapter.run_slice(RpcAttemptBudget(limit=limit, selected=status.selected))


async def test_status_reads_authoritative_cadence_without_side_effects() -> None:
    cadence = _Cadence(DemandStatus(release_at=50.0, freshness_deadline=75.0))
    dependencies = SelfProfileMaintenanceDependencies(
        cadence=cadence,
        get_me=_unused_get_me,
        update_profile=_unused_update_profile,
    )
    adapter = SelfProfileMaintenanceDemandAdapter(dependencies, account_id=42, clock=lambda: 100.0)

    status = adapter.status(100.0)

    assert status is not None and status.release_at == 50.0 and status.freshness_deadline == 75.0
    assert status.selected is not None and status.selected.revision == "0"
    assert cadence.statuses == [100.0]
    assert cadence.refreshed_at == []


@pytest.mark.asyncio
async def test_run_slice_skips_profile_fetch_before_release() -> None:
    cadence = _Cadence(DemandStatus(release_at=101.0))
    get_me_called = False

    async def get_me() -> object:
        nonlocal get_me_called
        get_me_called = True
        return SimpleNamespace(id=1)

    dependencies = SelfProfileMaintenanceDependencies(
        cadence=cadence,
        get_me=get_me,
        update_profile=_unused_update_profile,
    )
    adapter = SelfProfileMaintenanceDemandAdapter(dependencies, account_id=42, clock=lambda: 100.0)

    await _run(adapter)

    assert not get_me_called
    assert cadence.refreshed_at == []


@pytest.mark.asyncio
async def test_run_slice_fetches_and_applies_one_profile_under_exact_demand() -> None:
    cadence = _Cadence(DemandStatus(release_at=100.0))
    seen: dict[str, object] = {}
    profile = SimpleNamespace(id=42, username="account")

    async def get_me() -> object:
        token = current_demand_token()
        scope = current_rpc_scope()
        seen["kind"] = token.kind
        seen["acquisition"] = token.acquisition_kind
        seen["scope_kind"] = scope.demand_kind
        seen["budget"] = scope.attempt_budget
        assert scope.attempt_budget is not None
        scope.attempt_budget.debit()
        return profile

    async def update_profile(me: object) -> None:
        seen["profile"] = me

    dependencies = SelfProfileMaintenanceDependencies(
        cadence=cadence,
        get_me=get_me,
        update_profile=update_profile,
    )
    adapter = SelfProfileMaintenanceDemandAdapter(dependencies, account_id=42, clock=lambda: 100.0)
    status = adapter.status(100.0)
    assert status is not None and status.selected is not None
    budget = RpcAttemptBudget(limit=1, selected=status.selected)

    await adapter.run_slice(budget)

    assert seen["kind"] is DemandKind.SELF_PROFILE_MAINTENANCE
    assert seen["acquisition"] is AcquisitionKind.ACCOUNT_SELF_PROFILE
    assert seen["scope_kind"] is DemandKind.SELF_PROFILE_MAINTENANCE
    assert seen["budget"] is budget
    assert seen["profile"] is profile
    assert cadence.refreshed_at == [100.0]
    assert budget.attempts == 1


@pytest.mark.asyncio
async def test_run_slice_does_not_apply_profile_when_attempt_budget_is_exhausted() -> None:
    cadence = _Cadence(DemandStatus(release_at=100.0))
    applied: list[object] = []

    async def get_me() -> object:
        raise RpcAttemptBudgetExhaustedError("budget exhausted before get_me")

    async def update_profile(profile: object) -> None:
        applied.append(profile)

    dependencies = SelfProfileMaintenanceDependencies(
        cadence=cadence,
        get_me=get_me,
        update_profile=update_profile,
    )
    adapter = SelfProfileMaintenanceDemandAdapter(dependencies, account_id=42, clock=lambda: 100.0)

    await _run(adapter)

    assert applied == []
    assert cadence.refreshed_at == []


@pytest.mark.asyncio
async def test_startup_bypasses_cadence_with_one_identity_probe() -> None:
    cadence = _Cadence(DemandStatus(release_at=10_000.0))
    startup = StartupIdentityState(100.0, 200.0)
    profile = SimpleNamespace(id=42)
    observed: list[tuple[DemandKind, AcquisitionKind]] = []
    published: list[tuple[object, int]] = []

    async def get_me() -> object:
        token = current_demand_token()
        assert token.acquisition_kind is not None
        observed.append((token.kind, token.acquisition_kind))
        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        budget.debit()
        return profile

    async def publish(value: object, account_id: int) -> None:
        published.append((value, account_id))

    adapter = SelfProfileMaintenanceDemandAdapter(
        SelfProfileMaintenanceDependencies(cadence, get_me, _unused_update_profile, startup, publish),
        account_id=42,
        clock=lambda: 100.0,
    )
    await adapter.run_startup(RpcAttemptBudget(1))
    assert startup.phase is StartupIdentityPhase.READY
    assert startup.result().profile is profile and startup.result().account_id == 42
    assert published == [(profile, 42)]
    assert observed == [(DemandKind.SELF_PROFILE_MAINTENANCE, AcquisitionKind.ACCOUNT_SELF_PROFILE)]
    assert cadence.refreshed_at == []


@pytest.mark.asyncio
async def test_permanent_startup_profile_failure_signals_terminal_state() -> None:
    startup = StartupIdentityState(100.0, 200.0)

    async def get_me() -> object:
        raise RuntimeError("permanent")

    adapter = SelfProfileMaintenanceDemandAdapter(
        SelfProfileMaintenanceDependencies(
            cadence=_Cadence(DemandStatus(release_at=10_000.0)),
            get_me=get_me,
            update_profile=_unused_update_profile,
            startup=startup,
            publish_startup_identity=_unused_publish_startup_identity,
        ),
        account_id=42,
        clock=lambda: 100.0,
    )

    status = adapter.status(100.0)
    assert status is not None and status.selected is not None
    with pytest.raises(StartupIdentityUnavailableError, match="profile failed"):
        await adapter.run_slice(RpcAttemptBudget(limit=2, selected=status.selected))

    assert startup.phase is StartupIdentityPhase.FAILED
    assert startup.done_event.is_set()
    assert adapter.status(100.0) is None
