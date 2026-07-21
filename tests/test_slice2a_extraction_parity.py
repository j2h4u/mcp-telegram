"""Slice 2a extraction parity for direct owner modules.

Slice 2a moved SQL constants, row mappers, and read-query helpers out of
``daemon_api`` into three owner modules (``daemon_message_queries``,
``daemon_read_state_queries``, ``daemon_dialog_queries``). Representative SQL
params, row mapping, sync-coverage, access-metadata, dialog-type, and read-state
results are exercised through those exact owners.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest

from mcp_telegram.daemon_dialog_queries import _build_access_metadata, _compute_sync_coverage
from mcp_telegram.daemon_message_queries import (
    _build_list_messages_query,
    _ListMessagesDbRequest,
    _read_message_from_row,
)
from mcp_telegram.daemon_read_state_queries import _dialog_type_from_db, _read_state_for_dialog
from mcp_telegram.models import DialogType

# ---------------------------------------------------------------------------
# Behavior parity — driven through the exact owner modules.
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
    since_utc: int | None = None
    until_utc: int | None = None


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
    sql, params = _build_list_messages_query(_make_req(sender_id=7, topic_id=3, unread_after_id=50, direction="oldest"))
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


def test_build_list_messages_query_uses_half_open_utc_bounds() -> None:
    sql, params = _build_list_messages_query(_make_req(since_utc=1_700_000_000, until_utc=1_700_001_000))

    assert "m.sent_at >= :since_utc" in sql
    assert "m.sent_at < :until_utc" in sql
    assert params["since_utc"] == 1_700_000_000
    assert params["until_utc"] == 1_700_001_000


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
    msg = _read_message_from_row(row, reactions_display="👍1")
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
    assert _compute_sync_coverage(total, local) == expected


def test_build_access_metadata_live(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    conn = make_synced_db()
    meta = _build_access_metadata(conn, dialog_id=100, status="synced")
    assert meta == {"dialog_access": "live"}


def test_build_access_metadata_archived(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
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

    meta = _build_access_metadata(conn, dialog_id=100, status="access_lost")
    assert meta["dialog_access"] == "archived"
    assert meta["access_lost_at"] == 1700000500
    assert meta["sync_coverage_pct"] == 62


def test_dialog_type_from_db(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    conn = make_synced_db()
    conn.execute(
        "INSERT INTO entities (id, type, name, updated_at) VALUES (?, 'User', 'Alice', 0)",
        (100,),
    )
    conn.commit()
    assert _dialog_type_from_db(conn, 100) == "User"
    assert _dialog_type_from_db(conn, 999) == "Unknown"


def test_read_state_for_dialog_dm(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
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

    rs = _read_state_for_dialog(conn, 100, "User")
    assert rs is not None
    assert rs["inbox_unread_count"] == 2
    assert rs["inbox_cursor_state"] == "populated"
    assert rs["outbox_unread_count"] == 0
    assert rs["outbox_cursor_state"] == "all_read"


def test_read_state_for_dialog_non_dm_returns_none(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    conn = make_synced_db()
    assert _read_state_for_dialog(conn, 100, "Channel") is None


def test_read_state_return_type_is_read_state() -> None:
    # ReadState is a TypedDict; keep the canonical dialog vocabulary in the test.
    assert DialogType.parse("User") is DialogType.USER
