from datetime import UTC, datetime

from pydantic import Field, model_validator

from ..errors import dialog_not_found_text, invalid_navigation_text
from ..formatter import (
    _compute_inline_markers,
    _render_read_state_header,
    format_messages,
    frame_telegram_snippet,
    resolve_sender_label,
)
from ..models import DialogType, ReadMessage
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
from .structured import StructuredWarning, TelegramContent, TelegramContentKind, structured_warning, telegram_content

# ---------------------------------------------------------------------------
# Shared archived-dialog warning formatter
# ---------------------------------------------------------------------------


def _format_archived_warning(data: dict) -> str:
    """Build the ⚠ archived-dialog warning string from a daemon response dict.

    Returns an empty string when dialog_access != "archived".
    Uses last_synced_at (preferred) or last_event_at for the archive date —
    never access_lost_at, which records when access was lost, not when sync ran.
    """
    if data.get("dialog_access") != "archived":
        return ""
    last_synced_at = data.get("last_synced_at")
    last_event_at = data.get("last_event_at")
    sync_ts = last_synced_at or last_event_at
    date_str = datetime.fromtimestamp(sync_ts, tz=UTC).strftime("%Y-%m-%d") if sync_ts else "unknown date"
    warning = f"\u26a0 No current access to this dialog. Messages are from the local archive (last sync: {date_str}).\n"
    sync_coverage_pct = data.get("sync_coverage_pct")
    archived_message_count = data.get("archived_message_count")
    if sync_coverage_pct is not None and sync_coverage_pct < 100:
        warning += f"Archive coverage: {sync_coverage_pct}% of dialog history.\n"
    elif sync_coverage_pct is None:
        if archived_message_count is not None:
            warning += f"Archive coverage: unknown ({archived_message_count} messages archived locally).\n"
        else:
            warning += "Archive coverage: unknown.\n"
    return warning


# ---------------------------------------------------------------------------
# Text renderers for future non-MCP surfaces
# ---------------------------------------------------------------------------


def _format_daemon_messages(
    rows: list[dict],
    *,
    global_mode: bool = False,
    read_state: dict | None = None,
    dialog_type: str | None = None,
) -> str:
    """Format daemon row dicts into human-readable message text."""
    if not rows:
        return ""

    messages = [ReadMessage(**r) for r in rows]
    reply_map: dict[int, ReadMessage] = {m.id: m for m in messages}

    has_topics = any(m.topic_title for m in messages)
    topic_name_getter = (lambda msg: msg.topic_title) if has_topics else None
    line_prefix_getter = (lambda msg: f"[{msg.dialog_name or '?'}]") if global_mode else None

    return format_messages(
        messages,
        reply_map=reply_map,
        topic_name_getter=topic_name_getter,
        line_prefix_getter=line_prefix_getter,
        read_state=read_state,
        dialog_type=dialog_type,
    )


# ---------------------------------------------------------------------------
# Tool: ListMessages structured output
# ---------------------------------------------------------------------------

