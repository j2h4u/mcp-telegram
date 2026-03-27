from __future__ import annotations

import typing as t
from datetime import datetime, timezone

from pydantic import Field

from ..errors import no_unread_all_text, no_unread_personal_text
from ..formatter import UnreadChatData, format_unread_messages_grouped
from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _text_response,
    daemon_connection,
    mcp_tool,
)


def _daemon_not_running_text() -> str:
    return (
        "Sync daemon is not running.\n"
        "Action: Start it with: mcp-telegram sync"
    )


class _DaemonUnreadMessage:
    """Adapter: daemon row dict -> MessageLike for format_messages()."""

    __slots__ = (
        "id", "date", "message", "sender", "sender_id",
        "media", "reply_to", "reactions", "edit_date", "forum_topic_id",
    )

    def __init__(self, row: dict) -> None:
        self.id: int = row.get("message_id", 0)
        ts = row.get("sent_at", 0)
        self.date = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(tz=timezone.utc)
        self.message: str | None = row.get("text")
        self.sender_id: int | None = row.get("sender_id")
        self.media: None = None
        self.reply_to: None = None
        self.reactions: None = None
        self.edit_date: None = None
        self.forum_topic_id: None = None

        first_name = row.get("sender_first_name")
        self.sender: _Sender | None = _Sender(first_name) if first_name else None


class _Sender:
    __slots__ = ("first_name", "last_name")

    def __init__(self, name: str | None) -> None:
        self.first_name = name
        self.last_name = None


class ListUnreadMessages(ToolArgs):
    """Fetch unread messages from personal chats and small groups, prioritized by tier.

    Priority tiers (lower = higher priority): @mentions in DMs, @mentions in groups,
    human DMs, bot DMs, small groups, large groups, channels.
    Within each tier, chats are sorted by recency (newest first).
    Per-chat message budget is allocated proportionally to prevent flooding.

    Use scope="personal" (default) to see only DMs and small groups (≤ group_size_threshold members).
    Use scope="all" to include large groups and channels (shows counts only, no messages).
    Use limit to control total messages (default 100, minimum across all chats).
    """

    scope: t.Literal["personal", "all"] = Field(
        default="personal",
        description="'personal' (DMs + small groups) or 'all' (everything)"
    )
    limit: int = Field(
        default=100,
        ge=50,
        le=500,
        description="Total message budget across all chats (50-500)"
    )
    group_size_threshold: int = Field(
        default=100,
        ge=10,
        description="Group member count above which to hide messages (scope=personal only)"
    )


@mcp_tool("primary")
async def list_unread_messages(args: ListUnreadMessages) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.list_unread_messages(
                scope=args.scope,
                limit=args.limit,
                group_size_threshold=args.group_size_threshold,
            )
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if not response.get("ok"):
        error_msg = response.get("message", "Daemon returned an error.")
        return ToolResult(content=_text_response(f"Error: {error_msg}"))

    data = response.get("data", {})
    groups = data.get("groups", [])

    if not groups:
        empty_msg = no_unread_all_text() if args.scope == "all" else no_unread_personal_text()
        return ToolResult(content=_text_response(empty_msg))

    # Convert daemon response to UnreadChatData for formatting
    chats_data: list[UnreadChatData] = []
    total_messages_shown = 0

    for group in groups:
        messages = [_DaemonUnreadMessage(m) for m in group.get("messages", [])]
        chat_data = UnreadChatData(
            chat_id=group.get("dialog_id", 0),
            display_name=group.get("display_name", ""),
            unread_count=group.get("unread_count", 0),
            unread_mentions_count=group.get("unread_mentions_count", 0),
            total_in_chat=group.get("unread_count", 0),
            is_channel=group.get("category") == "channel",
            is_bot=group.get("category") == "bot",
        )
        chat_data.messages = messages
        total_messages_shown += len(messages)
        chats_data.append(chat_data)

    result_text = format_unread_messages_grouped(chats_data)
    return ToolResult(content=_text_response(result_text), result_count=total_messages_shown)
