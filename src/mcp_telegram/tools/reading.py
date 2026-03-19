from __future__ import annotations

from pydantic import Field, model_validator

from .. import capabilities
from ..capabilities import ExactTargetHints
from ..errors import search_no_hits_text
from ..formatter import format_messages
from ..resolver import parse_exact_dialog_id
from ._base import REACTION_NAMES_THRESHOLD, ToolArgs, ToolResult, _resolve_dialog, _text_response, connected_client, get_entity_cache, get_prefetch_coordinator, mcp_tool


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

    cache = get_entity_cache()
    exact = ExactTargetHints(
        dialog_id=args.exact_dialog_id,
        topic_id=args.exact_topic_id,
    ) if args.exact_dialog_id is not None or args.exact_topic_id is not None else None

    async with connected_client() as client:
        history_execution = await capabilities.execute_history_read_capability(
            client,
            cache=cache,
            dialog_query=args.dialog,
            limit=args.limit,
            navigation=args.navigation,
            sender_query=args.sender,
            topic_query=args.topic,
            unread=args.unread,
            retry_tool="ListMessages",
            resolve_dialog=_resolve_dialog,

            reaction_names_threshold=REACTION_NAMES_THRESHOLD,
            load_topics=capabilities.load_dialog_topics,
            fetch_topic_messages_fn=capabilities.fetch_topic_messages,
            refresh_topic_by_id_fn=capabilities.refresh_topic_by_id,
            exact=exact,
            prefetch_coordinator=get_prefetch_coordinator(),
        )
    if isinstance(
        history_execution,
        (
            capabilities.DialogTargetFailure,
            capabilities.ForumTopicFailure,
            capabilities.MessageReadFailure,
            capabilities.NavigationFailure,
        ),
    ):
        return ToolResult(content=_text_response(history_execution.text), has_filter=has_filter, has_cursor=has_cursor)

    messages = list(history_execution.messages)
    text = format_messages(
        messages,
        reply_map=history_execution.reply_map,
        reaction_names_map=history_execution.reaction_names_map,
        topic_name_getter=history_execution.topic_name_getter,
    )
    if not text:
        text = capabilities.topic_empty_state_text(unread=args.unread)

    topic_prefix = (
        f"[topic: {history_execution.topic_name}]\n"
        if history_execution.topic_name
        else ""
    )
    result_text = history_execution.resolve_prefix + topic_prefix + text
    if history_execution.navigation is not None:
        result_text += f"\n\nnext_navigation: {history_execution.navigation.token}"
    return ToolResult(
        content=_text_response(result_text),
        result_count=len(messages),
        has_filter=has_filter,
        has_cursor=has_cursor,
    )


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
    cache = get_entity_cache()
    exact_dialog_id = parse_exact_dialog_id(args.dialog)
    dialog_query = None if exact_dialog_id is not None else args.dialog
    exact_dialog_name = cache.get_name(exact_dialog_id) if exact_dialog_id is not None else None

    exact = ExactTargetHints(
        dialog_id=exact_dialog_id,
        dialog_name=exact_dialog_name,
    ) if exact_dialog_id is not None else None

    async with connected_client() as client:
        search_execution = await capabilities.execute_search_messages_capability(
            client,
            cache=cache,
            dialog_query=dialog_query,
            query=args.query,
            limit=args.limit,
            navigation=args.navigation,
            retry_tool="SearchMessages",
            resolve_dialog=_resolve_dialog,

            reaction_names_threshold=REACTION_NAMES_THRESHOLD,
            exact=exact,
        )
    if isinstance(
        search_execution,
        capabilities.DialogTargetFailure | capabilities.NavigationFailure,
    ):
        return ToolResult(content=_text_response(search_execution.text), has_filter=True, has_cursor=args.navigation is not None)

    hits = list(search_execution.hits)
    if hits:
        result_text = search_execution.resolve_prefix + search_execution.rendered_text
    else:
        result_text = search_execution.resolve_prefix + search_no_hits_text(
            search_execution.dialog_name,
            args.query,
        )
    if search_execution.navigation is not None:
        result_text += f"\n\nnext_navigation: {search_execution.navigation.token}"
    return ToolResult(
        content=_text_response(result_text),
        result_count=len(hits),
        has_filter=True,
        has_cursor=args.navigation is not None,
    )
