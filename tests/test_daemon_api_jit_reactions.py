"""JIT reactions freshen-on-read tests (Phase 39.2 Plan 02).

Covers AC-3, AC-4, AC-4-PAGED, AC-5, AC-6, AC-6-PARTIAL plus per-path wiring
for _list_messages, _list_messages_context_window, scoped _search_messages,
and _list_unread_messages.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import DaemonAPIServer, _DaemonClientLike
from mcp_telegram.daemon_message import REACTIONS_TTL_SECONDS

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
# Seed helpers — schema via make_synced_db fixture (_apply_migrations)
# ---------------------------------------------------------------------------


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


class _TestClient(MagicMock):
    get_messages: object
    iter_dialogs: object


def _fetchone_tuple(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    row = cast(tuple[object, ...] | None, conn.execute(sql, params).fetchone())
    assert row is not None
    return row


def _fetchall_tuples(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    rows = conn.execute(sql, params).fetchall()
    return cast(list[tuple[object, ...]], rows)


def make_server(conn: sqlite3.Connection, client: object) -> DaemonAPIServer:
    shutdown_event = asyncio.Event()
    return DaemonAPIServer(conn, cast(_DaemonClientLike, client), shutdown_event)


def _call_kwargs(mock: object) -> dict[str, object]:
    return cast(dict[str, object], cast(AsyncMock, mock).call_args.kwargs)


# ---------------------------------------------------------------------------
# Task 1: injected reaction freshener tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jit_cold_fetch_updates_state(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """AC-3: 30 cold ids → 1 get_messages, 30 freshness rows, 30 reaction rows."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 31))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)

    client = _TestClient()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in ids])
    server = make_server(conn, client)

    await server._reaction_freshener.refresh(dialog_id, dialog_id, ids)

    assert cast(AsyncMock, client.get_messages).call_count == 1
    assert cast(list[int], _call_kwargs(client.get_messages)["ids"]) == ids

    fresh = _fetchone_tuple(
        conn,
        "SELECT COUNT(*) FROM message_reactions_freshness WHERE dialog_id=?",
        (dialog_id,),
    )[0]
    assert fresh == 30

    rxn = _fetchone_tuple(conn, "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=?", (dialog_id,))[0]
    assert rxn == 30


@pytest.mark.asyncio
async def test_jit_all_fresh_no_api_call(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """AC-4: all 30 ids fresh in TTL → zero get_messages, DB untouched."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 31))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
    now = int(time.time())
    _seed_freshness(conn, dialog_id, ids, now - 100)

    client = _TestClient()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)

    await server._reaction_freshener.refresh(dialog_id, dialog_id, ids)

    assert cast(AsyncMock, client.get_messages).call_count == 0
    rxn = _fetchone_tuple(conn, "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=?", (dialog_id,))[0]
    assert rxn == 0


@pytest.mark.asyncio
async def test_jit_page1_fresh_page2_cold_partial_fetch(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """AC-4-PAGED: page1 ids 1..30 fresh; ids 1..60 requested → fetch only 31..60."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids_all = list(range(1, 61))
    for mid in ids_all:
        _seed_message(conn, dialog_id, mid)
    now = int(time.time())
    _seed_freshness(conn, dialog_id, list(range(1, 31)), now - 100)

    client = _TestClient()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in range(31, 61)])
    server = make_server(conn, client)

    await server._reaction_freshener.refresh(dialog_id, dialog_id, ids_all)

    assert cast(AsyncMock, client.get_messages).call_count == 1
    call_ids = cast(list[int], _call_kwargs(client.get_messages)["ids"])
    assert call_ids == list(range(31, 61))
    assert len(call_ids) == 30

    # page 1 freshness rows unchanged
    p1_checked = _fetchall_tuples(
        conn,
        "SELECT checked_at FROM message_reactions_freshness WHERE dialog_id=? AND message_id<=30",
        (dialog_id,),
    )
    assert all(c[0] == now - 100 for c in p1_checked)

    # page 2 upserted
    p2_count = _fetchone_tuple(
        conn,
        "SELECT COUNT(*) FROM message_reactions_freshness WHERE dialog_id=? AND message_id>30",
        (dialog_id,),
    )[0]
    assert p2_count == 30


