import time
import typing as t

from pydantic import Field

from ..formatter import (
    _compute_inline_markers,
    _render_read_state_header,
    resolve_sender_label,
)
from ..models import DialogType, ReadMessage, ReadState
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
from .structured import TelegramContent, TelegramContentKind, structured_warning, telegram_content

GET_INBOX_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {"type": "string"},
        "limit": {"type": "integer"},
        "group_size_threshold": {"type": "integer"},
        "bootstrap_pending": {"type": "integer"},
        "coverage": {
            "type": "object",
            "properties": {
                "complete": {"type": "boolean"},
                "state": {"type": "string"},
                "bootstrap_pending_count": {"type": "integer"},
            },
            "required": ["complete", "state", "bootstrap_pending_count"],
            "additionalProperties": False,
        },
        "warnings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "severity": {"type": "string"},
                    "message": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["kind", "severity", "message"],
                "additionalProperties": False,
            },
        },
        "budget": {
            "type": "object",
            "properties": {
                "requested_limit": {"type": "integer"},
                "result_message_count": {"type": "integer"},
                "dialog_count": {"type": "integer"},
                "hidden_count": {"type": "integer"},
                "hidden_count_by_dialog": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "dialog_id": {"type": "integer"},
                            "hidden_count": {"type": "integer"},
                        },
                        "required": ["dialog_id", "hidden_count"],
                        "additionalProperties": False,
                    },
                },
                "allocation_policy": {"type": "string"},
            },
            "required": [
                "requested_limit",
                "result_message_count",
                "dialog_count",
                "hidden_count",
                "hidden_count_by_dialog",
                "allocation_policy",
            ],
            "additionalProperties": False,
        },
        "dialogs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "category": {"type": ["string", "null"]},
                    "dialog_type": {"type": ["string", "null"]},
                    "unread_count": {"type": "integer"},
                    "unread_mentions_count": {"type": "integer"},
                    "total_in_chat": {"type": "integer"},
                    "is_channel": {"type": "boolean"},
                    "is_bot": {"type": "boolean"},
                    "read_state": {
                        "type": ["object", "null"],
                        "properties": {
                            "dialog_type": {"type": ["string", "null"]},
                            "state": {"type": ["object", "null"]},
                            "header_lines": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["dialog_type", "state", "header_lines"],
                        "additionalProperties": False,
                    },
                    "budget": {
                        "type": "object",
                        "properties": {
                            "shown_count": {"type": "integer"},
                            "total_in_chat": {"type": "integer"},
                            "hidden_count": {"type": "integer"},
                        },
                        "required": ["shown_count", "total_in_chat", "hidden_count"],
                        "additionalProperties": False,
                    },
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "msg_id": {"type": "integer"},
                                "sender": {"type": ["string", "null"]},
                                "sender_id": {"type": ["integer", "null"]},
                                "effective_sender_id": {"type": ["integer", "null"]},
                                "out": {"type": "boolean"},
                                "date": {"type": ["string", "null"]},
                                "text": {"type": "string"},
                                "content": {"type": ["object", "null"]},
                                "media_description": {"type": ["string", "null"]},
                                "media": {"type": ["object", "null"]},
                                "reply_to_msg_id": {"type": ["integer", "null"]},
                                "edit_date": {"type": ["integer", "null"]},
                                "reactions": {"type": ["object", "null"]},
                                "read_markers": {"type": "array", "items": {"type": "object"}},
                                "inline_markers": {"type": "array", "items": {"type": "object"}},
                            },
                            "required": [
                                "msg_id",
                                "sender",
                                "sender_id",
                                "effective_sender_id",
                                "out",
                                "date",
                                "text",
                                "content",
                                "media_description",
                                "media",
                                "reply_to_msg_id",
                                "edit_date",
                                "reactions",
                                "read_markers",
                                "inline_markers",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "dialog_id",
                    "name",
                    "category",
                    "dialog_type",
                    "unread_count",
                    "unread_mentions_count",
                    "total_in_chat",
                    "is_channel",
                    "is_bot",
                    "read_state",
                    "budget",
                    "messages",
                ],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "result_count_semantics": {"type": "string"},
    },
    "required": [
        "scope",
        "limit",
        "group_size_threshold",
        "bootstrap_pending",
        "coverage",
        "warnings",
        "budget",
        "dialogs",
        "count",
        "result_count_semantics",
    ],
    "additionalProperties": False,
}


