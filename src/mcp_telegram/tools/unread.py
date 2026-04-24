import typing as t

from pydantic import Field

from ..errors import no_unread_all_text, no_unread_personal_text
from ..formatter import UnreadChatData, format_unread_messages_grouped
from ..models import ReadMessage, ReadState
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    mcp_tool,
)


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


@mcp_tool("primary", annotations=ToolAnnotations(readOnlyHint=True))
async def get_inbox(args: GetInbox) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_inbox(
                scope=args.scope,
                limit=args.limit,
                group_size_threshold=args.group_size_threshold,
            )
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if err := _check_daemon_response(response):
        return err

    data = response.get("data", {})
    groups = data.get("groups", [])
    # Defensive: older daemon responses or test mocks may omit bootstrap_pending.
    # Treat missing as 0 (full coverage assumed). Also guard against explicit None.
    bootstrap_pending = int(data.get("bootstrap_pending", 0) or 0)

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
            return ToolResult(content=_text_response(warning))
        empty_msg = no_unread_all_text() if args.scope == "all" else no_unread_personal_text()
        return ToolResult(content=_text_response(empty_msg))

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
    return ToolResult(content=_text_response(result_text), result_count=result_count)
