"""Tests for shared helpers in tools/_base.py and daemon.py."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_telegram.tools._base import ToolResult, _check_daemon_response

# ---------------------------------------------------------------------------
# _check_daemon_response (M-13)
# ---------------------------------------------------------------------------


def test_check_daemon_response_ok_returns_none():
    assert _check_daemon_response({"ok": True, "data": {}}) is None


def test_check_daemon_response_error_returns_tool_result():
    result = _check_daemon_response({"ok": False, "message": "something broke"})
    assert isinstance(result, ToolResult)
    assert "something broke" in result.content[0].text


def test_check_daemon_response_missing_message_uses_default():
    result = _check_daemon_response({"ok": False})
    assert isinstance(result, ToolResult)
    assert "Request failed" in result.content[0].text


def test_check_daemon_response_passes_extra_kwargs():
    result = _check_daemon_response(
        {"ok": False, "message": "err"},
        has_filter=True,
        has_cursor=True,
    )
    assert result.has_filter is True
    assert result.has_cursor is True


# ---------------------------------------------------------------------------
# _maybe_heartbeat_and_gap_scan (M-11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_heartbeat_fires_when_interval_elapsed():
    """Heartbeat fires when enough time has passed."""
    from mcp_telegram.daemon import HEARTBEAT_INTERVAL_S, _maybe_heartbeat_and_gap_scan

    conn = MagicMock(spec=sqlite3.Connection)
    # Make the stats query return something
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("synced", 1)]
    mock_fetchone = MagicMock()
    mock_fetchone.fetchone.return_value = (10,)
    conn.execute.side_effect = [mock_cursor, mock_fetchone]

    client = MagicMock()
    client.is_connected.return_value = True

    handler_manager = MagicMock()
    handler_manager.run_dm_gap_scan = AsyncMock(return_value=0)

    import time

    sync_start = time.monotonic()
    # Set last_heartbeat far in the past to trigger
    old_heartbeat = sync_start - HEARTBEAT_INTERVAL_S - 1
    old_gap_scan = sync_start  # gap scan should NOT fire

    new_hb, new_gs, _hb_count, _hb_mono = await _maybe_heartbeat_and_gap_scan(
        conn,
        client,
        handler_manager,
        sync_start,
        old_heartbeat,
        old_gap_scan,
        0,
        old_heartbeat,
    )

    assert new_hb > old_heartbeat, "heartbeat timestamp should be updated"
    handler_manager.refresh_synced_dialogs.assert_called_once()


@pytest.mark.asyncio
async def test_maybe_heartbeat_skips_when_recent():
    """Heartbeat does NOT fire when interval hasn't elapsed."""
    from mcp_telegram.daemon import _maybe_heartbeat_and_gap_scan

    conn = MagicMock(spec=sqlite3.Connection)
    client = MagicMock()
    handler_manager = MagicMock()

    import time

    now = time.monotonic()

    new_hb, new_gs, _hb_count, _hb_mono = await _maybe_heartbeat_and_gap_scan(
        conn,
        client,
        handler_manager,
        now,
        now,
        now,
        0,
        now,
    )

    assert new_hb == now, "heartbeat timestamp should not change"
    handler_manager.refresh_synced_dialogs.assert_not_called()
