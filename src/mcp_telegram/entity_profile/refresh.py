"""Bounded, single-flight profile enrichment refreshes."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

RefreshCallback = Callable[[int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RefreshLimits:
    foreground_resolve_seconds: float = 3.0
    per_rpc_seconds: float = 8.0
    whole_refresh_seconds: float = 25.0
    max_concurrent_refreshes: int = 1

    def __post_init__(self) -> None:
        if any(
            not math.isfinite(value) or value <= 0
            for value in (self.foreground_resolve_seconds, self.per_rpc_seconds, self.whole_refresh_seconds)
        ):
            raise ValueError("entity profile budgets must be positive")
        if self.foreground_resolve_seconds > self.per_rpc_seconds:
            raise ValueError("foreground resolve budget cannot exceed per-RPC budget")
        if self.per_rpc_seconds > self.whole_refresh_seconds:
            raise ValueError("per-RPC budget cannot exceed whole-refresh budget")
        if (
            isinstance(self.max_concurrent_refreshes, bool)
            or not isinstance(self.max_concurrent_refreshes, int)
            or self.max_concurrent_refreshes < 1
        ):
            raise ValueError("entity profile refresh concurrency must be positive")


class EntityRefreshCoordinator:
    """Coordinate at most one refresh task per entity and track shutdown."""

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
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._refresh_semaphore = asyncio.Semaphore(self._limits.max_concurrent_refreshes)
        self._closed = False

    @property
    def queue_depth(self) -> int:
        return len(self._tasks)

    def enqueue(self, entity_id: int) -> bool:
        """Queue one refresh; duplicate entity requests share the existing task."""
        if self._closed or entity_id in self._tasks:
            return False
        task = asyncio.create_task(self._run(entity_id), name=f"entity-profile-refresh-{entity_id}")
        self._tasks[entity_id] = task
        task.add_done_callback(lambda completed: self._task_done(entity_id, completed))
        return True

    def _task_done(self, entity_id: int, task: asyncio.Task[None]) -> None:
        self._tasks.pop(entity_id, None)
        if task.cancelled():
            return
        try:
            task.exception()
        except RuntimeError, asyncio.CancelledError:
            return

    async def run_rpc[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Apply the per-RPC budget to an operation owned by a refresh."""
        return await asyncio.wait_for(operation(), timeout=self._limits.per_rpc_seconds)

    async def _run(self, entity_id: int) -> None:
        try:
            async with self._refresh_semaphore:
                await asyncio.wait_for(self._callback(entity_id), timeout=self._limits.whole_refresh_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - coordinator is the failure boundary
            if self._on_failure is not None:
                self._on_failure(entity_id, exc)

    async def shutdown(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
