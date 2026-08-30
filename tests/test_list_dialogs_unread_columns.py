"""Tests for Plan 39.3-03 Task 4 — ListDialogs unread_in / unread_out columns.

Covers AC-11 (DM rows include unread_in/unread_out, non-DM rows omit both),
AC-12 HARD GUARD (EXPLAIN QUERY PLAN hits messages PK),
AC-12 scaling+correctness (200-dialog SQL path, replaces removed iter_dialogs
latency benchmark — see commit body for rationale),
D-13 (description mentions unread_in / unread_out).

Phase 44 conversion: iter_dialogs mock helpers deleted; all WR-06 tests now
seed the dialogs table directly. AC-12 latency benchmark removed
(it pinned the iter_dialogs hot-path which is gone; replaced with
scales_to_200_dialogs correctness+scaling test covering both SQL query
performance and DM filtering at volume).

Schema is set up inline. Self-contained — does not cross-import from test_daemon_api.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.reading.sqlite_projection import _LIST_DIALOG_MESSAGE_AGGREGATES_SQL, _LIST_DIALOGS_SQL
from mcp_telegram.sync_read_model import build_sync_read_model
from tests.daemon_api_policy import make_daemon_api_policy
from tests.history_enrollment_helpers import seed_full_history_enrollment
from tests.reaction_helpers import make_reaction_freshener

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
# Schema + helpers (self-contained — no cross-import from test_daemon_api)
# ---------------------------------------------------------------------------


@contextmanager
def _make_db() -> Iterator[sqlite3.Connection]:
    """In-memory DB with full Phase 44 schema: synced_dialogs, messages,
    entities, message_forwards, and the dialogs snapshot table + 4 indexes."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            """
            CREATE TABLE synced_dialogs (
                dialog_id           INTEGER PRIMARY KEY,
                status              TEXT NOT NULL DEFAULT 'not_synced',
                last_synced_at      INTEGER,
                last_event_at       INTEGER,
                last_delta_checked_at INTEGER,
                delta_refresh_requested_at INTEGER,
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
            CREATE TABLE full_history_enrollment (
                dialog_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                source TEXT NOT NULL CHECK(source IN ('explicit', 'automatic', 'migration')),
                updated_at INTEGER NOT NULL
            ) WITHOUT ROWID
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
                media_kind          TEXT,
                media_payload       TEXT,
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
        # Phase 44: dialogs snapshot table + 4 indexes
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dialogs (
                dialog_id               INTEGER PRIMARY KEY,
                name                    TEXT,
                type                    TEXT,
                archived                INTEGER NOT NULL DEFAULT 0,
                pinned                  INTEGER NOT NULL DEFAULT 0,
                members                 INTEGER,
                created                 INTEGER,
                last_message_at         INTEGER,
                snapshot_at             INTEGER,
                hidden                  INTEGER NOT NULL DEFAULT 0,
                needs_refresh           INTEGER NOT NULL DEFAULT 0,
                unread_mentions_count   INTEGER NOT NULL DEFAULT 0,
                unread_reactions_count  INTEGER NOT NULL DEFAULT 0,
                unread_count            INTEGER,
                unread_mark             INTEGER,
                unread_count_observed_at INTEGER,
                unread_mark_observed_at INTEGER,
                draft_text              TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_hidden_pinned ON dialogs(hidden, pinned DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_type ON dialogs(type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_snapshot_at ON dialogs(snapshot_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dialogs_needs_refresh_hidden ON dialogs(needs_refresh, hidden)")
        conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        yield conn
    finally:
        conn.close()


def _insert_synced_dialog(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    status: str = "synced",
    enrollment_enabled: bool = False,
    read_inbox_max_id: int | None = None,
    read_outbox_max_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id, read_outbox_max_id) VALUES (?, ?, ?, ?)",
        (dialog_id, status, read_inbox_max_id, read_outbox_max_id),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=enrollment_enabled)
    conn.commit()


def _insert_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    name: str = "Peer",
    type_: str = "User",
) -> None:
    """Insert a row into the dialogs snapshot table."""
    conn.execute(
        "INSERT INTO dialogs (dialog_id, name, type, hidden) VALUES (?, ?, ?, 0)",
        (dialog_id, name, type_),
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
        "INSERT INTO messages (dialog_id, message_id, sent_at, out, is_deleted) VALUES (?, ?, ?, ?, 0)",
        (dialog_id, message_id, sent_at, out),
    )
    conn.commit()


def _make_server(conn: sqlite3.Connection, client: object) -> DaemonAPIServer:
    server = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, client),
        asyncio.Event(),
        reaction_freshener=make_reaction_freshener(conn, client),
        policy=make_daemon_api_policy(),
    )
    server._ready = True
    return server


class _TestClient(MagicMock):
    iter_dialogs: object
    get_entity: object
    send_message: object


def _dialog_rows(result: dict[str, object]) -> list[dict[str, object]]:
    data = cast(dict[str, object], result["data"])
    return cast(list[dict[str, object]], data["dialogs"])


def _assert_not_called(mock: object) -> None:
    cast(MagicMock, mock).assert_not_called()


# ---------------------------------------------------------------------------
# AC-11 — DM rows carry unread_in / unread_out (SQL path)
# ---------------------------------------------------------------------------


async def test_list_dialogs_dm_row_has_unread_in_and_unread_out() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 1, name="Peer", type_="User")
        _insert_synced_dialog(conn, 1, read_inbox_max_id=5, read_outbox_max_id=10)
        # 2 unread incoming (6,7), 1 unread outgoing (11)
        _insert_message(conn, 1, 6, out=0)
        _insert_message(conn, 1, 7, out=0)
        _insert_message(conn, 1, 11, out=1)
        _insert_message(conn, 1, 3, out=0)  # read
        _insert_message(conn, 1, 9, out=1)  # read

        client = _TestClient()
        server = _make_server(conn, client)

        result = await server._list_dialogs({})
        assert result["ok"] is True
        row = _dialog_rows(result)[0]
        assert row["unread_in"] == 2
        assert row["unread_out"] == 1


