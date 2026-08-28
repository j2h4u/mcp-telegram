"""Shared bounded runner for durable message-fact hydration jobs."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

from .access_lifecycle import set_access_lost
from .flood import flood_seconds
from .hydration_queue import HydrationJob, HydrationPriority, HydrationQueueRepository
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_rpc import TelegramRpcCircuitOpenError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FactHydrationCycleResult:
    """Sanitized counters for one bounded worker cycle."""

    requests: int = 0
    hydrated: int = 0
    completed: int = 0
    retried: int = 0
    dropped: int = 0
    stopped: bool = False


@dataclass(frozen=True, slots=True)
class AppliedFacts:
    hydrated: int = 0
    completed: int = 0
    retried: int = 0
    dropped: int = 0
    pending: bool = False


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
    retried: int = 0
    dropped: int = 0
    stopped: bool = False


def batch_jobs(
    jobs: Sequence[HydrationJob],
    handlers: dict[str, HydrationHandler],
) -> list[list[HydrationJob]]:
    """Batch compatible jobs without weakening queue priority order."""
    grouped: dict[tuple[str, HydrationPriority, int], list[tuple[int, HydrationJob]]] = defaultdict(list)
    for position, job in enumerate(jobs):
        grouped[(job.kind, job.priority, job.dialog_id)].append((position, job))

    positioned: list[tuple[HydrationPriority, int, list[HydrationJob]]] = []
    for (kind, priority, _dialog_id), positioned_jobs in grouped.items():
        handler = handlers.get(kind)
        if handler is None:
            continue
        for offset in range(0, len(positioned_jobs), handler.batch_size):
            chunk = positioned_jobs[offset : offset + handler.batch_size]
            positioned.append((priority, chunk[0][0], [job for _, job in chunk]))
    positioned.sort(key=lambda batch: (-int(batch[0]), batch[1]))
    return [batch for _priority, _position, batch in positioned]


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
        self._handlers = {handler.kind: handler for handler in handlers}
        self._interval_seconds = interval_seconds
        self._max_requests_per_cycle = max_requests_per_cycle
        self._max_jobs_per_cycle = max_jobs_per_cycle
        self._retry_delay_seconds = retry_delay_seconds
        self._circuit_retry_seconds = circuit_retry_seconds
        self._max_attempts = max_attempts
        self._pause_between_requests_seconds = pause_between_requests_seconds
        self._clock = clock
        self._queue = HydrationQueueRepository(conn)

    async def run_cycle(self, *, now: int | None = None) -> FactHydrationCycleResult:
        effective_now = int(self._clock()) if now is None else now
        due = self._queue.due_jobs(effective_now, self._max_jobs_per_cycle)
        due = [job for job in due if job.kind in self._handlers]
        if not due:
            return FactHydrationCycleResult()
        request_batches = batch_jobs(due, self._handlers)
        outcome = await self._run_batches(request_batches, int(effective_now))
        logger.info(
            "message_fact_hydration cycle jobs=%d requests=%d hydrated=%d completed=%d "
            "retried=%d dropped=%d stopped=%s",
            len(due),
            outcome.requests,
            outcome.hydrated,
            outcome.completed,
            outcome.retried,
            outcome.dropped,
            outcome.stopped,
        )
        return outcome

    async def _run_batches(
        self, request_batches: Sequence[Sequence[HydrationJob]], effective_now: int
    ) -> FactHydrationCycleResult:
        requests = hydrated = completed = retried = dropped = 0
        stopped = False
        used_requests = 0
        for batch_index, batch in enumerate(request_batches):
            handler = self._handlers[batch[0].kind]
            if used_requests + handler.request_cost > self._max_requests_per_cycle:
                break
            if self._shutdown_event.is_set():
                break
            used_requests += handler.request_cost
            outcome = await self._process_batch(handler, batch, effective_now)
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
        return FactHydrationCycleResult(requests, hydrated, completed, retried, dropped, stopped)

    async def _process_batch(  # noqa: PLR0911
        self,
        handler: HydrationHandler,
        batch: Sequence[HydrationJob],
        effective_now: int,
    ) -> _BatchOutcome:
        started, preflight_dropped = self._start_batch(handler, batch)
        if not started:
            return _BatchOutcome(dropped=preflight_dropped)
        try:
            result = await handler.request(self._client, started)
        except FloodWaitError as exc:
            retry_delay = flood_seconds(
                exc,
            )
            retried, dropped = self._retry_or_drop(started, effective_now + retry_delay)
            self._conn.commit()
            logger.warning(
                "message_fact_hydration flood_wait kind=%s dialog_id=%d jobs=%d retry_s=%d",
                handler.kind,
                started[0].dialog_id,
                len(started),
                retry_delay,
            )
            return _BatchOutcome(requests=handler.request_cost, retried=retried, dropped=dropped, stopped=True)
        except TelegramRpcCircuitOpenError:
            retried, dropped = self._retry_or_drop(started, effective_now + self._circuit_retry_seconds)
            self._conn.commit()
            logger.info(
                "message_fact_hydration circuit_open kind=%s dialog_id=%d jobs=%d retry_s=%d",
                handler.kind,
                started[0].dialog_id,
                len(started),
                self._circuit_retry_seconds,
            )
            return _BatchOutcome(requests=handler.request_cost, retried=retried, dropped=dropped, stopped=True)
        except ACCESS_LOST_ERRORS as exc:
            set_access_lost(self._conn, started[0].dialog_id, effective_now, reason=type(exc).__name__)
            self._conn.commit()
            logger.info(
                "message_fact_hydration access_lost kind=%s dialog_id=%d jobs=%d",
                handler.kind,
                started[0].dialog_id,
                len(started),
            )
            return _BatchOutcome(requests=handler.request_cost, dropped=len(started))
        except Exception as exc:  # noqa: BLE001 - Telegram transient classes vary by RPC layer
            if handler.is_terminal_error(exc):
                for job in started:
                    self._queue.remove(job)
                self._conn.commit()
                logger.info(
                    "message_fact_hydration terminal kind=%s dialog_id=%d jobs=%d error_type=%s",
                    handler.kind,
                    started[0].dialog_id,
                    len(started),
                    type(exc).__name__,
                )
                return _BatchOutcome(requests=handler.request_cost, dropped=len(started))
            retried, dropped = self._retry_or_drop(started, effective_now + self._retry_delay_seconds)
            self._conn.commit()
            logger.warning(
                "message_fact_hydration transient kind=%s dialog_id=%d jobs=%d error_type=%s",
                handler.kind,
                started[0].dialog_id,
                len(started),
                type(exc).__name__,
            )
            return _BatchOutcome(requests=handler.request_cost, retried=retried, dropped=dropped)

        applied = handler.apply(self._conn, self._queue, started, result, now=effective_now)
        if applied.pending:
            retried, dropped = self._retry_or_drop(
                started,
                effective_now + handler.pending_delay_seconds,
            )
            applied = AppliedFacts(
                hydrated=applied.hydrated,
                completed=applied.completed,
                retried=retried,
                dropped=dropped,
            )
        self._conn.commit()
        return _BatchOutcome(
            requests=handler.request_cost,
            hydrated=applied.hydrated,
            completed=applied.completed,
            retried=applied.retried,
            dropped=preflight_dropped + applied.dropped,
        )

    def _start_batch(self, handler: HydrationHandler, jobs: Sequence[HydrationJob]) -> tuple[list[HydrationJob], int]:
        started: list[HydrationJob] = []
        dropped = 0
        for job in jobs:
            if not handler.eligible(self._conn, job):
                self._queue.remove(job)
                dropped += 1
                continue
            current = self._queue.start(job)
            if current is None:
                continue
            if current.attempts > self._max_attempts:
                self._queue.remove(current)
                dropped += 1
                continue
            started.append(current)
        self._conn.commit()
        return started, dropped

    def _retry_or_drop(self, jobs: Sequence[HydrationJob], due_at: int) -> tuple[int, int]:
        retried = dropped = 0
        for job in jobs:
            if job.attempts >= self._max_attempts:
                self._queue.remove(job)
                dropped += 1
            elif self._queue.reschedule(job, due_at):
                retried += 1
        return retried, dropped

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
    "HydrationHandler",
    "MessageFactHydrationWorker",
    "batch_jobs",
]
