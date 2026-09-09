"""Durable adapter for the account self-profile maintenance demand."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

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
from .telegram_rpc_scheduler import rpc_attempt_budget


class SelfProfileCadenceState(Protocol):
    """Authoritative cadence state owned by the account domain.

    The implementation is expected to read and write the existing domain state
    (for example, a small sync.db helper).  This adapter deliberately does not
    create a second cadence store or infer readiness from the profile snapshot.
    """

    def status(self, now: float) -> DemandStatus | None:
        """Return the next due boundary without changing local state."""

    def mark_refreshed(self, completed_at: float) -> None:
        """Commit the next cadence boundary after a successful refresh."""


@dataclass(frozen=True, slots=True)
class SelfProfileMaintenanceDependencies:
    """Narrow dependency port for self-profile execution."""

    cadence: SelfProfileCadenceState
    get_me: Callable[[], Awaitable[object]]
    update_profile: Callable[[object], None]


class SelfProfileMaintenanceDemandAdapter(DurableDemandAdapter):
    """Run one bounded self-profile refresh from authoritative cadence state."""

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
        """Read cadence readiness without fetching Telegram or mutating state."""
        return self._dependencies.cadence.status(now)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Refresh the profile once, charging the exact durable slice budget."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")

        now = self._clock()
        status = self.status(now)
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
