"""Unit tests for the shared FloodWait helpers (mcp_telegram.flood)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from mcp_telegram.flood import (
    DEFAULT_FLOOD_WAIT_SECONDS,
    FloodWaitAccumulator,
    FloodWaitKillSwitchPolicy,
    TelethonFloodWaitMetricsFilter,
    flood_seconds,
    install_telethon_flood_wait_metrics_filter,
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


def test_flood_wait_kill_switch_opens_on_event_threshold() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(
            enabled=True,
            window_seconds=60,
            max_events=3,
            max_wait_seconds=999,
            minimum_cooldown_seconds=300,
        )
    )

    accumulator.observe(source="test.one", seconds=1, now_mono=100.0)
    accumulator.observe(source="test.two", seconds=1, now_mono=110.0)
    assert accumulator.kill_switch_status(now_mono=110.0).open is False

    accumulator.observe(source="test.three", seconds=1, now_mono=120.0)
    status = accumulator.kill_switch_status(now_mono=120.0)

    assert status.open is True
    assert status.reason == "too_many_flood_wait_events"
    assert status.events_in_window == 3
    assert status.wait_s_in_window == 3
    assert status.minimum_cooldown_seconds == 300
    assert "too_many_flood_wait_events" in status.detail()


def test_flood_wait_kill_switch_opens_on_wait_threshold() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(
            enabled=True,
            window_seconds=60,
            max_events=10,
            max_wait_seconds=20,
            minimum_cooldown_seconds=300,
        )
    )

    accumulator.observe(source="test.large", seconds=21, now_mono=100.0)
    status = accumulator.kill_switch_status(now_mono=100.0)

    assert status.open is True
    assert status.reason == "too_much_flood_wait_time"
    assert status.events_in_window == 1
    assert status.wait_s_in_window == 21


def test_flood_wait_kill_switch_sets_operator_event() -> None:
    event = asyncio.Event()
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(
            enabled=True,
            window_seconds=60,
            max_events=1,
            max_wait_seconds=999,
            minimum_cooldown_seconds=300,
        ),
        event=event,
    )

    accumulator.observe(source="test", seconds=1, now_mono=100.0)

    assert event.is_set()


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


def test_telethon_flood_wait_filter_ignores_unrelated_log_records(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, int]] = []
    record = logging.LogRecord(
        name="telethon.client.users",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Connected to Telegram data center",
        args=(),
        exc_info=None,
    )

    from mcp_telegram import flood as flood_module

    monkeypatch.setattr(
        flood_module,
        "observe_flood_wait",
        lambda *, source, seconds: observed.append((source, seconds)),
    )

    assert TelethonFloodWaitMetricsFilter().filter(record) is True
    assert observed == []


def test_install_telethon_flood_wait_metrics_filter_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("telethon.client.users")
    marker = "_mcp_telegram_flood_wait_metrics_installed"
    original_filters = tuple(logger.filters)
    monkeypatch.delattr(logger, marker, raising=False)

    try:
        install_telethon_flood_wait_metrics_filter()
        added_filters = [flood_filter for flood_filter in logger.filters if flood_filter not in original_filters]

        install_telethon_flood_wait_metrics_filter()

        assert len(added_filters) == 1
        assert isinstance(added_filters[0], TelethonFloodWaitMetricsFilter)
        assert [
            flood_filter for flood_filter in logger.filters if flood_filter not in original_filters
        ] == added_filters
    finally:
        for flood_filter in logger.filters:
            if flood_filter not in original_filters:
                logger.removeFilter(flood_filter)


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