@pytest.mark.asyncio
async def test_jit_partial_stale_subset_fetch(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """30 requested, 10 fresh, 20 stale → get_messages with 20 ids."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 31))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
    now = int(time.time())
    fresh_ids = ids[:10]
    stale_ids = ids[10:]
    _seed_freshness(conn, dialog_id, fresh_ids, now - 50)

    client = _TestClient()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in stale_ids])
    server = make_server(conn, client)

    await server._reaction_freshener.refresh(dialog_id, dialog_id, ids)

    call_ids = cast(list[int], _call_kwargs(client.get_messages)["ids"])
    assert call_ids == stale_ids
    assert len(call_ids) == 20

    # freshness upserts only for the 20 stale (10 originals stay)
    upserts = _fetchall_tuples(
        conn, "SELECT message_id FROM message_reactions_freshness WHERE dialog_id=?", (dialog_id,)
    )
    upsert_ids = sorted(cast(int, r[0]) for r in upserts)
    assert upsert_ids == ids


@pytest.mark.asyncio
async def test_jit_ttl_expired_refreshes_no_duplicates(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """AC-5: TTL expired → refetch; no duplicates; freshness rows updated."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 6))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
        _seed_reaction(conn, dialog_id, mid, "❤", 1)
    expired = int(time.time()) - 700
    _seed_freshness(conn, dialog_id, ids, expired)

    client = _TestClient()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid, emoji="❤", count=1) for mid in ids])
    server = make_server(conn, client)

    await server._reaction_freshener.refresh(dialog_id, dialog_id, ids)

    assert cast(AsyncMock, client.get_messages).call_count == 1
    rxn_rows = _fetchall_tuples(
        conn,
        "SELECT message_id, emoji, count FROM message_reactions WHERE dialog_id=? ORDER BY message_id",
        (dialog_id,),
    )
    assert [(cast(int, r[0]), cast(str, r[1]), cast(int, r[2])) for r in rxn_rows] == [(mid, "❤", 1) for mid in ids]

    # freshness updated to ~now
    now = int(time.time())
    fr = _fetchall_tuples(
        conn, "SELECT message_id, checked_at FROM message_reactions_freshness WHERE dialog_id=?", (dialog_id,)
    )
    for _mid, ca in fr:
        assert cast(int, ca) >= now - 5
        assert cast(int, ca) > expired


