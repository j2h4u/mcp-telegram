from datetime import UTC, datetime

from pydantic import Field, model_validator

from ..errors import dialog_not_found_text, invalid_navigation_text, search_no_hits_text
from ..formatter import (
    _render_read_state_header,
    frame_telegram_snippet,
    format_messages,
    resolve_sender_label,
)
from ..models import ReadMessage
from ..resolver import parse_exact_dialog_id
from .structured import structured_warning
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    error_result,
    mcp_tool,
)

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
# format_messages adapter for daemon data
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
                "direction_input": {"type": "string"},
                "anchor_message_id": {"type": ["integer", "null"]},
            },
            "required": [
                "next_navigation",
                "has_more",
                "source_cursor",
                "direction",
                "direction_input",
                "anchor_message_id",
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
        "read_state",
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


def _list_messages_warnings(data: dict) -> list[dict[str, object]]:
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


def _list_messages_structured_content(
    *,
    args: "ListMessages",
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
            "direction_input": direction,
            "anchor_message_id": args.anchor_message_id,
        },
        "read_state": structured_read_state,
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
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": "integer"},
                    "msg_id": {"type": "integer"},
                    "date": {"type": ["string", "null"]},
                    "sender": {"type": ["string", "null"]},
                    "snippet": {"type": "string"},
                },
                "required": ["dialog_id", "msg_id", "snippet"],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "next_navigation": {"type": ["string", "null"]},
    },
    "required": ["query", "results", "count", "next_navigation"],
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


