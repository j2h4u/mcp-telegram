"""Tests for DaemonAPIServer — Unix socket request handlers (Plan 29-01, Task 2).

Uses in-memory SQLite for DB connections, MagicMock/AsyncMock for the
Telegram client.  No real Telegram API calls are made.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import UTC
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer, _classify_dialog_type, get_daemon_socket_path
from mcp_telegram.fts import MESSAGES_FTS_DDL, stem_text

# ---------------------------------------------------------------------------
# Module-wide patch: telethon_utils.get_peer_id returns entity.id for mocks
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_get_peer_id():
    """All tests use MagicMock entities — real get_peer_id can't handle them."""
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda entity: int(getattr(entity, "id", 0)),
    ):
        yield


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
    server = DaemonAPIServer(conn, client, shutdown_event)
    server._ready = True
    return server


def _make_db(*, with_fts: bool = False, with_entities: bool = False) -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the required schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE synced_dialogs (
            dialog_id           INTEGER PRIMARY KEY,
            status              TEXT NOT NULL DEFAULT 'not_synced',
            last_synced_at      INTEGER,
            last_event_at       INTEGER,
            sync_progress       INTEGER DEFAULT 0,
            total_messages      INTEGER,
            access_lost_at      INTEGER,
            read_inbox_max_id   INTEGER,
            read_outbox_max_id  INTEGER
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
            is_deleted          INTEGER NOT NULL DEFAULT 0,
            deleted_at          INTEGER,
            edit_date           INTEGER,
            out                 INTEGER NOT NULL DEFAULT 0,
            is_service          INTEGER NOT NULL DEFAULT 0,
            post_author         TEXT,
            PRIMARY KEY (dialog_id, message_id)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_reactions (
            dialog_id   INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            emoji       TEXT NOT NULL,
            count       INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (dialog_id, message_id, emoji)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_reactions_freshness (
            dialog_id   INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            checked_at  INTEGER NOT NULL,
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS topic_metadata (
            dialog_id           INTEGER NOT NULL,
            topic_id            INTEGER NOT NULL,
            title               TEXT NOT NULL,
            top_message_id      INTEGER,
            is_general          INTEGER NOT NULL DEFAULT 0,
            is_deleted          INTEGER NOT NULL DEFAULT 0,
            inaccessible_error  TEXT,
            inaccessible_at     INTEGER,
            updated_at          INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (dialog_id, topic_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
            id              INTEGER PRIMARY KEY,
            type            TEXT NOT NULL,
            name            TEXT,
            username        TEXT,
            name_normalized TEXT,
            updated_at      INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_forwards (
            dialog_id           INTEGER NOT NULL,
            message_id          INTEGER NOT NULL,
            fwd_from_peer_id    INTEGER,
            fwd_from_name       TEXT,
            fwd_date            INTEGER,
            fwd_channel_post    INTEGER,
            PRIMARY KEY (dialog_id, message_id)
        ) WITHOUT ROWID
        """
    )
    if with_fts:
        conn.execute(MESSAGES_FTS_DDL)
    conn.commit()
    return conn


def _make_db_with_activity() -> sqlite3.Connection:
    """_make_db() + v14/v15 activity_sync_state (Phase 999.1 and 999.1.1).

    Since v15 dropped activity_comments, this helper only layers the
    activity_sync_state table on top of the in-memory schema from _make_db().
    Tests that used to INSERT into activity_comments now INSERT into the
    messages table with out=1 — the unified write path post-Phase 999.1.1.
    """
    conn = _make_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_sync_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    # Seed the three state rows (mirroring v14 migration seeds).
    conn.executemany(
        "INSERT OR IGNORE INTO activity_sync_state (key, value) VALUES (?, ?)",
        [
            ("backfill_complete", "0"),
            ("backfill_offset_id", "0"),
            ("last_sync_at", None),
        ],
    )
    # entities table (needed by LEFT JOIN entities in _get_my_recent_activity).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
            id              INTEGER PRIMARY KEY,
            type            TEXT NOT NULL,
            name            TEXT,
            username        TEXT,
            name_normalized TEXT,
            updated_at      INTEGER NOT NULL
        )
        """
    )
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
        "INSERT INTO message_versions (dialog_id, message_id, version, old_text, edit_date) VALUES (?, ?, ?, ?, ?)",
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
    sender_id: int | None = None,
    forum_topic_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO messages "
        "(dialog_id, message_id, sent_at, text, sender_first_name, sender_id, forum_topic_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (dialog_id, message_id, sent_at, text, sender_first_name, sender_id, forum_topic_id),
    )
    conn.commit()


def _insert_topic_metadata(
    conn: sqlite3.Connection,
    dialog_id: int,
    topic_id: int,
    title: str,
) -> None:
    conn.execute(
        "INSERT INTO topic_metadata (dialog_id, topic_id, title) VALUES (?, ?, ?)",
        (dialog_id, topic_id, title),
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
# search_messages — global mode (no dialog)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_messages_global_searches_all_dialogs() -> None:
    """search_messages with no dialog searches across all synced dialogs."""
    conn = _make_db(with_fts=True, with_entities=True)
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_synced_dialog(conn, 2, status="synced")
    _insert_message(conn, 1, 10, text="написал сообщение")
    _insert_message(conn, 2, 20, text="написали письмо")
    _insert_entity(conn, 1, name="Dialog One")
    _insert_entity(conn, 2, name="Dialog Two")

    for dialog_id, message_id, text in [(1, 10, "написал сообщение"), (2, 20, "написали письмо")]:
        stemmed = stem_text(text)
        conn.execute(
            "INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
            (dialog_id, message_id, stemmed),
        )
    conn.commit()

    server = make_server(conn)
    result = await server._search_messages({"query": "написали", "limit": 10})

    assert result["ok"] is True
    messages = result["data"]["messages"]
    assert len(messages) == 2
    dialog_ids = {m["dialog_id"] for m in messages}
    assert dialog_ids == {1, 2}
    dialog_names = {m["dialog_name"] for m in messages}
    assert dialog_names == {"Dialog One", "Dialog Two"}


@pytest.mark.asyncio
async def test_search_messages_global_dialog_name_fallback() -> None:
    """Global search falls back to string dialog_id when entity has no name."""
    conn = _make_db(with_fts=True, with_entities=True)
    _insert_synced_dialog(conn, 99, status="synced")
    _insert_message(conn, 99, 5, text="тест")
    stemmed = stem_text("тест")
    conn.execute(
        "INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (99, 5, ?)",
        (stemmed,),
    )
    conn.commit()
    # No entity row for dialog 99 — COALESCE should return '99'

    server = make_server(conn)
    result = await server._search_messages({"query": "тест", "limit": 10})

    assert result["ok"] is True
    messages = result["data"]["messages"]
    assert len(messages) == 1
    assert messages[0]["dialog_name"] == "99"
    assert messages[0]["dialog_id"] == 99


@pytest.mark.asyncio
async def test_search_messages_global_navigation_token_uses_dialog_id_zero() -> None:
    """Global search next_navigation token encodes dialog_id=0."""
    from mcp_telegram.pagination import decode_navigation_token

    conn = _make_db(with_fts=True, with_entities=True)
    _insert_synced_dialog(conn, 1, status="synced")
    for msg_id in range(1, 6):
        _insert_message(conn, 1, msg_id, text="слово")
        conn.execute(
            "INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (1, ?, ?)",
            (msg_id, stem_text("слово")),
        )
    conn.commit()

    server = make_server(conn)
    result = await server._search_messages({"query": "слово", "limit": 3})

    assert result["ok"] is True
    next_nav = result["data"]["next_navigation"]
    assert next_nav is not None
    nav = decode_navigation_token(next_nav)
    assert nav.dialog_id == 0


# ---------------------------------------------------------------------------
# _classify_dialog_type unit tests
# ---------------------------------------------------------------------------


def test_classify_dialog_type_forum() -> None:
    from telethon.tl.types import Channel  # type: ignore[import-untyped]

    entity = MagicMock()
    entity.__class__ = Channel
    entity.megagroup = True
    entity.forum = True
    entity.broadcast = False
    assert _classify_dialog_type(entity) == "Forum"


def test_classify_dialog_type_group() -> None:
    from telethon.tl.types import Channel  # type: ignore[import-untyped]

    entity = MagicMock()
    entity.__class__ = Channel
    entity.megagroup = True
    entity.forum = False
    entity.broadcast = False
    assert _classify_dialog_type(entity) == "Group"


def test_classify_dialog_type_channel_broadcast() -> None:
    from telethon.tl.types import Channel  # type: ignore[import-untyped]

    entity = MagicMock()
    entity.__class__ = Channel
    entity.broadcast = True
    entity.megagroup = False
    entity.forum = False
    assert _classify_dialog_type(entity) == "Channel"


def test_classify_dialog_type_bot() -> None:
    entity = MagicMock()
    entity.first_name = "BotFather"
    entity.bot = True
    assert _classify_dialog_type(entity) == "Bot"


def test_classify_dialog_type_user() -> None:
    entity = MagicMock()
    entity.first_name = "Alice"
    entity.bot = False
    assert _classify_dialog_type(entity) == "User"


def test_classify_dialog_type_chat() -> None:
    from telethon.tl.types import Chat  # type: ignore[import-untyped]

    entity = MagicMock()
    entity.__class__ = Chat
    assert _classify_dialog_type(entity) == "Chat"


def test_classify_dialog_type_none() -> None:
    assert _classify_dialog_type(None) == "Unknown"


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


@pytest.mark.asyncio
async def test_list_dialogs_filter_substring_latinized() -> None:
    """filter='женск' matches 'KS x Женские сезоны' via latinized substring."""
    conn = _make_db()
    dialogs = [
        ("KS x Женские сезоны", -100),
        ("Random Chat", -200),
        ("Golang Дайджест", -300),
    ]
    mocks = []
    for name, did in dialogs:
        m = MagicMock()
        m.id = did
        m.name = name
        m.entity = MagicMock()
        m.entity.__class__.__name__ = "Chat"
        m.date = MagicMock()
        m.date.timestamp.return_value = 1700000000.0
        m.unread_count = 0
        mocks.append(m)

    async def _iter(*args: Any, **kwargs: Any):  # type: ignore[misc]
        for m in mocks:
            yield m

    client = MagicMock()
    client.iter_dialogs = _iter
    server = make_server(conn, client)

    out = await server._list_dialogs({"filter": "женск"})
    names = {d["name"] for d in out["data"]["dialogs"]}
    assert "KS x Женские сезоны" in names
    assert "Random Chat" not in names


@pytest.mark.asyncio
async def test_list_dialogs_filter_acronym() -> None:
    """filter='ЖС' matches 'KS x Женские сезоны' via word-initials (к,x,ж,с → кxжс)."""
    conn = _make_db()
    dialogs = [
        ("KS x Женские Сезоны", -100),
        ("Random Channel", -200),
    ]
    mocks = []
    for name, did in dialogs:
        m = MagicMock()
        m.id = did
        m.name = name
        m.entity = MagicMock()
        m.entity.__class__.__name__ = "Chat"
        m.date = MagicMock()
        m.date.timestamp.return_value = 1700000000.0
        m.unread_count = 0
        mocks.append(m)

    async def _iter(*args: Any, **kwargs: Any):  # type: ignore[misc]
        for m in mocks:
            yield m

    client = MagicMock()
    client.iter_dialogs = _iter
    server = make_server(conn, client)

    out = await server._list_dialogs({"filter": "ЖС"})
    names = {d["name"] for d in out["data"]["dialogs"]}
    assert names == {"KS x Женские Сезоны"}


@pytest.mark.asyncio
async def test_list_dialogs_filter_empty_and_none_are_noop() -> None:
    """Missing filter / empty string must NOT filter anything (backward-compat)."""
    conn = _make_db()
    mocks = []
    for i, name in enumerate(["Alpha", "Beta"]):
        m = MagicMock()
        m.id = -(100 + i)
        m.name = name
        m.entity = MagicMock()
        m.entity.__class__.__name__ = "Chat"
        m.date = MagicMock()
        m.date.timestamp.return_value = 1700000000.0
        m.unread_count = 0
        mocks.append(m)

    async def _iter(*args: Any, **kwargs: Any):  # type: ignore[misc]
        for m in mocks:
            yield m

    client = MagicMock()
    client.iter_dialogs = _iter

    for req in ({}, {"filter": None}, {"filter": "   "}):
        server = make_server(conn, client)
        out = await server._list_dialogs(req)
        assert len(out["data"]["dialogs"]) == 2, req


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

    with patch("mcp_telegram.daemon_api.GetForumTopicsRequest"):
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
        conn,
        -1001234567890,
        status="synced",
        last_synced_at=1700000000,
        last_event_at=1700001000,
        sync_progress=500,
        total_messages=500,
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
        conn.execute(f"UPDATE messages SET is_deleted = 1, deleted_at = {1700000000 + i} WHERE message_id = {100 + i}")
    conn.commit()
    server = make_server(conn)
    result = await server._dispatch({"method": "get_sync_alerts", "since": 0, "limit": 3})
    assert len(result["data"]["deleted_messages"]) == 3


# ---------------------------------------------------------------------------
# get_entity_info — daemon-side handler tests live in test_entity_info_user.py,
# test_entity_info_channel.py, test_entity_info_supergroup.py, test_entity_info_group.py,
# and test_entity_info_ttl.py (Plans 02–03).
# ---------------------------------------------------------------------------

    assert data["ttl_period"] == 604800


# ---------------------------------------------------------------------------
# list_unread_messages
# ---------------------------------------------------------------------------


def _make_dialog_mock(
    *,
    chat_id: int,
    name: str,
    unread_count: int,
    is_user: bool = False,
    is_group: bool = False,
    is_channel: bool = False,
    is_bot: bool = False,
    participants_count: int | None = None,
    unread_mentions_count: int = 0,
    read_inbox_max_id: int = 0,
    timestamp: float = 1700000000.0,
) -> MagicMock:
    """Build a mock dialog object matching the attributes _list_unread_messages reads."""
    dialog = MagicMock()
    dialog.id = chat_id
    dialog.name = name
    dialog.unread_count = unread_count
    dialog.unread_mentions_count = unread_mentions_count
    dialog.is_user = is_user
    dialog.is_group = is_group
    dialog.is_channel = is_channel

    entity = MagicMock()
    entity.bot = is_bot
    entity.participants_count = participants_count
    dialog.entity = entity

    raw_dialog = MagicMock()
    raw_dialog.read_inbox_max_id = read_inbox_max_id
    dialog.dialog = raw_dialog

    date_mock = MagicMock()
    date_mock.timestamp.return_value = timestamp
    dialog.date = date_mock

    return dialog


def _make_msg_mock(
    msg_id: int = 1,
    text: str = "Hello",
    timestamp: float = 1700000001.0,
    sender_first_name: str | None = "Alice",
    sender_id: int | None = 999,
) -> MagicMock:
    msg = MagicMock()
    msg.id = msg_id
    msg.message = text
    msg.sender_id = sender_id

    date_mock = MagicMock()
    date_mock.timestamp.return_value = timestamp
    msg.date = date_mock

    sender = MagicMock()
    sender.first_name = sender_first_name
    msg.sender = sender

    return msg


# ---------------------------------------------------------------------------
# Seeding helpers for Plan 38-02 SQL-only unread tests
# ---------------------------------------------------------------------------


def _seed_unread_state(
    conn: sqlite3.Connection,
    dialog_id: int,
    read_inbox_max_id: int | None,
    *,
    entity_type: str = "User",
    entity_name: str = "Alice",
    last_event_at: int | None = None,
    status: str = "synced",
) -> None:
    """Seed synced_dialogs (with read_inbox_max_id + status) + entities row."""
    import time as _time

    conn.execute(
        "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, ?)",
        (dialog_id, status),
    )
    conn.execute(
        "UPDATE synced_dialogs SET status=?, read_inbox_max_id=?, last_event_at=? WHERE dialog_id=?",
        (status, read_inbox_max_id, last_event_at or int(_time.time()), dialog_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, type, name, username, name_normalized, updated_at) "
        "VALUES (?, ?, ?, NULL, NULL, ?)",
        (dialog_id, entity_type, entity_name, int(_time.time())),
    )
    conn.commit()


def _seed_message(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    text: str = "hello",
    sender_id: int = 99,
    sender_first_name: str = "Alice",
    sent_at: int | None = None,
    is_deleted: int = 0,
) -> None:
    """Seed a single message row."""
    import time as _time

    conn.execute(
        "INSERT INTO messages "
        "(dialog_id, message_id, sent_at, text, sender_id, sender_first_name, is_deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            dialog_id,
            message_id,
            sent_at or int(_time.time()),
            text,
            sender_id,
            sender_first_name,
            is_deleted,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Plan 38-02: SQL-only unread tests (T1-T11)
# 7 rewrites of original iter_dialogs-based tests + 4 new edge-case tests
# All assert client was never called (zero Telegram API calls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_unread_messages_basic() -> None:
    """list_unread_messages returns grouped unread messages from sync.db (zero API calls)."""
    conn = _make_db()
    _seed_unread_state(conn, 1001, read_inbox_max_id=10, entity_type="User", entity_name="Alice")
    _seed_message(conn, 1001, message_id=11, text="Hi", sender_first_name="Alice")
    _seed_message(conn, 1001, message_id=12, text="Hey", sender_first_name="Alice")
    _seed_message(conn, 1001, message_id=13, text="Bye", sender_first_name="Alice")

    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )

    assert result["ok"] is True, f"Expected ok=True, got {result}"
    groups = result["data"]["groups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["dialog_id"] == 1001
    assert group["display_name"] == "Alice"
    assert group["category"] == "user"
    assert group["unread_count"] == 3
    msgs = group["messages"]
    assert [m["message_id"] for m in msgs] == [13, 12, 11]  # DESC order

    client.get_input_entity.assert_not_called()
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_empty() -> None:
    """list_unread_messages returns empty groups when synced_dialogs is empty."""
    conn = _make_db()
    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )

    assert result["ok"] is True
    assert result["data"]["groups"] == []
    client.get_input_entity.assert_not_called()
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_filters_channels_in_personal_scope() -> None:
    """scope=personal excludes channels; includes users."""
    conn = _make_db()
    _seed_unread_state(conn, 200, read_inbox_max_id=0, entity_type="Channel", entity_name="News")
    _seed_message(conn, 200, message_id=1)
    _seed_unread_state(conn, 201, read_inbox_max_id=0, entity_type="User", entity_name="Bob")
    _seed_message(conn, 201, message_id=1)

    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )

    assert result["ok"] is True
    ids = [g["dialog_id"] for g in result["data"]["groups"]]
    assert 200 not in ids, "Channel should be filtered in personal scope"
    assert 201 in ids
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_includes_groups_in_personal_scope() -> None:
    """SQL path has no participants_count column; _should_include_unread_dialog
    with participants_count=None defaults to include groups. This is a documented
    behaviour change vs the API path which used live participants_count to filter
    large groups (see RESEARCH.md Pitfall 2).
    """
    conn = _make_db()
    _seed_unread_state(conn, 1001, read_inbox_max_id=10, entity_type="Group", entity_name="BigGroup")
    _seed_message(conn, 1001, message_id=11)

    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )

    assert result["ok"] is True
    groups = result["data"]["groups"]
    assert len(groups) == 1
    assert groups[0]["category"] == "group"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_budget_limits_messages() -> None:
    """Budget allocation proportionally caps total messages returned."""
    conn = _make_db()
    # Two users with 50 unread each
    for dialog_id in [400, 401]:
        _seed_unread_state(conn, dialog_id, read_inbox_max_id=0, entity_type="User", entity_name=f"User{dialog_id}")
        for msg_id in range(1, 51):
            _seed_message(conn, dialog_id, message_id=msg_id)

    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 10,
            "group_size_threshold": 100,
        }
    )

    assert result["ok"] is True
    total_messages = sum(len(g["messages"]) for g in result["data"]["groups"])
    assert total_messages <= 10, f"Budget exceeded: {total_messages} messages returned"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_dispatch_routing() -> None:
    """_dispatch routes 'list_unread_messages' to _list_unread_messages; empty DB → ok=True."""
    conn = _make_db()
    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch({"method": "get_inbox"})

    assert result["ok"] is True, f"list_unread_messages dispatch failed: {result}"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_skips_non_synced_or_null() -> None:
    """Only status='synced' AND read_inbox_max_id IS NOT NULL dialogs appear in results."""
    conn = _make_db()
    _seed_unread_state(conn, 1001, read_inbox_max_id=10, status="synced")
    _seed_message(conn, 1001, message_id=11)
    _seed_unread_state(conn, 1002, read_inbox_max_id=10, status="access_lost")
    _seed_message(conn, 1002, message_id=11)
    _seed_unread_state(conn, 1003, read_inbox_max_id=None, status="synced")
    _seed_message(conn, 1003, message_id=11)

    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )

    groups = result["data"]["groups"]
    assert len(groups) == 1
    assert groups[0]["dialog_id"] == 1001
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_read_inbox_max_id_zero_returns_all() -> None:
    """read_inbox_max_id=0 is valid 'never read anything' — all messages are unread.
    Review-mandated test (Codex MEDIUM).
    """
    conn = _make_db()
    _seed_unread_state(conn, 1001, read_inbox_max_id=0)
    _seed_message(conn, 1001, message_id=1)
    _seed_message(conn, 1001, message_id=2)
    _seed_message(conn, 1001, message_id=3)

    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )

    groups = result["data"]["groups"]
    assert len(groups) == 1
    assert groups[0]["unread_count"] == 3
    assert [m["message_id"] for m in groups[0]["messages"]] == [3, 2, 1]
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_excludes_deleted_messages() -> None:
    """Deleted messages (is_deleted=1) must NOT count as unread or appear in messages.
    Review-mandated test (Codex MEDIUM).
    """
    conn = _make_db()
    _seed_unread_state(conn, 1001, read_inbox_max_id=10)
    _seed_message(conn, 1001, message_id=11, is_deleted=0)
    _seed_message(conn, 1001, message_id=12, is_deleted=1)
    _seed_message(conn, 1001, message_id=13, is_deleted=0)

    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )

    groups = result["data"]["groups"]
    assert len(groups) == 1
    assert groups[0]["unread_count"] == 2
    returned_ids = [m["message_id"] for m in groups[0]["messages"]]
    assert 12 not in returned_ids
    assert sorted(returned_ids) == [11, 13]
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_filter_excludes_ids_below_read_position() -> None:
    """message_id <= read_inbox_max_id excluded; strict > predicate."""
    conn = _make_db()
    _seed_unread_state(conn, 1001, read_inbox_max_id=10)
    _seed_message(conn, 1001, message_id=8)  # below — excluded
    _seed_message(conn, 1001, message_id=10)  # equal — excluded (strict >)
    _seed_message(conn, 1001, message_id=11)  # above — included
    _seed_message(conn, 1001, message_id=12)  # above — included

    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )

    groups = result["data"]["groups"]
    assert len(groups) == 1
    assert groups[0]["unread_count"] == 2
    assert [m["message_id"] for m in groups[0]["messages"]] == [12, 11]
    client.assert_not_called()


