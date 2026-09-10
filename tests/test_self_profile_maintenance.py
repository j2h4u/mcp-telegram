from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from mcp_telegram.own_only import OwnOnlyContext
from mcp_telegram.self_profile_maintenance import (
    SelfProfileMaintenanceDemandAdapter,
    SelfProfileMaintenanceDependencies,
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
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope


async def _unused_get_me() -> object:
    return None


def _unused_update_profile(me: object) -> None:
    del me


@dataclass
class _Cadence:
    current: DemandStatus | None
    statuses: list[float] = field(default_factory=list)
    refreshed_at: list[float] = field(default_factory=list)

    def status(self, now: float) -> DemandStatus | None:
        self.statuses.append(now)
        return self.current

    def mark_refreshed(self, completed_at: float) -> None:
        self.refreshed_at.append(completed_at)


async def test_status_reads_authoritative_cadence_without_side_effects() -> None:
    cadence = _Cadence(DemandStatus(release_at=50.0, freshness_deadline=75.0))
    dependencies = SelfProfileMaintenanceDependencies(
        cadence=cadence,
        get_me=_unused_get_me,
        update_profile=_unused_update_profile,
    )
    adapter = SelfProfileMaintenanceDemandAdapter(dependencies, clock=lambda: 100.0)

    status = adapter.status(100.0)

    assert status == DemandStatus(release_at=50.0, freshness_deadline=75.0)
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
    adapter = SelfProfileMaintenanceDemandAdapter(dependencies, clock=lambda: 100.0)

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert not get_me_called
    assert cadence.refreshed_at == []


@pytest.mark.asyncio
async def test_run_slice_fetches_and_applies_one_profile_under_exact_demand() -> None:
    cadence = _Cadence(DemandStatus(release_at=100.0))
    seen: dict[str, object] = {}
    now_values = iter((100.0, 101.0))
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

    def update_profile(me: object) -> None:
        seen["profile"] = me

    dependencies = SelfProfileMaintenanceDependencies(
        cadence=cadence,
        get_me=get_me,
        update_profile=update_profile,
    )
    adapter = SelfProfileMaintenanceDemandAdapter(dependencies, clock=lambda: next(now_values))
    budget = RpcAttemptBudget(limit=1)

    await adapter.run_slice(budget)

    assert seen["kind"] is DemandKind.SELF_PROFILE_MAINTENANCE
    assert seen["acquisition"] is AcquisitionKind.ACCOUNT_SELF_PROFILE
    assert seen["scope_kind"] is DemandKind.SELF_PROFILE_MAINTENANCE
    assert seen["budget"] is budget
    assert seen["profile"] is profile
    assert cadence.refreshed_at == [101.0]
    assert budget.attempts == 1


@pytest.mark.asyncio
async def test_run_slice_does_not_apply_profile_when_attempt_budget_is_exhausted() -> None:
    cadence = _Cadence(DemandStatus(release_at=100.0))
    applied: list[object] = []

    async def get_me() -> object:
        raise RpcAttemptBudgetExhaustedError("budget exhausted before get_me")

    dependencies = SelfProfileMaintenanceDependencies(
        cadence=cadence,
        get_me=get_me,
        update_profile=applied.append,
    )
    adapter = SelfProfileMaintenanceDemandAdapter(dependencies, clock=lambda: 100.0)

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert applied == []
    assert cadence.refreshed_at == []


@pytest.mark.asyncio
async def test_startup_bypasses_persisted_cadence_and_resumes_after_budget_split() -> None:
    cadence = _Cadence(DemandStatus(release_at=10_000.0))
    startup = StartupIdentityState(100.0, 200.0)
    profile = SimpleNamespace(id=42, username="account")
    input_user = object()
    applied_profiles: list[object] = []
    applied_contexts: list[OwnOnlyContext] = []
    observed: list[tuple[DemandKind, AcquisitionKind]] = []

    def observe_scope() -> None:
        token = current_demand_token()
        scope = current_rpc_scope()
        assert token.acquisition_kind is not None
        assert scope.attempt_budget is not None
        observed.append((token.kind, token.acquisition_kind))

    async def get_me() -> object:
        observe_scope()
        current_rpc_scope().attempt_budget.debit()  # type: ignore[union-attr]
        return profile

    async def get_input_entity(account_id: int) -> object:
        observe_scope()
        assert account_id == 42
        return input_user

    async def get_full_user(received: object) -> object:
        observe_scope()
        assert received is input_user
        current_rpc_scope().attempt_budget.debit()  # type: ignore[union-attr]
        return SimpleNamespace(full_user=SimpleNamespace(personal_channel_id=9001))

    def publish_startup_identity(applied_profile: object, context: OwnOnlyContext) -> None:
        applied_profiles.append(applied_profile)
        applied_contexts.append(context)

    adapter = SelfProfileMaintenanceDemandAdapter(
        SelfProfileMaintenanceDependencies(
            cadence=cadence,
            get_me=get_me,
            update_profile=applied_profiles.append,
            startup=startup,
            get_input_entity=get_input_entity,
            get_full_user=get_full_user,
            publish_startup_identity=publish_startup_identity,
        ),
        clock=lambda: 100.0,
    )

    assert adapter.status(100.0) == DemandStatus(release_at=0.0, freshness_deadline=200.0)
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert startup.phase is StartupIdentityPhase.FULL_USER
    assert applied_profiles == []

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert startup.phase is StartupIdentityPhase.READY
    assert startup.done_event.is_set()
    assert startup.result().profile is profile
    assert startup.result().own_only_context.account_id == 42
    assert startup.result().own_only_context.personal_channel_id == -1000000009001
    assert applied_profiles == [profile]
    assert applied_contexts == [startup.result().own_only_context]
    assert cadence.refreshed_at == [100.0]
    assert adapter.status(100.0) == DemandStatus(release_at=10_000.0)
    assert observed == [(DemandKind.SELF_PROFILE_MAINTENANCE, AcquisitionKind.ACCOUNT_SELF_PROFILE)] * 4


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
            get_input_entity=lambda _account_id: _unused_get_me(),
            get_full_user=lambda _input_user: _unused_get_me(),
            publish_startup_identity=lambda _profile, _context: None,
        ),
        clock=lambda: 100.0,
    )

    with pytest.raises(StartupIdentityUnavailableError, match="profile failed"):
        await adapter.run_slice(RpcAttemptBudget(limit=2))

    assert startup.phase is StartupIdentityPhase.FAILED
    assert startup.done_event.is_set()
    assert adapter.status(100.0) is None
