"""Tests for shared helpers in tools/_base.py and daemon.py."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Protocol, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_telegram.tools import _base
from mcp_telegram.tools._base import (
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _send_telemetry_event,
    _telemetry_done_callback,
    _track_tool_telemetry,
)


class _HeartbeatHandlerManager(Protocol):
    refresh_synced_dialogs: AsyncMock
    run_dm_gap_scan: AsyncMock


class _TelemetryConnection(Protocol):
    record_telemetry: AsyncMock


class _TextContent(Protocol):
    text: str


class _TelemetryContext(Protocol):
    __aenter__: AsyncMock
    __aexit__: AsyncMock


# ---------------------------------------------------------------------------
# _check_daemon_response (M-13)
# ---------------------------------------------------------------------------


def test_check_daemon_response_ok_returns_none():
    assert _check_daemon_response({"ok": True, "data": {}}) is None


def test_check_daemon_response_error_returns_tool_result():
    result = _check_daemon_response({"ok": False, "error": "bad_request", "message": "something broke"})
    assert isinstance(result, ToolResult)
    content = cast(_TextContent, result.content[0])
    assert "bad_request" in content.text
    assert "something broke" in content.text
    assert "Action:" in content.text


def test_check_daemon_response_missing_message_uses_default():
    result = _check_daemon_response({"ok": False})
    assert isinstance(result, ToolResult)
    content = cast(_TextContent, result.content[0])
    assert "Request failed" in content.text
    assert "Action:" in content.text


def test_check_daemon_response_preserves_existing_action_hint():
    result = _check_daemon_response({"ok": False, "message": "boom\nAction: Retry later."})
    assert isinstance(result, ToolResult)
    assert cast(_TextContent, result.content[0]).text.count("Action:") == 1


def test_check_daemon_response_passes_extra_kwargs():
    result = _check_daemon_response(
        {"ok": False, "message": "err"},
        has_filter=True,
        has_cursor=True,
    )
    assert result is not None
    assert result.has_filter is True
    assert result.has_cursor is True


# ---------------------------------------------------------------------------
# _maybe_heartbeat_and_gap_scan (M-11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_heartbeat_fires_when_interval_elapsed():
    """Heartbeat fires when enough time has passed."""
    from mcp_telegram.daemon import HEARTBEAT_INTERVAL_S, _maybe_heartbeat_and_gap_scan, _SyncLoopState
    from mcp_telegram.event_handlers import EventHandlerManager

    conn_mock = MagicMock(spec=sqlite3.Connection)
    # Make the stats query return something
    cursor_fetchall = MagicMock(return_value=[("synced", 1)])
    mock_cursor = MagicMock()
    mock_cursor.fetchall = cursor_fetchall
    execute = MagicMock()
    execute.return_value = mock_cursor
    conn_mock.execute = execute
    conn = cast(sqlite3.Connection, conn_mock)

    client = MagicMock()
    client.is_connected = MagicMock(return_value=True)

    handler_manager = cast(EventHandlerManager, MagicMock())
    refresh_synced_dialogs = MagicMock(return_value=None)
    handler_manager.refresh_synced_dialogs = refresh_synced_dialogs

    import time

    sync_start = time.monotonic()
    # Set last_heartbeat far in the past to trigger
    old_heartbeat = sync_start - HEARTBEAT_INTERVAL_S - 1
    old_gap_scan = sync_start  # gap scan should NOT fire

    state = _SyncLoopState(
        sync_start=sync_start,
        last_heartbeat=old_heartbeat,
        last_gap_scan=old_gap_scan,
    )

    new_state = await _maybe_heartbeat_and_gap_scan(
        conn,
        client,
        handler_manager,
        state,
    )

    assert new_state is state
    assert state.last_heartbeat > old_heartbeat, "heartbeat timestamp should be updated"
    assert state.last_gap_scan == old_gap_scan
    refresh_synced_dialogs.assert_called_once()


@pytest.mark.asyncio
async def test_maybe_heartbeat_skips_when_recent():
    """Heartbeat does NOT fire when interval hasn't elapsed."""
    from mcp_telegram.daemon import _maybe_heartbeat_and_gap_scan, _SyncLoopState
    from mcp_telegram.event_handlers import EventHandlerManager

    conn = MagicMock(spec=sqlite3.Connection)
    client = MagicMock()
    handler_manager = cast(EventHandlerManager, MagicMock())
    refresh_synced_dialogs = MagicMock(return_value=None)
    handler_manager.refresh_synced_dialogs = refresh_synced_dialogs

    import time

    now = time.monotonic()

    state = _SyncLoopState(
        sync_start=now,
        last_heartbeat=now,
        last_gap_scan=now,
    )

    new_state = await _maybe_heartbeat_and_gap_scan(
        conn,
        client,
        handler_manager,
        state,
    )

    assert new_state is state
    assert state.last_heartbeat == now, "heartbeat timestamp should not change"
    assert state.last_gap_scan == now
    refresh_synced_dialogs.assert_not_called()


