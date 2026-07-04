"""Shared FloodWait helpers and low-volume FloodWait telemetry.

Telethon raises ``FloodWaitError`` (carrying a ``.seconds`` field) from any
request that trips Telegram's per-account rate limiter. Short floods
(``seconds <= flood_sleep_threshold``, Telethon's default 60) are absorbed
inside Telethon itself; these helpers cover only the long-flood path that our
own loops must handle — extracting the wait duration and sleeping through it
without losing shutdown responsiveness.

The *recovery policy* — commit partial progress, stamp a checkpoint, return a
neutral result, retry the same batch — is intentionally NOT captured here. It
differs per call site and stays explicit in each handler. Only the two
genuinely-duplicated mechanics live in this module.
"""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Final

DEFAULT_FLOOD_WAIT_SECONDS = 60
"""Fallback when an exception carries no usable ``.seconds``.

Defensive only — a real Telethon ``FloodWaitError`` always sets ``.seconds``.
Matches Telethon's own default ``flood_sleep_threshold`` so the long/short
boundary stays consistent.
"""

_SECONDS_PER_HOUR: Final[int] = 60 * 60
_SECONDS_PER_DAY: Final[int] = 24 * _SECONDS_PER_HOUR
_SECONDS_PER_WEEK: Final[int] = 7 * _SECONDS_PER_DAY
_MAX_RETAINED_EVENTS: Final[int] = 10_000
_ROLLUP_LOG_INTERVAL_S: Final[int] = _SECONDS_PER_DAY
_TELETHON_FLOOD_WAIT_RE: Final[re.Pattern[str]] = re.compile(
    r"Sleeping for (?P<seconds>\d+)s .* flood wait",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FloodWaitRollup:
    """Aggregated FloodWait counters for operational logs."""

    events_1h: int
    wait_s_1h: int
    events_24h: int
    wait_s_24h: int
    events_7d: int
    wait_s_7d: int

    @property
    def has_events(self) -> bool:
        return self.events_7d > 0


@dataclass(frozen=True, slots=True)
class _FloodWaitEvent:
    at_mono: float
    seconds: int
    source: str


@dataclass(slots=True)
class FloodWaitAccumulator:
    """In-memory FloodWait counters for the current daemon process."""

    max_events: int = _MAX_RETAINED_EVENTS
    log_interval_s: int = _ROLLUP_LOG_INTERVAL_S
    _events: deque[_FloodWaitEvent] = field(default_factory=deque)
    _last_log_mono: float = field(default_factory=time.monotonic)

    def observe(self, *, source: str, seconds: int, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        safe_seconds = max(0, int(seconds))
        self._events.append(_FloodWaitEvent(at_mono=now, seconds=safe_seconds, source=source))
        self._prune(now)

    def snapshot(self, *, now_mono: float | None = None) -> FloodWaitRollup:
        now = time.monotonic() if now_mono is None else now_mono
        self._prune(now)
        return FloodWaitRollup(
            events_1h=self._count_since(now - _SECONDS_PER_HOUR),
            wait_s_1h=self._sum_since(now - _SECONDS_PER_HOUR),
            events_24h=self._count_since(now - _SECONDS_PER_DAY),
            wait_s_24h=self._sum_since(now - _SECONDS_PER_DAY),
            events_7d=self._count_since(now - _SECONDS_PER_WEEK),
            wait_s_7d=self._sum_since(now - _SECONDS_PER_WEEK),
        )

    def maybe_log_rollup(self, logger: logging.Logger, *, now_mono: float | None = None) -> bool:
        now = time.monotonic() if now_mono is None else now_mono
        if now - self._last_log_mono < self.log_interval_s:
            return False

        self._last_log_mono = now
        rollup = self.snapshot(now_mono=now)
        if not rollup.has_events:
            return False

        logger.info(
            "flood_wait_rollup events_1h=%d wait_s_1h=%d events_24h=%d wait_s_24h=%d events_7d=%d wait_s_7d=%d",
            rollup.events_1h,
            rollup.wait_s_1h,
            rollup.events_24h,
            rollup.wait_s_24h,
            rollup.events_7d,
            rollup.wait_s_7d,
        )
        return True

    def _prune(self, now_mono: float) -> None:
        min_at = now_mono - _SECONDS_PER_WEEK
        while self._events and (self._events[0].at_mono < min_at or len(self._events) > self.max_events):
            self._events.popleft()

    def _count_since(self, min_at: float) -> int:
        return sum(1 for event in self._events if event.at_mono >= min_at)

    def _sum_since(self, min_at: float) -> int:
        return sum(event.seconds for event in self._events if event.at_mono >= min_at)


_FLOOD_WAIT_ACCUMULATOR: Final[FloodWaitAccumulator] = FloodWaitAccumulator()


def observe_flood_wait(*, source: str, seconds: int) -> None:
    """Record a FloodWait event in the process-local accumulator."""
    _FLOOD_WAIT_ACCUMULATOR.observe(source=source, seconds=seconds)


def maybe_log_flood_wait_rollup(logger: logging.Logger) -> bool:
    """Emit a daily FloodWait rollup if the process observed recent floods."""
    return _FLOOD_WAIT_ACCUMULATOR.maybe_log_rollup(logger)


class TelethonFloodWaitMetricsFilter(logging.Filter):
    """Observe Telethon's internal short FloodWait sleeps without hiding logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        match = _TELETHON_FLOOD_WAIT_RE.search(message)
        if match is not None:
            observe_flood_wait(
                source=f"{record.name}.auto_sleep",
                seconds=int(match.group("seconds")),
            )
        return True


def install_telethon_flood_wait_metrics_filter() -> None:
    """Install the Telethon short-FloodWait observer once per process."""
    logger = logging.getLogger("telethon.client.users")
    marker = "_mcp_telegram_flood_wait_metrics_installed"
    if getattr(logger, marker, False):
        return
    logger.addFilter(TelethonFloodWaitMetricsFilter())
    setattr(logger, marker, True)


def flood_seconds(
    exc: BaseException,
    *,
    default: int = DEFAULT_FLOOD_WAIT_SECONDS,
    source: str = "flood_wait_error",
) -> int:
    """Return a FloodWait's wait duration in whole seconds.

    Reads ``exc.seconds`` defensively: a missing, ``None``, or zero value
    falls back to ``default`` so callers never sleep for 0s or crash on a
    malformed exception.
    """
    seconds = getattr(exc, "seconds", None)
    wait_s = int(seconds or default)
    observe_flood_wait(source=source, seconds=wait_s)
    return wait_s


async def sleep_through_flood(shutdown_event: asyncio.Event, seconds: float) -> bool:
    """Sleep ``seconds``, waking early if ``shutdown_event`` is set.

    Returns ``True`` if shutdown was signalled during the wait — the caller
    should bail out of its current pass. Returns ``False`` if the full
    duration elapsed normally — the caller may retry or continue.
    """
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=float(seconds))
        return True
    except TimeoutError:
        return False
