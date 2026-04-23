"""JIT reactions freshen-on-read tests (Phase 39.2 Plan 02).

Covers AC-3, AC-4, AC-4-PAGED, AC-5, AC-6, AC-6-PARTIAL plus per-path wiring
for _list_messages, _list_messages_context_window, scoped _search_messages,
and _list_unread_messages.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import (
    REACTIONS_TTL_SECONDS,
    DaemonAPIServer,
)
from mcp_telegram.fts import MESSAGES_FTS_DDL

# ---------------------------------------------------------------------------
# Patch get_peer_id for MagicMock entities
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_get_peer_id():
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda entity: int(getattr(entity, "id", 0)),
    ):
        yield


# ---------------------------------------------------------------------------
# In-memory DB harness — schema mirrors Plan 01 v11
# ---------------------------------------------------------------------------


def _make_db(*, with_fts: bool = False) -> sqlite3.Connection:
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
        CREATE TABLE message_reactions (
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
        CREATE TABLE message_reactions_freshness (
            dialog_id   INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            checked_at  INTEGER NOT NULL,
            PRIMARY KEY (dialog_id, message_id)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE message_versions (
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
        CREATE TABLE topic_metadata (
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
        CREATE TABLE entities (
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


def _seed_synced(conn: sqlite3.Connection, dialog_id: int) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id, last_event_at) VALUES (?, 'synced', 0, ?)",
        (dialog_id, int(time.time())),
    )
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, type, name, username, name_normalized, updated_at) "
        "VALUES (?, 'User', 'Alice', NULL, NULL, ?)",
        (dialog_id, int(time.time())),
    )
    conn.commit()


def _seed_message(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> None:
    conn.execute(
        "INSERT INTO messages "
        "(dialog_id, message_id, sent_at, text, sender_id, sender_first_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dialog_id, message_id, 1700000000 + message_id, f"msg {message_id}", 99, "Alice"),
    )
    conn.commit()


def _seed_freshness(conn: sqlite3.Connection, dialog_id: int, message_ids: list[int], checked_at: int) -> None:
    for mid in message_ids:
        conn.execute(
            "INSERT OR REPLACE INTO message_reactions_freshness (dialog_id, message_id, checked_at) VALUES (?, ?, ?)",
            (dialog_id, mid, checked_at),
        )
    conn.commit()


def _seed_reaction(conn: sqlite3.Connection, dialog_id: int, message_id: int, emoji: str, count: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (dialog_id, message_id, emoji, count),
    )
    conn.commit()


def _msg_with_reactions(msg_id: int, emoji: str = "❤", count: int = 1) -> SimpleNamespace:
    """Mock Telethon Message with .reactions populated like a real ReactionCount."""
    rc = SimpleNamespace(
        reaction=SimpleNamespace(emoticon=emoji),
        count=count,
        chosen_order=None,
    )
    reactions = SimpleNamespace(results=[rc], recent_reactions=None)
    return SimpleNamespace(id=msg_id, reactions=reactions)


def _msg_no_reactions(msg_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=msg_id, reactions=None)


def make_server(conn: sqlite3.Connection, client: Any) -> DaemonAPIServer:
    shutdown_event = asyncio.Event()
    return DaemonAPIServer(conn, client, shutdown_event)


# ---------------------------------------------------------------------------
# Task 1: _freshen_reactions_if_stale tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jit_cold_fetch_updates_state() -> None:
    """AC-3: 30 cold ids → 1 get_messages, 30 freshness rows, 30 reaction rows."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 31))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in ids])
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(dialog_id, dialog_id, ids)

    assert client.get_messages.call_count == 1
    call = client.get_messages.call_args
    assert call.kwargs["ids"] == ids

    fresh = conn.execute(
        "SELECT COUNT(*) FROM message_reactions_freshness WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()[0]
    assert fresh == 30

    rxn = conn.execute("SELECT COUNT(*) FROM message_reactions WHERE dialog_id=?", (dialog_id,)).fetchone()[0]
    assert rxn == 30


@pytest.mark.asyncio
async def test_jit_all_fresh_no_api_call() -> None:
    """AC-4: all 30 ids fresh in TTL → zero get_messages, DB untouched."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 31))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
    now = int(time.time())
    _seed_freshness(conn, dialog_id, ids, now - 100)

    client = MagicMock()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(dialog_id, dialog_id, ids)

    assert client.get_messages.call_count == 0
    rxn = conn.execute("SELECT COUNT(*) FROM message_reactions WHERE dialog_id=?", (dialog_id,)).fetchone()[0]
    assert rxn == 0


@pytest.mark.asyncio
async def test_jit_page1_fresh_page2_cold_partial_fetch() -> None:
    """AC-4-PAGED: page1 ids 1..30 fresh; ids 1..60 requested → fetch only 31..60."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids_all = list(range(1, 61))
    for mid in ids_all:
        _seed_message(conn, dialog_id, mid)
    now = int(time.time())
    _seed_freshness(conn, dialog_id, list(range(1, 31)), now - 100)

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in range(31, 61)])
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(dialog_id, dialog_id, ids_all)

    assert client.get_messages.call_count == 1
    call_ids = client.get_messages.call_args.kwargs["ids"]
    assert call_ids == list(range(31, 61))
    assert len(call_ids) == 30

    # page 1 freshness rows unchanged
    p1_checked = conn.execute(
        "SELECT checked_at FROM message_reactions_freshness WHERE dialog_id=? AND message_id<=30",
        (dialog_id,),
    ).fetchall()
    assert all(c[0] == now - 100 for c in p1_checked)

    # page 2 upserted
    p2_count = conn.execute(
        "SELECT COUNT(*) FROM message_reactions_freshness WHERE dialog_id=? AND message_id>30",
        (dialog_id,),
    ).fetchone()[0]
    assert p2_count == 30


