from __future__ import annotations

import logging
import time
import typing as t
from contextlib import asynccontextmanager

from pydantic import Field

from .. import telegram as _telegram_mod
from ..capability_unread import execute_unread_messages_capability
from ..errors import no_unread_all_text, no_unread_personal_text
from ..formatter import format_unread_messages_grouped
from ._base import ToolArgs, ToolResult, _text_response, get_entity_cache, mcp_tool

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _connected_client():
    """Local direct Telegram connection for tools not yet routed through daemon API."""
    client = _telegram_mod.create_client()
    owns_connection = not client.is_connected()
    if owns_connection:
        t0 = time.monotonic()
        await client.connect()
        logger.debug("tg_connect: %.1fms", (time.monotonic() - t0) * 1000)
    try:
        yield client
    finally:
        if owns_connection:
            try:
                t0 = time.monotonic()
                await client.disconnect()
                logger.debug("tg_disconnect: %.1fms", (time.monotonic() - t0) * 1000)
            except Exception:
                logger.warning("tg_disconnect failed", exc_info=True)


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
    cache = get_entity_cache()

    async with _connected_client() as client:
        execution = await execute_unread_messages_capability(
            client,
            cache=cache,
            scope=args.scope,
            limit=args.limit,
            group_size_threshold=args.group_size_threshold,
        )

    if not execution.chats_data:
        empty_msg = no_unread_all_text() if args.scope == "all" else no_unread_personal_text()
        return ToolResult(content=_text_response(empty_msg))

    result_text = format_unread_messages_grouped(execution.chats_data)
    return ToolResult(content=_text_response(result_text), result_count=execution.total_messages_shown)
