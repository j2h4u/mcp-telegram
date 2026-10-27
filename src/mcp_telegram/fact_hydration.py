"""Shared bounded runner for durable message-fact hydration jobs."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, cast

from .access_lifecycle import set_access_lost
from .flood import TelegramRpcThrottled
from .hydration_queue import (
    MEDIA_METADATA_KIND,
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationJob,
    HydrationOutcome,
    HydrationPriority,
    HydrationQueueRepository,
    HydrationQueueSummary,
)
from .messages.sqlite_hydration import hydration_observation_current, hydration_work_revision
from .messages.sqlite_hydration_jobs import (
    HydrationRepairCursor,
    repair_media_metadata_hydration_jobs,
    repair_transcription_hydration_jobs,
)
from .observation_order import allocate_observation_order
from .sqlite_writer import SQLiteWriter
from .sync_transactions import require_write_transaction, write_transaction
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
    current_demand_token,
    durable_demand_context,
)
from .telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_error import TelegramRpcErrorDescriptor, describe_telegram_rpc_error
from .telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    TelegramRpcSource,
    create_scoped_rpc_task,
    rpc_attempt_budget,
    rpc_scope,
)

logger = logging.getLogger(__name__)
_MAX_LOGGED_MESSAGE_IDS = 32
_DROP_LEVELS = {
    "terminal_rpc": logging.INFO,
    "access_lost": logging.INFO,
    "attempt_limit": logging.WARNING,
    "invalid_result": logging.WARNING,
    "ineligible": logging.DEBUG,
    "missing_response": logging.DEBUG,
    "not_applied": logging.DEBUG,
}


def _has_terminal_rpc_symbol(exc: BaseException, symbols: frozenset[str]) -> bool:
    """Match a bounded set of safe Telegram RPC symbols for a handler."""
    return describe_telegram_rpc_error(exc).symbol in symbols


@dataclass(frozen=True, slots=True)
class AppliedFacts:
    hydrated: int = 0
    completed: int = 0
    dropped: int = 0
    pending: bool = False
    drop_observations: tuple[HydrationDropObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class HydrationDropObservation:
    """One runner-owned reason/coordinate pair awaiting batch aggregation."""

    reason: str
    message_id: int
    kind: str | None = None
    dialog_id: int | None = None
    attempts: int | None = None


@dataclass(frozen=True, slots=True)
class HydrationDrop:
    """One bounded, aggregated hydration drop suitable for log-only telemetry."""

    reason: str
    kind: str
    dialog_id: int
    job_count: int
    message_ids: tuple[int, ...]
    attempts_min: int
    attempts_max: int
    error_type: str | None
    rpc_code: int | None
    rpc_symbol: str | None


class HydrationHandler(Protocol):
    kind: str
    batch_size: int
    request_cost: int
    pending_delay_seconds: int

    def eligible(self, conn: sqlite3.Connection, job: HydrationJob) -> bool: ...

    async def request(self, client: object, jobs: Sequence[HydrationJob]) -> object: ...

    def apply(  # noqa: PLR0913 - request boundary accompanies the existing fact application contract
        self,
        conn: sqlite3.Connection,
        queue: HydrationQueueRepository,
        jobs: Sequence[HydrationJob],
        result: object,
        *,
        now: int,
        observation_order: int | None = None,
    ) -> AppliedFacts: ...

    def is_terminal_error(self, exc: BaseException) -> bool: ...


@dataclass(frozen=True, slots=True)
class _BatchOutcome:
    hydrated: int = 0
    completed: int = 0
    pending: int = 0
    retried: int = 0
    dropped: int = 0
    stopped: bool = False
    dropped_by_kind: tuple[tuple[str, int], ...] = ()
    committed_revision: str | None = None
    terminal_revision: str | None = None
    deferred_error: Exception | None = None


def _hydration_rpc_source(priority: HydrationPriority) -> TelegramRpcSource:
    """Map the selected queue tier to its account-wide RPC source."""
    if priority is HydrationPriority.FOREGROUND:
        return TelegramRpcSource.FACT_HYDRATION_LIVE
    if priority is HydrationPriority.BACKFILL:
        return TelegramRpcSource.FACT_HYDRATION_BACKFILL
    raise ValueError(f"unsupported hydration priority: {priority!r}")


def _hydration_demand_kind(priority: HydrationPriority) -> DemandKind:
    """Map one durable queue tier to its registered root demand."""
    if priority is HydrationPriority.FOREGROUND:
        return DemandKind.LIVE_HYDRATION_BATCH
    if priority is HydrationPriority.BACKFILL:
        return DemandKind.BACKFILL_HYDRATION_BATCH
    raise ValueError(f"unsupported hydration priority: {priority!r}")


def _due_job_order_key(job: HydrationJob) -> tuple[int, int, str, int, int]:
    return (-job.message_sent_at, job.due_at, job.kind, job.dialog_id, job.message_id)


HydrationBatch = tuple[HydrationPriority, int, list[HydrationJob]]


def _group_hydration_batches(
    jobs: Sequence[HydrationJob], handlers: dict[str, HydrationHandler]
) -> dict[str, list[HydrationBatch]]:
    grouped: dict[tuple[str, HydrationPriority, int], list[tuple[int, HydrationJob]]] = defaultdict(list)
    for position, job in enumerate(jobs):
        grouped[(job.kind, job.priority, job.dialog_id)].append((position, job))

    by_kind: dict[str, list[HydrationBatch]] = defaultdict(list)
    for (kind, priority, _dialog_id), positioned_jobs in grouped.items():
        handler = handlers.get(kind)
        if handler is None:
            continue
        for offset in range(0, len(positioned_jobs), handler.batch_size):
            chunk = positioned_jobs[offset : offset + handler.batch_size]
            by_kind[kind].append((priority, chunk[0][0], [job for _, job in chunk]))
    for kind_batches in by_kind.values():
        kind_batches.sort(key=lambda batch: (-int(batch[0]), batch[1]))
    return by_kind


def _order_hydration_batches(
    by_kind: dict[str, list[HydrationBatch]], *, backfill_first: bool = False
) -> list[list[HydrationJob]]:
    ordered: list[list[HydrationJob]] = []
    priority_order = (
        (HydrationPriority.BACKFILL, HydrationPriority.FOREGROUND)
        if backfill_first
        else (HydrationPriority.FOREGROUND, HydrationPriority.BACKFILL)
    )
    for priority in priority_order:
        ordered.extend(_order_priority_batches(by_kind, priority))
    return ordered


def _order_priority_batches(
    by_kind: dict[str, list[HydrationBatch]], priority: HydrationPriority
) -> list[list[HydrationJob]]:
    tiered = {kind: [batch for batch in batches if batch[0] == priority] for kind, batches in by_kind.items()}
    ordered: list[list[HydrationJob]] = []
    for round_index in range(max((len(batches) for batches in tiered.values()), default=0)):
        round_batches = [batches[round_index] for batches in tiered.values() if round_index < len(batches)]
        round_batches.sort(key=lambda batch: _due_job_order_key(batch[2][0]))
        ordered.extend(batch[2] for batch in round_batches)
    return ordered


def batch_jobs(
    jobs: Sequence[HydrationJob],
    handlers: dict[str, HydrationHandler],
    *,
    backfill_first: bool = False,
) -> list[list[HydrationJob]]:
    """Batch compatible jobs without weakening queue priority order."""
    return _order_hydration_batches(_group_hydration_batches(jobs, handlers), backfill_first=backfill_first)


def _append_due_priority_tier(
    selected: list[HydrationJob],
    remaining: dict[str, list[HydrationJob]],
    priority: HydrationPriority,
    limit: int,
) -> None:
    tiered = {kind: [job for job in jobs if job.priority == priority] for kind, jobs in remaining.items()}
    while len(selected) < limit and any(tiered.values()):
        heads = sorted(
            ((jobs[0], kind) for kind, jobs in tiered.items() if jobs),
            key=lambda pair: _due_job_order_key(pair[0]),
        )
        for _head, kind in heads:
            if len(selected) >= limit:
                break
            selected.append(tiered[kind].pop(0))


class FactHydrationDemandAdapter(DurableDemandAdapter):
    """Expose one durable hydration tier to the sole demand coordinator.

    Live and backfill rows share ``hydration_jobs`` but have distinct demand
    contracts. The priority column is the authoritative partition. Missing
    historical jobs are observed without mutation and seeded only by a
    backfill slice.
    """

    def __init__(self, worker: MessageFactHydrationWorker, priority: HydrationPriority, *, account_id: int) -> None:
        if not isinstance(priority, HydrationPriority):
            raise TypeError("priority must be a HydrationPriority")
        self._worker = worker
        self._priority = priority
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        self._account_id = account_id
        self.demand_kind = _hydration_demand_kind(priority)

    def _revision(self, job: HydrationJob) -> WorkRevision:
        revision = hydration_work_revision(self._worker._conn, job)
        target = f"{job.kind}:{job.dialog_id}:{job.message_id}"
        return WorkRevision(WorkKey(self.demand_kind, self._account_id, target), revision)

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Return queued work or the next bounded backfill-repair time."""
        jobs = [
            job
            for kind in self._worker._handlers
            for job in self._worker._queue.due_jobs(
                int(now), self._worker._max_jobs_per_cycle, kind=kind, priority=self._priority
            )
        ]
        for job in jobs:
            selected = self._revision(job)
            if selected not in excluded:
                return DemandStatus(release_at=float(job.due_at), selected=selected)
        return None

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Seed and run one compatible queue batch within the attempt budget."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise ValueError("hydration demand requires a selected revision")
        now = self._worker._clock()
        selection = self._prepare_selected_job(selected, budget, now)
        if isinstance(selection, SliceOutcome):
            return selection
        with durable_demand_context(self.demand_kind):
            outcome = await self._worker.run_priority_slice(self._priority, budget, selected_job=selection)
        return self._slice_outcome(selected, outcome)

    def _prepare_selected_job(
        self, selected: WorkRevision, budget: RpcAttemptBudget, now: float
    ) -> HydrationJob | SliceOutcome:
        job = self._selected_job(selected)
        if job is None or self._revision(job) != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if job.due_at > int(now) or (budget.exhausted and not budget.can_prepare_retry):
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        return job

    @staticmethod
    def _slice_outcome(selected: WorkRevision, outcome: _BatchOutcome | None) -> SliceOutcome:
        if outcome is not None and outcome.stopped:
            if outcome.deferred_error is not None:
                raise outcome.deferred_error
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        if outcome is not None and outcome.terminal_revision is not None:
            return SliceOutcome(selected, SliceOutcomeKind.TERMINAL, outcome.terminal_revision)
        if (
            outcome is not None
            and outcome.committed_revision is not None
            and outcome.committed_revision != selected.revision
        ):
            if outcome.completed:
                return SliceOutcome(selected, SliceOutcomeKind.TERMINAL, outcome.committed_revision)
            return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, outcome.committed_revision)
        return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)

    def _selected_job(self, selected: WorkRevision) -> HydrationJob | None:
        kind, raw_dialog_id, raw_message_id = selected.key.target.rsplit(":", 2)
        row = cast(
            tuple[str, int, int, int, int, int, int, int] | None,
            self._worker._conn.execute(
                "SELECT kind,dialog_id,message_id,due_at,attempts,message_sent_at,priority,terminal "
                "FROM hydration_jobs WHERE kind=? AND dialog_id=? AND message_id=? AND priority=?",
                (kind, int(raw_dialog_id), int(raw_message_id), int(self._priority)),
            ).fetchone(),
        )
        if row is None:
            return None
        return HydrationJob(
            kind=str(row[0]),
            dialog_id=int(row[1]),
            message_id=int(row[2]),
            due_at=int(row[3]),
            attempts=int(row[4]),
            message_sent_at=int(row[5]),
            priority=HydrationPriority(int(row[6])),
            terminal=bool(row[7]),
        )


