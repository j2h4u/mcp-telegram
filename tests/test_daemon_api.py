"""Tests for DaemonAPIServer — Unix socket request handlers (Plan 29-01, Task 2).

Uses in-memory SQLite for DB connections, MagicMock/AsyncMock for the
Telegram client.  No real Telegram API calls are made.
"""
from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer, get_daemon_socket_path
from mcp_telegram.fts import MESSAGES_FTS_DDL, stem_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_server(
    conn: sqlite3.Connection | None = None,
    client: Any | None = None,
) -> DaemonAPIServer:
    """Return a DaemonAPIServer wired to in-memory DB and mock client."""
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    shutdown_event = asyncio.Event()
    return DaemonAPIServer(conn, client, shutdown_event)


def _make_db(*, with_fts: bool = False) -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the required schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE synced_dialogs (
            dialog_id       INTEGER PRIMARY KEY,
            status          TEXT NOT NULL DEFAULT 'not_synced',
            last_synced_at  INTEGER,
            last_event_at   INTEGER,
            sync_progress   INTEGER DEFAULT 0,
            total_messages  INTEGER,
            access_lost_at  INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            dialog_id           INTEGER NOT NULL,
            message_id          INTEGER NOT NULL,
            sent_at             INTEGER NOT NULL,
            text                TEXT,
            sender_id           INTEGER,
            sender_first_name   TEXT,
            media_description   TEXT,
            reply_to_msg_id     INTEGER,
            forum_topic_id      INTEGER,
            reactions           TEXT,
            is_deleted          INTEGER NOT NULL DEFAULT 0,
            deleted_at          INTEGER,
            PRIMARY KEY (dialog_id, message_id)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_versions (
            dialog_id   INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            version     INTEGER NOT NULL,
            old_text    TEXT,
            edit_date   INTEGER,
            PRIMARY KEY (dialog_id, message_id, version)
        ) WITHOUT ROWID
        """
    )
    if with_fts:
        conn.execute(MESSAGES_FTS_DDL)
    conn.commit()
    return conn


def _insert_synced_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    status: str = "synced",
    *,
    last_synced_at: int | None = None,
    last_event_at: int | None = None,
    sync_progress: int | None = None,
    total_messages: int | None = None,
    access_lost_at: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, last_synced_at, last_event_at, sync_progress, total_messages, access_lost_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (dialog_id, status, last_synced_at, last_event_at, sync_progress, total_messages, access_lost_at),
    )
    conn.commit()


def _insert_message_version(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    version: int,
    old_text: str = "old text",
    edit_date: int = 1700000000,
) -> None:
    conn.execute(
        "INSERT INTO message_versions (dialog_id, message_id, version, old_text, edit_date) "
        "VALUES (?, ?, ?, ?, ?)",
        (dialog_id, message_id, version, old_text, edit_date),
    )
    conn.commit()


def _insert_message(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    text: str = "test message",
    sent_at: int = 1700000000,
    sender_first_name: str = "Alice",
) -> None:
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text, sender_first_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (dialog_id, message_id, sent_at, text, sender_first_name),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# get_daemon_socket_path
# ---------------------------------------------------------------------------


def test_get_daemon_socket_path() -> None:
    """get_daemon_socket_path returns a path ending in daemon.sock."""
    path = get_daemon_socket_path()
    assert path.name == "daemon.sock", f"Expected daemon.sock, got {path.name}"
    assert "mcp-telegram" in str(path)


# ---------------------------------------------------------------------------
# list_messages — from sync.db
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_from_db() -> None:
    """list_messages with synced dialog returns messages from sync.db, no client call."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 100, text="Hello from DB")

    client = MagicMock()
    client.iter_messages = AsyncMock()
    server = make_server(conn, client)

    result = await server._list_messages({"dialog_id": 1, "limit": 10})

    assert result["ok"] is True, f"Expected ok=True, got {result}"
    assert result["data"]["source"] == "sync_db"
    messages = result["data"]["messages"]
    assert len(messages) == 1
    assert messages[0]["message_id"] == 100
    assert messages[0]["text"] == "Hello from DB"
    # Client must NOT be called for synced dialogs
    client.iter_messages.assert_not_called()


# ---------------------------------------------------------------------------
# list_messages — on-demand fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_on_demand() -> None:
    """list_messages with no synced_dialog row calls client.iter_messages on-demand."""
    conn = _make_db()
    # No synced_dialog row for dialog_id=2

    # Mock message object
    mock_msg = MagicMock()
    mock_msg.id = 200
    mock_msg.date = MagicMock()
    mock_msg.date.timestamp.return_value = 1700000001.0
    mock_msg.message = "On-demand message"
    mock_msg.sender_id = None
    sender = MagicMock()
    sender.first_name = "Bob"
    mock_msg.sender = sender
    mock_msg.media = None
    mock_msg.reply_to = None
    mock_msg.reactions = None
    mock_msg.reply_to_msg_id = None
    mock_msg.forum_topic_id = None

    async def _fake_iter_messages(*args: Any, **kwargs: Any):  # type: ignore[misc]
        yield mock_msg

    client = MagicMock()
    client.iter_messages = _fake_iter_messages
    server = make_server(conn, client)

    result = await server._list_messages({"dialog_id": 2, "limit": 10})

    assert result["ok"] is True, f"Expected ok=True, got {result}"
    assert result["data"]["source"] == "telegram"
    messages = result["data"]["messages"]
    assert len(messages) == 1
    assert messages[0]["message_id"] == 200


# ---------------------------------------------------------------------------
# list_messages — name resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_name_resolution() -> None:
    """list_messages resolves dialog name to dialog_id via client.get_entity."""
    conn = _make_db()

    # Entity returned by get_entity
    entity = MagicMock()
    entity.id = 123

    client = MagicMock()
    client.get_entity = AsyncMock(return_value=entity)

    async def _fake_iter(*args: Any, **kwargs: Any):  # type: ignore[misc]
        return
        yield  # make it an async generator

    client.iter_messages = _fake_iter
    server = make_server(conn, client)

    result = await server._list_messages({"dialog": "Alice", "limit": 10})

    assert result["ok"] is True, f"Expected ok=True, got {result}"
    client.get_entity.assert_called_once_with("Alice")


# ---------------------------------------------------------------------------
# list_messages — name resolution not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_name_resolution_not_found() -> None:
    """list_messages returns error when dialog name cannot be resolved."""
    conn = _make_db()

    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=ValueError("Not found"))

    # iter_dialogs returns nothing
    async def _no_dialogs(*args: Any, **kwargs: Any):  # type: ignore[misc]
        return
        yield

    client.iter_dialogs = _no_dialogs
    server = make_server(conn, client)

    result = await server._list_messages({"dialog": "nonexistent", "limit": 10})

    assert result["ok"] is False
    assert result["error"] == "dialog_not_found"


