from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import cast

from mcp_telegram.conversation_changes import ConversationChangesTokenCodec, parse_request, query_conversation_changes


def _data(result: dict[str, object]) -> dict[str, object]:
    data = result["data"]
    assert isinstance(data, dict)
    return cast(dict[str, object], data)


def _events(data: dict[str, object]) -> list[dict[str, object]]:
    events = data["events"]
    assert isinstance(events, list)
    return cast(list[dict[str, object]], events)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE conversation_history_events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, occurred_at INTEGER, time_basis TEXT,
          dialog_id INTEGER, message_id INTEGER, version INTEGER, reason_code TEXT,
          access_change_cause TEXT, actor_id INTEGER);
        CREATE TABLE dialogs(dialog_id INTEGER PRIMARY KEY,name TEXT);
        CREATE TABLE messages(dialog_id INTEGER,message_id INTEGER,text TEXT,PRIMARY KEY(dialog_id,message_id));
        CREATE TABLE message_versions(dialog_id INTEGER,message_id INTEGER,version INTEGER,old_text TEXT,
          PRIMARY KEY(dialog_id,message_id,version));
        INSERT INTO dialogs VALUES (1,'Alice'),(2,'Group');
        INSERT INTO messages VALUES (1,10,'after'),(1,11,'deleted candidate');
        INSERT INTO message_versions VALUES (1,10,1,'before');
        INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id,message_id,version)
          VALUES ('edit',100,'telegram',1,10,1),('deleted_message',101,'observed',1,11,NULL);
        INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id,reason_code,access_change_cause,actor_id)
          VALUES ('access_lost',102,'observed',2,'UpdateChannelParticipant','removed_by_admin',99);
        INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id)
          VALUES ('access_restored',103,'observed',2);
        """
    )
    return conn


def test_query_returns_one_canonical_event_shape_with_truthful_evidence() -> None:
    with closing(_conn()) as conn:
        result = query_conversation_changes(conn, {"kinds": ["edit"], "dialog_id": 1}, ConversationChangesTokenCodec())
    assert result["ok"] is True
    data = _data(result)
    assert data["count"] == 1
    assert data["coverage"] == {
        "message_changes": "incoming_human_direct_messages",
        "access_changes": "synced_dialogs",
    }
    event = _events(data)[0]
    assert event["event_id"] == 1
    assert event["kind"] == "edit"
    assert event["time_basis"] == "telegram"
    assert event["dialog_title"] == "Alice"
    assert event["text_evidence"] == {
        "untrusted_content": True,
        "last_known_text": None,
        "before_text": "before",
        "after_text": "after",
        "provenance": "message_versions.old_text+messages.text[current_candidate]",
        "confidence": "exact_before_candidate_after",
    }


def test_filters_use_inclusive_since_and_exclusive_until() -> None:
    with closing(_conn()) as conn:
        result = query_conversation_changes(conn, {"since_utc": 101, "until_utc": 103}, ConversationChangesTokenCodec())
    assert [event["event_id"] for event in _events(_data(result))] == [3, 2]


def test_snapshot_pagination_has_no_duplicates() -> None:
    with closing(_conn()) as conn:
        codec = ConversationChangesTokenCodec()
        first = _data(query_conversation_changes(conn, {"page_limit": 2}, codec))
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id) VALUES ('access_restored',104,'observed',2)"
        )
        second = _data(query_conversation_changes(conn, {"navigation": first["next_navigation"]}, codec))
    assert [event["event_id"] for event in _events(first) + _events(second)] == [4, 3, 2, 1]
    assert second["has_more"] is False


def test_cursor_rejects_filter_changes() -> None:
    with closing(_conn()) as conn:
        codec = ConversationChangesTokenCodec()
        first = _data(query_conversation_changes(conn, {"page_limit": 1}, codec))
        result = query_conversation_changes(
            conn, {"navigation": first["next_navigation"], "kinds": ["access_lost"]}, codec
        )
    assert result["error"] == "invalid_navigation"


def test_parse_request_rejects_invalid_ranges_and_kinds() -> None:
    for request in cast(
        tuple[dict[str, object], ...],
        (
            {"since_utc": 2, "until_utc": 2},
            {"kinds": []},
            {"kinds": ["unknown"]},
            {"page_limit": 0},
        ),
    ):
        try:
            parse_request(request)
        except ValueError:
            pass
        else:
            raise AssertionError(request)
