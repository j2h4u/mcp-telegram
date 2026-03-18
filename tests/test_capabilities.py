from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import RPCError

from mcp_telegram.cache import EntityCache, TopicMetadataCache
from mcp_telegram.capabilities import (
    DialogTargetFailure,
    ExactTargetHints,
    ForumTopicFailure,
    HistoryReadExecution,
    ListTopicsExecution,
    NavigationFailure,
    ResolvedDialogTarget,
    ResolvedForumTopic,
    SearchExecution,
    execute_history_read_capability,
    execute_list_topics_capability,
    execute_search_messages_capability,
    fetch_messages_for_topic,
    load_forum_topic_capability,
    resolve_dialog_target,
)
from mcp_telegram.pagination import decode_history_navigation, decode_search_navigation, encode_search_navigation
from mcp_telegram.resolver import Candidates, NotFound, Resolved


async def test_resolve_dialog_target_returns_resolved_prefix_for_resolved_query(tmp_db_path) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))

    result = await resolve_dialog_target(
        cache=cache,
        query="backend",
        retry_tool="ListMessages",
        resolve_dialog=resolver,
    )

    assert isinstance(result, ResolvedDialogTarget)
    assert result.entity_id == 701
    assert result.display_name == "Backend Forum"
    assert result.resolve_prefix == '[resolved: "backend" → Backend Forum]\n'


async def test_resolve_dialog_target_returns_not_found_failure(tmp_db_path) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=NotFound(query="missing"))

    result = await resolve_dialog_target(
        cache=cache,
        query="missing",
        retry_tool="SearchMessages",
        resolve_dialog=resolver,
    )

    assert isinstance(result, DialogTargetFailure)
    assert result.kind == "not_found"
    assert 'Dialog "missing" was not found.' in result.text
    assert "SearchMessages" in result.text


async def test_resolve_dialog_target_returns_ambiguous_failure(tmp_db_path) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(
        return_value=Candidates(
            query="backend",
            matches=[
                {
                    "entity_id": 701,
                    "display_name": "Backend Forum",
                    "score": 97,
                    "username": None,
                    "entity_type": "group",
                },
                {
                    "entity_id": 702,
                    "display_name": "Backend Feedback",
                    "score": 95,
                    "username": None,
                    "entity_type": "group",
                },
            ],
        )
    )

    result = await resolve_dialog_target(
        cache=cache,
        query="backend",
        retry_tool="ListTopics",
        resolve_dialog=resolver,
    )

    assert isinstance(result, DialogTargetFailure)
    assert result.kind == "ambiguous"
    assert len(result.matches) == 2
    assert 'Dialog "backend" matched multiple dialogs.' in result.text
    assert 'id=701 name="Backend Forum" score=97 [group]' in result.text


async def test_resolve_dialog_target_accepts_exact_dialog_id_without_resolution(tmp_db_path) -> None:
    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    resolver = AsyncMock()

    result = await resolve_dialog_target(
        cache=cache,
        query=None,
        retry_tool="ListMessages",
        resolve_dialog=resolver,
        exact_dialog_id=701,
    )

    assert isinstance(result, ResolvedDialogTarget)
    assert result.entity_id == 701
    assert result.display_name == "Backend Forum"
    assert result.resolve_prefix == ""
    assert resolver.await_count == 0


async def test_load_forum_topic_capability_returns_resolved_topic(tmp_db_path, mock_client, make_mock_topic) -> None:
    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    topic_cache.upsert_topics(
        701,
        [
            make_mock_topic(topic_id=1, title="General", top_message_id=None, is_general=True),
            make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011),
        ],
    )

    result = await load_forum_topic_capability(
        mock_client,
        entity=701,
        dialog_id=701,
        dialog_name="Backend Forum",
        topic_cache=topic_cache,
        requested_topic="Release Notes",
        retry_tool="ListMessages",
    )

    assert isinstance(result, ResolvedForumTopic)
    assert result.display_name == "Release Notes"
    assert result.reply_to_message_id == 5011
    assert result.topic_catalog["choices"] == {1: "General", 11: "Release Notes"}