@pytest.mark.asyncio
async def test_list_unread_messages_response_reports_bootstrap_pending() -> None:
    """Response carries bootstrap_pending count so callers detect incomplete coverage.
    Review-mandated by all 3 reviewers as HIGH priority.
    """
    conn = _make_db()
    _seed_unread_state(conn, 1001, read_inbox_max_id=10)
    _seed_message(conn, 1001, message_id=11)
    _seed_unread_state(conn, 1002, read_inbox_max_id=None)
    _seed_unread_state(conn, 1003, read_inbox_max_id=None)

    client = MagicMock()
    server = make_server(conn, client)

    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )

    assert result["ok"] is True
    assert len(result["data"]["groups"]) == 1
    assert result["data"]["bootstrap_pending"] == 2, (
        f"Expected bootstrap_pending=2, got {result['data'].get('bootstrap_pending')}"
    )
    client.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers for Plan 33-01 tests (entities + telemetry tables)
# ---------------------------------------------------------------------------


def _make_db_with_entities(*, with_fts: bool = False) -> sqlite3.Connection:
    """Return an in-memory SQLite connection with sync.db schema + entities + telemetry."""
    conn = _make_db(with_fts=with_fts)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type_updated ON entities(type, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_username ON entities(username)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name   TEXT NOT NULL,
            timestamp   REAL NOT NULL,
            duration_ms REAL NOT NULL,
            result_count INTEGER NOT NULL,
            has_cursor  BOOLEAN NOT NULL,
            page_depth  INTEGER NOT NULL,
            has_filter  BOOLEAN NOT NULL,
            error_type  TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_tool_timestamp ON telemetry_events(tool_name, timestamp)")
    conn.commit()
    return conn


def _insert_entity(
    conn: sqlite3.Connection,
    entity_id: int,
    entity_type: str = "user",
    name: str = "Test User",
    username: str | None = None,
    name_normalized: str | None = None,
    updated_at: int | None = None,
) -> None:
    import time as _time

    if updated_at is None:
        updated_at = int(_time.time())
    conn.execute(
        "INSERT OR REPLACE INTO entities (id, type, name, username, name_normalized, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (entity_id, entity_type, name, username, name_normalized, updated_at),
    )
    conn.commit()


def _insert_telemetry(
    conn: sqlite3.Connection,
    tool_name: str = "ListDialogs",
    timestamp: float | None = None,
    duration_ms: float = 50.0,
    result_count: int = 10,
    has_cursor: bool = False,
    page_depth: int = 1,
    has_filter: bool = False,
    error_type: str | None = None,
) -> None:
    import time as _time

    if timestamp is None:
        timestamp = _time.time()
    conn.execute(
        "INSERT INTO telemetry_events "
        "(tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# record_telemetry (Plan 33-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_telemetry_inserts_row() -> None:
    """record_telemetry inserts a row into telemetry_events with all 8 fields."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch(
        {
            "method": "record_telemetry",
            "event": {
                "tool_name": "ListDialogs",
                "timestamp": time.time(),
                "duration_ms": 123.4,
                "result_count": 5,
                "has_cursor": False,
                "page_depth": 1,
                "has_filter": True,
                "error_type": None,
            },
        }
    )
    assert result["ok"] is True
    row = conn.execute("SELECT tool_name, duration_ms, has_filter FROM telemetry_events").fetchone()
    assert row is not None
    assert row[0] == "ListDialogs"
    assert row[1] == 123.4
    assert row[2] is True or row[2] == 1


@pytest.mark.asyncio
async def test_record_telemetry_returns_ok() -> None:
    """record_telemetry returns ok=True on success."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._record_telemetry(
        {
            "event": {
                "tool_name": "SearchMessages",
                "timestamp": 1700000000.0,
                "duration_ms": 50.0,
                "result_count": 0,
                "has_cursor": False,
                "page_depth": 1,
                "has_filter": False,
                "error_type": "NotFound",
            },
        }
    )
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_record_telemetry_db_failure() -> None:
    """record_telemetry returns ok=False on DB failure."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    conn.close()  # force DB failure
    result = await server._record_telemetry(
        {
            "event": {
                "tool_name": "X",
                "timestamp": 0,
                "duration_ms": 0,
                "result_count": 0,
                "has_cursor": False,
                "page_depth": 1,
                "has_filter": False,
                "error_type": None,
            },
        }
    )
    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# get_usage_stats (Plan 33-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_usage_stats_returns_stats() -> None:
    """get_usage_stats returns tool_distribution, total_calls, and latency stats."""
    conn = _make_db_with_entities()
    import time as _time

    now = _time.time()
    _insert_telemetry(conn, tool_name="ListDialogs", timestamp=now, duration_ms=100.0)
    _insert_telemetry(conn, tool_name="ListDialogs", timestamp=now, duration_ms=200.0)
    _insert_telemetry(conn, tool_name="SearchMessages", timestamp=now, duration_ms=50.0)

    server = make_server(conn)
    result = await server._dispatch({"method": "get_usage_stats"})
    assert result["ok"] is True
    data = result["data"]
    assert data["total_calls"] == 3
    assert data["tool_distribution"]["ListDialogs"] == 2
    assert data["tool_distribution"]["SearchMessages"] == 1


@pytest.mark.asyncio
async def test_get_usage_stats_empty_table() -> None:
    """get_usage_stats with empty table returns zeroed stats (not error)."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch({"method": "get_usage_stats"})
    assert result["ok"] is True
    data = result["data"]
    assert data["total_calls"] == 0
    assert data["tool_distribution"] == {}


@pytest.mark.asyncio
async def test_get_usage_stats_respects_since() -> None:
    """get_usage_stats respects since parameter — only counts recent rows."""
    conn = _make_db_with_entities()
    import time as _time

    old_ts = _time.time() - 90 * 86400  # 90 days ago
    new_ts = _time.time() - 1  # 1 second ago
    _insert_telemetry(conn, tool_name="Old", timestamp=old_ts)
    _insert_telemetry(conn, tool_name="New", timestamp=new_ts)

    server = make_server(conn)
    since = int(_time.time()) - 7 * 86400  # last 7 days
    result = await server._dispatch({"method": "get_usage_stats", "since": since})
    assert result["ok"] is True
    assert result["data"]["total_calls"] == 1
    assert "New" in result["data"]["tool_distribution"]
    assert "Old" not in result["data"]["tool_distribution"]


# ---------------------------------------------------------------------------
# upsert_entities (Plan 33-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_entities_inserts_rows() -> None:
    """upsert_entities inserts/replaces rows in entities table."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch(
        {
            "method": "upsert_entities",
            "entities": [
                {"id": 100, "type": "user", "name": "Alice", "username": "alice"},
                {"id": 200, "type": "group", "name": "Dev Chat"},
            ],
        }
    )
    assert result["ok"] is True
    assert result["upserted"] == 2
    rows = conn.execute("SELECT id, name FROM entities ORDER BY id").fetchall()
    assert len(rows) == 2
    assert tuple(rows[0]) == (100, "Alice")
    assert tuple(rows[1]) == (200, "Dev Chat")


@pytest.mark.asyncio
async def test_upsert_entities_computes_name_normalized() -> None:
    """upsert_entities computes name_normalized via latinize() for each entity."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    await server._dispatch(
        {
            "method": "upsert_entities",
            "entities": [
                {"id": 300, "type": "user", "name": "Николай"},
            ],
        }
    )
    row = conn.execute("SELECT name_normalized FROM entities WHERE id = 300").fetchone()
    assert row is not None
    assert row[0] is not None
    # latinize("Николай") should produce a Latin string
    assert row[0] == row[0].lower()
    assert all(c.isalnum() or c == " " for c in row[0])


