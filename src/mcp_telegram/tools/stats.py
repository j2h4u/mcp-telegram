from __future__ import annotations

import logging

from ..errors import (
    no_usage_data_text,
    usage_stats_query_error_text,
)
from ._base import DaemonNotRunningError, ToolArgs, ToolResult, _text_response, daemon_connection, mcp_tool

logger = logging.getLogger(__name__)


class GetUsageStats(ToolArgs):
    """Get actionable usage statistics from telemetry (last 30 days)."""

    pass


@mcp_tool("secondary/helper")
async def get_usage_stats(args: GetUsageStats) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_usage_stats()
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(
            "Sync daemon is not running.\nAction: Start it with: mcp-telegram sync"
        ))

    if not response.get("ok"):
        error_msg = response.get("error", "Unknown error")
        return ToolResult(content=_text_response(usage_stats_query_error_text(error_msg)))

    stats = response.get("data", {})
    if not stats or stats.get("total_calls", 0) == 0:
        return ToolResult(content=_text_response(no_usage_data_text()))

    from ..analytics import format_usage_summary
    summary = format_usage_summary(stats)
    return ToolResult(content=_text_response(summary if summary else no_usage_data_text()))
