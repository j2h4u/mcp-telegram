"""MCP-layer tests for GetDialogStats tool (260416-frw)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
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
async def test_get_dialog_stats_formats_sections() -> None:
    """GetDialogStats with populated data formats four section headers and entries."""
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

    # @mcp_tool wraps the runner and returns tool_result.content (list of TextContent)
    text = content[0].text  # type: ignore[index]
    assert "Top Reactions" in text
    assert "Top Mentions" in text
    assert "Top Hashtags" in text
    assert "Top Forward Sources" in text
    assert "count=4" in text
    assert "count=3" in text
    assert "count=5" in text
    assert "Channel A" in text


@pytest.mark.asyncio
async def test_get_dialog_stats_not_synced_error() -> None:
    """GetDialogStats with not_synced error returns actionable text referencing MarkDialogForSync."""
    conn = _make_conn({
        "ok": False,
        "error": "not_synced",
        "message": "GetDialogStats requires a synced dialog. Use MarkDialogForSync first.",
    })

    with _patch_daemon(conn):
        content = await get_dialog_stats(GetDialogStats(dialog="Unknown Chat"))

    text = content[0].text  # type: ignore[index]
    assert "MarkDialogForSync" in text


@pytest.mark.asyncio
async def test_get_dialog_stats_empty_sections() -> None:
    """GetDialogStats with all empty lists shows (none) in each section and result_count=0."""
    conn = _make_conn({
        "ok": True,
        "data": {
            "dialog_id": 1,
            "top_reactions": [],
            "top_mentions": [],
            "top_hashtags": [],
            "top_forwards": [],
        },
    })

    with _patch_daemon(conn):
        content = await get_dialog_stats(GetDialogStats(dialog="Empty Chat"))

    text = content[0].text  # type: ignore[index]
    assert text.count("(none)") == 4


@pytest.mark.asyncio
async def test_get_dialog_stats_daemon_not_running() -> None:
    """GetDialogStats returns daemon-not-running message when daemon is unreachable."""

    @asynccontextmanager
    async def _raise_not_running() -> AsyncIterator[None]:
        raise DaemonNotRunningError("Sync daemon is not running.")
        yield  # noqa: unreachable

    with patch("mcp_telegram.tools.stats.daemon_connection", _raise_not_running):
        content = await get_dialog_stats(GetDialogStats(dialog="Any Chat"))

    text = content[0].text  # type: ignore[index]
    assert "mcp-telegram sync" in text or "not running" in text.lower()
