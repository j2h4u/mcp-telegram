from __future__ import annotations

from datetime import datetime, timezone

from pydantic import Field, model_validator

from ..errors import dialog_not_found_text, invalid_navigation_text, search_no_hits_text
from ..formatter import format_messages
from ..resolver import parse_exact_dialog_id
from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _text_response,
    daemon_connection,
    mcp_tool,
)

# ---------------------------------------------------------------------------
# Thin adapter: daemon row dict → MessageLike-compatible object
# ---------------------------------------------------------------------------


class _DaemonMessage:
    """Lightweight MessageLike adapter for daemon API row dicts.

    format_messages() accesses: .id, .date, .message, .sender, .reply_to,
    .reactions, .media, and optionally .edit_date.
    """

    __slots__ = (
        "id", "date", "message", "sender", "reply_to", "reactions", "media",
        "edit_date", "topic_title",
    )

    def __init__(self, row: dict) -> None:
        self.id: int = row["message_id"]
        sent_at = row.get("sent_at") or 0
        self.date = datetime.fromtimestamp(int(sent_at), tz=timezone.utc)
        self.message: str | None = row.get("text")
        sender_name = row.get("sender_first_name")
        self.sender = _Sender(sender_name) if sender_name else None
        reply_id = row.get("reply_to_msg_id")
        self.reply_to = _ReplyHeader(reply_id) if reply_id else None
        self.reactions = row.get("reactions")  # JSON string or None
        media_desc = row.get("media_description")
        self.media = _MediaPlaceholder(media_desc) if media_desc else None
        edit_date_raw = row.get("edit_date")
        if edit_date_raw is not None:
            self.edit_date: datetime | None = datetime.fromtimestamp(
                int(edit_date_raw), tz=timezone.utc
            )
        else:
            self.edit_date = None
        self.topic_title: str | None = row.get("topic_title")


class _Sender:
    __slots__ = ("first_name",)

    def __init__(self, name: str | None) -> None:
        self.first_name = name


class _ReplyHeader:
    __slots__ = ("reply_to_msg_id",)

    def __init__(self, msg_id: int | None) -> None:
        self.reply_to_msg_id = msg_id


class _MediaPlaceholder:
    """Wraps a pre-formatted media description string from sync.db."""

    __slots__ = ("_description",)

    def __init__(self, description: str) -> None:
        self._description = description

    def __str__(self) -> str:
        return self._description


# ---------------------------------------------------------------------------
# format_messages adapter for daemon data
# ---------------------------------------------------------------------------


def _format_daemon_messages(rows: list[dict]) -> str:
    """Format daemon row dicts into human-readable message text.

    Produces the same HH:mm Name: text format as format_messages(),
    but handles pre-formatted media descriptions and skips Telethon-specific
    protocol details that don't apply to daemon rows.
    """
    if not rows:
        return ""

    messages = [_DaemonMessage(r) for r in rows]

    # Build reply map from rows available in this page
    reply_map: dict[int, _DaemonMessage] = {m.id: m for m in messages}

    # Pass topic_name_getter when any message has a topic_title
    has_topics = any(getattr(m, "topic_title", None) for m in messages)
    topic_name_getter = (lambda msg: getattr(msg, "topic_title", None)) if has_topics else None

    return format_messages(  # type: ignore[arg-type]
        messages,
        reply_map=reply_map,
        topic_name_getter=topic_name_getter,
    )


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
    Use navigation= with the next_navigation token from a previous response to continue.
    Use sender= to filter messages from a specific person (name string, resolved via fuzzy match).
    Use topic= to filter messages to one forum topic after the dialog has been resolved.
    Use exact_topic_id= when the forum topic is already known and you want the direct-read path
    without defaulting to full topic discovery first.
    In forum dialogs, omitting topic= returns a cross-topic page and each message is labeled inline.
    Use unread=True to show only messages you haven't read yet.
    Default limit=50; set limit explicitly if you want a smaller MCP response.

    If response is ambiguous (multiple matches), retry with one exact selector instead of leaving
    both fuzzy and exact selectors in the same request.
    For @username lookups, prepend @ to the name: dialog="@username".
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
    sender: str | None = Field(default=None, max_length=500)
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


