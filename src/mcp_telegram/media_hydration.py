"""Bounded lazy hydration of normalized media facts.

Only the daemon owns this worker.  Queue state is committed before each RPC,
so a process crash cannot turn an in-flight request into an unbounded retry.
The worker never rewrites message text or FTS: media columns are an isolated
projection and are updated with one narrow SQL statement.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

from .access_lifecycle import set_access_lost
from .flood import flood_seconds
from .hydration_queue import HydrationJob, HydrationPriority, HydrationQueueRepository
from .media_fact import encode_media_payload
from .messages.sqlite_repository import apply_hydrated_media_fact, media_hydration_eligible
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_rpc import TelegramRpcCircuitOpenError
from .telethon_media import extract_media_fact

logger = logging.getLogger(__name__)

MEDIA_METADATA_KIND = "media_metadata"


class MediaHydrationClient(Protocol):
    async def get_messages(self, *_args: object, **_kwargs: object) -> object: ...


@dataclass(frozen=True, slots=True)
class MediaHydrationCycleResult:
    """Sanitized counters for one bounded worker cycle."""

    requests: int = 0
    hydrated: int = 0
    completed: int = 0
    retried: int = 0
    dropped: int = 0
    stopped: bool = False


@dataclass(frozen=True, slots=True)
class _BatchOutcome:
    """Counters and control signal produced by one attempted RPC batch."""

    requests: int = 0
    hydrated: int = 0
    completed: int = 0
    retried: int = 0
    dropped: int = 0
    stopped: bool = False


def _response_items(result: object) -> list[object]:
    if result is None:
        return []
    if getattr(result, "id", None) is not None:
        return [result]
    if isinstance(result, (str, bytes, dict)):
        return []
    try:
        return list(cast(Sequence[object], result))
    except TypeError:
        return []


def _response_map(result: object) -> dict[int, object]:
    mapped: dict[int, object] = {}
    for item in _response_items(result):
        raw_id = getattr(item, "id", None)
        if isinstance(raw_id, int) and raw_id > 0:
            mapped[raw_id] = item
    return mapped


def _batch_jobs(jobs: Sequence[HydrationJob], batch_size: int) -> list[list[HydrationJob]]:
    """Batch per Telegram peer without weakening queue priority order."""
    grouped: dict[tuple[HydrationPriority, int], list[tuple[int, HydrationJob]]] = defaultdict(list)
    for position, job in enumerate(jobs):
        grouped[(job.priority, job.dialog_id)].append((position, job))

    positioned_batches: list[tuple[HydrationPriority, int, list[HydrationJob]]] = []
    for (priority, _dialog_id), positioned_jobs in grouped.items():
        for offset in range(0, len(positioned_jobs), batch_size):
            chunk = positioned_jobs[offset : offset + batch_size]
            positioned_batches.append((priority, chunk[0][0], [job for _, job in chunk]))

    positioned_batches.sort(key=lambda batch: (-int(batch[0]), batch[1]))
    return [batch for _priority, _position, batch in positioned_batches]


class MediaHydrationWorker:
    """Process due ``media_metadata`` jobs through a governed client."""

    def __init__(  # noqa: PLR0913
        self,
        client: MediaHydrationClient,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        *,
        interval_seconds: float,
        max_requests_per_cycle: int,
        max_jobs_per_cycle: int,
        batch_size: int,
        pause_between_requests_seconds: float,
        retry_delay_seconds: int,
        circuit_retry_seconds: int,
        max_attempts: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._interval_seconds = interval_seconds
        self._max_requests_per_cycle = max_requests_per_cycle
        self._max_jobs_per_cycle = max_jobs_per_cycle
        self._batch_size = batch_size
        self._pause_between_requests_seconds = pause_between_requests_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._circuit_retry_seconds = circuit_retry_seconds
        self._max_attempts = max_attempts
        self._clock = clock
        self._queue = HydrationQueueRepository(conn)

    async def run_cycle(self, *, now: int | None = None) -> MediaHydrationCycleResult:
        """Process one deterministic, request- and job-bounded due pass."""
        effective_now = int(self._clock()) if now is None else now
        due = self._queue.due_jobs(effective_now, self._max_jobs_per_cycle, kind=MEDIA_METADATA_KIND)
        if not due:
            return MediaHydrationCycleResult()

        requests = hydrated = completed = retried = dropped = 0
        stopped = False
        request_batches = _batch_jobs(due, self._batch_size)[: self._max_requests_per_cycle]
        jobs_by_priority = Counter(job.priority for job in due)
        foreground_jobs = jobs_by_priority[HydrationPriority.FOREGROUND]
        backfill_jobs = jobs_by_priority[HydrationPriority.BACKFILL]
        for batch_index, batch in enumerate(request_batches):
            if self._shutdown_event.is_set():
                break
            outcome = await self._process_batch(batch, effective_now)
            if outcome is None:
                continue
            requests += outcome.requests
            hydrated += outcome.hydrated
            completed += outcome.completed
            retried += outcome.retried
            dropped += outcome.dropped
            if outcome.stopped:
                stopped = True
                break
            if batch_index + 1 < len(request_batches) and await self._pause_between_requests():
                break

        logger.info(
            "media_hydration cycle selected_foreground=%d selected_backfill=%d "
            "requests=%d hydrated=%d completed=%d retried=%d dropped=%d stopped=%s",
            foreground_jobs,
            backfill_jobs,
            requests,
            hydrated,
            completed,
            retried,
            dropped,
            stopped,
        )
        return MediaHydrationCycleResult(requests, hydrated, completed, retried, dropped, stopped)

    async def _process_batch(self, batch: Sequence[HydrationJob], effective_now: int) -> _BatchOutcome | None:
        started = self._start_batch(batch)
        if not started:
            return None
        try:
            result = await self._client.get_messages(
                entity=started[0].dialog_id,
                ids=[job.message_id for job in started],
            )
        except FloodWaitError as exc:
            retry_delay = flood_seconds(exc, source="media_hydration")
            retried, dropped = self._retry_or_drop(started, effective_now + retry_delay)
            self._conn.commit()
            logger.warning(
                "media_hydration flood_wait dialog_id=%d jobs=%d retry_s=%d",
                started[0].dialog_id,
                len(started),
                retry_delay,
            )
            return _BatchOutcome(requests=1, retried=retried, dropped=dropped, stopped=True)
        except TelegramRpcCircuitOpenError:
            retried, dropped = self._retry_or_drop(started, effective_now + self._circuit_retry_seconds)
            self._conn.commit()
            logger.info(
                "media_hydration circuit_open dialog_id=%d jobs=%d retry_s=%d",
                started[0].dialog_id,
                len(started),
                self._circuit_retry_seconds,
            )
            return _BatchOutcome(requests=1, retried=retried, dropped=dropped, stopped=True)
        except ACCESS_LOST_ERRORS as exc:
            set_access_lost(self._conn, started[0].dialog_id, effective_now, reason=type(exc).__name__)
            self._conn.commit()
            logger.info("media_hydration access_lost dialog_id=%d jobs=%d", started[0].dialog_id, len(started))
            return _BatchOutcome(requests=1, dropped=len(started))
        except Exception as exc:  # noqa: BLE001 - Telegram transient classes vary by RPC layer
            retried, dropped = self._retry_or_drop(started, effective_now + self._retry_delay_seconds)
            self._conn.commit()
            logger.warning(
                "media_hydration transient dialog_id=%d jobs=%d error_type=%s",
                started[0].dialog_id,
                len(started),
                type(exc).__name__,
            )
            return _BatchOutcome(requests=1, retried=retried, dropped=dropped)

        hydrated, completed, dropped = self._apply_authoritative(started, result)
        self._conn.commit()
        return _BatchOutcome(requests=1, hydrated=hydrated, completed=completed, dropped=dropped)

    def _start_batch(self, jobs: Sequence[HydrationJob]) -> list[HydrationJob]:
        started: list[HydrationJob] = []
        for job in jobs:
            if not media_hydration_eligible(self._conn, job.dialog_id):
                self._queue.remove(job)
                continue
            current = self._queue.start(job)
            if current is None:
                continue
            if current.attempts > self._max_attempts:
                self._queue.remove(current)
                continue
            started.append(current)
        # This commit is deliberately before the Telegram RPC.
        self._conn.commit()
        return started

    def _retry_or_drop(self, jobs: Sequence[HydrationJob], due_at: int) -> tuple[int, int]:
        retried = dropped = 0
        for job in jobs:
            if job.attempts >= self._max_attempts:
                self._queue.remove(job)
                dropped += 1
            elif self._queue.reschedule(job, due_at):
                retried += 1
        return retried, dropped

    def _apply_authoritative(self, jobs: Sequence[HydrationJob], result: object) -> tuple[int, int, int]:
        by_id = _response_map(result)
        hydrated = completed = dropped = 0
        for job in jobs:
            message = by_id.get(job.message_id)
            if message is None:
                self._queue.remove(job)
                dropped += 1
                continue
            fact = extract_media_fact(getattr(message, "media", None))
            kind = None if fact is None else fact.kind
            payload = encode_media_payload(fact)
            applied = apply_hydrated_media_fact(self._conn, job.dialog_id, job.message_id, kind, payload)
            self._queue.remove(job)
            if not applied:
                dropped += 1
            else:
                completed += 1
            if fact is not None and applied:
                hydrated += 1
        return hydrated, completed, dropped

    async def _pause_between_requests(self) -> bool:
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._pause_between_requests_seconds)
            return True
        except TimeoutError:
            return False

    async def run(self) -> None:
        """Run the single daemon-owned worker until shutdown."""
        while not self._shutdown_event.is_set():
            await self.run_cycle()
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue


__all__ = [
    "MEDIA_METADATA_KIND",
    "MediaHydrationClient",
    "MediaHydrationCycleResult",
    "MediaHydrationWorker",
]