@pytest.mark.asyncio
async def test_jit_floodwait_preserves_stale(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """AC-6: FloodWait → no reactions mutation, no freshness upsert, warning logged."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 6))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
        _seed_reaction(conn, dialog_id, mid, "👍", 2)
    expired = int(time.time()) - 700
    _seed_freshness(conn, dialog_id, ids, expired)

    client = _TestClient()
    err = FloodWaitError(request=None)
    err.seconds = 30
    client.get_messages = AsyncMock(side_effect=err)
    server = make_server(conn, client)

    freshness = await server._reaction_freshener.refresh(dialog_id, dialog_id, ids)

    # reactions untouched
    rxn_rows = _fetchall_tuples(conn, "SELECT count FROM message_reactions WHERE dialog_id=?", (dialog_id,))
    assert all(r[0] == 2 for r in rxn_rows)
    assert len(rxn_rows) == 5

    # freshness rows still at old `expired` value
    fr = _fetchall_tuples(conn, "SELECT checked_at FROM message_reactions_freshness WHERE dialog_id=?", (dialog_id,))
    assert all(cast(int, c[0]) == expired for c in fr)

    assert freshness.status == "flood_wait"


@pytest.mark.asyncio
async def test_jit_partial_none_results_skip_freshness_upsert(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """AC-6-PARTIAL: None entries get NO freshness row, no reactions mutation."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = [1, 2, 3, 4, 5]
    for mid in ids:
        _seed_message(conn, dialog_id, mid)

    client = _TestClient()
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

    await server._reaction_freshener.refresh(dialog_id, dialog_id, ids)

    fr_ids = sorted(
        cast(int, r[0])
        for r in _fetchall_tuples(
            conn,
            "SELECT message_id FROM message_reactions_freshness WHERE dialog_id=?",
            (dialog_id,),
        )
    )
    assert fr_ids == [1, 3, 5]

    rxn_ids = sorted(
        cast(int, r[0])
        for r in _fetchall_tuples(
            conn,
            "SELECT DISTINCT message_id FROM message_reactions WHERE dialog_id=?",
            (dialog_id,),
        )
    )
    assert rxn_ids == [1, 3, 5]


@pytest.mark.asyncio
async def test_jit_empty_message_ids_early_returns(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    conn = make_synced_db()
    _seed_synced(conn, 1001)
    client = _TestClient()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)

    await server._reaction_freshener.refresh(1001, 1001, [])

    assert cast(AsyncMock, client.get_messages).call_count == 0


@pytest.mark.asyncio
async def test_jit_unsynced_dialog_early_returns(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    conn = make_synced_db()
    # No synced_dialogs row
    client = _TestClient()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)

    await server._reaction_freshener.refresh(1001, 1001, [1, 2, 3])

    assert cast(AsyncMock, client.get_messages).call_count == 0


@pytest.mark.asyncio
async def test_jit_fetch_window_never_expanded(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """5 stale ids → get_messages called with exactly those 5, not more."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    # Seed many messages but only ask about 5
    for mid in range(1, 100):
        _seed_message(conn, dialog_id, mid)
    ask_ids = [10, 20, 30, 40, 50]

    client = _TestClient()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in ask_ids])
    server = make_server(conn, client)

    await server._reaction_freshener.refresh(dialog_id, dialog_id, ask_ids)

    assert cast(AsyncMock, client.get_messages).call_count == 1
    assert cast(list[int], _call_kwargs(client.get_messages)["ids"]) == ask_ids


@pytest.mark.asyncio
async def test_jit_reactions_cleared_when_telegram_has_none(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """msg.reactions=None should clear cached rows (apply_reactions_delta with [])."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    _seed_message(conn, dialog_id, 1)
    _seed_reaction(conn, dialog_id, 1, "❤", 5)

    client = _TestClient()
    client.get_messages = AsyncMock(return_value=[_msg_no_reactions(1)])
    server = make_server(conn, client)

    await server._reaction_freshener.refresh(dialog_id, dialog_id, [1])

    rxn = _fetchone_tuple(
        conn, "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=? AND message_id=1", (dialog_id,)
    )[0]
    assert rxn == 0

    fr = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT checked_at FROM message_reactions_freshness WHERE dialog_id=? AND message_id=1", (dialog_id,)
        ).fetchone(),
    )
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
async def test_list_messages_triggers_jit_on_cold_read(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """AC-3 end-to-end through _list_messages: one get_messages, reactions in response."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    for mid in range(1, 6):
        _seed_message(conn, dialog_id, mid)

    client = _TestClient()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid, "❤", 2) for mid in range(1, 6)])
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "list_messages", "dialog_id": dialog_id, "limit": 10})

    assert result["ok"] is True
    assert cast(AsyncMock, client.get_messages).call_count == 1
    msgs = cast(list[dict[str, object]], cast(dict[str, object], result["data"])["messages"])
    assert msgs
    assert any(m.get("reactions_display") for m in msgs)


@pytest.mark.asyncio
async def test_list_messages_skips_jit_when_all_fresh(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """AC-4 through dispatcher: pre-seeded fresh → zero get_messages."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    ids = list(range(1, 6))
    for mid in ids:
        _seed_message(conn, dialog_id, mid)
    _seed_freshness(conn, dialog_id, ids, int(time.time()) - 50)

    client = _TestClient()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "list_messages", "dialog_id": dialog_id, "limit": 10})

    assert result["ok"] is True
    assert cast(AsyncMock, client.get_messages).call_count == 0


