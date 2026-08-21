from __future__ import annotations

from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jsonschema import validate

from mcp_telegram.models import ReadMessage
from mcp_telegram.temporal import normalize_temporal_output_schema
from mcp_telegram.tools.unread import (
    GET_INBOX_OUTPUT_SCHEMA,
    GetInbox,
    GetUnreadSummary,
    _project_message_sender,
    _project_message_topic,
    _project_unread_summary_dialog,
    _project_unread_summary_dialogs,
    _structured_messages,
    get_inbox,
    get_unread_summary,
)


def _connection(*, inbox: dict[str, object] | None = None, summary: dict[str, object] | None = None) -> MagicMock:
    connection = MagicMock()
    connection.get_inbox = AsyncMock(return_value=inbox or {"ok": True, "data": {"groups": []}})
    connection.get_unread_summary = AsyncMock(return_value=summary or {"ok": True, "data": {"dialogs": []}})
    return connection


def test_unread_summary_projection_helper_keeps_identity_contract_and_skips_bad_rows() -> None:
    row = _project_unread_summary_dialog(
        {
            "dialog_id": 42,
            "name": "Alice",
            "username": "alice",
            "dialog_type": "User",
            "unread_count": 1,
            "unread_mark": False,
            "unread_mentions_count": 0,
            "unread_reactions_count": 0,
            "archived": False,
            "last_message_at": None,
        }
    )

    assert row is not None
    assert row["entity"] == {"display_name": "Alice", "username": "@alice"}
    assert "dialog_id" not in row
    assert _project_unread_summary_dialog({"dialog_id": "42"}) is None
    assert _project_unread_summary_dialog({"dialog_id": 42, "name": 123, "username": False}) == {
        "entity": {"display_name": "42", "telegram_id": 42},
        "dialog_type": None,
        "unread_count": None,
        "unread_mark": None,
        "unread_mentions_count": 0,
        "unread_reactions_count": 0,
        "archived": False,
        "last_message_at": None,
    }
    assert _project_unread_summary_dialogs([{"dialog_id": 42, "name": "Alice"}, "bad"]) == [
        {
            "entity": {"display_name": "Alice", "telegram_id": 42},
            "dialog_type": None,
            "unread_count": None,
            "unread_mark": None,
            "unread_mentions_count": 0,
            "unread_reactions_count": 0,
            "archived": False,
            "last_message_at": None,
        }
    ]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            ReadMessage(
                message_id=1,
                sent_at=1,
                dialog_id=1001,
                effective_sender_id=1001,
                sender_first_name="Alice",
                sender_username="alice",
            ),
            {"display_name": "Alice", "username": "@alice"},
        ),
        (
            ReadMessage(
                message_id=2,
                sent_at=1,
                dialog_id=1001,
                effective_sender_id=777,
                sender_first_name="Me",
                sender_username="me",
                out=1,
            ),
            {"display_name": "Me", "username": "@me"},
        ),
        (
            ReadMessage(
                message_id=3,
                sent_at=1,
                dialog_id=-1001,
                sender_id=55,
                effective_sender_id=55,
                sender_first_name="Group member",
                sender_username="member",
            ),
            {"display_name": "Group member", "username": "@member"},
        ),
        (
            ReadMessage(
                message_id=4,
                sent_at=1,
                dialog_id=-1001,
                sender_id=56,
                effective_sender_id=56,
                sender_first_name="No Username",
            ),
            {"display_name": "No Username", "telegram_id": 56},
        ),
    ],
    ids=["dm-incoming", "dm-outgoing-self", "group-username", "group-no-username"],
)
def test_inbox_sender_projection_uses_identity_contract_or_null(
    message: ReadMessage, expected: dict[str, object]
) -> None:
    assert _project_message_sender(message) == expected


def test_inbox_sender_projection_returns_null_without_actor_id() -> None:
    assert _project_message_sender(ReadMessage(message_id=5, sent_at=1, dialog_id=-1001)) is None


def test_structured_inbox_message_omits_sender_without_actor_id() -> None:
    message = _structured_messages(
        [{"message_id": 5, "sent_at": 1, "dialog_id": -1001, "forum_topic_id": 7}],
        read_state=None,
        dialog_type="group",
    )[0]
    assert "sender" not in message
    assert message["topic"] == {"topic_id": 7}

    message_schema = GET_INBOX_OUTPUT_SCHEMA["properties"]["dialogs"]["items"]["properties"]["messages"]["items"]
    validate(instance=message, schema=message_schema)


