"""Unread messages capability: collect, prioritize, and fetch unread messages."""
from __future__ import annotations

import logging
import sqlite3
import typing as t
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telethon import TelegramClient  # type: ignore[import-untyped]

from .budget import allocate_message_budget_proportional, unread_chat_tier
from .cache import EntityCache
from .dialog_target import classify_dialog
from .formatter import UnreadChatData
from .resolver import cache_dialog_entry

logger = logging.getLogger(__name__)


@dataclass
class UnreadDialogEntry:
    """One unread dialog discovered during collection."""

    chat_id: int
    display_name: str
    unread_count: int
    unread_mentions_count: int
    category: str
    date: datetime | None
    read_inbox_max_id: int
    tier: int = field(default=0, init=False)


@dataclass(frozen=True)
class UnreadMessagesExecution:
    """Result of unread messages capability execution."""

    chats_data: list[UnreadChatData]
    total_messages_shown: int


async def collect_unread_dialogs(
    client: TelegramClient,
    *,
    cache: EntityCache,
    scope: t.Literal["personal", "all"],
    group_size_threshold: int,
) -> tuple[list[UnreadDialogEntry], dict[int, int]]:
    """Iterate Telegram dialogs and collect those with unread messages.

    Returns (unread_chats, unread_counts) after applying scope filters and
    caching each dialog entry.
    """
    unread_chats: list[UnreadDialogEntry] = []
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

        if scope == "personal":
            if category == "channel" or (
                category == "group"
                and participants_count is not None
                and participants_count > group_size_threshold
            ):
                continue

        try:
            cache_dialog_entry(cache, dialog)
        except sqlite3.Error as cache_exc:
            logger.warning("dialog_cache_update_failed dialog_id=%r error=%s", chat_id, cache_exc)

        unread_chats.append(UnreadDialogEntry(
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


async def execute_unread_messages_capability(
    client: TelegramClient,
    *,
    cache: EntityCache,
    scope: t.Literal["personal", "all"],
    limit: int,
    group_size_threshold: int,
) -> UnreadMessagesExecution:
    """Collect, prioritize, allocate budget, and fetch unread messages.

    Returns an execution result with chats_data (possibly empty) and
    total_messages_shown count.
    """
    unread_chats, unread_counts = await collect_unread_dialogs(
        client, cache=cache, scope=scope, group_size_threshold=group_size_threshold,
    )

    if not unread_chats:
        return UnreadMessagesExecution(chats_data=[], total_messages_shown=0)

    for entry in unread_chats:
        entry.tier = unread_chat_tier({
            "unread_mentions_count": entry.unread_mentions_count,
            "category": entry.category,
        })

    unread_chats.sort(key=lambda e: (e.tier, -(e.date.timestamp() if e.date else 0)))
    allocation = allocate_message_budget_proportional(unread_counts, limit)

    chats_data: list[UnreadChatData] = []
    total_messages_shown = 0

    for entry in unread_chats:
        budget_for_chat = allocation.get(entry.chat_id, 0)

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

    return UnreadMessagesExecution(chats_data=chats_data, total_messages_shown=total_messages_shown)
