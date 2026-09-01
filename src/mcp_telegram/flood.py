"""Shared FloodWait extraction, telemetry, and durable worker sleep helpers.

The TelegramRpcGate is the sole production observer of FloodWait exceptions.
This module keeps the accumulator API used by health/telemetry and provides
pure extraction for worker recovery paths.

The *recovery policy* — commit partial progress, stamp a checkpoint, return a
neutral result, retry the same batch — is intentionally NOT captured here. It
differs per call site and stays explicit in each handler. Only the two
genuinely-duplicated mechanics live in this module.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Final

DEFAULT_FLOOD_WAIT_SECONDS = 60
"""Fallback when an exception carries no usable ``.seconds``.

Defensive only — a real Telethon FloodWait exception always sets ``.seconds``.
"""

_SECONDS_PER_HOUR: Final[int] = 60 * 60
_SECONDS_PER_DAY: Final[int] = 24 * _SECONDS_PER_HOUR
_SECONDS_PER_WEEK: Final[int] = 7 * _SECONDS_PER_DAY
_MAX_RETAINED_EVENTS: Final[int] = 10_000
_ROLLUP_LOG_INTERVAL_S: Final[int] = _SECONDS_PER_DAY
logger = logging.getLogger(__name__)


class TelegramRpcThrottled(RuntimeError):  # noqa: N818 - public domain outcome name is intentional
    """An application-owned account-wide Telegram throttling outcome.

    ``retry_after_seconds`` is a positive duration when the caller may retry
    after waiting.  A latched outcome has no finite retry duration: the
    account circuit is open and work must remain stopped until the process is
    restarted or the circuit is otherwise reset by its owner.
    """

    retry_after_seconds: int | None
    latched: bool

    def __init__(
        self,
        retry_after_seconds: int | None = None,
        *,
        latched: bool = False,
        detail: str | None = None,
    ) -> None:
        if retry_after_seconds is not None and retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be >= 1 when finite")
        if latched and retry_after_seconds is not None:
            raise ValueError("latched throttling cannot have a finite retry duration")
        self.retry_after_seconds = retry_after_seconds
        self.latched = latched
        super().__init__(detail or ("Telegram RPC circuit is latched" if latched else "Telegram RPC throttled"))


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
class FloodWaitKillSwitchPolicy:
    """Account-level FloodWait storm policy.

    The switch is latch-open: once tripped, the daemon must stop Telegram-facing
    work and stay unhealthy until an operator restarts it after investigation.
    """

    enabled: bool
    window_seconds: int
    max_events: int
    max_wait_seconds: int


@dataclass(frozen=True, slots=True)
class FloodWaitKillSwitchStatus:
    """Current account-level FloodWait kill-switch state."""

    open: bool
    reason: str | None
    opened_at: int | None
    events_in_window: int
    wait_s_in_window: int
    window_seconds: int
    source: str | None

    def detail(self) -> str:
        if not self.open:
            return "flood wait kill switch is closed"
        return (
            f"{self.reason or 'FloodWait storm'}; opened_at={self.opened_at}; events={self.events_in_window}; "
            f"wait_s={self.wait_s_in_window}; window_s={self.window_seconds}; source={self.source or 'unknown'}"
        )


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
    _kill_switch_policy: FloodWaitKillSwitchPolicy | None = None
    _kill_switch_event: asyncio.Event | None = None
    _kill_switch_status: FloodWaitKillSwitchStatus = field(
        default_factory=lambda: FloodWaitKillSwitchStatus(False, None, None, 0, 0, 0, None)
    )

    def observe(self, *, source: str, seconds: int, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        safe_seconds = max(0, int(seconds))
        self._events.append(_FloodWaitEvent(at_mono=now, seconds=safe_seconds, source=source))
        self._prune(now)
        self._evaluate_kill_switch(now, source)

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

    def configure_kill_switch(
        self,
        policy: FloodWaitKillSwitchPolicy,
        *,
        event: asyncio.Event | None = None,
    ) -> None:
        """Install a kill-switch policy for future FloodWait observations."""
        self._kill_switch_policy = policy
        self._kill_switch_event = event
        if not policy.enabled:
            self._kill_switch_status = FloodWaitKillSwitchStatus(
                False,
                None,
                None,
                0,
                0,
                policy.window_seconds,
                None,
            )

    def kill_switch_status(self, *, now_mono: float | None = None) -> FloodWaitKillSwitchStatus:
        now = time.monotonic() if now_mono is None else now_mono
        self._prune(now)
        status = self._kill_switch_status
        if status.open:
            return status
        policy = self._kill_switch_policy
        if policy is None:
            return FloodWaitKillSwitchStatus(False, None, None, 0, 0, 0, None)
        return FloodWaitKillSwitchStatus(
            False,
            None,
            None,
            self._count_since(now - policy.window_seconds),
            self._sum_since(now - policy.window_seconds),
            policy.window_seconds,
            None,
        )

    def _prune(self, now_mono: float) -> None:
        min_at = now_mono - _SECONDS_PER_WEEK
        while self._events and (self._events[0].at_mono < min_at or len(self._events) > self.max_events):
            self._events.popleft()

    def _count_since(self, min_at: float) -> int:
        return sum(1 for event in self._events if event.at_mono >= min_at)

    def _sum_since(self, min_at: float) -> int:
        return sum(event.seconds for event in self._events if event.at_mono >= min_at)

    def _evaluate_kill_switch(self, now_mono: float, source: str) -> None:
        policy = self._kill_switch_policy
        if policy is None or not policy.enabled or self._kill_switch_status.open:
            return

        min_at = now_mono - policy.window_seconds
        events = self._count_since(min_at)
        wait_s = self._sum_since(min_at)
        reason: str | None = None
        if events >= policy.max_events:
            reason = "too_many_flood_wait_events"
        elif wait_s >= policy.max_wait_seconds:
            reason = "too_much_flood_wait_time"
        if reason is None:
            return

        self._kill_switch_status = FloodWaitKillSwitchStatus(
            open=True,
            reason=reason,
            opened_at=int(time.time()),
            events_in_window=events,
            wait_s_in_window=wait_s,
            window_seconds=policy.window_seconds,
            source=source,
        )
        logger.critical("flood_wait_kill_switch_open %s", self._kill_switch_status.detail())
        if self._kill_switch_event is not None:
            self._kill_switch_event.set()


_FLOOD_WAIT_ACCUMULATOR: Final[FloodWaitAccumulator] = FloodWaitAccumulator()


def observe_flood_wait(*, source: str, seconds: int) -> None:
    """Record a FloodWait event in the process-local accumulator."""
    _FLOOD_WAIT_ACCUMULATOR.observe(source=source, seconds=seconds)


def configure_flood_wait_kill_switch(policy: FloodWaitKillSwitchPolicy, *, event: asyncio.Event | None = None) -> None:
    """Configure the process-local account-level FloodWait kill switch."""
    _FLOOD_WAIT_ACCUMULATOR.configure_kill_switch(policy, event=event)


def flood_wait_kill_switch_status() -> FloodWaitKillSwitchStatus:
    """Return the process-local account-level FloodWait kill-switch status."""
    return _FLOOD_WAIT_ACCUMULATOR.kill_switch_status()


def maybe_log_flood_wait_rollup(logger: logging.Logger) -> bool:
    """Emit a daily FloodWait rollup if the process observed recent floods."""
    return _FLOOD_WAIT_ACCUMULATOR.maybe_log_rollup(logger)


def flood_seconds(exc: BaseException, *, default: int = DEFAULT_FLOOD_WAIT_SECONDS) -> int:
    """Return a FloodWait's wait duration in whole seconds.

    Reads ``exc.seconds`` defensively: a missing, ``None``, or zero value
    falls back to ``default`` so callers never sleep for 0s or crash on a
    malformed exception. This function has no telemetry side effects.
    """
    seconds = getattr(exc, "seconds", None)
    return max(1, int(seconds or default))


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