@pytest.mark.asyncio
async def test_upsert_entities_empty_list() -> None:
    """upsert_entities with empty list returns ok=True, upserted=0."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch({"method": "upsert_entities", "entities": []})
    assert result == {"ok": True, "upserted": 0}


@pytest.mark.asyncio
async def test_upsert_entities_replaces_on_conflict() -> None:
    """upsert_entities with same id replaces existing row (INSERT OR REPLACE)."""
    conn = _make_db_with_entities()
    _insert_entity(conn, 100, name="Old Name")
    server = make_server(conn)
    await server._dispatch(
        {
            "method": "upsert_entities",
            "entities": [{"id": 100, "type": "user", "name": "New Name"}],
        }
    )
    row = conn.execute("SELECT name FROM entities WHERE id = 100").fetchone()
    assert row[0] == "New Name"


# ---------------------------------------------------------------------------
# resolve_entity (Plan 33-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_entity_exact_name() -> None:
    """resolve_entity with exact name match returns resolved result."""
    conn = _make_db_with_entities()
    import time as _time

    now = int(_time.time())
    _insert_entity(conn, 101, name="Alice Smith", name_normalized="alice smith", updated_at=now)

    server = make_server(conn)
    result = await server._dispatch({"method": "resolve_entity", "query": "Alice Smith"})
    assert result["ok"] is True
    assert result["data"]["result"] == "resolved"
    assert result["data"]["entity_id"] == 101
    assert result["data"]["display_name"] == "Alice Smith"


@pytest.mark.asyncio
async def test_resolve_entity_username_lookup() -> None:
    """resolve_entity with @username query looks up entities by username column."""
    conn = _make_db_with_entities()
    import time as _time

    now = int(_time.time())
    _insert_entity(conn, 102, name="Bob", username="bobby", updated_at=now)

    server = make_server(conn)
    result = await server._dispatch({"method": "resolve_entity", "query": "@bobby"})
    assert result["ok"] is True
    assert result["data"]["result"] == "resolved"
    assert result["data"]["entity_id"] == 102
    assert result["data"]["display_name"] == "Bob"


@pytest.mark.asyncio
async def test_resolve_entity_not_found() -> None:
    """resolve_entity with no match returns not_found result."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch({"method": "resolve_entity", "query": "Nobody"})
    assert result["ok"] is True
    assert result["data"]["result"] == "not_found"


@pytest.mark.asyncio
async def test_resolve_entity_username_not_found() -> None:
    """resolve_entity with @username that doesn't exist returns not_found."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch({"method": "resolve_entity", "query": "@nobody"})
    assert result["ok"] is True
    assert result["data"]["result"] == "not_found"


@pytest.mark.asyncio
async def test_resolve_entity_missing_query() -> None:
    """resolve_entity with empty query returns ok=False, error=missing_query."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch({"method": "resolve_entity", "query": ""})
    assert result["ok"] is False
    assert result["error"] == "missing_query"


@pytest.mark.asyncio
async def test_resolve_entity_fuzzy_candidates() -> None:
    """resolve_entity with fuzzy match returns candidates list."""
    conn = _make_db_with_entities()
    import time as _time

    now = int(_time.time())
    _insert_entity(conn, 201, name="Alex", name_normalized="alex", entity_type="user", updated_at=now)
    _insert_entity(conn, 202, name="Alexa", name_normalized="alexa", entity_type="user", updated_at=now)

    server = make_server(conn)
    result = await server._dispatch({"method": "resolve_entity", "query": "Alex"})
    assert result["ok"] is True
    # With two similar single-word matches, resolver returns candidates
    assert result["data"]["result"] in ("resolved", "candidates")


@pytest.mark.asyncio
async def test_resolve_entity_pascalcase_type_forward_compat() -> None:
    """resolve_entity finds entities written with PascalCase type (new vocabulary)."""
    conn = _make_db_with_entities()
    import time as _time

    now = int(_time.time())
    _insert_entity(conn, 201, entity_type="User", name="Carlos New", name_normalized="carlos new", updated_at=now)

    server = make_server(conn)
    result = await server._dispatch({"method": "resolve_entity", "query": "Carlos New"})
    assert result["ok"] is True
    assert result["data"]["result"] == "resolved"
    assert result["data"]["entity_id"] == 201


# ---------------------------------------------------------------------------
# dispatch routing for new methods (Plan 33-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_routes_record_telemetry() -> None:
    """_dispatch routes 'record_telemetry' correctly."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch(
        {
            "method": "record_telemetry",
            "event": {
                "tool_name": "X",
                "timestamp": 0,
                "duration_ms": 0,
                "result_count": 0,
                "has_cursor": False,
                "page_depth": 1,
                "has_filter": False,
                "error_type": None,
            },
        }
    )
    assert result.get("error") != "unknown_method"


@pytest.mark.asyncio
async def test_dispatch_routes_get_usage_stats() -> None:
    """_dispatch routes 'get_usage_stats' correctly."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch({"method": "get_usage_stats"})
    assert result.get("error") != "unknown_method"


@pytest.mark.asyncio
async def test_dispatch_routes_upsert_entities() -> None:
    """_dispatch routes 'upsert_entities' correctly."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch({"method": "upsert_entities", "entities": []})
    assert result.get("error") != "unknown_method"


@pytest.mark.asyncio
async def test_dispatch_routes_resolve_entity() -> None:
    """_dispatch routes 'resolve_entity' correctly."""
    conn = _make_db_with_entities()
    server = make_server(conn)
    result = await server._dispatch({"method": "resolve_entity", "query": "test"})
    assert result.get("error") != "unknown_method"


# ---------------------------------------------------------------------------
# DaemonConnection convenience wrappers (Plan 33-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_connection_record_telemetry_payload() -> None:
    """DaemonConnection.record_telemetry sends correct JSON payload."""
    from mcp_telegram.daemon_client import DaemonConnection

    reader = asyncio.StreamReader()
    writer = MagicMock()

    sent_data: list[bytes] = []
    writer.write = lambda data: sent_data.append(data)
    writer.drain = AsyncMock()

    conn = DaemonConnection(reader, writer)

    # Feed a response so request() completes
    reader.feed_data(json.dumps({"ok": True}).encode() + b"\n")

    event = {"tool_name": "X", "timestamp": 1.0}
    await conn.record_telemetry(event=event)

    payload = json.loads(sent_data[0].decode().strip())
    assert payload["method"] == "record_telemetry"
    assert payload["event"] == event


@pytest.mark.asyncio
async def test_daemon_connection_get_usage_stats_payload() -> None:
    """DaemonConnection.get_usage_stats sends correct JSON payload."""
    from mcp_telegram.daemon_client import DaemonConnection

    reader = asyncio.StreamReader()
    writer = MagicMock()

    sent_data: list[bytes] = []
    writer.write = lambda data: sent_data.append(data)
    writer.drain = AsyncMock()

    conn = DaemonConnection(reader, writer)
    reader.feed_data(json.dumps({"ok": True, "data": {}}).encode() + b"\n")

    await conn.get_usage_stats()

    payload = json.loads(sent_data[0].decode().strip())
    assert payload["method"] == "get_usage_stats"
    assert "since" not in payload  # default: no since param


@pytest.mark.asyncio
async def test_daemon_connection_get_usage_stats_with_since() -> None:
    """DaemonConnection.get_usage_stats with since sends the parameter."""
    from mcp_telegram.daemon_client import DaemonConnection

    reader = asyncio.StreamReader()
    writer = MagicMock()

    sent_data: list[bytes] = []
    writer.write = lambda data: sent_data.append(data)
    writer.drain = AsyncMock()

    conn = DaemonConnection(reader, writer)
    reader.feed_data(json.dumps({"ok": True, "data": {}}).encode() + b"\n")

    await conn.get_usage_stats(since=1000)

    payload = json.loads(sent_data[0].decode().strip())
    assert payload["since"] == 1000


@pytest.mark.asyncio
async def test_daemon_connection_upsert_entities_payload() -> None:
    """DaemonConnection.upsert_entities sends correct JSON payload."""
    from mcp_telegram.daemon_client import DaemonConnection

    reader = asyncio.StreamReader()
    writer = MagicMock()

    sent_data: list[bytes] = []
    writer.write = lambda data: sent_data.append(data)
    writer.drain = AsyncMock()

    conn = DaemonConnection(reader, writer)
    reader.feed_data(json.dumps({"ok": True, "upserted": 1}).encode() + b"\n")

    entities = [{"id": 1, "type": "user", "name": "A"}]
    await conn.upsert_entities(entities=entities)

    payload = json.loads(sent_data[0].decode().strip())
    assert payload["method"] == "upsert_entities"
    assert payload["entities"] == entities


@pytest.mark.asyncio
async def test_daemon_connection_resolve_entity_payload() -> None:
    """DaemonConnection.resolve_entity sends correct JSON payload."""
    from mcp_telegram.daemon_client import DaemonConnection

    reader = asyncio.StreamReader()
    writer = MagicMock()

    sent_data: list[bytes] = []
    writer.write = lambda data: sent_data.append(data)
    writer.drain = AsyncMock()

    conn = DaemonConnection(reader, writer)
    reader.feed_data(json.dumps({"ok": True, "data": {"result": "not_found"}}).encode() + b"\n")

    await conn.resolve_entity(query="Alice")

    payload = json.loads(sent_data[0].decode().strip())
    assert payload["method"] == "resolve_entity"
    assert payload["query"] == "Alice"


# ---------------------------------------------------------------------------
# daemon.py startup wiring (Plan 33-01)
# ---------------------------------------------------------------------------


def test_daemon_imports_migrate_legacy_databases() -> None:
    """daemon.py imports migrate_legacy_databases from sync_db."""
    from mcp_telegram import daemon

    assert hasattr(daemon, "migrate_legacy_databases")


# ---------------------------------------------------------------------------
# Phase 35-01: _build_list_messages_query — dynamic SQL builder
# ---------------------------------------------------------------------------


def test_build_list_messages_query_exists() -> None:
    """_build_list_messages_query is exported from daemon_api."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    assert callable(_build_list_messages_query)


def test_build_list_messages_query_basic_shape() -> None:
    """_build_list_messages_query returns (sql, params) with edit_date and topic_title columns."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    sql, params = _build_list_messages_query(dialog_id=1, limit=10)
    assert "edit_date" in sql
    assert "topic_title" in sql or "tm.title" in sql
    assert "topic_metadata" in sql
    assert "message_versions" in sql
    assert params["limit"] == 10
    assert params["dialog_id"] == 1
    assert "self_id" in params  # Phase 39.1-02: always bound


def test_build_list_messages_query_direction_newest() -> None:
    """_build_list_messages_query with direction=newest uses DESC order."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    sql, _ = _build_list_messages_query(dialog_id=1, limit=10, direction="newest")
    assert "DESC" in sql.upper()


def test_build_list_messages_query_direction_oldest() -> None:
    """_build_list_messages_query with direction=oldest uses ASC order."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    sql, _ = _build_list_messages_query(dialog_id=1, limit=10, direction="oldest")
    assert "ASC" in sql.upper()


def test_build_list_messages_query_sender_id_filter() -> None:
    """_build_list_messages_query with sender_id adds AND m.sender_id = :filter_sender_id clause."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    sql, params = _build_list_messages_query(dialog_id=1, limit=10, sender_id=42)
    assert "sender_id" in sql
    assert 42 in params.values()


def test_build_list_messages_query_sender_name_filter() -> None:
    """_build_list_messages_query with sender_name adds LIKE clause."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    sql, params = _build_list_messages_query(dialog_id=1, limit=10, sender_name="Alice")
    assert "LIKE" in sql.upper()
    assert any("Alice" in str(p) for p in params.values())


def test_build_list_messages_query_topic_filter() -> None:
    """_build_list_messages_query with topic_id adds forum_topic_id filter."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    sql, params = _build_list_messages_query(dialog_id=1, limit=10, topic_id=5)
    assert "forum_topic_id" in sql
    assert 5 in params.values()


def test_build_list_messages_query_unread_filter() -> None:
    """_build_list_messages_query with unread_after_id adds message_id > :unread_after_id clause."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    sql, params = _build_list_messages_query(dialog_id=1, limit=10, unread_after_id=100)
    assert "message_id" in sql
    assert 100 in params.values()


def test_build_list_messages_query_cursor_newest() -> None:
    """_build_list_messages_query with cursor and direction=newest uses message_id < :anchor."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    sql, params = _build_list_messages_query(dialog_id=1, limit=10, anchor_msg_id=500, direction="newest")
    assert "<" in sql
    assert 500 in params.values()


def test_build_list_messages_query_cursor_oldest() -> None:
    """_build_list_messages_query with cursor and direction=oldest uses message_id > :anchor."""
    from mcp_telegram.daemon_api import _build_list_messages_query

    sql, params = _build_list_messages_query(dialog_id=1, limit=10, anchor_msg_id=500, direction="oldest")
    assert ">" in sql
    assert 500 in params.values()


# ---------------------------------------------------------------------------
# Phase 35-01: list_messages — pagination (sync.db)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_pagination_sync_db() -> None:
    """list_messages with synced dialog + limit=2 returns 2 msgs and a next_navigation token."""
    DIALOG_ID = 9001
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="msg 101", sent_at=1700000001)
    _insert_message(conn, DIALOG_ID, 102, text="msg 102", sent_at=1700000002)
    _insert_message(conn, DIALOG_ID, 103, text="msg 103", sent_at=1700000003)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 2})

    assert result["ok"] is True, f"Unexpected error: {result}"
    data = result["data"]
    assert len(data["messages"]) == 2
    assert data.get("next_navigation") is not None, "Expected next_navigation token"


@pytest.mark.asyncio
async def test_list_messages_pagination_cursor_continues() -> None:
    """list_messages with navigation token continues from cursor position."""
    from mcp_telegram.pagination import HistoryDirection, encode_history_navigation

    DIALOG_ID = 9002
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="msg 101", sent_at=1700000001)
    _insert_message(conn, DIALOG_ID, 102, text="msg 102", sent_at=1700000002)
    _insert_message(conn, DIALOG_ID, 103, text="msg 103", sent_at=1700000003)
    _insert_message(conn, DIALOG_ID, 104, text="msg 104", sent_at=1700000004)

    # Get first page, cursor at msg 103 (newest-first, so 104 and 103 returned)
    token = encode_history_navigation(103, DIALOG_ID, direction=HistoryDirection.NEWEST)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 2, "navigation": token})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    # cursor=103 with direction=newest → message_id < 103 → returns 102, 101
    assert all(m["message_id"] < 103 for m in messages), (
        f"Expected messages before 103, got: {[m['message_id'] for m in messages]}"
    )