async def test_load_forum_topic_capability_uses_cached_exact_topic_id_without_catalog_load(
    tmp_db_path,
    mock_client,
    make_mock_topic,
) -> None:
    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    topic_cache.upsert_topics(
        701,
        [make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)],
    )
    load_topics = AsyncMock()
    refresh_topic = AsyncMock()

    result = await load_forum_topic_capability(
        mock_client,
        entity=701,
        dialog_id=701,
        dialog_name="Backend Forum",
        topic_cache=topic_cache,
        requested_topic=None,
        retry_tool="ListMessages",
        load_topics=load_topics,
        exact_topic_id=11,
        refresh_topic_by_id_fn=refresh_topic,
    )

    assert isinstance(result, ResolvedForumTopic)
    assert result.display_name == "Release Notes"
    assert result.reply_to_message_id == 5011
    assert load_topics.await_count == 0
    assert refresh_topic.await_count == 0


async def test_load_forum_topic_capability_returns_deleted_exact_topic_from_cache_tombstone(
    tmp_db_path,
    mock_client,
    make_deleted_topic,
) -> None:
    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    topic_cache.upsert_topics(
        701,
        [make_deleted_topic(topic_id=9, title="Deprecated Topic", top_message_id=5009)],
    )
    load_topics = AsyncMock()

    result = await load_forum_topic_capability(
        mock_client,
        entity=701,
        dialog_id=701,
        dialog_name="Backend Forum",
        topic_cache=topic_cache,
        requested_topic=None,
        retry_tool="ListMessages",
        load_topics=load_topics,
        exact_topic_id=9,
    )

    assert isinstance(result, ForumTopicFailure)
    assert result.kind == "deleted"
    assert 'Topic "Deprecated Topic" was deleted and can no longer be fetched.' in result.text
    assert load_topics.await_count == 0


async def test_load_forum_topic_capability_returns_deleted_failure(
    tmp_db_path,
    mock_client,
    make_deleted_topic,
    make_mock_topic,
) -> None:
    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    general_topic = make_mock_topic(topic_id=1, title="General", top_message_id=None, is_general=True)
    deleted_topic = make_deleted_topic(topic_id=9, title="Deprecated Topic", top_message_id=5009)
    topic_cache.upsert_topics(701, [general_topic, deleted_topic])

    result = await load_forum_topic_capability(
        mock_client,
        entity=701,
        dialog_id=701,
        dialog_name="Backend Forum",
        topic_cache=topic_cache,
        requested_topic="Deprecated Topic",
        retry_tool="ListMessages",
    )

    assert isinstance(result, ForumTopicFailure)
    assert result.kind == "deleted"
    assert 'Topic "Deprecated Topic" was deleted and can no longer be fetched.' in result.text


async def test_load_forum_topic_capability_returns_inaccessible_failure(
    tmp_db_path,
    mock_client,
    monkeypatch,
) -> None:
    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    load_topics = AsyncMock(side_effect=RPCError(request=None, message="CHAT_NOT_FORUM", code=400))
    monkeypatch.setattr("mcp_telegram.forum_topics.load_dialog_topics", load_topics)

    result = await load_forum_topic_capability(
        mock_client,
        entity=701,
        dialog_id=701,
        dialog_name="Backend Forum",
        topic_cache=topic_cache,
        requested_topic="Release Notes",
        retry_tool="ListMessages",
    )

    assert isinstance(result, ForumTopicFailure)
    assert result.kind == "inaccessible"
    assert 'Topic "Release Notes" could not be loaded because Telegram rejected topic access (CHAT_NOT_FORUM).' in result.text


