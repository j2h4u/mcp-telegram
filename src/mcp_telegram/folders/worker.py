"""Daemon-owned single-flight scheduler for local folder projection."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from ..flood import TelegramRpcThrottled, _raise_if_latched
from ..telegram_demand import (
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    demand_context,
)
from ..telegram_rpc_consumers import DemandKind
from ..telegram_rpc_scheduler import rpc_attempt_budget
from .contracts import RULE_TTL_SECONDS, FolderSourceUnavailableError
from .ports import FolderSnapshotRepository
from .refresh import FolderRefresher

logger = logging.getLogger(__name__)


class FolderProjectionScheduling(Protocol):
    @property
    def refresh_interval_seconds(self) -> float: ...
    @property
    def retry_delays_seconds(self) -> tuple[int, ...]: ...
    @property
    def retry_cap_seconds(self) -> int: ...
    @property
    def warning_failure_threshold(self) -> int: ...


class FolderAttemptResult(StrEnum):
    SUCCESS = "success"
    SOURCE_UNAVAILABLE = "source_unavailable"
    FLOOD_WAIT = "flood_wait"
    CIRCUIT_OPEN = "circuit_open"
    UNEXPECTED = "unexpected"


class FolderProjectionWorker:
    def __init__(
        self,
        refresher: FolderRefresher,
        repository: FolderSnapshotRepository,
        shutdown_event: asyncio.Event,
        policy: FolderProjectionScheduling,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._refresher = refresher
        self._repository = repository
        self._shutdown_event = shutdown_event
        self._policy = policy
        self._clock = clock
        self._failure_count = repository.read_consecutive_failures()
        self._next_due_at = self._due_at()
        self._primed = False
        self._attempt_lock = asyncio.Lock()

    def _due_at(self) -> int | None:
        retry = self._repository.read_next_retry_at()
        if retry is not None:
            return retry
        success = self._repository.read_last_success_at()
        outcome = self._repository.read_last_outcome()
        if success is None or outcome not in {None, FolderAttemptResult.SUCCESS}:
            return 0
        return success + RULE_TTL_SECONDS

    async def prime(self) -> None:
        if self._primed:
            return
        self._primed = True
        if self._next_due_at is not None and self._next_due_at <= self._clock():
            await self._attempt("startup")

    async def run(self) -> None:
        if not self._primed:
            await self.prime()
        while not self._shutdown_event.is_set() and self._next_due_at is not None:
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=max(0.0, self._next_due_at - self._clock()))
            except TimeoutError:
                await self._attempt("scheduled")

    async def _attempt(self, reason: str, budget: RpcAttemptBudget | None = None) -> None:
        del reason
        async with self._attempt_lock:
            now = int(self._clock())
            if self._attempt_preflight(now, budget):
                return
            try:
                await self._refresh_once(now, budget)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._handle_refresh_error(exc):
                    return
                raise
            else:
                self._record_success()

    def _attempt_preflight(self, now: int, budget: RpcAttemptBudget | None) -> bool:
        """Run the no-RPC checks while the attempt lock is held."""
        expiry = self._repository.next_mute_expiry()
        if expiry is not None and now >= expiry:
            self._repository.reproject_current_rules(now=now)
        if self._repository.rules_are_fresh(now=now):
            self._next_due_at = self._due_at()
            return True
        return budget is not None and budget.exhausted

    async def _refresh_once(self, now: int, budget: RpcAttemptBudget | None) -> None:
        if budget is None:
            await self._refresher.refresh(completed_at=now)
            return
        with rpc_attempt_budget(budget):
            await self._refresher.refresh(completed_at=now)

    def _handle_refresh_error(self, exc: Exception) -> bool:
        if isinstance(exc, (FolderSourceUnavailableError, TimeoutError, OSError)):
            self._record_failure(FolderAttemptResult.SOURCE_UNAVAILABLE, None)
            return True
        if isinstance(exc, TelegramRpcThrottled):
            # An account circuit is a coordinator-owned latch.  Let the
            # existing convention propagate it so this demand does not
            # create a private retry loop against a blocked account.
            _raise_if_latched(exc)
            self._record_failure(
                FolderAttemptResult.CIRCUIT_OPEN
                if exc.retry_after_seconds is None
                else FolderAttemptResult.FLOOD_WAIT,
                exc.retry_after_seconds,
            )
            return True
        if isinstance(exc, RpcAttemptBudgetExhaustedError):
            return True
        self._record_failure(FolderAttemptResult.UNEXPECTED, None)
        return False

    def _record_success(self) -> None:
        self._failure_count = 0
        self._next_due_at = self._due_at()

    def _record_failure(self, outcome: FolderAttemptResult, retry_after: int | None) -> None:
        self._failure_count += 1
        schedule = self._policy.retry_delays_seconds
        delay = schedule[min(self._failure_count - 1, len(schedule) - 1)]
        retry_at = math.ceil(self._clock() + max(delay, retry_after or 0))
        self._repository.record_attempt(
            attempted_at=int(self._clock()),
            outcome=outcome,
            next_retry_at=retry_at,
            consecutive_failures=self._failure_count,
        )
        self._next_due_at = retry_at


class FolderProjectionDemandAdapter(DurableDemandAdapter):
    demand_kind = DemandKind.FOLDER_SNAPSHOT

    def __init__(self, worker: FolderProjectionWorker) -> None:
        self._worker = worker

    def status(self, now: float) -> DemandStatus | None:
        repository = self._worker._repository
        retry = repository.read_next_retry_at()
        outcome = repository.read_last_outcome()
        if retry is not None:
            return DemandStatus(release_at=float(retry))
        if outcome in {FolderAttemptResult.CIRCUIT_OPEN, FolderAttemptResult.UNEXPECTED}:
            # Older versions persisted these outcomes without a retry time.
            # Treat that state as immediately due so an upgrade cannot mute
            # folder projection permanently.
            return DemandStatus(release_at=now)
        success = repository.read_last_success_at()
        if success is None:
            return DemandStatus(release_at=now)
        deadline = success + RULE_TTL_SECONDS
        return DemandStatus(release_at=float(deadline), freshness_deadline=float(deadline))

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        if budget.exhausted:
            return
        with demand_context(self.demand_kind):
            await self._worker._attempt("demand", budget)