@pytest.mark.asyncio
async def test_list_messages_pagination_wrong_dialog_error() -> None:
    """list_messages with navigation token for wrong dialog returns error."""
    from mcp_telegram.pagination import HistoryDirection, encode_history_navigation

    DIALOG_ID = 9003
    OTHER_DIALOG = 9099
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="msg 101", sent_at=1700000001)

    # Token for a different dialog
    token = encode_history_navigation(101, OTHER_DIALOG, direction=HistoryDirection.NEWEST)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "navigation": token})

    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# Phase 35-01: list_messages — direction (sync.db)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_direction_oldest() -> None:
    """list_messages with direction=oldest returns messages ORDER BY message_id ASC."""
    DIALOG_ID = 9010
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="first", sent_at=1700000001)
    _insert_message(conn, DIALOG_ID, 102, text="second", sent_at=1700000002)
    _insert_message(conn, DIALOG_ID, 103, text="third", sent_at=1700000003)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "direction": "oldest"})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    ids = [m["message_id"] for m in messages]
    assert ids == sorted(ids), f"Expected ascending order, got: {ids}"


@pytest.mark.asyncio
async def test_list_messages_direction_newest() -> None:
    """list_messages with direction=newest returns messages ORDER BY message_id DESC."""
    DIALOG_ID = 9011
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="first", sent_at=1700000001)
    _insert_message(conn, DIALOG_ID, 102, text="second", sent_at=1700000002)
    _insert_message(conn, DIALOG_ID, 103, text="third", sent_at=1700000003)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "direction": "newest"})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    ids = [m["message_id"] for m in messages]
    assert ids == sorted(ids, reverse=True), f"Expected descending order, got: {ids}"


# ---------------------------------------------------------------------------
# Phase 35-01: list_messages — sender filter (sync.db)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_sender_filter() -> None:
    """list_messages with sender_id=42 returns only messages from sender_id=42."""
    DIALOG_ID = 9020
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="from alice", sender_id=42)
    _insert_message(conn, DIALOG_ID, 102, text="from bob", sender_id=99)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "sender_id": 42})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    assert len(messages) == 1
    assert messages[0]["message_id"] == 101
    assert messages[0]["sender_id"] == 42


@pytest.mark.asyncio
async def test_list_messages_sender_name_filter() -> None:
    """list_messages with sender_name='Alice' returns only Alice's messages (case-insensitive)."""
    DIALOG_ID = 9021
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="from alice", sender_first_name="Alice")
    _insert_message(conn, DIALOG_ID, 102, text="from bob", sender_first_name="Bob")

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "sender_name": "alice"})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    assert len(messages) == 1
    assert messages[0]["message_id"] == 101


# ---------------------------------------------------------------------------
# Phase 35-01: list_messages — topic filter (sync.db)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_topic_filter() -> None:
    """list_messages with topic_id=5 returns only messages with forum_topic_id=5."""
    DIALOG_ID = 9030
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="topic 5 msg", forum_topic_id=5)
    _insert_message(conn, DIALOG_ID, 102, text="topic 7 msg", forum_topic_id=7)
    _insert_message(conn, DIALOG_ID, 103, text="no topic msg", forum_topic_id=None)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "topic_id": 5})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    assert len(messages) == 1
    assert messages[0]["message_id"] == 101
    assert messages[0]["forum_topic_id"] == 5


# ---------------------------------------------------------------------------
# Phase 35-01: list_messages — unread filter (sync.db)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_unread_filter() -> None:
    """list_messages with unread_after_id=100 returns only messages with message_id > 100."""
    DIALOG_ID = 9040
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 99, text="old msg", sent_at=1700000001)
    _insert_message(conn, DIALOG_ID, 100, text="boundary msg", sent_at=1700000002)
    _insert_message(conn, DIALOG_ID, 101, text="new msg", sent_at=1700000003)
    _insert_message(conn, DIALOG_ID, 102, text="newer msg", sent_at=1700000004)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "unread_after_id": 100})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    msg_ids = {m["message_id"] for m in messages}
    assert 99 not in msg_ids
    assert 100 not in msg_ids
    assert 101 in msg_ids
    assert 102 in msg_ids


# ---------------------------------------------------------------------------
# Phase 35-01: list_messages — edit_date (sync.db)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_edit_date_sync_db() -> None:
    """sync.db path returns edit_date from message_versions (MAX edit_date per message)."""
    DIALOG_ID = 9050
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="edited msg", sent_at=1700000001)
    _insert_message_version(conn, DIALOG_ID, 101, version=1, edit_date=1700001000)
    _insert_message_version(conn, DIALOG_ID, 101, version=2, edit_date=1700002000)
    _insert_message(conn, DIALOG_ID, 102, text="unedited msg", sent_at=1700000002)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    msgs_by_id = {m["message_id"]: m for m in messages}

    # Edited message should have edit_date = MAX(1700001000, 1700002000) = 1700002000
    assert "edit_date" in msgs_by_id[101], "edit_date key must be present in message dict"
    assert msgs_by_id[101]["edit_date"] == 1700002000

    # Unedited message should have edit_date = None
    assert msgs_by_id[102].get("edit_date") is None


@pytest.mark.asyncio
async def test_list_messages_no_edit_date_is_none() -> None:
    """Messages without edits have edit_date=None in the response."""
    DIALOG_ID = 9051
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_message(conn, DIALOG_ID, 101, text="never edited", sent_at=1700000001)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10})

    assert result["ok"] is True
    messages = result["data"]["messages"]
    assert messages[0]["edit_date"] is None


# ---------------------------------------------------------------------------
# Phase 35-01: list_messages — topic_title label (sync.db)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_topic_label() -> None:
    """sync.db path returns topic_title from LEFT JOIN topic_metadata when forum_topic_id is set."""
    DIALOG_ID = 9060
    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    _insert_topic_metadata(conn, DIALOG_ID, topic_id=5, title="General Discussion")
    _insert_message(conn, DIALOG_ID, 101, text="topic msg", forum_topic_id=5)
    _insert_message(conn, DIALOG_ID, 102, text="no topic msg", forum_topic_id=None)

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    msgs_by_id = {m["message_id"]: m for m in messages}

    assert "topic_title" in msgs_by_id[101], "topic_title key must be present"
    assert msgs_by_id[101]["topic_title"] == "General Discussion"
    assert msgs_by_id[102].get("topic_title") is None


# ---------------------------------------------------------------------------
# Phase 35-01: list_messages — on-demand path: navigation token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_on_demand_navigation_offset_id() -> None:
    """list_messages with on-demand dialog + navigation token passes offset_id to iter_messages."""
    from mcp_telegram.pagination import HistoryDirection, encode_history_navigation

    DIALOG_ID = 9070

    captured_kwargs: dict = {}

    async def _fake_iter_messages(*args: Any, **kwargs: Any):  # type: ignore[misc]
        captured_kwargs.update(kwargs)
        return
        yield  # make it an async generator

    conn = _make_db()
    # No synced_dialog row → on-demand path
    client = MagicMock()
    client.iter_messages = _fake_iter_messages
    server = make_server(conn, client)

    token = encode_history_navigation(200, DIALOG_ID, direction=HistoryDirection.NEWEST)
    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "navigation": token})

    assert result["ok"] is True, f"Unexpected error: {result}"
    assert captured_kwargs.get("offset_id") == 200, (
        f"Expected offset_id=200 in iter_messages kwargs, got: {captured_kwargs}"
    )


@pytest.mark.asyncio
async def test_list_messages_on_demand_direction_oldest_reverse() -> None:
    """list_messages with on-demand dialog + direction=oldest passes reverse=True to iter_messages."""
    DIALOG_ID = 9071

    captured_kwargs: dict = {}

    async def _fake_iter_messages(*args: Any, **kwargs: Any):  # type: ignore[misc]
        captured_kwargs.update(kwargs)
        return
        yield  # make it an async generator

    conn = _make_db()
    client = MagicMock()
    client.iter_messages = _fake_iter_messages
    server = make_server(conn, client)

    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "direction": "oldest"})

    assert result["ok"] is True, f"Unexpected error: {result}"
    assert captured_kwargs.get("reverse") is True, (
        f"Expected reverse=True in iter_messages kwargs, got: {captured_kwargs}"
    )


# ---------------------------------------------------------------------------
# Phase 35-01: list_messages — on-demand path: sender/topic/unread filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_on_demand_sender_id_from_user() -> None:
    """list_messages with sender_id=42 on on-demand passes from_user=42 to iter_messages."""
    DIALOG_ID = 9080

    captured_kwargs: dict = {}

    async def _fake_iter_messages(*args: Any, **kwargs: Any):  # type: ignore[misc]
        captured_kwargs.update(kwargs)
        return
        yield

    conn = _make_db()
    client = MagicMock()
    client.iter_messages = _fake_iter_messages
    server = make_server(conn, client)

    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "sender_id": 42})

    assert result["ok"] is True, f"Unexpected error: {result}"
    assert captured_kwargs.get("from_user") == 42, (
        f"Expected from_user=42 in iter_messages kwargs, got: {captured_kwargs}"
    )


@pytest.mark.asyncio
async def test_list_messages_on_demand_topic_id_reply_to() -> None:
    """list_messages with topic_id=5 on on-demand passes reply_to=5 to iter_messages."""
    DIALOG_ID = 9081

    captured_kwargs: dict = {}

    async def _fake_iter_messages(*args: Any, **kwargs: Any):  # type: ignore[misc]
        captured_kwargs.update(kwargs)
        return
        yield

    conn = _make_db()
    client = MagicMock()
    client.iter_messages = _fake_iter_messages
    server = make_server(conn, client)

    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "topic_id": 5})

    assert result["ok"] is True, f"Unexpected error: {result}"
    assert captured_kwargs.get("reply_to") == 5, f"Expected reply_to=5 in iter_messages kwargs, got: {captured_kwargs}"


@pytest.mark.asyncio
async def test_list_messages_on_demand_unread_after_id_min_id() -> None:
    """list_messages with unread_after_id=100 on on-demand passes min_id=100 to iter_messages."""
    DIALOG_ID = 9082

    captured_kwargs: dict = {}

    async def _fake_iter_messages(*args: Any, **kwargs: Any):  # type: ignore[misc]
        captured_kwargs.update(kwargs)
        return
        yield

    conn = _make_db()
    client = MagicMock()
    client.iter_messages = _fake_iter_messages
    server = make_server(conn, client)

    result = await server._list_messages({"dialog_id": DIALOG_ID, "limit": 10, "unread_after_id": 100})

    assert result["ok"] is True, f"Unexpected error: {result}"
    assert captured_kwargs.get("min_id") == 100, f"Expected min_id=100 in iter_messages kwargs, got: {captured_kwargs}"


# ---------------------------------------------------------------------------
# context_message_id — _list_messages_context_window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_context_window_centred() -> None:
    """context_message_id returns messages centred on anchor (before + anchor + after)."""
    DIALOG_ID = 7001

    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    for mid in range(1, 11):
        _insert_message(conn, DIALOG_ID, mid, text=f"msg {mid}", sent_at=1700000000 + mid)
    server = make_server(conn)

    result = await server._list_messages({"dialog_id": DIALOG_ID, "context_message_id": 5, "context_size": 4})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    ids = [m["message_id"] for m in messages]
    # context_size=4 → half=2, so: ids <= 5 DESC LIMIT 3 → [5,4,3] → reversed [3,4,5]
    # ids > 5 ASC LIMIT 2 → [6,7]
    assert ids == [3, 4, 5, 6, 7]
    assert result["data"]["anchor_message_id"] == 5
    assert result["data"]["source"] == "sync_db"


@pytest.mark.asyncio
async def test_list_messages_context_window_near_start() -> None:
    """context_message_id with anchor near start returns fewer before-messages, no error."""
    DIALOG_ID = 7002

    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    for mid in range(1, 6):
        _insert_message(conn, DIALOG_ID, mid, text=f"msg {mid}", sent_at=1700000000 + mid)
    server = make_server(conn)

    result = await server._list_messages({"dialog_id": DIALOG_ID, "context_message_id": 2, "context_size": 6})

    assert result["ok"] is True, f"Unexpected error: {result}"
    messages = result["data"]["messages"]
    ids = [m["message_id"] for m in messages]
    # half=3; before: ids <= 2 DESC LIMIT 4 → [2,1]; after: ids > 2 ASC LIMIT 3 → [3,4,5]
    assert ids == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_list_messages_context_window_not_synced_error() -> None:
    """context_message_id returns not_synced error for access_lost dialogs.

    Phase 999.1 (D-07): absent/not_synced/fragment statuses now trigger the
    fragment-fetch path instead of being rejected. Only 'access_lost' dialogs
    (where we lost access and cannot fetch) are rejected with not_synced.
    """
    DIALOG_ID = 7003

    conn = _make_db()
    # access_lost: we had access but lost it — cannot perform fragment fetch
    _insert_synced_dialog(conn, DIALOG_ID, status="access_lost")
    server = make_server(conn)

    result = await server._list_messages({"dialog_id": DIALOG_ID, "context_message_id": 10, "context_size": 4})

    assert result["ok"] is False
    assert result["error"] == "not_synced"


@pytest.mark.asyncio
async def test_list_messages_context_window_reactions_injected() -> None:
    """context_message_id path injects reactions_display from message_reactions table."""
    DIALOG_ID = 7004

    conn = _make_db()
    _insert_synced_dialog(conn, DIALOG_ID, status="synced")
    for mid in [10, 20, 30]:
        _insert_message(conn, DIALOG_ID, mid, text=f"msg {mid}", sent_at=1700000000 + mid)
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (DIALOG_ID, 20, "👍", 3),
    )
    conn.commit()
    server = make_server(conn)

    result = await server._list_messages({"dialog_id": DIALOG_ID, "context_message_id": 20, "context_size": 4})

    assert result["ok"] is True
    by_id = {m["message_id"]: m for m in result["data"]["messages"]}
    assert "👍" in by_id[20]["reactions_display"]
    assert by_id[10]["reactions_display"] == ""
    assert by_id[30]["reactions_display"] == ""


# ---------------------------------------------------------------------------
# Phase 35-01: _msg_to_dict — edit_date from Telethon message
# ---------------------------------------------------------------------------


def test_msg_to_dict_edit_date() -> None:
    """_msg_to_dict includes edit_date as unix timestamp from msg.edit_date."""
    from datetime import datetime

    from mcp_telegram.daemon_api import DaemonAPIServer

    mock_msg = MagicMock()
    mock_msg.id = 300
    mock_msg.date = MagicMock()
    mock_msg.date.timestamp.return_value = 1700000000.0
    mock_msg.message = "edited message"
    mock_msg.sender_id = 42
    mock_msg.sender = MagicMock()
    mock_msg.sender.first_name = "Alice"
    mock_msg.media = None
    mock_msg.reply_to = None
    mock_msg.reactions = None
    edit_dt = datetime(2023, 11, 14, 12, 0, 0, tzinfo=UTC)
    mock_msg.edit_date = edit_dt

    result = DaemonAPIServer._msg_to_dict(mock_msg)

    assert "edit_date" in result, "edit_date key must be present in _msg_to_dict output"
    assert result["edit_date"] == int(edit_dt.timestamp())