# ---------------------------------------------------------------------------
# list_messages — missing dialog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_missing_dialog() -> None:
    """list_messages with no dialog_id and no dialog string returns missing_dialog error."""
    server = make_server()

    result = await server._list_messages({})

    assert result["ok"] is False
    assert result["error"] == "missing_dialog"


# ---------------------------------------------------------------------------
# search_messages — FTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_messages_fts() -> None:
    """search_messages stems query and runs FTS MATCH, returning matching messages."""
    conn = _make_db(with_fts=True)
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 100, text="написал сообщение")

    # Populate FTS table
    stemmed = stem_text("написал сообщение")
    conn.execute(
        "INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
        (1, 100, stemmed),
    )
    conn.commit()

    server = make_server(conn)
    result = await server._search_messages({"dialog_id": 1, "query": "написали", "limit": 10})

    assert result["ok"] is True, f"Expected ok=True, got {result}"
    messages = result["data"]["messages"]
    assert len(messages) >= 1, "FTS must find morphological match"
    assert messages[0]["message_id"] == 100


# ---------------------------------------------------------------------------
# search_messages — empty query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_messages_empty_query() -> None:
    """search_messages with empty query returns empty result."""
    server = make_server(_make_db(with_fts=True))
    result = await server._search_messages({"dialog_id": 1, "query": "", "limit": 10})

    assert result["ok"] is True
    assert result["data"]["messages"] == []
    assert result["data"]["total"] == 0