class GetInbox(ToolArgs):
    """Fetch unread messages from personal chats and small groups, prioritized by tier.

    Reads local sync.db only. Prioritizes mentions, DMs, bots, groups, and
    channels; messages inside each chat are chronological. scope="personal"
    shows DMs and small groups; scope="all" includes counts for large groups
    and channels. Check bootstrap_pending to detect incomplete read-position
    coverage instead of treating an empty result as final.
    """

    scope: t.Literal["personal", "all"] = Field(
        default="personal", description="'personal' (DMs + small groups) or 'all' (everything)"
    )
    limit: int = Field(default=100, ge=50, le=500, description="Total message budget across all chats (50-500)")
    group_size_threshold: int = Field(
        default=100,
        ge=10,
        description=(
            "Group member count above which to hide messages (scope=personal only). "
            "NOTE: currently has no effect — participants_count is not stored locally. "
            "All groups are included regardless of size."
        ),
    )


def _message_date(sent_at: object) -> str | None:
    if sent_at is None:
        return None
    try:
        return str(sent_at)
    except Exception:
        return None


def _message_sender(row: dict) -> str | None:
    first = row.get("sender_first_name")
    last = row.get("sender_last_name")
    name = " ".join(str(part) for part in (first, last) if part)
    return name or None


_READ_MARKER_METADATA = {
    "[I read up to here]": {
        "kind": "i_read_up_to_here",
        "side": "inbox",
        "role": "boundary",
    },
    "[unread by me]": {
        "kind": "unread_by_me",
        "side": "inbox",
        "role": "tail_start",
    },
    "[peer read up to here]": {
        "kind": "peer_read_up_to_here",
        "side": "outbox",
        "role": "boundary",
    },
    "[unread by peer]": {
        "kind": "unread_by_peer",
        "side": "outbox",
        "role": "tail_start",
    },
}


def _content_or_none(text: str | None, kind: TelegramContentKind) -> TelegramContent | None:
    if not text:
        return None
    return telegram_content(text, kind)


def _structured_media(description: str | None) -> dict[str, object] | None:
    if not description:
        return None
    return {
        "description": description,
        "content": _content_or_none(description, "media_description"),
    }


def _structured_reactions(display: str | None) -> dict[str, object] | None:
    if not display:
        return None
    return {
        "display": display,
        "content": _content_or_none(display, "reaction"),
    }


def _structured_read_marker(message_id: int, label: str) -> dict[str, object]:
    metadata = _READ_MARKER_METADATA[label]
    return {
        "kind": metadata["kind"],
        "label": label,
        "side": metadata["side"],
        "role": metadata["role"],
        "anchor_message_id": message_id,
    }


def _read_state_payload(read_state: ReadState | dict | None, dialog_type: str | None) -> dict[str, object] | None:
    if read_state is None and dialog_type is None:
        return None
    return {
        "dialog_type": dialog_type,
        "state": read_state,
        "header_lines": _render_read_state_header(read_state, dialog_type, int(time.time())),
    }


def _structured_messages(rows: list[dict], *, read_state: dict | None, dialog_type: str | None) -> list[dict[str, object]]:
    if not rows:
        return []
    ordered_rows = sorted(
        rows,
        key=lambda row: (
            int(row.get("sent_at") or 0),
            int(row.get("message_id") or 0),
        ),
    )
    messages = [ReadMessage(**row) for row in ordered_rows]
    marker_by_message = _compute_inline_markers(messages, read_state) if DialogType.parse(dialog_type) == DialogType.USER else {}
    structured: list[dict[str, object]] = []
    for row, message in zip(ordered_rows, messages, strict=False):
        marker_label = marker_by_message.get(message.id)
        read_markers = [_structured_read_marker(message.id, marker_label)] if marker_label else []
        text = message.text or ""
        structured.append(
            {
                "msg_id": message.id,
                "sender": _message_sender(row) or resolve_sender_label(row),
                "sender_id": message.sender_id,
                "effective_sender_id": message.effective_sender_id,
                "out": bool(message.out),
                "date": _message_date(message.sent_at),
                "text": text,
                "content": _content_or_none(text, "message_text"),
                "media_description": message.media_description,
                "media": _structured_media(message.media_description),
                "reply_to_msg_id": message.reply_to_msg_id,
                "edit_date": message.edit_date,
                "reactions": _structured_reactions(message.reactions_display),
                "read_markers": read_markers,
                "inline_markers": read_markers,
            }
        )
    return structured