def test_msg_to_dict_no_edit_date_is_none() -> None:
    """_msg_to_dict returns edit_date=None when msg.edit_date is None."""
    from mcp_telegram.daemon_api import DaemonAPIServer

    mock_msg = MagicMock()
    mock_msg.id = 301
    mock_msg.date = MagicMock()
    mock_msg.date.timestamp.return_value = 1700000000.0
    mock_msg.message = "unedited"
    mock_msg.sender_id = None
    mock_msg.sender = None
    mock_msg.media = None
    mock_msg.reply_to = None
    mock_msg.reactions = None
    mock_msg.edit_date = None

    result = DaemonAPIServer._msg_to_dict(mock_msg)

    assert result.get("edit_date") is None


# ---------------------------------------------------------------------------
# _decode_history_navigation error paths (M-12)
# ---------------------------------------------------------------------------


def test_decode_nav_returns_tuple_on_no_navigation() -> None:
    """No navigation → returns (None, direction) tuple."""
    result = DaemonAPIServer._decode_history_navigation(None, 123, "newest")
    assert result == (None, "newest")


def test_decode_nav_sentinel_newest() -> None:
    """navigation="newest" → returns (None, "newest")."""
    result = DaemonAPIServer._decode_history_navigation("newest", 123, "oldest")
    assert result == (None, "oldest")


def test_decode_nav_sentinel_oldest() -> None:
    """navigation="oldest" → overrides direction."""
    result = DaemonAPIServer._decode_history_navigation("oldest", 123, "newest")
    assert result == (None, "oldest")


def test_decode_nav_invalid_token_returns_error_dict() -> None:
    """Garbage navigation token → error dict."""
    result = DaemonAPIServer._decode_history_navigation("not-a-valid-token", 123, "newest")
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["error"] == "invalid_navigation"


def test_decode_nav_wrong_dialog_returns_error_dict() -> None:
    """Navigation token for different dialog → error dict."""
    from mcp_telegram.pagination import HistoryDirection, encode_history_navigation

    token = encode_history_navigation(100, dialog_id=999, direction=HistoryDirection.NEWEST)
    result = DaemonAPIServer._decode_history_navigation(token, 123, "newest")
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert "999" in result["message"]


def test_decode_nav_valid_token_returns_anchor() -> None:
    """Valid history token → returns (anchor_msg_id, direction)."""
    from mcp_telegram.pagination import HistoryDirection, encode_history_navigation

    token = encode_history_navigation(42, dialog_id=123, direction=HistoryDirection.NEWEST)
    result = DaemonAPIServer._decode_history_navigation(token, 123, "oldest")
    assert isinstance(result, tuple)
    anchor, direction = result
    assert anchor == 42
    assert direction == "newest"


def test_decode_nav_search_token_returns_error() -> None:
    """Search navigation token used in history context → error."""
    from mcp_telegram.pagination import encode_search_navigation

    token = encode_search_navigation(20, dialog_id=123, query="test")
    result = DaemonAPIServer._decode_history_navigation(token, 123, "newest")
    assert isinstance(result, dict)
    assert result["error"] == "invalid_navigation"
    assert "search" in result["message"]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# _compute_sync_coverage unit tests (Plan 36-02, Task 1)
# ---------------------------------------------------------------------------


def test_compute_sync_coverage_normal():
    from mcp_telegram.daemon_api import _compute_sync_coverage

    assert _compute_sync_coverage(1000, 800) == 80


def test_compute_sync_coverage_complete():
    from mcp_telegram.daemon_api import _compute_sync_coverage

    assert _compute_sync_coverage(1000, 1000) == 100


def test_compute_sync_coverage_clamped_over_100():
    from mcp_telegram.daemon_api import _compute_sync_coverage

    assert _compute_sync_coverage(100, 150) == 100


def test_compute_sync_coverage_null_total():
    from mcp_telegram.daemon_api import _compute_sync_coverage

    assert _compute_sync_coverage(None, 500) is None


def test_compute_sync_coverage_zero_total():
    from mcp_telegram.daemon_api import _compute_sync_coverage

    assert _compute_sync_coverage(0, 0) == 100


# ---------------------------------------------------------------------------
# get_sync_status enrichment tests (Plan 36-02, Task 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sync_status_includes_coverage():
    """get_sync_status returns sync_coverage_pct and access_lost_at."""
    conn = _make_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages) VALUES (?, 'synced', 1000)",
        (-100001,),
    )
    for i in range(800):
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, is_deleted) VALUES (?, ?, 1000, 'msg', 0)",
            (-100001, i + 1),
        )
    conn.commit()
    server = make_server(conn)
    result = await server._get_sync_status({"dialog_id": -100001})
    assert result["ok"] is True
    assert result["data"]["sync_coverage_pct"] == 80
    assert result["data"]["access_lost_at"] is None


@pytest.mark.asyncio
async def test_get_sync_status_access_lost_with_coverage():
    """access_lost dialog shows frozen coverage with access_lost_at."""
    conn = _make_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages, access_lost_at) "
        "VALUES (?, 'access_lost', 500, 1700000000)",
        (-100002,),
    )
    for i in range(400):
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, is_deleted) VALUES (?, ?, 1000, 'msg', 0)",
            (-100002, i + 1),
        )
    conn.commit()
    server = make_server(conn)
    result = await server._get_sync_status({"dialog_id": -100002})
    assert result["data"]["sync_coverage_pct"] == 80
    assert result["data"]["access_lost_at"] == 1700000000


@pytest.mark.asyncio
async def test_get_sync_status_access_lost_null_total_has_archived_count():
    """access_lost + total_messages=NULL returns archived_message_count with local count."""
    conn = _make_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages, access_lost_at) "
        "VALUES (?, 'access_lost', NULL, 1700000000)",
        (-100009,),
    )
    for i in range(150):
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, is_deleted) VALUES (?, ?, 1000, 'msg', 0)",
            (-100009, i + 1),
        )
    conn.commit()
    server = make_server(conn)
    result = await server._get_sync_status({"dialog_id": -100009})
    assert result["data"]["sync_coverage_pct"] is None
    assert result["data"]["access_lost_at"] == 1700000000
    assert result["data"]["archived_message_count"] == 150


# ---------------------------------------------------------------------------
# list_dialogs enrichment tests (Plan 36-02, Task 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dialogs_includes_coverage():
    """list_dialogs includes sync_coverage_pct and access_lost_at per dialog."""
    conn = _make_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages) VALUES (?, 'synced', 100)",
        (5001,),
    )
    for i in range(50):
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, is_deleted) VALUES (?, ?, 1000, 'msg', 0)",
            (5001, i + 1),
        )
    conn.commit()

    mock_client = MagicMock()
    mock_dialog = MagicMock()
    mock_dialog.id = 5001
    mock_dialog.name = "Test Dialog"
    mock_dialog.entity = MagicMock()
    mock_dialog.entity.first_name = "Test"
    mock_dialog.entity.bot = False
    mock_dialog.entity.participants_count = None
    mock_dialog.entity.date = None
    mock_dialog.date = MagicMock()
    mock_dialog.date.timestamp.return_value = 1700000000
    mock_dialog.unread_count = 0

    async def _iter_dialogs(**kwargs):
        yield mock_dialog

    mock_client.iter_dialogs = _iter_dialogs

    server = make_server(conn, mock_client)
    result = await server._list_dialogs({})
    assert result["ok"] is True
    dialogs = result["data"]["dialogs"]
    assert len(dialogs) == 1
    assert dialogs[0]["sync_coverage_pct"] == 50
    assert dialogs[0]["access_lost_at"] is None
    assert dialogs[0]["type"] == "User"


@pytest.mark.asyncio
async def test_list_dialogs_classifies_forum() -> None:
    """list_dialogs returns type='Forum' for a megagroup with forum flag."""
    from telethon.tl.types import Channel  # type: ignore[import-untyped]

    conn = _make_db()

    entity = MagicMock()
    entity.__class__ = Channel
    entity.megagroup = True
    entity.forum = True
    entity.broadcast = False
    entity.participants_count = None
    entity.date = None

    mock_dialog = MagicMock()
    mock_dialog.id = 6001
    mock_dialog.name = "Forum Group"
    mock_dialog.entity = entity
    mock_dialog.date = MagicMock()
    mock_dialog.date.timestamp.return_value = 1700000000
    mock_dialog.unread_count = 0

    mock_client = MagicMock()

    async def _iter_dialogs(**kwargs):
        yield mock_dialog

    mock_client.iter_dialogs = _iter_dialogs

    server = make_server(conn, mock_client)
    result = await server._list_dialogs({})
    assert result["ok"] is True
    dialogs = result["data"]["dialogs"]
    assert len(dialogs) == 1
    assert dialogs[0]["type"] == "Forum"


# ---------------------------------------------------------------------------
# list_messages access_lost routing tests (Plan 36-02, Task 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_access_lost_returns_archived():
    """list_messages on access_lost dialog reads from sync.db with dialog_access=archived."""
    conn = _make_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at, total_messages, "
        "last_synced_at, last_event_at) "
        "VALUES (?, 'access_lost', 1700000000, 500, 1699990000, 1699999000)",
        (6001,),
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text, sender_id, "
        "sender_first_name, is_deleted) VALUES (?, 1, 1000, 'archived msg', 42, 'Alice', 0)",
        (6001,),
    )
    conn.commit()

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": 6001, "limit": 10})
    assert result["ok"] is True
    assert result["data"]["dialog_access"] == "archived"
    assert result["data"]["source"] == "sync_db"
    assert result["data"]["access_lost_at"] == 1700000000
    assert result["data"]["last_synced_at"] == 1699990000
    assert result["data"]["last_event_at"] == 1699999000
    assert len(result["data"]["messages"]) == 1


@pytest.mark.asyncio
async def test_list_messages_access_lost_null_total_has_archived_count():
    """access_lost + total_messages=NULL in list_messages returns archived_message_count."""
    conn = _make_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at, total_messages) "
        "VALUES (?, 'access_lost', 1700000000, NULL)",
        (6010,),
    )
    for i in range(75):
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, sender_id, "
            "sender_first_name, is_deleted) VALUES (?, ?, 1000, 'msg', 42, 'Alice', 0)",
            (6010, i + 1),
        )
    conn.commit()

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": 6010, "limit": 10})
    assert result["ok"] is True
    assert result["data"]["dialog_access"] == "archived"
    assert result["data"]["sync_coverage_pct"] is None
    assert result["data"]["archived_message_count"] == 75


@pytest.mark.asyncio
async def test_list_messages_access_lost_sync_coverage_pct() -> None:
    """access_lost dialog with known total_messages returns correct sync_coverage_pct."""
    conn = _make_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at, total_messages, "
        "last_synced_at, last_event_at) VALUES (?, 'access_lost', 1700000000, 100, 1699990000, 1699999000)",
        (6020,),
    )
    for i in range(25):
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, sender_id, "
            "sender_first_name, is_deleted) VALUES (?, ?, 1000, 'msg', 42, 'Alice', 0)",
            (6020, i + 1),
        )
    conn.commit()

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": 6020, "limit": 10})

    assert result["ok"] is True
    assert result["data"]["dialog_access"] == "archived"
    assert result["data"]["access_lost_at"] == 1700000000
    assert result["data"]["last_synced_at"] == 1699990000
    assert result["data"]["last_event_at"] == 1699999000
    assert result["data"]["sync_coverage_pct"] == 25  # 25/100


@pytest.mark.asyncio
async def test_list_messages_synced_returns_live():
    """list_messages on synced dialog returns dialog_access=live."""
    conn = _make_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (6002,),
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text, sender_id, "
        "sender_first_name, is_deleted) VALUES (?, 1, 1000, 'live msg', 42, 'Alice', 0)",
        (6002,),
    )
    conn.commit()

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": 6002, "limit": 10})
    assert result["ok"] is True
    assert result["data"]["dialog_access"] == "live"


# ---------------------------------------------------------------------------
# search_messages access metadata tests (Plan 36-02, Task 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_messages_access_lost_returns_full_metadata():
    """search_messages on access_lost dialog returns dialog_access=archived with full metadata."""
    conn = _make_db(with_fts=True)
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at, total_messages, "
        "last_synced_at, last_event_at) "
        "VALUES (?, 'access_lost', 1700000000, 500, 1699990000, 1699999000)",
        (-100003,),
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text, sender_id, "
        "sender_first_name, is_deleted) VALUES (?, 1, 1000, 'test content', 42, 'Alice', 0)",
        (-100003,),
    )
    from mcp_telegram.fts import INSERT_FTS_SQL, stem_text

    conn.execute(INSERT_FTS_SQL, (-100003, 1, stem_text("test content")))
    conn.commit()

    server = make_server(conn)
    result = await server._search_messages({"dialog_id": -100003, "query": "test"})
    assert result["ok"] is True
    assert result["data"]["dialog_access"] == "archived"
    assert result["data"]["access_lost_at"] == 1700000000
    assert result["data"]["last_synced_at"] == 1699990000
    assert result["data"]["last_event_at"] == 1699999000
    assert result["data"]["sync_coverage_pct"] is not None


@pytest.mark.asyncio
async def test_search_messages_access_lost_null_total_has_archived_count():
    """search_messages on access_lost + total_messages=NULL returns archived_message_count."""
    conn = _make_db(with_fts=True)
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at, total_messages) "
        "VALUES (?, 'access_lost', 1700000000, NULL)",
        (-100011,),
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text, sender_id, "
        "sender_first_name, is_deleted) VALUES (?, 1, 1000, 'test content', 42, 'Alice', 0)",
        (-100011,),
    )
    from mcp_telegram.fts import INSERT_FTS_SQL, stem_text

    conn.execute(INSERT_FTS_SQL, (-100011, 1, stem_text("test content")))
    conn.commit()

    server = make_server(conn)
    result = await server._search_messages({"dialog_id": -100011, "query": "test"})
    assert result["ok"] is True
    assert result["data"]["dialog_access"] == "archived"
    assert result["data"]["sync_coverage_pct"] is None
    assert result["data"]["archived_message_count"] == 1


@pytest.mark.asyncio
async def test_search_messages_global_omits_dialog_access():
    """Global search omits dialog_access since results span multiple dialogs."""
    conn = _make_db(with_fts=True, with_entities=True)
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (-100004,),
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text, sender_id, "
        "sender_first_name, is_deleted) VALUES (?, 1, 1000, 'global test', 42, 'Alice', 0)",
        (-100004,),
    )
    from mcp_telegram.fts import INSERT_FTS_SQL, stem_text

    conn.execute(INSERT_FTS_SQL, (-100004, 1, stem_text("global test")))
    conn.commit()

    server = make_server(conn)
    result = await server._search_messages({"query": "global"})
    assert result["ok"] is True
    assert "dialog_access" not in result["data"]


# ---------------------------------------------------------------------------
# _fetch_reaction_counts tests (Plan 37-02, Task 3)
# ---------------------------------------------------------------------------


def test_fetch_reaction_counts_empty_list() -> None:
    """_fetch_reaction_counts returns {} for empty message_ids without hitting DB."""
    from mcp_telegram.daemon_api import _fetch_reaction_counts

    conn = _make_db()
    result = _fetch_reaction_counts(conn, dialog_id=1, message_ids=[])
    assert result == {}


