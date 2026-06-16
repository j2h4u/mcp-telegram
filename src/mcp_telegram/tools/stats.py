import logging

from pydantic import Field

from ..errors import (
    no_usage_data_text,
    usage_stats_query_error_text,
)
from ..resolver import parse_exact_dialog_id
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _daemon_not_running_text,
    daemon_connection,
    error_result,
    mcp_tool,
    structured_result,
)

logger = logging.getLogger(__name__)
_DEEP_PAGE_DEPTH_THRESHOLD = 5
_USAGE_SUMMARY_TOKEN_LIMIT = 100

GET_USAGE_STATS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "empty": {"type": "boolean"},
        "total_calls": {"type": "integer"},
        "tool_distribution": {"type": "object"},
        "error_distribution": {"type": "object"},
        "max_page_depth": {"type": "integer"},
        "filter_count": {"type": "integer"},
        "latency_median_ms": {"type": ["number", "null"]},
        "latency_p95_ms": {"type": ["number", "null"]},
    },
    "required": [
        "summary",
        "empty",
        "total_calls",
        "tool_distribution",
        "error_distribution",
        "max_page_depth",
        "filter_count",
        "latency_median_ms",
        "latency_p95_ms",
    ],
    "additionalProperties": False,
}


GET_DIALOG_STATS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog": {"type": "string"},
        "dialog_id": {"type": ["integer", "null"]},
        "top_n": {"type": "integer"},
        "top_reactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "emoji": {"type": ["string", "null"]},
                    "count": {"type": "integer"},
                },
                "required": ["emoji", "count"],
                "additionalProperties": False,
            },
        },
        "top_mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": ["string", "null"]},
                    "count": {"type": "integer"},
                },
                "required": ["value", "count"],
                "additionalProperties": False,
            },
        },
        "top_hashtags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": ["string", "null"]},
                    "count": {"type": "integer"},
                },
                "required": ["value", "count"],
                "additionalProperties": False,
            },
        },
        "top_forwards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "peer_id": {"type": ["integer", "null"]},
                    "name": {"type": ["string", "null"]},
                    "count": {"type": "integer"},
                },
                "required": ["peer_id", "name", "count"],
                "additionalProperties": False,
            },
        },
        "section_counts": {
            "type": "object",
            "properties": {
                "top_reactions": {"type": "integer"},
                "top_mentions": {"type": "integer"},
                "top_hashtags": {"type": "integer"},
                "top_forwards": {"type": "integer"},
            },
            "required": ["top_reactions", "top_mentions", "top_hashtags", "top_forwards"],
            "additionalProperties": False,
        },
        "count": {"type": "integer"},
        "result_count_semantics": {"type": "string"},
    },
    "required": [
        "dialog",
        "dialog_id",
        "top_n",
        "top_reactions",
        "top_mentions",
        "top_hashtags",
        "top_forwards",
        "section_counts",
        "count",
        "result_count_semantics",
    ],
    "additionalProperties": False,
}


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

    if stats.get("max_page_depth", 0) >= _DEEP_PAGE_DEPTH_THRESHOLD:
        parts.append(f"Deep scrolling detected: max page depth {stats['max_page_depth']}")

    if stats.get("error_distribution"):
        errors_str = ", ".join(
            [
                f"{err} ({cnt})"
                for err, cnt in sorted(stats["error_distribution"].items(), key=lambda x: x[1], reverse=True)[:3]
            ]
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
    if len(tokens) > _USAGE_SUMMARY_TOKEN_LIMIT:
        summary = " ".join(tokens[:_USAGE_SUMMARY_TOKEN_LIMIT]) + "..."

    return summary


class GetUsageStats(ToolArgs):
    """Get actionable usage statistics from telemetry (last 30 days)."""


def _usage_structured_content(stats: dict, *, summary: str, empty: bool) -> dict[str, object]:
    return {
        "summary": summary,
        "empty": empty,
        "total_calls": int(stats.get("total_calls", 0) or 0),
        "tool_distribution": stats.get("tool_distribution") or {},
        "error_distribution": stats.get("error_distribution") or {},
        "max_page_depth": int(stats.get("max_page_depth", 0) or 0),
        "filter_count": int(stats.get("filter_count", 0) or 0),
        "latency_median_ms": stats.get("latency_median_ms"),
        "latency_p95_ms": stats.get("latency_p95_ms"),
    }


@mcp_tool(
    name="get_usage_stats",
    title="Usage Stats",
    posture="secondary/helper",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    output_schema=GET_USAGE_STATS_OUTPUT_SCHEMA,
)
async def get_usage_stats(args: GetUsageStats) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_usage_stats()
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if not response.get("ok"):
        error_msg = response.get("error", "Unknown error")
        return error_result(usage_stats_query_error_text(error_msg))

    stats = response.get("data", {})
    if not stats or stats.get("total_calls", 0) == 0:
        summary = no_usage_data_text()
        return structured_result(_usage_structured_content(stats, summary=summary, empty=True))

    summary = format_usage_summary(stats)
    summary_text = summary or no_usage_data_text()
    return structured_result(_usage_structured_content(stats, summary=summary_text, empty=False))


class GetDialogStats(ToolArgs):
    """Return aggregate analytics for one synced dialog: top reactions (emoji+count),
    top @mentions, top #hashtags, and top forward sources. Pass a dialog name, @username,
    or numeric dialog_id. Requires the dialog to be synced (use MarkDialogForSync first);
    non-synced dialogs return an actionable error.

    top_n controls how many entries are returned in each category independently —
    e.g. top_n=5 returns up to 5 reactions, 5 mentions, 5 hashtags, and 5 forward sources."""

    dialog: str = Field(max_length=500, description="Dialog name, @username, or numeric id")
    top_n: int = Field(
        default=5,
        ge=1,
        le=20,
        description="How many top entries to return per category (reactions, mentions, hashtags, forward sources)",
    )


@mcp_tool(
    name="get_dialog_stats",
    title="Dialog Stats",
    posture="secondary/helper",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    output_schema=GET_DIALOG_STATS_OUTPUT_SCHEMA,
)
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
        return error_result(_daemon_not_running_text())

    if not response.get("ok"):
        error = response.get("error", "")
        msg = response.get("message", "Request failed.")
        if error == "not_synced":
            return error_result(
                f"Error: dialog is not synced. {msg}\n"
                "Action: Call MarkDialogForSync for this dialog, wait for sync completion, then retry GetDialogStats."
            )
        if error == "dialog_not_found":
            from ..errors import dialog_not_found_text

            return error_result(dialog_not_found_text(args.dialog, retry_tool="GetDialogStats"))
        return error_result(
            f"Error: {error}: {msg}\n"
            "Action: Retry GetDialogStats with a corrected dialog id/name, or call ListDialogs first."
        )

    data = response.get("data", {})
    reactions = data.get("top_reactions", [])
    mentions = data.get("top_mentions", [])
    hashtags = data.get("top_hashtags", [])
    forwards = data.get("top_forwards", [])

    total = len(reactions) + len(mentions) + len(hashtags) + len(forwards)
    structured_content = {
        "dialog": args.dialog,
        "dialog_id": data.get("dialog_id", dialog_id),
        "top_n": args.top_n,
        "top_reactions": reactions,
        "top_mentions": mentions,
        "top_hashtags": hashtags,
        "top_forwards": forwards,
        "section_counts": {
            "top_reactions": len(reactions),
            "top_mentions": len(mentions),
            "top_hashtags": len(hashtags),
            "top_forwards": len(forwards),
        },
        "count": total,
        "result_count_semantics": "count is the total number of aggregate rows across reactions, mentions, hashtags, and forward sources",
    }
    return structured_result(structured_content, result_count=total)