def _search_result_structured_rows(rows: list[dict], query: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for row in rows:
        sent_at = row.get("sent_at")
        date: str | None = None
        if sent_at is not None:
            date = datetime.fromtimestamp(int(sent_at), tz=UTC).strftime("%Y-%m-%d %H:%M")
        results.append(
            {
                "dialog_id": row.get("dialog_id"),
                "msg_id": row["message_id"],
                "date": date,
                "sender": resolve_sender_label(row),
                "snippet": _extract_snippet(row.get("text"), query),
            }
        )
    return results


def _format_search_results(
    rows: list[dict],
    query: str,
    *,
    global_mode: bool = False,
    read_state_per_dialog: dict[int, dict] | None = None,
) -> str:
    """Format search result rows as compact snippet lines with msg_id anchors.

    Each line:  [DialogName] YYYY-MM-DD HH:MM Sender (msg_id:N): "...snippet..."

    dialog_name prefix is included only when global_mode=True.

    Phase 39.3 (HIGH-1): when ``read_state_per_dialog`` is supplied, the output
    is prefixed with a per-dialog header block — one read-state header (collapsed
    or split per AC-5/6) per DM dialog whose hits appear in results. Non-DM
    dialog_ids are absent from the map per the daemon contract and therefore
    emit no header line.

    Markers are a full-message concept; snippet lines are not full messages.
    Per-dialog header block above covers SPEC R5/AC-5/6 for search — inline
    snippet lines do NOT get the four inline markers (documented trade-off).
    """
    if not rows:
        return ""

    # Build header block first (Phase 39.3 HIGH-1).
    header_lines: list[str] = []
    if read_state_per_dialog:
        # Preserve first-seen order across results (stable for tests).
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
            rs_lines = _render_read_state_header(rs, "User", now_unix)
            if not rs_lines:
                continue
            label = dname or str(did)
            header_lines.append(f"# {label}:")
            header_lines.extend(rs_lines)

    lines: list[str] = []
    for row in rows:
        msg_id = row["message_id"]
        sent_at = row.get("sent_at") or 0
        # Phase 39.1-02: shared 5-branch resolution with formatter.resolve_sender_label.
        # Single source of truth — same decision tree for list_messages and search_messages.
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

    Provide either dialog= for the ambiguity-safe natural-name flow or exact_dialog_id= when the
    target dialog is already known. This tool does not support a global "latest messages across all
    dialogs" mode.

    Returns messages in human-readable format (HH:mm FirstName: text) with date headers and
    session breaks.

    Use navigation="newest" (or omit navigation) to start from the latest messages.
    Use navigation="oldest" to start from the oldest messages in the dialog.
    Use navigation= with the next_navigation token from a previous response to continue paging.
    To read an entire channel or chat history: call this tool repeatedly, passing the next_navigation
    token from each response as navigation= in the next call. Stop when next_navigation is absent.

    dialog= accepts: fuzzy name, @username, numeric id, or https://t.me/username links directly.
    Use sender= to filter messages from a specific person (name string, resolved via fuzzy match).
    Use topic= to filter messages to one forum topic after the dialog has been resolved.
    Use exact_topic_id= when the forum topic is already known and you want the direct-read path
    without defaulting to full topic discovery first.
    In forum dialogs, omitting topic= returns a cross-topic page and each message is labeled inline.
    Use unread=True to show only messages you haven't read yet.
    Default limit=50; set limit explicitly if you want a smaller MCP response.
    Use anchor_message_id= with a msg_id: value from SearchMessages to read context around a hit.
    anchor_message_id requires the dialog to be synced; use exact_dialog_id= on this path.

    If response is ambiguous (multiple matches), retry with one exact selector instead of leaving
    both fuzzy and exact selectors in the same request.

    **Read-state annotations** (DMs only):
    The response begins with a one-line `[read-state: all caught up]` when both sides are clean, OR two lines `[inbox: …]` / `[outbox: …]` (each independently `all read`, `N unread …`, or `unknown (sync pending)`).
    Inline trailing markers fire on at most four message lines per page:
      `[I read up to here]` — last incoming you read.
      `[unread by me]` — first incoming on this page you have not read.
      `[peer read up to here]` — last outgoing the peer read.
      `[unread by peer]` — first outgoing on this page the peer has not read.
    Check the header first for triage, then inspect inline markers if reading the full history.

    When this tool is called with `context_message_id` on a dialog that has not been fully
    synced ("fragment" dialog), the daemon performs a targeted message fetch. In that case,
    the response is prefixed with a `Coverage: fragment` header — treat the returned messages
    as a snippet, NOT the full chat history.
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
            'Optional shared navigation state. Omit or set to "newest" to start from the latest '
            'messages. Set to "oldest" to start from the oldest messages. Reuse the exact '
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
        error_detail = response.get("message", "")
        return error_result(f"Topic lookup failed: {error}: {error_detail}")

    topics = response.get("data", {}).get("topics", [])
    query = topic_name.lower()
    fuzzy_matches = [t for t in topics if query in (t.get("title") or "").lower()]

    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0]["id"]

    if len(fuzzy_matches) > 1:
        exact_matches = [t for t in fuzzy_matches if (t.get("title") or "").lower() == query]
        if len(exact_matches) == 1:
            return exact_matches[0]["id"]
        names = ", ".join(t.get("title", "?") for t in fuzzy_matches[:5])
        return error_result(f"Ambiguous topic '{topic_name}'. Matches: {names}. Use exact_topic_id.")

    return error_result(f"Topic '{topic_name}' not found in this dialog.")