async def test_load_forum_topic_capability_refreshes_exact_topic_id_on_cache_miss(
    tmp_db_path,
    mock_client,
    make_mock_topic,
) -> None:
    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    refreshed_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=6011)
    load_topics = AsyncMock()
    refresh_topic = AsyncMock(return_value=refreshed_topic)

    result = await load_forum_topic_capability(
        mock_client,
        entity=701,
        dialog_id=701,
        dialog_name="Backend Forum",
        topic_cache=topic_cache,
        requested_topic=None,
        retry_tool="ListMessages",
        load_topics=load_topics,
        exact_topic_id=11,
        refresh_topic_by_id_fn=refresh_topic,
    )

    assert isinstance(result, ResolvedForumTopic)
    assert result.display_name == "Release Notes"
    assert result.reply_to_message_id == 6011
    assert refresh_topic.await_count == 1
    assert load_topics.await_count == 0


async def test_fetch_messages_for_topic_refreshes_stale_anchor(
    tmp_db_path,
    mock_client,
    make_mock_message,
    make_mock_topic,
    monkeypatch,
) -> None:
    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    stale_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    refreshed_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=6011)
    topic_cache.upsert_topics(701, [stale_topic])
    message = make_mock_message(id=77, text="Refreshed topic message")
    fetch_topic_messages = AsyncMock(
        side_effect=[
            RPCError(
                request=None,
                message="TOPIC_ID_INVALID",
                code=400,
            ),
            [message],
        ]
    )

    refresh_topic = AsyncMock(return_value=refreshed_topic)
    monkeypatch.setattr("mcp_telegram.message_ops.refresh_topic_by_id", refresh_topic)
    monkeypatch.setattr("mcp_telegram.message_ops.fetch_topic_messages", fetch_topic_messages)

    fetched_messages, returned_topic, returned_iter_kwargs = await fetch_messages_for_topic(
        mock_client,
        entity_id=701,
        iter_kwargs={"entity": 701, "limit": 1, "reply_to": 5011},
        topic_metadata=stale_topic,
        topic_cache=topic_cache,
        allow_headerless_messages=False,
    )

    assert fetched_messages == [message]
    assert returned_topic["top_message_id"] == 6011
    assert returned_iter_kwargs["reply_to"] == 6011
    assert refresh_topic.await_count == 1
    assert fetch_topic_messages.await_count == 2


async def test_execute_list_topics_capability_returns_active_topics(
    tmp_db_path,
    mock_client,
    make_mock_topic,
) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))
    load_topics = AsyncMock(
        return_value={
            "choices": {1: "General", 11: "Release Notes"},
            "metadata_by_id": {
                1: make_mock_topic(topic_id=1, title="General", top_message_id=None, is_general=True),
                11: make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011),
            },
            "deleted_topics": {},
        }
    )

    result = await execute_list_topics_capability(
        mock_client,
        cache=cache,
        dialog_query="Backend",
        retry_tool="ListTopics",
        resolve_dialog=resolver,
        load_topics=load_topics,
    )

    assert isinstance(result, ListTopicsExecution)
    assert result.resolve_prefix == '[resolved: "Backend" → Backend Forum]\n'
    assert result.dialog_name == "Backend Forum"
    assert [topic["title"] for topic in result.active_topics] == ["General", "Release Notes"]


async def test_execute_history_read_capability_filters_topic_sender_locally_and_keeps_cursor(
    tmp_db_path,
    mock_client,
    make_mock_message,
    make_mock_topic,
) -> None:
    cache = EntityCache(tmp_db_path)
    cache.upsert(9001, "user", "Alice", None)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))
    load_topics = AsyncMock(
        return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {
                11: make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011),
            },
            "deleted_topics": {},
        }
    )

    alice_message = make_mock_message(id=30, text="Alice topic update", sender_id=9001, sender_name="Alice")
    bob_message = make_mock_message(id=20, text="Bob topic update", sender_id=9002, sender_name="Bob")
    fetch_topic_messages = AsyncMock(return_value=[alice_message, bob_message])
    mock_client.get_messages = AsyncMock(return_value=[])

    result = await execute_history_read_capability(
        mock_client,
        cache=cache,
        dialog_query="Backend Forum",
        limit=2,
        navigation=None,
        sender_query="Alice",
        topic_query="Release Notes",
        unread=False,
        retry_tool="ListMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
        load_topics=load_topics,
        fetch_topic_messages_fn=fetch_topic_messages,
        refresh_topic_by_id_fn=AsyncMock(),
    )

    assert isinstance(result, HistoryReadExecution)
    assert result.topic_name == "Release Notes"
    assert list(result.messages) == [alice_message]
    assert list(result.fetched_messages) == [alice_message, bob_message]
    assert result.navigation is not None


