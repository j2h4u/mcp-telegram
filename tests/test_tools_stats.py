"""MCP-layer tests for GetDialogStats tool (260416-frw)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.tools._base import DaemonNotRunningError
from mcp_telegram.tools.stats import GetDialogStats, get_dialog_stats

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(response: dict) -> MagicMock:
    """Return a mock DaemonConnection whose get_dialog_stats returns response."""
    conn = MagicMock()
    conn.get_dialog_stats = AsyncMock(return_value=response)
    return conn


@asynccontextmanager
async def _patched_connection(conn: MagicMock) -> AsyncIterator[MagicMock]:
    yield conn


def _patch_daemon(conn: MagicMock):
    """Return a context manager that patches daemon_connection in stats module."""
    return patch(
        "mcp_telegram.tools.stats.daemon_connection",
        return_value=_patched_connection(conn),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dialog_stats_structures_sections() -> None:
    """GetDialogStats with populated data returns four structured sections."""
    data = {
        "dialog_id": 1,
        "top_reactions": [
            {"emoji": "👍", "count": 4},
            {"emoji": "❤️", "count": 2},
        ],
        "top_mentions": [
            {"value": "@alice", "count": 3},
            {"value": "@bob", "count": 1},
        ],
        "top_hashtags": [
            {"value": "#python", "count": 5},
            {"value": "#rust", "count": 2},
        ],
        "top_forwards": [
            {"peer_id": 100, "name": "Channel A", "count": 3},
            {"peer_id": 200, "name": "Channel B", "count": 1},
        ],
    }
    conn = _make_conn({"ok": True, "data": data})

    with _patch_daemon(conn):
        content = await get_dialog_stats(GetDialogStats(dialog="Chat Foo"))

    assert content.content == ()
    assert content.structured_content is not None
    assert content.structured_content["dialog"] == "Chat Foo"
    assert content.structured_content["dialog_id"] == 1
    assert content.structured_content["top_n"] == 5
    assert content.structured_content["top_reactions"] == data["top_reactions"]
    assert content.structured_content["top_mentions"] == data["top_mentions"]
    assert content.structured_content["top_hashtags"] == data["top_hashtags"]
    assert content.structured_content["top_forwards"] == data["top_forwards"]
    assert content.structured_content["section_counts"] == {
        "top_reactions": 2,
        "top_mentions": 2,
        "top_hashtags": 2,
        "top_forwards": 2,
    }
    assert content.structured_content["count"] == 8


@pytest.mark.asyncio
async def test_get_dialog_stats_not_synced_error() -> None:
    """GetDialogStats with not_synced error returns actionable text referencing MarkDialogForSync."""
    conn = _make_conn(
        {
            "ok": False,
            "error": "not_synced",
            "message": "GetDialogStats requires a synced dialog. Use MarkDialogForSync first.",
        }
    )

    with _patch_daemon(conn):
        content = await get_dialog_stats(GetDialogStats(dialog="Unknown Chat"))

    assert content.is_error is True
    text = content.content[0].text
    assert "MarkDialogForSync" in text


@pytest.mark.asyncio
async def test_get_dialog_stats_empty_sections() -> None:
    """GetDialogStats with all empty lists returns result_count=0."""
    conn = _make_conn(
        {
            "ok": True,
            "data": {
                "dialog_id": 1,
                "top_reactions": [],
                "top_mentions": [],
                "top_hashtags": [],
                "top_forwards": [],
            },
        }
    )

    with _patch_daemon(conn):
        content = await get_dialog_stats(GetDialogStats(dialog="Empty Chat"))

    assert content.content == ()
    assert content.structured_content is not None
    assert content.structured_content["top_reactions"] == []
    assert content.structured_content["top_mentions"] == []
    assert content.structured_content["top_hashtags"] == []
    assert content.structured_content["top_forwards"] == []
    assert content.structured_content["count"] == 0


@pytest.mark.asyncio
async def test_get_dialog_stats_daemon_not_running() -> None:
    """GetDialogStats returns daemon-not-running message when daemon is unreachable."""

    @asynccontextmanager
    async def _raise_not_running() -> AsyncIterator[None]:
        raise DaemonNotRunningError("Sync daemon is not running.")
        yield  # noqa: unreachable

    with patch("mcp_telegram.tools.stats.daemon_connection", _raise_not_running):
        content = await get_dialog_stats(GetDialogStats(dialog="Any Chat"))

    assert content.is_error is True
    text = content.content[0].text
    assert "mcp-telegram sync" in text or "not running" in text.lower()
