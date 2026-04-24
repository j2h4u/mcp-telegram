"""GetMyRecentActivity MCP tool — Phase 999.1."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ._base import (
    ToolArgs,
    ToolResult,
    _text_response,
    daemon_connection,
    mcp_tool,
)


class GetMyRecentActivity(ToolArgs):
    """[primary] Show messages you sent across all chats.

    Reads from the local own-message archive populated by the daemon's
    activity_sync loop — zero Telegram API calls in the hot path.

    Per-comment granularity: if you sent 3 messages in the same group,
    the response contains 3 separate blocks (not one collapsed entry).

    Use `scan_status` to distinguish `never_run` (archive empty — backfill
    has not completed yet) from `complete` + empty result (you were quiet).
    """

    since_hours: int = Field(
        default=168,
        ge=1,
        le=8760,
        description="Look-back window in hours. Default 168 = 7 days.",
    )
    limit: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="Maximum number of per-comment blocks to return.",
    )


def _format_block(comment: dict[str, Any]) -> str:
    ts = comment.get("sent_at") or 0
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    dialog_name = comment.get("dialog_name") or str(comment.get("dialog_id", "?"))
    text = (comment.get("text") or "").replace("\n", " ")
    block = (
        f"[{dialog_name}] {dt}  {text}\n"
        f"  nav: dialog_id={comment.get('dialog_id')} message_id={comment.get('message_id')}"
    )
    reactions = comment.get("reactions") or []
    if reactions:
        rx_str = "  ".join(f"{r['emoji']}×{r['count']}" for r in reactions)
        block += f"\n  reactions: {rx_str}"
    return block


@mcp_tool("primary", annotations=ToolAnnotations(readOnlyHint=True))
async def get_my_recent_activity(args: GetMyRecentActivity) -> ToolResult:
    async with daemon_connection() as conn:
        response = await conn.get_my_recent_activity(
            since_hours=args.since_hours,
            limit=args.limit,
        )

    data = response.get("data") or {}
    comments = data.get("comments") or []
    scan_status = data.get("scan_status") or "never_run"
    scanned_at = data.get("scanned_at")

    header_lines: list[str] = []
    if scan_status == "never_run":
        header_lines.append(
            "Scan status: never run — backfill has not completed yet."
        )
    elif scan_status == "in_progress":
        header_lines.append(
            "Scan status: in progress — backfill still running, results may be incomplete."
        )
    else:
        if scanned_at:
            dt = datetime.fromtimestamp(int(scanned_at), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            header_lines.append(f"Scanned at: {dt}")

    if not comments:
        body = f"No recent activity in the last {args.since_hours}h."
    else:
        body = "\n\n".join(_format_block(c) for c in comments)

    output = "\n".join(header_lines + [body]) if header_lines else body
    return ToolResult(content=_text_response(output), result_count=len(comments))
