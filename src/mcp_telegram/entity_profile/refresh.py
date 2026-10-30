"""Bounded, single-flight profile enrichment refreshes."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from ..telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    durable_demand_context,
)
from ..telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkRevision
from ..telegram_rpc_consumers import DemandKind
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_attempt_budget, rpc_scope
from .contracts import PROFILE_FLOOD_RETRY_BASE_SECONDS, PROFILE_FLOOD_RETRY_CAP_SECONDS

DurableRefreshStatusCallback = Callable[[float, frozenset[WorkRevision]], DemandStatus | None]


def scope_changed_retry_at(now: int) -> int:
    """Return the immediate retry boundary after an auth-scope change."""
    return now + 1


def _validate_positive_duration(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
        raise ValueError(f"entity profile {name} must be positive")


def _validate_positive_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"entity profile {name} must be positive")


@dataclass(frozen=True, slots=True)
class RefreshLimits:
    foreground_resolve_seconds: float = 3.0
    per_rpc_seconds: float = 8.0
    whole_refresh_seconds: float = 25.0
    max_queued_refreshes: int = 128
    foreground_refresh_wait_seconds: float = 15.0
    flood_retry_base_seconds: int = PROFILE_FLOOD_RETRY_BASE_SECONDS
    flood_retry_cap_seconds: int = PROFILE_FLOOD_RETRY_CAP_SECONDS

    def __post_init__(self) -> None:
        for name, value in (
            ("foreground resolve budget", self.foreground_resolve_seconds),
            ("foreground refresh wait", self.foreground_refresh_wait_seconds),
            ("per-RPC budget", self.per_rpc_seconds),
            ("whole-refresh budget", self.whole_refresh_seconds),
        ):
            _validate_positive_duration(value, name)
        if self.foreground_resolve_seconds > self.per_rpc_seconds:
            raise ValueError("foreground resolve budget cannot exceed per-RPC budget")
        if self.per_rpc_seconds > self.whole_refresh_seconds:
            raise ValueError("per-RPC budget cannot exceed whole-refresh budget")
        _validate_positive_integer(self.max_queued_refreshes, "refresh queue capacity")
        _validate_positive_integer(self.flood_retry_base_seconds, "Flood retry base")
        _validate_positive_integer(self.flood_retry_cap_seconds, "Flood retry cap")
        if self.flood_retry_base_seconds > self.flood_retry_cap_seconds:
            raise ValueError("entity profile Flood retry base cannot exceed cap")


class RefreshEnqueueResult(StrEnum):
    """Result of attempting to admit one entity refresh."""

    QUEUED = "queued"
    COALESCED = "coalesced"
    REJECTED = "rejected"

    def __bool__(self) -> bool:
        """Keep the pre-queue API's truthiness for existing callers."""
        return self is RefreshEnqueueResult.QUEUED


@dataclass(frozen=True, slots=True)
class DurableRefreshSliceResult:
    """Foreground waiter result plus the independent durable slice receipt."""

    entity_id: int
    terminal: DurableRefreshTerminal | None = None
    outcome: SliceOutcome | None = None
    retryable_failure: bool = False


class DurableRefreshTerminal(StrEnum):
    """Terminal state for the foreground wait associated with one refresh attempt."""

    SUCCESS = "success"
    FAILURE = "failure"


type DurableRefreshSliceCallback = Callable[[RpcAttemptBudget], Awaitable[DurableRefreshSliceResult | None]]


