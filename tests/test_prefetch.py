"""Unit tests for PrefetchCoordinator and background task coroutines.

Tests verify:
- Dedup set lifecycle (PRE-05): schedule returns True first call, False for duplicate
- Key release after success and failure (finally block)
- Exception logged as warning, never propagated
- Success logged at debug level
- _prefetch_task iter_messages params for NEWEST/OLDEST directions (PRE-04)
- _prefetch_task with topic_id scoping
- _prefetch_task empty result skips store_messages
- _delta_refresh_task min_id semantics (REF-02)
- _delta_refresh_task with topic_id
- _delta_refresh_task calls store_messages on results
- _next_prefetch_anchor direction semantics
- No asyncio.sleep or Timer in source (REF-03)
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from mcp_telegram.prefetch import (
    PrefetchCoordinator,
    _delta_refresh_task,
    _next_prefetch_anchor,
    _prefetch_task,
)
from mcp_telegram.pagination import HistoryDirection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _successful_coro() -> None:
    """Coroutine that completes normally."""
    pass


async def _failing_coro() -> None:
    """Coroutine that raises a generic exception."""
    raise RuntimeError("simulated prefetch failure")


async def _rpc_error_coro() -> None:
    """Coroutine that raises an RPCError-like exception."""
    from telethon.errors import RPCError
    raise RPCError(request=None, message="FLOOD_WAIT_30", code=420)


async def _async_iter(items):
    """Async generator that yields items from a list."""
    for item in items:
        yield item


def _make_msg(id: int) -> MagicMock:
    msg = MagicMock()
    msg.id = id
    return msg


# ---------------------------------------------------------------------------
# PrefetchCoordinator — dedup and lifecycle
# ---------------------------------------------------------------------------

KEY_A: tuple = (12345, "newest", 100, None)
KEY_B: tuple = (12345, "oldest", None, None)


async def test_schedule_returns_true_first_call():
    """schedule() returns True when key is new."""
    coordinator = PrefetchCoordinator()
    result = coordinator.schedule(_successful_coro(), key=KEY_A)
    assert result is True
    # Clean up pending tasks
    await asyncio.sleep(0)


async def test_schedule_returns_false_duplicate_key():
    """schedule() returns False when key is already in-flight."""
    coordinator = PrefetchCoordinator()
    coordinator.schedule(_successful_coro(), key=KEY_A)
    result = coordinator.schedule(_successful_coro(), key=KEY_A)
    assert result is False
    await asyncio.sleep(0)


async def test_key_released_after_success():
    """After successful task completes, key is removed from _in_flight."""
    coordinator = PrefetchCoordinator()
    coordinator.schedule(_successful_coro(), key=KEY_A)
    # Drain event loop so _run completes
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Key should be gone — re-scheduling returns True
    result = coordinator.schedule(_successful_coro(), key=KEY_A)
    assert result is True
    await asyncio.sleep(0)


async def test_key_released_after_failure():
    """After failing task, key is removed from _in_flight (finally runs)."""
    coordinator = PrefetchCoordinator()
    coordinator.schedule(_failing_coro(), key=KEY_A)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    result = coordinator.schedule(_successful_coro(), key=KEY_A)
    assert result is True
    await asyncio.sleep(0)


async def test_exception_logged_not_propagated():
    """RPCError raised in coro logs warning; nothing escapes to caller."""
    coordinator = PrefetchCoordinator()
    with patch("mcp_telegram.prefetch.logger") as mock_logger:
        coordinator.schedule(_rpc_error_coro(), key=KEY_A)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        mock_logger.warning.assert_called_once()
        # exc_info=True must be passed
        _, kwargs = mock_logger.warning.call_args
        assert kwargs.get("exc_info") is True


async def test_success_logged_debug():
    """Successful coro logs at debug level."""
    coordinator = PrefetchCoordinator()
    with patch("mcp_telegram.prefetch.logger") as mock_logger:
        coordinator.schedule(_successful_coro(), key=KEY_A)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        mock_logger.debug.assert_called_once()


async def test_dedup_suppresses_duplicate_schedule():
    """Two synchronous schedule() calls with same key create only one asyncio task (PRE-05)."""
    coordinator = PrefetchCoordinator()
    with patch("asyncio.create_task", wraps=asyncio.create_task) as mock_create:
        coordinator.schedule(_successful_coro(), key=KEY_A)
        coordinator.schedule(_successful_coro(), key=KEY_A)
        assert mock_create.call_count == 1
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# _prefetch_task — iter_messages semantics
# ---------------------------------------------------------------------------

async def test_prefetch_task_stores_messages():
    """_prefetch_task calls client.iter_messages then msg_cache.store_messages (PRE-04)."""
    client = MagicMock()
    msgs = [_make_msg(10), _make_msg(9), _make_msg(8)]
    client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    msg_cache = MagicMock()

    await _prefetch_task(client, msg_cache, 99, HistoryDirection.NEWEST, 11, 3, None)

    client.iter_messages.assert_called_once()
    msg_cache.store_messages.assert_called_once_with(99, msgs)


async def test_prefetch_task_newest_direction():
    """NEWEST direction uses max_id=anchor_id, reverse not set (default False)."""
    client = MagicMock()
    msgs = [_make_msg(5)]
    client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    msg_cache = MagicMock()

    await _prefetch_task(client, msg_cache, 99, HistoryDirection.NEWEST, 20, 5, None)

    kwargs = client.iter_messages.call_args[1]
    assert kwargs["max_id"] == 20
    assert "min_id" not in kwargs
    assert kwargs.get("reverse") is not True  # reverse should be absent or False


async def test_prefetch_task_oldest_direction():
    """OLDEST direction uses min_id=anchor_id, reverse=True."""
    client = MagicMock()
    msgs = [_make_msg(3)]
    client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    msg_cache = MagicMock()

    await _prefetch_task(client, msg_cache, 99, HistoryDirection.OLDEST, 5, 5, None)

    kwargs = client.iter_messages.call_args[1]
    assert kwargs["min_id"] == 5
    assert "max_id" not in kwargs
    assert kwargs["reverse"] is True


async def test_prefetch_task_oldest_no_anchor():
    """OLDEST with anchor_id=None uses min_id=1 (fetch from very beginning)."""
    client = MagicMock()
    msgs = [_make_msg(1)]
    client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    msg_cache = MagicMock()

    await _prefetch_task(client, msg_cache, 99, HistoryDirection.OLDEST, None, 5, None)

    kwargs = client.iter_messages.call_args[1]
    assert kwargs["min_id"] == 1
    assert kwargs["reverse"] is True


async def test_prefetch_task_with_topic():
    """Non-general topic adds reply_to=topic_id to iter_kwargs."""
    client = MagicMock()
    msgs = [_make_msg(7)]
    client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    msg_cache = MagicMock()

    await _prefetch_task(client, msg_cache, 99, HistoryDirection.NEWEST, 10, 5, topic_id=42)

    kwargs = client.iter_messages.call_args[1]
    assert kwargs["reply_to"] == 42


async def test_prefetch_task_general_topic_no_reply_to():
    """General topic (topic_id=1) does NOT add reply_to."""
    client = MagicMock()
    msgs = [_make_msg(7)]
    client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    msg_cache = MagicMock()

    await _prefetch_task(client, msg_cache, 99, HistoryDirection.NEWEST, 10, 5, topic_id=1)

    kwargs = client.iter_messages.call_args[1]
    assert "reply_to" not in kwargs


async def test_prefetch_task_empty_result_no_store():
    """Empty iter_messages result skips store_messages call."""
    client = MagicMock()
    client.iter_messages = MagicMock(return_value=_async_iter([]))
    msg_cache = MagicMock()

    await _prefetch_task(client, msg_cache, 99, HistoryDirection.NEWEST, 10, 5, None)

    msg_cache.store_messages.assert_not_called()


# ---------------------------------------------------------------------------
# _delta_refresh_task — min_id semantics
# ---------------------------------------------------------------------------

async def test_delta_refresh_uses_min_id():
    """_delta_refresh_task passes min_id=last_id, reverse=True (REF-02)."""
    client = MagicMock()
    msgs = [_make_msg(101), _make_msg(102)]
    client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    msg_cache = MagicMock()

    await _delta_refresh_task(client, msg_cache, 99, last_id=100, limit=10, topic_id=None)

    kwargs = client.iter_messages.call_args[1]
    assert kwargs["min_id"] == 100
    assert kwargs["reverse"] is True
    assert "max_id" not in kwargs


async def test_delta_refresh_with_topic():
    """Non-general topic_id adds reply_to to iter_kwargs."""
    client = MagicMock()
    msgs = [_make_msg(50)]
    client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    msg_cache = MagicMock()

    await _delta_refresh_task(client, msg_cache, 99, last_id=45, limit=5, topic_id=7)

    kwargs = client.iter_messages.call_args[1]
    assert kwargs["reply_to"] == 7


async def test_delta_refresh_stores_messages():
    """_delta_refresh_task calls msg_cache.store_messages with fetched results."""
    client = MagicMock()
    msgs = [_make_msg(101), _make_msg(102)]
    client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    msg_cache = MagicMock()

    await _delta_refresh_task(client, msg_cache, 99, last_id=100, limit=10, topic_id=None)

    msg_cache.store_messages.assert_called_once_with(99, msgs)


async def test_delta_refresh_empty_no_store():
    """Empty result skips store_messages."""
    client = MagicMock()
    client.iter_messages = MagicMock(return_value=_async_iter([]))
    msg_cache = MagicMock()

    await _delta_refresh_task(client, msg_cache, 99, last_id=100, limit=10, topic_id=None)

    msg_cache.store_messages.assert_not_called()


# ---------------------------------------------------------------------------
# _next_prefetch_anchor
# ---------------------------------------------------------------------------

def test_next_prefetch_anchor_newest():
    """NEWEST direction returns min(ids) — next page older than current."""
    msgs = [_make_msg(10), _make_msg(7), _make_msg(5)]
    result = _next_prefetch_anchor(msgs, HistoryDirection.NEWEST)
    assert result == 5


def test_next_prefetch_anchor_oldest():
    """OLDEST direction returns max(ids) — next page newer than current."""
    msgs = [_make_msg(1), _make_msg(3), _make_msg(8)]
    result = _next_prefetch_anchor(msgs, HistoryDirection.OLDEST)
    assert result == 8


def test_next_prefetch_anchor_empty():
    """Empty list returns None."""
    result = _next_prefetch_anchor([], HistoryDirection.NEWEST)
    assert result is None


def test_next_prefetch_anchor_single_newest():
    """Single-element list, NEWEST returns that id."""
    msgs = [_make_msg(42)]
    result = _next_prefetch_anchor(msgs, HistoryDirection.NEWEST)
    assert result == 42


def test_next_prefetch_anchor_single_oldest():
    """Single-element list, OLDEST returns that id."""
    msgs = [_make_msg(42)]
    result = _next_prefetch_anchor(msgs, HistoryDirection.OLDEST)
    assert result == 42


# ---------------------------------------------------------------------------
# REF-03 — no periodic timer / sleep in source
# ---------------------------------------------------------------------------

def test_no_background_timer_refresh():
    """prefetch.py must not contain asyncio.sleep or Timer (REF-03)."""
    import mcp_telegram.prefetch as _mod
    source = inspect.getsource(_mod)
    assert "asyncio.sleep" not in source, "asyncio.sleep found in prefetch.py — violates REF-03"
    assert "Timer" not in source, "Timer found in prefetch.py — violates REF-03"