def test_fetch_reaction_counts_returns_grouped_by_message() -> None:
    """_fetch_reaction_counts groups reactions by message_id in DESC count order."""
    from mcp_telegram.daemon_api import _fetch_reaction_counts

    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 10)
    _insert_message(conn, 1, 20)
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 10, "👍", 3),
    )
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 10, "❤️", 1),
    )
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 20, "👍", 1),
    )
    conn.commit()

    result = _fetch_reaction_counts(conn, dialog_id=1, message_ids=[10, 20])

    assert 10 in result
    assert 20 in result
    # msg 10: thumbsup(3) before heart(1) by count DESC
    assert result[10][0] == ("👍", 3)
    assert result[10][1] == ("❤️", 1)
    assert result[20] == [("👍", 1)]


def test_fetch_reaction_counts_missing_messages_omitted() -> None:
    """_fetch_reaction_counts omits message_ids that have no reactions."""
    from mcp_telegram.daemon_api import _fetch_reaction_counts

    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 10)
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 10, "👍", 2),
    )
    conn.commit()

    result = _fetch_reaction_counts(conn, dialog_id=1, message_ids=[10, 99])

    assert 10 in result
    assert 99 not in result


# ---------------------------------------------------------------------------
# list_messages reactions injection tests (Plan 37-02, Task 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_from_db_includes_reactions_display() -> None:
    """list_messages injects reactions_display from message_reactions table."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 100, text="Hello")
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 100, "👍", 3),
    )
    conn.commit()

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": 1, "limit": 10})

    assert result["ok"] is True
    messages = result["data"]["messages"]
    assert len(messages) == 1
    msg = messages[0]
    assert "reactions_display" in msg
    assert "reactions" not in msg  # bare reactions key must not exist
    assert "👍" in msg["reactions_display"]
    assert "\u00d7" in msg["reactions_display"]  # × (U+00D7)


@pytest.mark.asyncio
async def test_list_messages_from_db_no_reactions_empty_display() -> None:
    """list_messages sets reactions_display='' for messages with no reactions."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 101, text="No reactions")
    conn.commit()

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": 1, "limit": 10})

    assert result["ok"] is True
    messages = result["data"]["messages"]
    assert messages[0]["reactions_display"] == ""


# ---------------------------------------------------------------------------
# search_messages reactions injection tests (Plan 37-02, Task 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoped_search_includes_reactions_display() -> None:
    """Scoped search injects reactions_display from message_reactions."""
    from mcp_telegram.fts import INSERT_FTS_SQL, stem_text

    conn = _make_db(with_fts=True)
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 200, text="fire message")
    conn.execute(INSERT_FTS_SQL, (1, 200, stem_text("fire message")))
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 200, "🔥", 5),
    )
    conn.commit()

    server = make_server(conn)
    result = await server._search_messages({"query": "fire", "dialog_id": 1})

    assert result["ok"] is True
    messages = result["data"]["messages"]
    assert len(messages) == 1
    assert "reactions_display" in messages[0]
    assert "🔥" in messages[0]["reactions_display"]


@pytest.mark.asyncio
async def test_global_search_returns_empty_reactions_display() -> None:
    """Global search returns reactions_display='' (intentional, cross-dialog result set)."""
    from mcp_telegram.fts import INSERT_FTS_SQL, stem_text

    conn = _make_db(with_fts=True, with_entities=True)
    _insert_synced_dialog(conn, 5)
    _insert_message(conn, 5, 300, text="global search test")
    conn.execute(INSERT_FTS_SQL, (5, 300, stem_text("global search test")))
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (5, 300, "❤️", 2),
    )
    conn.commit()

    server = make_server(conn)
    # No dialog_id => global mode
    result = await server._search_messages({"query": "global"})

    assert result["ok"] is True
    messages = result["data"]["messages"]
    assert len(messages) == 1
    # Global search: reactions_display must be present but empty
    assert "reactions_display" in messages[0]
    assert messages[0]["reactions_display"] == ""


# ---------------------------------------------------------------------------
# _msg_to_dict reactions_display tests (Plan 37-02, Task 3)
# ---------------------------------------------------------------------------


def test_msg_to_dict_formats_reactions_display() -> None:
    """_msg_to_dict extracts reactions from Telethon message and formats display string."""
    from mcp_telegram.daemon_api import DaemonAPIServer

    mock_reaction = MagicMock()
    mock_reaction.emoticon = "👍"
    mock_rc = MagicMock()
    mock_rc.reaction = mock_reaction
    mock_rc.count = 3

    mock_reactions_obj = MagicMock()
    mock_reactions_obj.results = [mock_rc]

    mock_msg = MagicMock()
    mock_msg.id = 999
    mock_msg.date = MagicMock()
    mock_msg.date.timestamp.return_value = 1700000000.0
    mock_msg.message = "hello"
    mock_msg.sender_id = 42
    mock_msg.sender = None
    mock_msg.media = None
    mock_msg.reply_to = None
    mock_msg.reply_to_msg_id = None
    mock_msg.forum_topic_id = None
    mock_msg.reactions = mock_reactions_obj
    mock_msg.edit_date = None

    result = DaemonAPIServer._msg_to_dict(mock_msg)

    assert "reactions_display" in result
    assert "reactions" not in result  # bare 'reactions' key must not exist
    assert "👍" in result["reactions_display"]
    assert "\u00d7" in result["reactions_display"]  # × U+00D7


def test_msg_to_dict_no_reactions_returns_empty_display() -> None:
    """_msg_to_dict returns reactions_display='' when msg.reactions is None."""
    from mcp_telegram.daemon_api import DaemonAPIServer

    mock_msg = MagicMock()
    mock_msg.id = 1
    mock_msg.date = MagicMock()
    mock_msg.date.timestamp.return_value = 1700000000.0
    mock_msg.message = "no reactions"
    mock_msg.sender_id = None
    mock_msg.sender = None
    mock_msg.media = None
    mock_msg.reply_to = None
    mock_msg.reply_to_msg_id = None
    mock_msg.forum_topic_id = None
    mock_msg.reactions = None
    mock_msg.edit_date = None

    result = DaemonAPIServer._msg_to_dict(mock_msg)

    assert result["reactions_display"] == ""
    assert "reactions" not in result


@pytest.mark.asyncio
async def test_no_remaining_reactions_key_in_responses() -> None:
    """Integration: list_messages response dict has only reactions_display, no bare 'reactions'."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 50, text="clean response test")
    conn.commit()

    server = make_server(conn)
    result = await server._list_messages({"dialog_id": 1, "limit": 10})

    assert result["ok"] is True
    for msg in result["data"]["messages"]:
        assert "reactions" not in msg, f"bare 'reactions' key found: {msg}"
        assert "reactions_display" in msg


# ---------------------------------------------------------------------------
# format_reaction_counts and _format_reactions tests (Plan 37-02, Task 3)
# ---------------------------------------------------------------------------


def test_format_reactions_with_preformatted_display() -> None:
    """_format_reactions returns reactions_display from ReadMessage unchanged."""
    from mcp_telegram.formatter import _format_reactions
    from mcp_telegram.models import ReadMessage

    msg = ReadMessage(message_id=1, sent_at=0, dialog_id=0, reactions_display="[👍×3 ❤️×1]")
    assert _format_reactions(msg) == "[👍×3 ❤️×1]"


def test_format_reactions_with_preformatted_empty_string() -> None:
    """_format_reactions returns '' when reactions_display is empty."""
    from mcp_telegram.formatter import _format_reactions
    from mcp_telegram.models import ReadMessage

    msg = ReadMessage(message_id=1, sent_at=0, dialog_id=0, reactions_display="")
    assert _format_reactions(msg) == ""


def test_format_reactions_with_none_reactions() -> None:
    """_format_reactions returns '' when reactions_display is empty (default)."""
    from mcp_telegram.formatter import _format_reactions
    from mcp_telegram.models import ReadMessage

    msg = ReadMessage(message_id=1, sent_at=0, dialog_id=0)
    assert _format_reactions(msg) == ""


def test_format_reaction_counts_emoji_glyphs_with_multiplication_sign() -> None:
    """format_reaction_counts uses actual emoji glyphs with × (U+00D7), shows ×1 for count=1."""
    from mcp_telegram.formatter import format_reaction_counts

    result = format_reaction_counts([("👍", 3), ("❤️", 1)])

    # Must start/end with brackets
    assert result.startswith("[")
    assert result.endswith("]")
    # Must contain × (U+00D7), NOT lowercase x
    assert "\u00d7" in result
    assert "👍\u00d73" in result
    # count=1 must be shown (×1 not omitted)
    assert "❤️\u00d71" in result


def test_format_reaction_counts_single_reaction_shows_count() -> None:
    """format_reaction_counts shows ×1 for count=1, never omits it."""
    from mcp_telegram.formatter import format_reaction_counts

    result = format_reaction_counts([("❤️", 1)])

    assert result == "[❤️\u00d71]"


def test_format_reaction_counts_empty_returns_empty() -> None:
    """format_reaction_counts returns '' for empty input."""
    from mcp_telegram.formatter import format_reaction_counts

    assert format_reaction_counts([]) == ""


def test_format_reaction_counts_sort_order_with_tied_counts() -> None:
    """Tied counts are broken by emoji Unicode code point (Priority Action #5)."""
    from mcp_telegram.formatter import format_reaction_counts

    # fire (🔥 U+1F525) and thumbsup (👍 U+1F44D): both count=3
    # heart (❤️ U+2764): count=1
    # Unicode order: 👍 (U+1F44D) < 🔥 (U+1F525), so 👍 comes first in tie
    result = format_reaction_counts([("❤️", 1), ("👍", 3), ("🔥", 3)])

    inner = result[1:-1]  # strip brackets
    parts = inner.split(" ")
    assert len(parts) == 3
    # count=3 entries come first
    assert "\u00d73" in parts[0]
    assert "\u00d73" in parts[1]
    # count=1 entry is last
    assert "\u00d71" in parts[2]
    # Within count=3 tie: 👍 (lower code point) before 🔥
    assert "👍" in parts[0]
    assert "🔥" in parts[1]


# ---------------------------------------------------------------------------
# Analytics query tests: SCHEMA-02 (entities), SCHEMA-03 (forwards)
# (Plan 37-02, Task 3 -- proving Priority Action #1 read paths work)
# ---------------------------------------------------------------------------


def _make_db_with_normalized_tables() -> sqlite3.Connection:
    """Return in-memory DB with messages + message_entities + message_forwards."""
    conn = _make_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_entities (
            dialog_id   INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            offset      INTEGER NOT NULL,
            length      INTEGER NOT NULL,
            type        TEXT NOT NULL,
            value       TEXT,
            PRIMARY KEY (dialog_id, message_id, offset, length, type)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_forwards (
            dialog_id           INTEGER NOT NULL,
            message_id          INTEGER NOT NULL,
            fwd_from_peer_id    INTEGER,
            fwd_from_name       TEXT,
            fwd_date            INTEGER,
            PRIMARY KEY (dialog_id, message_id)
        ) WITHOUT ROWID
        """
    )
    conn.commit()
    return conn


def test_entity_read_path_mention_query() -> None:
    """Analytics: entity mention/hashtag query returns populated value column (Priority Action #1)."""
    conn = _make_db_with_normalized_tables()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 1)
    _insert_message(conn, 1, 2)
    conn.execute(
        "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, 0, 6, "mention", "@alice"),
    )
    conn.execute(
        "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 2, 0, 7, "hashtag", "#python"),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT type, value, COUNT(*) as cnt FROM message_entities "
        "WHERE dialog_id = ? GROUP BY type, value ORDER BY cnt DESC",
        (1,),
    ).fetchall()

    assert len(rows) == 2
    values = {row[1] for row in rows}
    assert "@alice" in values
    assert "#python" in values
    # All values populated (not NULL) -- proves Priority Action #1
    for row in rows:
        assert row[1] is not None


def test_entity_read_path_hashtag_frequency() -> None:
    """Analytics: hashtag frequency query works with populated value column (Priority Action #1)."""
    conn = _make_db_with_normalized_tables()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 1)
    _insert_message(conn, 1, 2)
    _insert_message(conn, 1, 3)
    conn.execute(
        "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, 0, 7, "hashtag", "#python"),
    )
    conn.execute(
        "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 2, 0, 5, "hashtag", "#rust"),
    )
    conn.execute(
        "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 3, 0, 7, "hashtag", "#python"),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT me.value, COUNT(*) as cnt FROM message_entities me "
        "WHERE me.dialog_id = ? AND me.type = 'hashtag' "
        "GROUP BY me.value ORDER BY cnt DESC",
        (1,),
    ).fetchall()

    assert rows[0] == ("#python", 2)
    assert rows[1] == ("#rust", 1)


def test_forward_read_path_source_ranking() -> None:
    """Analytics: forward source ranking query works (SCHEMA-03 read path)."""
    conn = _make_db_with_normalized_tables()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 1)
    _insert_message(conn, 1, 2)
    _insert_message(conn, 1, 3)
    conn.execute(
        "INSERT INTO message_forwards (dialog_id, message_id, fwd_from_peer_id, fwd_from_name) VALUES (?, ?, ?, ?)",
        (1, 1, 100, "Channel A"),
    )
    conn.execute(
        "INSERT INTO message_forwards (dialog_id, message_id, fwd_from_peer_id, fwd_from_name) VALUES (?, ?, ?, ?)",
        (1, 2, 100, "Channel A"),
    )
    conn.execute(
        "INSERT INTO message_forwards (dialog_id, message_id, fwd_from_peer_id, fwd_from_name) VALUES (?, ?, ?, ?)",
        (1, 3, 200, "Channel B"),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT fwd_from_peer_id, fwd_from_name, COUNT(*) as cnt "
        "FROM message_forwards WHERE dialog_id = ? AND fwd_from_peer_id IS NOT NULL "
        "GROUP BY fwd_from_peer_id ORDER BY cnt DESC",
        (1,),
    ).fetchall()

    assert rows[0][0] == 100
    assert rows[0][1] == "Channel A"
    assert rows[0][2] == 2
    assert rows[1][0] == 200


def test_forward_read_path_includes_private_forwards() -> None:
    """Analytics: forwards with NULL peer_id return fwd_from_name (private/hidden sender)."""
    conn = _make_db_with_normalized_tables()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 1)
    conn.execute(
        "INSERT INTO message_forwards (dialog_id, message_id, fwd_from_peer_id, fwd_from_name) VALUES (?, ?, ?, ?)",
        (1, 1, None, "Hidden User"),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT fwd_from_peer_id, fwd_from_name FROM message_forwards WHERE dialog_id = ?",
        (1,),
    ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] is None  # peer_id is NULL
    assert rows[0][1] == "Hidden User"  # display name is populated


def test_reaction_analytics_most_reacted_messages() -> None:
    """Analytics: SUM-based reaction ranking works without Python-level JSON parsing (phase goal)."""
    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 1)
    _insert_message(conn, 1, 2)
    _insert_message(conn, 1, 3)
    # msg 1: 3 thumbsup + 2 heart = 5 total
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 1, "👍", 3),
    )
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 1, "❤️", 2),
    )
    # msg 2: 1 heart = 1 total
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 2, "❤️", 1),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT message_id, SUM(count) as total_reactions "
        "FROM message_reactions WHERE dialog_id = ? "
        "GROUP BY message_id ORDER BY total_reactions DESC LIMIT 5",
        (1,),
    ).fetchall()

    assert len(rows) == 2
    assert rows[0][0] == 1  # msg 1 has highest reactions
    assert rows[0][1] == 5
    assert rows[1][0] == 2
    assert rows[1][1] == 1


