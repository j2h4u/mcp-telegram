"""Durable adapter for startup identity and periodic self-profile maintenance."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

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
    demand_context,
)
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    TelegramRpcAdmissionDeferred,
    rpc_attempt_budget,
)


class SelfProfileCadenceState(Protocol):
    """Authoritative cadence state owned by the account domain."""

    def status(self, now: float) -> DemandStatus | None:
        """Return the next due boundary without changing local state."""

    def mark_refreshed(self, completed_at: float) -> None:
        """Commit the next cadence boundary after a successful refresh."""


class _MeLike(Protocol):
    id: int


@dataclass(frozen=True, slots=True)
class SelfProfileMaintenanceDependencies:
    """Narrow dependency port for startup identity and periodic refresh."""

    cadence: SelfProfileCadenceState
    get_me: Callable[[], Awaitable[object]]
    update_profile: Callable[[object], None]
    startup: StartupIdentityState | None = None
    get_input_entity: Callable[[int], Awaitable[object]] | None = None
    get_full_user: Callable[[object], Awaitable[object]] | None = None
    publish_startup_identity: Callable[[object, OwnOnlyContext], None] | None = None

    def __post_init__(self) -> None:
        if self.startup is None:
            return
        if self.get_input_entity is None or self.get_full_user is None or self.publish_startup_identity is None:
            raise ValueError("startup identity requires all own-only dependencies")


class SelfProfileMaintenanceDemandAdapter(DurableDemandAdapter):
    """Acquire mandatory startup identity, then maintain its periodic profile."""

    demand_kind = DemandKind.SELF_PROFILE_MAINTENANCE

    def __init__(
        self,
        dependencies: SelfProfileMaintenanceDependencies,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dependencies = dependencies
        self._clock = clock

    def status(self, now: float) -> DemandStatus | None:
        """Bypass persisted cadence while this restart still needs identity."""
        startup = self._dependencies.startup
        if startup is not None:
            if startup.phase is StartupIdentityPhase.FAILED:
                return None
            if startup.pending:
                return DemandStatus(release_at=0.0, freshness_deadline=startup.deadline_at)
        return self._dependencies.cadence.status(now)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Run one bounded startup continuation or one periodic profile refresh."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")

        startup = self._dependencies.startup
        if startup is not None and startup.pending:
            await self._run_startup_slice(startup, budget)
            return
        await self._run_periodic_slice(budget)

    async def _run_startup_slice(self, startup: StartupIdentityState, budget: RpcAttemptBudget) -> None:
        now = self._clock()
        if now >= startup.deadline_at:
            startup.fail("startup identity deadline expired")
            return

        try:
            with demand_context(DemandKind.SELF_PROFILE_MAINTENANCE):
                with acquisition_context(AcquisitionKind.ACCOUNT_SELF_PROFILE):
                    with rpc_attempt_budget(budget):
                        await self._continue_startup(startup)
        except RpcAttemptBudgetExhaustedError:
            return
        except RpcAdmissionClosedError, TelegramRpcAdmissionDeferred, TelegramRpcThrottled:
            raise
        except Exception as exc:
            reason = f"startup identity {startup.phase.value} failed ({type(exc).__name__})"
            startup.fail(reason)
            raise StartupIdentityUnavailableError(reason) from exc

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
        get_full_user = self._dependencies.get_full_user
        publish_startup_identity = self._dependencies.publish_startup_identity
        assert get_full_user is not None
        assert publish_startup_identity is not None
        full_result = await get_full_user(input_user)
        personal_channel_id = self._personal_channel_id(full_result)
        own_only_context = OwnOnlyContext(
            account_id=int(cast(_MeLike, profile).id),
            personal_channel_id=personal_channel_id,
        )
        publish_startup_identity(profile, own_only_context)
        self._dependencies.cadence.mark_refreshed(self._clock())
        startup.complete(StartupIdentityResult(profile, own_only_context))

    @staticmethod
    def _personal_channel_id(full_result: object) -> int | None:
        user_full = getattr(full_result, "full_user", None)
        value = getattr(user_full, "personal_channel_id", None)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    async def _run_periodic_slice(self, budget: RpcAttemptBudget) -> None:
        now = self._clock()
        status = self._dependencies.cadence.status(now)
        if status is None or not status.is_ready(now):
            return

        with demand_context(DemandKind.SELF_PROFILE_MAINTENANCE):
            with acquisition_context(AcquisitionKind.ACCOUNT_SELF_PROFILE):
                with rpc_attempt_budget(budget):
                    try:
                        me = await self._dependencies.get_me()
                    except RpcAttemptBudgetExhaustedError:
                        return

                    self._dependencies.update_profile(me)
                    self._dependencies.cadence.mark_refreshed(self._clock())


__all__ = [
    "SelfProfileCadenceState",
    "SelfProfileMaintenanceDemandAdapter",
    "SelfProfileMaintenanceDependencies",
]