LIST_MESSAGES_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": ["integer", "null"]},
        "dialog": {
            "type": "object",
            "properties": {
                "id": {"type": ["integer", "null"]},
                "name": {"type": ["string", "null"]},
                "type": {"type": ["string", "null"]},
                "access": {"type": ["string", "null"]},
            },
            "required": ["id", "name", "type", "access"],
            "additionalProperties": False,
        },
        "source": {"type": "string"},
        "coverage": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "state": {"type": "string"},
                "fragment_coverage": {"type": "boolean"},
                "dialog_access": {"type": ["string", "null"]},
                "access_lost_at": {"type": ["integer", "null"]},
                "last_synced_at": {"type": ["integer", "null"]},
                "last_event_at": {"type": ["integer", "null"]},
                "sync_coverage_pct": {"type": ["integer", "null"]},
                "archived_message_count": {"type": ["integer", "null"]},
            },
            "required": [
                "kind",
                "state",
                "fragment_coverage",
                "dialog_access",
                "access_lost_at",
                "last_synced_at",
                "last_event_at",
                "sync_coverage_pct",
                "archived_message_count",
            ],
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
        "filters": {
            "type": "object",
            "properties": {
                "dialog": {"type": ["string", "null"]},
                "exact_dialog_id": {"type": ["integer", "null"]},
                "sender": {"type": ["string", "null"]},
                "sender_id": {"type": ["integer", "null"]},
                "sender_name": {"type": ["string", "null"]},
                "topic": {"type": ["string", "null"]},
                "exact_topic_id": {"type": ["integer", "null"]},
                "applied_topic_id": {"type": ["integer", "null"]},
                "unread": {"type": "boolean"},
                "anchor_message_id": {"type": ["integer", "null"]},
            },
            "required": [
                "dialog",
                "exact_dialog_id",
                "sender",
                "sender_id",
                "sender_name",
                "topic",
                "exact_topic_id",
                "applied_topic_id",
                "unread",
                "anchor_message_id",
            ],
            "additionalProperties": False,
        },
        "limits": {
            "type": "object",
            "properties": {
                "requested_limit": {"type": "integer"},
                "applied_limit": {"type": "integer"},
                "requested_context_size": {"type": "integer"},
                "applied_context_size": {"type": ["integer", "null"]},
            },
            "required": ["requested_limit", "applied_limit", "requested_context_size", "applied_context_size"],
            "additionalProperties": False,
        },
        "navigation": {
            "type": "object",
            "properties": {
                "next_navigation": {"type": ["string", "null"]},
                "has_more": {"type": "boolean"},
                "source_cursor": {"type": ["string", "null"]},
                "direction": {"type": "string"},
                "anchor_message_id": {"type": ["integer", "null"]},
            },
            "required": [
                "next_navigation",
                "has_more",
                "source_cursor",
                "direction",
                "anchor_message_id",
            ],
            "additionalProperties": False,
        },
        "presentation": {
            "type": "object",
            "properties": {
                "messages_order": {"type": "string"},
                "is_chronological": {"type": "boolean"},
                "reply_context_policy": {"type": "string"},
            },
            "required": [
                "messages_order",
                "is_chronological",
                "reply_context_policy",
            ],
            "additionalProperties": False,
        },
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
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": "integer"},
                    "msg_id": {"type": "integer"},
                    "sent_at": {"type": "integer"},
                    "date": {"type": "string"},
                    "sender": {"type": "string"},
                    "sender_id": {"type": ["integer", "null"]},
                    "effective_sender_id": {"type": ["integer", "null"]},
                    "out": {"type": "boolean"},
                    "is_service": {"type": "boolean"},
                    "topic_id": {"type": ["integer", "null"]},
                    "topic_title": {"type": ["string", "null"]},
                    "text": {"type": ["string", "null"]},
                    "content": {"type": ["object", "null"]},
                    "media_description": {"type": ["string", "null"]},
                    "media": {"type": ["object", "null"]},
                    "reply_to_msg_id": {"type": ["integer", "null"]},
                    "reply_context_ref": {"type": ["object", "null"]},
                    "reply_context": {"type": ["object", "null"]},
                    "forward": {"type": ["object", "null"]},
                    "post_author": {"type": ["string", "null"]},
                    "edit_date": {"type": ["integer", "null"]},
                    "reactions": {"type": ["object", "null"]},
                    "read_markers": {"type": "array", "items": {"type": "object"}},
                    "inline_markers": {"type": "array", "items": {"type": "object"}},
                },
                "required": [
                    "dialog_id",
                    "msg_id",
                    "sent_at",
                    "date",
                    "sender",
                    "sender_id",
                    "effective_sender_id",
                    "out",
                    "is_service",
                    "topic_id",
                    "topic_title",
                    "text",
                    "content",
                    "media_description",
                    "media",
                    "reply_to_msg_id",
                    "reply_context_ref",
                    "reply_context",
                    "forward",
                    "post_author",
                    "edit_date",
                    "reactions",
                    "read_markers",
                    "inline_markers",
                ],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "result_count_semantics": {"type": "string"},
    },
    "required": [
        "dialog_id",
        "dialog",
        "source",
        "coverage",
        "warnings",
        "filters",
        "limits",
        "navigation",
        "presentation",
        "read_state",
        "messages",
        "count",
        "result_count_semantics",
    ],
    "additionalProperties": False,
}


def _list_messages_resolved_dialog_id(data: dict, rows: list[dict], fallback_dialog_id: int | None) -> int | None:
    if fallback_dialog_id is not None:
        return fallback_dialog_id
    data_dialog_id = data.get("dialog_id")
    if isinstance(data_dialog_id, int):
        return data_dialog_id
    if rows:
        row_dialog_id = rows[0].get("dialog_id")
        if isinstance(row_dialog_id, int):
            return row_dialog_id
    return None


def _list_messages_dialog_name(data: dict, rows: list[dict], fallback_dialog: str | None) -> str | None:
    data_dialog_name = data.get("dialog_name")
    if isinstance(data_dialog_name, str):
        return data_dialog_name
    if rows:
        row_dialog_name = rows[0].get("dialog_name")
        if isinstance(row_dialog_name, str):
            return row_dialog_name
    return fallback_dialog


