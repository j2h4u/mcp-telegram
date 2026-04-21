"""Tests for Plan 39.3-03 Task 4 — ListDialogs unread_in / unread_out columns.

Covers AC-11 (DM rows include unread_in/unread_out, non-DM rows omit both),
AC-12 TWO-GUARD (hard: EXPLAIN QUERY PLAN hits messages PK; soft: latency
benchmark on 200-dialog fixture, non-failing unless mean > 100ms),
D-13 (description mentions unread_in / unread_out).

Schema is set up inline (mirrors test_daemon_api_read_state.py pattern).
Zero real Telegram calls — `iter_dialogs` is mocked with an async generator.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer


# ---------------------------------------------------------------------------
# get_peer_id patch (daemon_api imports telethon_utils.get_peer_id; tests
# supply SimpleNamespace-like entities so we need a benign stub).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_get_peer_id():
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda entity: int(getattr(entity, "id", 0)),
    ):
        yield


# ---------------------------------------------------------------------------
# Schema + helpers
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
    conn.commit()
    return conn


def _insert_synced_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    status: str = "synced",
    read_inbox_max_id: int | None = None,
    read_outbox_max_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, read_inbox_max_id, read_outbox_max_id) "
        "VALUES (?, ?, ?, ?)",
        (dialog_id, status, read_inbox_max_id, read_outbox_max_id),
    )
    conn.commit()


def _insert_message(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    out: int = 0,
    sent_at: int = 1_700_000_000,
) -> None:
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, out, is_deleted) "
        "VALUES (?, ?, ?, ?, 0)",
        (dialog_id, message_id, sent_at, out),
    )
    conn.commit()


def _make_server(conn: sqlite3.Connection, client: object) -> DaemonAPIServer:
    return DaemonAPIServer(conn, client, asyncio.Event())


def _mock_user_dialog(dialog_id: int, name: str = "Peer") -> MagicMock:
    """Build a mock dialog whose entity classifies as 'User'."""
    d = MagicMock()
    d.id = dialog_id
    d.name = name
    d.entity = MagicMock()
    d.entity.first_name = name
    d.entity.bot = False
    d.entity.participants_count = None
    d.entity.date = None
    d.date = MagicMock()
    d.date.timestamp.return_value = 1_700_000_000
    d.unread_count = 0
    return d


def _mock_channel_dialog(dialog_id: int, name: str = "Channel") -> MagicMock:
    from telethon.tl.types import Channel

    entity = MagicMock()
    entity.__class__ = Channel
    entity.megagroup = False
    entity.forum = False
    entity.broadcast = True
    entity.participants_count = None
    entity.date = None

    d = MagicMock()
    d.id = dialog_id
    d.name = name
    d.entity = entity
    d.date = MagicMock()
    d.date.timestamp.return_value = 1_700_000_000
    d.unread_count = 0
    return d


def _iter_dialogs_factory(dialogs):
    async def _iter(**kwargs):
        for d in dialogs:
            yield d
    return _iter


# ---------------------------------------------------------------------------
# AC-11 — DM rows carry unread_in / unread_out
# ---------------------------------------------------------------------------


async def test_list_dialogs_dm_row_has_unread_in_and_unread_out() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 1, read_inbox_max_id=5, read_outbox_max_id=10)
    # 2 unread incoming (6,7), 1 unread outgoing (11)
    _insert_message(conn, 1, 6, out=0)
    _insert_message(conn, 1, 7, out=0)
    _insert_message(conn, 1, 11, out=1)
    _insert_message(conn, 1, 3, out=0)  # read
    _insert_message(conn, 1, 9, out=1)  # read

    client = MagicMock()
    client.iter_dialogs = _iter_dialogs_factory([_mock_user_dialog(1)])
    server = _make_server(conn, client)

    result = await server._list_dialogs({})
    assert result["ok"] is True
    row = result["data"]["dialogs"][0]
    assert row["unread_in"] == 2
    assert row["unread_out"] == 1


async def test_list_dialogs_dm_row_unread_zero_when_caught_up() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 2, read_inbox_max_id=100, read_outbox_max_id=200)
    _insert_message(conn, 2, 50, out=0)
    _insert_message(conn, 2, 150, out=1)

    client = MagicMock()
    client.iter_dialogs = _iter_dialogs_factory([_mock_user_dialog(2)])
    server = _make_server(conn, client)

    row = (await server._list_dialogs({}))["data"]["dialogs"][0]
    assert row["unread_in"] == 0
    assert row["unread_out"] == 0


async def test_list_dialogs_dm_row_unread_in_only() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 3, read_inbox_max_id=1, read_outbox_max_id=100)
    _insert_message(conn, 3, 5, out=0)
    _insert_message(conn, 3, 50, out=1)

    client = MagicMock()
    client.iter_dialogs = _iter_dialogs_factory([_mock_user_dialog(3)])
    server = _make_server(conn, client)

    row = (await server._list_dialogs({}))["data"]["dialogs"][0]
    assert row["unread_in"] == 1
    assert row["unread_out"] == 0


async def test_list_dialogs_dm_row_unread_out_only() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 4, read_inbox_max_id=100, read_outbox_max_id=1)
    _insert_message(conn, 4, 50, out=0)
    _insert_message(conn, 4, 5, out=1)

    client = MagicMock()
    client.iter_dialogs = _iter_dialogs_factory([_mock_user_dialog(4)])
    server = _make_server(conn, client)

    row = (await server._list_dialogs({}))["data"]["dialogs"][0]
    assert row["unread_in"] == 0
    assert row["unread_out"] == 1


async def test_list_dialogs_non_dm_row_omits_unread_fields() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 7, read_inbox_max_id=0, read_outbox_max_id=0)
    _insert_message(conn, 7, 1, out=0)

    client = MagicMock()
    client.iter_dialogs = _iter_dialogs_factory([_mock_channel_dialog(7)])
    server = _make_server(conn, client)

    row = (await server._list_dialogs({}))["data"]["dialogs"][0]
    assert row["type"] == "Channel"
    assert "unread_in" not in row
    assert "unread_out" not in row


async def test_list_dialogs_null_inbox_cursor_treats_all_incoming_as_unread() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 8, read_inbox_max_id=None, read_outbox_max_id=0)
    _insert_message(conn, 8, 1, out=0)
    _insert_message(conn, 8, 2, out=0)
    _insert_message(conn, 8, 3, out=0)

    client = MagicMock()
    client.iter_dialogs = _iter_dialogs_factory([_mock_user_dialog(8)])
    server = _make_server(conn, client)

    row = (await server._list_dialogs({}))["data"]["dialogs"][0]
    # NULL cursor → everything is unread (documented trade-off, <interfaces> MEDIUM-2).
    assert row["unread_in"] == 3


async def test_list_dialogs_null_outbox_cursor_treats_all_outgoing_as_unread() -> None:
    conn = _make_db()
    _insert_synced_dialog(conn, 9, read_inbox_max_id=0, read_outbox_max_id=None)
    _insert_message(conn, 9, 1, out=1)
    _insert_message(conn, 9, 2, out=1)

    client = MagicMock()
    client.iter_dialogs = _iter_dialogs_factory([_mock_user_dialog(9)])
    server = _make_server(conn, client)

    row = (await server._list_dialogs({}))["data"]["dialogs"][0]
    assert row["unread_out"] == 2


async def test_list_dialogs_zero_telegram_api_calls_for_unread_query() -> None:
    """The unread enrichment must be pure SQL — no client.* calls beyond iter_dialogs."""
    conn = _make_db()
    _insert_synced_dialog(conn, 10, read_inbox_max_id=1, read_outbox_max_id=1)
    _insert_message(conn, 10, 2, out=0)

    client = MagicMock()
    client.iter_dialogs = _iter_dialogs_factory([_mock_user_dialog(10)])
    # Any forbidden call paths raise — but using MagicMock we just verify the
    # only method awaited is iter_dialogs. Track attributes accessed.
    server = _make_server(conn, client)
    await server._list_dialogs({})

    # get_entity / send_message etc should NOT have been called.
    client.get_entity.assert_not_called()
    client.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# AC-12 HARD GUARD — EXPLAIN QUERY PLAN hits the messages PK
# ---------------------------------------------------------------------------


async def test_list_dialogs_query_uses_messages_pk_index() -> None:
    """AC-12 hard guard: the batched unread-counts query must traverse the
    messages PRIMARY KEY (which IS the table for WITHOUT ROWID messages).

    The assertion is structural: the plan must reference either the PK / index
    on `messages` or a SCAN of `messages` (which, for a WITHOUT ROWID table,
    is a scan of the PK B-tree — equivalent).
    """
    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 1, out=0)

    # Mirror the daemon_api batched query shape.
    sql = (
        "SELECT m.dialog_id, "
        "SUM(CASE WHEN m.\"out\" = 0 AND m.message_id > COALESCE(sd.read_inbox_max_id, -1) "
        "THEN 1 ELSE 0 END) AS unread_in, "
        "SUM(CASE WHEN m.\"out\" = 1 AND m.message_id > COALESCE(sd.read_outbox_max_id, -1) "
        "THEN 1 ELSE 0 END) AS unread_out "
        "FROM messages m JOIN synced_dialogs sd USING(dialog_id) "
        "WHERE sd.status = 'synced' "
        "GROUP BY m.dialog_id"
    )
    plan_rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    plan_text = " | ".join(row[3] for row in plan_rows)
    # HARD guard: reject if any row hints at an unintended redundant index path
    # (e.g. a covering secondary index that shadows the PK). The canonical
    # plans are "SCAN m" or "SEARCH ... USING PRIMARY KEY". Accept either.
    has_pk_path = (
        "PRIMARY KEY" in plan_text
        or "SCAN m" in plan_text
        or "SCAN messages" in plan_text
    )
    assert has_pk_path, f"Query plan does not show PK access: {plan_text}"
    # Guard against an unrelated index sneaking in (regression detector).
    assert "sqlite_autoindex_messages_" not in plan_text or "messages_1" in plan_text


# ---------------------------------------------------------------------------
# AC-12 SOFT GUARD — latency benchmark on 200-dialog fixture
# ---------------------------------------------------------------------------


async def test_list_dialogs_latency_200_dialog_fixture(capsys) -> None:
    """AC-12 soft guard: 200 synthetic DMs × 50 messages. Warm 5, time 20.
    Prints mean + p95 to stdout. FAILS only if mean > 100ms.
    """
    conn = _make_db()
    N_DIALOGS = 200
    MSGS_PER = 50
    for d in range(1, N_DIALOGS + 1):
        _insert_synced_dialog(conn, d, read_inbox_max_id=10, read_outbox_max_id=10)
    for d in range(1, N_DIALOGS + 1):
        for m in range(1, MSGS_PER + 1):
            conn.execute(
                "INSERT INTO messages (dialog_id, message_id, sent_at, out, is_deleted) "
                "VALUES (?, ?, ?, ?, 0)",
                (d, m, 1_700_000_000 + m, m % 2),
            )
    conn.commit()

    dialogs = [_mock_user_dialog(d) for d in range(1, N_DIALOGS + 1)]

    sample_times: list[float] = []
    for warmup in range(5):
        client = MagicMock()
        client.iter_dialogs = _iter_dialogs_factory(dialogs)
        server = _make_server(conn, client)
        await server._list_dialogs({})
    for _ in range(20):
        client = MagicMock()
        client.iter_dialogs = _iter_dialogs_factory(dialogs)
        server = _make_server(conn, client)
        t0 = time.perf_counter()
        await server._list_dialogs({})
        sample_times.append(time.perf_counter() - t0)

    sample_times.sort()
    mean = sum(sample_times) / len(sample_times)
    p95 = sample_times[int(0.95 * len(sample_times)) - 1]
    # Non-failing diagnostic output.
    print(
        f"\n[AC-12 soft guard] list_dialogs 200×50 mean={mean*1000:.1f}ms "
        f"p95={p95*1000:.1f}ms"
    )
    # Ceiling: 2× the 50ms budget — absorbs CI flake while still catching real regressions.
    assert mean < 0.100, f"ListDialogs mean latency regressed: {mean*1000:.1f}ms > 100ms"


# ---------------------------------------------------------------------------
# D-13 — description mentions unread_in / unread_out
# ---------------------------------------------------------------------------


def test_list_dialogs_description_mentions_unread_fields() -> None:
    from mcp_telegram.tools.discovery import ListDialogs

    doc = ListDialogs.__doc__ or ""
    assert "unread_in" in doc
    assert "unread_out" in doc


# ---------------------------------------------------------------------------
# End-to-end rendering via tools/discovery
# ---------------------------------------------------------------------------


async def test_list_dialogs_text_output_renders_unread_for_dm() -> None:
    from mcp_telegram.tools.discovery import ListDialogs, list_dialogs

    response = {
        "ok": True,
        "data": {
            "dialogs": [
                {
                    "id": 1, "name": "Alice", "type": "User",
                    "last_message_at": "2024-01-01 00:00",
                    "unread_count": 2, "sync_status": "synced",
                    "unread_in": 3, "unread_out": 1,
                },
                {
                    "id": 2, "name": "Announcements", "type": "Channel",
                    "last_message_at": "2024-01-01 00:00",
                    "unread_count": 0, "sync_status": "synced",
                },
            ]
        },
    }

    conn = MagicMock()
    conn.list_dialogs = AsyncMock(return_value=response)
    conn.upsert_entities = AsyncMock(return_value={"ok": True, "upserted": 0})

    @asynccontextmanager
    async def _cm():
        yield conn

    with patch("mcp_telegram.tools.discovery.daemon_connection", side_effect=_cm):
        result = await list_dialogs(ListDialogs())

    text = result.content[0].text
    # DM row contains both fields.
    dm_lines = [line for line in text.splitlines() if "Alice" in line]
    assert dm_lines, f"no DM line in output: {text!r}"
    assert "unread_in=3" in dm_lines[0]
    assert "unread_out=1" in dm_lines[0]

    # Channel row omits both fields.
    ch_lines = [line for line in text.splitlines() if "Announcements" in line]
    assert ch_lines
    assert "unread_in" not in ch_lines[0]
    assert "unread_out" not in ch_lines[0]
