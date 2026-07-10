"""MCP-layer tests for GetDialogStats tool (260416-frw)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol, cast
from unittest.mock import AsyncMock, patch

import pytest

from mcp_telegram.tools._base import DaemonNotRunningError
from mcp_telegram.tools.stats import GetDialogStats, format_usage_summary, get_dialog_stats


class _TextContent(Protocol):
    text: str


@dataclass
class _StatsConn:
    get_dialog_stats: AsyncMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(response: dict) -> _StatsConn:
    """Return a mock DaemonConnection whose get_dialog_stats returns response."""
    return _StatsConn(get_dialog_stats=AsyncMock(return_value=response))


@asynccontextmanager
async def _patched_connection(conn: _StatsConn) -> AsyncIterator[_StatsConn]:
    yield conn


def _patch_daemon(conn: _StatsConn):
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
    structured = cast(dict[str, object], content.structured_content)
    assert structured["dialog"] == "Chat Foo"
    assert structured["dialog_id"] == 1
    assert structured["top_n"] == 5
    assert structured["top_reactions"] == data["top_reactions"]
    assert structured["top_mentions"] == data["top_mentions"]
    assert structured["top_hashtags"] == data["top_hashtags"]
    assert structured["top_forwards"] == data["top_forwards"]
    assert structured["section_counts"] == {
        "top_reactions": 2,
        "top_mentions": 2,
        "top_hashtags": 2,
        "top_forwards": 2,
    }
    assert structured["count"] == 8


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
    text = cast(_TextContent, content.content[0]).text
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
    structured = cast(dict[str, object], content.structured_content)
    assert structured["top_reactions"] == []
    assert structured["top_mentions"] == []
    assert structured["top_hashtags"] == []
    assert structured["top_forwards"] == []
    assert structured["count"] == 0


@pytest.mark.asyncio
async def test_get_dialog_stats_daemon_not_running() -> None:
    """GetDialogStats returns daemon-not-running message when daemon is unreachable."""

    @asynccontextmanager
    async def _raise_not_running() -> AsyncIterator[None]:
        raise DaemonNotRunningError("Sync daemon is not running.")
        if False:
            yield

    with patch("mcp_telegram.tools.stats.daemon_connection", _raise_not_running):
        content = await get_dialog_stats(GetDialogStats(dialog="Any Chat"))

    assert content.is_error is True
    text = cast(_TextContent, content.content[0]).text
    assert "mcp-telegram sync" in text or "not running" in text.lower()


# ---------------------------------------------------------------------------
# format_usage_summary
# ---------------------------------------------------------------------------


def test_format_usage_summary_most_active_tool() -> None:
    stats: dict[str, object] = {"total_calls": 100, "tool_distribution": {"list_messages": 60, "get_inbox": 40}}
    result = format_usage_summary(stats)
    assert "list_messages" in result
    assert "60%" in result


def test_format_usage_summary_deep_scrolling_detected() -> None:
    stats: dict[str, object] = {"total_calls": 50, "tool_distribution": {}, "max_page_depth": 7}
    result = format_usage_summary(stats)
    assert "Deep scrolling detected" in result
    assert "7" in result


def test_format_usage_summary_no_deep_scrolling_below_threshold() -> None:
    stats: dict[str, object] = {"total_calls": 50, "tool_distribution": {}, "max_page_depth": 4}
    result = format_usage_summary(stats)
    assert "Deep scrolling" not in result


def test_format_usage_summary_errors() -> None:
    stats: dict[str, object] = {"total_calls": 10, "error_distribution": {"ValueError": 3, "TimeoutError": 2}}
    result = format_usage_summary(stats)
    assert "ValueError (3)" in result
    assert "TimeoutError (2)" in result


def test_format_usage_summary_filtered_queries() -> None:
    stats: dict[str, object] = {"total_calls": 100, "filter_count": 25, "tool_distribution": {}}
    result = format_usage_summary(stats)
    assert "Filtered queries: 25%" in result


def test_format_usage_summary_latency() -> None:
    stats: dict[str, object] = {"total_calls": 10, "latency_median_ms": 45.7, "latency_p95_ms": 120.3}
    result = format_usage_summary(stats)
    assert "46ms median" in result
    assert "120ms p95" in result


def test_format_usage_summary_handles_large_input_gracefully() -> None:
    stats: dict[str, object] = {
        "total_calls": 10000,
        "tool_distribution": {f"tool_name_{i}": i for i in range(500)},
        "error_distribution": {f"error_type_{i}": i for i in range(500)},
        "max_page_depth": 20,
        "filter_count": 5000,
        "latency_median_ms": 100.0,
        "latency_p95_ms": 500.0,
    }
    result = format_usage_summary(stats)
    assert isinstance(result, str)
    assert len(result) < 2000


def test_format_usage_summary_empty_stats() -> None:
    result = format_usage_summary({"total_calls": 0})
    assert result == ""