def _list_messages_coverage(data: dict) -> dict[str, object]:
    raw_coverage = data.get("coverage")
    dialog_access = data.get("dialog_access")
    if raw_coverage == "fragment":
        kind = "fragment"
    elif dialog_access == "archived":
        kind = "archived"
    elif dialog_access == "live":
        kind = "live"
    else:
        kind = str(data.get("source") or "unknown")
    return {
        "kind": kind,
        "state": kind,
        "fragment_coverage": raw_coverage == "fragment",
        "dialog_access": dialog_access,
        "access_lost_at": data.get("access_lost_at"),
        "last_synced_at": data.get("last_synced_at"),
        "last_event_at": data.get("last_event_at"),
        "sync_coverage_pct": data.get("sync_coverage_pct"),
        "archived_message_count": data.get("archived_message_count"),
    }


def _list_messages_warnings(data: dict) -> list[StructuredWarning]:
    archived_warning = _format_archived_warning(data).strip()
    if not archived_warning:
        return []
    return [
        structured_warning(
            "archived_dialog",
            archived_warning,
            severity="warning",
            action="Treat results as local archive content; sync cannot fetch current messages until access is restored.",
        )
    ]


def _navigation_direction_for_structured(direction: str, anchor_message_id: int | None) -> str:
    if anchor_message_id is not None:
        return "around"
    if direction == "oldest":
        return "newer"
    return "older"


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


def _topic_candidate_payload(topic: dict) -> dict[str, object]:
    title = topic.get("title") or ""
    return {
        "topic_id": topic.get("id", topic.get("topic_id")),
        "title_content": telegram_content(str(title), "message_text"),
        "untrusted_content": True,
        "trust": {
            "source": "telegram",
            "is_untrusted": True,
        },
    }


def _message_date(sent_at: int) -> str:
    return datetime.fromtimestamp(int(sent_at), tz=UTC).isoformat()


def _structured_read_marker(message_id: int, label: str) -> dict[str, object]:
    metadata = _READ_MARKER_METADATA[label]
    return {
        "kind": metadata["kind"],
        "label": label,
        "side": metadata["side"],
        "role": metadata["role"],
        "anchor_message_id": message_id,
    }


def _structured_reply_context_ref(
    reply_to_msg_id: int | None,
    *,
    parent_in_page: bool,
    context_included: bool,
) -> dict[str, object] | None:
    if reply_to_msg_id is None:
        return None
    return {
        "msg_id": reply_to_msg_id,
        "in_page": parent_in_page,
        "context_included": context_included,
    }


def _structured_forward(from_name: str | None) -> dict[str, object] | None:
    if not from_name:
        return None
    return {
        "from_name": from_name,
        "content": _content_or_none(from_name, "forward_snippet"),
    }


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


def _list_messages_structured_messages(
    rows: list[dict],
    *,
    read_state: dict | None = None,
    dialog_type: str | None = None,
) -> list[dict[str, object]]:
    if not rows:
        return []
    messages = [ReadMessage(**row) for row in rows]
    reply_map: dict[int, ReadMessage] = {message.id: message for message in messages}
    marker_by_message = (
        _compute_inline_markers(messages, read_state) if DialogType.parse(dialog_type) == DialogType.USER else {}
    )

    structured: list[dict[str, object]] = []
    for message in messages:
        marker_label = marker_by_message.get(message.id)
        read_markers = [_structured_read_marker(message.id, marker_label)] if marker_label else []
        reply_parent = reply_map.get(message.reply_to_msg_id or -1)
        parent_in_page = reply_parent is not None
        structured.append(
            {
                "dialog_id": message.dialog_id,
                "msg_id": message.id,
                "sent_at": message.sent_at,
                "date": _message_date(message.sent_at),
                "sender": resolve_sender_label(message),
                "sender_id": message.sender_id,
                "effective_sender_id": message.effective_sender_id,
                "out": bool(message.out),
                "is_service": bool(message.is_service),
                "topic_id": message.forum_topic_id,
                "topic_title": message.topic_title,
                "text": message.text,
                "content": _content_or_none(message.text, "message_text"),
                "media_description": message.media_description,
                "media": _structured_media(message.media_description),
                "reply_to_msg_id": message.reply_to_msg_id,
                "reply_context_ref": _structured_reply_context_ref(
                    message.reply_to_msg_id,
                    parent_in_page=parent_in_page,
                    context_included=False,
                ),
                "reply_context": None,
                "forward": _structured_forward(message.fwd_from_name),
                "post_author": message.post_author,
                "edit_date": message.edit_date,
                "reactions": _structured_reactions(message.reactions_display),
                "read_markers": read_markers,
                "inline_markers": read_markers,
            }
        )
    return structured


def _chronological_message_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("sent_at") or 0),
            int(row.get("message_id") or 0),
        ),
    )


