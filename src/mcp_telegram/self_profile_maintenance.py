"""Durable adapter for startup identity and periodic self-profile maintenance."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from .flood import TelegramRpcThrottled
from .own_only import OwnOnlyContext
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    demand_context,
)
from .telegram_rpc_consumers import DemandKind, demand_contract
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


class StartupIdentityPhase(StrEnum):
    """Restart-local continuation point for mandatory account identity."""

    PROFILE = "profile"
    INPUT_USER = "input_user"
    FULL_USER = "full_user"
    READY = "ready"
    FAILED = "failed"


class StartupIdentityUnavailableError(RuntimeError):
    """Raised when mandatory startup identity cannot become ready."""


@dataclass(frozen=True, slots=True)
class StartupIdentityResult:
    """Identity facts published atomically before daemon readiness."""

    profile: object
    own_only_context: OwnOnlyContext


@dataclass(slots=True)
class StartupIdentityState:
    """Bounded, restart-local state shared by the adapter and daemon startup."""

    started_at: float
    deadline_at: float
    phase: StartupIdentityPhase = StartupIdentityPhase.PROFILE
    profile: object | None = None
    input_user: object | None = None
    _result: StartupIdentityResult | None = None
    _failure_reason: str | None = None
    _done_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        for value, label in ((self.started_at, "started_at"), (self.deadline_at, "deadline_at")):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{label} must be a finite timestamp")
        if self.started_at < 0 or self.deadline_at <= self.started_at:
            raise ValueError("startup identity deadline must follow its start")

    @classmethod
    def begin(cls, *, now: float | None = None) -> StartupIdentityState:
        """Create state bounded by the maximum admission time in its contract."""
        started_at = time.time() if now is None else float(now)
        contract = demand_contract(DemandKind.SELF_PROFILE_MAINTENANCE)
        attempts = contract.max_rpc_attempts_per_slice
        if attempts is None:
            raise RuntimeError("self-profile maintenance has no RPC attempt bound")
        return cls(started_at, started_at + contract.admission_timeout_seconds * attempts)

    @property
    def pending(self) -> bool:
        return self.phase not in {StartupIdentityPhase.READY, StartupIdentityPhase.FAILED}

    @property
    def done_event(self) -> asyncio.Event:
        return self._done_event

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    def remaining(self, now: float) -> float:
        """Return the remaining startup window."""
        return max(0.0, self.deadline_at - now)

    def advance_profile(self, profile: object) -> None:
        if self.phase is not StartupIdentityPhase.PROFILE:
            raise RuntimeError("startup identity profile arrived out of order")
        self.profile = profile
        self.phase = StartupIdentityPhase.INPUT_USER

    def advance_input_user(self, input_user: object) -> None:
        if self.phase is not StartupIdentityPhase.INPUT_USER:
            raise RuntimeError("startup identity input user arrived out of order")
        self.input_user = input_user
        self.phase = StartupIdentityPhase.FULL_USER

    def complete(self, result: StartupIdentityResult) -> None:
        if self.phase is not StartupIdentityPhase.FULL_USER:
            raise RuntimeError("startup identity completed out of order")
        self._result = result
        self.phase = StartupIdentityPhase.READY
        self._done_event.set()

    def fail(self, reason: str) -> None:
        if not self.pending:
            return
        self._failure_reason = reason
        self.phase = StartupIdentityPhase.FAILED
        self._done_event.set()

    def result(self) -> StartupIdentityResult:
        if self._result is not None:
            return self._result
        reason = self._failure_reason or "startup identity is not ready"
        raise StartupIdentityUnavailableError(reason)


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
    "StartupIdentityPhase",
    "StartupIdentityResult",
    "StartupIdentityState",
    "StartupIdentityUnavailableError",
]