@pytest.mark.asyncio
async def test_list_messages_page1_fresh_page2_cold(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """AC-4-PAGED end-to-end: simulate by pre-seeding freshness for half ids."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    for mid in range(1, 11):
        _seed_message(conn, dialog_id, mid)
    # mark first 5 as fresh
    _seed_freshness(conn, dialog_id, [1, 2, 3, 4, 5], int(time.time()) - 50)

    client = _TestClient()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in [10, 9, 8, 7, 6]])
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "list_messages", "dialog_id": dialog_id, "limit": 10})

    assert result["ok"] is True
    assert cast(AsyncMock, client.get_messages).call_count == 1
    # Only the 5 cold ids fetched
    fetched_ids = sorted(cast(list[int], _call_kwargs(client.get_messages)["ids"]))
    assert fetched_ids == [6, 7, 8, 9, 10]


@pytest.mark.asyncio
async def test_list_messages_context_window_wiring(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """Context-window path triggers JIT for the surrounding slice."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    for mid in range(1, 11):
        _seed_message(conn, dialog_id, mid)

    client = _TestClient()
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
    assert cast(AsyncMock, client.get_messages).call_count == 1


@pytest.mark.asyncio
async def test_search_messages_scoped_triggers_jit(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """Scoped search (dialog_id provided) triggers JIT freshen."""
    conn = make_synced_db()
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

    client = _TestClient()
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
    assert cast(AsyncMock, client.get_messages).call_count == 1


@pytest.mark.asyncio
async def test_search_messages_global_skips_jit(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """Global search (dialog_id=None) does NOT trigger JIT."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    for mid in range(1, 4):
        _seed_message(conn, dialog_id, mid)
        conn.execute(
            "INSERT INTO messages_fts (dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
            (dialog_id, mid, f"hello {mid}"),
        )
    conn.commit()

    client = _TestClient()
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
    assert cast(AsyncMock, client.get_messages).call_count == 0


@pytest.mark.asyncio
async def test_list_unread_messages_injects_reactions(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """Unread path now surfaces reactions_display populated from message_reactions."""
    conn = make_synced_db()
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

    client = _TestClient()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "get_inbox", "scope": "personal", "limit": 100})

    assert result["ok"] is True
    groups = cast(list[dict[str, object]], cast(dict[str, object], result["data"])["groups"])
    assert len(groups) == 1
    msgs = cast(list[dict[str, object]], groups[0]["messages"])
    assert msgs
    assert all("reactions_display" in m for m in msgs)
    assert all(m["reactions_display"] for m in msgs)
    freshness = cast(dict[str, object], groups[0]["reaction_freshness"])
    assert freshness == {
        "requested_count": 3,
        "fresh_count": 3,
        "stale_count": 0,
        "refreshed_count": 0,
        "status": "fresh",
        "retry_after": None,
    }


@pytest.mark.asyncio
async def test_unread_group_without_stored_messages_omits_freshness(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    """An allocated but empty group has no reaction-freshness claim."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)

    client = _TestClient()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)
    server.self_id = 99

    groups = await server._fetch_unread_groups(
        [
            {
                "chat_id": dialog_id,
                "display_name": "Alice",
                "tier": 1,
                "category": "personal",
                "unread_count": 1,
                "unread_mentions_count": 0,
                "read_inbox_max_id": 0,
            }
        ],
        {dialog_id: 1},
    )

    assert groups[0]["messages"] == []
    assert "reaction_freshness" not in groups[0]
    assert cast(AsyncMock, client.get_messages).call_count == 0


@pytest.mark.asyncio
async def test_list_unread_messages_triggers_jit_on_cold_read(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """Unread path JIT wiring: cold read fires get_messages."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    conn.execute(
        "UPDATE synced_dialogs SET read_inbox_max_id=10 WHERE dialog_id=?",
        (dialog_id,),
    )
    for mid in [11, 12, 13]:
        _seed_message(conn, dialog_id, mid)
    conn.commit()

    client = _TestClient()
    client.get_messages = AsyncMock(return_value=[_msg_with_reactions(mid) for mid in [13, 12, 11]])
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "get_inbox", "scope": "personal", "limit": 100})

    assert result["ok"] is True
    assert cast(AsyncMock, client.get_messages).call_count == 1


@pytest.mark.asyncio
async def test_list_unread_messages_skips_jit_when_all_fresh(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """TTL gate on unread path: pre-fresh → zero get_messages."""
    conn = make_synced_db()
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

    client = _TestClient()
    client.get_messages = AsyncMock()
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "get_inbox", "scope": "personal", "limit": 100})

    assert result["ok"] is True
    assert cast(AsyncMock, client.get_messages).call_count == 0


@pytest.mark.asyncio
async def test_non_content_methods_do_not_trigger_jit(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """get_sync_status, list_dialogs etc. → zero JIT calls."""
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)

    client = _TestClient()
    client.get_messages = AsyncMock()

    # iter_dialogs needs to be an async iterator; return empty
    async def _empty(*_a: object, **_k: object) -> None:
        if False:
            yield  # pragma: no cover

    client.iter_dialogs = MagicMock(side_effect=_empty)
    server = make_server(conn, client)
    server.self_id = 99

    result = await server._dispatch({"method": "get_sync_status", "dialog_id": dialog_id})
    assert result["ok"] is True
    assert cast(AsyncMock, client.get_messages).call_count == 0