def _list_messages_structured_content(
    *,
    args: ListMessages,
    data: dict,
    rows: list[dict],
    dialog_id: int | None,
    sender_id: int | None,
    sender_name: str | None,
    topic_id: int | None,
    direction: str,
    next_navigation: str | None,
) -> dict[str, object]:
    resolved_dialog_id = _list_messages_resolved_dialog_id(data, rows, dialog_id)
    dialog_type = data.get("dialog_type")
    read_state = data.get("read_state")
    header_lines = _render_read_state_header(
        read_state,
        dialog_type,
        int(datetime.now(tz=UTC).timestamp()),
    )
    structured_read_state: dict[str, object] | None = None
    if read_state is not None or dialog_type is not None or header_lines:
        structured_read_state = {
            "dialog_type": dialog_type,
            "state": read_state,
            "header_lines": header_lines,
        }
    ordered_rows = _chronological_message_rows(rows)
    return {
        "dialog_id": resolved_dialog_id,
        "dialog": {
            "id": resolved_dialog_id,
            "name": _list_messages_dialog_name(data, rows, args.dialog),
            "type": dialog_type,
            "access": data.get("dialog_access"),
        },
        "source": data.get("source", "unknown"),
        "coverage": _list_messages_coverage(data),
        "warnings": _list_messages_warnings(data),
        "filters": {
            "dialog": args.dialog,
            "exact_dialog_id": args.exact_dialog_id,
            "sender": args.sender,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "topic": args.topic,
            "exact_topic_id": args.exact_topic_id,
            "applied_topic_id": topic_id,
            "unread": args.unread,
            "anchor_message_id": args.anchor_message_id,
        },
        "limits": {
            "requested_limit": args.limit,
            "applied_limit": len(rows),
            "requested_context_size": args.context_size,
            "applied_context_size": args.context_size if args.anchor_message_id is not None else None,
        },
        "navigation": {
            "next_navigation": next_navigation,
            "has_more": next_navigation is not None,
            "source_cursor": args.navigation,
            "direction": _navigation_direction_for_structured(direction, args.anchor_message_id),
            "anchor_message_id": args.anchor_message_id,
        },
        "presentation": {
            "messages_order": "chronological",
            "is_chronological": True,
            "reply_context_policy": (
                "reply_context is omitted to keep messages[] as a clean timeline; "
                "use reply_context_ref.msg_id to link replies to parent rows"
            ),
        },
        "read_state": structured_read_state,
        "messages": _list_messages_structured_messages(
            ordered_rows,
            read_state=read_state,
            dialog_type=dialog_type,
        ),
        "count": len(rows),
        "result_count_semantics": "count is the number of message rows returned in this response page",
    }


# ---------------------------------------------------------------------------
# Search result formatting — snippets + anchors
# ---------------------------------------------------------------------------

_SNIPPET_MAX_LEN = 150
_SNIPPET_LEAD = 50  # chars to show before the matched word

SEARCH_MESSAGES_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "dialog_name": {"type": ["string", "null"]},
        "scope": {
            "type": "object",
            "properties": {
                "dialog": {"type": ["string", "null"]},
                "dialog_id": {"type": ["integer", "null"]},
                "global": {"type": "boolean"},
            },
            "required": ["dialog", "dialog_id", "global"],
            "additionalProperties": False,
        },
        "source": {"type": "string"},
        "coverage": {"type": "object"},
        "warnings": {"type": "array", "items": {"type": "object"}},
        "read_state_per_dialog": {"type": "object"},
        "navigation": {
            "type": "object",
            "properties": {
                "next_navigation": {"type": ["string", "null"]},
                "has_more": {"type": "boolean"},
                "source_cursor": {"type": ["string", "null"]},
                "offset": {"type": "integer"},
            },
            "required": ["next_navigation", "has_more", "source_cursor", "offset"],
            "additionalProperties": False,
        },
        "limits": {
            "type": "object",
            "properties": {
                "requested_limit": {"type": "integer"},
                "applied_limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["requested_limit", "applied_limit", "offset"],
            "additionalProperties": False,
        },
        "anchor_call": {
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "arguments_template": {"type": "object"},
            },
            "required": ["tool", "arguments_template"],
            "additionalProperties": False,
        },
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": "integer"},
                    "dialog_name": {"type": ["string", "null"]},
                    "msg_id": {"type": "integer"},
                    "date": {"type": ["string", "null"]},
                    "sender": {"type": ["string", "null"]},
                    "snippet": {"type": "string"},
                    "content": {"type": "object"},
                    "anchor_call": {"type": "object"},
                },
                "required": ["dialog_id", "dialog_name", "msg_id", "snippet", "content", "anchor_call"],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "next_navigation": {"type": ["string", "null"]},
        "result_count_semantics": {"type": "string"},
    },
    "required": [
        "query",
        "dialog_name",
        "scope",
        "source",
        "coverage",
        "warnings",
        "read_state_per_dialog",
        "navigation",
        "limits",
        "anchor_call",
        "results",
        "count",
        "next_navigation",
        "result_count_semantics",
    ],
    "additionalProperties": False,
}


