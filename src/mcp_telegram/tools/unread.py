import typing as t
import time

from pydantic import Field

from ..errors import no_unread_all_text, no_unread_personal_text
from ..formatter import (
    UnreadChatData,
    _compute_inline_markers,
    _render_read_state_header,
    format_unread_messages_grouped,
    resolve_sender_label,
)
from ..models import ReadMessage, ReadState
from .structured import structured_warning, telegram_content
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    error_result,
    mcp_tool,
)

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

    Priority tiers (lower = higher priority): @mentions in DMs, @mentions in groups,
    human DMs, bot DMs, small groups, large groups, channels.
    Within each tier, chats are sorted by recency (newest first).
    Per-chat message budget is allocated proportionally to prevent flooding.

    Use scope="personal" (default) to see only DMs and small groups (≤ group_size_threshold members).
    Use scope="all" to include large groups and channels (shows counts only, no messages).
    Use limit to control total messages (default 100, minimum across all chats).

    **Data source**: Results come exclusively from the local sync.db (synced_dialogs, messages,
    and entities tables) via a single grouped SQL query — zero Telegram API calls in the hot path.
    Only dialogs with status='synced' AND read_inbox_max_id IS NOT NULL are scanned.

    **Bootstrap**: On first daemon start after a schema upgrade, dialogs are bootstrapped in the
    background by _initialize_read_positions. Until bootstrap completes for a given dialog it is
    excluded from results. The response includes bootstrap_pending (count of synced dialogs with
    NULL read_inbox_max_id) so callers can detect incomplete coverage and retry later — no silent
    empty results.

    **Real-time updates**: read_inbox_max_id is maintained live via events.MessageRead(inbox=True)
    with monotonic writes (MAX(COALESCE(existing,0), incoming) — never regresses).
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


def _content_or_none(text: str | None, kind: str) -> dict[str, object] | None:
    if not text:
        return None
    return telegram_content(text, t.cast(t.Any, kind))


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


def _read_state_payload(read_state: object, dialog_type: str | None) -> dict[str, object] | None:
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
    messages = [ReadMessage(**row) for row in rows]
    marker_by_message = _compute_inline_markers(messages, read_state) if dialog_type == "User" else {}
    structured: list[dict[str, object]] = []
    for row, message in zip(rows, messages, strict=False):
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
    annotations=ToolAnnotations(readOnlyHint=True),
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
        messages = _structured_messages(
            message_rows,
            read_state=read_state if isinstance(read_state, dict) else None,
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
                "is_channel": category == "channel",
                "is_bot": category == "bot",
                "read_state": _read_state_payload(read_state, dialog_type),
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
        # Closes UAT gap 1: when groups=[] but bootstrap_pending>0 the response is
        # NOT 'no unread' — results are incomplete because dialogs are still being
        # bootstrapped. The canned 'no unread' text would mislead the caller.
        if bootstrap_pending > 0:
            warning = (
                f"No unread messages yet — bootstrap_pending={bootstrap_pending} "
                f"dialog(s) are still being seeded by the sync daemon. Results are "
                f"incomplete. Retry shortly once bootstrap completes."
            )
            return ToolResult(content=_text_response(warning), structured_content=structured_content)
        empty_msg = no_unread_all_text() if args.scope == "all" else no_unread_personal_text()
        return ToolResult(content=_text_response(empty_msg), structured_content=structured_content)

    chats: list[UnreadChatData] = []
    result_count = 0
    # Phase 39.3 (HIGH-3): build per-dialog read_state + dialog_type maps from
    # daemon response; threaded into format_unread_messages_grouped so each DM
    # block gets its own header (AC-5/6/7). Absent fields → no header for that
    # block (backward compat with pre-39.3 daemon).
    read_state_per_dialog: dict[int, ReadState | dict] = {}
    dialog_type_per_dialog: dict[int, str] = {}

    for group in groups:
        messages = [ReadMessage(**m) for m in group.get("messages", [])]
        dialog_id = group.get("dialog_id", 0)
        chat_data = UnreadChatData(
            chat_id=dialog_id,
            display_name=group.get("display_name", ""),
            unread_count=group.get("unread_count", 0),
            unread_mentions_count=group.get("unread_mentions_count", 0),
            total_in_chat=group.get("unread_count", 0),
            is_channel=group.get("category") == "channel",
            is_bot=group.get("category") == "bot",
        )
        chat_data.messages = messages
        result_count += len(messages)
        chats.append(chat_data)

        rs = group.get("read_state")
        if rs is not None:
            read_state_per_dialog[dialog_id] = rs
        dt = group.get("dialog_type")
        if dt is not None:
            dialog_type_per_dialog[dialog_id] = dt

    result_text = format_unread_messages_grouped(
        chats,
        read_state_per_dialog=read_state_per_dialog or None,
        dialog_type_per_dialog=dialog_type_per_dialog or None,
    )
    # Closes UAT gap 2: even when results are non-empty, the caller must be told
    # if some dialogs are still bootstrapping — otherwise partial coverage looks
    # like complete coverage.
    if bootstrap_pending > 0:
        result_text = (
            f"{result_text}\n\n"
            f"Note: bootstrap_pending={bootstrap_pending} dialog(s) not yet seeded "
            f"by the sync daemon — results may be incomplete. Retry shortly."
        )
    return ToolResult(content=_text_response(result_text), structured_content=structured_content, result_count=result_count)