async def test_list_dialogs_unread_count_is_nullable_telegram_fact() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 11, name="Peer11", type_="User")
        conn.execute("UPDATE dialogs SET unread_count=7 WHERE dialog_id=11")
        _insert_synced_dialog(conn, 11, read_inbox_max_id=100, read_outbox_max_id=100)
        _insert_message(conn, 11, 101, out=0)
        conn.commit()

        row = _dialog_rows(await _make_server(conn, _TestClient())._list_dialogs({}))[0]
        assert row["unread_count"] == 7


async def test_list_dialogs_dm_row_unread_zero_when_caught_up() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 2, name="Peer2", type_="User")
        _insert_synced_dialog(conn, 2, read_inbox_max_id=100, read_outbox_max_id=200)
        _insert_message(conn, 2, 50, out=0)
        _insert_message(conn, 2, 150, out=1)

        client = _TestClient()
        server = _make_server(conn, client)

        row = _dialog_rows(await server._list_dialogs({}))[0]
        assert row["unread_in"] == 0
        assert row["unread_out"] == 0


async def test_list_dialogs_dm_row_unread_in_only() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 3, name="Peer3", type_="User")
        _insert_synced_dialog(conn, 3, read_inbox_max_id=1, read_outbox_max_id=100)
        _insert_message(conn, 3, 5, out=0)
        _insert_message(conn, 3, 50, out=1)

        client = _TestClient()
        server = _make_server(conn, client)

        row = _dialog_rows(await server._list_dialogs({}))[0]
        assert row["unread_in"] == 1
        assert row["unread_out"] == 0


async def test_list_dialogs_dm_row_unread_out_only() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 4, name="Peer4", type_="User")
        _insert_synced_dialog(conn, 4, read_inbox_max_id=100, read_outbox_max_id=1)
        _insert_message(conn, 4, 50, out=0)
        _insert_message(conn, 4, 5, out=1)

        client = _TestClient()
        server = _make_server(conn, client)

        row = _dialog_rows(await server._list_dialogs({}))[0]
        assert row["unread_in"] == 0
        assert row["unread_out"] == 1


