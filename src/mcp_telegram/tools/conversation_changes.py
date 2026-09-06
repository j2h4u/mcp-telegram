"""MCP surface for durable user-relevant conversation changes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from pydantic import Field, model_validator

from ..conversation_change_contracts import CHANGE_KINDS
from ..temporal import parse_utc_boundary
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    daemon_connection,
    error_result,
    mcp_tool,
    structured_result,
)

ChangeKind = Literal["edit", "deleted_message", "access_lost", "access_restored"]

_TEXT_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "untrusted_content": {"type": "boolean", "const": True},
        "last_known_text": {"type": ["string", "null"]},
        "before_text": {"type": ["string", "null"]},
        "after_text": {"type": ["string", "null"]},
        "provenance": {"type": ["string", "null"]},
        "confidence": {"type": "string"},
    },
    "required": [
        "untrusted_content",
        "last_known_text",
        "before_text",
        "after_text",
        "provenance",
        "confidence",
    ],
    "additionalProperties": False,
}

CONVERSATION_CHANGES_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer"},
                    "kind": {"type": "string", "enum": list(CHANGE_KINDS)},
                    "occurred_at": {"type": "integer"},
                    "time_basis": {"type": "string", "enum": ["telegram", "observed"]},
                    "dialog_id": {"type": "integer"},
                    "dialog_title": {"type": ["string", "null"]},
                    "message_id": {"type": ["integer", "null"]},
                    "version": {"type": ["integer", "null"]},
                    "summary": {"type": "string"},
                    "reason_code": {"type": ["string", "null"]},
                    "access_change_cause": {
                        "type": ["string", "null"],
                        "enum": ["self_left", "removed_by_admin", "banned_by_admin", "unknown", None],
                    },
                    "actor_id": {"type": ["integer", "null"]},
                    "text_evidence": {"anyOf": [_TEXT_EVIDENCE_SCHEMA, {"type": "null"}]},
                },
                "required": [
                    "event_id",
                    "kind",
                    "occurred_at",
                    "time_basis",
                    "dialog_id",
                    "dialog_title",
                    "message_id",
                    "version",
                    "summary",
                    "reason_code",
                    "access_change_cause",
                    "actor_id",
                    "text_evidence",
                ],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "next_navigation": {"type": ["string", "null"]},
        "coverage": {
            "type": "object",
            "properties": {
                "message_changes": {"type": "string", "const": "incoming_human_direct_messages"},
                "access_changes": {"type": "string", "const": "synced_dialogs"},
            },
            "required": ["message_changes", "access_changes"],
            "additionalProperties": False,
        },
    },
    "required": ["events", "count", "has_more", "next_navigation", "coverage"],
    "additionalProperties": False,
}


class ListConversationChanges(ToolArgs):
    """List durable changes to conversations: incoming human-DM edits and deletions,
    plus access loss and restoration for synced dialogs. Results are newest-observed
    first and use a stable snapshot while following pagination."""

    kinds: list[ChangeKind] | None = Field(
        default=None,
        min_length=1,
        description="Optional change types; omit to include edits, deletions, and access changes.",
    )
    dialog_id: int | None = Field(default=None, description="Optional exact Telegram dialog ID.")
    since_utc: str | None = Field(
        default=None,
        description="Inclusive RFC3339 UTC lower bound (Z or +00:00).",
    )
    until_utc: str | None = Field(
        default=None,
        description="Exclusive RFC3339 UTC upper bound (Z or +00:00).",
    )
    page_limit: int = Field(default=50, ge=1, le=500, strict=True)
    navigation: str | None = Field(default=None, description="Opaque cursor returned by the preceding page.")

    @model_validator(mode="after")
    def validate_bounds(self) -> ListConversationChanges:
        since = parse_utc_boundary(self.since_utc, field="since_utc")
        until = parse_utc_boundary(self.until_utc, field="until_utc")
        if since is not None and until is not None and since >= until:
            raise ValueError("since_utc must be earlier than until_utc.")
        return self


@mcp_tool(
    name="list_conversation_changes",
    title="Conversation Changes",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    output_schema=CONVERSATION_CHANGES_OUTPUT_SCHEMA,
)
async def list_conversation_changes(args: ListConversationChanges) -> ToolResult:
    has_cursor = args.navigation is not None
    has_filter = any((args.kinds, args.dialog_id, args.since_utc, args.until_utc))
    try:
        async with daemon_connection() as conn:
            response = await conn.list_conversation_changes(
                since_utc=parse_utc_boundary(args.since_utc, field="since_utc"),
                until_utc=parse_utc_boundary(args.until_utc, field="until_utc"),
                kinds=list(args.kinds) if args.kinds is not None else None,
                dialog_id=args.dialog_id,
                page_limit=args.page_limit if "page_limit" in args.model_fields_set else None,
                navigation=args.navigation,
            )
    except DaemonNotRunningError as exc:
        return error_result(
            _daemon_not_running_text(exc),
            result_count=0,
            has_cursor=has_cursor,
            page_depth=1,
            has_filter=has_filter,
        )
    if err := _check_daemon_response(
        response,
        action="Restart the traversal without navigation if the cursor is invalid.",
        result_count=0,
        has_cursor=has_cursor,
        page_depth=1,
        has_filter=has_filter,
    ):
        return err
    data = response.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("events"), list):
        return error_result(
            "Error: daemon returned an invalid conversation changes response.",
            result_count=0,
            has_cursor=has_cursor,
            page_depth=1,
            has_filter=has_filter,
        )
    output = dict(cast(Mapping[str, object], data))
    page_depth = int(cast(int, output.pop("_page_depth", 1)))
    return structured_result(
        output,
        result_count=len(cast(list[object], output["events"])),
        has_cursor=args.navigation is not None or bool(output.get("next_navigation")),
        page_depth=page_depth,
        has_filter=has_filter,
    )


__all__ = ["ListConversationChanges", "list_conversation_changes"]