# Deferred cleanup items from Phase 37 (concrete removal criteria):
# - Remove _PreformattedReactions shim in _adapters.py
#   Criterion: when reaction_names_map is removed from MessageLike protocol in models.py
# - Remove reaction_names_map infrastructure in models.py/formatter.py
#   Criterion: when on-demand Telethon path is fully migrated to daemon-only reads


# ---------------------------------------------------------------------------
# _get_dialog_stats (260416-frw)
# ---------------------------------------------------------------------------


def _make_db_for_dialog_stats() -> sqlite3.Connection:
    """Return in-memory DB with all tables needed by _get_dialog_stats."""
    conn = _make_db_with_normalized_tables()
    return conn


@pytest.mark.asyncio
async def test_get_dialog_stats_synced_returns_aggregated() -> None:
    """_get_dialog_stats with synced dialog returns aggregated reactions/mentions/hashtags/forwards."""
    conn = _make_db_for_dialog_stats()
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 1)
    _insert_message(conn, 1, 2)
    _insert_message(conn, 1, 3)

    # Reactions: thumbsup appears on msg1 (3) and msg2 (1) = total 4; heart on msg1 = 2
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 1, "👍", 3),
    )
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 2, "👍", 1),
    )
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 1, "❤️", 2),
    )

    # Mentions: @alice once
    conn.execute(
        "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, 0, 6, "mention", "@alice"),
    )

    # Hashtags: #python twice, #rust once
    conn.execute(
        "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, 10, 7, "hashtag", "#python"),
    )
    conn.execute(
        "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 2, 0, 7, "hashtag", "#python"),
    )
    conn.execute(
        "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 3, 0, 5, "hashtag", "#rust"),
    )

    # Forwards: Channel A twice, Channel B once
    conn.execute(
        "INSERT INTO message_forwards (dialog_id, message_id, fwd_from_peer_id, fwd_from_name) VALUES (?, ?, ?, ?)",
        (1, 1, 100, "Channel A"),
    )
    conn.execute(
        "INSERT INTO message_forwards (dialog_id, message_id, fwd_from_peer_id, fwd_from_name) VALUES (?, ?, ?, ?)",
        (1, 2, 100, "Channel A"),
    )
    conn.execute(
        "INSERT INTO message_forwards (dialog_id, message_id, fwd_from_peer_id, fwd_from_name) VALUES (?, ?, ?, ?)",
        (1, 3, 200, "Channel B"),
    )
    conn.commit()

    server = make_server(conn)
    result = await server._get_dialog_stats({"dialog_id": 1, "limit": 5})

    assert result["ok"] is True, f"Expected ok=True, got {result}"
    data = result["data"]
    assert data["dialog_id"] == 1

    # Reactions sorted by total DESC: 👍 total=4, ❤️ total=2
    reactions = data["top_reactions"]
    assert len(reactions) == 2
    assert reactions[0]["emoji"] == "👍"
    assert reactions[0]["count"] == 4
    assert reactions[1]["emoji"] == "❤️"

    # Mentions: @alice count=1
    mentions = data["top_mentions"]
    assert len(mentions) == 1
    assert mentions[0]["value"] == "@alice"
    assert mentions[0]["count"] == 1

    # Hashtags: #python first (count=2)
    hashtags = data["top_hashtags"]
    assert hashtags[0]["value"] == "#python"
    assert hashtags[0]["count"] == 2

    # Forwards: Channel A first (count=2)
    forwards = data["top_forwards"]
    assert forwards[0]["peer_id"] == 100
    assert forwards[0]["name"] == "Channel A"
    assert forwards[0]["count"] == 2


@pytest.mark.asyncio
async def test_get_dialog_stats_not_synced_returns_error() -> None:
    """_get_dialog_stats with no synced row returns not_synced error referencing MarkDialogForSync."""
    server = make_server()
    result = await server._get_dialog_stats({"dialog_id": 999})

    assert result["ok"] is False
    assert result["error"] == "not_synced"
    assert "MarkDialogForSync" in result["message"]


@pytest.mark.asyncio
async def test_get_dialog_stats_empty_synced() -> None:
    """_get_dialog_stats with synced but empty dialog returns ok=True with all lists empty."""
    conn = _make_db_for_dialog_stats()
    _insert_synced_dialog(conn, 1, status="synced")
    server = make_server(conn)

    result = await server._get_dialog_stats({"dialog_id": 1})

    assert result["ok"] is True
    data = result["data"]
    assert data["top_reactions"] == []
    assert data["top_mentions"] == []
    assert data["top_hashtags"] == []
    assert data["top_forwards"] == []


@pytest.mark.asyncio
async def test_get_dialog_stats_access_lost_allowed() -> None:
    """_get_dialog_stats allows access_lost status — archived analytics remain useful."""
    conn = _make_db_for_dialog_stats()
    _insert_synced_dialog(conn, 1, status="access_lost")
    _insert_message(conn, 1, 1)
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (1, 1, "👍", 5),
    )
    conn.commit()

    server = make_server(conn)
    result = await server._get_dialog_stats({"dialog_id": 1})

    assert result["ok"] is True
    assert result["data"]["top_reactions"][0]["emoji"] == "👍"


@pytest.mark.asyncio
async def test_get_dialog_stats_resolves_fuzzy_dialog_name() -> None:
    """_get_dialog_stats resolves fuzzy dialog name to dialog_id via get_entity."""
    conn = _make_db_for_dialog_stats()
    _insert_synced_dialog(conn, 1, status="synced")

    entity = MagicMock()
    entity.id = 1
    client = MagicMock()
    client.get_entity = AsyncMock(return_value=entity)

    server = make_server(conn, client)
    result = await server._get_dialog_stats({"dialog": "Chat Foo"})

    assert result["ok"] is True
    assert result["data"]["dialog_id"] == 1
    client.get_entity.assert_called_once_with("Chat Foo")


# - Simplify _format_reactions to only handle count-only display path
#   Criterion: same as above


# ---------------------------------------------------------------------------
# Phase 39-01: entity-as-SoT for sender rendering
# Tests for LEFT JOIN entities + COALESCE in all five SELECT constants,
# structured log counter, and related invariants.
# ---------------------------------------------------------------------------


def _seed_entity_p39(conn: sqlite3.Connection, entity_id: int, name: str, entity_type: str = "User") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO entities "
        "(id, type, name, username, name_normalized, updated_at) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (entity_id, entity_type, name, name.lower(), 0),
    )


def _seed_message_p39(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    sender_id: int | None,
    sender_first_name: str | None,
    text: str = "hi",
    out: int = 0,
    is_service: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO messages "
        "(dialog_id, message_id, sent_at, text, sender_id, sender_first_name, is_deleted, out, is_service) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (dialog_id, message_id, 1_700_000_000 + message_id, text, sender_id, sender_first_name, out, is_service),
    )


def _seed_synced_dialog_p39(conn: sqlite3.Connection, dialog_id: int, read_inbox_max_id: int | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', ?)",
        (dialog_id, read_inbox_max_id),
    )


# --- Task 1: JOIN + COALESCE behavioral tests ---


@pytest.mark.asyncio
async def test_list_messages_uses_entity_name_when_sender_first_name_is_null() -> None:
    """DM row with sender_first_name=NULL but entities row yields entities.name."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=555)
    _seed_entity_p39(conn, entity_id=123, name="Konstantin")
    _seed_message_p39(conn, dialog_id=555, message_id=1, sender_id=123, sender_first_name=None)
    conn.commit()
    server = make_server(conn)
    resp = await server._list_messages({"dialog_id": 555, "limit": 10})
    assert resp["ok"] is True
    assert resp["data"]["messages"][0]["sender_first_name"] == "Konstantin"


@pytest.mark.asyncio
async def test_list_messages_falls_back_to_denormalized_column_when_no_entity() -> None:
    """No entities row → COALESCE falls back to messages.sender_first_name."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=555)
    _seed_message_p39(conn, dialog_id=555, message_id=1, sender_id=124, sender_first_name="Old Name")
    conn.commit()
    server = make_server(conn)
    resp = await server._list_messages({"dialog_id": 555, "limit": 10})
    assert resp["data"]["messages"][0]["sender_first_name"] == "Old Name"


@pytest.mark.asyncio
async def test_list_messages_returns_none_when_both_sources_missing() -> None:
    """Both entities and sender_first_name NULL → result is None (no crash)."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=555)
    _seed_message_p39(conn, dialog_id=555, message_id=1, sender_id=125, sender_first_name=None)
    conn.commit()
    server = make_server(conn)
    resp = await server._list_messages({"dialog_id": 555, "limit": 10})
    assert resp["data"]["messages"][0]["sender_first_name"] is None


@pytest.mark.asyncio
async def test_list_messages_entity_name_wins_over_stale_column() -> None:
    """entities.name wins over non-NULL but stale sender_first_name."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=555)
    _seed_entity_p39(conn, entity_id=126, name="New")
    _seed_message_p39(conn, dialog_id=555, message_id=1, sender_id=126, sender_first_name="Old")
    conn.commit()
    server = make_server(conn)
    resp = await server._list_messages({"dialog_id": 555, "limit": 10})
    assert resp["data"]["messages"][0]["sender_first_name"] == "New"


@pytest.mark.asyncio
async def test_list_unread_messages_uses_entity_name() -> None:
    """_FETCH_UNREAD_MESSAGES_SQL: NULL sender_first_name resolved via entities JOIN."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=555, read_inbox_max_id=0)
    # dialog entity (needed by _COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL)
    _seed_entity_p39(conn, entity_id=555, name="Test Dialog")
    # sender entity (resolved via _FETCH_UNREAD_MESSAGES_SQL JOIN)
    _seed_entity_p39(conn, entity_id=123, name="Konstantin")
    _seed_message_p39(conn, dialog_id=555, message_id=10, sender_id=123, sender_first_name=None)
    conn.commit()
    server = make_server(conn)
    result = await server._dispatch(
        {
            "method": "get_inbox",
            "scope": "personal",
            "limit": 100,
            "group_size_threshold": 100,
        }
    )
    assert result["ok"] is True
    groups = result["data"]["groups"]
    dialog_group = next((g for g in groups if g["dialog_id"] == 555), None)
    assert dialog_group is not None
    assert len(dialog_group["messages"]) == 1
    assert dialog_group["messages"][0]["sender_first_name"] == "Konstantin"


# --- Task 1: SQL invariant tests (MANDATORY, no skip) ---


def test_fts_sql_invariants_mandatory() -> None:
    """MANDATORY — no skip clause. Locks FTS SQL-string contract (Phase 39.1-02 aliases)."""
    from mcp_telegram import daemon_api as d

    # _SELECT_FTS_SQL: dual sender entity JOINs (e_raw + e_eff) for effective_sender_id lookup
    assert "LEFT JOIN entities e_raw ON e_raw.id = m.sender_id" in d._SELECT_FTS_SQL, (
        "_SELECT_FTS_SQL missing LEFT JOIN entities e_raw"
    )
    assert "LEFT JOIN entities e_eff" in d._SELECT_FTS_SQL, "_SELECT_FTS_SQL missing LEFT JOIN entities e_eff"
    assert "COALESCE(e_raw.name, e_eff.name, m.sender_first_name)" in d._SELECT_FTS_SQL, (
        "_SELECT_FTS_SQL missing triple COALESCE(e_raw.name, e_eff.name, m.sender_first_name)"
    )
    # _SELECT_FTS_ALL_SQL: de for dialog, e_raw/e_eff for sender
    assert "LEFT JOIN entities e_raw ON e_raw.id = m.sender_id" in d._SELECT_FTS_ALL_SQL, (
        "_SELECT_FTS_ALL_SQL missing LEFT JOIN entities e_raw"
    )
    assert "LEFT JOIN entities de ON de.id = f.dialog_id" in d._SELECT_FTS_ALL_SQL, (
        "_SELECT_FTS_ALL_SQL missing LEFT JOIN entities de ON de.id = f.dialog_id"
    )
    assert "COALESCE(e_raw.name, e_eff.name, m.sender_first_name)" in d._SELECT_FTS_ALL_SQL, (
        "_SELECT_FTS_ALL_SQL must use triple COALESCE(e_raw.name, e_eff.name, m.sender_first_name)"
    )


def test_all_sql_constants_contain_entity_join_and_coalesce() -> None:
    from mcp_telegram import daemon_api as d

    for name in (
        "_SELECT_MESSAGES_SQL",
        "_SELECT_FTS_SQL",
        "_SELECT_FTS_ALL_SQL",
        "_FETCH_UNREAD_MESSAGES_SQL",
        "_LIST_MESSAGES_BASE_SQL",
    ):
        sql = getattr(d, name)
        assert "LEFT JOIN entities" in sql, f"{name} missing LEFT JOIN entities"
        assert "COALESCE(" in sql, f"{name} missing COALESCE"
        # Phase 39.1-02: every read-path SELECT projects effective_sender_id
        assert "effective_sender_id" in sql, f"{name} missing effective_sender_id projection"
        assert ":self_id" in sql, f"{name} missing :self_id parameter binding"


def test_line_409_filter_has_documenting_comment() -> None:
    """Documents that sender_name filter intentionally uses the denormalized column."""
    import pathlib

    src = pathlib.Path("src/mcp_telegram/daemon_api.py").read_text()
    assert src.count("Filter uses denormalized column intentionally") == 1


# --- Task 2: structured log counter tests ---


@pytest.mark.asyncio
async def test_list_messages_emits_structured_log_on_sync_db_success(caplog: pytest.LogCaptureFixture) -> None:
    """sync.db success path emits exactly one list_messages rendered log with correct counters."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=555)
    _seed_entity_p39(conn, entity_id=100, name="Resolved")
    # Row 1: sender resolved via entity (sender_id=100, entity exists)
    _seed_message_p39(conn, 555, 1, sender_id=100, sender_first_name="Resolved")
    # Row 2: sender_id present but no entity row → unresolved_entity_rows
    _seed_message_p39(conn, 555, 2, sender_id=999, sender_first_name=None)
    # Row 3: sender_id is NULL → null_sender_rows
    _seed_message_p39(conn, 555, 3, sender_id=None, sender_first_name=None)
    conn.commit()
    server = make_server(conn)
    caplog.clear()
    with caplog.at_level("INFO", logger="mcp_telegram.daemon_api"):
        resp = await server._list_messages({"dialog_id": 555, "limit": 50})
    assert resp["ok"] is True
    records = [r for r in caplog.records if r.message == "list_messages rendered"]
    assert len(records) == 1, f"Expected 1 log record, got: {[r.message for r in caplog.records]}"
    rec = records[0]
    assert rec.dialog_id == 555
    assert rec.rows == 3
    assert rec.null_sender_rows == 1
    assert rec.unresolved_entity_rows == 1