async def test_list_dialogs_non_dm_row_omits_unread_fields() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 7, name="News Channel", type_="Channel")
        _insert_synced_dialog(conn, 7, read_inbox_max_id=0, read_outbox_max_id=0)
        _insert_message(conn, 7, 1, out=0)

        client = _TestClient()
        server = _make_server(conn, client)

        row = (await server._list_dialogs({}))["data"]["dialogs"][0]
        assert row["type"] == "Channel"
        assert "unread_in" not in row
        assert "unread_out" not in row


async def test_list_dialogs_null_inbox_cursor_treats_all_incoming_as_unread() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 8, name="Peer8", type_="User")
        _insert_synced_dialog(conn, 8, read_inbox_max_id=None, read_outbox_max_id=0)
        _insert_message(conn, 8, 1, out=0)
        _insert_message(conn, 8, 2, out=0)
        _insert_message(conn, 8, 3, out=0)

        client = _TestClient()
        server = _make_server(conn, client)

        row = (await server._list_dialogs({}))["data"]["dialogs"][0]
        # NULL cursor -> everything is unread (documented trade-off, <interfaces> MEDIUM-2).
        assert row["unread_in"] == 3


async def test_list_dialogs_null_outbox_cursor_treats_all_outgoing_as_unread() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 9, name="Peer9", type_="User")
        _insert_synced_dialog(conn, 9, read_inbox_max_id=0, read_outbox_max_id=None)
        _insert_message(conn, 9, 1, out=1)
        _insert_message(conn, 9, 2, out=1)

        client = _TestClient()
        server = _make_server(conn, client)

        row = (await server._list_dialogs({}))["data"]["dialogs"][0]
        assert row["unread_out"] == 2


async def test_list_dialogs_zero_telegram_api_calls_for_unread_query() -> None:
    """The unread enrichment must be pure SQL — no Telegram client calls at all."""
    with _make_db() as conn:
        _insert_dialog(conn, 10, name="Peer10", type_="User")
        _insert_synced_dialog(conn, 10, read_inbox_max_id=1, read_outbox_max_id=1)
        _insert_message(conn, 10, 2, out=0)

        client = _TestClient()
        server = _make_server(conn, client)
        await server._list_dialogs({})

    # No Telegram API calls: iter_dialogs, get_entity, send_message etc all absent.
    cast(MagicMock, client.iter_dialogs).assert_not_called()
    cast(MagicMock, client.get_entity).assert_not_called()
    cast(MagicMock, client.send_message).assert_not_called()


# ---------------------------------------------------------------------------
# AC-12 HARD GUARD — EXPLAIN QUERY PLAN hits the messages PK
# ---------------------------------------------------------------------------


