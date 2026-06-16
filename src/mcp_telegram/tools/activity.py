"""GetMyRecentActivity MCP tool — Phase 999.1."""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import Field, field_validator

from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    daemon_connection,
    error_result,
    mcp_tool,
    structured_result,
)
from .structured import telegram_content

DEFAULT_ACTIVITY_DIALOG_KINDS = ("group", "forum")
_ALLOWED_ACTIVITY_DIALOG_KINDS = {"all", "user", "bot", "group", "forum", "channel", "unknown"}
_ACTIVITY_DIALOG_KIND_ALIASES = {
    "dm": ("user", "bot"),
    "dms": ("user", "bot"),
    "private": ("user", "bot"),
    "personal": ("user", "bot"),
    "direct": ("user", "bot"),
    "groups": ("group", "forum"),
    "supergroup": ("group",),
    "supergroups": ("group",),
    "chat": ("group",),
    "chats": ("group",),
    "forums": ("forum",),
}

GET_MY_RECENT_ACTIVITY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "since_hours": {"type": "integer"},
        "limit": {"type": "integer"},
        "dialog_kinds": {"type": "array", "items": {"type": "string"}},
        "scan_status": {"type": "string"},
        "scanned_at": {"type": ["integer", "null"]},
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": "integer"},
                    "dialog_name": {"type": ["string", "null"]},
                    "dialog_type": {"type": "string"},
                    "dialog_category": {"type": "string"},
                    "message_id": {"type": "integer"},
                    "sent_at": {"type": ["integer", "null"]},
                    "text": {"type": "string"},
                    "content": {"type": "object"},
                    "sync_status": {"type": ["string", "null"]},
                    "reply_count": {"type": "integer"},
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
                    "dialog_type",
                    "dialog_category",
                    "message_id",
                    "sent_at",
                    "text",
                    "content",
                    "sync_status",
                    "reply_count",
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
        "dialog_kinds",
        "scan_status",
        "scanned_at",
        "comments",
        "count",
        "result_count_semantics",
    ],
    "additionalProperties": False,
}


class GetMyRecentActivity(ToolArgs):
    """Show messages the connected account sent in recent non-DM chats by default.

    Reads the local own-message archive, not Telegram. Returns one block per
    sent message with dialog kind/category, reply_count, reactions, and
    navigation arguments for list_messages. Default dialog_kinds=["group",
    "forum"] excludes DMs; pass ["user","bot"], ["channel"], or ["all"] when
    needed. Use scan_status to distinguish an empty archive from no activity.
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
    dialog_kinds: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ACTIVITY_DIALOG_KINDS),
        min_length=1,
        max_length=8,
        description=(
            "Dialog kinds to include. Default ['group','forum'] excludes DMs. "
            "Use ['user','bot'] for personal/bot DMs, ['channel'] for channels, or ['all'] for no filter."
        ),
    )

    @field_validator("dialog_kinds", mode="before")
    @classmethod
    def _normalize_dialog_kinds(cls, value: object) -> list[str]:
        if value is None:
            return list(DEFAULT_ACTIVITY_DIALOG_KINDS)
        if isinstance(value, str):
            raw_values: list[object] = [value]
        elif isinstance(value, list | tuple | set):
            raw_values = list(value)
        else:
            raise ValueError("dialog_kinds must be a list of strings")

        normalized_values: list[str] = []
        for raw in raw_values:
            if not isinstance(raw, str):
                raise ValueError("dialog_kinds entries must be strings")
            normalized = raw.strip().lower()
            if not normalized:
                continue
            expanded = _ACTIVITY_DIALOG_KIND_ALIASES.get(normalized, (normalized,))
            for kind in expanded:
                if kind not in _ALLOWED_ACTIVITY_DIALOG_KINDS:
                    allowed = ", ".join(sorted(_ALLOWED_ACTIVITY_DIALOG_KINDS))
                    raise ValueError(f"dialog_kinds entries must be one of: {allowed}")
                if kind not in normalized_values:
                    normalized_values.append(kind)

        if "all" in normalized_values:
            return ["all"]
        if not normalized_values:
            raise ValueError("dialog_kinds must include at least one kind")
        return normalized_values


def _structured_comment(comment: dict[str, Any]) -> dict[str, object]:
    dialog_id = int(comment.get("dialog_id") or 0)
    message_id = int(comment.get("message_id") or 0)
    text = comment.get("text") or ""
    return {
        "dialog_id": dialog_id,
        "dialog_name": comment.get("dialog_name"),
        "dialog_type": str(comment.get("dialog_type") or "unknown"),
        "dialog_category": str(comment.get("dialog_category") or "unknown"),
        "message_id": message_id,
        "sent_at": comment.get("sent_at"),
        "text": text,
        "content": telegram_content(text, "message_text"),
        "sync_status": comment.get("sync_status"),
        "reply_count": int(comment.get("reply_count") or 0),
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


def _chronological_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        comments,
        key=lambda comment: (
            int(comment.get("sent_at") or 0),
            int(comment.get("dialog_id") or 0),
            int(comment.get("message_id") or 0),
        ),
    )


@mcp_tool(
    name="get_my_recent_activity",
    title="Recent Activity",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    output_schema=GET_MY_RECENT_ACTIVITY_OUTPUT_SCHEMA,
)
async def get_my_recent_activity(args: GetMyRecentActivity) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_my_recent_activity(
                since_hours=args.since_hours,
                limit=args.limit,
                dialog_kinds=args.dialog_kinds,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if err := _check_daemon_response(response):
        return err

    data = response.get("data") or {}
    comments = data.get("comments") or []
    dialog_kinds = data.get("dialog_kinds") or args.dialog_kinds
    scan_status = data.get("scan_status") or "never_run"
    scanned_at = data.get("scanned_at")
    structured_comments = [_structured_comment(comment) for comment in _chronological_comments(comments)]
    structured_content = {
        "since_hours": args.since_hours,
        "limit": args.limit,
        "dialog_kinds": dialog_kinds,
        "scan_status": scan_status,
        "scanned_at": scanned_at,
        "comments": structured_comments,
        "count": len(structured_comments),
        "result_count_semantics": "count is the number of own-message activity rows returned in this response",
    }

    return structured_result(
        structured_content,
        result_count=len(comments),
        has_filter=dialog_kinds != ["all"],
    )
