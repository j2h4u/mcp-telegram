"""Bounded, single-flight profile enrichment refreshes."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
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
from ..telegram_rpc_scheduler import TelegramRpcSource, create_scoped_rpc_task, rpc_attempt_budget, rpc_scope

RefreshCallback = Callable[[int], Awaitable[None]]
DurableRefreshStatusCallback = Callable[[float], DemandStatus | None]
DurableRefreshSliceCallback = Callable[[RpcAttemptBudget], Awaitable[None]]


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
class _RefreshItem:
    entity_id: int
    deadline: float


class EntityRefreshCoordinator:
    """Run bounded, coalescing entity refreshes on a fixed worker pool.

    The queue contains identifiers rather than tasks.  This keeps the number
    of asyncio tasks fixed while still preserving single-flight behavior for an
    entity that is requested repeatedly.
    """

    def __init__(
        self,
        callback: RefreshCallback,
        *,
        limits: RefreshLimits | None = None,
        on_failure: Callable[[int, BaseException], None] | None = None,
    ) -> None:
        self._callback = callback
        self._limits = limits or RefreshLimits()
        self._on_failure = on_failure
        self._queue: deque[_RefreshItem] = deque()
        self._pending_entities: set[int] = set()
        self._active_entities: set[int] = set()
        self._workers: set[asyncio.Task[None]] = set()
        self._completion_events: dict[int, asyncio.Event] = {}
        self._wake = asyncio.Event()
        self._closed = False
        self._durable_status_callback: DurableRefreshStatusCallback | None = None
        self._durable_slice_callback: DurableRefreshSliceCallback | None = None

    @property
    def queue_depth(self) -> int:
        """Return all admitted work, including refreshes currently running."""
        return len(self._pending_entities) + len(self._active_entities)

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def enqueue(self, entity_id: int) -> RefreshEnqueueResult:
        """Admit one refresh, coalescing duplicates and rejecting saturation.

        The deadline is captured before the item enters the queue.  Therefore
        time spent waiting for a fixed worker is part of the refresh budget.
        """
        if self._closed:
            return RefreshEnqueueResult.REJECTED
        loop = asyncio.get_running_loop()
        self._expire_pending(loop.time())
        if entity_id in self._pending_entities or entity_id in self._active_entities:
            return RefreshEnqueueResult.COALESCED
        if len(self._pending_entities) >= self._limits.max_queued_refreshes:
            return RefreshEnqueueResult.REJECTED

        item = _RefreshItem(entity_id, loop.time() + self._limits.whole_refresh_seconds)
        self._completion_events[entity_id] = asyncio.Event()
        self._queue.append(item)
        self._pending_entities.add(entity_id)
        self._ensure_workers()
        self._wake.set()
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

    async def run_durable_slice(self, budget: RpcAttemptBudget) -> None:
        """Run the bound restart-safe entity acquisition slice, when available."""
        if self._durable_slice_callback is not None:
            await self._durable_slice_callback(budget)

    def _ensure_workers(self) -> None:
        required = self._limits.max_concurrent_refreshes
        while len(self._workers) < required:
            task = create_scoped_rpc_task(
                self._worker(),
                source=TelegramRpcSource.ENTITY_INFO_REFRESH,
                name="entity-profile-refresh-worker",
                sanitize_context=True,
            )
            self._workers.add(task)
            task.add_done_callback(self._worker_done)

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

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        self._workers.discard(task)
        if not task.cancelled():
            try:
                task.exception()
            except RuntimeError, asyncio.CancelledError:
                return

    def _expire_pending(self, now: float) -> None:
        if not self._queue:
            return
        retained: deque[_RefreshItem] = deque()
        expired: list[_RefreshItem] = []
        while self._queue:
            item = self._queue.popleft()
            if item.deadline <= now:
                self._pending_entities.remove(item.entity_id)
                expired.append(item)
            else:
                retained.append(item)
        self._queue = retained
        for item in expired:
            self._report_failure(item.entity_id, TimeoutError("entity profile refresh expired in queue"))
            event = self._completion_events.pop(item.entity_id, None)
            if event is not None:
                event.set()

    async def run_rpc[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Apply the per-RPC budget to the refresh's entity lookup."""
        with rpc_scope(
            TelegramRpcSource.ENTITY_INFO_REFRESH,
            acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
        ):
            return await asyncio.wait_for(operation(), timeout=self._limits.per_rpc_seconds)

    async def _worker(self) -> None:
        while True:
            while not self._queue and not self._closed:
                await self._wake.wait()
                self._wake.clear()
            if self._closed:
                return
            item = self._queue.popleft()
            self._pending_entities.remove(item.entity_id)
            self._active_entities.add(item.entity_id)

            try:
                await self._run(item)
            finally:
                self._active_entities.discard(item.entity_id)
                event = self._completion_events.pop(item.entity_id, None)
                if event is not None:
                    event.set()

    async def _run(self, item: _RefreshItem) -> None:
        loop = asyncio.get_running_loop()
        remaining = item.deadline - loop.time()
        if remaining <= 0:
            self._report_failure(item.entity_id, TimeoutError("entity profile refresh expired in queue"))
            return
        try:
            with rpc_scope(TelegramRpcSource.ENTITY_INFO_REFRESH, deadline=item.deadline):
                await asyncio.wait_for(self._callback(item.entity_id), remaining)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - coordinator is the failure boundary
            self._report_failure(item.entity_id, exc)

    def _report_failure(self, entity_id: int, error: BaseException) -> None:
        if self._on_failure is None:
            return
        try:
            self._on_failure(entity_id, error)
        except Exception:  # noqa: BLE001 - failure callback must not kill a worker
            # Failure persistence is a best effort boundary; it must not kill
            # a fixed worker and thereby strand all following queue entries.
            return

    async def shutdown(self) -> None:
        self._closed = True
        self._queue.clear()
        self._pending_entities.clear()
        for event in self._completion_events.values():
            event.set()
        self._completion_events.clear()
        self._wake.set()
        workers = tuple(self._workers)
        if workers:
            # Let scheduler-created wrappers enter their owned context before
            # cancellation so the wrapped worker coroutine is always awaited.
            await asyncio.sleep(0)
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._active_entities.clear()
        self._workers.clear()


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
                        await self._coordinator.run_durable_slice(budget)
                    except RpcAttemptBudgetExhaustedError:
                        return