async def test_list_dialogs_candidate_aggregate_filters_before_messages() -> None:  # noqa: PLR0915
    """Final dialog selection drives one bounded aggregate query."""
    with _make_db() as conn:
        _insert_dialog(conn, 1, name="Target peer")
        _insert_synced_dialog(conn, 1, read_inbox_max_id=10, read_outbox_max_id=20)
        # Three non-deleted rows count locally, but service and deleted rows do
        # not contribute to unread columns.
        _insert_message(conn, 1, 1, out=0)
        _insert_message(conn, 1, 11, out=0)
        _insert_message(conn, 1, 21, out=1)
        conn.execute("UPDATE messages SET is_service = 1 WHERE dialog_id = 1 AND message_id = 21")
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, out, is_deleted) VALUES (1, 22, 1700000000, 1, 1)"
        )
        _insert_dialog(conn, 4, name="Анна Петрова")
        _insert_synced_dialog(conn, 4, read_inbox_max_id=0, read_outbox_max_id=0)
        _insert_message(conn, 4, 1, out=0)
        _insert_message(conn, 4, 2, out=1)
        _insert_dialog(conn, 3, name="No messages")

        # Visible distractor: approximately 100k rows outside each selected set.
        _insert_dialog(conn, 2, name="Distractor")
        conn.executemany(
            "INSERT INTO messages (dialog_id, message_id, sent_at, out, is_deleted) VALUES (2, ?, 1700000000, 0, 0)",
            ((message_id,) for message_id in range(1, 100_001)),
        )
        conn.commit()

        server = _make_server(conn, _TestClient())
        trace: list[str] = []
        conn.set_trace_callback(trace.append)
        result = await server._list_dialogs({"filter": "tp"})
        conn.set_trace_callback(None)
        assert result["ok"] is True
        rows = _dialog_rows(result)
        assert [row["id"] for row in rows] == [1]
        assert rows[0]["saved_message_count"] == 3
        assert rows[0]["unread_in"] == 1
        assert rows[0]["unread_out"] == 0
        aggregate_trace = [statement for statement in trace if "json_each" in statement]
        assert len(aggregate_trace) == 1
        assert "json_each('[1]')" in aggregate_trace[0]

        trace.clear()
        conn.set_trace_callback(trace.append)
        result = await server._list_dialogs({"filter": "АП"})
        conn.set_trace_callback(None)
        assert result["ok"] is True
        rows = _dialog_rows(result)
        assert [row["id"] for row in rows] == [4]
        assert rows[0]["saved_message_count"] == 2
        assert rows[0]["unread_in"] == 1
        assert rows[0]["unread_out"] == 1
        aggregate_trace = [statement for statement in trace if "json_each" in statement]
        assert len(aggregate_trace) == 1
        assert "json_each('[4]')" in aggregate_trace[0]

        metadata_params = {"archived_filter": None, "pinned_filter": None, "name_pat": "%target%"}
        metadata_plan = cast(
            list[tuple[object, ...]],
            conn.execute("EXPLAIN QUERY PLAN " + _LIST_DIALOGS_SQL, metadata_params).fetchall(),
        )
        metadata_details = [cast(str, row[3]) for row in metadata_plan]
        assert not any(
            re.search(r"\b(?:SCAN|SEARCH) m\b|messages", detail, re.IGNORECASE) for detail in metadata_details
        )

        conn.row_factory = sqlite3.Row
        aggregate_params = {"dialog_ids_json": "[1]"}
        aggregate_plan = cast(
            list[tuple[object, ...]],
            conn.execute("EXPLAIN QUERY PLAN " + _LIST_DIALOG_MESSAGE_AGGREGATES_SQL, aggregate_params).fetchall(),
        )
        aggregate_details = [cast(str, row[3]) for row in aggregate_plan]
        message_access = [detail for detail in aggregate_details if re.search(r"\b(?:SCAN|SEARCH) m\b", detail)]
        assert len(message_access) == 1
        assert "SEARCH" in message_access[0]
        assert "dialog_id=?" in message_access[0]
        assert "SCAN m" not in message_access[0]
        no_message_row = cast(
            sqlite3.Row | None,
            conn.execute(
                _LIST_DIALOG_MESSAGE_AGGREGATES_SQL,
                {"dialog_ids_json": "[3]"},
            ).fetchone(),
        )
        assert no_message_row is not None
        assert tuple(no_message_row) == (3, 0, 0, 0)

        vm_steps = 0

        def progress() -> int:
            nonlocal vm_steps
            vm_steps += 1_000
            return int(vm_steps > 100_000)

        conn.set_progress_handler(progress, 1_000)
        try:
            conn.execute(_LIST_DIALOG_MESSAGE_AGGREGATES_SQL, aggregate_params).fetchall()
        finally:
            conn.set_progress_handler(None, 0)
        assert vm_steps < 100_000


async def test_list_dialogs_empty_final_selection_skips_message_aggregate() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 1, name="Visible peer")
        conn.commit()
        trace: list[str] = []
        conn.set_trace_callback(trace.append)
        result = await _make_server(conn, _TestClient())._list_dialogs({"filter": "no-such-peer"})
        conn.set_trace_callback(None)

        assert result["ok"] is True
        assert _dialog_rows(result) == []
        assert not any("json_each" in statement for statement in trace)


# ---------------------------------------------------------------------------
# AC-12 SCALING + CORRECTNESS — 200-dialog SQL path (replaces latency benchmark)
#
# The original AC-12 soft guard benchmarked the iter_dialogs hot-path.
# That path is gone (Phase 44 rewrite). A new SQL-path benchmark at 200 rows
# would be sub-millisecond and not meaningful as a performance gate.
# Replacement: correctness + scaling test that seeds 200 SQL dialog rows
# (mix of User/Channel), verifies all 200 are returned, and asserts
# WR-06 enrichment (unread_in/unread_out) is correct for every row type.
# ---------------------------------------------------------------------------