async def test_execute_history_read_capability_exposes_shared_navigation_for_topic_pages(
    tmp_db_path,
    mock_client,
    make_mock_message,
    make_mock_topic,
) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))
    load_topics = AsyncMock(
        return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {
                11: make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011),
            },
            "deleted_topics": {},
        }
    )
    newer = make_mock_message(id=30, text="Newest topic update")
    older = make_mock_message(id=20, text="Older topic update")
    fetch_topic_messages = AsyncMock(return_value=[newer, older])
    mock_client.get_messages = AsyncMock(return_value=[])

    result = await execute_history_read_capability(
        mock_client,
        cache=cache,
        dialog_query="Backend Forum",
        limit=2,
        navigation=None,
        sender_query=None,
        topic_query="Release Notes",
        unread=False,
        retry_tool="ListMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
        load_topics=load_topics,
        fetch_topic_messages_fn=fetch_topic_messages,
        refresh_topic_by_id_fn=AsyncMock(),
    )

    assert isinstance(result, HistoryReadExecution)
    assert result.navigation is not None
    assert result.navigation.kind == "history"
    assert decode_history_navigation(
        result.navigation.token,
        expected_dialog_id=701,
        expected_topic_id=11,
        expected_direction="newest",
    ) == 20


async def test_execute_history_read_capability_uses_exact_targets_without_resolution_or_catalog_load(
    tmp_db_path,
    mock_client,
    make_mock_message,
    make_mock_topic,
) -> None:
    cache = EntityCache(tmp_db_path)
    TopicMetadataCache(cache._conn).upsert_topics(
        701,
        [make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)],
    )
    resolver = AsyncMock()
    load_topics = AsyncMock()
    message = make_mock_message(id=30, text="Direct topic update")
    fetch_topic_messages = AsyncMock(return_value=[message])
    mock_client.get_messages = AsyncMock(return_value=[])

    result = await execute_history_read_capability(
        mock_client,
        cache=cache,
        dialog_query=None,
        limit=1,
        navigation=None,
        sender_query=None,
        topic_query=None,
        unread=False,
        retry_tool="ListMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
        load_topics=load_topics,
        fetch_topic_messages_fn=fetch_topic_messages,
        refresh_topic_by_id_fn=AsyncMock(),
        exact=ExactTargetHints(dialog_id=701, dialog_name="Backend Forum", topic_id=11),
    )

    assert isinstance(result, HistoryReadExecution)
    assert result.topic_name == "Release Notes"
    assert list(result.messages) == [message]
    assert resolver.await_count == 0
    assert load_topics.await_count == 0
    assert fetch_topic_messages.await_args.kwargs["iter_kwargs"]["reply_to"] == 5011


async def test_execute_history_read_capability_returns_navigation_failure(tmp_db_path, mock_client) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))

    result = await execute_history_read_capability(
        mock_client,
        cache=cache,
        dialog_query="Backend Forum",
        limit=2,
        navigation="BADINVALID==garbage",
        sender_query=None,
        topic_query=None,
        unread=False,
        retry_tool="ListMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
    )

    assert isinstance(result, NavigationFailure)
    assert result.kind == "invalid_navigation"
    assert "Navigation token is invalid:" in result.text


