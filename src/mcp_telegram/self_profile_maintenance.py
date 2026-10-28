"""Durable adapter for startup identity and periodic self-profile maintenance."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

from .entity_profile.contracts import ProjectionStatus, TargetKind, UserProfileObservation
from .entity_profile.ports import UserProfilePort
from .flood import TelegramRpcThrottled
from .own_only_contracts import OwnOnlyContext
from .startup_identity import (
    StartupIdentityPhase,
    StartupIdentityResult,
    StartupIdentityState,
    StartupIdentityUnavailableError,
)
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    durable_demand_context,
)
from .telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    rpc_attempt_budget,
)


class SelfProfileCadenceState(Protocol):
    """Authoritative cadence state owned by the account domain."""

    def status(self, now: float) -> DemandStatus | None:
        """Return the next due boundary without changing local state."""
        ...

    def business_revision(self) -> str:
        """Return the persisted profile observation generation, not a clock value."""
        ...

    async def mark_refreshed(self, completed_at: float) -> str:
        """Commit and return the generation for one accepted profile observation."""
        ...


class _MeLike(Protocol):
    id: int


@dataclass(frozen=True, slots=True)
class SelfProfileMaintenanceDependencies:
    """Narrow dependency port for startup identity and periodic refresh."""

    cadence: SelfProfileCadenceState
    get_me: Callable[[], Awaitable[object]]
    update_profile: Callable[[object], Awaitable[None]]
    startup: StartupIdentityState | None = None
    get_input_entity: Callable[[int], Awaitable[object]] | None = None
    user_profile_port: UserProfilePort | None = None
    publish_startup_identity: Callable[[object, OwnOnlyContext], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        if self.startup is None:
            return
        if self.get_input_entity is None or self.user_profile_port is None or self.publish_startup_identity is None:
            raise ValueError("startup identity requires all own-only dependencies")


class SelfProfileMaintenanceDemandAdapter(DurableDemandAdapter):
    """Acquire mandatory startup identity, then maintain its periodic profile."""

    demand_kind = DemandKind.SELF_PROFILE_MAINTENANCE

    def __init__(
        self,
        dependencies: SelfProfileMaintenanceDependencies,
        *,
        account_id: int | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dependencies = dependencies
        self._clock = clock
        if account_id is None:
            if dependencies.startup is None or not dependencies.startup.pending:
                raise ValueError("unknown account requires the existing startup identity owner")
        elif type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        self._account_id = account_id

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Bypass persisted cadence while this restart still needs identity."""
        account_id = self._account_id
        if account_id is None:
            raise RuntimeError("startup identity must verify an account before durable selection")
        startup = self._dependencies.startup
        if startup is not None:
            if startup.phase is StartupIdentityPhase.FAILED:
                return None
            if startup.pending:
                selected = WorkRevision(WorkKey(self.demand_kind, account_id, "startup-profile"), startup.phase.value)
                return (
                    None
                    if selected in excluded
                    else DemandStatus(release_at=0.0, freshness_deadline=startup.deadline_at, selected=selected)
                )
        cadence = self._dependencies.cadence.status(now)
        if cadence is None:
            return None
        selected = WorkRevision(
            WorkKey(self.demand_kind, account_id, "periodic-profile"), self._dependencies.cadence.business_revision()
        )
        return (
            None
            if selected in excluded
            else DemandStatus(
                release_at=cadence.release_at, freshness_deadline=cadence.freshness_deadline, selected=selected
            )
        )

    async def run_startup(self, budget: RpcAttemptBudget) -> None:
        """Continue the bounded pre-identity owner without inventing an account key."""
        startup = self._dependencies.startup
        if startup is None or not startup.pending:
            raise RuntimeError("startup identity owner is not pending")
        await self._run_startup_slice(startup, budget)
        if startup.pending and budget.exhausted:
            startup.fail("startup identity attempt budget exhausted")
            startup.result()

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Run one bounded startup continuation or one periodic profile refresh."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")

        startup = self._dependencies.startup
        selected = budget.selected
        if selected is None:
            raise ValueError("self-profile demand requires a selected revision")
        readiness = self._slice_readiness(budget, selected)
        if readiness is not None:
            return readiness
        committed_generation = await self._run_selected_profile(startup, budget)
        if committed_generation is None:
            return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
        outcome = (
            SliceOutcomeKind.TERMINAL
            if startup is not None and startup.phase is StartupIdentityPhase.READY
            else SliceOutcomeKind.PROGRESSED
        )
        return SliceOutcome(selected, outcome, committed_generation)

    def _slice_readiness(self, budget: RpcAttemptBudget, selected: WorkRevision) -> SliceOutcome | None:
        now = self._clock()
        status = self.status(now)
        if status is None or status.selected != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if not status.is_ready(now) or (budget.exhausted and not budget.can_prepare_retry):
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        return None

    async def _run_selected_profile(self, startup: StartupIdentityState | None, budget: RpcAttemptBudget) -> str | None:
        if startup is not None and startup.pending:
            return await self._run_startup_slice(startup, budget)
        return await self._run_periodic_slice(budget)

    async def _run_startup_slice(self, startup: StartupIdentityState, budget: RpcAttemptBudget) -> str | None:
        now = self._clock()
        if now >= startup.deadline_at:
            startup.fail("startup identity deadline expired")
            return None

        try:
            with durable_demand_context(DemandKind.SELF_PROFILE_MAINTENANCE):
                with acquisition_context(AcquisitionKind.ACCOUNT_SELF_PROFILE):
                    with rpc_attempt_budget(budget):
                        await self._continue_startup(startup)
        except RpcAttemptBudgetExhaustedError:
            return None
        except (
            RpcAdmissionClosedError,
            TelegramRpcAdmissionDeferred,
            TelegramRpcMethodRecoveryPending,
            TelegramRpcThrottled,
        ):
            raise
        except Exception as exc:
            reason = f"startup identity {startup.phase.value} failed ({type(exc).__name__})"
            startup.fail(reason)
            raise StartupIdentityUnavailableError(reason) from exc
        if startup.phase is StartupIdentityPhase.READY:
            return await self._dependencies.cadence.mark_refreshed(self._clock())
        return None

    async def _continue_startup(self, startup: StartupIdentityState) -> None:
        if startup.phase is StartupIdentityPhase.PROFILE:
            await self._acquire_startup_profile(startup)

        if startup.phase is StartupIdentityPhase.INPUT_USER:
            await self._acquire_startup_input_user(startup)

        if startup.phase is StartupIdentityPhase.FULL_USER:
            await self._acquire_startup_full_user(startup)

    async def _acquire_startup_profile(self, startup: StartupIdentityState) -> None:
        profile = await self._dependencies.get_me()
        account_id = getattr(profile, "id", None)
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            raise ValueError("Telegram returned no authenticated account profile")
        if self._account_id is not None and self._account_id != account_id:
            raise ValueError("startup identity changed the selected account")
        self._account_id = account_id
        startup.advance_profile(profile)

    async def _acquire_startup_input_user(self, startup: StartupIdentityState) -> None:
        profile = startup.profile
        assert profile is not None
        get_input_entity = self._dependencies.get_input_entity
        assert get_input_entity is not None
        input_user = await get_input_entity(int(cast(_MeLike, profile).id))
        startup.advance_input_user(input_user)

    async def _acquire_startup_full_user(self, startup: StartupIdentityState) -> None:
        profile = startup.profile
        input_user = startup.input_user
        assert profile is not None
        assert input_user is not None
        user_profile_port = self._dependencies.user_profile_port
        publish_startup_identity = self._dependencies.publish_startup_identity
        assert user_profile_port is not None
        assert publish_startup_identity is not None
        observation = await user_profile_port.fetch_user_profile(int(cast(_MeLike, profile).id), TargetKind.USER)
        personal_channel_id = self._personal_channel_id(observation)
        own_only_context = OwnOnlyContext(
            account_id=int(cast(_MeLike, profile).id),
            personal_channel_id=personal_channel_id,
        )
        await publish_startup_identity(profile, own_only_context)
        startup.complete(StartupIdentityResult(profile, own_only_context))

    @staticmethod
    def _personal_channel_id(observation: UserProfileObservation) -> int | None:
        if observation.personal_channel.status not in {ProjectionStatus.USABLE, ProjectionStatus.PARTIAL}:
            return None
        payload = observation.personal_channel.payload or {}
        value = payload.get("personal_channel_id")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    async def _run_periodic_slice(self, budget: RpcAttemptBudget) -> str | None:
        now = self._clock()
        status = self._dependencies.cadence.status(now)
        if status is None or not status.is_ready(now):
            return None

        with durable_demand_context(DemandKind.SELF_PROFILE_MAINTENANCE):
            with acquisition_context(AcquisitionKind.ACCOUNT_SELF_PROFILE):
                with rpc_attempt_budget(budget):
                    try:
                        me = await self._dependencies.get_me()
                    except RpcAttemptBudgetExhaustedError:
                        return None

                    await self._dependencies.update_profile(me)
                    return await self._dependencies.cadence.mark_refreshed(self._clock())


__all__ = [
    "SelfProfileCadenceState",
    "SelfProfileMaintenanceDemandAdapter",
    "SelfProfileMaintenanceDependencies",
]
