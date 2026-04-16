
import logging

from pydantic import Field

from ..errors import (
    no_usage_data_text,
    usage_stats_query_error_text,
)
from ..resolver import parse_exact_dialog_id
from ._base import DaemonNotRunningError, ToolAnnotations, ToolArgs, ToolResult, _check_daemon_response, _daemon_not_running_text, _text_response, daemon_connection, mcp_tool

logger = logging.getLogger(__name__)


def format_usage_summary(stats: dict) -> str:
    """Generate <100 token natural-language summary of usage patterns.

    Input dict keys:
    - tool_distribution: dict[str, int] — {tool_name: count}
    - error_distribution: dict[str, int] — {error_type: count}
    - max_page_depth: int
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


@mcp_tool("secondary/helper", annotations=ToolAnnotations(readOnlyHint=True))
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


class GetDialogStats(ToolArgs):
    """Return aggregate analytics for one synced dialog: top reactions (emoji+count),
    top @mentions, top #hashtags, and top forward sources. Pass a dialog name, @username,
    or numeric dialog_id. Requires the dialog to be synced (use MarkDialogForSync first);
    non-synced dialogs return an actionable error.

    top_n controls how many entries are returned in each category independently —
    e.g. top_n=5 returns up to 5 reactions, 5 mentions, 5 hashtags, and 5 forward sources."""

    dialog: str = Field(max_length=500, description="Dialog name, @username, or numeric id")
    top_n: int = Field(default=5, ge=1, le=20, description="How many top entries to return per category (reactions, mentions, hashtags, forward sources)")


def _format_stats_section(title: str, entries: list[dict], key: str) -> list[str]:
    lines = [f"=== {title} ({len(entries)}) ==="]
    if not entries:
        lines.append("  (none)")
        return lines
    for e in entries:
        label = e.get(key) or "?"
        count = e.get("count", 0)
        lines.append(f"  {label} count={count}")
    return lines


@mcp_tool("secondary/helper", annotations=ToolAnnotations(readOnlyHint=True))
async def get_dialog_stats(args: GetDialogStats) -> ToolResult:
    dialog_id: int | None = parse_exact_dialog_id(args.dialog)
    dialog_name: str | None = None if dialog_id else args.dialog
    try:
        async with daemon_connection() as conn:
            response = await conn.get_dialog_stats(
                dialog_id=dialog_id or 0,
                dialog=dialog_name,
                limit=args.top_n,
            )
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if not response.get("ok"):
        error = response.get("error", "")
        msg = response.get("message", "Request failed.")
        if error == "not_synced":
            return ToolResult(content=_text_response(
                f"Error: dialog is not synced. {msg}"
            ))
        if error == "dialog_not_found":
            from ..errors import dialog_not_found_text
            return ToolResult(content=_text_response(
                dialog_not_found_text(args.dialog, retry_tool="GetDialogStats")
            ))
        return ToolResult(content=_text_response(f"Error: {error}: {msg}"))

    data = response.get("data", {})
    reactions = data.get("top_reactions", [])
    mentions = data.get("top_mentions", [])
    hashtags = data.get("top_hashtags", [])
    forwards = data.get("top_forwards", [])

    sections: list[str] = []
    sections += _format_stats_section("Top Reactions", reactions, "emoji")
    sections += _format_stats_section("Top Mentions", mentions, "value")
    sections += _format_stats_section("Top Hashtags", hashtags, "value")
    forwards_flat = [
        {"label": (f.get("name") or str(f.get("peer_id") or "?")), "count": f.get("count", 0)}
        for f in forwards
    ]
    sections += _format_stats_section("Top Forward Sources", forwards_flat, "label")

    total = len(reactions) + len(mentions) + len(hashtags) + len(forwards)
    return ToolResult(content=_text_response("\n".join(sections)), result_count=total)