# ---------------------------------------------------------------------------
# search_messages — name resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_messages_name_resolution() -> None:
    """search_messages resolves dialog name via client.get_entity, then runs FTS."""
    conn = _make_db(with_fts=True)

    entity = MagicMock()
    entity.id = 123
    client = MagicMock()
    client.get_entity = AsyncMock(return_value=entity)
    server = make_server(conn, client)

    result = await server._search_messages({"dialog": "Alice", "query": "hello"})

    assert result["ok"] is True
    client.get_entity.assert_called_once_with("Alice")


# ---------------------------------------------------------------------------
# list_dialogs — sync_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dialogs_sync_status() -> None:
    """list_dialogs returns correct sync_status for each dialog."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1, status="synced")
    # dialog_id=2 not in synced_dialogs

    # Mock two dialogs from Telegram
    dialog1 = MagicMock()
    dialog1.id = 1
    dialog1.name = "Chat One"
    dialog1.entity = MagicMock()
    dialog1.entity.__class__.__name__ = "User"
    dialog1.date = MagicMock()
    dialog1.date.timestamp.return_value = 1700000000.0
    dialog1.unread_count = 0

    dialog2 = MagicMock()
    dialog2.id = 2
    dialog2.name = "Chat Two"
    dialog2.entity = MagicMock()
    dialog2.entity.__class__.__name__ = "Channel"
    dialog2.date = MagicMock()
    dialog2.date.timestamp.return_value = 1700000001.0
    dialog2.unread_count = 5

    async def _fake_iter_dialogs(*args: Any, **kwargs: Any):  # type: ignore[misc]
        yield dialog1
        yield dialog2

    client = MagicMock()
    client.iter_dialogs = _fake_iter_dialogs
    server = make_server(conn, client)

    result = await server._list_dialogs({})

    assert result["ok"] is True
    dialogs = result["data"]["dialogs"]
    assert len(dialogs) == 2

    by_id = {d["id"]: d for d in dialogs}
    assert by_id[1]["sync_status"] == "synced"
    assert by_id[2]["sync_status"] == "not_synced"


# ---------------------------------------------------------------------------
# list_topics — through daemon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_topics_through_daemon() -> None:
    """list_topics calls Telegram and returns topics list."""
    conn = _make_db()

    # Mock forum topics result
    mock_topic = MagicMock()
    mock_topic.id = 1
    mock_topic.title = "General"
    mock_topic.date = 1700000000
    mock_topic.icon_emoji_id = None

    mock_result = MagicMock()
    mock_result.topics = [mock_topic]

    # Build a client where get_entity and __call__ are awaitable
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=MagicMock(id=123))
    # client(request) should return the mock result
    client.return_value = mock_result

    server = make_server(conn, client)

    # Patch GetForumTopicsRequest so the import guard is satisfied
    with patch("mcp_telegram.daemon_api.GetForumTopicsRequest") as _mock_req, \
         patch("mcp_telegram.daemon_api._TELETHON_AVAILABLE", True):
        result = await server._list_topics({"dialog_id": 123})

    assert result["ok"] is True, f"Expected ok=True, got {result}"
    assert "topics" in result["data"]
    assert len(result["data"]["topics"]) == 1
    assert result["data"]["topics"][0]["id"] == 1


# ---------------------------------------------------------------------------
# get_me — through daemon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_through_daemon() -> None:
    """get_me calls client.get_me() and returns user info dict."""
    me = MagicMock()
    me.id = 12345
    me.first_name = "Test"
    me.last_name = "User"
    me.username = "testuser"
    me.phone = "+1234567890"

    client = MagicMock()
    client.get_me = AsyncMock(return_value=me)
    server = make_server(client=client)

    result = await server._get_me({})

    assert result["ok"] is True
    assert result["data"]["id"] == 12345
    assert result["data"]["first_name"] == "Test"
    assert result["data"]["username"] == "testuser"
    client.get_me.assert_called_once()


# ---------------------------------------------------------------------------
# _dispatch — unknown method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_method() -> None:
    """_dispatch with unknown method returns ok=False, error=unknown_method."""
    server = make_server()
    result = await server._dispatch({"method": "foo_unknown"})

    assert result["ok"] is False
    assert result["error"] == "unknown_method"


# ---------------------------------------------------------------------------
# handle_client — exception in handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_client_exception() -> None:
    """handle_client writes error response when _dispatch raises."""
    server = make_server()

    # Override _dispatch to raise
    async def _bad_dispatch(req: dict) -> dict:
        raise RuntimeError("boom")

    server._dispatch = _bad_dispatch  # type: ignore[method-assign]

    # Create mock reader/writer
    request_line = json.dumps({"method": "list_messages"}).encode() + b"\n"
    reader = asyncio.StreamReader()
    reader.feed_data(request_line)
    reader.feed_eof()

    written: list[bytes] = []

    writer = MagicMock()
    writer.write = lambda data: written.append(data)
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()

    await server.handle_client(reader, writer)

    # Verify error response was written
    assert len(written) > 0
    response_data = b"".join(written)
    parsed = json.loads(response_data.decode().strip())
    assert parsed["ok"] is False


# ---------------------------------------------------------------------------
# handle_client — round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_client_round_trip() -> None:
    """handle_client reads JSON request, dispatches, writes JSON response."""
    server = make_server()

    # Provide a get_me request
    me = MagicMock()
    me.id = 42
    me.first_name = "Round"
    me.last_name = "Trip"
    me.username = "rt"
    me.phone = "+0"
    server._client = MagicMock()  # type: ignore[attr-defined]
    server._client.get_me = AsyncMock(return_value=me)

    request_line = json.dumps({"method": "get_me"}).encode() + b"\n"
    reader = asyncio.StreamReader()
    reader.feed_data(request_line)
    reader.feed_eof()

    written: list[bytes] = []
    writer = MagicMock()
    writer.write = lambda data: written.append(data)
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()

    await server.handle_client(reader, writer)

    response_data = b"".join(written)
    parsed = json.loads(response_data.decode().strip())
    assert parsed["ok"] is True
    assert parsed["data"]["id"] == 42


# ---------------------------------------------------------------------------
# mark_dialog_for_sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_dialog_for_sync_enable() -> None:
    """mark_dialog_for_sync with enable=True inserts row with status='not_synced'."""
    conn = _make_db()
    server = make_server(conn)
    result = await server._dispatch({"method": "mark_dialog_for_sync", "dialog_id": 42, "enable": True})
    assert result["ok"] is True
    row = conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = 42").fetchone()
    assert row is not None
    assert row[0] == "not_synced"


@pytest.mark.asyncio
async def test_mark_dialog_for_sync_ignores_existing() -> None:
    """mark_dialog_for_sync with enable=True on already-synced dialog does NOT overwrite status."""
    conn = _make_db()
    _insert_synced_dialog(conn, 42, status="synced")
    server = make_server(conn)
    result = await server._dispatch({"method": "mark_dialog_for_sync", "dialog_id": 42, "enable": True})
    assert result["ok"] is True
    row = conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = 42").fetchone()
    assert row[0] == "synced"  # NOT overwritten


@pytest.mark.asyncio
async def test_mark_dialog_for_sync_disable() -> None:
    """mark_dialog_for_sync with enable=False resets status to 'not_synced'."""
    conn = _make_db()
    _insert_synced_dialog(conn, 42, status="synced")
    server = make_server(conn)
    result = await server._dispatch({"method": "mark_dialog_for_sync", "dialog_id": 42, "enable": False})
    assert result["ok"] is True
    row = conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = 42").fetchone()
    assert row[0] == "not_synced"


# ---------------------------------------------------------------------------
# get_sync_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sync_status_synced_dialog() -> None:
    """get_sync_status returns all required fields for a synced dialog."""
    conn = _make_db()
    _insert_synced_dialog(
        conn, -1001234567890, status="synced",
        last_synced_at=1700000000, last_event_at=1700001000,
        sync_progress=500, total_messages=500,
    )
    _insert_message(conn, -1001234567890, 1, text="msg1")
    _insert_message(conn, -1001234567890, 2, text="msg2")
    server = make_server(conn)
    result = await server._dispatch({"method": "get_sync_status", "dialog_id": -1001234567890})
    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "synced"
    assert data["message_count"] == 2
    assert data["last_synced_at"] == 1700000000
    assert data["last_event_at"] == 1700001000
    assert data["delete_detection"] == "reliable (channel)"


@pytest.mark.asyncio
async def test_get_sync_status_dm_delete_detection() -> None:
    """get_sync_status returns 'best-effort weekly (DM)' delete_detection for positive dialog_id."""
    conn = _make_db()
    _insert_synced_dialog(conn, 12345, status="synced")
    server = make_server(conn)
    result = await server._dispatch({"method": "get_sync_status", "dialog_id": 12345})
    assert result["ok"] is True
    assert result["data"]["delete_detection"] == "best-effort weekly (DM)"


@pytest.mark.asyncio
async def test_get_sync_status_non_synced() -> None:
    """get_sync_status for non-synced dialog returns status='not_synced' and zero counts."""
    conn = _make_db()
    server = make_server(conn)
    result = await server._dispatch({"method": "get_sync_status", "dialog_id": 99999})
    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "not_synced"
    assert data["message_count"] == 0


# ---------------------------------------------------------------------------
# get_sync_alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sync_alerts_deleted_messages() -> None:
    """get_sync_alerts returns deleted messages with preserved text."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 100, text="deleted msg")
    conn.execute("UPDATE messages SET is_deleted = 1, deleted_at = 1700000500 WHERE message_id = 100")
    conn.commit()
    server = make_server(conn)
    result = await server._dispatch({"method": "get_sync_alerts", "since": 0, "limit": 50})
    assert result["ok"] is True
    deleted = result["data"]["deleted_messages"]
    assert len(deleted) == 1
    assert deleted[0]["text"] == "deleted msg"
    assert deleted[0]["deleted_at"] == 1700000500