@pytest.mark.asyncio
async def test_jit_partial_stale_subset_fetch() -> None:
    """30 requested, 10 fresh, 20 stale → get_messages with 20 ids."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 31))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
    now = int(time.time())
    fresh_ids = ids[:10]
    stale_ids = ids[10:]
    _seed_freshness(conn, dialog_id, fresh_ids, now - 50)

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in stale_ids])
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(dialog_id, dialog_id, ids)

    call_ids = client.get_messages.call_args.kwargs["ids"]
    assert call_ids == stale_ids
    assert len(call_ids) == 20

    # freshness upserts only for the 20 stale (10 originals stay)
    upserts = conn.execute(
        "SELECT message_id FROM message_reactions_freshness WHERE dialog_id=?",
        (dialog_id,),
    ).fetchall()
    upsert_ids = sorted(r[0] for r in upserts)
    assert upsert_ids == ids


@pytest.mark.asyncio
async def test_jit_ttl_expired_refreshes_no_duplicates() -> None:
    """AC-5: TTL expired → refetch; no duplicates; freshness rows updated."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 6))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
        _seed_reaction(conn, dialog_id, mid, "❤", 1)
    expired = int(time.time()) - 700
    _seed_freshness(conn, dialog_id, ids, expired)

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid, emoji="❤", count=1) for mid in ids])
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(dialog_id, dialog_id, ids)

    assert client.get_messages.call_count == 1
    rxn_rows = conn.execute(
        "SELECT message_id, emoji, count FROM message_reactions WHERE dialog_id=? ORDER BY message_id",
        (dialog_id,),
    ).fetchall()
    assert [tuple(r) for r in rxn_rows] == [(mid, "❤", 1) for mid in ids]

    # freshness updated to ~now
    now = int(time.time())
    fr = conn.execute(
        "SELECT message_id, checked_at FROM message_reactions_freshness WHERE dialog_id=?",
        (dialog_id,),
    ).fetchall()
    for _mid, ca in fr:
        assert ca >= now - 5
        assert ca > expired


@pytest.mark.asyncio
async def test_jit_floodwait_preserves_stale() -> None:
    """AC-6: FloodWait → no reactions mutation, no freshness upsert, warning logged."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 6))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
        _seed_reaction(conn, dialog_id, mid, "👍", 2)
    expired = int(time.time()) - 700
    _seed_freshness(conn, dialog_id, ids, expired)

    client = MagicMock()
    err = FloodWaitError(request=None)
    err.seconds = 30
    client.get_messages = AsyncMock(side_effect=err)
    server = make_server(conn, client)

    with patch("mcp_telegram.daemon_api.logger") as mock_logger:
        await server._freshen_reactions_if_stale(dialog_id, dialog_id, ids)

    # reactions untouched
    rxn_rows = conn.execute("SELECT count FROM message_reactions WHERE dialog_id=?", (dialog_id,)).fetchall()
    assert all(r[0] == 2 for r in rxn_rows)
    assert len(rxn_rows) == 5

    # freshness rows still at old `expired` value
    fr = conn.execute(
        "SELECT checked_at FROM message_reactions_freshness WHERE dialog_id=?",
        (dialog_id,),
    ).fetchall()
    assert all(c[0] == expired for c in fr)

    assert mock_logger.warning.called


@pytest.mark.asyncio
async def test_jit_partial_none_results_skip_freshness_upsert() -> None:
    """AC-6-PARTIAL: None entries get NO freshness row, no reactions mutation."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = [1, 2, 3, 4, 5]
    for mid in ids:
        _seed_message(conn, dialog_id, mid)

    client = MagicMock()
    client.get_messages = AsyncMock(
        return_value=[
            _msg_with_reactions(1),
            None,
            _msg_with_reactions(3),
            None,
            _msg_with_reactions(5),
        ]
    )
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(dialog_id, dialog_id, ids)

    fr_ids = sorted(
        r[0]
        for r in conn.execute(
            "SELECT message_id FROM message_reactions_freshness WHERE dialog_id=?",
            (dialog_id,),
        )
    )
    assert fr_ids == [1, 3, 5]

    rxn_ids = sorted(
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT message_id FROM message_reactions WHERE dialog_id=?",
            (dialog_id,),
        )
    )
    assert rxn_ids == [1, 3, 5]