def _extract_snippet(text: str | None, query: str) -> str:
    """Return a short excerpt from *text* centred on the first query word match.

    Falls back to a simple head-truncation when no query word appears in the
    original text (e.g. stemming produced a morphologically distant match).
    """
    if not text:
        return "(no text)"
    if len(text) <= _SNIPPET_MAX_LEN:
        return text

    for word in query.split():
        pos = text.lower().find(word.lower())
        if pos >= 0:
            start = max(0, pos - _SNIPPET_LEAD)
            end = min(len(text), start + _SNIPPET_MAX_LEN)
            snippet = text[start:end]
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(text) else ""
            return f"{prefix}{snippet}{suffix}"

    return text[:_SNIPPET_MAX_LEN] + "..."


def _search_anchor_call(dialog_id: int, msg_id: int) -> dict[str, object]:
    return {
        "tool": "list_messages",
        "arguments": {
            "exact_dialog_id": dialog_id,
            "anchor_message_id": msg_id,
        },
    }


def _search_result_structured_rows(rows: list[dict], query: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for row in rows:
        sent_at = row.get("sent_at")
        date: str | None = None
        if sent_at is not None:
            date = datetime.fromtimestamp(int(sent_at), tz=UTC).strftime("%Y-%m-%d %H:%M")
        snippet = _extract_snippet(row.get("text"), query)
        dialog_id = int(row.get("dialog_id") or 0)
        results.append(
            {
                "dialog_id": dialog_id,
                "dialog_name": row.get("dialog_name"),
                "msg_id": row["message_id"],
                "date": date,
                "sender": resolve_sender_label(row),
                "snippet": snippet,
                "content": telegram_content(snippet, "snippet"),
                "anchor_call": _search_anchor_call(dialog_id, row["message_id"]),
            }
        )
    return results


def _search_read_state_per_dialog(data: dict) -> dict[str, object]:
    read_state_per_dialog = data.get("read_state_per_dialog") or {}
    structured: dict[str, object] = {}
    for raw_dialog_id, read_state in read_state_per_dialog.items():
        try:
            dialog_id = int(raw_dialog_id)
        except TypeError, ValueError:
            continue
        structured[str(dialog_id)] = {
            "dialog_id": dialog_id,
            "dialog_type": DialogType.USER.value,
            "state": read_state,
            "header_lines": _render_read_state_header(
                read_state,
                DialogType.USER.value,
                int(datetime.now(tz=UTC).timestamp()),
            ),
        }
    return structured


def _search_dialog_name(rows: list[dict], global_mode: bool, dialog_label: str | None) -> str | None:
    if global_mode:
        return None
    for row in rows:
        dialog_name = row.get("dialog_name")
        if isinstance(dialog_name, str):
            return dialog_name
    return dialog_label


def _search_structured_content(
    *,
    args: SearchMessages,
    data: dict,
    rows: list[dict],
    dialog_id: int | None,
    dialog_label: str | None,
    global_mode: bool,
    offset: int,
    next_navigation: str | None,
) -> dict[str, object]:
    structured_results = _search_result_structured_rows(rows, args.query)
    source = data.get("source", "sync_db")
    data_with_source = {**data, "source": source}
    return {
        "query": args.query,
        "dialog_name": _search_dialog_name(rows, global_mode, dialog_label),
        "scope": {
            "dialog": args.dialog,
            "dialog_id": dialog_id,
            "global": global_mode,
        },
        "source": source,
        "coverage": _list_messages_coverage(data_with_source),
        "warnings": _list_messages_warnings(data),
        "read_state_per_dialog": _search_read_state_per_dialog(data),
        "navigation": {
            "next_navigation": next_navigation,
            "has_more": next_navigation is not None,
            "source_cursor": args.navigation,
            "offset": offset,
        },
        "limits": {
            "requested_limit": args.limit,
            "applied_limit": len(rows),
            "offset": offset,
        },
        "anchor_call": {
            "tool": "list_messages",
            "arguments_template": {
                "exact_dialog_id": "<result.dialog_id>",
                "anchor_message_id": "<result.msg_id>",
            },
        },
        "results": structured_results,
        "count": len(structured_results),
        "next_navigation": next_navigation,
        "result_count_semantics": "count is the number of search hits returned in this response page",
    }


def _format_search_results(
    rows: list[dict],
    query: str,
    *,
    global_mode: bool = False,
    read_state_per_dialog: dict[int, dict] | None = None,
) -> str:
    """Format search result rows as compact snippet lines with msg_id anchors."""
    if not rows:
        return ""

    header_lines: list[str] = []
    if read_state_per_dialog:
        seen: dict[int, str | None] = {}
        for row in rows:
            did = row.get("dialog_id")
            if did is None or did in seen:
                continue
            seen[did] = row.get("dialog_name")
        now_unix = int(datetime.now(tz=UTC).timestamp())
        for did, dname in seen.items():
            rs = read_state_per_dialog.get(did)
            if rs is None:
                continue
            rs_lines = _render_read_state_header(rs, DialogType.USER.value, now_unix)
            if not rs_lines:
                continue
            label = dname or str(did)
            header_lines.append(f"# {label}:")
            header_lines.extend(rs_lines)

    lines: list[str] = []
    for row in rows:
        msg_id = row["message_id"]
        sent_at = row.get("sent_at") or 0
        sender = resolve_sender_label(row)
        dt = datetime.fromtimestamp(int(sent_at), tz=UTC)
        time_str = dt.strftime("%Y-%m-%d %H:%M")
        snippet = frame_telegram_snippet(_extract_snippet(row.get("text"), query))

        dialog_prefix = f"[{row.get('dialog_name') or '?'}] " if global_mode else ""
        lines.append(f"{dialog_prefix}{time_str} {sender} (msg_id:{msg_id}): {snippet}")

    if header_lines:
        return "\n".join(header_lines + lines)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: ListMessages
# ---------------------------------------------------------------------------


class ListMessages(ToolArgs):
    """
    List messages in one dialog.

    Provide dialog= for fuzzy/name/link resolution or exact_dialog_id= when known.
    This is not a global latest-across-all-dialogs tool.

    Omit navigation or use navigation="latest" for the recent tail; use
    navigation="start" for the beginning; pass next_navigation to continue.
    Every page is chronological (oldest-to-newest). Use anchor_message_id from
    search_messages to read context around a hit; that path requires a synced
    dialog and exact_dialog_id.

    Supports sender, topic/exact_topic_id, and unread filters. DM rows include
    read_state plus inline read markers. Fragment coverage means a targeted
    snippet, not full chat history.
    """

    dialog: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional natural dialog selector: numeric id, @username, or fuzzy dialog name. "
            "Use this for exploratory or ambiguity-safe reads. Mutually exclusive with exact_dialog_id."
        ),
    )
    exact_dialog_id: int | None = Field(
        default=None,
        description=(
            "Optional exact dialog id for direct reads when the target dialog is already known. "
            "Bypasses fuzzy dialog resolution. Mutually exclusive with dialog."
        ),
    )
    limit: int = Field(default=50, ge=1, le=500)
    navigation: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            'Optional shared navigation state. Omit or set to "latest" to start from the latest '
            'message page. Set to "start" to start from the beginning. Reuse the exact '
            "next_navigation token from the previous ListMessages response to continue."
        ),
    )
    sender: str | None = Field(
        default=None,
        max_length=500,
        description="Filter by sender name (fuzzy match, case-insensitive).",
    )
    topic: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional natural topic title resolved within the selected dialog. Mutually exclusive with exact_topic_id."
        ),
    )
    exact_topic_id: int | None = Field(
        default=None,
        description=(
            "Optional exact forum topic id for direct reads when the topic is already known. "
            "Reuses cached or by-id topic metadata instead of loading the full topic catalog by "
            "default. Mutually exclusive with topic."
        ),
    )
    unread: bool = False
    anchor_message_id: int | None = Field(
        default=None,
        description=(
            "Optional message id to centre the response on. Returns context_size messages "
            "around this message (half before, half after). Requires the dialog to be synced. "
            "When set, navigation and direction are ignored. "
            "Obtain from msg_id: values in SearchMessages results."
        ),
    )
    context_size: int = Field(
        default=10,
        ge=2,
        le=50,
        description="Number of messages to return around anchor_message_id (default 10).",
    )

    @model_validator(mode="after")
    def validate_direct_read_selectors(self) -> ListMessages:
        """Reject missing or conflicting selector combinations."""
        if self.dialog is None and self.exact_dialog_id is None:
            raise ValueError("Provide either dialog or exact_dialog_id.")
        if self.dialog is not None and self.exact_dialog_id is not None:
            raise ValueError("dialog and exact_dialog_id are mutually exclusive.")
        if self.topic is not None and self.exact_topic_id is not None:
            raise ValueError("topic and exact_topic_id are mutually exclusive.")
        return self


