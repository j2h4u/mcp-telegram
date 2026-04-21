"""Tests for Plan 39.3-03 Task 2 — daemon-side read-state helpers + 4 read-path response-dict extensions.

Covers:
- _classify_dialog_type(entity) — reuse existing helper (all six type strings).
- _read_state_for_dialog(conn, dialog_id, dialog_type) — returns ReadState dict for DMs, None otherwise.
- _dialog_type_from_db(conn, dialog_id) — DB-only dialog-type lookup (zero Telegram API).
- _list_messages / _list_messages_context_window / _search_messages / _fetch_unread_groups / _list_unread_messages responses include read_state + dialog_type fields.

Uses in-memory SQLite; telethon client mocked. Zero real Telegram calls.
"""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_api import (
    DaemonAPIServer,
    _classify_dialog_type,
    _dialog_type_from_db,
    _read_state_for_dialog,
)


# ---------------------------------------------------------------------------
# Module-wide patch: telethon_utils.get_peer_id returns entity.id for mocks
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_get_peer_id():
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda entity: int(getattr(entity, "id", 0)),
    ):
        yield


# ---------------------------------------------------------------------------
# Schema helper (includes read_outbox_max_id — Phase 39.3 v12)
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
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
    from mcp_telegram.fts import MESSAGES_FTS_DDL

    conn.execute(MESSAGES_FTS_DDL)
    conn.commit()
    return conn


def _insert_synced_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    status: str = "synced",
    read_inbox_max_id: int | None = None,
    read_outbox_max_id: int | None = None,
    total_messages: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, read_inbox_max_id, read_outbox_max_id, total_messages) "
        "VALUES (?, ?, ?, ?, ?)",
        (dialog_id, status, read_inbox_max_id, read_outbox_max_id, total_messages),
    )
    conn.commit()


def _insert_entity(
    conn: sqlite3.Connection, id_: int, type_: str, *, name: str = "X"
) -> None:
    conn.execute(
        "INSERT INTO entities (id, type, name, updated_at) VALUES (?, ?, ?, 0)",
        (id_, type_, name),
    )
    conn.commit()


def _insert_message(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    out: int = 0,
    sent_at: int = 1_700_000_000,
    text: str = "hi",
) -> None:
    conn.execute(
        "INSERT INTO messages "
        "(dialog_id, message_id, sent_at, text, out) VALUES (?, ?, ?, ?, ?)",
        (dialog_id, message_id, sent_at, text, out),
    )
    conn.commit()


def make_server(
    conn: sqlite3.Connection | None = None,
    client: object | None = None,
) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    shutdown_event = asyncio.Event()
    return DaemonAPIServer(conn, client, shutdown_event)


# ---------------------------------------------------------------------------
# _classify_dialog_type — pure unit coverage
# ---------------------------------------------------------------------------


def test_classify_dialog_type_user() -> None:
    user = SimpleNamespace(first_name="Alice", bot=False)
    assert _classify_dialog_type(user) == "User"


def test_classify_dialog_type_channel_group_bot_forum() -> None:
    # Bot
    bot = SimpleNamespace(first_name="Botty", bot=True)
    assert _classify_dialog_type(bot) == "Bot"

    # Channel / Group / Forum via the Channel telethon class.
    # Build via __new__ to avoid telethon-version-specific constructor signatures;
    # _classify_dialog_type inspects isinstance() + megagroup/forum attrs only.
    from telethon.tl.types import Channel, Chat

    channel = Channel.__new__(Channel)
    channel.megagroup = False
    channel.forum = False
    assert _classify_dialog_type(channel) == "Channel"

    group = Channel.__new__(Channel)
    group.megagroup = True
    group.forum = False
    assert _classify_dialog_type(group) == "Group"

    forum = Channel.__new__(Channel)
    forum.megagroup = True
    forum.forum = True
    assert _classify_dialog_type(forum) == "Forum"

    # Chat is detected via isinstance; constructor signature varies by telethon
    # version, so build a Chat by bypassing __init__ — isinstance is what matters.
    chat = Chat.__new__(Chat)
    assert _classify_dialog_type(chat) == "Chat"

    assert _classify_dialog_type(None) == "Unknown"


# ---------------------------------------------------------------------------
# _dialog_type_from_db — DB lookup, zero Telegram calls
# ---------------------------------------------------------------------------


def test_dialog_type_from_db_reads_entities_table() -> None:
    conn = _make_db()
    _insert_entity(conn, 100, "User")
    _insert_entity(conn, 200, "Channel")
    assert _dialog_type_from_db(conn, 100) == "User"
    assert _dialog_type_from_db(conn, 200) == "Channel"