async def test_list_dialogs_unread_columns_scales_to_200_dialogs() -> None:
    """200 dialogs (mix User/Channel): all returned, WR-06 enrichment correct for each.

    Replaces the iter_dialogs latency benchmark (removed — pinned the old hot-path;
    the SQL path is sub-millisecond at this scale and correctness is the meaningful gate).
    """
    with _make_db() as conn:
        N_DIALOGS = 200
        user_ids = set()
        channel_ids = set()

        for d in range(1, N_DIALOGS + 1):
            type_ = "User" if d % 2 == 1 else "Channel"
            _insert_dialog(conn, d, name=f"Peer {d}", type_=type_)
            if type_ == "User":
                user_ids.add(d)
                # Seed synced_dialogs + messages so unread enrichment has data
                _insert_synced_dialog(conn, d, read_inbox_max_id=5, read_outbox_max_id=5)
                conn.execute(
                    "INSERT INTO messages (dialog_id, message_id, sent_at, out, is_deleted) VALUES (?, 6, 1700000000, 0, 0)",
                    (d,),
                )
                conn.execute(
                    "INSERT INTO messages (dialog_id, message_id, sent_at, out, is_deleted) VALUES (?, 7, 1700000001, 1, 0)",
                    (d,),
                )
            else:
                channel_ids.add(d)
        conn.commit()

        client = _TestClient()
        server = _make_server(conn, client)
        trace: list[str] = []
        conn.set_trace_callback(trace.append)
        result = await server._list_dialogs({})
        conn.set_trace_callback(None)
        assert result["ok"] is True

        dialogs = result["data"]["dialogs"]
        assert len(dialogs) == N_DIALOGS, f"Expected {N_DIALOGS} dialogs, got {len(dialogs)}"
        assert [row["id"] for row in dialogs] == list(range(1, N_DIALOGS + 1))
        aggregate_trace = [statement for statement in trace if "json_each" in statement]
        assert len(aggregate_trace) == 1
        assert "json_each('[1,2,3" in aggregate_trace[0]

        for row in dialogs:
            if row["id"] in user_ids:
                assert "unread_in" in row, f"User row {row['id']} missing unread_in"
                assert "unread_out" in row, f"User row {row['id']} missing unread_out"
                assert row["unread_in"] == 1, f"User row {row['id']} unread_in should be 1"
                assert row["unread_out"] == 1, f"User row {row['id']} unread_out should be 1"
            else:
                assert "unread_in" not in row, f"Channel row {row['id']} should not have unread_in"
                assert "unread_out" not in row, f"Channel row {row['id']} should not have unread_out"

        # iter_dialogs is never called in the SQL path
        cast(MagicMock, client.iter_dialogs).assert_not_called()


async def test_unread_summary_uses_dialog_projection_for_unsynced_and_own_only() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 1, name="Unsynced", type_="User")
        _insert_dialog(conn, 2, name="Own only", type_="Group")
        conn.execute("UPDATE dialogs SET unread_count=3, last_message_at=100 WHERE dialog_id=1")
        conn.execute("UPDATE dialogs SET unread_mark=1, last_message_at=200 WHERE dialog_id=2")
        _insert_synced_dialog(conn, 2, status="own_only")
        conn.commit()

        result = await _make_server(conn, _TestClient())._get_unread_summary({})

        assert result["ok"] is True
        rows = cast(dict[str, object], result["data"])["dialogs"]
        assert [row["dialog_id"] for row in cast(list[dict[str, object]], rows)] == [2, 1]


async def test_unread_summary_never_derives_unread_from_message_rows() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 1, name="Only messages", type_="User")
        _insert_message(conn, 1, 999)
        conn.commit()

        result = await _make_server(conn, _TestClient())._get_unread_summary({})

        data = cast(dict[str, object], result["data"])
        assert data["dialogs"] == []
        assert data["total_matching"] == 0