@pytest.mark.asyncio
async def test_get_sync_alerts_edits() -> None:
    """get_sync_alerts returns edit history entries from message_versions."""
    conn = _make_db()
    _insert_message_version(conn, 1, 100, version=1, old_text="before edit", edit_date=1700000600)
    server = make_server(conn)
    result = await server._dispatch({"method": "get_sync_alerts", "since": 0, "limit": 50})
    assert result["ok"] is True
    edits = result["data"]["edits"]
    assert len(edits) == 1
    assert edits[0]["old_text"] == "before edit"


@pytest.mark.asyncio
async def test_get_sync_alerts_access_lost() -> None:
    """get_sync_alerts returns access_lost dialogs."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1, status="access_lost", access_lost_at=1700000700)
    server = make_server(conn)
    result = await server._dispatch({"method": "get_sync_alerts", "since": 0, "limit": 50})
    assert result["ok"] is True
    lost = result["data"]["access_lost"]
    assert len(lost) == 1
    assert lost[0]["dialog_id"] == 1


@pytest.mark.asyncio
async def test_get_sync_alerts_since_filters() -> None:
    """get_sync_alerts respects since parameter — only returns events after the timestamp."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 100, text="old delete")
    conn.execute("UPDATE messages SET is_deleted = 1, deleted_at = 1700000100 WHERE message_id = 100")
    _insert_message(conn, 1, 200, text="new delete")
    conn.execute("UPDATE messages SET is_deleted = 1, deleted_at = 1700000900 WHERE message_id = 200")
    conn.commit()
    server = make_server(conn)
    result = await server._dispatch({"method": "get_sync_alerts", "since": 1700000500, "limit": 50})
    deleted = result["data"]["deleted_messages"]
    assert len(deleted) == 1
    assert deleted[0]["message_id"] == 200


@pytest.mark.asyncio
async def test_get_sync_alerts_respects_limit() -> None:
    """get_sync_alerts respects the limit parameter for deleted_messages."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1, status="synced")
    for i in range(10):
        _insert_message(conn, 1, 100 + i, text=f"del {i}")
        conn.execute(
            f"UPDATE messages SET is_deleted = 1, deleted_at = {1700000000 + i} "
            f"WHERE message_id = {100 + i}"
        )
    conn.commit()
    server = make_server(conn)
    result = await server._dispatch({"method": "get_sync_alerts", "since": 0, "limit": 3})
    assert len(result["data"]["deleted_messages"]) == 3
