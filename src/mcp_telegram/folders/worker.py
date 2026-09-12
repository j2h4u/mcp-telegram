"""Daemon-owned single-flight scheduler for local folder projection."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from ..flood import TelegramRpcThrottled
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
            expiry = self._repository.next_mute_expiry()
            if expiry is not None and now >= expiry:
                self._repository.reproject_current_rules(now=now)
            if self._repository.rules_are_fresh(now=now):
                self._next_due_at = self._due_at()
                return
            if budget is not None and budget.exhausted:
                return
            try:
                if budget is None:
                    await self._refresher.refresh(completed_at=now)
                else:
                    with rpc_attempt_budget(budget):
                        await self._refresher.refresh(completed_at=now)
            except asyncio.CancelledError:
                raise
            except FolderSourceUnavailableError, TimeoutError, OSError:
                self._record_failure(FolderAttemptResult.SOURCE_UNAVAILABLE, None)
            except TelegramRpcThrottled as exc:
                self._record_failure(
                    FolderAttemptResult.CIRCUIT_OPEN if exc.retry_after_seconds is None else FolderAttemptResult.FLOOD_WAIT,
                    exc.retry_after_seconds,
                )
            except RpcAttemptBudgetExhaustedError:
                return
            except Exception:
                self._record_failure(FolderAttemptResult.UNEXPECTED, None)
                raise
            else:
                self._failure_count = 0
                self._next_due_at = self._due_at()

    def _record_failure(self, outcome: FolderAttemptResult, retry_after: int | None) -> None:
        self._failure_count += 1
        if outcome in {FolderAttemptResult.CIRCUIT_OPEN, FolderAttemptResult.UNEXPECTED}:
            retry_at = None
        else:
            schedule = self._policy.retry_delays_seconds
            delay = schedule[min(self._failure_count - 1, len(schedule) - 1)]
            retry_at = math.ceil(self._clock() + max(delay, retry_after or 0))
        self._repository.record_attempt(
            attempted_at=int(self._clock()), outcome=outcome, next_retry_at=retry_at, consecutive_failures=self._failure_count
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
            return None
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
