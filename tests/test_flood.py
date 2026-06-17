"""Unit tests for the shared FloodWait helpers (mcp_telegram.flood)."""

from __future__ import annotations

import asyncio

import pytest

from mcp_telegram.flood import (
    DEFAULT_FLOOD_WAIT_SECONDS,
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
