"""Integration tests for cache-first reads and bypass rules in capability_history.

These tests verify:
- Page 2+ reads are served from MessageCache when coverage exists (no API call)
- navigation=None/newest always hits API (BYP-01)
- unread=True always hits API (BYP-02)
- Every API fetch writes results to MessageCache (CACHE-05)
- Reply map tries cache first before API
- Prefetch scheduling: first page triggers dual prefetch, subsequent page triggers single prefetch
- Delta refresh is triggered on cache hit
- unread=True and empty results skip all prefetch
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from mcp_telegram.cache import EntityCache, MessageCache
from mcp_telegram.capability_history import execute_history_read_capability
from mcp_telegram.models import HistoryReadExecution
from mcp_telegram.pagination import HistoryDirection, encode_history_navigation
from mcp_telegram.resolver import Resolved

DIALOG_ID = 101
DIALOG_NAME = "Test Chat"


def _make_msg(
    id: int,
    text: str = "hello",
    sender_id: int = 201,
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


def _resolver():
    return AsyncMock(return_value=Resolved(entity_id=DIALOG_ID, display_name=DIALOG_NAME))


def _client_with_messages(messages):
    """Build a minimal mock client where iter_messages yields the given messages."""
    client = MagicMock()
    client.iter_messages = MagicMock(return_value=_make_async_iter(messages))
    client.get_messages = AsyncMock(return_value=[])
    return client


async def _call_history(
    client,
    cache: EntityCache,
    *,
    navigation: str | None,
    limit: int = 5,
    unread: bool = False,
) -> HistoryReadExecution:
    result = await execute_history_read_capability(
        client,
        cache=cache,
        dialog_query=DIALOG_NAME,
        limit=limit,
        navigation=navigation,
        sender_query=None,
        topic_query=None,
        unread=unread,
        retry_tool="ListMessages",
        resolve_dialog=_resolver(),
        reaction_names_threshold=15,
    )
    return result  # type: ignore[return-value]


async def test_history_cache_hit_skips_api(tmp_db_path: Path) -> None:
    """Page 2+ token with full cache coverage — API must NOT be called."""
    cache = EntityCache(tmp_db_path)
    msg_cache = MessageCache(cache._conn)

    # Seed 5 messages with IDs 95-99 (older than anchor 100, NEWEST direction)
    msg_cache.store_messages(DIALOG_ID, [_make_msg(id=i) for i in range(95, 100)])

    # Page-2 token: anchor=100, direction=NEWEST → want messages with id < 100
    token = encode_history_navigation(
        message_id=100, dialog_id=DIALOG_ID, direction=HistoryDirection.NEWEST
    )

    client = _client_with_messages([])  # API returns nothing — should not be called

    result = await _call_history(client, cache, navigation=token)

    assert isinstance(result, HistoryReadExecution)
    # Cache hit: iter_messages must NOT have been called
    client.iter_messages.assert_not_called()
    assert len(result.messages) == 5
    assert {m.id for m in result.messages} == {95, 96, 97, 98, 99}


async def test_history_cache_miss_falls_through_to_api(tmp_db_path: Path) -> None:
    """Partial cache (fewer than limit rows) triggers API fallback."""
    cache = EntityCache(tmp_db_path)
    msg_cache = MessageCache(cache._conn)

    # Only 2 messages — partial coverage for limit=5 → miss
    msg_cache.store_messages(DIALOG_ID, [_make_msg(id=98), _make_msg(id=99)])

    token = encode_history_navigation(
        message_id=100, dialog_id=DIALOG_ID, direction=HistoryDirection.NEWEST
    )

    api_msgs = [_make_msg(id=i) for i in range(90, 95)]
    client = _client_with_messages(api_msgs)

    result = await _call_history(client, cache, navigation=token)

    assert isinstance(result, HistoryReadExecution)
    # Cache miss: API must have been called
    client.iter_messages.assert_called_once()
    assert len(result.messages) == 5


async def test_history_cache_miss_populates_cache(tmp_db_path: Path) -> None:
    """API fetch results are written to MessageCache for future hits."""
    cache = EntityCache(tmp_db_path)

    token = encode_history_navigation(
        message_id=200, dialog_id=DIALOG_ID, direction=HistoryDirection.NEWEST
    )

    api_msgs = [_make_msg(id=i) for i in range(190, 195)]
    client = _client_with_messages(api_msgs)

    result = await _call_history(client, cache, navigation=token)

    assert isinstance(result, HistoryReadExecution)

    # After the API call, the cache must hold those messages
    msg_cache = MessageCache(cache._conn)
    cached = msg_cache.try_read_page(
        DIALOG_ID,
        topic_id=None,
        anchor_id=200,
        limit=5,
        direction=HistoryDirection.NEWEST,
    )
    assert cached is not None
    assert {m.id for m in cached} == {190, 191, 192, 193, 194}


async def test_history_newest_bypasses_cache(tmp_db_path: Path) -> None:
    """navigation=None (newest) always hits API even when cache is seeded — BYP-01."""
    cache = EntityCache(tmp_db_path)
    msg_cache = MessageCache(cache._conn)

    # Fully seed messages — cache bypass should ignore them
    msg_cache.store_messages(DIALOG_ID, [_make_msg(id=i) for i in range(95, 100)])

    api_msgs = [_make_msg(id=i) for i in range(200, 205)]
    client = _client_with_messages(api_msgs)

    result = await _call_history(client, cache, navigation=None)  # newest → bypass

    assert isinstance(result, HistoryReadExecution)
    # BYP-01: API must always be called for newest/None navigation
    client.iter_messages.assert_called_once()


async def test_history_unread_bypasses_cache(tmp_db_path: Path) -> None:
    """unread=True always hits API regardless of cache state — BYP-02."""
    cache = EntityCache(tmp_db_path)
    msg_cache = MessageCache(cache._conn)

    # Fully seed messages
    msg_cache.store_messages(DIALOG_ID, [_make_msg(id=i) for i in range(95, 100)])

    token = encode_history_navigation(
        message_id=100, dialog_id=DIALOG_ID, direction=HistoryDirection.NEWEST
    )

    api_msgs = [_make_msg(id=i) for i in range(95, 100)]

    peer_dialog_mock = MagicMock()
    peer_dialog_mock.dialogs = [MagicMock(read_inbox_max_id=90)]

    client = AsyncMock()
    client.iter_messages = MagicMock(return_value=_make_async_iter(api_msgs))
    client.get_messages = AsyncMock(return_value=[])
    client.get_input_entity = AsyncMock(return_value=MagicMock())
    # telethon client(SomeRequest()) awaitable call pattern — AsyncMock makes client(...) awaitable
    client.return_value = peer_dialog_mock

    result = await _call_history(client, cache, navigation=token, unread=True)

    assert isinstance(result, HistoryReadExecution)
    # BYP-02: must always hit API when unread=True
    client.iter_messages.assert_called_once()


async def test_history_oldest_first_page_uses_cache(tmp_db_path: Path) -> None:
    """navigation='oldest' is not a bypass case — cache is tried and served."""
    cache = EntityCache(tmp_db_path)
    msg_cache = MessageCache(cache._conn)

    # Seed 5 oldest messages (IDs 1-5) for OLDEST direction (min_id=1, ASC order)
    msg_cache.store_messages(DIALOG_ID, [_make_msg(id=i) for i in range(1, 6)])

    client = _client_with_messages([])  # API should not be called

    result = await _call_history(client, cache, navigation="oldest")

    assert isinstance(result, HistoryReadExecution)
    # Cache was tried for 'oldest' — API must NOT have been called
    client.iter_messages.assert_not_called()


async def test_history_always_populates_cache_on_api_fetch(tmp_db_path: Path) -> None:
    """Even a bypassed (newest) fetch writes its result to MessageCache — CACHE-05."""
    cache = EntityCache(tmp_db_path)

    api_msgs = [_make_msg(id=i) for i in range(200, 205)]
    client = _client_with_messages(api_msgs)

    await _call_history(client, cache, navigation=None)  # newest → bypass, but must populate

    rows = cache._conn.execute(
        "SELECT message_id FROM message_cache WHERE dialog_id = ?", (DIALOG_ID,)
    ).fetchall()
    stored_ids = {row[0] for row in rows}
    assert stored_ids == {200, 201, 202, 203, 204}


# ---------------------------------------------------------------------------
# Prefetch scheduling tests (PRE-01, PRE-02, PRE-03, REF-01)
# ---------------------------------------------------------------------------


async def _call_history_with_coordinator(
    client,
    cache: EntityCache,
    coordinator,
    *,
    navigation: str | None,
    limit: int = 5,
    unread: bool = False,
) -> HistoryReadExecution:
    result = await execute_history_read_capability(
        client,
        cache=cache,
        dialog_query=DIALOG_NAME,
        limit=limit,
        navigation=navigation,
        sender_query=None,
        topic_query=None,
        unread=unread,
        retry_tool="ListMessages",
        resolve_dialog=_resolver(),
        reaction_names_threshold=15,
        prefetch_coordinator=coordinator,
    )
    return result  # type: ignore[return-value]


async def test_first_page_schedules_dual_prefetch(tmp_db_path: Path) -> None:
    """PRE-01: navigation=None, non-empty messages -> coordinator.schedule called twice."""
    cache = EntityCache(tmp_db_path)
    msgs = [_make_msg(id=i) for i in range(96, 101)]  # ids 96,97,98,99,100
    client = _client_with_messages(msgs)

    coordinator = MagicMock()
    coordinator.schedule = MagicMock(return_value=True)

    result = await _call_history_with_coordinator(client, cache, coordinator, navigation=None)

    assert isinstance(result, HistoryReadExecution)
    assert coordinator.schedule.call_count == 2
    # Collect all keys passed
    keys = [call.kwargs["key"] for call in coordinator.schedule.call_args_list]
    # Next NEWEST page: anchor = min(ids) = 96, direction = "newest"
    assert (DIALOG_ID, "newest", 96, None) in keys
    # Oldest page: anchor = None, direction = "oldest"
    assert (DIALOG_ID, "oldest", None, None) in keys


async def test_first_page_oldest_schedules_next_oldest(tmp_db_path: Path) -> None:
    """PRE-01 + PRE-03: navigation='oldest', non-empty messages -> coordinator.schedule called once."""
    cache = EntityCache(tmp_db_path)
    # Oldest-first page: messages seeded so cache serves them (ids 1-5, ASC order)
    msgs = [_make_msg(id=i) for i in range(1, 6)]  # ids 1,2,3,4,5
    msg_cache = MessageCache(cache._conn)
    msg_cache.store_messages(DIALOG_ID, msgs)

    client = _client_with_messages([])  # cache hit — no API call needed

    coordinator = MagicMock()
    coordinator.schedule = MagicMock(return_value=True)

    result = await _call_history_with_coordinator(client, cache, coordinator, navigation="oldest")

    assert isinstance(result, HistoryReadExecution)
    # Only one task: next OLDEST page with anchor = max(ids) = 5
    # (dual oldest collapses — already reading oldest direction)
    assert coordinator.schedule.call_count == 1
    key = coordinator.schedule.call_args.kwargs["key"]
    assert key == (DIALOG_ID, "oldest", 5, None)


async def test_subsequent_page_schedules_next_prefetch(tmp_db_path: Path) -> None:
    """PRE-02: navigation=base64 token -> coordinator.schedule called once for next page."""
    cache = EntityCache(tmp_db_path)
    msgs = [_make_msg(id=i) for i in range(95, 100)]  # ids 95-99
    msg_cache = MessageCache(cache._conn)
    msg_cache.store_messages(DIALOG_ID, msgs)

    # Token: anchor=100 means "give me messages before id 100"
    token = encode_history_navigation(100, DIALOG_ID, direction=HistoryDirection.NEWEST)

    client = _client_with_messages([])  # cache hit

    coordinator = MagicMock()
    coordinator.schedule = MagicMock(return_value=True)

    result = await _call_history_with_coordinator(client, cache, coordinator, navigation=token)

    assert isinstance(result, HistoryReadExecution)
    # PRE-02: exactly one schedule call — next page in current NEWEST direction
    assert coordinator.schedule.call_count == 1
    key = coordinator.schedule.call_args.kwargs["key"]
    # Next anchor = min(95-99) = 95
    assert key == (DIALOG_ID, "newest", 95, None)


async def test_cache_hit_triggers_delta_refresh(tmp_db_path: Path) -> None:
    """REF-01: cached_page is not None -> coordinator.schedule called with delta key."""
    cache = EntityCache(tmp_db_path)
    msgs = [_make_msg(id=i) for i in range(95, 100)]  # ids 95-99; max=99
    msg_cache = MessageCache(cache._conn)
    msg_cache.store_messages(DIALOG_ID, msgs)

    token = encode_history_navigation(100, DIALOG_ID, direction=HistoryDirection.NEWEST)

    client = _client_with_messages([])

    coordinator = MagicMock()
    coordinator.schedule = MagicMock(return_value=True)

    result = await _call_history_with_coordinator(client, cache, coordinator, navigation=token)

    assert isinstance(result, HistoryReadExecution)
    keys = [call.kwargs["key"] for call in coordinator.schedule.call_args_list]
    # Delta refresh key: (entity_id, "delta", max_cached_id=99, topic_id=None)
    assert (DIALOG_ID, "delta", 99, None) in keys


async def test_unread_skips_all_prefetch(tmp_db_path: Path) -> None:
    """unread=True -> coordinator.schedule never called."""
    cache = EntityCache(tmp_db_path)

    api_msgs = [_make_msg(id=i) for i in range(95, 100)]

    peer_dialog_mock = MagicMock()
    peer_dialog_mock.dialogs = [MagicMock(read_inbox_max_id=90)]

    client = AsyncMock()
    client.iter_messages = MagicMock(return_value=_make_async_iter(api_msgs))
    client.get_messages = AsyncMock(return_value=[])
    client.get_input_entity = AsyncMock(return_value=MagicMock())
    client.return_value = peer_dialog_mock

    coordinator = MagicMock()
    coordinator.schedule = MagicMock(return_value=True)

    result = await _call_history_with_coordinator(
        client, cache, coordinator, navigation=None, unread=True
    )

    assert isinstance(result, HistoryReadExecution)
    coordinator.schedule.assert_not_called()


async def test_empty_messages_no_prefetch(tmp_db_path: Path) -> None:
    """API returns empty list -> no schedule calls."""
    cache = EntityCache(tmp_db_path)
    client = _client_with_messages([])  # empty result

    coordinator = MagicMock()
    coordinator.schedule = MagicMock(return_value=True)

    # Use navigation=None (newest) so cache is bypassed and empty API result is used
    result = await _call_history_with_coordinator(client, cache, coordinator, navigation=None)

    assert isinstance(result, HistoryReadExecution)
    coordinator.schedule.assert_not_called()


async def test_prefetch_coordinator_none_no_error(tmp_db_path: Path) -> None:
    """prefetch_coordinator=None -> function returns normally without AttributeError."""
    cache = EntityCache(tmp_db_path)
    msgs = [_make_msg(id=i) for i in range(96, 101)]
    client = _client_with_messages(msgs)

    result = await execute_history_read_capability(
        client,
        cache=cache,
        dialog_query=DIALOG_NAME,
        limit=5,
        navigation=None,
        sender_query=None,
        topic_query=None,
        unread=False,
        retry_tool="ListMessages",
        resolve_dialog=_resolver(),
        reaction_names_threshold=15,
        prefetch_coordinator=None,
    )
    assert isinstance(result, HistoryReadExecution)


async def test_prefetch_does_not_block_response(tmp_db_path: Path) -> None:
    """execute_history_read_capability returns HistoryReadExecution before tasks complete."""
    cache = EntityCache(tmp_db_path)
    msgs = [_make_msg(id=i) for i in range(96, 101)]
    client = _client_with_messages(msgs)

    # Coordinator that records schedule was called (tasks are fire-and-forget)
    schedule_calls: list[object] = []

    coordinator = MagicMock()
    coordinator.schedule = MagicMock(side_effect=lambda coro, *, key: schedule_calls.append(key) or True)

    result = await _call_history_with_coordinator(client, cache, coordinator, navigation=None)

    # Response returned immediately even if schedule was called
    assert isinstance(result, HistoryReadExecution)
    # Scheduling was invoked (fire-and-forget pattern confirmed)
    assert len(schedule_calls) >= 1
