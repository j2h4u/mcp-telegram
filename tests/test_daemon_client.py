"""Tests for DaemonClient — Unix socket context manager for MCP tools (Plan 29-01, Task 3).

Uses real asyncio Unix sockets in tests for protocol round-trip tests.
Uses monkeypatching for error path tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_client import (
    DaemonConnection,
    DaemonNotRunningError,
    daemon_connection,
)

# ---------------------------------------------------------------------------
# DaemonNotRunningError — not running when socket absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_not_running_file_not_found(tmp_path: Path) -> None:
    """daemon_connection raises DaemonNotRunningError when socket file is absent."""
    nonexistent = tmp_path / "no_daemon.sock"

    with patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=nonexistent):
        with pytest.raises(DaemonNotRunningError) as exc_info:
            async with daemon_connection():
                pass  # pragma: no cover

    assert "mcp-telegram sync" in str(exc_info.value), (
        f"Error message must contain 'mcp-telegram sync', got: {exc_info.value}"
    )


# ---------------------------------------------------------------------------
# DaemonNotRunningError — connection refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_not_running_connection_refused(tmp_path: Path) -> None:
    """daemon_connection raises DaemonNotRunningError on ConnectionRefusedError."""
    sock_path = tmp_path / "refused.sock"

    with (
        patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=sock_path),
        patch("asyncio.open_unix_connection", side_effect=ConnectionRefusedError("refused")),
    ):
        with pytest.raises(DaemonNotRunningError) as exc_info:
            async with daemon_connection():
                pass  # pragma: no cover

    assert "mcp-telegram sync" in str(exc_info.value)


@pytest.mark.asyncio
async def test_daemon_connection_timeout_raises(tmp_path: Path) -> None:
    """daemon_connection wraps connect timeout into DaemonNotRunningError."""
    sock_path = tmp_path / "timeout.sock"

    with (
        patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=sock_path),
        patch("asyncio.open_unix_connection", side_effect=TimeoutError("connect timeout")),
    ):
        with pytest.raises(DaemonNotRunningError) as exc_info:
            async with daemon_connection():
                pass  # pragma: no cover

    assert "timed out while connecting" in str(exc_info.value)


@pytest.mark.asyncio
async def test_daemon_connection_handles_missing_reader_writer() -> None:
    """daemon_connection validates open_unix_connection returned pair."""
    with (
        patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=Path("/tmp/missing.sock")),
        patch("asyncio.open_unix_connection", return_value=(None, None)),
    ):
        with pytest.raises(DaemonNotRunningError, match="connection was not established"):
            async with daemon_connection():
                pass  # pragma: no cover


# ---------------------------------------------------------------------------
# request round trip — real asyncio Unix socket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_round_trip(tmp_path: Path) -> None:
    """DaemonConnection.request sends JSON, receives JSON response from real server."""
    sock_path = tmp_path / "echo.sock"

    echo_responses: list[bytes] = []

    async def _echo_server(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Echo server: reads one line, responds with {"ok": true, "echo": <request>}."""
        line = await reader.readline()
        req = cast(dict[str, object], json.loads(line.decode()))
        response = {"ok": True, "echo": req}
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(_echo_server, path=str(sock_path))
    try:
        with patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=sock_path):
            async with daemon_connection() as conn:
                result = cast(dict[str, object], await conn.request({"method": "get_me"}))

        assert result["ok"] is True
        assert cast(dict[str, object], result["echo"])["method"] == "get_me"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_request_supports_multiple_calls_in_one_connection(tmp_path: Path) -> None:
    """DaemonConnection can send sequential requests on the same stream."""
    sock_path = tmp_path / "multi.sock"

    async def _multi_server(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while line := await reader.readline():
            req = cast(dict[str, object], json.loads(line.decode()))
            response = {"ok": True, "method": req["method"]}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(_multi_server, path=str(sock_path))
    try:
        with patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=sock_path):
            async with daemon_connection() as conn:
                first = cast(dict[str, object], await conn.request({"method": "get_me"}))
                second = cast(dict[str, object], await conn.request({"method": "describe_source"}))

        assert first["method"] == "get_me"
        assert second["method"] == "describe_source"
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# request — EOF raises DaemonNotRunningError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_eof_raises(tmp_path: Path) -> None:
    """DaemonConnection.request raises DaemonNotRunningError when server closes without response."""
    sock_path = tmp_path / "eof.sock"

    async def _close_immediately(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Accept connection and close without writing a response (triggers EOF or reset)."""
        try:
            await reader.read(1024)  # consume incoming data
        except OSError:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        except RuntimeError:
            pass

    server = await asyncio.start_unix_server(_close_immediately, path=str(sock_path))
    try:
        with patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=sock_path):
            async with daemon_connection() as conn:
                with pytest.raises(DaemonNotRunningError):
                    await conn.request({"method": "get_me"})
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_request_timeout_raises_daemon_not_running(tmp_path: Path) -> None:
    """DaemonConnection.request times out instead of waiting forever."""
    sock_path = tmp_path / "stall.sock"

    async def _stall_server(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readline()
        await asyncio.sleep(1)
        writer.close()

    server = await asyncio.start_unix_server(_stall_server, path=str(sock_path))
    try:
        with patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=sock_path):
            async with daemon_connection(timeout_seconds=0.01) as conn:
                with pytest.raises(DaemonNotRunningError, match="timed out"):
                    await conn.request({"method": "get_me"})
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_request_send_timeout_raises_daemon_not_running() -> None:
    """Timeout while draining writer turns into DaemonNotRunningError."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.drain = AsyncMock(side_effect=TimeoutError("writer stall"))

    conn = DaemonConnection(reader, writer)

    with pytest.raises(DaemonNotRunningError, match="timed out while sending request"):
        await conn.request({"method": "get_me"})


@pytest.mark.asyncio
async def test_request_read_connection_reset_raises_daemon_not_running() -> None:
    """Connection reset while reading response is surfaced as daemon-not-running."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline = AsyncMock(side_effect=ConnectionResetError("reset"))
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.drain = AsyncMock()

    conn = DaemonConnection(reader, writer)

    with pytest.raises(DaemonNotRunningError, match="closed the connection unexpectedly"):
        await conn.request({"method": "get_me"})


@pytest.mark.asyncio
async def test_request_malformed_json_raises_daemon_not_running() -> None:
    """Malformed daemon payload is surfaced as DaemonNotRunningError."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readline = AsyncMock(return_value=b"{not-json}")
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.drain = AsyncMock()

    conn = DaemonConnection(reader, writer)

    with pytest.raises(DaemonNotRunningError, match="malformed JSON"):
        await conn.request({"method": "get_me"})


# ---------------------------------------------------------------------------
# Convenience method: list_messages with dialog_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_convenience() -> None:
    """list_messages convenience method sends correct request dict."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"messages": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.list_messages(dialog_id=123, limit=10)

    assert len(captured) == 1
    req = captured[0]
    assert req["method"] == "list_messages"
    assert req["dialog_id"] == 123
    assert req["limit"] == 10


# ---------------------------------------------------------------------------
# Convenience method: list_messages with dialog name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_convenience_with_name() -> None:
    """list_messages with dialog name sends dialog='Alice', dialog_id=0."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"messages": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.list_messages(dialog="Alice", limit=10)

    req = captured[0]
    assert req["method"] == "list_messages"
    assert req["dialog"] == "Alice"
    assert req["dialog_id"] == 0
    assert req["limit"] == 10


# ---------------------------------------------------------------------------
# Convenience method: search_messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_messages_convenience() -> None:
    """search_messages convenience method sends correct request dict."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"messages": [], "total": 0}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.search_messages(dialog_id=123, query="test query")

    req = captured[0]
    assert req["method"] == "search_messages"
    assert req["dialog_id"] == 123
    assert req["query"] == "test query"


# ---------------------------------------------------------------------------
# Convenience method: search_messages with dialog name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_messages_convenience_with_name() -> None:
    """search_messages with dialog name sends dialog='Alice', dialog_id=0."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"messages": [], "total": 0}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.search_messages(dialog="Alice", query="hello there")

    req = captured[0]
    assert req["method"] == "search_messages"
    assert req["dialog"] == "Alice"
    assert req["dialog_id"] == 0
    assert req["query"] == "hello there"


# ---------------------------------------------------------------------------
# Convenience method: trace_account_messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_account_messages_convenience() -> None:
    """trace_account_messages sends flat request and omits None optionals."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"groups": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.trace_account_messages(
        exact_account_id=123,
        exact_topic_id=5,
        group_by="dialog",
        limit=25,
        coverage_goal="best_effort_visible",
    )

    req = captured[0]
    assert req["method"] == "trace_account_messages"
    assert req["exact_account_id"] == 123
    assert req["exact_topic_id"] == 5
    assert req["group_by"] == "dialog"
    assert req["limit"] == 25
    assert req["coverage_goal"] == "best_effort_visible"
    assert "account" not in req


@pytest.mark.asyncio
async def test_trace_account_messages_includes_optional_filters() -> None:
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"groups": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.trace_account_messages(
        account="@alice",
        dialog="Forum",
        exact_dialog_id=-100222,
        sent_after="2024-01-01T00:00:00Z",
        sent_before="2024-02-01T00:00:00Z",
        navigation="cursor",
    )

    req = captured[0]
    assert req["method"] == "trace_account_messages"
    assert req["account"] == "@alice"
    assert req["dialog"] == "Forum"
    assert req["exact_dialog_id"] == -100222
    assert req["sent_after"] == "2024-01-01T00:00:00Z"
    assert req["sent_before"] == "2024-02-01T00:00:00Z"
    assert req["navigation"] == "cursor"


# ---------------------------------------------------------------------------
# Convenience method: list_dialogs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dialogs_convenience() -> None:
    """list_dialogs sends correct request dict with exclude_archived flag."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"dialogs": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.list_dialogs(exclude_archived=True)

    req = captured[0]
    assert req["method"] == "list_dialogs"
    assert req["exclude_archived"] is True


# ---------------------------------------------------------------------------
# Convenience method: list_topics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_topics_convenience() -> None:
    """list_topics sends correct request dict with dialog_id."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"topics": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.list_topics(dialog_id=123)

    req = captured[0]
    assert req["method"] == "list_topics"
    assert req["dialog_id"] == 123


# ---------------------------------------------------------------------------
# Convenience method: get_me
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_convenience() -> None:
    """get_me sends correct request dict."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"id": 42}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.get_me()

    req = captured[0]
    assert req["method"] == "get_me"


# ---------------------------------------------------------------------------
# Convenience method: get_entity_info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_info_convenience() -> None:
    """get_entity_info sends correct request dict with entity_id."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"type": "user", "name": "Alice"}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.get_entity_info(entity_id=123)

    assert len(captured) == 1
    req = captured[0]
    assert req["method"] == "get_entity_info"
    assert req["entity_id"] == 123


# ---------------------------------------------------------------------------
# Convenience method: get_inbox with explicit params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_inbox_convenience_explicit() -> None:
    """get_inbox sends correct request dict with all explicit params."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"groups": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.get_inbox(scope="all", limit=200, group_size_threshold=50)

    assert len(captured) == 1
    req = captured[0]
    assert req["method"] == "get_inbox"
    assert req["scope"] == "all"
    assert req["limit"] == 200
    assert req["group_size_threshold"] == 50


# ---------------------------------------------------------------------------
# Convenience method: get_inbox with default params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_inbox_convenience_defaults() -> None:
    """get_inbox sends scope='personal', limit=100, group_size_threshold=100 by default."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"groups": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.get_inbox()

    assert len(captured) == 1
    req = captured[0]
    assert req["method"] == "get_inbox"
    assert req["scope"] == "personal"
    assert req["limit"] == 100
    assert req["group_size_threshold"] == 100


@pytest.mark.asyncio
async def test_describe_source_convenience() -> None:
    """describe_source sends expected method payload."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"source_id": "telegram"}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.describe_source()

    req = captured[0]
    assert req["method"] == "describe_source"


@pytest.mark.asyncio
async def test_export_source_changes_convenience_with_optionals() -> None:
    """export_source_changes includes optional fields when provided."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"changes": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.export_source_changes(
        cursor="abc",
        limit=50,
        updated_after="2026-01-01T00:00:00Z",
        updated_after_cursor="last",
    )

    req = captured[0]
    assert req["method"] == "export_source_changes"
    assert req["cursor"] == "abc"
    assert req["limit"] == 50
    assert req["updated_after"] == "2026-01-01T00:00:00Z"
    assert req["updated_after_cursor"] == "last"


@pytest.mark.asyncio
async def test_export_source_changes_omits_optional_fields() -> None:
    """export_source_changes omits optional fields by default."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"changes": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.export_source_changes()

    req = captured[0]
    assert req["method"] == "export_source_changes"
    assert req["limit"] == 100
    assert "updated_after" not in req
    assert "updated_after_cursor" not in req


@pytest.mark.asyncio
async def test_read_source_unit_window_convenience() -> None:
    """read_source_unit_window forwards unit_ref and framing parameters."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"unit_ref": "u1"}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.read_source_unit_window(unit_ref="u1", before=2, after=3)

    req = captured[0]
    assert req["method"] == "read_source_unit_window"
    assert req["unit_ref"] == "u1"
    assert req["before"] == 2
    assert req["after"] == 3


@pytest.mark.asyncio
async def test_get_sync_status_convenience() -> None:
    """get_sync_status forwards dialog_id exactly."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"status": "synced"}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.get_sync_status(dialog_id=228055330)

    req = captured[0]
    assert req["method"] == "get_sync_status"
    assert req["dialog_id"] == 228055330


@pytest.mark.asyncio
async def test_get_sync_alerts_convenience_defaults() -> None:
    """get_sync_alerts sends defaults when params are omitted."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"alerts": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.get_sync_alerts()

    req = captured[0]
    assert req["method"] == "get_sync_alerts"
    assert req["since"] == 0
    assert req["limit"] == 50


@pytest.mark.asyncio
async def test_get_sync_alerts_convenience_custom_params() -> None:
    """get_sync_alerts forwards custom since/limit values."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True, "data": {"alerts": []}}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.get_sync_alerts(since=10, limit=5)

    req = captured[0]
    assert req["method"] == "get_sync_alerts"
    assert req["since"] == 10
    assert req["limit"] == 5


@pytest.mark.asyncio
async def test_submit_feedback_convenience_with_optional_fields() -> None:
    """submit_feedback forwards optional context and status metadata."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.submit_feedback(
        message="text",
        severity="high",
        context="ctx",
        model="mcp",
        harness="tests",
    )

    req = captured[0]
    assert req["method"] == "submit_feedback"
    assert req["message"] == "text"
    assert req["severity"] == "high"
    assert req["context"] == "ctx"
    assert req["model"] == "mcp"
    assert req["harness"] == "tests"


@pytest.mark.asyncio
async def test_submit_feedback_omits_optional_fields() -> None:
    """submit_feedback requires only message when optional args are omitted."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.submit_feedback(message="just text")

    req = captured[0]
    assert req["method"] == "submit_feedback"
    assert req["message"] == "just text"
    assert "severity" not in req
    assert "context" not in req
    assert "model" not in req
    assert "harness" not in req


@pytest.mark.asyncio
async def test_update_feedback_status_convenience_with_reason() -> None:
    """update_feedback_status includes optional reason when provided."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.update_feedback_status(feedback_id=77, status="done", reason="repro accepted")

    req = captured[0]
    assert req["method"] == "update_feedback_status"
    assert req["id"] == 77
    assert req["status"] == "done"
    assert req["reason"] == "repro accepted"


@pytest.mark.asyncio
async def test_update_feedback_status_omits_optional_reason() -> None:
    """update_feedback_status omits reason when not provided."""
    reader = MagicMock(spec=asyncio.StreamReader)
    writer = MagicMock(spec=asyncio.StreamWriter)
    conn = DaemonConnection(reader, writer)

    captured: list[dict] = []

    async def _mock_request(payload: dict) -> dict:
        captured.append(payload)
        return {"ok": True}

    conn.request = _mock_request  # type: ignore[method-assign]

    await conn.update_feedback_status(feedback_id=77, status="done")

    req = captured[0]
    assert req["method"] == "update_feedback_status"
    assert req["id"] == 77
    assert req["status"] == "done"
    assert "reason" not in req
