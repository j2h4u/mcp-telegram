"""Shared bounded runner for durable message-fact hydration jobs."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from .access_lifecycle import set_access_lost
from .flood import TelegramRpcThrottled
from .hydration_queue import (
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationJob,
    HydrationPriority,
    HydrationQueueKindSnapshot,
    HydrationQueueRepository,
    HydrationQueueSummary,
)
from .messages.sqlite_repository import repair_transcription_hydration_jobs
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_rpc_error import TelegramRpcErrorDescriptor, describe_telegram_rpc_error

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


@dataclass(frozen=True, slots=True)
class FactHydrationCycleResult:
    """Sanitized counters for one bounded worker cycle."""

    requests: int = 0
    hydrated: int = 0
    completed: int = 0
    pending: int = 0
    retried: int = 0
    dropped: int = 0
    stopped: bool = False
    repaired_transcription_jobs: int = 0
    repair_has_more: bool = False


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

    def apply(
        self,
        conn: sqlite3.Connection,
        queue: HydrationQueueRepository,
        jobs: Sequence[HydrationJob],
        result: object,
        *,
        now: int,
    ) -> AppliedFacts: ...

    def is_terminal_error(self, exc: BaseException) -> bool: ...


@dataclass(frozen=True, slots=True)
class _BatchOutcome:
    requests: int = 0
    hydrated: int = 0
    completed: int = 0
    pending: int = 0
    retried: int = 0
    dropped: int = 0
    stopped: bool = False
    dropped_by_kind: tuple[tuple[str, int], ...] = ()


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


def _order_hydration_batches(by_kind: dict[str, list[HydrationBatch]]) -> list[list[HydrationJob]]:
    ordered: list[list[HydrationJob]] = []
    for priority in (HydrationPriority.FOREGROUND, HydrationPriority.BACKFILL):
        tiered = {kind: [batch for batch in batches if batch[0] == priority] for kind, batches in by_kind.items()}
        for round_index in range(max((len(batches) for batches in tiered.values()), default=0)):
            round_batches = [batches[round_index] for batches in tiered.values() if round_index < len(batches)]
            round_batches.sort(key=lambda batch: _due_job_order_key(batch[2][0]))
            ordered.extend(batch[2] for batch in round_batches)
    return ordered


def batch_jobs(
    jobs: Sequence[HydrationJob],
    handlers: dict[str, HydrationHandler],
) -> list[list[HydrationJob]]:
    """Batch compatible jobs without weakening queue priority order."""
    return _order_hydration_batches(_group_hydration_batches(jobs, handlers))


def _load_due_jobs_by_kind(
    queue: HydrationQueueRepository,
    effective_now: int,
    limit: int,
    handlers: dict[str, HydrationHandler],
) -> dict[str, list[HydrationJob]]:
    return {kind: queue.due_jobs(effective_now, limit, kind=kind) for kind in handlers}


def _protect_due_heads(
    per_kind: dict[str, list[HydrationJob]],
) -> tuple[list[HydrationJob], dict[str, list[HydrationJob]]]:
    remaining = {kind: list(jobs) for kind, jobs in per_kind.items()}
    protected = [jobs.pop(0) for jobs in remaining.values() if jobs]
    protected.sort(key=lambda job: (-int(job.priority), *_due_job_order_key(job)))
    return protected, remaining


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


class MessageFactHydrationWorker:
    """Process all registered fact kinds through one bounded runner."""

    def __init__(  # noqa: PLR0913
        self,
        client: object,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        *,
        handlers: Sequence[HydrationHandler],
        interval_seconds: float,
        max_requests_per_cycle: int,
        max_jobs_per_cycle: int,
        retry_delay_seconds: int,
        circuit_retry_seconds: int,
        max_attempts: int,
        pause_between_requests_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._handlers: dict[str, HydrationHandler] = {}
        for handler in handlers:
            if handler.kind in self._handlers:
                raise ValueError(f"fact hydration handler kind must be unique: {handler.kind}")
            self._handlers[handler.kind] = handler
        self._max_requests_per_cycle = max_requests_per_cycle
        self._max_jobs_per_cycle = max_jobs_per_cycle
        if self._max_jobs_per_cycle < len(self._handlers):
            raise ValueError("fact hydration max_jobs_per_cycle must cover registered handler kinds")
        request_capacity = sum(handler.request_cost for handler in self._handlers.values())
        if self._max_requests_per_cycle < request_capacity:
            raise ValueError("fact hydration max_requests_per_cycle must cover registered handler costs")
        self._interval_seconds = interval_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._max_attempts = max_attempts
        self._pause_between_requests_seconds = pause_between_requests_seconds
        self._clock = clock
        self._queue = HydrationQueueRepository(conn)

    async def run_cycle(self, *, now: int | None = None) -> FactHydrationCycleResult:
        effective_now = int(self._clock()) if now is None else now
        repair = (
            repair_transcription_hydration_jobs(self._conn, due_at=effective_now, max_jobs=self._max_jobs_per_cycle)
            if TRANSCRIPTION_HYDRATION_KIND in self._handlers
            else None
        )
        # The repair is a producer transaction. Commit before any awaited RPC.
        self._conn.commit()
        due = self._fair_due_jobs(effective_now)
        if not due:
            result = FactHydrationCycleResult(
                repaired_transcription_jobs=0 if repair is None else repair.enqueued,
                repair_has_more=False if repair is None else repair.has_more,
            )
            self._log_cycle(result, {}, self._queue.snapshot(effective_now), now=effective_now)
            return result
        selected_by_kind: dict[str, int] = defaultdict(int)
        for job in due:
            selected_by_kind[job.kind] += 1
        request_batches = batch_jobs(due, self._handlers)
        outcome, per_kind = await self._run_batches(request_batches, int(effective_now))
        result = replace(
            outcome,
            repaired_transcription_jobs=0 if repair is None else repair.enqueued,
            repair_has_more=False if repair is None else repair.has_more,
        )
        self._log_cycle(
            result,
            per_kind,
            self._queue.snapshot(effective_now),
            now=effective_now,
            selected=len(due),
            selected_by_kind=selected_by_kind,
        )
        return result

    def _fair_due_jobs(self, effective_now: int) -> list[HydrationJob]:
        per_kind = _load_due_jobs_by_kind(self._queue, effective_now, self._max_jobs_per_cycle, self._handlers)
        selected, remaining = _protect_due_heads(per_kind)
        for priority in (HydrationPriority.FOREGROUND, HydrationPriority.BACKFILL):
            _append_due_priority_tier(selected, remaining, priority, self._max_jobs_per_cycle)
        return selected

    def _log_cycle(  # noqa: PLR0913 - one log record combines cycle outcome with current queue state
        self,
        result: FactHydrationCycleResult,
        per_kind: dict[str, tuple[int, int, int, int, int, int]],
        queue_snapshot: Sequence[HydrationQueueKindSnapshot],
        *,
        now: int,
        selected: int = 0,
        selected_by_kind: dict[str, int] | None = None,
    ) -> None:
        snapshot_by_kind = {snapshot.kind: snapshot for snapshot in queue_snapshot}
        logger.info(
            "message_fact_hydration cycle selected=%d requests=%d hydrated=%d completed=%d "
            "pending=%d retried=%d dropped=%d stopped=%s repaired_transcription_jobs=%d repair_has_more=%s "
            "queue_active=%d queue_ready=%d queue_deferred=%d queue_terminal=%d",
            selected,
            result.requests,
            result.hydrated,
            result.completed,
            result.pending,
            result.retried,
            result.dropped,
            result.stopped,
            result.repaired_transcription_jobs,
            result.repair_has_more,
            sum(snapshot.active for snapshot in queue_snapshot),
            sum(snapshot.ready for snapshot in queue_snapshot),
            sum(snapshot.deferred for snapshot in queue_snapshot),
            sum(snapshot.terminal for snapshot in queue_snapshot),
        )
        selected_by_kind = selected_by_kind or {}
        for kind in self._handlers:
            requests, hydrated, completed, pending, retried, dropped = per_kind.get(kind, (0, 0, 0, 0, 0, 0))
            snapshot = snapshot_by_kind.get(
                kind,
                HydrationQueueKindSnapshot(
                    kind=kind,
                    active=0,
                    ready=0,
                    foreground=0,
                    backfill=0,
                    terminal=0,
                    oldest_message_sent_at=None,
                    newest_message_sent_at=None,
                    max_attempts=0,
                ),
            )
            logger.debug(
                "message_fact_hydration kind=%s selected=%d requests=%d hydrated=%d completed=%d pending=%d "
                "retried=%d dropped=%d queue_active=%d queue_ready=%d queue_deferred=%d "
                "queue_foreground=%d queue_backfill=%d "
                "queue_terminal=%d oldest_message_age_s=%s newest_message_age_s=%s max_attempts=%d",
                kind,
                selected_by_kind.get(kind, 0),
                requests,
                hydrated,
                completed,
                pending,
                retried,
                dropped,
                snapshot.active,
                snapshot.ready,
                snapshot.deferred,
                snapshot.foreground,
                snapshot.backfill,
                snapshot.terminal,
                self._message_age(now, snapshot.oldest_message_sent_at),
                self._message_age(now, snapshot.newest_message_sent_at),
                snapshot.max_attempts,
            )

    @staticmethod
    def _message_age(now: int, sent_at: int | None) -> int | None:
        return None if sent_at is None else max(0, now - sent_at)

    async def _run_batches(
        self, request_batches: Sequence[Sequence[HydrationJob]], effective_now: int
    ) -> tuple[FactHydrationCycleResult, dict[str, tuple[int, int, int, int, int, int]]]:
        requests = hydrated = completed = pending = retried = dropped = 0
        stopped = False
        used_requests = 0
        per_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
        for batch_index, batch in enumerate(request_batches):
            handler = self._handlers[batch[0].kind]
            if used_requests + handler.request_cost > self._max_requests_per_cycle:
                break
            if self._shutdown_event.is_set():
                break
            used_requests += handler.request_cost
            outcome = await self._process_batch(handler, batch, effective_now)
            counters = per_kind[handler.kind]
            counters[0] += outcome.requests
            counters[1] += outcome.hydrated
            counters[2] += outcome.completed
            counters[3] += outcome.pending
            counters[4] += outcome.retried
            counters[5] += outcome.dropped
            for kind, dropped_for_kind in outcome.dropped_by_kind:
                per_kind[kind][5] += dropped_for_kind - (outcome.dropped if kind == handler.kind else 0)
            requests += outcome.requests
            hydrated += outcome.hydrated
            completed += outcome.completed
            pending += outcome.pending
            retried += outcome.retried
            dropped += outcome.dropped
            if outcome.stopped:
                stopped = True
                break
            if batch_index + 1 < len(request_batches) and await self._pause_between_requests():
                break
        return FactHydrationCycleResult(
            requests=requests,
            hydrated=hydrated,
            completed=completed,
            pending=pending,
            retried=retried,
            dropped=dropped,
            stopped=stopped,
        ), {
            kind: (counters[0], counters[1], counters[2], counters[3], counters[4], counters[5])
            for kind, counters in per_kind.items()
        }

    async def _process_batch(
        self,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        effective_now: int,
    ) -> _BatchOutcome:
        started, preflight_observations = self._start_batch(handler, batch)
        if not started:
            self._log_drops(batch, preflight_observations)
            return _BatchOutcome(dropped=len(preflight_observations))
        try:
            result = await handler.request(self._client, started)
        except TelegramRpcThrottled as exc:
            if exc.retry_after_seconds is None:
                return self._handle_circuit_open(handler, batch, started, preflight_observations, effective_now)
            return self._handle_flood_wait(handler, batch, started, preflight_observations, exc, effective_now)
        except ACCESS_LOST_ERRORS as exc:
            return self._handle_access_lost(handler, batch, started, preflight_observations, exc, effective_now)
        except Exception as exc:  # noqa: BLE001 - Telegram transient classes vary by RPC layer
            return self._handle_request_error(handler, batch, started, preflight_observations, exc, effective_now)

        applied = handler.apply(self._conn, self._queue, started, result, now=effective_now)
        return self._finish_applied(handler, batch, started, preflight_observations, applied, effective_now)

    def _handle_flood_wait(  # noqa: PLR0913, PLR0917
        self,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: TelegramRpcThrottled,
        effective_now: int,
    ) -> _BatchOutcome:
        retry_delay = exc.retry_after_seconds
        if retry_delay is None:
            return self._handle_circuit_open(handler, batch, started, preflight_observations, effective_now)
        retried, dropped, drop_observations = self._reschedule_or_drop(handler, started, effective_now + retry_delay)
        self._conn.commit()
        self._log_drops(
            batch,
            preflight_observations,
        )
        self._log_drops(
            started,
            drop_observations,
            descriptor=describe_telegram_rpc_error(exc) if drop_observations else None,
        )
        logger.warning(
            "message_fact_hydration flood_wait kind=%s dialog_id=%d jobs=%d retry_s=%d",
            handler.kind,
            started[0].dialog_id,
            len(started),
            retry_delay,
        )
        return _BatchOutcome(
            requests=handler.request_cost,
            retried=retried,
            dropped=len(preflight_observations) + dropped,
            stopped=True,
        )

    def _handle_circuit_open(
        self,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        effective_now: int,
    ) -> _BatchOutcome:
        self._conn.commit()
        self._log_drops(batch, preflight_observations)
        logger.info(
            "message_fact_hydration circuit_open kind=%s dialog_id=%d jobs=%d paused_until_reset=true",
            handler.kind,
            started[0].dialog_id,
            len(started),
        )
        return _BatchOutcome(
            requests=handler.request_cost,
            dropped=len(preflight_observations),
            stopped=True,
        )

    def _handle_access_lost(  # noqa: PLR0913, PLR0917
        self,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: BaseException,
        effective_now: int,
    ) -> _BatchOutcome:
        descriptor = describe_telegram_rpc_error(exc)
        self._log_drops(batch, preflight_observations)
        dialog_ids = tuple(dict.fromkeys(job.dialog_id for job in started))
        summaries = tuple(
            summary for dialog_id in dialog_ids for summary in self._queue.summarize_for_dialog(dialog_id)
        )
        for dialog_id in dialog_ids:
            set_access_lost(self._conn, dialog_id, effective_now, reason=descriptor.error_type)
        self._conn.commit()
        self._log_summaries(summaries, descriptor)
        drop_counts: dict[str, int] = defaultdict(int)
        for summary in summaries:
            drop_counts[summary.kind] += summary.job_count
        for observation in preflight_observations:
            if observation.kind is not None:
                drop_counts[observation.kind] += 1
        dropped_by_kind = tuple(drop_counts.items())
        return _BatchOutcome(
            requests=handler.request_cost,
            dropped=len(preflight_observations) + sum(summary.job_count for summary in summaries),
            dropped_by_kind=dropped_by_kind,
        )

    def _handle_request_error(  # noqa: PLR0913, PLR0917
        self,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        exc: BaseException,
        effective_now: int,
    ) -> _BatchOutcome:
        descriptor = describe_telegram_rpc_error(exc)
        if handler.is_terminal_error(exc):
            for job in started:
                self._queue.mark_terminal(job)
            self._conn.commit()
            self._log_drops(batch, preflight_observations)
            self._log_drops(started, self._observations("terminal_rpc", started), descriptor=descriptor)
            return _BatchOutcome(requests=handler.request_cost, dropped=len(preflight_observations) + len(started))
        retried, dropped, drop_observations = self._reschedule_or_drop(
            handler, started, effective_now + self._retry_delay_seconds
        )
        self._conn.commit()
        self._log_drops(batch, preflight_observations)
        self._log_drops(started, drop_observations, descriptor=descriptor)
        logger.warning(
            "message_fact_hydration transient kind=%s dialog_id=%d jobs=%d error_type=%s",
            handler.kind,
            started[0].dialog_id,
            len(started),
            descriptor.error_type,
        )
        return _BatchOutcome(
            requests=handler.request_cost,
            retried=retried,
            dropped=len(preflight_observations) + dropped,
        )

    def _finish_applied(  # noqa: PLR0913, PLR0917
        self,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        started: Sequence[HydrationJob],
        preflight_observations: Sequence[HydrationDropObservation],
        applied: AppliedFacts,
        effective_now: int,
    ) -> _BatchOutcome:
        pending = 0
        if applied.pending:
            pending, dropped, drop_observations = self._reschedule_or_drop(
                handler,
                started,
                effective_now + handler.pending_delay_seconds,
            )
            applied = AppliedFacts(
                hydrated=applied.hydrated,
                completed=applied.completed,
                dropped=applied.dropped + dropped,
                drop_observations=applied.drop_observations + drop_observations,
            )
        self._log_drops(batch, preflight_observations)
        self._log_drops(started, applied.drop_observations)
        self._conn.commit()
        return _BatchOutcome(
            requests=handler.request_cost,
            hydrated=applied.hydrated,
            completed=applied.completed,
            pending=pending,
            dropped=len(preflight_observations) + applied.dropped,
        )

    def _start_batch(
        self, handler: HydrationHandler, jobs: Sequence[HydrationJob]
    ) -> tuple[list[HydrationJob], tuple[HydrationDropObservation, ...]]:
        started: list[HydrationJob] = []
        observations: list[HydrationDropObservation] = []
        for job in jobs:
            if not handler.eligible(self._conn, job):
                self._queue.remove(job)
                observations.append(
                    HydrationDropObservation("ineligible", job.message_id, job.kind, job.dialog_id, job.attempts)
                )
                continue
            current = self._queue.start(job)
            if current is None:
                continue
            if current.attempts > self._max_attempts:
                if current.kind == TRANSCRIPTION_HYDRATION_KIND:
                    self._queue.mark_terminal(current)
                else:
                    self._queue.remove(current)
                observations.append(
                    HydrationDropObservation(
                        "attempt_limit", current.message_id, current.kind, current.dialog_id, current.attempts
                    )
                )
                continue
            started.append(current)
        self._conn.commit()
        return started, tuple(observations)

    def _reschedule_or_drop(
        self, handler: HydrationHandler, jobs: Sequence[HydrationJob], due_at: int
    ) -> tuple[int, int, tuple[HydrationDropObservation, ...]]:
        rescheduled = dropped = 0
        observations: list[HydrationDropObservation] = []
        for job in jobs:
            if job.attempts >= self._max_attempts:
                if handler.kind == TRANSCRIPTION_HYDRATION_KIND:
                    self._queue.mark_terminal(job)
                else:
                    self._queue.remove(job)
                dropped += 1
                observations.append(
                    HydrationDropObservation("attempt_limit", job.message_id, job.kind, job.dialog_id, job.attempts)
                )
            elif self._queue.reschedule(job, due_at):
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

    async def _pause_between_requests(self) -> bool:
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._pause_between_requests_seconds)
            return True
        except TimeoutError:
            return False

    async def run(self) -> None:
        while not self._shutdown_event.is_set():
            await self.run_cycle()
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue


__all__ = [
    "AppliedFacts",
    "FactHydrationCycleResult",
    "HydrationDrop",
    "HydrationDropObservation",
    "HydrationHandler",
    "MessageFactHydrationWorker",
    "batch_jobs",
]
