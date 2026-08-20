"""Focused MCP-surface checks for the persisted dialog unread overview."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.tools import TOOL_REGISTRY
from mcp_telegram.tools.unread import GetUnreadSummary, get_unread_summary


@pytest.mark.asyncio
async def test_unread_summary_renders_dialog_and_observation_times_in_requested_timezone() -> None:
    connection = MagicMock()
    connection.get_unread_summary = AsyncMock(
        return_value={
            "ok": True,
            "data": {
                "dialogs": [
                    {
                        "dialog_id": 7,
                        "name": "Alice",
                        "dialog_type": "User",
                        "unread_count": None,
                        "unread_mark": True,
                        "unread_mentions_count": 0,
                        "unread_reactions_count": 0,
                        "archived": False,
                        "last_message_at": 1_700_000_000,
                    }
                ],
                "count": 1,
                "total_matching": 1,
                "truncated": False,
                "source_observation": {
                    "status": "complete",
                    "completed_at": 1_700_000_100,
                    "observed_count": 4,
                    "visible_count": 4,
                },
            },
        }
    )
    get_unread_summary_mock = cast(AsyncMock, connection.get_unread_summary)

    @asynccontextmanager
    async def fake_connection():
        yield connection

    with patch("mcp_telegram.tools.unread.daemon_connection", fake_connection):
        result = await get_unread_summary(GetUnreadSummary(timezone="Asia/Almaty"))

    payload = cast(dict[str, object], result.structured_content)
    row = cast(list[dict[str, object]], payload["dialogs"])[0]
    observation = cast(dict[str, object], payload["source_observation"])
    assert row["last_message_at"] == "2023-11-15T04:13:20+06:00"
    assert observation["completed_at"] == "2023-11-15T04:15:00+06:00"
    assert cast(dict[str, object], payload["time_context"])["timezone"] == "Asia/Almaty"
    get_unread_summary_mock.assert_called_once_with(limit=50)


def test_unread_summary_input_has_bounded_limit_and_no_scope() -> None:
    schema = GetUnreadSummary.model_json_schema()
    properties = cast(dict[str, object], schema["properties"])
    assert "scope" not in properties
    assert cast(dict[str, object], schema)["additionalProperties"] is False
    assert cast(dict[str, object], properties["limit"])["default"] == 50
    with pytest.raises(ValueError):
        GetUnreadSummary(limit=201)
    with pytest.raises(ValueError):
        GetUnreadSummary.model_validate({"scope": "all"})


def test_unread_summary_output_preserves_nullable_mark_and_shared_timezone_context() -> None:
    schema = TOOL_REGISTRY["get_unread_summary"].output_schema
    assert schema is not None
    properties = cast(dict[str, object], schema["properties"])
    assert "timezone" not in properties
    dialogs = cast(dict[str, object], properties["dialogs"])
    item = cast(dict[str, object], dialogs["items"])
    mark = cast(dict[str, object], cast(dict[str, object], item["properties"])["unread_mark"])
    assert mark["type"] == ["boolean", "null"]
