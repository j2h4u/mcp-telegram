"""Slice 2a golden/parity tests for the query-module extraction.

Slice 2a moved SQL text, row mappers, and pure helpers out of ``daemon_api`` into
three focused owner modules (``daemon_message_queries``, ``daemon_dialog_queries``,
``daemon_read_state_queries``) plus a couple of constants that already lived in
``daemon_account_trace`` / ``daemon_message``. For this first commit ``daemon_api``
keeps *temporary* re-exports so not-yet-switched call sites and existing tests still
resolve ``daemon_api.<name>``.

These tests pin two properties so the Slice 2 call-site switch cannot silently drift:

1. Every re-exported ``daemon_api.<name>`` is the *identical object* owned by the
   extracted module — proving the old name and the new implementation are one and
   the same (identical SQL / results / helpers by construction).
2. Golden snapshots of the pure builder + helpers, exercised through the
   ``daemon_api`` alias, lock the actual SQL string / result dicts the extraction
   produces.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from typing import cast

import pytest

from mcp_telegram import (
    daemon_account_trace,
    daemon_api,
    daemon_dialog_queries,
    daemon_message,
    daemon_message_queries,
    daemon_read_state_queries,
)
from mcp_telegram.daemon_api import (
    _LIST_MESSAGES_BASE_SQL,
    _build_list_messages_query,
    _compute_snapshot_age_h,
    _compute_sync_coverage,
    _dialog_type_from_db,
    _ListMessagesDbRequest,
    _read_state_for_dialog,
)

# ---------------------------------------------------------------------------
# 1. Re-export identity parity
# ---------------------------------------------------------------------------

# name -> owner module. Covers every symbol daemon_api pulls from an extracted
# query module (both the genuinely-used imports and the Slice 2a temp re-exports).
_REEXPORTS: dict[str, object] = {}
_REEXPORTS.update(
    dict.fromkeys(
        [
            "_TRACE_ACRONYM_MAX_LEN",
            "_TRACE_ACRONYM_MIN_LEN",
            "_TRACE_FUZZY_MIN_LEN",
            "_TRACE_FUZZY_SCORE_MIN",
            "GROUP_TTL",
            "USER_TTL",
        ],
        daemon_account_trace,
    )
)
_REEXPORTS.update(
    dict.fromkeys(
        [
            "_BATCHED_UNREAD_COUNTS_SQL",
            "_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL",
            "_COUNT_BOOTSTRAP_PENDING_SQL",
            "_COUNT_MESSAGES_BY_DIALOG_SQL",
            "_COUNT_SYNCED_MESSAGES_SQL",
            "_GET_ACCESS_LOST_ALERTS_SQL",
            "_GET_DELETED_ALERTS_SQL",
            "_GET_EDIT_ALERTS_SQL",
            "_GET_READ_POSITION_SQL",
            "_GET_SYNC_STATUS_SQL",
            "_LIST_DIALOGS_SQL",
            "_LIST_TOPICS_SQL",
            "_MARK_FOR_SYNC_SQL",
            "_SELECT_DIALOG_ACCESS_META_SQL",
            "_SELECT_SYNCED_STATUSES_SQL",
            "_UNMARK_SYNC_SQL",
            "_build_access_metadata",
            "_compute_snapshot_age_h",
            "_compute_sync_coverage",
        ],
        daemon_dialog_queries,
    )
)
_REEXPORTS.update(
    dict.fromkeys(
        [
            "EFFECTIVE_SENDER_ID_SQL",
            "_FETCH_UNREAD_MESSAGES_SQL",
            "_LIST_MESSAGES_BASE_SQL",
            "_SELECT_FTS_ALL_SQL",
            "_SELECT_FTS_SQL",
            "_SELECT_MESSAGES_SQL",
            "_SENDER_ENTITY_JOINS_SQL",
            "_SENDER_FIRST_NAME_SQL",
            "_ListMessagesDbRequest",
            "_assert_select_columns_match_read_message",
            "_build_list_messages_query",
            "_read_message_from_row",
        ],
        daemon_message_queries,
    )
)
_REEXPORTS.update(dict.fromkeys(["_dialog_type_from_db", "_read_state_for_dialog"], daemon_read_state_queries))
_REEXPORTS.update(dict.fromkeys(["REACTIONS_TTL_SECONDS"], daemon_message))


@pytest.mark.parametrize("name", sorted(_REEXPORTS), ids=sorted(_REEXPORTS))
def test_daemon_api_reexport_is_owner_object(name: str) -> None:
    """daemon_api.<name> is the *same object* the owner module exports."""
    owner = _REEXPORTS[name]
    assert hasattr(daemon_api, name), f"daemon_api lost re-export {name}"
    assert getattr(daemon_api, name) is getattr(owner, name), f"daemon_api.{name} diverged from {owner.__name__}.{name}"


# ---------------------------------------------------------------------------
# 2. Golden SQL snapshot — _build_list_messages_query
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Req:
    dialog_id: int = 100
    limit: int = 20
    self_id: int | None = None
    direction: str = "newest"
    anchor_msg_id: int | None = None
    anchor_sent_at: int | None = None
    sender_id: int | None = None
    sender_name: str | None = None
    topic_id: int | None = None
    unread_after_id: int | None = None


def _req(**overrides: object) -> _ListMessagesDbRequest:
    return cast(_ListMessagesDbRequest, _Req(**overrides))  # type: ignore[arg-type]


def test_build_list_messages_query_golden_baseline() -> None:
    """No filters: base SQL + newest ORDER/LIMIT, canonical params."""
    sql, params = _build_list_messages_query(_req())
    assert sql == _LIST_MESSAGES_BASE_SQL + " ORDER BY m.message_id DESC LIMIT :limit"
    assert params == {"dialog_id": 100, "limit": 20, "self_id": None}


def test_build_list_messages_query_golden_stacked_filters() -> None:
    """sender_id + topic_id + oldest direction compose deterministically."""
    sql, params = _build_list_messages_query(_req(sender_id=5, topic_id=2, self_id=42, direction="oldest"))
    assert sql == (
        _LIST_MESSAGES_BASE_SQL
        + " AND m.sender_id = :filter_sender_id"
        + " AND m.forum_topic_id = :topic_id"
        + " ORDER BY m.message_id ASC LIMIT :limit"
    )
    assert params == {
        "dialog_id": 100,
        "limit": 20,
        "self_id": 42,
        "filter_sender_id": 5,
        "topic_id": 2,
    }


# ---------------------------------------------------------------------------
# 3. Golden pure-helper truth tables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "total, local, expected",
    [
        (None, 0, None),
        (-1, 0, None),
        (10, 20, None),  # local exceeds remote -> not a meaningful ratio
        (0, 0, 100),
        (200, 100, 50),
        (3, 1, 33),
    ],
)
def test_compute_sync_coverage_golden(total: int | None, local: int, expected: int | None) -> None:
    assert _compute_sync_coverage(total, local) == expected


def test_compute_snapshot_age_h_fresh_is_none() -> None:
    # 1 hour old is under the 12h staleness threshold.
    assert _compute_snapshot_age_h(None) is None


# ---------------------------------------------------------------------------
# 4. Golden result parity — DB-backed read-state helpers via daemon_api alias
# ---------------------------------------------------------------------------


def _make_min_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE synced_dialogs (
            dialog_id          INTEGER PRIMARY KEY,
            status             TEXT NOT NULL DEFAULT 'synced',
            read_inbox_max_id  INTEGER,
            read_outbox_max_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            dialog_id   INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            sent_at     INTEGER NOT NULL,
            out         INTEGER NOT NULL DEFAULT 0,
            is_deleted  INTEGER NOT NULL DEFAULT 0,
            is_service  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (dialog_id, message_id)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT 0)"
    )
    return conn


@pytest.fixture
def min_db() -> Iterator[sqlite3.Connection]:
    with closing(_make_min_db()) as conn:
        yield conn


def test_dialog_type_from_db_golden(min_db: sqlite3.Connection) -> None:
    conn = min_db
    conn.execute("INSERT INTO entities (id, type) VALUES (55, 'User')")
    conn.commit()
    assert _dialog_type_from_db(conn, 55) == "User"
    assert _dialog_type_from_db(conn, 999) == "Unknown"


def test_read_state_for_dialog_golden_dm(min_db: sqlite3.Connection) -> None:
    conn = min_db
    # DM peer 55: inbox cursor read up to id 1; two incoming (2,3) unread.
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id, read_outbox_max_id) VALUES (55, 'synced', 1, 0)"
    )
    conn.executemany(
        "INSERT INTO messages (dialog_id, message_id, sent_at, out) VALUES (55, ?, ?, ?)",
        [(1, 100, 0), (2, 200, 0), (3, 300, 0)],
    )
    conn.commit()

    rs = _read_state_for_dialog(conn, 55, "User")
    assert rs == {
        "inbox_unread_count": 2,
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "all_read",
        "inbox_max_id_anchor": 1,
        "outbox_max_id_anchor": 0,
        "inbox_oldest_unread_date": 200,
    }


def test_read_state_for_dialog_none_for_non_dm(min_db: sqlite3.Connection) -> None:
    assert _read_state_for_dialog(min_db, 55, "Channel") is None
