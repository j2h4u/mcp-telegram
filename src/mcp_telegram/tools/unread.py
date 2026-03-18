from __future__ import annotations

import logging
import sqlite3
import typing as t
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import Field

from .. import capabilities
from ..capabilities import allocate_message_budget_proportional
from ..cache import EntityCache
from ..dialog_target import classify_dialog
from ..errors import no_unread_all_text, no_unread_personal_text
from ..formatter import UnreadChatData, format_unread_messages_grouped
from ..resolver import cache_dialog_entry
from ._base import ToolArgs, ToolResult, _text_response, connected_client, get_entity_cache, mcp_tool

logger = logging.getLogger(__name__)


@dataclass
class _UnreadDialogEntry:
    """Internal accumulator for one unread dialog during collection."""

    chat_id: int
    display_name: str
    unread_count: int
    unread_mentions_count: int
    category: str
    date: datetime | None
    read_inbox_max_id: int
    tier: int = field(default=0, init=False)


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


async def _collect_unread_dialogs(
    client,  # noqa: ANN001
    args: ListUnreadMessages,
    cache: EntityCache,
) -> tuple[list[_UnreadDialogEntry], dict[int, int]]:
    """Iterate Telegram dialogs and collect those with unread messages.

    Returns (unread_chats, unread_counts) after applying scope filters and
    caching each dialog entry.
    """
    unread_chats: list[_UnreadDialogEntry] = []
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

        # Raw TL dialog carries read_inbox_max_id — used as min_id for fetch
        raw_dialog = getattr(dialog, "dialog", None)
        read_inbox_max_id = getattr(raw_dialog, "read_inbox_max_id", 0) if raw_dialog else 0

        entity = getattr(dialog, "entity", None)
        participants_count = getattr(entity, "participants_count", None) if entity is not None else None

        if args.scope == "personal":
            if category == "channel" or (
                category == "group"
                and participants_count is not None
                and participants_count > args.group_size_threshold
            ):
                continue

        try:
            cache_dialog_entry(cache, dialog)
        except sqlite3.Error as cache_exc:
            logger.warning("dialog_cache_update_failed dialog_id=%r error=%s", chat_id, cache_exc)

        unread_chats.append(_UnreadDialogEntry(
            chat_id=chat_id,
            display_name=display_name,
            unread_count=unread_count,
            unread_mentions_count=unread_mentions_count,
            category=category,
            date=date,
            read_inbox_max_id=read_inbox_max_id,
        ))
        unread_counts[chat_id] = unread_count

    return unread_chats, unread_counts


@mcp_tool("primary")
async def list_unread_messages(args: ListUnreadMessages) -> ToolResult:
    cache = get_entity_cache()

    async with connected_client() as client:
        unread_chats, unread_counts = await _collect_unread_dialogs(client, args, cache)

        if not unread_chats:
            empty_msg = no_unread_all_text() if args.scope == "all" else no_unread_personal_text()
            return ToolResult(content=_text_response(empty_msg))

        for entry in unread_chats:
            entry.tier = capabilities.unread_chat_tier({
                "unread_mentions_count": entry.unread_mentions_count,
                "category": entry.category,
            })

        unread_chats.sort(key=lambda e: (e.tier, -(e.date.timestamp() if e.date else 0)))
        allocation = allocate_message_budget_proportional(unread_counts, args.limit)

        chats_data: list[UnreadChatData] = []
        total_messages_shown = 0

        for entry in unread_chats:
            budget_for_chat = allocation.get(entry.chat_id, 0)

            # Base fields shared by all paths
            chat_data = UnreadChatData(
                chat_id=entry.chat_id,
                display_name=entry.display_name,
                unread_count=entry.unread_count,
                unread_mentions_count=entry.unread_mentions_count,
                total_in_chat=entry.unread_count,
                is_channel=entry.category == "channel",
                is_bot=entry.category == "bot",
            )

            if budget_for_chat == 0:
                if entry.category == "channel":
                    chats_data.append(chat_data)
                continue

            try:
                messages: list = []
                async for msg in client.iter_messages(entry.chat_id, min_id=entry.read_inbox_max_id, limit=budget_for_chat):
                    messages.append(msg)

                chat_data.messages = messages
                total_messages_shown += len(chat_data.messages)

            except Exception as exc:
                logger.warning("unread_fetch_failed chat_id=%r error_type=%s error=%s", entry.chat_id, type(exc).__name__, exc, exc_info=True)

            chats_data.append(chat_data)

    result_text = format_unread_messages_grouped(chats_data)
    return ToolResult(content=_text_response(result_text), result_count=total_messages_shown)