@mcp_tool(
    name="get_inbox",
    title="Inbox",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    output_schema=GET_INBOX_OUTPUT_SCHEMA,
)
async def get_inbox(args: GetInbox) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_inbox(
                scope=args.scope,
                limit=args.limit,
                group_size_threshold=args.group_size_threshold,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if err := _check_daemon_response(response):
        return err

    data = response.get("data", {})
    groups = data.get("groups", [])
    # Defensive: older daemon responses or test mocks may omit bootstrap_pending.
    # Treat missing as 0 (full coverage assumed). Also guard against explicit None.
    bootstrap_pending = int(data.get("bootstrap_pending", 0) or 0)
    warning_message = (
        f"bootstrap_pending={bootstrap_pending} dialog(s) are still being seeded by the sync daemon. "
        "Results may be incomplete until bootstrap completes."
    )
    warnings = (
        [
            structured_warning(
                "bootstrap_pending",
                warning_message,
                severity="warning",
                action="Retry shortly once the sync daemon finishes read-state bootstrap.",
            )
        ]
        if bootstrap_pending > 0
        else []
    )
    structured_dialogs: list[dict[str, object]] = []
    hidden_count_by_dialog: list[dict[str, int]] = []
    result_message_count = 0
    for group in groups:
        message_rows = group.get("messages", [])
        category = group.get("category")
        dialog_type = group.get("dialog_type")
        read_state = group.get("read_state")
        total_in_chat = int(group.get("total_in_chat", group.get("unread_count", 0)) or 0)
        hidden_count = max(0, total_in_chat - len(message_rows))
        result_message_count += len(message_rows)
        if hidden_count:
            hidden_count_by_dialog.append({"dialog_id": int(group.get("dialog_id", 0) or 0), "hidden_count": hidden_count})
        read_state_payload = read_state if isinstance(read_state, dict) else None
        messages = _structured_messages(
            message_rows,
            read_state=read_state_payload,
            dialog_type=dialog_type,
        )
        structured_dialogs.append(
            {
                "dialog_id": group.get("dialog_id", 0),
                "name": group.get("display_name", ""),
                "category": category,
                "dialog_type": dialog_type,
                "unread_count": group.get("unread_count", 0),
                "unread_mentions_count": group.get("unread_mentions_count", 0),
                "total_in_chat": total_in_chat,
                "is_channel": DialogType.parse(category) == DialogType.CHANNEL,
                "is_bot": DialogType.parse(category) == DialogType.BOT,
                "read_state": _read_state_payload(read_state_payload, dialog_type),
                "budget": {
                    "shown_count": len(message_rows),
                    "total_in_chat": total_in_chat,
                    "hidden_count": hidden_count,
                },
                "messages": messages,
            }
        )
    structured_content = {
        "scope": args.scope,
        "limit": args.limit,
        "group_size_threshold": args.group_size_threshold,
        "bootstrap_pending": bootstrap_pending,
        "coverage": {
            "complete": bootstrap_pending == 0,
            "state": "complete" if bootstrap_pending == 0 else "partial",
            "bootstrap_pending_count": bootstrap_pending,
        },
        "warnings": warnings,
        "budget": {
            "requested_limit": args.limit,
            "result_message_count": result_message_count,
            "dialog_count": len(structured_dialogs),
            "hidden_count": sum(item["hidden_count"] for item in hidden_count_by_dialog),
            "hidden_count_by_dialog": hidden_count_by_dialog,
            "allocation_policy": "daemon allocates the requested unread message budget across dialogs",
        },
        "dialogs": structured_dialogs,
        "count": len(structured_dialogs),
        "result_count_semantics": "count is the number of unread dialogs returned; budget.result_message_count is the number of message rows shown",
    }

    if not groups:
        return structured_result(structured_content, result_count=0)

    return structured_result(structured_content, result_count=result_message_count)