@pytest.mark.asyncio
async def test_jit_empty_message_ids_early_returns() -> None:
    conn = _make_db()
    _seed_synced(conn, 1001)
    client = MagicMock()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(1001, 1001, [])

    assert client.get_messages.call_count == 0


@pytest.mark.asyncio
async def test_jit_unsynced_dialog_early_returns() -> None:
    conn = _make_db()
    # No synced_dialogs row
    client = MagicMock()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(1001, 1001, [1, 2, 3])

    assert client.get_messages.call_count == 0


@pytest.mark.asyncio
async def test_jit_fetch_window_never_expanded() -> None:
    """5 stale ids → get_messages called with exactly those 5, not more."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    # Seed many messages but only ask about 5
    for mid in range(1, 100):
        _seed_message(conn, dialog_id, mid)
    ask_ids = [10, 20, 30, 40, 50]

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in ask_ids])
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(dialog_id, dialog_id, ask_ids)

    assert client.get_messages.call_count == 1
    assert client.get_messages.call_args.kwargs["ids"] == ask_ids


@pytest.mark.asyncio
async def test_jit_reactions_cleared_when_telegram_has_none() -> None:
    """msg.reactions=None should clear cached rows (apply_reactions_delta with [])."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    _seed_message(conn, dialog_id, 1)
    _seed_reaction(conn, dialog_id, 1, "❤", 5)

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_no_reactions(1)])
    server = make_server(conn, client)

    await server._freshen_reactions_if_stale(dialog_id, dialog_id, [1])

    rxn = conn.execute(
        "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=? AND message_id=1",
        (dialog_id,),
    ).fetchone()[0]
    assert rxn == 0

    fr = conn.execute(
        "SELECT checked_at FROM message_reactions_freshness WHERE dialog_id=? AND message_id=1",
        (dialog_id,),
    ).fetchone()
    assert fr is not None


# ---------------------------------------------------------------------------
# Module sanity
# ---------------------------------------------------------------------------


def test_reactions_ttl_constant_is_600() -> None:
    assert REACTIONS_TTL_SECONDS == 600


# ---------------------------------------------------------------------------
# Task 2: Wiring tests through dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_triggers_jit_on_cold_read() -> None:
    """AC-3 end-to-end through _list_messages: one get_messages, reactions in response."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    for mid in range(1, 6):
        _seed_message(conn, dialog_id, mid)

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid, "❤", 2) for mid in range(1, 6)])
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "list_messages", "dialog_id": dialog_id, "limit": 10})

    assert result["ok"] is True
    assert client.get_messages.call_count == 1
    msgs = result["data"]["messages"]
    assert msgs
    assert any(m.get("reactions_display") for m in msgs)


@pytest.mark.asyncio
async def test_list_messages_skips_jit_when_all_fresh() -> None:
    """AC-4 through dispatcher: pre-seeded fresh → zero get_messages."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 6))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
    _seed_freshness(conn, dialog_id, ids, int(time.time()) - 50)

    client = MagicMock()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "list_messages", "dialog_id": dialog_id, "limit": 10})

    assert result["ok"] is True
    assert client.get_messages.call_count == 0


@pytest.mark.asyncio
async def test_list_messages_page1_fresh_page2_cold() -> None:
    """AC-4-PAGED end-to-end: simulate by pre-seeding freshness for half ids."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    for mid in range(1, 11):
        _seed_message(conn, dialog_id, mid)
    # mark first 5 as fresh
    _seed_freshness(conn, dialog_id, [1, 2, 3, 4, 5], int(time.time()) - 50)

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in [10, 9, 8, 7, 6]])
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "list_messages", "dialog_id": dialog_id, "limit": 10})

    assert result["ok"] is True
    assert client.get_messages.call_count == 1
    # Only the 5 cold ids fetched
    fetched_ids = sorted(client.get_messages.call_args.kwargs["ids"])
    assert fetched_ids == [6, 7, 8, 9, 10]


@pytest.mark.asyncio
async def test_list_messages_context_window_wiring() -> None:
    """Context-window path triggers JIT for the surrounding slice."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    for mid in range(1, 11):
        _seed_message(conn, dialog_id, mid)

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in range(1, 11)])
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch(
        {
            "method": "list_messages",
            "dialog_id": dialog_id,
            "context_message_id": 5,
            "context_size": 6,
        }
    )

    assert result["ok"] is True
    assert client.get_messages.call_count == 1


