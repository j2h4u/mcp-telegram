from __future__ import annotations

import logging

from ..errors import (
    no_usage_data_text,
    usage_stats_query_error_text,
)
from ._base import DaemonNotRunningError, ToolArgs, ToolResult, _daemon_not_running_text, _text_response, daemon_connection, mcp_tool

logger = logging.getLogger(__name__)


def format_usage_summary(stats: dict) -> str:
    """Generate <100 token natural-language summary of usage patterns.

    Input dict keys:
    - tool_distribution: dict[str, int] — {tool_name: count}
    - error_distribution: dict[str, int] — {error_type: count}
    - max_page_depth: int
    - dialogs_with_deep_scroll: int (estimated)
    - total_calls: int
    - filter_count: int
    - latency_median_ms: float
    - latency_p95_ms: float

    Output: natural-language string, target 60-80 tokens, < 100 hard limit.
    """
    parts = []

    if stats.get("tool_distribution"):
        most_used = sorted(stats["tool_distribution"].items(), key=lambda x: x[1], reverse=True)[:2]
        if most_used:
            most_used_name, most_used_count = most_used[0]
            most_used_pct = int(most_used_count * 100 / stats["total_calls"]) if stats["total_calls"] > 0 else 0
            parts.append(f"Most active: {most_used_name} ({most_used_pct}% of calls)")

    if stats.get("max_page_depth", 0) >= 5:
        parts.append(f"Deep scrolling detected: max page depth {stats['max_page_depth']}")

    if stats.get("error_distribution"):
        errors_str = ", ".join(
            [f"{err} ({cnt})" for err, cnt in sorted(stats["error_distribution"].items(), key=lambda x: x[1], reverse=True)[:3]]
        )
        parts.append(f"Errors: {errors_str}")

    if stats.get("total_calls", 0) > 0 and stats.get("filter_count", 0) > 0:
        filter_pct = int(stats["filter_count"] * 100 / stats["total_calls"])
        parts.append(f"Filtered queries: {filter_pct}%")

    median = stats.get("latency_median_ms", 0)
    p95 = stats.get("latency_p95_ms", 0)
    if median or p95:
        parts.append(f"Response time: {median:.0f}ms median, {p95:.0f}ms p95")

    summary = " ".join(parts)

    # Safety: if summary exceeds 100 tokens, truncate gracefully
    tokens = summary.split()
    if len(tokens) > 100:
        summary = " ".join(tokens[:100]) + "..."

    return summary


class GetUsageStats(ToolArgs):
    """Get actionable usage statistics from telemetry (last 30 days)."""

    pass


@mcp_tool("secondary/helper")
async def get_usage_stats(args: GetUsageStats) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_usage_stats()
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if not response.get("ok"):
        error_msg = response.get("error", "Unknown error")
        return ToolResult(content=_text_response(usage_stats_query_error_text(error_msg)))

    stats = response.get("data", {})
    if not stats or stats.get("total_calls", 0) == 0:
        return ToolResult(content=_text_response(no_usage_data_text()))

    summary = format_usage_summary(stats)
    return ToolResult(content=_text_response(summary if summary else no_usage_data_text()))