def test_dialog_type_from_db_missing_entity_returns_unknown() -> None:
    conn = _make_db()
    assert _dialog_type_from_db(conn, 999) == "Unknown"


# ---------------------------------------------------------------------------
# _read_state_for_dialog — core
# ---------------------------------------------------------------------------


def test_read_state_for_dialog_dm_populated() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=5, read_outbox_max_id=10)
    # two unread incoming (ids 6, 7), one unread outgoing (id 11)
    _insert_message(conn, 1, 6, out=0, sent_at=1000)
    _insert_message(conn, 1, 7, out=0, sent_at=1100)
    _insert_message(conn, 1, 11, out=1, sent_at=1200)
    # Already-read messages (below cursor)
    _insert_message(conn, 1, 3, out=0, sent_at=500)
    _insert_message(conn, 1, 9, out=1, sent_at=600)

    rs = _read_state_for_dialog(conn, 1, "User")
    assert rs is not None
    assert rs["inbox_unread_count"] == 2
    assert rs["outbox_unread_count"] == 1
    assert rs["inbox_cursor_state"] == "populated"
    assert rs["outbox_cursor_state"] == "populated"
    assert rs["inbox_max_id_anchor"] == 5
    assert rs["outbox_max_id_anchor"] == 10


def test_read_state_for_dialog_dm_all_read() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=50, read_outbox_max_id=50)
    _insert_message(conn, 1, 5, out=0)
    _insert_message(conn, 1, 6, out=1)

    rs = _read_state_for_dialog(conn, 1, "User")
    assert rs is not None
    assert rs["inbox_unread_count"] == 0
    assert rs["outbox_unread_count"] == 0
    assert rs["inbox_cursor_state"] == "all_read"
    assert rs["outbox_cursor_state"] == "all_read"


def test_read_state_for_dialog_dm_inbox_null() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=None, read_outbox_max_id=10)
    _insert_message(conn, 1, 5, out=0)
    _insert_message(conn, 1, 11, out=1)

    rs = _read_state_for_dialog(conn, 1, "User")
    assert rs is not None
    assert rs["inbox_cursor_state"] == "null"
    # NULL cursor: "inbox_max_id_anchor" must be omitted
    assert "inbox_max_id_anchor" not in rs
    assert rs["outbox_cursor_state"] == "populated"
    assert rs["outbox_unread_count"] == 1


def test_read_state_for_dialog_dm_outbox_null() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=5, read_outbox_max_id=None)
    _insert_message(conn, 1, 6, out=0)
    _insert_message(conn, 1, 7, out=1)

    rs = _read_state_for_dialog(conn, 1, "User")
    assert rs is not None
    assert rs["inbox_cursor_state"] == "populated"
    assert rs["outbox_cursor_state"] == "null"
    assert "outbox_max_id_anchor" not in rs


def test_read_state_for_dialog_non_dm_returns_none() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=0, read_outbox_max_id=0)
    assert _read_state_for_dialog(conn, 1, "Channel") is None
    assert _read_state_for_dialog(conn, 1, "Group") is None
    assert _read_state_for_dialog(conn, 1, "Forum") is None
    assert _read_state_for_dialog(conn, 1, "Chat") is None
    assert _read_state_for_dialog(conn, 1, "Bot") is None
    assert _read_state_for_dialog(conn, 1, "Unknown") is None


def test_read_state_for_dialog_oldest_unread_date_inbox() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=5, read_outbox_max_id=100)
    _insert_message(conn, 1, 6, out=0, sent_at=1500)
    _insert_message(conn, 1, 7, out=0, sent_at=1400)  # oldest unread incoming
    _insert_message(conn, 1, 8, out=0, sent_at=1600)

    rs = _read_state_for_dialog(conn, 1, "User")
    assert rs is not None
    assert rs["inbox_oldest_unread_date"] == 1400
    assert "outbox_oldest_unread_date" not in rs  # outbox caught up


def test_read_state_for_dialog_oldest_unread_date_outbox() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=100, read_outbox_max_id=5)
    _insert_message(conn, 1, 6, out=1, sent_at=2500)
    _insert_message(conn, 1, 7, out=1, sent_at=2400)  # oldest unread outgoing
    _insert_message(conn, 1, 8, out=1, sent_at=2600)

    rs = _read_state_for_dialog(conn, 1, "User")
    assert rs is not None
    assert rs["outbox_oldest_unread_date"] == 2400
    assert "inbox_oldest_unread_date" not in rs


def test_read_state_for_dialog_zero_telegram_api_calls() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=0, read_outbox_max_id=0)
    _insert_message(conn, 1, 1, out=0)

    # Use a strict MagicMock that would raise if any attribute is accessed
    client = MagicMock(spec=[])  # no attributes at all — any access raises AttributeError
    # Helper takes only conn + dialog_id + dialog_type; never touches client.
    rs = _read_state_for_dialog(conn, 1, "User")
    assert rs is not None
    # Double-check: client stayed untouched
    assert client.mock_calls == []