async def _resolve_topic_id(
    topic_name: str,
    *,
    dialog_id: int,
    dialog_name: str | None,
) -> int | ToolResult:
    """Resolve a fuzzy topic name to a numeric topic_id via the daemon.

    Returns the resolved int topic_id on success, or a ToolResult with an
    error message on failure (daemon error, not found, ambiguous match).
    """
    try:
        async with daemon_connection() as conn:
            response = await conn.list_topics(
                dialog_id=dialog_id,
                dialog=dialog_name,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if not response.get("ok"):
        error = response.get("error", "unknown")
        error_detail = response.get("message", "Request failed.")
        return error_result(
            f"Topic lookup failed: {error}: {error_detail}\n"
            "Action: Call list_topics for this dialog, then retry list_messages with a numeric exact_topic_id.",
        )

    topics = response.get("data", {}).get("topics", [])
    query = topic_name.lower()
    fuzzy_matches = [t for t in topics if query in (t.get("title") or "").lower()]

    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0]["id"]

    if len(fuzzy_matches) > 1:
        exact_matches = [t for t in fuzzy_matches if (t.get("title") or "").lower() == query]
        if len(exact_matches) == 1:
            return exact_matches[0]["id"]
        return error_result(
            "Multiple topics matched.\n"
            "Action: Retry list_messages with one numeric exact_topic_id from structuredContent.candidates.",
            structured_content={
                "error": "ambiguous_topic",
                "candidates": [_topic_candidate_payload(topic) for topic in fuzzy_matches[:5]],
            },
        )

    return error_result(
        "Topic was not found in this dialog.\n"
        "Action: Call list_topics for this dialog, then retry list_messages with a numeric exact_topic_id.",
    )