class EntityRefreshCoordinator:
    """Coalesce foreground waiters around the durable entity refresh queue."""

    def __init__(
        self,
        *,
        limits: RefreshLimits | None = None,
    ) -> None:
        self._limits = limits or RefreshLimits()
        self._completion_events: dict[int, asyncio.Event] = {}
        self._closed = False
        self._durable_status_callback: DurableRefreshStatusCallback | None = None
        self._durable_slice_callback: DurableRefreshSliceCallback | None = None

    @property
    def queue_depth(self) -> int:
        """Return process-local refresh admissions with foreground waiters."""
        return len(self._completion_events)

    @property
    def is_closed(self) -> bool:
        """Return whether this coordinator has stopped admitting refresh work."""
        return self._closed

    def enqueue(self, entity_id: int) -> RefreshEnqueueResult:
        """Register foreground interest in durable work and coalesce duplicates."""
        if self._closed:
            return RefreshEnqueueResult.REJECTED
        if entity_id in self._completion_events:
            return RefreshEnqueueResult.COALESCED
        if self.queue_depth >= self._limits.max_queued_refreshes:
            return RefreshEnqueueResult.REJECTED

        self._completion_events[entity_id] = asyncio.Event()
        return RefreshEnqueueResult.QUEUED

    def bind_durable_executor(
        self,
        status: DurableRefreshStatusCallback,
        run_slice: DurableRefreshSliceCallback,
    ) -> None:
        """Attach the domain-owned durable selector and one-acquisition runner."""
        self._durable_status_callback = status
        self._durable_slice_callback = run_slice

    def durable_status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Read durable entity refresh readiness without touching the queue."""
        if self._durable_status_callback is None:
            return None
        return self._durable_status_callback(now, excluded)

    async def run_durable_slice(self, budget: RpcAttemptBudget) -> DurableRefreshSliceResult | None:
        """Run the bound restart-safe entity acquisition slice, when available."""
        if self._closed:
            return None
        if self._durable_slice_callback is None:
            return None
        timeout_seconds = self._limits.per_rpc_seconds
        with rpc_scope(TelegramRpcSource.ENTITY_INFO_REFRESH, response_timeout_seconds=timeout_seconds):
            return await self._durable_slice_callback(budget)

    def signal_terminal(self, result: DurableRefreshSliceResult) -> None:
        """Release coalesced waiters after durable success or persisted failure."""
        if result.terminal is None:
            return
        event = self._completion_events.pop(result.entity_id, None)
        if event is not None:
            event.set()

    async def shutdown(self) -> None:
        self._closed = True
        for event in self._completion_events.values():
            event.set()
        self._completion_events.clear()


class EntityProfileDemandAdapter(DurableDemandAdapter):
    """Execute one restart-safe entity-profile acquisition from durable state."""

    demand_kind = DemandKind.ENTITY_PROFILE_REFRESH

    def __init__(self, coordinator: EntityRefreshCoordinator) -> None:
        self._coordinator = coordinator

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Report the oldest persisted entity-section release boundary."""
        return self._coordinator.durable_status(now, excluded=excluded)

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Run at most the supplied actual-attempt budget and retain its cursor."""
        selected = self._selected_revision(budget)
        if self._coordinator.is_closed:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        now = time.time()
        status = self.status(now)
        if status is None or not status.is_ready(now):
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        try:
            result = await self._run_scoped_slice(budget)
        except RpcAttemptBudgetExhaustedError:
            if budget.can_prepare_retry:
                raise
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        return self._finish_slice(selected, result)

    @staticmethod
    def _selected_revision(budget: RpcAttemptBudget) -> WorkRevision:
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise RuntimeError("entity profile slice requires an owner-selected revision")
        return selected

    async def _run_scoped_slice(self, budget: RpcAttemptBudget) -> DurableRefreshSliceResult | None:
        with durable_demand_context(DemandKind.ENTITY_PROFILE_REFRESH):
            with rpc_attempt_budget(budget):
                with rpc_scope(
                    TelegramRpcSource.ENTITY_INFO_REFRESH,
                    acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
                ):
                    return await self._coordinator.run_durable_slice(budget)

    def _finish_slice(self, selected: WorkRevision, result: DurableRefreshSliceResult | None) -> SliceOutcome:
        if result is None:
            return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
        if result.terminal is not None:
            self._coordinator.signal_terminal(result)
        if result.outcome is None:
            return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
        return result.outcome
