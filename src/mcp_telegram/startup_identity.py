"""Restart-local state contract for mandatory account identity startup."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from enum import StrEnum

from mcp_telegram.own_only_contracts import OwnOnlyContext
from mcp_telegram.telegram_rpc_consumers import DemandKind, demand_contract


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


__all__ = [
    "StartupIdentityPhase",
    "StartupIdentityResult",
    "StartupIdentityState",
    "StartupIdentityUnavailableError",
]
