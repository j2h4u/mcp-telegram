
from datetime import datetime, timezone

from pydantic import Field, model_validator

from ..errors import dialog_not_found_text, invalid_navigation_text, search_no_hits_text
from ..formatter import format_messages
from ..resolver import parse_exact_dialog_id
from ._adapters import DaemonMessage
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _text_response,
    daemon_connection,
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
    date_str = (
        datetime.fromtimestamp(sync_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if sync_ts
        else "unknown date"
    )
    warning = (
        "\u26a0 No current access to this dialog. "
        f"Messages are from the local archive (last sync: {date_str}).\n"
    )
    sync_coverage_pct = data.get("sync_coverage_pct")
    archived_message_count = data.get("archived_message_count")
    if sync_coverage_pct is not None and sync_coverage_pct < 100:
        warning += f"Archive coverage: {sync_coverage_pct}% of dialog history.\n"
    elif sync_coverage_pct is None:
        if archived_message_count is not None:
            warning += (
                f"Archive coverage: unknown "
                f"({archived_message_count} messages archived locally).\n"
            )
        else:
            warning += "Archive coverage: unknown.\n"
    return warning


# ---------------------------------------------------------------------------
# format_messages adapter for daemon data
# ---------------------------------------------------------------------------


def _format_daemon_messages(rows: list[dict], *, global_mode: bool = False) -> str:
    """Format daemon row dicts into human-readable message text.

    Produces the same HH:mm Name: text format as format_messages(),
    but handles pre-formatted media descriptions and skips Telethon-specific
    protocol details that don't apply to daemon rows.

    When global_mode=True, prefixes each line with "[dialog_name]" so results
    from different dialogs are distinguishable.
    """
    if not rows:
        return ""

    messages = [DaemonMessage(r) for r in rows]

    # Build reply map from rows available in this page
    reply_map: dict[int, DaemonMessage] = {m.id: m for m in messages}

    # Pass topic_name_getter when any message has a topic_title
    has_topics = any(getattr(m, "topic_title", None) for m in messages)
    topic_name_getter = (lambda msg: getattr(msg, "topic_title", None)) if has_topics else None

    line_prefix_getter = (
        (lambda msg: f"[{getattr(msg, 'dialog_name', None) or '?'}]") if global_mode else None
    )

    return format_messages(
        messages,  # type: ignore[arg-type]  # DaemonMessage satisfies MessageLike duck-typed, not statically
        reply_map=reply_map,  # type: ignore[arg-type]  # same: dict[int, DaemonMessage] vs dict[int, MessageLike]
        topic_name_getter=topic_name_getter,
        line_prefix_getter=line_prefix_getter,
    )


# ---------------------------------------------------------------------------
# Search result formatting — snippets + anchors
# ---------------------------------------------------------------------------

_SNIPPET_MAX_LEN = 150
_SNIPPET_LEAD = 50  # chars to show before the matched word


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


def _format_search_results(rows: list[dict], query: str, *, global_mode: bool = False) -> str:
    """Format search result rows as compact snippet lines with msg_id anchors.

    Each line:  [DialogName] YYYY-MM-DD HH:MM Sender (msg_id:N): "...snippet..."

    dialog_name prefix is included only when global_mode=True.
    """
    if not rows:
        return ""

    lines: list[str] = []
    for row in rows:
        msg_id = row["message_id"]
        sent_at = row.get("sent_at") or 0
        sender = row.get("sender_first_name") or "?"
        dt = datetime.fromtimestamp(int(sent_at), tz=timezone.utc)
        time_str = dt.strftime("%Y-%m-%d %H:%M")
        snippet = _extract_snippet(row.get("text"), query)

        dialog_prefix = f"[{row.get('dialog_name') or '?'}] " if global_mode else ""
        lines.append(f'{dialog_prefix}{time_str} {sender} (msg_id:{msg_id}): "{snippet}"')

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
    """

    dialog: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional natural dialog selector: numeric id, @username, or fuzzy dialog name. "
            "Use this for exploratory or ambiguity-safe reads. Mutually exclusive with exact_dialog_id."
        )
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
            "Optional natural topic title resolved within the selected dialog. "
            "Mutually exclusive with exact_topic_id."
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
                dialog_id=dialog_id, dialog=dialog_name,
            )
    except DaemonNotRunningError as e:
        return ToolResult(content=_text_response(str(e)))

    if not response.get("ok"):
        error = response.get("error", "unknown")
        error_detail = response.get("message", "")
        return ToolResult(content=_text_response(f"Topic lookup failed: {error}: {error_detail}"))

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
        return ToolResult(
            content=_text_response(
                f"Ambiguous topic '{topic_name}'. Matches: {names}. Use exact_topic_id."
            ),
        )

    return ToolResult(
        content=_text_response(f"Topic '{topic_name}' not found in this dialog."),
    )


@mcp_tool("primary", annotations=ToolAnnotations(readOnlyHint=True))
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

    # Sender passthrough
    sender_name: str | None = args.sender

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
                content=resolved.content, has_filter=has_filter,
            )
        topic_id = resolved

    id_kwarg: dict = (
        {"dialog_id": dialog_id} if dialog_id else {"dialog": args.dialog}
    )
    try:
        async with daemon_connection() as conn:
            response = await conn.list_messages(
                **id_kwarg,
                limit=args.limit,
                navigation=args.navigation,
                direction=direction,
                sender_name=sender_name,
                topic_id=topic_id,
                unread=unread_flag,
                context_message_id=args.anchor_message_id,
                context_size=args.context_size if args.anchor_message_id else None,
            )
    except DaemonNotRunningError as e:
        return ToolResult(content=_text_response(str(e)))

    if not response.get("ok"):
        error = response.get("error", "unknown")
        error_detail = response.get("message", "")
        if error == "dialog_not_found":
            dialog_label = str(dialog_id) if dialog_id else (args.dialog or "")
            return ToolResult(
                content=_text_response(dialog_not_found_text(dialog_label, retry_tool="ListMessages")),
                has_filter=has_filter,
                has_cursor=has_cursor,
            )
        if error == "not_synced":
            return ToolResult(
                content=_text_response(
                    "Error: dialog is not synced. "
                    "Use MarkDialogForSync to enable sync, then wait for syncing to complete."
                ),
                has_filter=has_filter,
                has_cursor=has_cursor,
            )
        return ToolResult(
            content=_text_response(f"Error: {error}: {error_detail}"),
            has_filter=has_filter,
            has_cursor=has_cursor,
        )

    data = response.get("data", {})
    rows = data.get("messages", [])
    source = data.get("source", "unknown")
    next_nav = data.get("next_navigation")

    text = _format_daemon_messages(rows)
    if not text:
        text = "No messages found."

    warning = _format_archived_warning(data)
    source_note = f"[source: {source}]\n" if source else ""
    nav_note = f"\nnext_navigation: {next_nav}" if next_nav else ""
    result_text = warning + source_note + text + nav_note

    return ToolResult(
        content=_text_response(result_text),
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


@mcp_tool("primary", annotations=ToolAnnotations(readOnlyHint=True))
async def search_messages(args: SearchMessages) -> ToolResult:
    global_mode = args.dialog is None

    # Resolve dialog_id locally if possible (numeric string / @username)
    dialog_id: int | None = None
    if not global_mode:
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
            if nav.kind == "search":
                offset = nav.value
        except Exception as exc:
            return ToolResult(
                content=_text_response(
                    invalid_navigation_text(str(exc), retry_tool="SearchMessages")
                ),
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
    except DaemonNotRunningError as e:
        return ToolResult(content=_text_response(str(e)))

    if not response.get("ok"):
        error = response.get("error", "unknown")
        error_detail = response.get("message", "")
        if error == "dialog_not_found":
            dialog_label = str(dialog_id) if dialog_id else args.dialog
            return ToolResult(
                content=_text_response(dialog_not_found_text(dialog_label, retry_tool="SearchMessages")),
                has_filter=True,
                has_cursor=args.navigation is not None,
            )
        return ToolResult(
            content=_text_response(f"Error: {error}: {error_detail}"),
            has_filter=True,
            has_cursor=args.navigation is not None,
        )

    data = response.get("data", {})
    rows = data.get("messages", [])
    next_nav = data.get("next_navigation")
    dialog_label: str | None = str(dialog_id) if dialog_id else args.dialog

    if not rows:
        result_text = search_no_hits_text(dialog_label, args.query)
        return ToolResult(
            content=_text_response(result_text),
            result_count=0,
            has_filter=True,
            has_cursor=args.navigation is not None,
        )

    text = _format_search_results(rows, args.query, global_mode=global_mode)
    nav_note = f"\nnext_navigation: {next_nav}" if next_nav else ""

    warning = _format_archived_warning(data)
    result_text = warning + text + nav_note

    return ToolResult(
        content=_text_response(result_text),
        result_count=len(rows),
        has_filter=True,
        has_cursor=args.navigation is not None or bool(next_nav),
    )