async def test_execute_history_read_capability_rejects_search_navigation_token(
    tmp_db_path,
    mock_client,
) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))

    result = await execute_history_read_capability(
        mock_client,
        cache=cache,
        dialog_query="Backend Forum",
        limit=2,
        navigation=encode_search_navigation(20, 701, "ship"),
        sender_query=None,
        topic_query=None,
        unread=False,
        retry_tool="ListMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
    )

    assert isinstance(result, NavigationFailure)
    assert result.kind == "invalid_navigation"
    assert "Navigation token is invalid:" in result.text
    assert "not history" in result.text
    assert "Action:" in result.text


async def test_execute_search_messages_capability_reuses_shared_enrichment(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))
    hit = make_mock_message(id=50, text="the hit", sender_id=9001, sender_name="Alice")
    context = make_mock_message(id=49, text="context", sender_id=9002, sender_name="Bob")

    async def _fake_iter_messages(*_args, **_kwargs):
        yield hit

    reaction_builder = AsyncMock(return_value={50: {"👍": ["Alice"]}})
    upsert_spy = MagicMock(wraps=cache.upsert)
    monkeypatch.setattr(cache, "upsert", upsert_spy)
    monkeypatch.setattr("mcp_telegram.capability_search._build_reaction_names_map", reaction_builder)
    mock_client.iter_messages = _fake_iter_messages
    mock_client.get_messages = AsyncMock(return_value=[context])

    result = await execute_search_messages_capability(
        mock_client,
        cache=cache,
        dialog_query="Backend Forum",
        query="hit",
        limit=1,
        navigation=None,
        retry_tool="SearchMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
    )

    assert isinstance(result, SearchExecution)
    assert list(result.hits) == [hit]
    assert result.context_messages_by_id == {49: context}
    assert result.reaction_names_map == {50: {"👍": ["Alice"]}}
    assert result.next_offset == 1
    sender_ids = {call.args[0] for call in upsert_spy.call_args_list}
    assert sender_ids.issuperset({9001, 9002})
    assert reaction_builder.await_args.kwargs["entity_id"] == 701
    assert reaction_builder.await_args.kwargs["messages"] == [hit]


async def test_execute_search_messages_capability_exposes_shared_navigation(
    tmp_db_path,
    mock_client,
    make_mock_message,
) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))
    hit = make_mock_message(id=50, text="the hit", sender_id=9001, sender_name="Alice")

    async def _fake_iter_messages(*_args, **_kwargs):
        yield hit

    mock_client.iter_messages = _fake_iter_messages
    mock_client.get_messages = AsyncMock(return_value=[])

    result = await execute_search_messages_capability(
        mock_client,
        cache=cache,
        dialog_query="Backend Forum",
        query="ship",
        limit=1,
        navigation=None,
        retry_tool="SearchMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
    )

    assert isinstance(result, SearchExecution)
    assert result.navigation is not None
    assert result.rendered_text.startswith("--- hit 1/1 ---\n")
    assert "[HIT]" in result.rendered_text
    assert decode_search_navigation(
        result.navigation.token,
        expected_dialog_id=701,
        expected_query="ship",
    ) == 1


async def test_execute_search_messages_capability_uses_exact_dialog_id_without_resolution(
    tmp_db_path,
    mock_client,
    make_mock_message,
) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock()
    hit = make_mock_message(id=50, text="the hit", sender_id=9001, sender_name="Alice")

    async def _fake_iter_messages(*_args, **_kwargs):
        yield hit

    mock_client.iter_messages = _fake_iter_messages
    mock_client.get_messages = AsyncMock(return_value=[])

    result = await execute_search_messages_capability(
        mock_client,
        cache=cache,
        dialog_query=None,
        query="ship",
        limit=1,
        navigation=None,
        retry_tool="SearchMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
        exact=ExactTargetHints(dialog_id=701, dialog_name="Backend Forum"),
    )

    assert isinstance(result, SearchExecution)
    assert result.dialog_name == "Backend Forum"
    assert list(result.hits) == [hit]
    assert "[HIT]" in result.rendered_text
    assert "the hit" in result.rendered_text
    assert resolver.await_count == 0