async def test_unread_summary_includes_mention_only_and_reaction_only_rows() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 1, name="Mention", type_="User")
        _insert_dialog(conn, 2, name="Reaction", type_="User")
        conn.execute(
            "UPDATE dialogs SET unread_count=NULL, unread_mark=NULL, unread_mentions_count=2 WHERE dialog_id=1"
        )
        conn.execute("UPDATE dialogs SET unread_count=NULL, unread_mark=0, unread_reactions_count=3 WHERE dialog_id=2")
        conn.commit()

        result = await _make_server(conn, _TestClient())._get_unread_summary({})

        rows = cast(list[dict[str, object]], cast(dict[str, object], result["data"])["dialogs"])
        by_id = {row["dialog_id"]: row for row in rows}
        assert by_id[1]["unread_mentions_count"] == 2
        assert by_id[1]["unread_mark"] is None
        assert by_id[2]["unread_reactions_count"] == 3
        assert by_id[2]["unread_mark"] is False


async def test_unread_summary_excludes_hidden_access_lost_and_zero_rows() -> None:
    with _make_db() as conn:
        _insert_dialog(conn, 1, name="Hidden", type_="User")
        _insert_dialog(conn, 2, name="Access lost", type_="User")
        _insert_dialog(conn, 3, name="Zero", type_="User")
        conn.execute("UPDATE dialogs SET hidden=1, unread_count=1 WHERE dialog_id=1")
        conn.execute("UPDATE dialogs SET unread_count=1 WHERE dialog_id=2")
        conn.execute(
            "UPDATE dialogs SET unread_count=0, unread_mark=0, unread_mentions_count=0, unread_reactions_count=0 WHERE dialog_id=3"
        )
        _insert_synced_dialog(conn, 2, status="access_lost")
        conn.commit()

        result = await _make_server(conn, _TestClient())._get_unread_summary({})

        data = cast(dict[str, object], result["data"])
        assert data["dialogs"] == []
        assert data["total_matching"] == 0


async def test_unread_summary_limit_is_deterministic_and_reports_truncation() -> None:
    with _make_db() as conn:
        for dialog_id in (3, 1, 2):
            _insert_dialog(conn, dialog_id, name=f"Peer {dialog_id}", type_="User")
            conn.execute("UPDATE dialogs SET unread_count=1, last_message_at=100 WHERE dialog_id=?", (dialog_id,))
        conn.commit()

        result = await _make_server(conn, _TestClient())._get_unread_summary({"limit": 2})

        data = cast(dict[str, object], result["data"])
        assert [row["dialog_id"] for row in cast(list[dict[str, object]], data["dialogs"])] == [1, 2]
        assert data["count"] == 2
        assert data["total_matching"] == 3
        assert data["truncated"] is True


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


async def test_list_dialogs_structured_output_includes_unread_values_and_channel_nulls() -> None:
    from mcp_telegram.tools.discovery import ListDialogs, list_dialogs

    sync_model = build_sync_read_model(
        persisted_status="synced",
        enrollment_enabled=True,
        last_synced_at=1700000000,
        last_event_at=None,
        last_delta_checked_at=None,
        saved_message_count=0,
        total_messages=None,
        now=1700000000,
    ).to_wire()
    response = {
        "ok": True,
        "data": {
            "dialogs": [
                {
                    "id": 1,
                    "name": "Alice",
                    "type": "User",
                    "last_message_at": "2024-01-01 00:00",
                    "unread_count": 2,
                    "unread_in": 3,
                    "unread_out": 1,
                    **sync_model,
                },
                {
                    "id": 2,
                    "name": "Announcements",
                    "type": "Channel",
                    "last_message_at": "2024-01-01 00:00",
                    "unread_count": 0,
                    **sync_model,
                },
            ],
            "snapshot_age_h": None,
            "bootstrap_pending": False,
            "scope": "all",
            "folder_snapshot": {
                "generation": None,
                "status": "unavailable",
                "completed_at": None,
                "age_seconds": None,
                "complete": False,
            },
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

    assert result.content == ()
    assert result.structured_content is not None
    dialogs = cast(list[dict[str, object]], cast(dict[str, object], result.structured_content)["dialogs"])
    dm_row = dialogs[0]
    assert dm_row["unread_in"] == 3
    assert dm_row["unread_out"] == 1

    channel_row = dialogs[1]
    assert channel_row["unread_in"] is None
    assert channel_row["unread_out"] is None
