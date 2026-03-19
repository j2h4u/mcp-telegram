"""Integration tests for search cache population (BYP-04, CACHE-05).

These tests verify:
- SearchMessages always hits the Telegram API (BYP-04 — no cache read for search)
- Search results are written to MessageCache after the API fetch
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_telegram.cache import EntityCache, MessageCache
from mcp_telegram.capability_search import execute_search_messages_capability
from mcp_telegram.models import SearchExecution
from mcp_telegram.resolver import Resolved


def _make_msg(
    id: int,
    text: str = "search result",
    sender_id: int = 101,
    sender_name: str = "Alice",
    date: datetime | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.id = id
    msg.message = text
    msg.text = text
    msg.sender_id = sender_id
    msg.sender = MagicMock(first_name=sender_name, last_name=None, username=None)
    msg.date = date or datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    msg.reply_to = None
    msg.reactions = None
    msg.media = None
    return msg


async def _make_async_iter(items):
    for item in items:
        yield item


DIALOG_ID = 101
DIALOG_NAME = "Test Chat"


def _make_resolver(entity_id: int = DIALOG_ID, display_name: str = DIALOG_NAME):
    return AsyncMock(return_value=Resolved(entity_id=entity_id, display_name=display_name))


async def test_search_always_hits_api(tmp_db_path: Path) -> None:
    """SearchMessages always fetches from API regardless of cache state — BYP-04."""
    cache = EntityCache(tmp_db_path)
    msg_cache = MessageCache(cache._conn)

    # Seed the cache with messages that could be returned
    msg_cache.store_messages(101, [_make_msg(id=i) for i in range(90, 95)])

    search_hits = [_make_msg(id=i, text="found it") for i in range(90, 93)]

    client = MagicMock()
    client.iter_messages = MagicMock(return_value=_make_async_iter(search_hits))
    client.get_messages = AsyncMock(return_value=[])

    result = await execute_search_messages_capability(
        client,
        cache=cache,
        dialog_query=DIALOG_NAME,
        query="found it",
        limit=3,
        navigation=None,
        retry_tool="SearchMessages",
        resolve_dialog=_make_resolver(entity_id=101),
        reaction_names_threshold=15,
    )

    assert isinstance(result, SearchExecution)
    # BYP-04: search always calls API
    client.iter_messages.assert_called_once()


async def test_search_populates_cache_after_fetch(tmp_db_path: Path) -> None:
    """Search hit messages are stored in MessageCache after the API fetch — CACHE-05."""
    cache = EntityCache(tmp_db_path)

    search_hits = [_make_msg(id=300 + i, text="keyword") for i in range(3)]

    client = MagicMock()
    client.iter_messages = MagicMock(return_value=_make_async_iter(search_hits))
    client.get_messages = AsyncMock(return_value=[])

    result = await execute_search_messages_capability(
        client,
        cache=cache,
        dialog_query=DIALOG_NAME,
        query="keyword",
        limit=3,
        navigation=None,
        retry_tool="SearchMessages",
        resolve_dialog=_make_resolver(entity_id=101),
        reaction_names_threshold=15,
    )

    assert isinstance(result, SearchExecution)

    # After search, messages must be in MessageCache
    rows = cache._conn.execute(
        "SELECT message_id FROM message_cache WHERE dialog_id = 101 ORDER BY message_id"
    ).fetchall()
    stored_ids = [row[0] for row in rows]
    assert stored_ids == [300, 301, 302]
