"""Tests for DaemonClient — Unix socket context manager for MCP tools (Plan 29-01, Task 3).

Uses real asyncio Unix sockets in tests for protocol round-trip tests.
Uses monkeypatching for error path tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

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

    async def _raise_refused(path: str) -> Any:
        raise ConnectionRefusedError("Connection refused")

    with (
        patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=sock_path),
        patch("asyncio.open_unix_connection", side_effect=ConnectionRefusedError("refused")),
    ):
        with pytest.raises(DaemonNotRunningError) as exc_info:
            async with daemon_connection():
                pass  # pragma: no cover

    assert "mcp-telegram sync" in str(exc_info.value)


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
        req = json.loads(line.decode())
        response = {"ok": True, "echo": req}
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(_echo_server, path=str(sock_path))
    try:
        with patch("mcp_telegram.daemon_client.get_daemon_socket_path", return_value=sock_path):
            async with daemon_connection() as conn:
                result = await conn.request({"method": "get_me"})

        assert result["ok"] is True
        assert result["echo"]["method"] == "get_me"
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
        except Exception:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
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