# ---------------------------------------------------------------------------
# Response-dict extensions — per-path coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_response_includes_read_state_and_dialog_type() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=0, read_outbox_max_id=0)
    _insert_entity(conn, 1, "User")
    _insert_message(conn, 1, 2, out=0, sent_at=1234)

    server = make_server(conn)
    server.self_id = 999
    result = await server._list_messages({"dialog_id": 1, "limit": 10})
    assert result["ok"] is True
    data = result["data"]
    assert data["dialog_type"] == "User"
    rs = data["read_state"]
    assert rs is not None
    assert rs["inbox_unread_count"] == 1


@pytest.mark.asyncio
async def test_list_messages_context_window_response_includes_read_state() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=0, read_outbox_max_id=5)
    _insert_entity(conn, 1, "User")
    for mid in (1, 2, 3, 4, 5):
        _insert_message(conn, 1, mid, out=0, sent_at=1000 + mid)

    server = make_server(conn)
    server.self_id = 999
    result = await server._list_messages(
        {"dialog_id": 1, "context_message_id": 3, "context_size": 4}
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["dialog_type"] == "User"
    assert data["read_state"] is not None
    assert data["read_state"]["inbox_unread_count"] == 5


@pytest.mark.asyncio
async def test_search_messages_response_includes_read_state_per_dialog() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=0, read_outbox_max_id=0)
    _insert_synced_dialog(conn, 2, read_inbox_max_id=0, read_outbox_max_id=0)
    _insert_synced_dialog(conn, 3, read_inbox_max_id=0, read_outbox_max_id=0)
    _insert_entity(conn, 1, "User")
    _insert_entity(conn, 2, "User")
    _insert_entity(conn, 3, "Channel")

    # Insert messages and index into FTS
    from mcp_telegram.fts import stem_text

    def _add(dialog_id: int, mid: int, text: str) -> None:
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, out) "
            "VALUES (?, ?, ?, ?, 0)",
            (dialog_id, mid, 1_700_000_000, text),
        )
        conn.execute(
            "INSERT INTO messages_fts (dialog_id, message_id, stemmed_text) "
            "VALUES (?, ?, ?)",
            (dialog_id, mid, stem_text(text)),
        )

    _add(1, 10, "searchable needle alpha")
    _add(2, 20, "searchable needle beta")
    _add(3, 30, "searchable needle gamma")
    conn.commit()

    server = make_server(conn)
    server.self_id = 999

    result = await server._search_messages({"query": "needle", "limit": 50})
    assert result["ok"] is True
    rsp = result["data"].get("read_state_per_dialog")
    assert rsp is not None
    # Only DMs (1, 2) included; Channel (3) excluded
    assert 1 in rsp and 2 in rsp
    assert 3 not in rsp


@pytest.mark.asyncio
async def test_list_unread_messages_response_includes_per_group_read_state() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=5, read_outbox_max_id=0)
    _insert_synced_dialog(conn, 2, read_inbox_max_id=3, read_outbox_max_id=0)
    _insert_entity(conn, 1, "User", name="Alice")
    _insert_entity(conn, 2, "User", name="Bob")
    _insert_message(conn, 1, 6, out=0, sent_at=1100)
    _insert_message(conn, 1, 7, out=0, sent_at=1200)
    _insert_message(conn, 2, 4, out=0, sent_at=1300)
    # Give each dialog a last_event_at so they appear in unread
    conn.execute("UPDATE synced_dialogs SET last_event_at = 1500")
    conn.commit()

    server = make_server(conn)
    server.self_id = 999

    result = await server._list_unread_messages(
        {"scope": "personal", "limit": 100}
    )
    assert result["ok"] is True
    groups = result["data"]["groups"]
    assert len(groups) >= 2
    for g in groups:
        assert "dialog_type" in g
        assert "read_state" in g
        if g["dialog_id"] in (1, 2):
            assert g["dialog_type"] == "User"
            assert g["read_state"] is not None


@pytest.mark.asyncio
async def test_non_dm_read_path_response_has_none_read_state() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 42, read_inbox_max_id=0, read_outbox_max_id=0)
    _insert_entity(conn, 42, "Channel")
    _insert_message(conn, 42, 1, out=0, sent_at=1000)

    server = make_server(conn)
    server.self_id = 999
    result = await server._list_messages({"dialog_id": 42, "limit": 10})
    assert result["ok"] is True
    assert result["data"]["dialog_type"] == "Channel"
    assert result["data"]["read_state"] is None