@mcp_tool(
    name="list_messages",
    title="List Messages",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    output_schema=LIST_MESSAGES_OUTPUT_SCHEMA,
)
async def list_messages(args: ListMessages) -> ToolResult:
    has_filter = bool(args.sender or args.topic or args.exact_topic_id is not None or args.unread)
    navigation_sentinels = {"latest", "start"}
    has_cursor = args.navigation is not None and args.navigation not in navigation_sentinels
    if args.navigation in {"newest", "oldest"}:
        return error_result(
            'Unsupported navigation selector. Use navigation="latest", navigation="start", '
            "or an opaque next_navigation token returned by list_messages.\n"
            "Action: Retry list_messages with latest/start page navigation.",
            has_filter=has_filter,
            has_cursor=False,
        )

    # Resolve dialog_id locally if possible (numeric string / @username / entity cache)
    dialog_id: int | None = args.exact_dialog_id
    if dialog_id is None and args.dialog is not None:
        exact_id = parse_exact_dialog_id(args.dialog)
        if exact_id is not None:
            dialog_id = exact_id
        # If still None, dialog name goes to daemon for server-side resolution

    # Derive page-selection direction from navigation sentinel; response order is always chronological.
    direction: str
    if args.navigation in {"start", "oldest"}:
        direction = "oldest"
    else:
        direction = "newest"
    # If navigation is an opaque token, direction is encoded in it (daemon handles it)
    daemon_navigation = None if args.navigation in navigation_sentinels else args.navigation

    # Sender passthrough: numeric string → sender_id (works on live Telegram path via from_user=)
    sender_id: int | None = None
    sender_name: str | None = None
    if args.sender is not None:
        try:
            sender_id = int(args.sender)
        except ValueError:
            sender_name = args.sender

    # Unread flag
    unread_flag: bool | None = True if args.unread else None

    # Topic resolution: exact_topic_id takes priority over fuzzy topic name.
    # Uses a separate daemon connection because the daemon handles one request
    # per connection — topic resolution must complete before list_messages.
    topic_id: int | None = args.exact_topic_id
    if topic_id is None and args.topic is not None:
        resolved = await _resolve_topic_id(
            args.topic,
            dialog_id=dialog_id or 0,
            dialog_name=args.dialog if not dialog_id else None,
        )
        if isinstance(resolved, ToolResult):
            return ToolResult(
                content=resolved.content,
                is_error=resolved.is_error,
                structured_content=resolved.structured_content,
                has_filter=has_filter,
                has_cursor=has_cursor,
            )
        topic_id = resolved

    id_kwarg: dict = {"dialog_id": dialog_id} if dialog_id else {"dialog": args.dialog}
    try:
        async with daemon_connection() as conn:
            response = await conn.list_messages(
                **id_kwarg,
                limit=args.limit,
                navigation=daemon_navigation,
                direction=direction,
                sender_id=sender_id,
                sender_name=sender_name,
                topic_id=topic_id,
                unread=unread_flag,
                context_message_id=args.anchor_message_id,
                context_size=args.context_size if args.anchor_message_id else None,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if not response.get("ok"):
        error = response.get("error", "unknown")
        error_detail = response.get("message", "")
        if error == "dialog_not_found":
            dialog_label = str(dialog_id) if dialog_id else (args.dialog or "")
            return error_result(
                dialog_not_found_text(dialog_label, retry_tool="ListMessages"),
                has_filter=has_filter,
                has_cursor=has_cursor,
            )
        if error == "not_synced":
            return error_result(
                "Error: dialog is not synced. "
                "Action: Use MarkDialogForSync to enable sync, then wait for syncing to complete.",
                has_filter=has_filter,
                has_cursor=has_cursor,
            )
        return error_result(
            f"Error: {error}: {error_detail}\n"
            "Action: Retry list_messages with corrected arguments, or call list_dialogs/list_topics first to discover valid ids.",
            has_filter=has_filter,
            has_cursor=has_cursor,
        )

    data = response.get("data", {})
    rows = data.get("messages", [])
    next_nav = data.get("next_navigation")

    structured_content = _list_messages_structured_content(
        args=args,
        data=data,
        rows=rows,
        dialog_id=dialog_id,
        sender_id=sender_id,
        sender_name=sender_name,
        topic_id=topic_id,
        direction=direction,
        next_navigation=next_nav,
    )
    return structured_result(
        structured_content,
        result_count=len(rows),
        has_filter=has_filter,
        has_cursor=has_cursor or bool(next_nav),
    )


# ---------------------------------------------------------------------------
# Tool: SearchMessages
# ---------------------------------------------------------------------------


class SearchMessages(ToolArgs):
    """
    Search messages by text query. Returns matching messages ranked by relevance.

    - Without dialog: searches across all synced dialogs. Each result includes the dialog name.
    - With dialog: scoped to that dialog only.

    Each result is a compact one-liner with a msg_id: anchor:
      [Dialog] 2024-01-15 14:32 Ivan (msg_id:42): "...snippet..."

    To read context around a hit, call ListMessages with exact_dialog_id and anchor_message_id=42.

    Omit navigation to start a new search.
    Pass the next_navigation token from a previous response to continue paging.

    For @username lookups, prepend @ to the dialog name: dialog="@channel_name".
    """

    dialog: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional dialog selector. Omit to search all synced dialogs. "
            "Provide an exact numeric dialog id, @username, or fuzzy dialog name to scope "
            "the search to one dialog."
        ),
    )
    query: str = Field(max_length=500)
    limit: int = Field(default=20, ge=1, le=200)
    navigation: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Optional shared navigation state. Omit navigation to start from the first search "
            "page. Reuse the exact next_navigation token from the previous SearchMessages "
            "response to continue."
        ),
    )