@pytest.mark.asyncio
async def test_list_messages_logs_zero_counters_when_empty(caplog: pytest.LogCaptureFixture) -> None:
    """Empty synced dialog emits log with zero counters."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=777)
    conn.commit()
    server = make_server(conn)
    caplog.clear()
    with caplog.at_level("INFO", logger="mcp_telegram.daemon_api"):
        await server._list_messages({"dialog_id": 777, "limit": 50})
    records = [r for r in caplog.records if r.message == "list_messages rendered"]
    assert len(records) == 1
    rec = records[0]
    assert rec.rows == 0
    assert rec.null_sender_rows == 0
    assert rec.unresolved_entity_rows == 0


def test_list_messages_has_exactly_two_log_sites_in_method() -> None:
    """Source invariant: exactly two list_messages rendered emissions in daemon_api.py —
    one in _list_messages_from_db (main sync.db path) and one in
    _list_messages_context_window (anchor path). Non-sync.db paths must not emit."""
    import pathlib

    src = pathlib.Path("src/mcp_telegram/daemon_api.py").read_text()
    count = src.count('"list_messages rendered"')
    assert count == 2, f"Expected exactly two `list_messages rendered` log calls in daemon_api.py, found {count}"


@pytest.mark.asyncio
async def test_fallback_path_does_not_emit_counter(caplog: pytest.LogCaptureFixture) -> None:
    """Non-sync.db path (dialog not in synced_dialogs → Telegram fallback) must NOT emit counter."""
    conn = _make_db()
    # No synced_dialogs row — triggers live Telegram fallback
    server = make_server(conn)
    # Mock telegram client to return empty list (avoid real API call)
    server._client.iter_messages = AsyncMock(return_value=iter([]))
    caplog.clear()
    with caplog.at_level("INFO", logger="mcp_telegram.daemon_api"):
        await server._list_messages({"dialog_id": 999_999_999, "limit": 10})
    records = [r for r in caplog.records if r.message == "list_messages rendered"]
    assert len(records) == 0, "Fallback/non-sync.db path must NOT emit the counter log"


# ---------------------------------------------------------------------------
# Phase 39.1-02: effective_sender_id SQL projection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_effective_sender_dm_outgoing() -> None:
    """DM outgoing (sender_id=NULL, out=1) resolves effective_sender_id to self_id."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=268071163)
    _seed_message_p39(
        conn,
        dialog_id=268071163,
        message_id=1,
        sender_id=None,
        sender_first_name=None,
        out=1,
        is_service=0,
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 99999
    resp = await server._list_messages({"dialog_id": 268071163, "limit": 10})
    assert resp["ok"] is True
    assert resp["data"]["messages"][0]["effective_sender_id"] == 99999


@pytest.mark.asyncio
async def test_list_messages_effective_sender_dm_incoming() -> None:
    """DM incoming (sender_id=NULL, out=0) resolves effective_sender_id to dialog_id (peer)."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=268071163)
    _seed_message_p39(
        conn,
        dialog_id=268071163,
        message_id=1,
        sender_id=None,
        sender_first_name=None,
        out=0,
        is_service=0,
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 99999
    resp = await server._list_messages({"dialog_id": 268071163, "limit": 10})
    assert resp["ok"] is True
    assert resp["data"]["messages"][0]["effective_sender_id"] == 268071163


@pytest.mark.asyncio
async def test_list_messages_effective_sender_service_message_is_null() -> None:
    """Service message (is_service=1, sender_id=NULL) keeps effective_sender_id=NULL."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=268071163)
    _seed_message_p39(
        conn,
        dialog_id=268071163,
        message_id=1,
        sender_id=None,
        sender_first_name=None,
        out=0,
        is_service=1,
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 99999
    resp = await server._list_messages({"dialog_id": 268071163, "limit": 10})
    assert resp["ok"] is True
    assert resp["data"]["messages"][0]["effective_sender_id"] is None
    assert resp["data"]["messages"][0]["is_service"] == 1


@pytest.mark.asyncio
async def test_list_messages_effective_sender_group_unknown_is_null() -> None:
    """Group unknown sender (dialog_id<0, sender_id=NULL, is_service=0) stays NULL."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=-100123)
    _seed_message_p39(
        conn,
        dialog_id=-100123,
        message_id=1,
        sender_id=None,
        sender_first_name=None,
        out=0,
        is_service=0,
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 99999
    resp = await server._list_messages({"dialog_id": -100123, "limit": 10})
    assert resp["ok"] is True
    assert resp["data"]["messages"][0]["effective_sender_id"] is None


def test_msg_to_dict_telethon_fallback_computes_effective_sender() -> None:
    """_msg_to_dict: DM outgoing with sender_id=None, out=True resolves effective_sender_id=self_id."""
    from types import SimpleNamespace

    # Minimal telethon-ish mock
    mock_date = SimpleNamespace(timestamp=lambda: 1700000000.0)
    mock_msg = SimpleNamespace(
        id=42,
        date=mock_date,
        message="hi",
        sender_id=None,
        sender=None,
        media=None,
        reactions=None,
        reply_to=None,
        reply_to_msg_id=None,
        forum_topic_id=None,
        edit_date=None,
        out=True,
    )
    result = DaemonAPIServer._msg_to_dict(mock_msg, dialog_id=268071163, self_id=99999)
    assert result["effective_sender_id"] == 99999
    assert result["is_service"] == 0
    assert result["out"] == 1


def test_msg_to_dict_telethon_fallback_dm_incoming() -> None:
    """_msg_to_dict: DM incoming (out=False, sender_id=None) resolves to dialog_id (peer)."""
    from types import SimpleNamespace

    mock_date = SimpleNamespace(timestamp=lambda: 1700000000.0)
    mock_msg = SimpleNamespace(
        id=42,
        date=mock_date,
        message="hi",
        sender_id=None,
        sender=None,
        media=None,
        reactions=None,
        reply_to=None,
        reply_to_msg_id=None,
        forum_topic_id=None,
        edit_date=None,
        out=False,
    )
    result = DaemonAPIServer._msg_to_dict(mock_msg, dialog_id=268071163, self_id=99999)
    assert result["effective_sender_id"] == 268071163
    assert result["is_service"] == 0


@pytest.mark.asyncio
async def test_list_messages_dm_outgoing_resolves_sender_first_name_via_e_eff() -> None:
    """DM outgoing: e_eff JOIN on effective_sender_id=self_id resolves self's name."""
    conn = _make_db()
    _seed_synced_dialog_p39(conn, dialog_id=268071163)
    _seed_entity_p39(conn, entity_id=99999, name="Me")
    _seed_message_p39(
        conn,
        dialog_id=268071163,
        message_id=1,
        sender_id=None,
        sender_first_name=None,
        out=1,
        is_service=0,
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 99999
    resp = await server._list_messages({"dialog_id": 268071163, "limit": 10})
    assert resp["ok"] is True
    assert resp["data"]["messages"][0]["sender_first_name"] == "Me"


# --- Phase 999.1: get_my_recent_activity ----------------------------


@pytest.mark.asyncio
async def test_get_my_recent_activity_never_run() -> None:
    """Empty DB with last_sync_at=NULL → scan_status='never_run', empty comments."""
    server = make_server(_make_db_with_activity())
    req = {"method": "get_my_recent_activity", "since_hours": 168, "limit": 100}
    resp = await server._dispatch(req)
    assert resp["ok"] is True
    data = resp["data"]
    assert data["comments"] == []
    assert data["scan_status"] == "never_run"
    assert data["scanned_at"] is None


@pytest.mark.asyncio
async def test_get_my_recent_activity_in_progress() -> None:
    """backfill_started_at set but backfill_complete='0' → scan_status='in_progress'."""
    server = make_server(_make_db_with_activity())
    with server._conn:
        server._conn.execute(
            "INSERT OR IGNORE INTO activity_sync_state (key, value) VALUES ('backfill_started_at', '1700000000')"
        )
        # backfill_complete stays '0', last_sync_at stays NULL
    resp = await server._dispatch({"method": "get_my_recent_activity"})
    assert resp["data"]["scan_status"] == "in_progress"
    assert resp["data"]["scanned_at"] is None


@pytest.mark.asyncio
async def test_get_my_recent_activity_filters_by_since_hours() -> None:
    """since_hours=1 returns only the recent row; scan_status='complete'."""
    server = make_server(_make_db_with_activity())
    now = int(time.time())
    with server._conn:
        server._conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 1, ?, 'recent', 1, 0, 0)",
            (now - 60,),
        )
        server._conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 2, ?, 'old', 1, 0, 0)",
            (now - 999_999,),
        )
        server._conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        server._conn.execute(f"UPDATE activity_sync_state SET value='{now}' WHERE key='last_sync_at'")
    resp = await server._dispatch({"method": "get_my_recent_activity", "since_hours": 1})
    texts = [c["text"] for c in resp["data"]["comments"]]
    assert texts == ["recent"]
    assert resp["data"]["scan_status"] == "complete"


@pytest.mark.asyncio
async def test_get_my_recent_activity_joins_dialog_name() -> None:
    """dialog_name is populated from entities table via LEFT JOIN."""
    server = make_server(_make_db_with_activity())
    now = int(time.time())
    with server._conn:
        server._conn.execute(
            "INSERT OR REPLACE INTO entities (id, type, name, username, name_normalized, updated_at) "
            "VALUES (42, 'group', 'My Group', NULL, 'my group', ?)",
            (now,),
        )
        server._conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 1, ?, 'hi', 1, 0, 0)",
            (now - 60,),
        )
        server._conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        server._conn.execute(f"UPDATE activity_sync_state SET value='{now}' WHERE key='last_sync_at'")
    resp = await server._dispatch({"method": "get_my_recent_activity"})
    names = [c["dialog_name"] for c in resp["data"]["comments"]]
    assert names == ["My Group"]


@pytest.mark.asyncio
async def test_get_my_recent_activity_falls_back_to_str_dialog_id() -> None:
    """When no entities row exists, dialog_name falls back to str(dialog_id)."""
    server = make_server(_make_db_with_activity())
    now = int(time.time())
    with server._conn:
        server._conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (999, 1, ?, 'x', 1, 0, 0)",
            (now - 60,),
        )
        server._conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        server._conn.execute(f"UPDATE activity_sync_state SET value='{now}' WHERE key='last_sync_at'")
    resp = await server._dispatch({"method": "get_my_recent_activity"})
    assert resp["data"]["comments"][0]["dialog_name"] == "999"


@pytest.mark.asyncio
async def test_get_my_recent_activity_clamps_since_hours() -> None:
    """Wildly out-of-range since_hours must not crash — clamped to 8760."""
    server = make_server(_make_db_with_activity())
    # Request wildly out-of-range since_hours — must not crash or error out
    resp = await server._dispatch({"method": "get_my_recent_activity", "since_hours": 999_999})
    assert resp["ok"] is True
    assert resp["data"]["scan_status"] == "never_run"


@pytest.mark.asyncio
async def test_get_my_recent_activity_filters_incoming_messages() -> None:
    """All three predicate filters are load-bearing: out=1 AND is_service=0 AND is_deleted=0.

    Cross-AI review (2026-04-24) HIGH finding: `WHERE m.out = 1` alone
    is too broad after unification because the unified messages table
    also holds is_service=1 rows (group join/leave events) and
    is_deleted=1 rows (tombstones) that activity_comments never stored.
    This test seeds all four combinations and asserts the tool returns
    ONLY the (out=1, is_service=0, is_deleted=0) row.
    """
    server = make_server(_make_db_with_activity())
    now = int(time.time())
    with server._conn:
        # KEEP: outgoing, non-service, non-deleted.
        server._conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 1, ?, 'mine', 1, 0, 0)",
            (now - 60,),
        )
        # DROP: out=0 (incoming from full sync).
        server._conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 2, ?, 'theirs', 0, 0, 0)",
            (now - 120,),
        )
        # DROP: out=1 but is_service=1 (e.g. 'You created this group').
        #   activity_comments never stored service rows — the read path
        #   must not start surfacing them after unification.
        server._conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 3, ?, 'you-created-group', 1, 1, 0)",
            (now - 180,),
        )
        # DROP: out=1 but is_deleted=1 (tombstone for a message the user
        #   deleted). Same rationale — must not leak via the read path.
        server._conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 4, ?, 'ghost', 1, 0, 1)",
            (now - 240,),
        )
        server._conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        server._conn.execute(f"UPDATE activity_sync_state SET value='{now}' WHERE key='last_sync_at'")
    resp = await server._dispatch({"method": "get_my_recent_activity"})
    texts = sorted(c["text"] for c in resp["data"]["comments"])
    assert texts == ["mine"], (
        f"Expected exactly the (out=1, is_service=0, is_deleted=0) row 'mine'; "
        f"got {texts}. Did the SQL predicate omit is_service/is_deleted filters?"
    )


def test_search_messages_finds_migrated_own_message() -> None:
    """D-5 proof: migrated own messages become FTS-searchable.

    Cross-AI review (2026-04-24) HIGH finding: neither Codex nor
    OpenCode could find a test that proves the headline phase goal
    — own messages become findable via SearchMessages after migration.
    This test simulates the migration-then-FTS-backfill path at unit
    level: seed an out=1 row directly in messages (no FTS entry yet,
    as would happen immediately after the v15 migration INSERT);
    invoke backfill_fts_index() to close the gap (the same function
    daemon.py runs at startup after ensure_sync_schema); then
    execute a raw `messages_fts MATCH` query and assert the row is
    findable. A live MCP-level SearchMessages call is additionally
    performed by Task 4 against the running container.
    """
    from mcp_telegram.fts import backfill_fts_index

    conn = _make_db_with_activity()
    # _make_db() creates messages_fts only with with_fts=True (default False).
    # Create the FTS table here to simulate the daemon startup environment.
    from mcp_telegram.fts import MESSAGES_FTS_DDL
    conn.execute(MESSAGES_FTS_DDL)
    conn.commit()

    # Seed an out=1 row as if v15 migration had just placed it there.
    conn.execute(
        "INSERT INTO messages "
        "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
        "VALUES (42, 7, 1700000000, 'unique-search-needle alpha', 1, 0, 0)",
    )
    conn.commit()

    # Close the FTS gap — this is what daemon.py does on startup after
    # ensure_sync_schema applies the v15 migration.
    backfill_fts_index(conn)

    # Now the row must be findable via FTS MATCH.
    # messages_fts stores dialog_id and message_id as UNINDEXED columns;
    # messages uses WITHOUT ROWID so we join on the natural PK pair.
    # FTS5 MATCH must target stemmed_text column and use the stemmed query form
    # (same as daemon_api._search_messages via stem_query()).
    from mcp_telegram.fts import stem_query
    stemmed = stem_query("unique-search-needle")
    hits = conn.execute(
        "SELECT m.dialog_id, m.message_id, m.text "
        "FROM messages_fts fts "
        "JOIN messages m ON m.dialog_id = fts.dialog_id AND m.message_id = fts.message_id "
        "WHERE fts.stemmed_text MATCH ? AND m.out = 1",
        (stemmed,),
    ).fetchall()
    assert len(hits) == 1, (
        f"D-5 violated: migrated own message must be FTS-searchable after "
        f"backfill_fts_index(). Hits: {hits}"
    )
    assert hits[0] == (42, 7, "unique-search-needle alpha")