@pytest.mark.asyncio
async def test_search_messages_scoped_triggers_jit() -> None:
    """Scoped search (dialog_id provided) triggers JIT freshen."""
    conn = _make_db(with_fts=True)
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    for mid in range(1, 4):
        _seed_message(conn, dialog_id, mid)
        # Index FTS row matching the message text
        conn.execute(
            "INSERT INTO messages_fts (dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
            (dialog_id, mid, f"hello {mid}"),
        )
    conn.commit()

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in [3, 2, 1]])
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch(
        {
            "method": "search_messages",
            "dialog_id": dialog_id,
            "query": "hello",
            "limit": 10,
        }
    )

    assert result["ok"] is True
    # JIT fired exactly once for the scoped search
    assert client.get_messages.call_count == 1


@pytest.mark.asyncio
async def test_search_messages_global_skips_jit() -> None:
    """Global search (dialog_id=None) does NOT trigger JIT."""
    conn = _make_db(with_fts=True)
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    for mid in range(1, 4):
        _seed_message(conn, dialog_id, mid)
        conn.execute(
            "INSERT INTO messages_fts (dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
            (dialog_id, mid, f"hello {mid}"),
        )
    conn.commit()

    client = MagicMock()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch(
        {
            "method": "search_messages",
            "query": "hello",
            "limit": 10,
        }
    )

    assert result["ok"] is True
    assert client.get_messages.call_count == 0


@pytest.mark.asyncio
async def test_list_unread_messages_injects_reactions() -> None:
    """Unread path now surfaces reactions_display populated from message_reactions."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    # Set read_inbox_max_id=10 so messages 11+ are unread
    conn.execute(
        "UPDATE synced_dialogs SET read_inbox_max_id=10 WHERE dialog_id=?",
        (dialog_id,),
    )
    for mid in [11, 12, 13]:
        _seed_message(conn, dialog_id, mid)
        _seed_reaction(conn, dialog_id, mid, "🔥", 3)
    # Pre-mark fresh so JIT does not fire (we just want to test reaction injection)
    _seed_freshness(conn, dialog_id, [11, 12, 13], int(time.time()) - 50)
    conn.commit()

    client = MagicMock()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "list_unread_messages", "scope": "personal", "limit": 100})

    assert result["ok"] is True
    groups = result["data"]["groups"]
    assert len(groups) == 1
    msgs = groups[0]["messages"]
    assert msgs
    assert all("reactions_display" in m for m in msgs)
    assert all(m["reactions_display"] for m in msgs)


@pytest.mark.asyncio
async def test_list_unread_messages_triggers_jit_on_cold_read() -> None:
    """Unread path JIT wiring: cold read fires get_messages."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    conn.execute(
        "UPDATE synced_dialogs SET read_inbox_max_id=10 WHERE dialog_id=?",
        (dialog_id,),
    )
    for mid in [11, 12, 13]:
        _seed_message(conn, dialog_id, mid)
    conn.commit()

    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in [13, 12, 11]])
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "list_unread_messages", "scope": "personal", "limit": 100})

    assert result["ok"] is True
    assert client.get_messages.call_count == 1


@pytest.mark.asyncio
async def test_list_unread_messages_skips_jit_when_all_fresh() -> None:
    """TTL gate on unread path: pre-fresh → zero get_messages."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    conn.execute(
        "UPDATE synced_dialogs SET read_inbox_max_id=10 WHERE dialog_id=?",
        (dialog_id,),
    )
    for mid in [11, 12, 13]:
        _seed_message(conn, dialog_id, mid)
    _seed_freshness(conn, dialog_id, [11, 12, 13], int(time.time()) - 50)
    conn.commit()

    client = MagicMock()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "list_unread_messages", "scope": "personal", "limit": 100})

    assert result["ok"] is True
    assert client.get_messages.call_count == 0


@pytest.mark.asyncio
async def test_non_content_methods_do_not_trigger_jit() -> None:
    """get_sync_status, list_dialogs etc. → zero JIT calls."""
    conn = _make_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)

    client = MagicMock()
    client.get_messages = AsyncMock()

    # iter_dialogs needs to be an async iterator; return empty
    async def _empty(*_a, **_k):
        if False:
            yield  # pragma: no cover

    client.iter_dialogs = MagicMock(side_effect=_empty)
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "get_sync_status", "dialog_id": dialog_id})
    assert result["ok"] is True
    assert client.get_messages.call_count == 0
