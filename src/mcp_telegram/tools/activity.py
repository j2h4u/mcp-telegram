"""GetMyRecentActivity MCP tool — Phase 999.1."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ..formatter import frame_telegram_snippet
from .structured import telegram_content
from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    error_result,
    mcp_tool,
)

GET_MY_RECENT_ACTIVITY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "since_hours": {"type": "integer"},
        "limit": {"type": "integer"},
        "scan_status": {"type": "string"},
        "scanned_at": {"type": ["integer", "null"]},
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": "integer"},
                    "dialog_name": {"type": ["string", "null"]},
                    "message_id": {"type": "integer"},
                    "sent_at": {"type": ["integer", "null"]},
                    "text": {"type": "string"},
                    "content": {"type": "object"},
                    "sync_status": {"type": ["string", "null"]},
                    "reactions": {"type": "array", "items": {"type": "object"}},
                    "navigation": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "tool": {"type": "string"},
                            "arguments": {
                                "type": "object",
                                "properties": {
                                    "exact_dialog_id": {"type": "integer"},
                                    "anchor_message_id": {"type": "integer"},
                                },
                                "required": ["exact_dialog_id", "anchor_message_id"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["text", "tool", "arguments"],
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "dialog_id",
                    "dialog_name",
                    "message_id",
                    "sent_at",
                    "text",
                    "content",
                    "sync_status",
                    "reactions",
                    "navigation",
                ],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "result_count_semantics": {"type": "string"},
    },
    "required": [
        "since_hours",
        "limit",
        "scan_status",
        "scanned_at",
        "comments",
        "count",
        "result_count_semantics",
    ],
    "additionalProperties": False,
}


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
    text = frame_telegram_snippet(text)
    block = (
        f"[{dialog_name}] {dt}  {text}\n"
        f"  nav: dialog_id={comment.get('dialog_id')} message_id={comment.get('message_id')}"
    )
    sync_status = comment.get("sync_status")
    if sync_status and sync_status != "synced":
        block += f"\n  sync: {sync_status}"
    reactions = comment.get("reactions") or []
    if reactions:
        rx_str = "  ".join(f"{r['emoji']}×{r['count']}" for r in reactions)
        block += f"\n  reactions: {rx_str}"
    return block


def _structured_comment(comment: dict[str, Any]) -> dict[str, object]:
    dialog_id = int(comment.get("dialog_id") or 0)
    message_id = int(comment.get("message_id") or 0)
    text = comment.get("text") or ""
    return {
        "dialog_id": dialog_id,
        "dialog_name": comment.get("dialog_name"),
        "message_id": message_id,
        "sent_at": comment.get("sent_at"),
        "text": text,
        "content": telegram_content(text, "message_text"),
        "sync_status": comment.get("sync_status"),
        "reactions": comment.get("reactions") or [],
        "navigation": {
            "text": f"nav: dialog_id={dialog_id} message_id={message_id}",
            "tool": "list_messages",
            "arguments": {
                "exact_dialog_id": dialog_id,
                "anchor_message_id": message_id,
            },
        },
    }


@mcp_tool(
    name="get_my_recent_activity",
    title="Recent Activity",
    annotations=ToolAnnotations(readOnlyHint=True),
    output_schema=GET_MY_RECENT_ACTIVITY_OUTPUT_SCHEMA,
)
async def get_my_recent_activity(args: GetMyRecentActivity) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_my_recent_activity(
                since_hours=args.since_hours,
                limit=args.limit,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if err := _check_daemon_response(response):
        return err

    data = response.get("data") or {}
    comments = data.get("comments") or []
    scan_status = data.get("scan_status") or "never_run"
    scanned_at = data.get("scanned_at")
    structured_comments = [_structured_comment(comment) for comment in comments]
    structured_content = {
        "since_hours": args.since_hours,
        "limit": args.limit,
        "scan_status": scan_status,
        "scanned_at": scanned_at,
        "comments": structured_comments,
        "count": len(structured_comments),
        "result_count_semantics": "count is the number of own-message activity rows returned in this response",
    }

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
    return ToolResult(
        content=_text_response(output),
        structured_content=structured_content,
        result_count=len(comments),
    )