# ---------------------------------------------------------------------------
# Telemetry helpers (_base.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_telemetry_event_records_payload_via_daemon_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = cast(_TelemetryConnection, AsyncMock())
    ctx = cast(_TelemetryContext, AsyncMock())
    aenter = AsyncMock(return_value=connection)
    ctx.__aenter__ = aenter
    record_telemetry = AsyncMock()
    connection.record_telemetry = record_telemetry

    def fake_daemon_connection() -> _TelemetryContext:
        return ctx

    monkeypatch.setattr(_base, "daemon_connection", fake_daemon_connection)
    event = {"tool_name": "test_tool", "result_count": 1}

    await _send_telemetry_event(event)

    record_telemetry.assert_awaited_once_with(event=event)


@pytest.mark.asyncio
async def test_send_telemetry_event_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingDaemonConnection:
        async def __aenter__(self) -> None:
            raise RuntimeError("daemon unavailable")

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    def failing_daemon_connection() -> FailingDaemonConnection:
        return FailingDaemonConnection()

    monkeypatch.setattr(_base, "daemon_connection", failing_daemon_connection)

    await _send_telemetry_event({"tool_name": "test_tool"})


@pytest.mark.asyncio
async def test_telemetry_done_callback_logs_error_on_exception(caplog: pytest.LogCaptureFixture) -> None:
    async def fail() -> None:
        raise RuntimeError("telemetry failed")

    task = asyncio.create_task(fail())
    with pytest.raises(RuntimeError):
        await task

    with caplog.at_level("WARNING", logger="mcp_telegram.tools._base"):
        _telemetry_done_callback(task)

    assert any("telemetry_event_failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_telemetry_done_callback_ignores_cancelled(caplog: pytest.LogCaptureFixture) -> None:
    task = asyncio.create_task(asyncio.sleep(10))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with caplog.at_level("WARNING", logger="mcp_telegram.tools._base"):
        _telemetry_done_callback(task)

    assert not any("telemetry_event_failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_track_tool_telemetry_has_no_telemetry_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    send_mock = AsyncMock()
    monkeypatch.setattr(_base, "_send_telemetry_event", send_mock)

    class Args(ToolArgs): ...

    @_track_tool_telemetry("ok_tool")
    async def ok_tool(_args: Args) -> ToolResult:
        return ToolResult(result_count=7, has_cursor=True, page_depth=3, has_filter=True)

    await ok_tool(Args())
    await asyncio.sleep(0.01)

    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_track_tool_telemetry_preserves_runner_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    send_mock = AsyncMock()
    monkeypatch.setattr(_base, "_send_telemetry_event", send_mock)

    class Args(ToolArgs): ...

    @_track_tool_telemetry("error_tool")
    async def error_tool(_args: Args) -> ToolResult:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await error_tool(Args())
    await asyncio.sleep(0.01)
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_track_tool_telemetry_does_not_handle_delivery_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    send_mock = AsyncMock(side_effect=RuntimeError("telemetry backend down"))
    monkeypatch.setattr(_base, "_send_telemetry_event", send_mock)
    class Args(ToolArgs): ...

    @_track_tool_telemetry("warn_tool")
    async def ok_tool(_args: Args) -> ToolResult:
        return ToolResult()

    await ok_tool(Args())
    await asyncio.sleep(0.01)

    send_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# _class_name_to_snake
# ---------------------------------------------------------------------------


def test_class_name_to_snake_two_word() -> None:
    from mcp_telegram.tools._base import _class_name_to_snake

    assert _class_name_to_snake("ListMessages") == "list_messages"


def test_class_name_to_snake_single_word() -> None:
    from mcp_telegram.tools._base import _class_name_to_snake

    assert _class_name_to_snake("Feedback") == "feedback"


def test_class_name_to_snake_multi_word() -> None:
    from mcp_telegram.tools._base import _class_name_to_snake

    assert _class_name_to_snake("GetMyRecentActivity") == "get_my_recent_activity"


def test_class_name_to_snake_with_digits() -> None:
    from mcp_telegram.tools._base import _class_name_to_snake

    assert _class_name_to_snake("Trace2FA") == "trace2_fa"


def test_class_name_to_snake_already_snake() -> None:
    from mcp_telegram.tools._base import _class_name_to_snake

    assert _class_name_to_snake("mark_dialog_for_sync") == "mark_dialog_for_sync"


def test_class_name_to_snake_all_caps_prefix() -> None:
    from mcp_telegram.tools._base import _class_name_to_snake

    assert _class_name_to_snake("APIToken") == "api_token"


# ---------------------------------------------------------------------------
# _sanitize_tool_schema
# ---------------------------------------------------------------------------


def test_sanitize_strips_null_anyof_and_merges() -> None:
    from mcp_telegram.tools._base import _sanitize_tool_schema

    schema: dict[str, object] = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "title": "Dialog",
    }
    result = _sanitize_tool_schema(schema)
    assert isinstance(result, dict)
    assert "anyOf" not in result
    assert "default" not in result
    assert result["type"] == "string"
    assert result["title"] == "Dialog"


def test_sanitize_preserves_multi_variant_anyof() -> None:
    from mcp_telegram.tools._base import _sanitize_tool_schema

    schema: dict[str, object] = {
        "anyOf": [{"type": "string"}, {"type": "integer"}],
        "default": None,
    }
    result = _sanitize_tool_schema(schema)
    assert isinstance(result, dict)
    assert "anyOf" in result
    assert len(result["anyOf"]) == 2  # type: ignore[arg-type]
    assert "default" not in result


def test_sanitize_removes_default_none_for_non_null_type() -> None:
    from mcp_telegram.tools._base import _sanitize_tool_schema

    schema: dict[str, object] = {
        "type": "string",
        "default": None,
    }
    result = _sanitize_tool_schema(schema)
    assert isinstance(result, dict)
    assert "default" not in result
    assert result["type"] == "string"


def test_sanitize_handles_nested_dicts() -> None:
    from mcp_telegram.tools._base import _sanitize_tool_schema

    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            }
        },
    }
    result = _sanitize_tool_schema(schema)
    assert isinstance(result, dict)
    properties = result["properties"]
    assert isinstance(properties, dict)
    name_schema = properties["name"]
    assert isinstance(name_schema, dict)
    assert "anyOf" not in name_schema
    assert "default" not in name_schema
    assert name_schema["type"] == "string"


def test_sanitize_handles_lists_recursively() -> None:
    from mcp_telegram.tools._base import _sanitize_tool_schema

    schema: dict[str, object] = {
        "anyOf": [
            {
                "type": "array",
                "items": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                },
            },
            {"type": "null"},
        ]
    }
    result = _sanitize_tool_schema(schema)
    assert isinstance(result, dict)
    assert "anyOf" not in result
    items = result["items"]
    assert isinstance(items, dict)
    assert "anyOf" not in items
    assert "default" not in items
    assert items["type"] == "string"


def test_sanitize_no_anyof_no_op() -> None:
    from mcp_telegram.tools._base import _sanitize_tool_schema

    schema: dict[str, object] = {"type": "integer", "minimum": 1}
    result = _sanitize_tool_schema(schema)
    assert result == {"type": "integer", "minimum": 1}