@mcp_tool(
    name="list_messages",
    title="List Messages",
    annotations=ToolAnnotations(readOnlyHint=True),
    output_schema=LIST_MESSAGES_OUTPUT_SCHEMA,
)
async def list_messages(args: ListMessages) -> ToolResult:
    has_filter = bool(args.sender or args.topic or args.exact_topic_id is not None or args.unread)
    has_cursor = args.navigation is not None and args.navigation not in {"newest", "oldest"}

    # Resolve dialog_id locally if possible (numeric string / @username / entity cache)
    dialog_id: int | None = args.exact_dialog_id
    if dialog_id is None and args.dialog is not None:
        exact_id = parse_exact_dialog_id(args.dialog)
        if exact_id is not None:
            dialog_id = exact_id
        # If still None, dialog name goes to daemon for server-side resolution

    # Derive direction from navigation sentinel or default to newest
    direction: str
    if args.navigation == "oldest":
        direction = "oldest"
    else:
        direction = "newest"
    # If navigation is an opaque token, direction is encoded in it (daemon handles it)

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
            dialog_id=dialog_id if dialog_id else 0,
            dialog_name=args.dialog if not dialog_id else None,
        )
        if isinstance(resolved, ToolResult):
            return ToolResult(
                content=resolved.content,
                has_filter=has_filter,
            )
        topic_id = resolved

    id_kwarg: dict = {"dialog_id": dialog_id} if dialog_id else {"dialog": args.dialog}
    try:
        async with daemon_connection() as conn:
            response = await conn.list_messages(
                **id_kwarg,
                limit=args.limit,
                navigation=args.navigation,
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
                "Use MarkDialogForSync to enable sync, then wait for syncing to complete.",
                has_filter=has_filter,
                has_cursor=has_cursor,
            )
        return error_result(
            f"Error: {error}: {error_detail}",
            has_filter=has_filter,
            has_cursor=has_cursor,
        )

    data = response.get("data", {})
    rows = data.get("messages", [])
    source = data.get("source", "unknown")
    next_nav = data.get("next_navigation")
    coverage = data.get("coverage")

    # Phase 39.3: extract read_state + dialog_type from daemon response (absent
    # in pre-39.3 responses → backward compat: format_messages no-ops).
    read_state = data.get("read_state")
    dialog_type = data.get("dialog_type")
    text = _format_daemon_messages(rows, read_state=read_state, dialog_type=dialog_type)
    if not text:
        text = "No messages found."

    warning = _format_archived_warning(data)
    source_note = f"[source: {source}]\n" if source else ""
    nav_note = f"\nnext_navigation: {next_nav}" if next_nav else ""
    result_text = warning + source_note + text + nav_note

    # Phase 999.1: surface coverage='fragment' annotation when daemon returns it.
    # Prepend a header so the agent knows the result is a point-fetched snippet,
    # not the full chat history.
    if coverage == "fragment":
        fragment_header = (
            "Coverage: fragment (partial — only point-fetched snippets; "
            "full sync not performed on this dialog)."
        )
        result_text = f"{fragment_header}\n\n{result_text}"

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
    return ToolResult(
        content=_text_response(result_text),
        structured_content=structured_content,
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
    annotations=ToolAnnotations(readOnlyHint=True),
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
        except Exception as exc:
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
            f"Error: {error}: {error_detail}",
            has_filter=True,
            has_cursor=args.navigation is not None,
        )

    data = response.get("data", {})
    rows = data.get("messages", [])
    next_nav = data.get("next_navigation")
    structured_results = _search_result_structured_rows(rows, args.query)
    structured_content = {
        "query": args.query,
        "results": structured_results,
        "count": len(structured_results),
        "next_navigation": next_nav,
    }

    if not rows:
        result_text = search_no_hits_text(dialog_label, args.query)
        return ToolResult(
            content=_text_response(result_text),
            structured_content=structured_content,
            result_count=0,
            has_filter=True,
            has_cursor=args.navigation is not None,
        )

    # Phase 39.3 (HIGH-1): extract per-dialog read_state map from daemon
    # response (absent in pre-39.3 responses → backward compat: no header block).
    read_state_per_dialog = data.get("read_state_per_dialog")
    text = _format_search_results(
        rows,
        args.query,
        global_mode=global_mode,
        read_state_per_dialog=read_state_per_dialog,
    )
    nav_note = f"\nnext_navigation: {next_nav}" if next_nav else ""

    warning = _format_archived_warning(data)
    result_text = warning + text + nav_note

    return ToolResult(
        content=_text_response(result_text),
        structured_content=structured_content,
        result_count=len(rows),
        has_filter=True,
        has_cursor=args.navigation is not None or bool(next_nav),
    )
