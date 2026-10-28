"""Daemon-owned single-flight scheduler for local folder projection."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from ..flood import TelegramRpcThrottled
from ..sqlite_writer import SQLiteWriter
from ..telegram_demand import (
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    durable_demand_context,
)
from ..telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from ..telegram_rpc_consumers import DemandKind
from ..telegram_rpc_scheduler import TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending, rpc_attempt_budget
from .contracts import RULE_TTL_SECONDS, FolderSourceUnavailableError
from .ports import FolderSnapshotRepository
from .refresh import FolderRefresher, FolderRefreshResult

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
    def __init__(  # noqa: PLR0913 - explicit lifecycle and writer dependencies
        self,
        refresher: FolderRefresher,
        repository: FolderSnapshotRepository,
        shutdown_event: asyncio.Event,
        policy: FolderProjectionScheduling,
        *,
        writer: SQLiteWriter,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._refresher = refresher
        self._writer = writer
        self._repository = repository
        self._shutdown_event = shutdown_event
        self._policy = policy
        self._clock = clock
        self._failure_count = repository.read_consecutive_failures()
        self._next_due_at: float | None
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

    async def _attempt(self, reason: str, budget: RpcAttemptBudget | None = None) -> FolderRefreshResult | None:
        del reason
        async with self._attempt_lock:
            now = int(self._clock())
            if await self._attempt_preflight(now, budget):
                return None
            try:
                receipt = await self._refresh_once(now, budget)
            except asyncio.CancelledError:
                raise
            except TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending, TelegramRpcThrottled:
                raise
            except Exception as exc:
                if await self._handle_refresh_error(exc):
                    return None
                raise
            else:
                self._record_success()
                return receipt

    async def _attempt_preflight(self, now: int, budget: RpcAttemptBudget | None) -> bool:
        """Run the no-RPC checks while the attempt lock is held."""
        expiry = self._repository.next_mute_expiry()
        if expiry is not None and now >= expiry:
            await self._writer.run(lambda conn: self._repository.for_connection(conn).reproject_current_rules(now=now))
        if self._repository.rules_are_fresh(now=now):
            self._next_due_at = self._due_at()
            return True
        return budget is not None and budget.exhausted and not budget.can_prepare_retry

    async def _refresh_once(self, now: int, budget: RpcAttemptBudget | None) -> FolderRefreshResult:
        if budget is None:
            return await self._refresher.refresh(completed_at=now)
        with rpc_attempt_budget(budget):
            return await self._refresher.refresh(
                completed_at=now, expected_revision=None if budget.selected is None else budget.selected.revision
            )

    async def _handle_refresh_error(self, exc: Exception) -> bool:
        if isinstance(exc, (FolderSourceUnavailableError, TimeoutError, OSError)):
            await self._record_failure(FolderAttemptResult.SOURCE_UNAVAILABLE, None)
            return True
        if isinstance(exc, TelegramRpcThrottled):
            raise exc
        if isinstance(exc, RpcAttemptBudgetExhaustedError):
            return True
        await self._record_failure(FolderAttemptResult.UNEXPECTED, None)
        return False

    def _record_success(self) -> None:
        self._failure_count = 0
        self._next_due_at = self._due_at()

    async def _record_failure(self, outcome: FolderAttemptResult, retry_after: int | None) -> None:
        del retry_after
        self._failure_count += 1
        retry_at = self._repository.read_next_retry_at()
        await self._writer.run(
            lambda conn: self._repository.for_connection(conn).record_attempt(
                attempted_at=int(self._clock()),
                outcome=outcome,
                next_retry_at=retry_at,
                consecutive_failures=self._failure_count,
            )
        )
        self._next_due_at = self._clock() if retry_at is None else float(retry_at)


class FolderProjectionDemandAdapter(DurableDemandAdapter):
    demand_kind = DemandKind.FOLDER_SNAPSHOT

    def __init__(self, worker: FolderProjectionWorker, *, account_id: int) -> None:
        self._worker = worker
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        self._account_id = account_id

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        repository = self._worker._repository
        retry = repository.read_next_retry_at()
        outcome = repository.read_last_outcome()
        if retry is not None:
            release_at = float(retry)
            selected = self._selected(repository)
            return None if selected in excluded else DemandStatus(release_at=release_at, selected=selected)
        if outcome in {FolderAttemptResult.CIRCUIT_OPEN, FolderAttemptResult.UNEXPECTED}:
            # Older versions persisted these outcomes without a retry time.
            # Treat that state as immediately due so an upgrade cannot mute
            # folder projection permanently.
            selected = self._selected(repository)
            return None if selected in excluded else DemandStatus(release_at=now, selected=selected)
        success = repository.read_last_success_at()
        if success is None:
            selected = self._selected(repository)
            return None if selected in excluded else DemandStatus(release_at=now, selected=selected)
        deadline = success + RULE_TTL_SECONDS
        selected = self._selected(repository)
        return (
            None
            if selected in excluded
            else DemandStatus(release_at=float(deadline), freshness_deadline=float(deadline), selected=selected)
        )

    def _selected(self, repository: FolderSnapshotRepository) -> WorkRevision:
        revision = repr(repository.read_snapshot_revision())
        return WorkRevision(WorkKey(self.demand_kind, self._account_id, "folder-snapshot"), revision)

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        selected = budget.selected
        if selected is None:
            raise ValueError("folder demand requires a selected revision")
        current = self.status(self._worker._clock())
        if current is None or current.selected != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if budget.exhausted and not budget.can_prepare_retry:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        with durable_demand_context(self.demand_kind):
            try:
                result = await self._worker._attempt("demand", budget)
            except TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending, TelegramRpcThrottled:
                raise
            except RpcAttemptBudgetExhaustedError:
                return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        return self._outcome(selected, result)

    @staticmethod
    def _outcome(selected: WorkRevision, result: FolderRefreshResult | None) -> SliceOutcome:
        if result is not None:
            if not result.accepted:
                return SliceOutcome(selected, SliceOutcomeKind.STALE)
            if result.committed_revision is not None and result.committed_revision != selected.revision:
                return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, result.committed_revision)
        return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
