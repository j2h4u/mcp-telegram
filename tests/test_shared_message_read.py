from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import jsonschema
import pytest
from telethon.tl.types import Message, MessageActionChatAddUser, MessageEntityBold, MessageService, PeerChat

from mcp_telegram.daemon_message import project_read_message_content
from mcp_telegram.formatter import _format_message_body
from mcp_telegram.reading.query_records import read_message_from_row
from mcp_telegram.reading.scheduled_projection import _SCHEDULED_MESSAGE_SELECT_SQL, scheduled_row_to_wire
from mcp_telegram.reading.sqlite_projection import _LIST_MESSAGES_BASE_SQL
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_message_projection import MessageLike, message_to_dict
from mcp_telegram.tools.message_view import MESSAGE_VIEW_SCHEMA, project_message_view
from mcp_telegram.tools.reading import _read_messages_from_rows


@pytest.mark.parametrize(
    ("action", "reply", "description"),
    [
        ({"_": "MessageActionChatAddUser", "users": [12, 13]}, None, "Members added: [12, 13]"),
        ({"_": "MessageActionChatDeleteUser", "user_id": 12}, None, "Member removed: 12"),
        ({"_": "MessageActionPinMessage"}, 7, "Message pinned: 7"),
    ],
)
def test_ordinary_read_preserves_typed_service_events(
    action: dict, reply: int | None, description: str, tmp_path: Path
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,is_service,service_action,formatting_entities,reply_to_msg_id) "
            "VALUES(-42,17,1700000000,1,?,'[]',?)",
            (json.dumps(action), reply),
        )
        row = cast(sqlite3.Row, conn.execute(_LIST_MESSAGES_BASE_SQL, {"dialog_id": -42, "self_id": 99}).fetchone())
        message = read_message_from_row(row)
        view = project_message_view(message)
        jsonschema.validate(view, MESSAGE_VIEW_SCHEMA)
        assert view["service_action"] == action
        assert view["service_action_status"] == "captured"
        assert view["formatting_entities"] == []
        assert description in _format_message_body(message, ZoneInfo("UTC"))


def test_utf16_spans_refer_to_original_text_after_hidden_link_rendering() -> None:
    entities = [
        {"_": "MessageEntityBold", "offset": 0, "length": 2},
        {"_": "MessageEntityTextUrl", "offset": 3, "length": 4, "url": "https://example.org"},
        {"_": "MessageEntityCustomEmoji", "offset": 0, "length": 2, "document_id": 123},
    ]
    message = read_message_from_row(
        {
            "message_id": 1,
            "sent_at": 1,
            "dialog_id": -42,
            "text": "😀 link",
            "formatting_entities": json.dumps(entities),
        }
    )
    message = project_read_message_content(message, text_links=[(3, 4, "https://example.org")])
    view = project_message_view(message)
    jsonschema.validate(view, MESSAGE_VIEW_SCHEMA)
    assert view["formatting_entities"] == entities
    assert view["formatting_text"] == "😀 link"
    assert message.text is not None
    assert "https://example.org" in message.text
    assert view["composition_is_telegram_content"] is True


def test_historical_or_partial_rows_do_not_invent_composition_facts() -> None:
    message = read_message_from_row({"message_id": 1, "sent_at": 1, "dialog_id": -42, "is_service": 1})
    view = project_message_view(message)
    jsonschema.validate(view, MESSAGE_VIEW_SCHEMA)
    assert view["formatting_entities_status"] == "unknown"
    assert view["service_action_status"] == "unknown"
    assert "formatting_entities" not in view
    assert "service_action" not in view
    assert "details not captured" in _format_message_body(message, ZoneInfo("UTC"))


def test_live_read_uses_same_composition_contract() -> None:
    service = MessageService(id=4, peer_id=PeerChat(42), date=datetime.now(UTC), action=MessageActionChatAddUser([12]))
    service_view = project_message_view(
        _read_messages_from_rows([message_to_dict(cast(MessageLike, service), dialog_id=-42)])[0]
    )
    assert service_view["service_action"] == {"_": "MessageActionChatAddUser", "users": [12]}
    rich = Message(
        id=5,
        peer_id=PeerChat(42),
        date=datetime.now(UTC),
        message="😀 hi",
        entities=[MessageEntityBold(offset=0, length=2)],
    )
    rich_view = project_message_view(
        _read_messages_from_rows([message_to_dict(cast(MessageLike, rich), dialog_id=-42)])[0]
    )
    assert rich_view["formatting_entities"] == [{"_": "MessageEntityBold", "offset": 0, "length": 2}]
    assert rich_view["formatting_text"] == "😀 hi"


def test_scheduled_read_keeps_common_formatting_facts(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    entities = [{"_": "MessageEntityBold", "offset": 0, "length": 2}]
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO scheduled_messages(dialog_id,message_id,scheduled_at,text,formatting_entities,first_seen_at,updated_at) "
            "VALUES(-42,5,1900000000,'😀 hi',?,1,1)",
            (json.dumps(entities),),
        )
        row = cast(sqlite3.Row, conn.execute(_SCHEDULED_MESSAGE_SELECT_SQL + " FROM scheduled_messages sm").fetchone())
        wire = scheduled_row_to_wire(row, inclusion_basis=["local_schedule"])
        view = project_message_view(_read_messages_from_rows([wire])[0])
        assert view["formatting_entities"] == entities
        assert view["formatting_text"] == "😀 hi"
        assert view["formatting_entities_status"] == "captured"
