"""MCP-layer tests for GetDialogStats tool (260416-frw)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import AsyncMock, patch

import pytest
from devtools.mcp_client.client import _assert_step_expectations
from jsonschema import validate

from mcp_telegram import server
from mcp_telegram.tools._base import TOOL_REGISTRY, DaemonNotRunningError, omit_none_mapping_values
from mcp_telegram.tools.stats import GetDialogStats, GetUsageStats, get_dialog_stats, get_usage_stats


class _TextContent(Protocol):
    text: str


@dataclass
class _StatsConn:
    get_dialog_stats: AsyncMock
    get_usage_stats: AsyncMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(response: dict) -> _StatsConn:
    """Return a mock DaemonConnection whose get_dialog_stats returns response."""
    return _StatsConn(
        get_dialog_stats=AsyncMock(return_value=response),
        get_usage_stats=AsyncMock(return_value=response),
    )


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
@pytest.mark.parametrize(
    ("sync_status", "coverage", "horizon"),
    [
        ("not_synced", "unknown", None),
        ("own_only", "partial", None),
        ("synced", "partial", None),
        ("synced", "complete_as_of", 1),
        ("access_lost", "partial", None),
    ],
)
async def test_get_dialog_stats_structures_sections(sync_status: str, coverage: str, horizon: int | None) -> None:
    """GetDialogStats with populated data returns four structured sections."""
    data = {
        "dialog_id": 1,
        "sync_status": sync_status,
        "coverage": coverage,
        "observation_horizon_at": horizon,
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
    assert structured["sync_status"] == sync_status
    assert structured["coverage"] == coverage
    assert structured["observation_horizon_at"] == ("1970-01-01T00:00:01+00:00" if horizon is not None else None)
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
    schema = TOOL_REGISTRY["get_dialog_stats"].output_schema
    assert schema is not None
    validate(instance=omit_none_mapping_values(structured), schema=schema)


@pytest.mark.asyncio
async def test_get_dialog_stats_query_error() -> None:
    """An actual query failure remains actionable without requiring sync enrollment."""
    conn = _make_conn(
        {
            "ok": False,
            "error": "database_locked",
            "message": "Query failed.",
        }
    )

    with _patch_daemon(conn):
        content = await get_dialog_stats(GetDialogStats(dialog="Unknown Chat"))

    assert content.is_error is True
    text = cast(_TextContent, content.content[0]).text
    assert "database_locked" in text
    assert "Action:" in text
    assert "MarkDialogForSync" not in text


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
    assert structured["coverage"] == "unknown"
    assert structured["sync_status"] == "not_synced"
    assert structured["observation_horizon_at"] is None
    schema = TOOL_REGISTRY["get_dialog_stats"].output_schema
    assert schema is not None
    validate(instance=omit_none_mapping_values(structured), schema=schema)


@pytest.mark.parametrize("horizon", [None, 1])
async def test_get_dialog_stats_mcp_wire_horizon_and_smoke_fixture(
    monkeypatch: pytest.MonkeyPatch, horizon: int | None
) -> None:
    conn = _make_conn(
        {
            "ok": True,
            "data": {
                "dialog_id": 987654321,
                "sync_status": "synced" if horizon is not None else "not_synced",
                "coverage": "complete_as_of" if horizon is not None else "unknown",
                "observation_horizon_at": horizon,
            },
        }
    )
    monkeypatch.setattr(server, "_account_protection_status", AsyncMock(return_value={}))
    monkeypatch.setattr(server, "_schedule_telemetry", lambda _event: None)
    with _patch_daemon(conn):
        result = await server.call_tool("get_dialog_stats", {"dialog": "987654321", "top_n": 3})

    assert result.is_error is False
    assert result.content == []
    structured = cast(dict[str, object], result.structured_content)
    schema = server.tool_by_name["get_dialog_stats"].output_schema
    assert schema is not None
    validate(instance=structured, schema=schema)
    if horizon is not None:
        assert structured["observation_horizon_at"] == "1970-01-01T00:00:01+00:00"
    else:
        assert "observation_horizon_at" not in structured
        script_path = Path(__file__).parents[1] / "devtools/mcp_client/smoke-integration.json"
        script = cast(dict[str, object], json.loads(script_path.read_text(encoding="utf-8")))
        steps = cast(list[dict[str, object]], script["steps"])
        step = next(item for item in steps if item.get("name") == "get_dialog_stats")
        wire = cast(dict[str, object], result.model_dump(mode="json", by_alias=True, exclude_none=True))
        _assert_step_expectations(index=0, action="call_tool", result=wire, expect=step["expect"])


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


@pytest.mark.asyncio
async def test_get_usage_stats_returns_summary_for_populated_telemetry() -> None:
    conn = _make_conn(
        {
            "ok": True,
            "data": {
                "tool_distribution": {"list_messages": 8, "list_dialogs": 2},
                "error_distribution": {"timeout": 3},
                "total_calls": 10,
                "max_page_depth": 6,
                "filter_count": 4,
                "latency_median_ms": 125,
                "latency_p95_ms": 480,
            },
        }
    )

    with _patch_daemon(conn):
        content = await get_usage_stats(GetUsageStats())

    assert content.content == ()
    structured = cast(dict[str, object], content.structured_content)
    assert structured["empty"] is False
    assert structured["total_calls"] == 10
    assert structured["tool_distribution"] == {"list_messages": 8, "list_dialogs": 2}
    assert structured["filter_count"] == 4
    assert structured["latency_p95_ms"] == 480
    assert structured["summary"] == (
        "Most active: list_messages (80% of calls) Deep scrolling detected: max page depth 6 "
        "Errors: timeout (3) Filtered queries: 40% Response time: 125ms median, 480ms p95"
    )


@pytest.mark.asyncio
async def test_get_usage_stats_returns_actionable_error_for_daemon_failure() -> None:
    conn = _make_conn({"ok": False, "error": "database_locked"})

    with _patch_daemon(conn):
        content = await get_usage_stats(GetUsageStats())

    assert content.is_error is True
    text = cast(_TextContent, content.content[0]).text
    assert "database_locked" in text
    assert "Retry GetUsageStats later." in text