class MessageFactHydrationWorker:
    """Process all registered fact kinds through one bounded runner."""

    def __init__(  # noqa: PLR0913
        self,
        client: object,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        *,
        writer: SQLiteWriter,
        handlers: Sequence[HydrationHandler],
        interval_seconds: float,
        max_requests_per_cycle: int,
        max_jobs_per_cycle: int,
        retry_delay_seconds: int,
        circuit_retry_seconds: int,
        max_attempts: int,
        pause_between_requests_seconds: float,
        backfill_debt_limit: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        del circuit_retry_seconds, retry_delay_seconds
        self._client = client
        self._conn = conn
        self._writer = writer
        self._shutdown_event = shutdown_event
        self._handlers: dict[str, HydrationHandler] = {}
        for handler in handlers:
            if handler.kind in self._handlers:
                raise ValueError(f"fact hydration handler kind must be unique: {handler.kind}")
            self._handlers[handler.kind] = handler
        self._max_jobs_per_cycle = max_jobs_per_cycle
        if self._max_jobs_per_cycle < len(self._handlers):
            raise ValueError("fact hydration max_jobs_per_cycle must cover registered handler kinds")
        request_capacity = sum(handler.request_cost for handler in self._handlers.values())
        if max_requests_per_cycle < request_capacity:
            raise ValueError("fact hydration max_requests_per_cycle must cover registered handler costs")
        self._max_attempts = max_attempts
        self._clock = clock
        self._interval_seconds = interval_seconds
        self.next_repair_at = clock()
        self._repair_cursors: tuple[
            HydrationRepairCursor | None, HydrationRepairCursor | None, HydrationRepairCursor | None
        ] = (None, None, None)
        if backfill_debt_limit <= 0:
            raise ValueError("fact hydration backfill_debt_limit must be positive")
        self._queue = HydrationQueueRepository(conn)

    async def run_priority_slice(
        self, priority: HydrationPriority, budget: RpcAttemptBudget, *, selected_job: HydrationJob | None = None
    ) -> _BatchOutcome | None:
        """Run at most one due batch from a single durable priority tier."""
        if not isinstance(priority, HydrationPriority):
            raise TypeError("priority must be a HydrationPriority")
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        if self._shutdown_event.is_set():
            return None
        clock_now = self._clock()
        effective_now = int(clock_now)
        if priority is HydrationPriority.BACKFILL and selected_job is None:
            await self._run_due_repairs(clock_now, effective_now)
            effective_now = int(self._clock())
        if budget.exhausted and not budget.can_prepare_retry:
            return None
        batch = self._select_batch(priority, budget, selected_job, effective_now)
        if batch is None:
            return None
        return await self._process_batch(
            self._handlers[batch[0].kind],
            batch,
            effective_now,
            attempt_budget=budget,
        )

    def _select_batch(
        self,
        priority: HydrationPriority,
        budget: RpcAttemptBudget,
        selected_job: HydrationJob | None,
        effective_now: int,
    ) -> list[HydrationJob] | None:
        per_kind = (
            {selected_job.kind: [selected_job]}
            if selected_job is not None
            else {
                kind: self._queue.due_jobs(
                    effective_now,
                    self._max_jobs_per_cycle,
                    kind=kind,
                    priority=priority,
                )
                for kind in self._handlers
            }
        )
        selected: list[HydrationJob] = []
        _append_due_priority_tier(
            selected,
            per_kind,
            priority,
            self._max_jobs_per_cycle,
        )
        batches = batch_jobs(selected, self._handlers, backfill_first=priority is HydrationPriority.BACKFILL)
        # A method-recovery retry may still need the exact selected batch to
        # reach the RPC gate, which owns the physical-send decision. Preparing
        # this one bounded candidate does not reserve or debit any attempts.
        # Without an owner-selected job, do not let retry credit select ambient
        # work while the ordinary slice budget is exhausted.
        can_prepare_selected_retry = selected_job is not None and budget.exhausted and budget.can_prepare_retry
        return next(
            (
                candidate
                for candidate in batches
                if self._handlers[candidate[0].kind].request_cost <= budget.remaining or can_prepare_selected_retry
            ),
            None,
        )

    async def _run_due_repairs(self, clock_now: float, effective_now: int) -> None:
        if clock_now < self.next_repair_at:
            return

        def repair(
            conn: sqlite3.Connection,
        ) -> tuple[
            bool, tuple[HydrationRepairCursor | None, HydrationRepairCursor | None, HydrationRepairCursor | None]
        ]:
            if conn.in_transaction:
                raise RuntimeError("fact hydration repair requires no open transaction")
            with write_transaction(conn):
                return self._run_repair_producers(conn, effective_now)

        has_more, cursors = await self._writer.run(repair)
        self._repair_cursors = cursors if has_more else (None, None, None)
        self.next_repair_at = clock_now if has_more else self._clock() + self._interval_seconds

    def _run_repair_producers(
        self, conn: sqlite3.Connection, effective_now: int
    ) -> tuple[bool, tuple[HydrationRepairCursor | None, HydrationRepairCursor | None, HydrationRepairCursor | None]]:
        require_write_transaction(conn)
        has_more = False
        transcription_cursor, contact_other_cursor, video_cursor = self._repair_cursors
        if TRANSCRIPTION_HYDRATION_KIND in self._handlers:
            transcription = repair_transcription_hydration_jobs(
                conn, due_at=effective_now, max_jobs=self._max_jobs_per_cycle, cursor=transcription_cursor
            )
            has_more = transcription.has_more
            transcription_cursor = transcription.next_cursor
        media_handler = self._handlers.get(MEDIA_METADATA_KIND)
        if media_handler is not None:
            media = repair_media_metadata_hydration_jobs(
                conn,
                due_at=effective_now,
                max_jobs=min(media_handler.batch_size, self._max_jobs_per_cycle),
                contact_other_cursor=contact_other_cursor,
                video_cursor=video_cursor,
            )
            has_more = media.has_more or has_more
            contact_other_cursor = media.next_contact_other_cursor
            video_cursor = media.next_video_cursor
        return has_more, (transcription_cursor, contact_other_cursor, video_cursor)

    async def _process_batch(
        self,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        effective_now: int,
        *,
        attempt_budget: RpcAttemptBudget | None = None,
    ) -> _BatchOutcome:
        started, preflight_observations, observation_order = await self._writer.run(
            lambda conn: self._start_batch(conn, handler, batch)
        )
        if not started:
            self._log_drops(batch, preflight_observations)
            return _BatchOutcome(dropped=len(preflight_observations))
        attempts_before_request = None if attempt_budget is None else attempt_budget.attempts
        assert observation_order is not None
        failure: Exception | None = None
        result: object = None
        try:
            result = await self._request_batch(handler, started, attempt_budget=attempt_budget)
        except Exception as exc:  # noqa: BLE001 - route Telegram and admission failures by their existing policies
            failure = exc
        finally:
            effective_now = int(self._clock())

        def requeue_stale(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                for job in started:
                    if not hydration_observation_current(conn, job.dialog_id, job.message_id, observation_order):
                        HydrationQueueRepository(conn).requeue_not_applied(job, job.due_at)

        await self._writer.run(requeue_stale)
        if failure is not None:
            return await self._writer.run(
                lambda conn: self._handle_batch_error(
                    conn,
                    handler,
                    batch,
                    started,
                    preflight_observations,
                    failure,
                    effective_now,
                    observation_order=observation_order,
                    attempt_budget=attempt_budget,
                    attempts_before_request=attempts_before_request,
                )
            )
        return await self._writer.run(
            lambda conn: self._apply_batch_result(
                conn, handler, batch, started, preflight_observations, result, effective_now, observation_order
            )
        )

    def _handle_batch_error(  # noqa: PLR0913, PLR0917 - dispatch preserves existing per-error recovery policies
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: Exception,
        effective_now: int,
        *,
        observation_order: int,
        attempt_budget: RpcAttemptBudget | None,
        attempts_before_request: int | None,
    ) -> _BatchOutcome:
        if isinstance(
            exc,
            (
                TelegramRpcAdmissionDeferred,
                TelegramRpcMethodRecoveryPending,
                RpcAttemptBudgetExhaustedError,
                RpcAdmissionSaturatedError,
                RpcAdmissionExpiredError,
            ),
        ):
            return self._handle_admission_rejection(
                conn,
                handler,
                batch,
                started,
                preflight_observations,
                exc,
                effective_now,
                observation_order=observation_order,
            )
        if isinstance(exc, TelegramRpcThrottled):
            return self._handle_throttle(
                conn,
                handler,
                batch,
                started,
                preflight_observations,
                exc,
                effective_now,
                attempt_budget=attempt_budget,
                attempts_before_request=attempts_before_request,
                observation_order=observation_order,
            )
        if isinstance(exc, RpcAdmissionClosedError):
            with write_transaction(conn):
                self._release_undispatched(
                    conn,
                    self._current_jobs(conn, started, observation_order),
                    effective_now,
                    error_code=type(exc).__name__,
                )
            raise exc
        if isinstance(exc, ACCESS_LOST_ERRORS):
            return self._handle_access_lost(
                conn,
                handler,
                batch,
                started,
                preflight_observations,
                exc,
                effective_now,
                observation_order=observation_order,
            )
        return self._handle_request_error(
            conn,
            handler,
            batch,
            started,
            preflight_observations,
            exc,
            effective_now,
            observation_order=observation_order,
        )

    def _apply_batch_result(  # noqa: PLR0913, PLR0917 - one transaction gates result and its queue effects
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        result: object,
        effective_now: int,
        observation_order: int,
    ) -> _BatchOutcome:
        with write_transaction(conn):
            current = self._current_jobs(conn, started, observation_order)
            stale = [job for job in started if job not in current]
            preflight_observations = (*preflight_observations, *self._observations("not_applied", stale))
            if not current:
                self._log_drops(batch, preflight_observations)
                return _BatchOutcome(dropped=len(preflight_observations))
            applied = handler.apply(
                conn,
                HydrationQueueRepository(conn),
                current,
                result,
                now=effective_now,
                observation_order=observation_order,
            )
            return self._finish_applied(
                conn, handler, batch, current, preflight_observations, applied, int(self._clock())
            )

    def _current_jobs(
        self,
        conn: sqlite3.Connection,
        jobs: Sequence[HydrationJob],
        observation_order: int | None,
    ) -> list[HydrationJob]:
        require_write_transaction(conn)
        return [
            job for job in jobs if hydration_observation_current(conn, job.dialog_id, job.message_id, observation_order)
        ]

    async def _request_batch(
        self,
        handler: HydrationHandler,
        jobs: Sequence[HydrationJob],
        *,
        attempt_budget: RpcAttemptBudget | None = None,
    ) -> object:
        """Run one tier-specific request under its selected root demand."""
        source = _hydration_rpc_source(jobs[0].priority)

        async def request() -> object:
            with rpc_scope(source, acquisition_kind=AcquisitionKind.MESSAGE_LOOKUP):
                if attempt_budget is None:
                    return await handler.request(self._client, jobs)
                with rpc_attempt_budget(attempt_budget):
                    return await handler.request(self._client, jobs)

        async def dispatch() -> object:
            return await create_scoped_rpc_task(
                request(),
                source=source,
                name=f"fact-hydration-{jobs[0].priority.name.lower()}-batch",
                demand_token=current_demand_token(),
            )

        try:
            active_token = current_demand_token()
        except UnclassifiedTelegramDemandError:
            active_token = None
        if active_token is not None and active_token.kind is _hydration_demand_kind(jobs[0].priority):
            return await dispatch()
        return await create_scoped_rpc_task(
            request(),
            source=source,
            name=f"fact-hydration-{jobs[0].priority.name.lower()}-batch",
        )

    def _handle_admission_rejection(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: RpcAdmissionSaturatedError
        | RpcAdmissionExpiredError
        | TelegramRpcAdmissionDeferred
        | TelegramRpcMethodRecoveryPending
        | RpcAttemptBudgetExhaustedError,
        effective_now: int,
        *,
        observation_order: int | None = None,
    ) -> _BatchOutcome:
        with write_transaction(conn):
            retried = self._release_undispatched(
                conn,
                self._current_jobs(conn, started, observation_order),
                None,
                error_code=type(exc).__name__,
            )
        self._log_drops(batch, preflight_observations)
        logger.warning(
            "message_fact_hydration admission_rejected kind=%s dialog_id=%d jobs=%d error_type=%s",
            handler.kind,
            started[0].dialog_id,
            len(started),
            type(exc).__name__,
        )
        return _BatchOutcome(
            dropped=len(preflight_observations),
            retried=retried,
            stopped=True,
            deferred_error=exc
            if isinstance(exc, (TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending))
            else None,
        )

    def _release_undispatched(
        self,
        conn: sqlite3.Connection,
        jobs: Sequence[HydrationJob],
        due_at: int | None,
        *,
        error_code: str,
    ) -> int:
        require_write_transaction(conn)
        return sum(
            HydrationQueueRepository(conn).requeue_undispatched(
                job, job.due_at if due_at is None else due_at, error_code=error_code
            )
            for job in jobs
        )

    def _handle_flood_wait(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: TelegramRpcThrottled,
        effective_now: int,
        *,
        observation_order: int | None = None,
    ) -> _BatchOutcome:
        del conn, observation_order, effective_now
        descriptor = describe_telegram_rpc_error(exc)
        self._log_drops(batch, preflight_observations)
        logger.warning(
            "message_fact_hydration network_deferred kind=%s dialog_id=%d error_type=%s",
            handler.kind,
            started[0].dialog_id,
            descriptor.error_type,
        )
        return _BatchOutcome(stopped=True, deferred_error=exc)

    def _handle_throttle(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: TelegramRpcThrottled,
        effective_now: int,
        *,
        attempt_budget: RpcAttemptBudget | None,
        attempts_before_request: int | None,
        observation_order: int | None = None,
    ) -> _BatchOutcome:
        """Preserve domain state, then select coordinator or standalone recovery."""
        if attempt_budget is not None:
            assert attempts_before_request is not None
            self._handle_coordinator_throttle(
                conn,
                handler,
                batch,
                started,
                preflight_observations,
                exc,
                effective_now,
                request_dispatched=attempt_budget.attempts > attempts_before_request,
                observation_order=observation_order,
            )
            raise exc
        if exc.retry_after_seconds is None:
            return self._handle_circuit_open(handler, batch, started, preflight_observations, effective_now)
        return self._handle_flood_wait(
            conn,
            handler,
            batch,
            started,
            preflight_observations,
            exc,
            effective_now,
            observation_order=observation_order,
        )

    def _handle_coordinator_throttle(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: TelegramRpcThrottled,
        effective_now: int,
        *,
        request_dispatched: bool,
        observation_order: int | None = None,
    ) -> None:
        """Restore durable admission state before the coordinator owns throttling."""
        if request_dispatched and exc.retry_after_seconds is not None:
            self._handle_flood_wait(
                conn,
                handler,
                batch,
                started,
                preflight_observations,
                exc,
                effective_now,
                observation_order=observation_order,
            )
            return

        if request_dispatched:
            return
        with write_transaction(conn):
            error_code = type(exc).__name__
            current = self._current_jobs(conn, started, observation_order)
            for job in current:
                HydrationQueueRepository(conn).requeue_undispatched(job, job.due_at, error_code=error_code)
        self._log_drops(batch, preflight_observations)
        logger.info(
            "message_fact_hydration coordinator_throttled kind=%s dialog_id=%d jobs=%d dispatched=%s latched=%s",
            handler.kind,
            started[0].dialog_id,
            len(started),
            request_dispatched,
            exc.latched,
        )

    def _handle_circuit_open(
        self,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        effective_now: int,
    ) -> _BatchOutcome:
        self._log_drops(batch, preflight_observations)
        logger.info(
            "message_fact_hydration circuit_open kind=%s dialog_id=%d jobs=%d paused_until_reset=true",
            handler.kind,
            started[0].dialog_id,
            len(started),
        )
        return _BatchOutcome(
            dropped=len(preflight_observations),
            stopped=True,
        )

    def _handle_access_lost(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: BaseException,
        effective_now: int,
        *,
        observation_order: int | None = None,
    ) -> _BatchOutcome:
        descriptor = describe_telegram_rpc_error(exc)
        self._log_drops(batch, preflight_observations)
        with write_transaction(conn):
            dialog_ids = tuple(
                dict.fromkeys(job.dialog_id for job in self._current_jobs(conn, started, observation_order))
            )
            summaries = tuple(
                summary
                for dialog_id in dialog_ids
                for summary in HydrationQueueRepository(conn).summarize_for_dialog(dialog_id)
            )
            for dialog_id in dialog_ids:
                set_access_lost(conn, dialog_id, effective_now, reason=descriptor.error_type)
        self._log_summaries(summaries, descriptor)
        drop_counts: dict[str, int] = defaultdict(int)
        for summary in summaries:
            drop_counts[summary.kind] += summary.job_count
        for observation in preflight_observations:
            if observation.kind is not None:
                drop_counts[observation.kind] += 1
        dropped_by_kind = tuple(drop_counts.items())
        return _BatchOutcome(
            dropped=len(preflight_observations) + sum(summary.job_count for summary in summaries),
            dropped_by_kind=dropped_by_kind,
        )

    def _handle_request_error(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: BaseException,
        effective_now: int,
        *,
        observation_order: int | None = None,
    ) -> _BatchOutcome:
        descriptor = describe_telegram_rpc_error(exc)
        if handler.is_terminal_error(exc):
            committed_revision: str | None = None
            with write_transaction(conn):
                current = self._current_jobs(conn, started, observation_order)
                for job in current:
                    HydrationQueueRepository(conn).mark_terminal(
                        job, outcome=HydrationOutcome.TERMINAL_ERROR, error_code=descriptor.symbol
                    )
                if current:
                    committed_revision = self._revision_for_conn(conn, current[0])
            self._log_drops(batch, preflight_observations)
            self._log_drops(current, self._observations("terminal_rpc", current), descriptor=descriptor)
            stale = [job for job in started if job not in current]
            self._log_drops(stale, self._observations("not_applied", stale))
            return _BatchOutcome(
                dropped=len(preflight_observations) + len(started),
                terminal_revision=committed_revision,
            )
        with write_transaction(conn):
            current = self._current_jobs(conn, started, observation_order)
            for job in current:
                HydrationQueueRepository(conn).requeue_not_applied(job, job.due_at)
        self._log_drops(batch, preflight_observations)
        self._log_drops(started, self._observations("not_applied", current), descriptor=descriptor)
        logger.warning(
            "message_fact_hydration transient kind=%s dialog_id=%d jobs=%d error_type=%s",
            handler.kind,
            started[0].dialog_id,
            len(started),
            descriptor.error_type,
        )
        return _BatchOutcome(
            retried=len(current),
            dropped=len(preflight_observations),
        )

    def _finish_applied(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        applied: AppliedFacts,
        effective_now: int,
    ) -> _BatchOutcome:
        require_write_transaction(conn)
        pending = 0
        if applied.pending:
            pending, dropped, drop_observations = self._reschedule_or_drop(
                conn,
                handler,
                started,
                effective_now + handler.pending_delay_seconds,
                outcome=HydrationOutcome.TELEGRAM_PENDING,
            )
            applied = AppliedFacts(
                hydrated=applied.hydrated,
                completed=applied.completed,
                dropped=applied.dropped + dropped,
                drop_observations=applied.drop_observations + drop_observations,
            )
        self._log_drops(batch, preflight_observations)
        self._log_drops(started, applied.drop_observations)
        return _BatchOutcome(
            hydrated=applied.hydrated,
            completed=applied.completed,
            pending=pending,
            dropped=len(preflight_observations) + applied.dropped,
            committed_revision=(
                self._revision_for_conn(conn, started[0])
                if started and (applied.hydrated or applied.completed)
                else None
            ),
        )

    @staticmethod
    def _revision_for_conn(conn: sqlite3.Connection, job: HydrationJob) -> str:
        return hydration_work_revision(conn, job)

    def _start_batch(
        self, conn: sqlite3.Connection, handler: HydrationHandler, jobs: Sequence[HydrationJob]
    ) -> tuple[list[HydrationJob], tuple[HydrationDropObservation, ...], int | None]:
        with write_transaction(conn):
            started: list[HydrationJob] = []
            observations: list[HydrationDropObservation] = []
            for job in jobs:
                if not handler.eligible(conn, job):
                    HydrationQueueRepository(conn).remove(job)
                    observations.append(
                        HydrationDropObservation("ineligible", job.message_id, job.kind, job.dialog_id, job.attempts)
                    )
                    continue
                current = HydrationQueueRepository(conn).start(job)
                if current is None:
                    continue
                if current.attempts > self._max_attempts:
                    HydrationQueueRepository(conn).mark_terminal(current, outcome=HydrationOutcome.EXHAUSTED)
                    observations.append(
                        HydrationDropObservation(
                            "attempt_limit", current.message_id, current.kind, current.dialog_id, current.attempts
                        )
                    )
                    continue
                started.append(current)
            observation_order = allocate_observation_order(conn) if started else None
        return started, tuple(observations), observation_order

    def _reschedule_or_drop(  # noqa: PLR0913 - explicit writer connection accompanies queue policy
        self,
        conn: sqlite3.Connection,
        handler: HydrationHandler,
        jobs: Sequence[HydrationJob],
        due_at: int,
        *,
        outcome: HydrationOutcome,
        error_code: str | None = None,
        terminal_on_max: bool = True,
    ) -> tuple[int, int, tuple[HydrationDropObservation, ...]]:
        require_write_transaction(conn)
        rescheduled = dropped = 0
        observations: list[HydrationDropObservation] = []
        for job in jobs:
            if terminal_on_max and job.attempts >= self._max_attempts:
                HydrationQueueRepository(conn).mark_terminal(
                    job, outcome=HydrationOutcome.EXHAUSTED, error_code=error_code
                )
                dropped += 1
                observations.append(
                    HydrationDropObservation("attempt_limit", job.message_id, job.kind, job.dialog_id, job.attempts)
                )
            elif HydrationQueueRepository(conn).reschedule(
                job, job.due_at if due_at is None else due_at, outcome=outcome, error_code=error_code
            ):
                rescheduled += 1
        return rescheduled, dropped, tuple(observations)

    @staticmethod
    def _observations(reason: str, jobs: Sequence[HydrationJob]) -> tuple[HydrationDropObservation, ...]:
        return tuple(
            HydrationDropObservation(reason, job.message_id, job.kind, job.dialog_id, job.attempts) for job in jobs
        )

    def _log_drops(
        self,
        jobs: Sequence[HydrationJob],
        observations: Sequence[HydrationDropObservation],
        *,
        descriptor: TelegramRpcErrorDescriptor | None = None,
    ) -> None:
        for drop in self._aggregate_drops(jobs, observations, descriptor):
            self._emit_drop(drop)

    def _log_summaries(
        self, summaries: Sequence[HydrationQueueSummary], descriptor: TelegramRpcErrorDescriptor
    ) -> None:
        for summary in summaries:
            self._emit_drop(
                HydrationDrop(
                    reason="access_lost",
                    kind=summary.kind,
                    dialog_id=summary.dialog_id,
                    job_count=summary.job_count,
                    message_ids=summary.message_ids,
                    attempts_min=summary.attempts_min,
                    attempts_max=summary.attempts_max,
                    error_type=descriptor.error_type,
                    rpc_code=descriptor.code,
                    rpc_symbol=descriptor.symbol,
                )
            )

    @staticmethod
    def _emit_drop(drop: HydrationDrop) -> None:
        logger.log(
            _DROP_LEVELS[drop.reason],
            "message_fact_hydration_drop reason=%s kind=%s dialog_id=%d job_count=%d "
            "message_ids=%s attempts_min=%d attempts_max=%d error_type=%s rpc_code=%s rpc_symbol=%s",
            drop.reason,
            drop.kind,
            drop.dialog_id,
            drop.job_count,
            drop.message_ids,
            drop.attempts_min,
            drop.attempts_max,
            drop.error_type,
            drop.rpc_code,
            drop.rpc_symbol,
        )

    @staticmethod
    def _resolve_observation_job(
        jobs: Sequence[HydrationJob], observation: HydrationDropObservation
    ) -> HydrationJob | None:
        candidates = [
            job
            for job in jobs
            if job.message_id == observation.message_id
            and (observation.kind is None or job.kind == observation.kind)
            and (observation.dialog_id is None or job.dialog_id == observation.dialog_id)
        ]
        if len(candidates) != 1:
            return None
        job = candidates[0]
        return replace(job, attempts=observation.attempts) if observation.attempts is not None else job

    @classmethod
    def _aggregate_drops(
        cls,
        jobs: Sequence[HydrationJob],
        observations: Sequence[HydrationDropObservation],
        descriptor: TelegramRpcErrorDescriptor | None,
    ) -> tuple[HydrationDrop, ...]:
        error_fields = (
            None if descriptor is None else descriptor.error_type,
            None if descriptor is None else descriptor.code,
            None if descriptor is None else descriptor.symbol,
        )
        grouped: dict[tuple[str, str, int, str | None, int | None, str | None], list[HydrationJob]] = {}
        for observation in observations:
            job = cls._resolve_observation_job(jobs, observation)
            if job is None:
                continue
            key = (observation.reason, job.kind, job.dialog_id, *error_fields)
            grouped.setdefault(key, []).append(job)
        return tuple(
            HydrationDrop(
                reason=reason,
                kind=kind,
                dialog_id=dialog_id,
                job_count=len(grouped_jobs),
                message_ids=tuple(job.message_id for job in grouped_jobs[:_MAX_LOGGED_MESSAGE_IDS]),
                attempts_min=min(job.attempts for job in grouped_jobs),
                attempts_max=max(job.attempts for job in grouped_jobs),
                error_type=error_type,
                rpc_code=rpc_code,
                rpc_symbol=rpc_symbol,
            )
            for (reason, kind, dialog_id, error_type, rpc_code, rpc_symbol), grouped_jobs in grouped.items()
        )


__all__ = [
    "AppliedFacts",
    "FactHydrationDemandAdapter",
    "HydrationDrop",
    "HydrationDropObservation",
    "HydrationHandler",
    "MessageFactHydrationWorker",
    "batch_jobs",
]
