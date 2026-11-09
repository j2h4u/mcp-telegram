from __future__ import annotations

import json
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
        CREATE TABLE dialogs(
          dialog_id INTEGER PRIMARY KEY,name TEXT,type TEXT,username TEXT,
          identity_observed_at INTEGER,identity_complete INTEGER DEFAULT 0,
          identity_source TEXT,identity_revision INTEGER DEFAULT 0);
        CREATE TABLE entities(id INTEGER PRIMARY KEY,type TEXT,name TEXT,username TEXT);
        CREATE TABLE messages(dialog_id INTEGER,message_id INTEGER,text TEXT,rich_message TEXT,formatting_text TEXT,
          formatting_entities TEXT,PRIMARY KEY(dialog_id,message_id));
        CREATE TABLE message_versions(dialog_id INTEGER,message_id INTEGER,version INTEGER,old_text TEXT,
          old_rich_message TEXT,old_formatting_text TEXT,old_formatting_entities TEXT,
          PRIMARY KEY(dialog_id,message_id,version));
        INSERT INTO dialogs(dialog_id,name,type,identity_complete,identity_source)
          VALUES (1,'Alice','user',1,'directory'),(2,'Group','supergroup',1,'directory');
        INSERT INTO entities VALUES (1,'user','Stale profile name','stale_user');
        INSERT INTO messages(dialog_id,message_id,text) VALUES (1,10,'after'),(1,11,'deleted candidate');
        INSERT INTO message_versions(dialog_id,message_id,version,old_text) VALUES (1,10,1,'before');
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
        "last_known_composition": None,
        "before_composition": {
            "rich_message": None,
            "rich_message_status": "unknown",
            "rich_text_status": None,
            "formatting_text": None,
            "formatting_entities": None,
        },
        "after_composition": {
            "rich_message": None,
            "rich_message_status": "unknown",
            "rich_text_status": None,
            "formatting_text": None,
            "formatting_entities": None,
        },
        "provenance": "message_versions.old_text+messages.text[current_candidate]",
        "confidence": "exact_before_candidate_after",
    }


def test_edit_returns_previous_rich_composition() -> None:
    with closing(_conn()) as conn:
        conn.execute(
            "UPDATE message_versions SET old_rich_message = ?, old_formatting_text = ?, old_formatting_entities = ?",
            ('{"_":"RichMessage","blocks":[]}', "😀", '[{"_":"MessageEntityBold","offset":0,"length":2}]'),
        )
        result = query_conversation_changes(conn, {"kinds": ["edit"], "dialog_id": 1}, ConversationChangesTokenCodec())
    evidence = cast(dict[str, object], _events(_data(result))[0]["text_evidence"])
    composition = cast(dict[str, object], evidence["before_composition"])
    assert cast(dict[str, object], composition["rich_message"])["_"] == "RichMessage"
    assert composition["formatting_text"] == "😀"
    assert cast(list[dict[str, object]], composition["formatting_entities"])[0]["length"] == 2


def test_rich_history_budget_resumes_without_losing_events() -> None:
    with closing(_conn()) as conn:
        rich = json.dumps({"_": "RichMessage", "blocks": [{"_": "PageBlockParagraph", "text": "x" * 140_000}]})
        conn.execute("UPDATE messages SET rich_message = ?", (rich,))
        conn.execute("UPDATE message_versions SET old_rich_message = ?", (rich,))
        codec = ConversationChangesTokenCodec()
        first = _data(query_conversation_changes(conn, {"kinds": ["edit", "deleted_message"], "page_limit": 10}, codec))
        assert first["truncation_reason"] == "response_budget"
        second = _data(query_conversation_changes(conn, {"navigation": first["next_navigation"]}, codec))
        assert [e["event_id"] for e in _events(first) + _events(second)] == [2, 1]


def test_dialog_title_uses_identity_owner_and_numeric_fallback_for_missing_dialog() -> None:
    with closing(_conn()) as conn:
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id) "
            "VALUES ('access_lost',104,'observed',404)"
        )
        result = query_conversation_changes(
            conn,
            {"kinds": ["access_lost"], "dialog_id": 404},
            ConversationChangesTokenCodec(),
        )

    event = _events(_data(result))[0]
    assert event["dialog_title"] == "404"


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
