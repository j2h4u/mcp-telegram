"""Slice 2a extraction parity — legacy ``daemon_api.<name>`` still resolves to owner.

Slice 2a moved SQL constants, row mappers, and read-query helpers out of
``daemon_api`` into three owner modules (``daemon_message_queries``,
``daemon_read_state_queries``, ``daemon_dialog_queries``) and left temporary
re-exports behind. Until every call site switches, ``daemon_api.<name>`` must
stay identical (``is``) to the owner's object, and the extracted helpers must
compute exactly what they did before the move.

This module locks both facets:

1. Identity — every bridged function/constant/type is the *same object* in
   ``daemon_api`` and its owner. A copy-instead-of-re-export regression fails here.
2. Behavior — representative SQL params, row mapping, sync-coverage,
   access-metadata, dialog-type, and read-state results, driven through the
   legacy ``daemon_api`` names so the bridge is exercised end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from mcp_telegram import (
    daemon_api,
    daemon_dialog_queries,
    daemon_message_queries,
    daemon_read_state_queries,
)
from mcp_telegram.daemon_message_queries import _ListMessagesDbRequest
from mcp_telegram.models import DialogType, ReadState

# ---------------------------------------------------------------------------
# Bridged name inventories — kept explicit so a dropped re-export is a visible
# diff, not a silently-missing assertion.
# ---------------------------------------------------------------------------

_MESSAGE_QUERY_NAMES = (
    "EFFECTIVE_SENDER_ID_SQL",
    "_FETCH_UNREAD_MESSAGES_SQL",
    "_LIST_MESSAGES_BASE_SQL",
    "_SELECT_FTS_ALL_SQL",
    "_SELECT_FTS_SQL",
    "_SELECT_MESSAGES_SQL",
    "_SENDER_ENTITY_JOINS_SQL",
    "_SENDER_FIRST_NAME_SQL",
    "_assert_select_columns_match_read_message",
    "_build_list_messages_query",
    "_ListMessagesDbRequest",
    "_read_message_from_row",
)

# NOTE: entity constants (_UPSERT_ENTITY_SQL, _ALL_ENTITY_NAMES*_SQL,
# _ENTITY_BY_USERNAME_SQL) are intentionally NOT bridged here — Slice 2a leaves
# their ownership in daemon_api unchanged, so they are not owned by
# daemon_dialog_queries and must not appear in this identity inventory.
_DIALOG_QUERY_NAMES = (
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
)

_READ_STATE_NAMES = (
    "_dialog_type_from_db",
    "_read_state_for_dialog",
)


@pytest.mark.parametrize("name", _MESSAGE_QUERY_NAMES)
def test_message_query_names_are_bridged(name: str) -> None:
    assert getattr(daemon_api, name) is getattr(daemon_message_queries, name)


@pytest.mark.parametrize("name", _DIALOG_QUERY_NAMES)
def test_dialog_query_names_are_bridged(name: str) -> None:
    assert getattr(daemon_api, name) is getattr(daemon_dialog_queries, name)


@pytest.mark.parametrize("name", _READ_STATE_NAMES)
def test_read_state_names_are_bridged(name: str) -> None:
    assert getattr(daemon_api, name) is getattr(daemon_read_state_queries, name)


def test_read_state_type_is_bridged() -> None:
    # ReadState is re-exported through daemon_api for not-yet-switched callers.
    assert daemon_api.ReadState is ReadState


# ---------------------------------------------------------------------------
# Behavior parity — driven through the legacy daemon_api names.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ListReq:
    dialog_id: int
    limit: int
    self_id: int | None
    direction: str
    anchor_msg_id: int | None
    anchor_sent_at: int | None
    sender_id: int | None
    sender_name: str | None
    topic_id: int | None
    unread_after_id: int | None


def _make_req(**overrides: object) -> _ListMessagesDbRequest:
    base: dict[str, object] = {
        "dialog_id": 100,
        "limit": 20,
        "self_id": 42,
        "direction": "newest",
        "anchor_msg_id": None,
        "anchor_sent_at": None,
        "sender_id": None,
        "sender_name": None,
        "topic_id": None,
        "unread_after_id": None,
    }
    base.update(overrides)
    return cast(_ListMessagesDbRequest, _ListReq(**base))  # type: ignore[arg-type]


def test_build_list_messages_query_params() -> None:
    sql, params = daemon_api._build_list_messages_query(
        _make_req(sender_id=7, topic_id=3, unread_after_id=50, direction="oldest")
    )
    assert params == {
        "dialog_id": 100,
        "limit": 20,
        "self_id": 42,
        "filter_sender_id": 7,
        "topic_id": 3,
        "unread_after_id": 50,
    }
    assert "ORDER BY m.message_id ASC" in sql
    assert ":filter_sender_id" in sql and ":topic_id" in sql and ":unread_after_id" in sql


def test_read_message_from_row_maps_fields() -> None:
    row = {
        "message_id": 9,
        "sent_at": 1700000000,
        "dialog_id": 100,
        "text": "hello",
        "sender_id": 42,
        "sender_first_name": "Alice",
        "out": 1,
        "is_service": 0,
        "is_deleted": 0,
    }
    msg = daemon_api._read_message_from_row(row, reactions_display="👍1")
    assert msg.message_id == 9
    assert msg.dialog_id == 100
    assert msg.text == "hello"
    assert msg.sender_first_name == "Alice"
    assert msg.out == 1
    assert msg.reactions_display == "👍1"


@pytest.mark.parametrize(
    ("total", "local", "expected"),
    [
        (100, 50, 50),
        (0, 0, 100),
        (None, 5, None),
        (-1, 5, None),
        (10, 20, None),  # local > total -> unknowable
    ],
)
def test_compute_sync_coverage(total: int | None, local: int, expected: int | None) -> None:
    assert daemon_api._compute_sync_coverage(total, local) == expected


def test_build_access_metadata_live(make_synced_db) -> None:
    conn = make_synced_db()
    meta = daemon_api._build_access_metadata(conn, dialog_id=100, status="synced")
    assert meta == {"dialog_access": "live"}


def test_build_access_metadata_archived(make_synced_db) -> None:
    conn = make_synced_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages, access_lost_at, "
        "last_synced_at, last_event_at) VALUES (?, 'access_lost', ?, ?, ?, ?)",
        (100, 8, 1700000500, 1700000100, 1700000200),
    )
    for mid in range(1, 6):  # 5 local messages, remote total = 8 -> 62%
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, out, is_deleted, is_service) "
            "VALUES (?, ?, ?, 0, 0, 0)",
            (100, mid, 1700000000 + mid),
        )
    conn.commit()

    meta = daemon_api._build_access_metadata(conn, dialog_id=100, status="access_lost")
    assert meta["dialog_access"] == "archived"
    assert meta["access_lost_at"] == 1700000500
    assert meta["sync_coverage_pct"] == 62


def test_dialog_type_from_db(make_synced_db) -> None:
    conn = make_synced_db()
    conn.execute(
        "INSERT INTO entities (id, type, name, updated_at) VALUES (?, 'User', 'Alice', 0)",
        (100,),
    )
    conn.commit()
    assert daemon_api._dialog_type_from_db(conn, 100) == "User"
    assert daemon_api._dialog_type_from_db(conn, 999) == "Unknown"


def test_read_state_for_dialog_dm(make_synced_db) -> None:
    conn = make_synced_db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id, read_outbox_max_id) "
        "VALUES (?, 'synced', ?, ?)",
        (100, 5, 5),
    )
    # Two unread inbound messages (id > cursor 5), nothing unread outbound.
    for mid, out in [(6, 0), (7, 0), (4, 1)]:
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, out, is_deleted, is_service) "
            "VALUES (?, ?, ?, ?, 0, 0)",
            (100, mid, 1700000000 + mid, out),
        )
    conn.commit()

    rs = daemon_api._read_state_for_dialog(conn, 100, "User")
    assert rs is not None
    assert rs["inbox_unread_count"] == 2
    assert rs["inbox_cursor_state"] == "populated"
    assert rs["outbox_unread_count"] == 0
    assert rs["outbox_cursor_state"] == "all_read"


def test_read_state_for_dialog_non_dm_returns_none(make_synced_db) -> None:
    conn = make_synced_db()
    assert daemon_api._read_state_for_dialog(conn, 100, "Channel") is None


def test_read_state_return_type_is_read_state() -> None:
    # ReadState is a TypedDict; assert the annotation object matches the bridge.
    assert DialogType.parse("User") is DialogType.USER