@mcp_tool("primary")
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
        try:
            async with daemon_connection() as topic_conn:
                topics_response = await topic_conn.list_topics(
                    dialog_id=dialog_id if dialog_id else 0,
                    dialog=args.dialog if not dialog_id else None,
                )
        except DaemonNotRunningError as e:
            return ToolResult(content=_text_response(str(e)))
        if topics_response.get("ok"):
            topics_data = topics_response.get("data", {}).get("topics", [])
            topic_lower = args.topic.lower()
            matched = [
                t for t in topics_data
                if topic_lower in (t.get("title") or "").lower()
            ]
            if len(matched) == 1:
                topic_id = matched[0]["id"]
            elif len(matched) > 1:
                exact = [
                    t for t in matched
                    if (t.get("title") or "").lower() == topic_lower
                ]
                if len(exact) == 1:
                    topic_id = exact[0]["id"]
                else:
                    names = ", ".join(t.get("title", "?") for t in matched[:5])
                    return ToolResult(
                        content=_text_response(
                            f"Ambiguous topic '{args.topic}'. Matches: {names}. "
                            "Use exact_topic_id."
                        ),
                        has_filter=has_filter,
                    )
            else:
                return ToolResult(
                    content=_text_response(
                        f"Topic '{args.topic}' not found in this dialog."
                    ),
                    has_filter=has_filter,
                )

    try:
        async with daemon_connection() as conn:
            if dialog_id is not None and dialog_id != 0:
                response = await conn.list_messages(
                    dialog_id=dialog_id,
                    limit=args.limit,
                    navigation=args.navigation,
                    direction=direction,
                    sender_name=sender_name,
                    topic_id=topic_id,
                    unread=unread_flag,
                )
            else:
                # Daemon resolves dialog name via Telegram
                response = await conn.list_messages(
                    dialog=args.dialog,
                    limit=args.limit,
                    navigation=args.navigation,
                    direction=direction,
                    sender_name=sender_name,
                    topic_id=topic_id,
                    unread=unread_flag,
                )
    except DaemonNotRunningError as e:
        return ToolResult(content=_text_response(str(e)))

    if not response.get("ok"):
        error = response.get("error", "unknown")
        message = response.get("message", "")
        if error == "dialog_not_found":
            dialog_label = str(dialog_id) if dialog_id else (args.dialog or "")
            return ToolResult(
                content=_text_response(dialog_not_found_text(dialog_label, retry_tool="ListMessages")),
                has_filter=has_filter,
                has_cursor=has_cursor,
            )
        return ToolResult(
            content=_text_response(f"Error: {error}: {message}"),
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

    source_note = f"[source: {source}]\n" if source else ""
    nav_note = f"\nnext_navigation: {next_nav}" if next_nav else ""
    result_text = source_note + text + nav_note

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
    Search messages in a dialog by text query. Returns matching messages newest to oldest.

    Omit navigation to start from the first search page.
    Use navigation= with the next_navigation token from a previous response to continue.

    If response is ambiguous, use the numeric ID from the matches list to disambiguate.
    For @username lookups, prepend @ to the dialog name: dialog="@channel_name".
    """

    dialog: str = Field(
        max_length=500,
        description=(
            "Dialog selector for one scoped search. Accepts an exact numeric dialog id for the "
            "direct path, or @username / fuzzy dialog name for the ambiguity-safe path."
        )
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


@mcp_tool("primary")
async def search_messages(args: SearchMessages) -> ToolResult:
    # Resolve dialog_id locally if possible (numeric string / @username)
    dialog_id: int | None = None
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
        async with daemon_connection() as conn:
            if dialog_id is not None and dialog_id != 0:
                response = await conn.search_messages(
                    dialog_id=dialog_id,
                    query=args.query,
                    limit=args.limit,
                    offset=offset,
                )
            else:
                # Daemon resolves dialog name via Telegram
                response = await conn.search_messages(
                    dialog=args.dialog,
                    query=args.query,
                    limit=args.limit,
                    offset=offset,
                )
    except DaemonNotRunningError as e:
        return ToolResult(content=_text_response(str(e)))

    if not response.get("ok"):
        error = response.get("error", "unknown")
        message = response.get("message", "")
        if error == "dialog_not_found":
            dialog_label = str(dialog_id) if dialog_id else args.dialog
            return ToolResult(
                content=_text_response(dialog_not_found_text(dialog_label, retry_tool="SearchMessages")),
                has_filter=True,
                has_cursor=args.navigation is not None,
            )
        return ToolResult(
            content=_text_response(f"Error: {error}: {message}"),
            has_filter=True,
            has_cursor=args.navigation is not None,
        )

    data = response.get("data", {})
    rows = data.get("messages", [])
    next_nav = data.get("next_navigation")
    dialog_label = str(dialog_id) if dialog_id else args.dialog

    if not rows:
        result_text = search_no_hits_text(dialog_label, args.query)
        return ToolResult(
            content=_text_response(result_text),
            result_count=0,
            has_filter=True,
            has_cursor=args.navigation is not None,
        )

    text = _format_daemon_messages(rows)
    nav_note = f"\nnext_navigation: {next_nav}" if next_nav else ""
    result_text = text + nav_note

    return ToolResult(
        content=_text_response(result_text),
        result_count=len(rows),
        has_filter=True,
        has_cursor=args.navigation is not None or bool(next_nav),
    )
