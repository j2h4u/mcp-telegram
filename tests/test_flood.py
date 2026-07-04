"""Unit tests for the shared FloodWait helpers (mcp_telegram.flood)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from mcp_telegram.flood import (
    DEFAULT_FLOOD_WAIT_SECONDS,
    FloodWaitAccumulator,
    TelethonFloodWaitMetricsFilter,
    flood_seconds,
    sleep_through_flood,
)


class _FloodWaitError(Exception):
    def __init__(self, seconds: object | None = None) -> None:
        super().__init__()
        self.seconds = seconds


# ---------------------------------------------------------------------------
# flood_seconds
# ---------------------------------------------------------------------------


def test_flood_seconds_reads_seconds_attribute() -> None:
    exc = _FloodWaitError(27)
    assert flood_seconds(exc) == 27


def test_flood_seconds_coerces_to_int() -> None:
    exc = _FloodWaitError(12.9)
    assert flood_seconds(exc) == 12


def test_flood_seconds_missing_attribute_uses_default() -> None:
    exc = _FloodWaitError()  # no `seconds`
    assert flood_seconds(exc) == DEFAULT_FLOOD_WAIT_SECONDS


def test_flood_seconds_none_uses_default() -> None:
    exc = _FloodWaitError(None)
    assert flood_seconds(exc) == DEFAULT_FLOOD_WAIT_SECONDS


def test_flood_seconds_zero_uses_default() -> None:
    # 0s would be a no-op sleep — fall back so callers never busy-spin.
    exc = _FloodWaitError(0)
    assert flood_seconds(exc) == DEFAULT_FLOOD_WAIT_SECONDS


def test_flood_seconds_custom_default() -> None:
    exc = _FloodWaitError(0)
    assert flood_seconds(exc, default=5) == 5


def test_flood_wait_accumulator_rollup_windows() -> None:
    accumulator = FloodWaitAccumulator(log_interval_s=10)
    accumulator.observe(source="test", seconds=5, now_mono=100.0)
    accumulator.observe(source="test", seconds=7, now_mono=3_800.0)
    accumulator.observe(source="test", seconds=11, now_mono=90_000.0)

    rollup = accumulator.snapshot(now_mono=90_000.0)

    assert rollup.events_1h == 1
    assert rollup.wait_s_1h == 11
    assert rollup.events_24h == 2
    assert rollup.wait_s_24h == 18
    assert rollup.events_7d == 3
    assert rollup.wait_s_7d == 23


def test_flood_wait_accumulator_daily_rollup_logs_only_when_due(caplog: pytest.LogCaptureFixture) -> None:
    accumulator = FloodWaitAccumulator(log_interval_s=10)
    accumulator._last_log_mono = 0.0
    logger = logging.getLogger("tests.flood")
    accumulator.observe(source="test", seconds=9, now_mono=1.0)

    assert accumulator.maybe_log_rollup(logger, now_mono=5.0) is False
    assert "flood_wait_rollup" not in caplog.text

    with caplog.at_level(logging.INFO, logger="tests.flood"):
        assert accumulator.maybe_log_rollup(logger, now_mono=12.0) is True

    assert "flood_wait_rollup" in caplog.text
    assert "events_1h=1" in caplog.text
    assert "wait_s_1h=9" in caplog.text


def test_telethon_flood_wait_filter_observes_auto_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    accumulator = FloodWaitAccumulator()

    def _observe(*, source: str, seconds: int) -> None:
        accumulator.observe(source=source, seconds=seconds, now_mono=100.0)

    flood_filter = TelethonFloodWaitMetricsFilter()
    record = logging.LogRecord(
        name="telethon.client.users",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Sleeping for 23s (0:00:23) on GetHistoryRequest flood wait",
        args=(),
        exc_info=None,
    )

    from mcp_telegram import flood as flood_module

    monkeypatch.setattr(flood_module, "observe_flood_wait", _observe)
    assert flood_filter.filter(record) is True

    rollup = accumulator.snapshot(now_mono=100.0)
    assert rollup.events_1h == 1
    assert rollup.wait_s_1h == 23


# ---------------------------------------------------------------------------
# sleep_through_flood
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_through_flood_returns_true_on_shutdown() -> None:
    event = asyncio.Event()
    event.set()  # already signalled — wait() resolves immediately
    assert await sleep_through_flood(event, 60) is True


@pytest.mark.asyncio
async def test_sleep_through_flood_returns_false_on_timeout() -> None:
    event = asyncio.Event()  # never set
    # Tiny timeout so the full duration elapses fast.
    assert await sleep_through_flood(event, 0.01) is False


@pytest.mark.asyncio
async def test_sleep_through_flood_wakes_early_when_event_set_mid_wait() -> None:
    event = asyncio.Event()

    async def signal_soon() -> None:
        await asyncio.sleep(0.01)
        event.set()

    signal_task = asyncio.create_task(signal_soon())
    # 5s nominal wait, but the event fires at ~10ms — must return True well
    # before the timeout, proving the wait is interruptible.
    result = await asyncio.wait_for(sleep_through_flood(event, 5), timeout=1.0)
    assert result is True
    await signal_task  # keep a reference and let the helper task finish cleanly
