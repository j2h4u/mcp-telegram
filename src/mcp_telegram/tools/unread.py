from __future__ import annotations

import logging
import sqlite3
import typing as t

from pydantic import Field

from .. import capabilities
from ..capabilities import allocate_message_budget_proportional
from ..dialog_target import classify_dialog
from ..errors import no_unread_all_text, no_unread_personal_text
from ..formatter import UnreadChatData, format_unread_messages_grouped
from ..resolver import cache_dialog_entry
from ._base import ToolArgs, ToolResult, _text_response, connected_client, get_entity_cache, mcp_tool

logger = logging.getLogger(__name__)


class ListUnreadMessages(ToolArgs):
    """Fetch unread messages from personal chats and small groups, sorted by mentions then recency.

    Surfaces @mentions at the top, groups DMs above group chats, and intelligently allocates
    a per-chat message budget to prevent flooding when many chats have unread messages.

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
    cache = get_entity_cache()

    async with connected_client() as client:
        # Iterate dialogs and collect unread chats
        unread_chats: list[dict] = []
        unread_counts: dict[int, int] = {}

        async for dialog in client.iter_dialogs(archived=None, ignore_pinned=False):
            unread_count = getattr(dialog, "unread_count", 0)
            if unread_count == 0:
                continue

            chat_id = getattr(dialog, "id", None)
            if not isinstance(chat_id, int):
                continue

            display_name = getattr(dialog, "name", f"Chat {chat_id}")
            category = classify_dialog(dialog)
            unread_mentions_count = getattr(dialog, "unread_mentions_count", 0)
            date = getattr(dialog, "date", None)

            # read_inbox_max_id from raw TL dialog — needed for min_id fetch
            raw_dialog = getattr(dialog, "dialog", None)
            read_inbox_max_id = getattr(raw_dialog, "read_inbox_max_id", 0) if raw_dialog else 0

            entity = getattr(dialog, "entity", None)
            participants_count = getattr(entity, "participants_count", None) if entity is not None else None

            # Apply scope filter
            if args.scope == "personal":
                if category == "channel" or (
                    category == "group"
                    and participants_count is not None
                    and participants_count > args.group_size_threshold
                ):
                    continue

            # Cache the dialog
            try:
                cache_dialog_entry(cache, dialog)
            except sqlite3.OperationalError as cache_exc:
                logger.warning("dialog_cache_update_failed dialog_id=%r error=%s", chat_id, cache_exc)

            # Collect unread chat info
            unread_chats.append({
                "chat_id": chat_id,
                "display_name": display_name,
                "unread_count": unread_count,
                "unread_mentions_count": unread_mentions_count,
                "category": category,
                "date": date,
                "read_inbox_max_id": read_inbox_max_id,
            })
            unread_counts[chat_id] = unread_count

        if not unread_chats:
            empty_msg = no_unread_all_text() if args.scope == "all" else no_unread_personal_text()
            return ToolResult(content=_text_response(empty_msg))

        # Assign priority tier, sort by (tier ASC, recency DESC).
        # Lower tier = higher priority. Add new tiers between existing values.
        for c in unread_chats:
            c["_tier"] = capabilities.unread_chat_tier(c)

        unread_chats.sort(key=lambda c: (c["_tier"], -(c["date"].timestamp() if c["date"] else 0)))

        # Allocate budget
        allocation = allocate_message_budget_proportional(unread_counts, args.limit)

        # Fetch messages for each chat
        chats_data: list[UnreadChatData] = []
        total_messages_shown = 0

        for chat_info in unread_chats:
            chat_id = chat_info["chat_id"]
            budget_for_chat = allocation.get(chat_id, 0)

            category = chat_info["category"]

            # Base fields shared by all paths
            base = UnreadChatData(
                chat_id=chat_id,
                display_name=chat_info["display_name"],
                unread_count=chat_info["unread_count"],
                unread_mentions_count=chat_info["unread_mentions_count"],
                total_in_chat=chat_info["unread_count"],
                is_channel=category == "channel",
                is_bot=category == "bot",
            )

            if budget_for_chat == 0:
                if category == "channel":
                    chats_data.append(base)
                continue

            # Fetch unread messages (min_id = read_inbox_max_id)
            try:
                read_max_id = chat_info.get("read_inbox_max_id", 0)
                messages: list = []
                async for msg in client.iter_messages(chat_id, min_id=read_max_id, limit=budget_for_chat):
                    messages.append(msg)

                base.messages = messages
                total_messages_shown += len(base.messages)

            except Exception as exc:
                logger.warning("Failed to fetch unread messages for chat %r: %s(%s)", chat_id, type(exc).__name__, exc)

            chats_data.append(base)

    # Format output
    result_text = format_unread_messages_grouped(chats_data)
    return ToolResult(content=_text_response(result_text), result_count=total_messages_shown)
