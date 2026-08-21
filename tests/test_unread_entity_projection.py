from __future__ import annotations

from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.tools.unread import GetInbox, GetUnreadSummary, get_inbox, get_unread_summary


def _connection(*, inbox: dict[str, object] | None = None, summary: dict[str, object] | None = None) -> MagicMock:
    connection = MagicMock()
    connection.get_inbox = AsyncMock(return_value=inbox or {"ok": True, "data": {"groups": []}})
    connection.get_unread_summary = AsyncMock(return_value=summary or {"ok": True, "data": {"dialogs": []}})
    return connection


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