@mcp_tool(
    name="search_messages",
    title="Search Messages",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    output_schema=SEARCH_MESSAGES_OUTPUT_SCHEMA,
)
async def search_messages(args: SearchMessages) -> ToolResult:
    global_mode = args.dialog is None

    # Resolve dialog_id locally if possible (numeric string / @username)
    dialog_id: int | None = None
    if args.dialog is not None:
        exact_id = parse_exact_dialog_id(args.dialog)
        if exact_id is not None:
            dialog_id = exact_id
        # If still None, dialog name goes to daemon for server-side resolution

    # Decode offset from navigation token if provided
    offset = 0
    if args.navigation and args.navigation not in {"newest", "oldest"}:
        try:
            from ..pagination import decode_navigation_token

            nav = decode_navigation_token(args.navigation)
            if nav.kind != "search":
                return error_result(
                    invalid_navigation_text(
                        f"Navigation token is for {nav.kind}, not search",
                        retry_tool="SearchMessages",
                    ),
                    has_cursor=True,
                )
            offset = nav.value
        except ValueError as exc:
            return error_result(
                invalid_navigation_text(str(exc), retry_tool="SearchMessages"),
                has_cursor=True,
            )

    try:
        if global_mode:
            id_kwarg: dict = {}
        elif dialog_id:
            id_kwarg = {"dialog_id": dialog_id}
        else:
            id_kwarg = {"dialog": args.dialog}
        async with daemon_connection() as conn:
            response = await conn.search_messages(
                **id_kwarg,
                query=args.query,
                limit=args.limit,
                offset=offset,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    dialog_label: str | None = str(dialog_id) if dialog_id else args.dialog

    if not response.get("ok"):
        error = response.get("error", "unknown")
        error_detail = response.get("message", "")
        if error == "dialog_not_found":
            return error_result(
                dialog_not_found_text(dialog_label or "?", retry_tool="SearchMessages"),
                has_filter=True,
                has_cursor=args.navigation is not None,
            )
        return error_result(
            f"Error: {error}: {error_detail}\n"
            "Action: Retry search_messages with corrected arguments, or call list_dialogs first to discover a valid dialog id.",
            has_filter=True,
            has_cursor=args.navigation is not None,
        )

    data = response.get("data", {})
    rows = data.get("messages", [])
    next_nav = data.get("next_navigation")
    structured_content = _search_structured_content(
        args=args,
        data=data,
        rows=rows,
        dialog_id=dialog_id,
        dialog_label=dialog_label,
        global_mode=global_mode,
        offset=offset,
        next_navigation=next_nav,
    )

    if not rows:
        return structured_result(
            structured_content,
            result_count=0,
            has_filter=True,
            has_cursor=args.navigation is not None,
        )

    return structured_result(
        structured_content,
        result_count=len(rows),
        has_filter=True,
        has_cursor=args.navigation is not None or bool(next_nav),
    )
