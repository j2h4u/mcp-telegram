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
    demand_context,
)
from ..telegram_rpc_consumers import DemandKind
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_attempt_budget, rpc_scope

DurableRefreshStatusCallback = Callable[[float], DemandStatus | None]


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
    max_concurrent_refreshes: int = 1
    max_queued_refreshes: int = 128
    foreground_refresh_wait_seconds: float = 15.0

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
        _validate_positive_integer(self.max_concurrent_refreshes, "refresh concurrency")
        _validate_positive_integer(self.max_queued_refreshes, "refresh queue capacity")


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
    """Durable work advanced for one entity, optionally reaching a terminal state."""

    entity_id: int
    terminal: DurableRefreshTerminal | None = None


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

    def durable_status(self, now: float) -> DemandStatus | None:
        """Read durable entity refresh readiness without touching the queue."""
        if self._durable_status_callback is None:
            return None
        return self._durable_status_callback(now)

    async def run_durable_slice(self, budget: RpcAttemptBudget) -> DurableRefreshSliceResult | None:
        """Run the bound restart-safe entity acquisition slice, when available."""
        if self._durable_slice_callback is None:
            return None
        return await self._durable_slice_callback(budget)

    async def wait_for_completion(self, entity_id: int, timeout_seconds: float) -> bool:
        """Wait for an admitted single-flight refresh without cancelling it on timeout."""
        event = self._completion_events.get(entity_id)
        if event is None:
            return True
        try:
            async with asyncio.timeout(timeout_seconds):
                await event.wait()
        except TimeoutError:
            return False
        return True

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

    def status(self, now: float) -> DemandStatus | None:
        """Report the oldest persisted entity-section release boundary."""
        return self._coordinator.durable_status(now)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Run at most the supplied actual-attempt budget and retain its cursor."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        now = time.time()
        status = self.status(now)
        if status is None or not status.is_ready(now):
            return
        with demand_context(DemandKind.ENTITY_PROFILE_REFRESH):
            with rpc_attempt_budget(budget):
                with rpc_scope(
                    TelegramRpcSource.ENTITY_INFO_REFRESH,
                    acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
                ):
                    try:
                        result = await self._coordinator.run_durable_slice(budget)
                    except RpcAttemptBudgetExhaustedError:
                        return
        if result is not None:
            self._coordinator.signal_terminal(result)