def test_inbox_output_schema_keeps_sender_and_topic_optional() -> None:
    message_schema = GET_INBOX_OUTPUT_SCHEMA["properties"]["dialogs"]["items"]["properties"]["messages"]["items"]
    assert "sender" not in message_schema["required"]
    assert "topic" not in message_schema["required"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (ReadMessage(message_id=1, sent_at=1, dialog_id=1001), None),
        (ReadMessage(message_id=2, sent_at=1, dialog_id=1001, forum_topic_id=7), {"topic_id": 7}),
        (
            ReadMessage(message_id=3, sent_at=1, dialog_id=1001, forum_topic_id=7, topic_title="Reports"),
            {"title": "Reports"},
        ),
        (
            ReadMessage(message_id=4, sent_at=1, dialog_id=1001, forum_topic_id=7, topic_title=" "),
            {"topic_id": 7},
        ),
    ],
    ids=["missing", "id-fallback", "title", "blank-title-falls-back"],
)
def test_inbox_topic_projection_is_universal_and_never_leaks_both_fields(
    message: ReadMessage, expected: dict[str, object] | None
) -> None:
    assert _project_message_topic(message) == expected


def test_structured_inbox_messages_remove_legacy_sender_identity_fields() -> None:
    rows = [
        {
            "message_id": 1,
            "sent_at": 1,
            "dialog_id": 1001,
            "effective_sender_id": 1001,
            "sender_first_name": "Alice",
            "sender_username": "@alice",
            "out": 0,
        }
    ]

    message = _structured_messages(rows, read_state=None, dialog_type="user")[0]

    assert message["sender"] == {"display_name": "Alice", "username": "@alice"}
    assert "topic" not in message
    assert "sender_id" not in message
    assert "effective_sender_id" not in message


@pytest.mark.asyncio
async def test_get_inbox_concrete_message_sender_validates_against_output_schema() -> None:
    connection = _connection(
        inbox={
            "ok": True,
            "data": {
                "groups": [
                    {
                        "dialog_id": -1001,
                        "display_name": "Small Group",
                        "category": "group",
                        "dialog_type": "group",
                        "unread_count": 1,
                        "messages": [
                            {
                                "message_id": 7,
                                "sent_at": 1_700_000_000,
                                "dialog_id": -1001,
                                "sender_id": 55,
                                "effective_sender_id": 55,
                                "sender_first_name": "Alice",
                                "sender_username": "alice",
                            }
                        ],
                    }
                ]
            },
        }
    )

    @asynccontextmanager
    async def fake_connection():
        yield connection

    with patch("mcp_telegram.tools.unread.daemon_connection", fake_connection):
        result = await get_inbox(GetInbox())

    payload = cast(dict[str, object], result.structured_content)
    output_schema = normalize_temporal_output_schema(GET_INBOX_OUTPUT_SCHEMA)
    assert output_schema is not None
    validate(instance=payload, schema=output_schema)
    dialogs = cast(list[dict[str, object]], payload["dialogs"])
    messages = cast(list[dict[str, object]], dialogs[0]["messages"])
    assert messages[0]["sender"] == {"display_name": "Alice", "username": "@alice"}
    assert "topic" not in messages[0]


@pytest.mark.asyncio
async def test_get_inbox_projects_username_and_numeric_dialog_identity() -> None:
    connection = _connection(
        inbox={
            "ok": True,
            "data": {
                "groups": [
                    {
                        "dialog_id": 42,
                        "display_name": "Alice",
                        "username": "alice",
                        "category": "user",
                        "messages": [],
                    },
                    {
                        "dialog_id": 43,
                        "display_name": "Bob",
                        "username": None,
                        "category": "user",
                        "messages": [],
                    },
                ]
            },
        }
    )

    @asynccontextmanager
    async def fake_connection():
        yield connection

    with patch("mcp_telegram.tools.unread.daemon_connection", fake_connection):
        result = await get_inbox(GetInbox())

    payload = cast(dict[str, object], result.structured_content)
    dialogs = cast(list[dict[str, object]], payload["dialogs"])
    assert dialogs[0]["entity"] == {"display_name": "Alice", "username": "@alice"}
    assert dialogs[1]["entity"] == {"display_name": "Bob", "telegram_id": 43}
    assert all("dialog_id" not in dialog and "name" not in dialog for dialog in dialogs)


@pytest.mark.asyncio
async def test_unread_summary_projects_identity_and_does_not_leak_dialog_id() -> None:
    connection = _connection(
        summary={
            "ok": True,
            "data": {
                "dialogs": [
                    {
                        "dialog_id": 42,
                        "name": "Alice",
                        "username": "@alice",
                        "dialog_type": "User",
                        "unread_count": 1,
                        "unread_mark": False,
                        "unread_mentions_count": 0,
                        "unread_reactions_count": 0,
                        "archived": False,
                        "last_message_at": None,
                    }
                ]
            },
        }
    )

    @asynccontextmanager
    async def fake_connection():
        yield connection

    with patch("mcp_telegram.tools.unread.daemon_connection", fake_connection):
        result = await get_unread_summary(GetUnreadSummary())

    payload = cast(dict[str, object], result.structured_content)
    row = cast(list[dict[str, object]], payload["dialogs"])[0]
    assert row["entity"] == {"display_name": "Alice", "username": "@alice"}
    assert "dialog_id" not in row
    assert "name" not in row
