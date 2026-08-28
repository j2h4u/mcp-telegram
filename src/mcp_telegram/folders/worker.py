"""Daemon-owned single-flight folder projection scheduler."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from ..flood import flood_seconds, is_flood_wait
from ..telegram_rpc import TelegramRpcCircuitOpenError
from .contracts import FolderSourceUnavailableError
from .ports import FolderSnapshotRepository
from .refresh import FolderRefresher, FolderRefreshResult

logger = logging.getLogger(__name__)


class FolderProjectionScheduling(Protocol):
    @property
    def refresh_interval_seconds(self) -> float: ...

    @property
    def jitter_ratio(self) -> float: ...

    @property
    def retry_delays_seconds(self) -> tuple[int, ...]: ...

    @property
    def retry_cap_seconds(self) -> int: ...

    @property
    def warning_failure_threshold(self) -> int: ...

    @property
    def stale_threshold_seconds(self) -> int: ...


class FolderAttemptResult(StrEnum):
    SUCCESS = "success"
    SOURCE_UNAVAILABLE = "source_unavailable"
    FLOOD_WAIT = "flood_wait"
    CIRCUIT_OPEN = "circuit_open"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class _Attempt:
    reason: str
    result: str
    duration_seconds: float
    folder_count: int
    dialog_count: int
    membership_count: int
    failure_count: int
    snapshot_age_seconds: int | None
    next_due_at: int | None


def _default_jitter(interval: float, ratio: float) -> float:
    return interval * (1.0 + random.uniform(-ratio, ratio))


class FolderProjectionWorker:
    """Run one folder source attempt at a time and schedule from completion."""

    def __init__(  # noqa: PLR0913 - explicit composition dependencies and deterministic test seams
        self,
        refresher: FolderRefresher,
        repository: FolderSnapshotRepository,
        shutdown_event: asyncio.Event,
        policy: FolderProjectionScheduling,
        *,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[float, float], float] = _default_jitter,
    ) -> None:
        self._refresher = refresher
        self._repository = repository
        self._shutdown_event = shutdown_event
        self._policy = policy
        self._clock = clock
        self._jitter = jitter
        self._failure_count = repository.read_consecutive_failures()
        self._last_outcome = repository.read_last_outcome()
        self._next_due_at = repository.read_next_retry_at()
        self._warning_bucket: int | None = None
        self._primed = False
        self._attempt_lock = asyncio.Lock()

    async def prime(self) -> None:
        """Perform the one startup attempt; the run loop must not duplicate it."""
        if self._primed:
            return
        self._primed = True
        self._warn_if_needed(self._last_outcome)
        if self._next_due_at is not None and self._next_due_at > self._clock():
            return
        await self._attempt("startup")

    async def run(self) -> None:
        """Run until daemon shutdown, with no detached or overlapping attempts."""
        if not self._primed:
            await self.prime()
        while not self._shutdown_event.is_set():
            due_at = self._next_due_at
            if due_at is None:
                return
            delay = max(0.0, float(due_at) - self._clock())
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=delay)
            except TimeoutError:
                pass
            if self._shutdown_event.is_set():
                return
            await self._attempt("scheduled")

    async def _attempt(self, reason: str) -> None:
        async with self._attempt_lock:
            await self._attempt_once(reason)

    async def _attempt_once(self, reason: str) -> None:
        started = self._clock()
        result, refresh_result, requested_flood_wait, unexpected, next_due_at = await self._perform_attempt()
        if result != FolderAttemptResult.SUCCESS:
            next_due_at = self._record_failure(result, requested_flood_wait)

        completion = self._clock()
        snapshot_at = self._repository.read_last_success_at()
        attempt = _Attempt(
            reason=reason,
            result=result,
            duration_seconds=max(0.0, completion - started),
            folder_count=0 if refresh_result is None else refresh_result.folder_count,
            dialog_count=0 if refresh_result is None else refresh_result.dialog_count,
            membership_count=0 if refresh_result is None else refresh_result.membership_count,
            failure_count=self._failure_count,
            snapshot_age_seconds=None if snapshot_at is None else max(0, int(completion - snapshot_at)),
            next_due_at=next_due_at,
        )
        self._log_attempt(attempt)
        if result != FolderAttemptResult.SUCCESS:
            self._warn_if_needed(result)
        if unexpected is not None:
            raise unexpected

    async def _perform_attempt(
        self,
    ) -> tuple[FolderAttemptResult, FolderRefreshResult | None, int | None, Exception | None, int | None]:
        refresh_result: FolderRefreshResult | None = None
        requested_flood_wait: int | None = None
        next_due_at: int | None = None
        try:
            projection = await self._refresher.acquire()
            completion = self._clock()
            refresh_result = self._refresher.persist(projection, completed_at=int(completion))
            self._failure_count = 0
            self._warning_bucket = None
            self._last_outcome = FolderAttemptResult.SUCCESS
            next_due_at = self._schedule_from_completion(completion)
            self._next_due_at = next_due_at
            return FolderAttemptResult.SUCCESS, refresh_result, None, None, next_due_at
        except FolderSourceUnavailableError, TimeoutError, OSError:
            return FolderAttemptResult.SOURCE_UNAVAILABLE, None, None, None, None
        except TelegramRpcCircuitOpenError:
            return FolderAttemptResult.CIRCUIT_OPEN, None, None, None, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - unexpected programming errors terminate the tracked worker
            if is_flood_wait(exc):
                requested_flood_wait = flood_seconds(exc)
                return FolderAttemptResult.FLOOD_WAIT, None, requested_flood_wait, None, None
            return FolderAttemptResult.UNEXPECTED, None, None, exc, None

    def _record_failure(self, result: FolderAttemptResult, requested_flood_wait: int | None) -> int | None:
        self._failure_count += 1
        if result == FolderAttemptResult.UNEXPECTED:
            next_due_at = None
        else:
            retry_delay = self._retry_delay(self._failure_count)
            if requested_flood_wait is not None:
                retry_delay = max(retry_delay, requested_flood_wait)
            next_due_at = math.ceil(self._clock() + retry_delay)
        self._next_due_at = next_due_at
        self._repository.record_attempt(
            attempted_at=int(self._clock()),
            outcome=result,
            next_retry_at=next_due_at,
            consecutive_failures=self._failure_count,
        )
        self._last_outcome = result
        return next_due_at

    def _log_attempt(self, attempt: _Attempt) -> None:
        logger.info(
            "folder_projection_complete reason=%s result=%s duration_s=%.3f folder_count=%d dialog_count=%d "
            "membership_count=%d failure_count=%d snapshot_age_seconds=%s next_due_at=%s",
            attempt.reason,
            attempt.result,
            attempt.duration_seconds,
            attempt.folder_count,
            attempt.dialog_count,
            attempt.membership_count,
            attempt.failure_count,
            attempt.snapshot_age_seconds,
            attempt.next_due_at,
        )

    def _retry_delay(self, failure_count: int) -> int:
        schedule = cast(tuple[int, ...], self._policy.retry_delays_seconds)
        normalized = max(1, failure_count)
        if normalized <= len(schedule):
            return min(self._policy.retry_cap_seconds, schedule[normalized - 1])
        multiplier = 1
        for _ in range(normalized - len(schedule)):
            multiplier *= 2
        final_delay = schedule[-1] * multiplier
        return min(self._policy.retry_cap_seconds, final_delay)

    def _schedule_from_completion(self, completion: float) -> int:
        delay = max(1.0, self._jitter(self._policy.refresh_interval_seconds, self._policy.jitter_ratio))
        return math.ceil(completion + delay)

    def _warn_if_needed(self, outcome: str | None) -> None:
        threshold = self._policy.warning_failure_threshold
        if threshold < 1 or self._failure_count < threshold:
            return
        bucket = self._failure_count // threshold
        if bucket == self._warning_bucket:
            return
        self._warning_bucket = bucket
        logger.warning(
            "folder_projection_warning consecutive_failures=%d threshold=%d result=%s",
            self._failure_count,
            threshold,
            outcome or "persisted",
        )