async def test_execute_search_messages_capability_rejects_query_mismatch_navigation_token(
    tmp_db_path,
    mock_client,
) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))

    result = await execute_search_messages_capability(
        mock_client,
        cache=cache,
        dialog_query="Backend Forum",
        query="ship",
        limit=1,
        retry_tool="SearchMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
        navigation=encode_search_navigation(5, 701, "deploy"),
    )

    assert isinstance(result, NavigationFailure)
    assert result.kind == "invalid_navigation"
    assert 'query "deploy", not "ship"' in result.text
    assert "Action:" in result.text


async def test_execute_search_messages_capability_rejects_dialog_mismatch_navigation_token(
    tmp_db_path,
    mock_client,
) -> None:
    cache = EntityCache(tmp_db_path)
    resolver = AsyncMock(return_value=Resolved(entity_id=701, display_name="Backend Forum"))

    result = await execute_search_messages_capability(
        mock_client,
        cache=cache,
        dialog_query="Backend Forum",
        query="ship",
        limit=1,
        retry_tool="SearchMessages",
        resolve_dialog=resolver,

        reaction_names_threshold=15,
        navigation=encode_search_navigation(5, 702, "ship"),
    )

    assert isinstance(result, NavigationFailure)
    assert result.kind == "invalid_navigation"
    assert "dialog 702, not 701" in result.text
    assert "Action:" in result.text


# ---------------------------------------------------------------------------
# Tests for allocate_message_budget_proportional
# ---------------------------------------------------------------------------


def test_allocate_budget_no_trim() -> None:
    """When total unread fits within limit, return unread_counts unchanged."""
    from mcp_telegram.capabilities import allocate_message_budget_proportional

    unread_counts = {1: 5, 2: 10, 3: 8}
    result = allocate_message_budget_proportional(unread_counts, limit=30)
    assert result == {1: 5, 2: 10, 3: 8}


def test_allocate_budget_proportional_trim() -> None:
    """When over limit, allocate min per chat, then proportional remainder."""
    from mcp_telegram.capabilities import allocate_message_budget_proportional

    unread_counts = {1: 20, 2: 30, 3: 40}  # total 90, limit 30
    result = allocate_message_budget_proportional(unread_counts, limit=30, min_per_chat=3)

    # Each gets 3 minimum: total reserved = 9, remaining = 21
    # Proportional: 1 gets 20/90 * 21 = 4.67 ≈ 4; 2 gets 7; 3 gets 9 (approx)
    # Min + proportion: 1→7, 2→10, 3→12 (or similar distribution)
    assert sum(result.values()) <= 30
    assert all(v >= 3 for v in result.values())
    assert result[1] <= 20  # Can't exceed unread count
    assert result[2] <= 30
    assert result[3] <= 40


def test_allocate_budget_min_per_chat_respected() -> None:
    """When over limit, every chat gets at least min_per_chat messages."""
    from mcp_telegram.capabilities import allocate_message_budget_proportional

    # Total unread (300) > limit (30) → proportional allocation path
    unread_counts = {1: 200, 2: 50, 3: 50}
    result = allocate_message_budget_proportional(unread_counts, limit=30, min_per_chat=3)

    assert sum(result.values()) <= 30
    for chat_id in unread_counts:
        assert result[chat_id] >= 3, f"chat {chat_id} got {result[chat_id]}, expected >= 3"


def test_allocate_budget_empty() -> None:
    """Empty input returns empty dict."""
    from mcp_telegram.capabilities import allocate_message_budget_proportional

    result = allocate_message_budget_proportional({}, limit=100)
    assert result == {}


def test_allocate_budget_single_chat() -> None:
    """Single chat allocation."""
    from mcp_telegram.capabilities import allocate_message_budget_proportional

    unread_counts = {42: 150}
    result = allocate_message_budget_proportional(unread_counts, limit=100)
    assert result[42] <= 100
    assert result[42] >= 3
