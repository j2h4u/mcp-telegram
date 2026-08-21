from __future__ import annotations

from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.models import ReadMessage
from mcp_telegram.tools.unread import (
    GetInbox,
    GetUnreadSummary,
    _project_message_sender,
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
        (
            ReadMessage(message_id=5, sent_at=1, dialog_id=-1001, is_service=1),
            {"kind": "system"},
        ),
        (
            ReadMessage(message_id=6, sent_at=1, dialog_id=-1001),
            {"kind": "unknown"},
        ),
    ],
    ids=["dm-incoming", "dm-outgoing-self", "group-username", "group-no-username", "system", "unknown"],
)
def test_inbox_sender_projection_uses_identity_contract_or_explicit_non_entity(
    message: ReadMessage, expected: dict[str, object]
) -> None:
    assert _project_message_sender(message) == expected


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
    assert "sender_id" not in message
    assert "effective_sender_id" not in message


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
