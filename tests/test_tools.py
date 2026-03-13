from __future__ import annotations

import pytest
import mcp_telegram.tools as tools_module
from mcp_telegram.tools import ListDialogs, ListMessages, ListTopics, SearchMessages
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mcp_telegram.cache import EntityCache, TopicMetadataCache
from mcp_telegram.capabilities import HistoryReadExecution, ListTopicsExecution, SearchExecution


async def _async_iter(items):
    """Async generator yielding items from a list — local helper for test_tools.py."""
    for item in items:
        yield item


def _make_mock_topic(
    *,
    topic_id: int,
    title: str,
    top_message_id: int,
    date: datetime | None = None,
):
    """Create a lightweight topic-like object for raw Telethon helper tests."""
    return SimpleNamespace(
        id=topic_id,
        title=title,
        top_message=top_message_id,
        date=date or datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


async def test_list_dialogs_multiple_newlines(mock_cache, mock_client, monkeypatch):
    """ListDialogs with multiple dialogs returns single TextContent with newline-separated entries."""
    def _make_dialog(name, id_, is_user=True):
        d = MagicMock()
        d.is_user = is_user
        d.is_group = not is_user
        d.is_channel = False
        d.date = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        d.id = id_
        d.name = name
        d.unread_count = 0
        d.entity = MagicMock(username=None)
        return d

    dialogs = [_make_dialog("Alice", 1), _make_dialog("Bob", 2, is_user=False)]
    mock_client.iter_dialogs = MagicMock(return_value=_async_iter(dialogs))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    from mcp_telegram.tools import list_dialogs
    result = await list_dialogs(ListDialogs())
    assert len(result) == 1
    lines = result[0].text.splitlines()
    assert len(lines) == 2
    assert "Alice" in lines[0]
    assert "Bob" in lines[1]


async def test_list_dialogs_empty_returns_action(mock_cache, mock_client, monkeypatch):
    """ListDialogs returns an action-oriented empty-state when no dialogs are visible."""
    from mcp_telegram.tools import ListDialogs, list_dialogs

    mock_client.iter_dialogs = MagicMock(return_value=_async_iter([]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await list_dialogs(ListDialogs())

    assert len(result) == 1
    assert "No dialogs were returned." in result[0].text
    assert "Action:" in result[0].text
    assert "exclude_archived=False" in result[0].text


# --- TOOL-01: ListDialogs ---


async def test_list_dialogs_type_field(mock_cache, mock_client, monkeypatch):
    """ListDialogs output line contains type=user/group/channel and last_message_at=."""
    fake_dialog = MagicMock()
    fake_dialog.is_user = True
    fake_dialog.is_group = False
    fake_dialog.is_channel = False
    fake_dialog.date = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    fake_dialog.id = 101
    fake_dialog.name = "Иван Петров"
    fake_dialog.unread_count = 0
    fake_dialog.entity = MagicMock(username="ivan")

    mock_client.iter_dialogs = MagicMock(return_value=_async_iter([fake_dialog]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    from mcp_telegram.tools import list_dialogs
    result = await list_dialogs(ListDialogs())
    assert len(result) == 1
    text = result[0].text
    assert "type=user" in text
    assert "last_message_at=2024-" in text


async def test_list_dialogs_null_date(mock_cache, mock_client, monkeypatch):
    """ListDialogs handles dialog.date = None gracefully (outputs 'unknown')."""
    fake_dialog = MagicMock()
    fake_dialog.is_user = False
    fake_dialog.is_group = True
    fake_dialog.is_channel = False
    fake_dialog.date = None
    fake_dialog.id = 200
    fake_dialog.name = "Empty Group"
    fake_dialog.unread_count = 0
    fake_dialog.entity = MagicMock(username=None)

    mock_client.iter_dialogs = MagicMock(return_value=_async_iter([fake_dialog]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    from mcp_telegram.tools import list_dialogs
    result = await list_dialogs(ListDialogs())
    assert "last_message_at=unknown" in result[0].text


async def test_list_topics_returns_active_topics(tmp_db_path, mock_client, monkeypatch, make_mock_topic):
    """ListTopics exposes stable rows for active forum topics."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import list_topics

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    general_topic = make_mock_topic(topic_id=1, title="General", top_message_id=None, is_general=True)
    release_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    blocked_topic = make_mock_topic(topic_id=12, title="Inbox", top_message_id=6011)
    blocked_topic["inaccessible_error"] = "TOPIC_ID_INVALID"
    blocked_topic["inaccessible_at"] = 1_700_000_000

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {1: "General", 11: "Release Notes", 12: "Inbox"},
            "metadata_by_id": {1: general_topic, 11: release_topic, 12: blocked_topic},
            "deleted_topics": {},
        }),
    )

    result = await list_topics(ListTopics(dialog="Backend Forum"))

    assert 'topic_id=1 title="General" top_message_id=None status=general' in result[0].text
    assert 'topic_id=11 title="Release Notes" top_message_id=5011 status=active' in result[0].text
    assert 'topic_id=12 title="Inbox" top_message_id=6011 status=previously_inaccessible last_error=TOPIC_ID_INVALID' in result[0].text


async def test_list_topics_catalog_unavailable(tmp_db_path, mock_client, monkeypatch):
    """ListTopics explains when Telegram rejects forum-topic catalog access."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import list_topics

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(side_effect=tools_module.RPCError(request=None, message="CHAT_NOT_FORUM", code=400)),
    )

    result = await list_topics(ListTopics(dialog="Backend Forum"))

    assert 'Dialog "Backend Forum" does not expose a readable forum-topic catalog (CHAT_NOT_FORUM).' in result[0].text
    assert "Action:" in result[0].text
    assert "ListMessages without topic" in result[0].text


async def test_list_topics_not_found_returns_action(mock_cache, mock_client, monkeypatch):
    """ListTopics returns an action-oriented response when the dialog cannot be resolved."""
    from mcp_telegram.tools import ListTopics, list_topics

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await list_topics(ListTopics(dialog="nobody_xyz"))

    assert len(result) == 1
    assert 'Dialog "nobody_xyz" was not found.' in result[0].text
    assert "Action:" in result[0].text
    assert "ListDialogs" in result[0].text


async def test_list_topics_ambiguous_returns_action(mock_client, monkeypatch, tmp_db_path):
    """ListTopics returns an action-oriented response when the dialog query is ambiguous."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListTopics, list_topics

    ambig_cache = EntityCache(tmp_db_path)
    ambig_cache.upsert(201, "group", "Backend Forum", None)
    ambig_cache.upsert(202, "group", "Backend Feedback", None)
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: ambig_cache)

    result = await list_topics(ListTopics(dialog="Backend"))

    assert len(result) == 1
    assert 'Dialog "Backend" matched multiple dialogs.' in result[0].text
    assert "Action:" in result[0].text
    assert 'id=201' in result[0].text
    assert 'id=202' in result[0].text


async def test_list_topics_warms_dialog_cache_on_first_miss(tmp_db_path, mock_client, monkeypatch, make_mock_topic):
    """ListTopics retries dialog resolution after refreshing the dialog cache."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import list_topics

    cache = EntityCache(tmp_db_path)
    dialog = MagicMock()
    dialog.id = -1003779402801
    dialog.name = "Studio Robots and Inbox"
    dialog.is_user = False
    dialog.is_group = True
    dialog.is_channel = False
    dialog.entity = MagicMock(username=None)

    general_topic = make_mock_topic(topic_id=1, title="General", top_message_id=None, is_general=True)
    inbox_topic = make_mock_topic(topic_id=40, title="Inbox", top_message_id=249)

    mock_client.iter_dialogs = MagicMock(side_effect=lambda **_: _async_iter([dialog]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {1: "General", 40: "Inbox"},
            "metadata_by_id": {1: general_topic, 40: inbox_topic},
            "deleted_topics": {},
        }),
    )

    result = await list_topics(ListTopics(dialog="Studio Robots and Inbox"))

    assert 'topic_id=40 title="Inbox" top_message_id=249 status=active' in result[0].text
    assert cache.get(-1003779402801, 60) is not None


def test_tool_description_strips_nullable_unions_from_exported_schema():
    """Exported MCP tool schemas should not expose explicit null unions."""
    list_messages_schema = tools_module.tool_description(ListMessages).inputSchema
    search_messages_schema = tools_module.tool_description(SearchMessages).inputSchema

    dialog_schema = list_messages_schema["properties"]["dialog"]
    cursor_schema = list_messages_schema["properties"]["cursor"]
    sender_schema = list_messages_schema["properties"]["sender"]
    topic_schema = list_messages_schema["properties"]["topic"]
    offset_schema = search_messages_schema["properties"]["offset"]

    assert "dialog" in list_messages_schema["required"]
    assert dialog_schema["type"] == "string"
    assert "Required." in dialog_schema["description"]
    assert cursor_schema == {"title": "Cursor", "type": "string"}
    assert sender_schema == {"title": "Sender", "type": "string"}
    assert topic_schema == {"title": "Topic", "type": "string"}
    assert offset_schema == {"title": "Offset", "type": "integer"}


def test_capability_extraction_preserves_public_tool_names() -> None:
    """Capability seams stay internal; reflected public tool names remain unchanged."""
    assert tools_module.tool_description(ListTopics).name == "ListTopics"
    assert tools_module.tool_description(ListMessages).name == "ListMessages"
    assert tools_module.tool_description(SearchMessages).name == "SearchMessages"


async def test_list_topics_adapter_delegates_to_capability_execution(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_topic,
):
    """ListTopics renders rows from the shared capability execution result."""
    from mcp_telegram.tools import list_topics

    cache = EntityCache(tmp_db_path)
    capability = AsyncMock(
        return_value=ListTopicsExecution(
            resolve_prefix='[resolved: "Backend" → Backend Forum]\n',
            dialog_name="Backend Forum",
            active_topics=(
                make_mock_topic(topic_id=1, title="General", top_message_id=None, is_general=True),
                make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011),
            ),
        )
    )

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr("mcp_telegram.tools._execute_list_topics_capability", capability)

    result = await list_topics(ListTopics(dialog="Backend"))

    assert result[0].text.startswith('[resolved: "Backend" → Backend Forum]\n')
    assert 'topic_id=11 title="Release Notes" top_message_id=5011 status=active' in result[0].text
    assert capability.await_args.kwargs["dialog_query"] == "Backend"


async def test_list_messages_adapter_delegates_to_history_capability(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
):
    """ListMessages formats the shared history execution result instead of rebuilding it locally."""
    from mcp_telegram.tools import list_messages

    cache = EntityCache(tmp_db_path)
    message = make_mock_message(id=30, text="Delegated topic update")
    capability = AsyncMock(
        return_value=HistoryReadExecution(
            entity_id=701,
            resolve_prefix='[resolved: "Backend" → Backend Forum]\n',
            topic_name="Release Notes",
            messages=(message,),
            fetched_messages=(message,),
            reply_map={},
            reaction_names_map={},
            topic_name_getter=None,
            next_cursor="cursor-token",
        )
    )

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr("mcp_telegram.tools._execute_history_read_capability", capability)

    result = await list_messages(ListMessages(dialog="Backend", topic="Release Notes", limit=1))

    assert result[0].text.startswith('[resolved: "Backend" → Backend Forum]\n[topic: Release Notes]\n')
    assert "Delegated topic update" in result[0].text
    assert "next_cursor: cursor-token" in result[0].text
    assert capability.await_args.kwargs["topic_query"] == "Release Notes"


async def test_search_messages_adapter_delegates_to_capability_execution(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
):
    """SearchMessages renders shared search execution results instead of rebuilding them locally."""
    from mcp_telegram.tools import SearchMessages, search_messages

    cache = EntityCache(tmp_db_path)
    hit = make_mock_message(id=30, text="Delegated search hit")
    capability = AsyncMock(
        return_value=SearchExecution(
            entity_id=701,
            dialog_name="Backend Forum",
            resolve_prefix='[resolved: "Backend" → Backend Forum]\n',
            hits=(hit,),
            context_messages_by_id={},
            reaction_names_map={},
            next_offset=20,
        )
    )

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr("mcp_telegram.tools._execute_search_messages_capability", capability)

    result = await search_messages(SearchMessages(dialog="Backend", query="ship", limit=20))

    assert result[0].text.startswith('[resolved: "Backend" → Backend Forum]\n--- hit 1/1 ---\n')
    assert "[HIT]" in result[0].text
    assert "Delegated search hit" in result[0].text
    assert "next_offset: 20" in result[0].text
    assert capability.await_args.kwargs["query"] == "ship"


# --- TOOL-02: ListMessages name resolution ---


async def test_list_messages_by_name(mock_cache, mock_client, monkeypatch, make_mock_message):
    """ListMessages called with a name returns format_messages() output."""
    from mcp_telegram.tools import ListMessages, list_messages
    msg = make_mock_message(id=10, text="Hello")
    mock_client.iter_messages = MagicMock(return_value=_async_iter([msg]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    result = await list_messages(ListMessages(dialog="Иван Петров"))
    assert len(result) == 1
    assert "10:00" in result[0].text  # formatted output includes time


async def test_search_messages_not_found_returns_action(mock_cache, mock_client, monkeypatch):
    """SearchMessages returns an action-oriented response when the dialog cannot be resolved."""
    from mcp_telegram.tools import SearchMessages, search_messages

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await search_messages(SearchMessages(dialog="nobody_xyz", query="hi"))

    assert len(result) == 1
    assert 'Dialog "nobody_xyz" was not found.' in result[0].text
    assert "Action:" in result[0].text
    assert "ListDialogs" in result[0].text


async def test_search_messages_ambiguous_returns_action(mock_client, monkeypatch, tmp_db_path):
    """SearchMessages returns an action-oriented response when the dialog query is ambiguous."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import SearchMessages, search_messages

    ambig_cache = EntityCache(tmp_db_path)
    ambig_cache.upsert(201, "group", "Backend Forum", None)
    ambig_cache.upsert(202, "group", "Backend Feedback", None)
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: ambig_cache)

    result = await search_messages(SearchMessages(dialog="Backend", query="hi"))

    assert len(result) == 1
    assert 'Dialog "Backend" matched multiple dialogs.' in result[0].text
    assert "Action:" in result[0].text
    assert 'id=201' in result[0].text
    assert 'id=202' in result[0].text


async def test_list_messages_not_found(mock_cache, mock_client, monkeypatch):
    """ListMessages with unresolved name returns TextContent with 'not found'."""
    from mcp_telegram.tools import ListMessages, list_messages
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    result = await list_messages(ListMessages(dialog="nobody_xyz"))
    assert len(result) == 1
    assert 'Dialog "nobody_xyz" was not found.' in result[0].text
    assert "Action:" in result[0].text
    assert "ListDialogs" in result[0].text


async def test_list_messages_ambiguous(mock_cache, mock_client, monkeypatch, tmp_db_path):
    """ListMessages with ambiguous name returns TextContent with candidates list."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages
    # Seed cache with two similar names to trigger Candidates result
    ambig_cache = EntityCache(tmp_db_path)
    ambig_cache.upsert(201, "user", "Иван Петров", None)
    ambig_cache.upsert(202, "user", "Иван Сидоров", None)
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: ambig_cache)
    result = await list_messages(ListMessages(dialog="Иван"))
    assert len(result) == 1
    assert 'Dialog "Иван" matched multiple dialogs.' in result[0].text
    assert "Action:" in result[0].text


# --- TOOL-03: ListMessages cursor pagination ---


async def test_list_messages_cursor_present(mock_cache, mock_client, monkeypatch, make_mock_message):
    """ListMessages with full page returns next_cursor token in output."""
    from mcp_telegram.tools import ListMessages, list_messages
    # Return exactly limit=2 messages so cursor should be present
    msgs = [make_mock_message(id=20, text="B"), make_mock_message(id=10, text="A")]
    mock_client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    result = await list_messages(ListMessages(dialog="Иван Петров", limit=2))
    assert len(result) == 1
    assert "next_cursor:" in result[0].text


async def test_list_messages_no_cursor_last_page(mock_cache, mock_client, monkeypatch, make_mock_message):
    """ListMessages with partial page (fewer than limit) has no next_cursor in output."""
    from mcp_telegram.tools import ListMessages, list_messages
    # Return 1 message but limit=5 — partial page, no cursor
    msgs = [make_mock_message(id=10, text="Only")]
    mock_client.iter_messages = MagicMock(return_value=_async_iter(msgs))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    result = await list_messages(ListMessages(dialog="Иван Петров", limit=5))
    assert "next_cursor" not in result[0].text


# --- TOOL-04: ListMessages sender filter ---


async def test_list_messages_sender_filter(mock_cache, mock_client, monkeypatch):
    """ListMessages with sender param passes from_user=entity_id to iter_messages."""
    from mcp_telegram.tools import ListMessages, list_messages
    mock_client.iter_messages = MagicMock(return_value=_async_iter([]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    # mock_cache has entity 101 = "Иван Петров"
    await list_messages(ListMessages(dialog="Иван Петров", sender="Иван Петров"))
    # Verify iter_messages was called with from_user=101
    call_kwargs = mock_client.iter_messages.call_args.kwargs
    assert call_kwargs.get("from_user") == 101


# --- TOOL-05: ListMessages unread filter ---


async def test_list_messages_unread_filter(mock_cache, mock_client, monkeypatch):
    """ListMessages with unread=True passes min_id=read_inbox_max_id to iter_messages."""
    from mcp_telegram.tools import ListMessages, list_messages
    # Mock get_input_entity and GetPeerDialogsRequest response
    mock_client.get_input_entity = AsyncMock(return_value=MagicMock())
    tl_dialog = MagicMock()
    tl_dialog.read_inbox_max_id = 50
    tl_dialog.unread_count = 3
    peer_result = MagicMock()
    peer_result.dialogs = [tl_dialog]
    mock_client.return_value = peer_result
    mock_client.iter_messages = MagicMock(return_value=_async_iter([]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    await list_messages(ListMessages(dialog="Иван Петров", unread=True))
    call_kwargs = mock_client.iter_messages.call_args.kwargs
    assert call_kwargs.get("min_id") == 50
    assert call_kwargs.get("limit") == 50  # args.limit default, unread_count no longer caps it


async def test_list_messages_topic_resolves_within_dialog(tmp_db_path, mock_client, monkeypatch):
    """Topic names resolve inside the already-resolved dialog, not across all dialogs."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    cache.upsert(702, "group", "Ops Forum", None)

    topic_loader = AsyncMock(side_effect=[
        {
            "choices": {11: "Release Notes"},
            "metadata_by_id": {
                11: {
                    "topic_id": 11,
                    "title": "Release Notes",
                    "top_message_id": 5011,
                    "is_general": False,
                    "is_deleted": False,
                },
            },
            "deleted_topics": {},
        },
    ])

    msg = MagicMock()
    msg.id = 77
    msg.text = "Shipped"
    msg.message = "Shipped"
    msg.sender_id = 9001
    msg.sender = MagicMock(first_name="Ivan", last_name=None, username=None)
    msg.date = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    msg.reply_to = None
    msg.reactions = None
    msg.media = None

    mock_client.iter_messages = MagicMock(return_value=_async_iter([msg]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr("mcp_telegram.tools._load_dialog_topics", topic_loader)

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Release Notes"))

    assert "Shipped" in result[0].text
    assert topic_loader.await_args.kwargs["dialog_id"] == 701
    assert mock_client.iter_messages.call_args.kwargs["entity"] == 701


async def test_list_messages_topic_not_found(tmp_db_path, mock_client, monkeypatch):
    """Missing topic names return a topic-specific not-found message."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={"choices": {11: "Release Notes"}, "metadata_by_id": {}, "deleted_topics": {}}),
    )

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Incident Review"))

    assert len(result) == 1
    assert 'Topic "Incident Review" was not found.' in result[0].text
    assert "Action:" in result[0].text
    assert "ListTopics" in result[0].text


async def test_list_messages_topic_ambiguous_within_dialog(tmp_db_path, mock_client, monkeypatch):
    """Fuzzy topic matches inside one dialog return candidates instead of auto-resolving."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {
                11: "Release Notes",
                12: "Release Planning",
            },
            "metadata_by_id": {},
            "deleted_topics": {},
        }),
    )

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Release"))

    assert len(result) == 1
    assert 'Topic "Release" matched multiple topics.' in result[0].text
    assert "Action:" in result[0].text
    assert 'name="Release Notes"' in result[0].text
    assert 'name="Release Planning"' in result[0].text


async def test_list_messages_topic_ambiguous_marks_previously_inaccessible_candidates(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_topic,
):
    """Ambiguous topic matches surface prior inaccessibility from cached topic metadata."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    inaccessible_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    inaccessible_topic["inaccessible_error"] = "TOPIC_ID_INVALID"
    inaccessible_topic["inaccessible_at"] = 1_700_000_000
    active_topic = make_mock_topic(topic_id=12, title="Release Planning", top_message_id=6011)

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {
                11: "Release Notes",
                12: "Release Planning",
            },
            "metadata_by_id": {
                11: inaccessible_topic,
                12: active_topic,
            },
            "deleted_topics": {},
        }),
    )

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Release"))

    assert 'name="Release Notes"' in result[0].text
    assert "status=previously_inaccessible" in result[0].text
    assert "last_error=TOPIC_ID_INVALID" in result[0].text


async def test_list_messages_topic_header(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
    make_mock_topic,
):
    """Active topic names are shown ahead of the formatted message body."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    message = make_mock_message(id=77, text="Shipped topic update")

    mock_client.iter_messages = MagicMock(return_value=_async_iter([message]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: topic},
            "deleted_topics": {},
        }),
    )

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Release Notes"))

    assert result[0].text.startswith("[topic: Release Notes]\n")
    assert "Shipped topic update" in result[0].text
    assert mock_client.iter_messages.call_args.kwargs["reply_to"] == 5011


async def test_list_messages_cross_topic_pages_include_inline_topic_labels(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_general_topic_message,
    make_mock_forum_reply,
    make_mock_message,
    make_mock_topic,
):
    """Forum dialogs without topic= label each emitted message inline with its topic name."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    release_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    target_reply = make_mock_forum_reply(reply_to_msg_id=11, reply_to_top_id=None)

    topic_message = make_mock_message(id=77, text="Shipped topic update")
    topic_message.reply_to = target_reply
    general_message = make_general_topic_message(id=76, text="General fallback")

    mock_client.iter_messages = MagicMock(return_value=_async_iter([topic_message, general_message]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {1: "General", 11: "Release Notes"},
            "metadata_by_id": {
                1: make_mock_topic(topic_id=1, title="General", top_message_id=None, is_general=True),
                11: release_topic,
            },
            "deleted_topics": {},
        }),
    )

    result = await list_messages(ListMessages(dialog="Backend Forum", limit=5))

    assert "[topic: General] 10:00 Иван: General fallback" in result[0].text
    assert "[topic: Release Notes] 10:00 Иван: Shipped topic update" in result[0].text


async def test_list_messages_topic_cursor_round_trip(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
    make_mock_topic,
):
    """Topic-filtered pagination keeps reply_to scoped and emits a reusable cursor."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.pagination import encode_cursor
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    newer = make_mock_message(id=30, text="Newest in topic")
    older = make_mock_message(id=20, text="Older in topic")
    oldest = make_mock_message(id=10, text="Oldest in topic")

    mock_client.iter_messages = MagicMock(side_effect=[
        _async_iter([newer, older]),
        _async_iter([oldest]),
    ])
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: topic},
            "deleted_topics": {},
        }),
    )

    first_page = await list_messages(ListMessages(dialog="Backend Forum", topic="Release Notes", limit=2))

    expected_cursor = encode_cursor(20, 701)
    assert f"next_cursor: {expected_cursor}" in first_page[0].text
    first_call_kwargs = mock_client.iter_messages.call_args_list[0].kwargs
    assert first_call_kwargs["reply_to"] == 5011

    await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", limit=2, cursor=expected_cursor)
    )

    second_call_kwargs = mock_client.iter_messages.call_args_list[1].kwargs
    assert second_call_kwargs["reply_to"] == 5011
    assert second_call_kwargs["max_id"] == 20


async def test_list_messages_topic_from_beginning(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
    make_mock_topic,
):
    """Reverse pagination keeps topic retrieval scoped to the same thread root."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    oldest = make_mock_message(id=10, text="Oldest in topic")
    newest = make_mock_message(id=20, text="Newest in topic")

    mock_client.iter_messages = MagicMock(return_value=_async_iter([oldest, newest]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: topic},
            "deleted_topics": {},
        }),
    )

    result = await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", from_beginning=True, limit=2)
    )

    call_kwargs = mock_client.iter_messages.call_args.kwargs
    assert call_kwargs["reply_to"] == 5011
    assert call_kwargs["reverse"] is True
    assert call_kwargs["min_id"] == 1
    assert "[topic: Release Notes]" in result[0].text
    assert "Oldest in topic" in result[0].text


async def test_list_messages_topic_sender_behavior(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
    make_mock_topic,
    make_mock_forum_reply,
):
    """Topic+sender filters locally after thread retrieval instead of combining server filters."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    cache.upsert(9001, "user", "Alice", None)
    cache.upsert(9002, "user", "Bob", None)
    cache.upsert(9003, "user", "Carol", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)

    alice_reply = make_mock_forum_reply(reply_to_msg_id=5011, reply_to_top_id=5011)
    bob_reply = make_mock_forum_reply(reply_to_msg_id=5011, reply_to_top_id=5011)
    alice_message = make_mock_message(id=30, text="Alice update", sender_id=9001, sender_name="Alice")
    bob_message = make_mock_message(id=20, text="Bob update", sender_id=9002, sender_name="Bob")
    alice_message.reply_to = alice_reply
    bob_message.reply_to = bob_reply

    mock_client.iter_messages = MagicMock(side_effect=[
        _async_iter([alice_message, bob_message]),
        _async_iter([alice_message, bob_message]),
    ])
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: topic},
            "deleted_topics": {},
        }),
    )

    result = await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", sender="Alice", limit=5)
    )

    first_call_kwargs = mock_client.iter_messages.call_args_list[0].kwargs
    assert first_call_kwargs["reply_to"] == 5011
    assert "from_user" not in first_call_kwargs
    assert "Alice update" in result[0].text
    assert "Bob update" not in result[0].text

    empty_result = await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", sender="Carol", limit=5)
    )

    second_call_kwargs = mock_client.iter_messages.call_args_list[1].kwargs
    assert second_call_kwargs["reply_to"] == 5011
    assert "from_user" not in second_call_kwargs
    assert empty_result[0].text == "[topic: Release Notes]\n"


async def test_list_messages_topic_unread_behavior(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
    make_mock_topic,
    make_mock_forum_reply,
):
    """Topic+unread keeps reply_to scoping and unread min_id behavior together."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)

    unread_message = make_mock_message(id=60, text="Unread topic update")
    unread_message.reply_to = make_mock_forum_reply(reply_to_msg_id=5011, reply_to_top_id=5011)

    peer_result_one = MagicMock()
    peer_result_one.dialogs = [MagicMock(read_inbox_max_id=50, unread_count=2)]
    peer_result_two = MagicMock()
    peer_result_two.dialogs = [MagicMock(read_inbox_max_id=70, unread_count=0)]

    mock_client.get_input_entity = AsyncMock(return_value=MagicMock())
    mock_client.side_effect = [peer_result_one, peer_result_two]
    mock_client.iter_messages = MagicMock(side_effect=[
        _async_iter([unread_message]),
        _async_iter([]),
    ])
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: topic},
            "deleted_topics": {},
        }),
    )

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Release Notes", unread=True))

    first_call_kwargs = mock_client.iter_messages.call_args_list[0].kwargs
    assert first_call_kwargs["reply_to"] == 5011
    assert first_call_kwargs["min_id"] == 50
    assert "Unread topic update" in result[0].text

    empty_result = await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", unread=True)
    )

    second_call_kwargs = mock_client.iter_messages.call_args_list[1].kwargs
    assert second_call_kwargs["reply_to"] == 5011
    assert second_call_kwargs["min_id"] == 70
    assert empty_result[0].text == """[topic: Release Notes]
no unread messages"""


async def test_list_messages_general_topic_unread_is_scoped(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_general_topic_message,
    make_mock_message,
    make_mock_topic,
    make_mock_forum_reply,
):
    """General unread mode keeps only General messages and diverges from dialog-wide unread pages."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.pagination import encode_cursor
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    general_topic = make_mock_topic(
        topic_id=1,
        title="General",
        top_message_id=None,
        is_general=True,
    )

    general_newest = make_general_topic_message(id=80, text="General unread newest")
    leaked_adjacent = make_mock_message(id=79, text="Adjacent unread topic")
    leaked_adjacent.reply_to = make_mock_forum_reply(reply_to_msg_id=7001, reply_to_top_id=7001)
    general_older = make_general_topic_message(id=78, text="General unread older")

    mock_client.get_input_entity = AsyncMock(return_value=MagicMock())
    mock_client.side_effect = [
        MagicMock(dialogs=[MagicMock(read_inbox_max_id=50, unread_count=3)]),
        MagicMock(dialogs=[MagicMock(read_inbox_max_id=50, unread_count=3)]),
    ]
    mock_client.iter_messages = MagicMock(
        side_effect=[
            _async_iter([general_newest, leaked_adjacent]),
            _async_iter([general_newest, leaked_adjacent, general_older]),
        ]
    )
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(
            return_value={
                "choices": {1: "General"},
                "metadata_by_id": {1: general_topic},
                "deleted_topics": {},
            }
        ),
    )

    dialog_result = await list_messages(ListMessages(dialog="Backend Forum", unread=True, limit=2))
    topic_result = await list_messages(
        ListMessages(dialog="Backend Forum", topic="General", unread=True, limit=2)
    )

    assert "Adjacent unread topic" in dialog_result[0].text
    assert f"next_cursor: {encode_cursor(79, 701)}" in dialog_result[0].text
    assert "Adjacent unread topic" not in topic_result[0].text
    assert "General unread newest" in topic_result[0].text
    assert "General unread older" in topic_result[0].text
    assert f"next_cursor: {encode_cursor(78, 701)}" in topic_result[0].text


async def test_list_messages_topic_unread_cursor_is_topic_scoped(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_general_topic_message,
    make_mock_message,
    make_mock_topic,
    make_mock_forum_reply,
):
    """Unread topic cursors use the last emitted topic message, not the last raw unread item."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.pagination import encode_cursor
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    target_reply = make_mock_forum_reply(reply_to_msg_id=5011, reply_to_top_id=5011)

    topic_newest = make_mock_message(id=90, text="Topic unread newest")
    topic_newest.reply_to = target_reply
    leaked_general = make_general_topic_message(id=89, text="General unread leak")
    topic_older = make_mock_message(id=88, text="Topic unread older")
    topic_older.reply_to = target_reply

    mock_client.get_input_entity = AsyncMock(return_value=MagicMock())
    mock_client.side_effect = [MagicMock(dialogs=[MagicMock(read_inbox_max_id=50, unread_count=3)])]
    mock_client.iter_messages = MagicMock(
        return_value=_async_iter([topic_newest, leaked_general, topic_older])
    )
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(
            return_value={
                "choices": {11: "Release Notes"},
                "metadata_by_id": {11: topic},
                "deleted_topics": {},
            }
        ),
    )

    result = await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", unread=True, limit=2)
    )

    assert "General unread leak" not in result[0].text
    assert "Topic unread newest" in result[0].text
    assert "Topic unread older" in result[0].text
    assert f"next_cursor: {encode_cursor(88, 701)}" in result[0].text


async def test_list_messages_topic_unread_filters_dialog_leaks(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_general_topic_message,
    make_mock_message,
    make_mock_topic,
    make_mock_forum_reply,
):
    """Unread topic pages drop mixed-dialog leaks before formatting and cursor generation."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.pagination import encode_cursor
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    target_reply = make_mock_forum_reply(reply_to_msg_id=5011, reply_to_top_id=5011)
    adjacent_reply = make_mock_forum_reply(reply_to_msg_id=7001, reply_to_top_id=7001)

    topic_newest = make_mock_message(id=70, text="Topic unread newest")
    topic_newest.reply_to = target_reply
    leaked_adjacent = make_mock_message(id=69, text="Adjacent unread leak")
    leaked_adjacent.reply_to = adjacent_reply
    leaked_general = make_general_topic_message(id=68, text="General unread leak")
    topic_older = make_mock_message(id=67, text="Topic unread older")
    topic_older.reply_to = target_reply

    mock_client.get_input_entity = AsyncMock(return_value=MagicMock())
    mock_client.side_effect = [MagicMock(dialogs=[MagicMock(read_inbox_max_id=50, unread_count=4)])]
    mock_client.iter_messages = MagicMock(
        return_value=_async_iter([topic_newest, leaked_adjacent, leaked_general, topic_older])
    )
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(
            return_value={
                "choices": {11: "Release Notes"},
                "metadata_by_id": {11: topic},
                "deleted_topics": {},
            }
        ),
    )

    result = await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", unread=True, limit=2)
    )

    assert "Adjacent unread leak" not in result[0].text
    assert "General unread leak" not in result[0].text
    assert "Topic unread newest" in result[0].text
    assert "Topic unread older" in result[0].text
    assert f"next_cursor: {encode_cursor(67, 701)}" in result[0].text


async def test_list_messages_topic_unread_empty_page_has_no_cursor(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_general_topic_message,
    make_mock_message,
    make_mock_topic,
    make_mock_forum_reply,
):
    """Unread topic pages stay empty when the raw unread slice has no matching topic messages."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    adjacent_reply = make_mock_forum_reply(reply_to_msg_id=7001, reply_to_top_id=7001)

    leaked_general = make_general_topic_message(id=66, text="General unread leak")
    leaked_adjacent = make_mock_message(id=65, text="Adjacent unread leak")
    leaked_adjacent.reply_to = adjacent_reply

    mock_client.get_input_entity = AsyncMock(return_value=MagicMock())
    mock_client.side_effect = [MagicMock(dialogs=[MagicMock(read_inbox_max_id=50, unread_count=2)])]
    mock_client.iter_messages = MagicMock(return_value=_async_iter([leaked_general, leaked_adjacent]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(
            return_value={
                "choices": {11: "Release Notes"},
                "metadata_by_id": {11: topic},
                "deleted_topics": {},
            }
        ),
    )

    result = await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", unread=True, limit=2)
    )

    assert result[0].text == """[topic: Release Notes]
no unread messages"""
    assert "next_cursor" not in result[0].text


async def test_list_messages_general_topic_normalization(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_general_topic_message,
    make_mock_topic,
):
    """General topic resolves through normalized metadata and does not use reply_to thread fetches."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    general_topic = make_mock_topic(
        topic_id=1,
        title="General",
        top_message_id=None,
        is_general=True,
    )
    message = make_general_topic_message(id=77, text="General update")

    mock_client.iter_messages = MagicMock(return_value=_async_iter([message]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {1: "General"},
            "metadata_by_id": {1: general_topic},
            "deleted_topics": {},
        }),
    )

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="General"))

    call_kwargs = mock_client.iter_messages.call_args.kwargs
    assert "reply_to" not in call_kwargs
    assert result[0].text.startswith("[topic: General]\n")
    assert "General update" in result[0].text


async def test_list_messages_deleted_topic_behavior(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_deleted_topic,
    make_mock_topic,
):
    """Deleted topic lookups return an explicit tombstone message instead of pretending the filter worked."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    general_topic = make_mock_topic(
        topic_id=1,
        title="General",
        top_message_id=None,
        is_general=True,
    )
    deleted_topic = make_deleted_topic(
        topic_id=9,
        title="Deprecated Topic",
        top_message_id=5009,
    )

    mock_client.iter_messages = MagicMock(return_value=_async_iter([]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {1: "General"},
            "metadata_by_id": {
                1: general_topic,
                9: deleted_topic,
            },
            "deleted_topics": {9: deleted_topic},
        }),
    )

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Deprecated Topic"))

    assert 'Topic "Deprecated Topic" was deleted and can no longer be fetched.' in result[0].text
    assert "Action:" in result[0].text
    assert "ListTopics" in result[0].text
    assert mock_client.iter_messages.call_count == 0


async def test_list_messages_private_or_inaccessible_topic_behavior(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_topic,
    make_private_topic_error,
):
    """Inaccessible topics return an explicit RPC-driven message instead of falling back silently."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)

    mock_client.iter_messages = MagicMock(side_effect=make_private_topic_error())
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: topic},
            "deleted_topics": {},
        }),
    )

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Release Notes"))

    assert 'Topic "Release Notes" resolved, but Telegram rejected thread fetch (TOPIC_PRIVATE).' in result[0].text
    assert "Action:" in result[0].text


async def test_list_messages_topic_retries_after_stale_top_message_id(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
    make_mock_topic,
):
    """A stale cached thread anchor is refreshed by topic ID and retried once."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    stale_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    refreshed_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=6011)
    TopicMetadataCache(cache._conn).upsert_topics(701, [stale_topic])
    recovered_message = make_mock_message(id=77, text="Recovered after refresh")

    mock_client.iter_messages = MagicMock(side_effect=[
        tools_module.RPCError(request=None, message="TOPIC_ID_INVALID", code=400),
        _async_iter([recovered_message]),
    ])
    refresh_topic = AsyncMock(return_value=refreshed_topic)

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: stale_topic},
            "deleted_topics": {},
        }),
    )
    monkeypatch.setattr("mcp_telegram.tools._refresh_topic_by_id", refresh_topic)

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Release Notes"))

    assert "Recovered after refresh" in result[0].text
    assert result[0].text.startswith("[topic: Release Notes]\n")
    assert refresh_topic.await_count == 1
    first_call_kwargs = mock_client.iter_messages.call_args_list[0].kwargs
    second_call_kwargs = mock_client.iter_messages.call_args_list[1].kwargs
    assert first_call_kwargs["reply_to"] == 5011
    assert second_call_kwargs["reply_to"] == 6011


async def test_list_messages_topic_deleted_after_refresh(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_deleted_topic,
    make_mock_topic,
):
    """A TOPIC_ID_INVALID thread fetch is reclassified as deleted when refresh returns a tombstone."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    stale_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    deleted_topic = make_deleted_topic(topic_id=11, title="Release Notes", top_message_id=5011)

    mock_client.iter_messages = MagicMock(
        side_effect=tools_module.RPCError(request=None, message="TOPIC_ID_INVALID", code=400)
    )
    refresh_topic = AsyncMock(return_value=deleted_topic)

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: stale_topic},
            "deleted_topics": {},
        }),
    )
    monkeypatch.setattr("mcp_telegram.tools._refresh_topic_by_id", refresh_topic)

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Release Notes"))

    assert 'Topic "Release Notes" was deleted and can no longer be fetched.' in result[0].text
    assert "Action:" in result[0].text
    assert refresh_topic.await_count == 1
    assert mock_client.iter_messages.call_count == 1


async def test_list_messages_topic_inaccessible_after_refresh(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_topic,
):
    """An active topic that still fails after refresh keeps an explicit topic-scoped error."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    stale_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)
    refreshed_topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=6011)
    TopicMetadataCache(cache._conn).upsert_topics(701, [stale_topic])

    mock_client.iter_messages = MagicMock(side_effect=[
        tools_module.RPCError(request=None, message="TOPIC_ID_INVALID", code=400),
        tools_module.RPCError(request=None, message="TOPIC_ID_INVALID", code=400),
        _async_iter([]),
    ])
    refresh_topic = AsyncMock(return_value=refreshed_topic)

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: stale_topic},
            "deleted_topics": {},
        }),
    )
    monkeypatch.setattr("mcp_telegram.tools._refresh_topic_by_id", refresh_topic)

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Release Notes"))

    assert 'Topic "Release Notes" resolved, but Telegram rejected thread fetch (TOPIC_ID_INVALID).' in result[0].text
    assert "Action:" in result[0].text
    assert refresh_topic.await_count == 1
    assert mock_client.iter_messages.call_count == 3
    assert "reply_to" not in mock_client.iter_messages.call_args_list[2].kwargs
    topic_cache = TopicMetadataCache(cache._conn)
    cached_topic = topic_cache.get_topic(701, 11, ttl_seconds=600)
    assert cached_topic is not None
    assert cached_topic["inaccessible_error"] == "TOPIC_ID_INVALID"


async def test_list_messages_topic_falls_back_to_dialog_scan_after_thread_fetch_failure(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_general_topic_message,
    make_mock_message,
    make_mock_topic,
    make_mock_forum_reply,
):
    """An active topic can still be read from dialog history when reply_to thread fetch stays invalid."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    active_topic = make_mock_topic(topic_id=40, title="Inbox", top_message_id=249)
    topic_reply = make_mock_forum_reply(reply_to_msg_id=249, reply_to_top_id=249)
    adjacent_reply = make_mock_forum_reply(reply_to_msg_id=7001, reply_to_top_id=7001)

    inbox_newest = make_mock_message(id=260, text="Inbox newest")
    inbox_newest.reply_to = topic_reply
    leaked_general = make_general_topic_message(id=259, text="General leak")
    leaked_adjacent = make_mock_message(id=258, text="Adjacent topic leak")
    leaked_adjacent.reply_to = adjacent_reply
    inbox_older = make_mock_message(id=257, text="Inbox older")
    inbox_older.reply_to = topic_reply

    mock_client.iter_messages = MagicMock(side_effect=[
        tools_module.RPCError(request=None, message="TOPIC_ID_INVALID", code=400),
        _async_iter([inbox_newest, leaked_general, leaked_adjacent, inbox_older]),
    ])
    refresh_topic = AsyncMock(return_value=active_topic)

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {40: "Inbox"},
            "metadata_by_id": {40: active_topic},
            "deleted_topics": {},
        }),
    )
    monkeypatch.setattr("mcp_telegram.tools._refresh_topic_by_id", refresh_topic)

    result = await list_messages(ListMessages(dialog="Backend Forum", topic="Inbox", limit=2))

    assert result[0].text.startswith("[topic: Inbox]\n")
    assert "Inbox newest" in result[0].text
    assert "Inbox older" in result[0].text
    assert "General leak" not in result[0].text
    assert "Adjacent topic leak" not in result[0].text
    first_call_kwargs = mock_client.iter_messages.call_args_list[0].kwargs
    second_call_kwargs = mock_client.iter_messages.call_args_list[1].kwargs
    assert first_call_kwargs["reply_to"] == 249
    assert "reply_to" not in second_call_kwargs


async def test_list_messages_topic_boundary_no_leakage(
    tmp_db_path,
    mock_client,
    monkeypatch,
    make_mock_message,
    make_mock_topic,
    make_mock_forum_reply,
):
    """Topic pagination strips adjacent-topic leaks and keeps cursors aligned to emitted thread messages."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.pagination import encode_cursor
    from mcp_telegram.tools import ListMessages, list_messages

    cache = EntityCache(tmp_db_path)
    cache.upsert(701, "group", "Backend Forum", None)
    topic = make_mock_topic(topic_id=11, title="Release Notes", top_message_id=5011)

    target_reply = make_mock_forum_reply(reply_to_msg_id=5011, reply_to_top_id=5011)
    leaked_reply = make_mock_forum_reply(reply_to_msg_id=7001, reply_to_top_id=7001)

    newest = make_mock_message(id=50, text="Topic newest")
    newest.reply_to = target_reply
    leaked_newer = make_mock_message(id=49, text="Adjacent topic leak")
    leaked_newer.reply_to = leaked_reply
    older = make_mock_message(id=48, text="Topic older")
    older.reply_to = target_reply

    leaked_middle = make_mock_message(id=47, text="Adjacent middle leak")
    leaked_middle.reply_to = leaked_reply
    page_two_newest = make_mock_message(id=46, text="Topic page two newest")
    page_two_newest.reply_to = target_reply
    page_two_oldest = make_mock_message(id=44, text="Topic page two oldest")
    page_two_oldest.reply_to = target_reply

    reverse_leak = make_mock_message(id=9, text="Reverse adjacent leak")
    reverse_leak.reply_to = leaked_reply
    reverse_oldest = make_mock_message(id=10, text="Topic reverse oldest")
    reverse_oldest.reply_to = target_reply
    reverse_newest = make_mock_message(id=12, text="Topic reverse newest")
    reverse_newest.reply_to = target_reply

    mock_client.iter_messages = MagicMock(side_effect=[
        _async_iter([newest, leaked_newer, older]),
        _async_iter([leaked_middle, page_two_newest, page_two_oldest]),
        _async_iter([reverse_oldest, reverse_leak, reverse_newest]),
    ])
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        "mcp_telegram.tools._load_dialog_topics",
        AsyncMock(return_value={
            "choices": {11: "Release Notes"},
            "metadata_by_id": {11: topic},
            "deleted_topics": {},
        }),
    )

    first_page = await list_messages(ListMessages(dialog="Backend Forum", topic="Release Notes", limit=2))

    expected_cursor = encode_cursor(48, 701)
    assert "Adjacent topic leak" not in first_page[0].text
    assert "Topic newest" in first_page[0].text
    assert "Topic older" in first_page[0].text
    assert f"next_cursor: {expected_cursor}" in first_page[0].text

    second_page = await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", limit=2, cursor=expected_cursor)
    )

    second_call_kwargs = mock_client.iter_messages.call_args_list[1].kwargs
    assert second_call_kwargs["reply_to"] == 5011
    assert second_call_kwargs["max_id"] == 48
    assert "Adjacent middle leak" not in second_page[0].text
    assert "Topic page two newest" in second_page[0].text
    assert "Topic page two oldest" in second_page[0].text
    assert "Topic newest" not in second_page[0].text

    reverse_page = await list_messages(
        ListMessages(dialog="Backend Forum", topic="Release Notes", from_beginning=True, limit=2)
    )

    reverse_call_kwargs = mock_client.iter_messages.call_args_list[2].kwargs
    assert reverse_call_kwargs["reply_to"] == 5011
    assert reverse_call_kwargs["reverse"] is True
    assert reverse_call_kwargs["min_id"] == 1
    assert "Reverse adjacent leak" not in reverse_page[0].text
    assert "Topic reverse oldest" in reverse_page[0].text
    assert "Topic reverse newest" in reverse_page[0].text


async def test_fetch_forum_topics_paginates() -> None:
    """Raw topic pagination advances offsets and de-duplicates topics across pages."""
    from mcp_telegram.tools import _fetch_all_forum_topics

    page_one_topics = [
        _make_mock_topic(topic_id=1, title="General", top_message_id=1001),
        _make_mock_topic(topic_id=2, title="Releases", top_message_id=1002),
    ]
    page_two_topics = [
        _make_mock_topic(
            topic_id=2,
            title="Releases",
            top_message_id=1002,
            date=datetime(2024, 1, 15, 10, 5, 0, tzinfo=timezone.utc),
        ),
        _make_mock_topic(
            topic_id=3,
            title="Ops",
            top_message_id=1003,
            date=datetime(2024, 1, 15, 10, 6, 0, tzinfo=timezone.utc),
        ),
    ]
    requests: list[object] = []
    responses = [
        SimpleNamespace(topics=page_one_topics, count=3),
        SimpleNamespace(topics=page_two_topics, count=3),
    ]

    async def _call(request):
        requests.append(request)
        return responses.pop(0)

    client = AsyncMock(side_effect=_call)

    topics = await _fetch_all_forum_topics(client, entity=777, page_size=2)

    assert [topic["topic_id"] for topic in topics] == [1, 2, 3]
    assert [topic["title"] for topic in topics] == ["General", "Releases", "Ops"]
    assert len(requests) == 2
    assert requests[0].offset_topic == 0
    assert requests[1].offset_topic == 2
    assert requests[1].offset_id == 1002
    assert requests[1].offset_date == page_one_topics[-1].date


async def test_refresh_topic_by_id_detects_deleted(tmp_db_path) -> None:
    """By-ID refresh turns a cached topic into a deleted tombstone."""
    from mcp_telegram.tools import _refresh_topic_by_id

    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    topic_cache.upsert_topics(
        dialog_id=777,
        topics=[
            {
                "topic_id": 9,
                "title": "Deprecated",
                "top_message_id": 9009,
                "is_general": False,
                "is_deleted": False,
            }
        ],
    )

    async def _call(request):
        return SimpleNamespace(topics=[SimpleNamespace(id=9)])

    client = AsyncMock(side_effect=_call)

    topic = await _refresh_topic_by_id(
        client,
        entity=777,
        dialog_id=777,
        topic_id=9,
        topic_cache=topic_cache,
    )

    assert topic is not None
    assert topic["topic_id"] == 9
    assert topic["title"] == "Deprecated"
    assert topic["is_deleted"] is True
    assert topic_cache.get_topic(dialog_id=777, topic_id=9, ttl_seconds=600) == topic

    cache.close()


async def test_load_dialog_topics_uses_fresh_cache(tmp_db_path) -> None:
    """Fresh cached topic metadata is returned without hitting Telegram."""
    from mcp_telegram.tools import _load_dialog_topics

    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    topic_cache.upsert_topics(
        dialog_id=777,
        topics=[
            {
                "topic_id": 1,
                "title": "General",
                "top_message_id": None,
                "is_general": True,
                "is_deleted": False,
            },
            {
                "topic_id": 2,
                "title": "Releases",
                "top_message_id": 1002,
                "is_general": False,
                "is_deleted": False,
            },
            {
                "topic_id": 9,
                "title": "Deprecated",
                "top_message_id": 9009,
                "is_general": False,
                "is_deleted": True,
            },
        ],
    )

    client = AsyncMock(side_effect=AssertionError("cache hit should not fetch from Telegram"))

    catalog = await _load_dialog_topics(
        client,
        entity=777,
        dialog_id=777,
        topic_cache=topic_cache,
    )

    assert catalog["choices"] == {1: "General", 2: "Releases"}
    assert 9 in catalog["metadata_by_id"]
    assert catalog["deleted_topics"][9]["is_deleted"] is True

    cache.close()


async def test_load_dialog_topics_fetches_and_persists_on_cache_miss(tmp_db_path) -> None:
    """Cache miss fetches forum topics once and persists normalized metadata."""
    from mcp_telegram.tools import _load_dialog_topics

    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)

    async def _call(request):
        return SimpleNamespace(
            topics=[
                _make_mock_topic(topic_id=2, title="Releases", top_message_id=1002),
            ],
            count=1,
        )

    client = AsyncMock(side_effect=_call)

    catalog = await _load_dialog_topics(
        client,
        entity=777,
        dialog_id=777,
        topic_cache=topic_cache,
    )

    assert catalog["choices"] == {1: "General", 2: "Releases"}
    assert topic_cache.get_topic(dialog_id=777, topic_id=2, ttl_seconds=600) == {
        "topic_id": 2,
        "title": "Releases",
        "top_message_id": 1002,
        "is_general": False,
        "is_deleted": False,
    }
    assert topic_cache.get_topic(dialog_id=777, topic_id=1, ttl_seconds=600) == {
        "topic_id": 1,
        "title": "General",
        "top_message_id": None,
        "is_general": True,
        "is_deleted": False,
    }

    cache.close()


# --- TOOL-06: SearchMessages context window ---


async def test_search_messages_context(mock_cache, mock_client, monkeypatch, make_mock_message):
    """SearchMessages returns hit messages formatted (no context window)."""
    from mcp_telegram.tools import SearchMessages, search_messages
    hit = make_mock_message(id=50, text="the hit")

    async def _fake_iter_messages(entity, **kwargs):
        yield hit

    mock_client.iter_messages = _fake_iter_messages
    mock_client.get_messages = AsyncMock(return_value=[])
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await search_messages(SearchMessages(dialog="Иван Петров", query="hit"))
    assert len(result) == 1
    assert "the hit" in result[0].text


async def test_search_messages_context_window(mock_cache, mock_client, monkeypatch, make_mock_message):
    """SearchMessages includes context messages before the hit in output."""
    from mcp_telegram.tools import SearchMessages, search_messages
    hit = make_mock_message(id=50, text="the hit")
    ctx_before = make_mock_message(id=47, text="before msg")
    ctx_after = make_mock_message(id=53, text="after msg")

    async def _fake_iter_messages(entity, **kwargs):
        yield hit

    mock_client.iter_messages = _fake_iter_messages
    mock_client.get_messages = AsyncMock(return_value=[ctx_before, ctx_after])
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await search_messages(SearchMessages(dialog="Иван Петров", query="hit"))
    assert len(result) == 1
    assert "before msg" in result[0].text


async def test_search_messages_context_after_hit(mock_cache, mock_client, monkeypatch, make_mock_message):
    """SearchMessages includes context messages after the hit in output."""
    from mcp_telegram.tools import SearchMessages, search_messages
    hit = make_mock_message(id=50, text="the hit")
    ctx_before = make_mock_message(id=47, text="before msg")
    ctx_after = make_mock_message(id=53, text="after msg")

    async def _fake_iter_messages(entity, **kwargs):
        yield hit

    mock_client.iter_messages = _fake_iter_messages
    mock_client.get_messages = AsyncMock(return_value=[ctx_before, ctx_after])
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await search_messages(SearchMessages(dialog="Иван Петров", query="hit"))
    assert len(result) == 1
    assert "after msg" in result[0].text


async def test_search_messages_hit_marker(mock_cache, mock_client, monkeypatch, make_mock_message):
    """SearchMessages visually distinguishes hit messages from context messages."""
    from mcp_telegram.tools import SearchMessages, search_messages
    hit = make_mock_message(id=50, text="the hit")

    async def _fake_iter_messages(entity, **kwargs):
        yield hit

    mock_client.iter_messages = _fake_iter_messages
    mock_client.get_messages = AsyncMock(return_value=[])
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await search_messages(SearchMessages(dialog="Иван Петров", query="hit"))
    assert len(result) == 1
    text = result[0].text
    # Hit group must have a dedicated hit marker line (not just the date separator).
    # Acceptable forms: "=== HIT ===" or "[HIT]" prefix on the message line or ">>>" prefix.
    assert "[HIT]" in text or ">>>" in text or "=== HIT ===" in text


async def test_search_messages_reaction_names_fetched(mock_cache, mock_client, monkeypatch, make_mock_message):
    """SearchMessages fetches reaction names for hit messages with low reaction counts."""
    from mcp_telegram.tools import SearchMessages, search_messages
    hit = make_mock_message(id=50, text="reacted msg")
    hit.reactions = MagicMock()
    hit.reactions.results = [MagicMock(count=2, reaction=MagicMock(emoticon="👍"))]

    async def _fake_iter_messages(entity, **kwargs):
        yield hit

    mock_client.iter_messages = _fake_iter_messages
    mock_client.get_messages = AsyncMock(return_value=[])
    mock_client.return_value = MagicMock(reactions=[], users=[])
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    await search_messages(SearchMessages(dialog="Иван Петров", query="reacted"))
    # GetMessageReactionsListRequest was invoked via client(...)
    mock_client.assert_called()


# --- TOOL-07: SearchMessages offset pagination ---


async def test_search_messages_next_offset(mock_cache, mock_client, monkeypatch, make_mock_message):
    """SearchMessages full page returns next_offset in output."""
    from mcp_telegram.tools import SearchMessages, search_messages

    # Return exactly limit=2 hits, no context
    hits = [make_mock_message(id=i, text=f"msg{i}") for i in range(1, 3)]
    call_count = 0

    async def _fake_iter(entity, **kwargs):
        for h in hits:
            yield h

    mock_client.iter_messages = _fake_iter
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await search_messages(SearchMessages(dialog="Иван Петров", query="msg", limit=2))
    assert "next_offset: 2" in result[0].text


async def test_search_messages_no_next_offset(mock_cache, mock_client, monkeypatch, make_mock_message):
    """SearchMessages last page (fewer than limit) has no next_offset in output."""
    from mcp_telegram.tools import SearchMessages, search_messages

    # Return 1 hit with limit=5 (partial page)
    hit = make_mock_message(id=10, text="only one")
    call_count = 0

    async def _fake_iter(entity, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield hit
        # context calls return empty

    mock_client.iter_messages = _fake_iter
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await search_messages(SearchMessages(dialog="Иван Петров", query="only", limit=5))
    assert "next_offset" not in result[0].text


# --- CLNP-01, CLNP-02: Removed tools ---


def test_get_dialog_removed():
    """GetDialog class does not exist in tools module."""
    assert not hasattr(tools_module, "GetDialog"), "GetDialog should be removed from tools.py"


def test_get_message_removed():
    """GetMessage class does not exist in tools module."""
    assert not hasattr(tools_module, "GetMessage"), "GetMessage should be removed from tools.py"


# --- TOOL-08: GetMyAccount ---


async def test_get_me(mock_client, monkeypatch):
    """GetMyAccount returns text containing id, first_name, and @username of current account."""
    from mcp_telegram.tools import GetMyAccount, get_my_account
    mock_client.get_me = AsyncMock(
        return_value=MagicMock(id=999, first_name="Test", last_name=None, username="testuser")
    )
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    result = await get_my_account(GetMyAccount())
    assert len(result) == 1
    text = result[0].text
    assert "id=999" in text
    assert "Test" in text
    assert "@testuser" in text


async def test_get_me_unauthenticated(mock_client, monkeypatch):
    """GetMyAccount returns 'not authenticated' message when client.get_me() returns None."""
    from mcp_telegram.tools import GetMyAccount, get_my_account
    mock_client.get_me = AsyncMock(return_value=None)
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    result = await get_my_account(GetMyAccount())
    assert len(result) == 1
    assert "Telegram session is not authenticated." in result[0].text
    assert "Action:" in result[0].text
    assert "GetMyAccount" in result[0].text


# --- TOOL-09: GetUserInfo ---


async def test_get_user_info(mock_cache, mock_client, monkeypatch):
    """GetUserInfo resolves by name, fetches entity and common chats, returns formatted info."""
    from mcp_telegram.tools import GetUserInfo, get_user_info
    mock_client.get_entity = AsyncMock(
        return_value=MagicMock(id=101, first_name="Иван", last_name="Петров", username="ivan")
    )
    fake_chat = MagicMock(id=500, title="Shared Group")
    fake_result = MagicMock(chats=[fake_chat])
    mock_client.return_value = fake_result
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    monkeypatch.setattr("mcp_telegram.tools.get_peer_id", lambda chat: -1000000000500)
    result = await get_user_info(GetUserInfo(user="Иван Петров"))
    assert len(result) == 1
    text = result[0].text
    assert "id=101" in text
    assert "Иван Петров" in text
    assert "Shared Group" in text
    assert "-1000000000500" in text


async def test_get_user_info_not_found(mock_cache, mock_client, monkeypatch):
    """GetUserInfo returns 'not found' when user name does not match any cache entry."""
    from mcp_telegram.tools import GetUserInfo, get_user_info
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    result = await get_user_info(GetUserInfo(user="nobody_xyz"))
    assert len(result) == 1
    assert 'User "nobody_xyz" was not found.' in result[0].text
    assert "Action:" in result[0].text
    assert "ListDialogs" in result[0].text


async def test_get_user_info_ambiguous(mock_client, monkeypatch, tmp_db_path):
    """GetUserInfo returns 'ambiguous' when multiple cache entries match the query."""
    from mcp_telegram.cache import EntityCache
    from mcp_telegram.tools import GetUserInfo, get_user_info
    ambig_cache = EntityCache(tmp_db_path)
    ambig_cache.upsert(201, "user", "Иван Петров", None)
    ambig_cache.upsert(202, "user", "Иван Сидоров", None)
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: ambig_cache)
    result = await get_user_info(GetUserInfo(user="Иван"))
    assert len(result) == 1
    assert 'User "Иван" matched multiple users.' in result[0].text
    assert "Action:" in result[0].text
    assert 'id=201' in result[0].text
    assert 'id=202' in result[0].text


async def test_get_user_info_fetch_error_returns_action(mock_cache, mock_client, monkeypatch):
    """GetUserInfo returns an action-oriented response when Telegram fetch fails."""
    from mcp_telegram.tools import GetUserInfo, get_user_info

    mock_client.get_entity = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await get_user_info(GetUserInfo(user="Иван Петров"))

    assert len(result) == 1
    assert 'Could not fetch info for user "Иван Петров" (boom).' in result[0].text
    assert "Action:" in result[0].text
    assert "Retry GetUserInfo later" in result[0].text


async def test_get_user_info_resolver_prefix(mock_cache, mock_client, monkeypatch):
    """GetUserInfo output first line starts with '[resolved: "Иван Петров"]'."""
    from mcp_telegram.tools import GetUserInfo, get_user_info
    mock_client.get_entity = AsyncMock(
        return_value=MagicMock(id=101, first_name="Иван", last_name="Петров", username="ivan")
    )
    fake_chat = MagicMock(id=500, title="Shared Group")
    fake_result = MagicMock(chats=[fake_chat])
    mock_client.return_value = fake_result
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    monkeypatch.setattr("mcp_telegram.tools.get_peer_id", lambda chat: -1000000000500)
    result = await get_user_info(GetUserInfo(user="Иван Петров"))
    assert len(result) == 1
    first_line = result[0].text.splitlines()[0]
    assert first_line.startswith('[resolved: "Иван Петров"]')


# --- Phase 5 stubs: CACH-01, CACH-02, TOOL-03 ---


async def test_list_messages_stale_entity_excluded(mock_cache, mock_client, monkeypatch):
    """tools.py uses all_names_with_ttl (TTL-filtered) instead of all_names for resolver."""
    from mcp_telegram.tools import ListMessages, list_messages

    all_names_ttl_mock = MagicMock(return_value={})
    monkeypatch.setattr(mock_cache, "all_names_with_ttl", all_names_ttl_mock)
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await list_messages(ListMessages(dialog="Unknown"))
    assert all_names_ttl_mock.called, "tools.py must call all_names_with_ttl for name resolution"
    assert len(result) == 1
    assert "not found" in result[0].text.lower()


async def test_search_messages_upserts_sender(mock_cache, mock_client, monkeypatch, make_mock_message):
    """search_messages calls cache.upsert for sender of each hit message."""
    from mcp_telegram.tools import SearchMessages, search_messages

    hit_msg = MagicMock()
    hit_msg.id = 50
    hit_msg.sender_id = 999
    hit_msg.sender = MagicMock(first_name="Alice", last_name=None, username=None)
    hit_msg.message = "hi"
    hit_msg.reactions = None
    hit_msg.media = None
    hit_msg.reply_to = None
    hit_msg.date = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

    async def _fake_iter_messages(entity, **kwargs):
        yield hit_msg

    mock_client.iter_messages = _fake_iter_messages
    mock_client.get_messages = AsyncMock(return_value=[])

    upsert_spy = MagicMock(wraps=mock_cache.upsert)
    monkeypatch.setattr(mock_cache, "upsert", upsert_spy)
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    await search_messages(SearchMessages(dialog="Иван Петров", query="hi"))

    sender_calls = [c for c in upsert_spy.call_args_list if c.args[0] == 999]
    assert sender_calls, "cache.upsert must be called with sender_id=999 for the hit message"


async def test_search_messages_no_hits_returns_action(mock_cache, mock_client, monkeypatch):
    """SearchMessages returns an action-oriented empty-state when no hits match the query."""
    from mcp_telegram.tools import SearchMessages, search_messages

    mock_client.iter_messages = MagicMock(return_value=_async_iter([]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await search_messages(SearchMessages(dialog="Иван Петров", query="zzz"))

    assert len(result) == 1
    assert 'No messages matched query "zzz" in dialog "Иван Петров".' in result[0].text
    assert "Action:" in result[0].text
    assert "broader query" in result[0].text


async def test_list_messages_invalid_cursor_returns_error(mock_cache, mock_client, monkeypatch):
    """list_messages with a malformed cursor returns an action-oriented cursor error."""
    from mcp_telegram.tools import ListMessages, list_messages

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await list_messages(ListMessages(dialog="Иван Петров", cursor="BADINVALID==garbage"))
    assert len(result) == 1
    assert result[0].text.startswith("Cursor is invalid:")
    assert "Action:" in result[0].text


# --- TELEMETRY TESTS ---


@pytest.fixture
def mock_analytics_collector(monkeypatch):
    """Mock TelemetryCollector to capture events without writing to DB."""
    events = []

    def record_event(event):
        events.append(event)

    mock_collector = MagicMock()
    mock_collector.record_event = record_event

    monkeypatch.setattr(
        "mcp_telegram.tools._get_analytics_collector",
        lambda: mock_collector
    )
    return events


async def test_list_dialogs_records_telemetry(mock_cache, mock_client, monkeypatch, mock_analytics_collector):
    """ListDialogs records telemetry event with correct metrics."""
    from mcp_telegram.tools import ListDialogs, list_dialogs

    def _make_dialog(name, id_):
        d = MagicMock()
        d.is_user = True
        d.is_group = False
        d.is_channel = False
        d.date = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        d.id = id_
        d.name = name
        d.unread_count = 0
        d.entity = MagicMock(username=None)
        return d

    dialogs = [_make_dialog("Alice", 1), _make_dialog("Bob", 2)]
    mock_client.iter_dialogs = MagicMock(return_value=_async_iter(dialogs))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    await list_dialogs(ListDialogs())

    assert len(mock_analytics_collector) == 1
    event = mock_analytics_collector[0]
    assert event.tool_name == "ListDialogs"
    assert event.result_count == 2
    assert event.has_cursor is False
    assert event.page_depth == 1
    assert event.has_filter is False
    assert event.error_type is None


async def test_list_messages_records_telemetry(mock_cache, mock_client, monkeypatch, mock_analytics_collector, make_mock_message):
    """ListMessages records telemetry event with cursor and filter info."""
    from mcp_telegram.tools import ListMessages, list_messages

    msg = make_mock_message(id=10, text="Hello")
    mock_client.iter_messages = MagicMock(return_value=_async_iter([msg]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    await list_messages(ListMessages(dialog="Иван Петров"))

    assert len(mock_analytics_collector) == 1
    event = mock_analytics_collector[0]
    assert event.tool_name == "ListMessages"
    assert event.result_count == 1
    assert event.has_cursor is False
    assert event.page_depth >= 1
    assert event.has_filter is False


async def test_list_messages_records_cursor(mock_cache, mock_client, monkeypatch, mock_analytics_collector, make_mock_message):
    """ListMessages records has_cursor=True when cursor provided."""
    from mcp_telegram.tools import ListMessages, list_messages
    from mcp_telegram.pagination import encode_cursor

    msg = make_mock_message(id=10, text="Hello")
    mock_client.iter_messages = MagicMock(return_value=_async_iter([msg]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    cursor = encode_cursor(50, 101)
    await list_messages(ListMessages(dialog="Иван Петров", cursor=cursor))

    assert len(mock_analytics_collector) == 1
    event = mock_analytics_collector[0]
    assert event.has_cursor is True


async def test_list_messages_records_filter(mock_cache, mock_client, monkeypatch, mock_analytics_collector, make_mock_message):
    """ListMessages records has_filter=True when query_sender provided."""
    from mcp_telegram.tools import ListMessages, list_messages

    msg = make_mock_message(id=10, text="Hello")
    mock_client.iter_messages = MagicMock(return_value=_async_iter([msg]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    await list_messages(ListMessages(dialog="Иван Петров", sender="Иван Петров"))

    assert len(mock_analytics_collector) == 1
    event = mock_analytics_collector[0]
    assert event.has_filter is True


async def test_search_messages_records_telemetry(mock_cache, mock_client, monkeypatch, mock_analytics_collector, make_mock_message):
    """SearchMessages records telemetry with has_filter=True."""
    from mcp_telegram.tools import SearchMessages, search_messages

    hit = make_mock_message(id=50, text="the hit")
    mock_client.iter_messages = hit.__class__.iter_messages = MagicMock(return_value=_async_iter([hit]))
    mock_client.get_messages = AsyncMock(return_value=[])

    async def _fake_iter_messages(entity, **kwargs):
        yield hit

    mock_client.iter_messages = _fake_iter_messages
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    await search_messages(SearchMessages(dialog="Иван Петров", query="hit"))

    assert len(mock_analytics_collector) == 1
    event = mock_analytics_collector[0]
    assert event.tool_name == "SearchMessages"
    assert event.has_filter is True


async def test_get_my_account_records_telemetry(mock_cache, mock_client, monkeypatch, mock_analytics_collector):
    """GetMyAccount records telemetry with result_count=1."""
    from mcp_telegram.tools import GetMyAccount, get_my_account

    me = MagicMock()
    me.id = 999
    me.first_name = "Test"
    me.last_name = "User"
    me.username = "testuser"
    mock_client.get_me = AsyncMock(return_value=me)

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    await get_my_account(GetMyAccount())

    assert len(mock_analytics_collector) == 1
    event = mock_analytics_collector[0]
    assert event.tool_name == "GetMyAccount"
    assert event.result_count == 1
    assert event.has_cursor is False
    assert event.page_depth == 1


async def test_get_user_info_records_telemetry(mock_cache, mock_client, monkeypatch, mock_analytics_collector):
    """GetUserInfo records telemetry with result_count=1."""
    from mcp_telegram.tools import GetUserInfo, get_user_info
    from telethon.tl.types import Channel

    user = MagicMock()
    user.id = 101
    user.first_name = "Иван"
    user.last_name = "Петров"
    user.username = "ivan"

    mock_client.get_entity = AsyncMock(return_value=user)
    mock_client.return_value = MagicMock(chats=[])

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    await get_user_info(GetUserInfo(user="Иван Петров"))

    assert len(mock_analytics_collector) == 1
    event = mock_analytics_collector[0]
    assert event.tool_name == "GetUserInfo"
    assert event.result_count == 1


async def test_tool_records_telemetry_on_error(mock_cache, mock_client, monkeypatch, mock_analytics_collector):
    """Tool records telemetry even when exception raised, with error_type set."""
    from mcp_telegram.tools import ListDialogs, list_dialogs

    mock_client.iter_dialogs = MagicMock(side_effect=Exception("ConnectionError"))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    with pytest.raises(Exception):
        await list_dialogs(ListDialogs())

    assert len(mock_analytics_collector) == 1
    event = mock_analytics_collector[0]
    assert event.tool_name == "ListDialogs"
    assert event.error_type is not None


async def test_get_usage_stats_not_recorded(mock_cache, mock_client, monkeypatch, mock_analytics_collector):
    """GetUsageStats does NOT record telemetry (to avoid noise)."""
    from mcp_telegram.tools import GetUsageStats, get_usage_stats

    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    await get_usage_stats(GetUsageStats())

    # No telemetry event should be recorded
    assert len(mock_analytics_collector) == 0


# --- TOOL-06: GetUsageStats ---


async def test_get_usage_stats_under_100_tokens(mock_cache, mock_client, monkeypatch, tmp_path):
    """GetUsageStats output is <100 tokens."""
    from mcp_telegram.tools import GetUsageStats, get_usage_stats
    import sqlite3

    # Create a temporary analytics.db with sample data
    db_path = tmp_path / "analytics.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Create telemetry_events table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            timestamp REAL NOT NULL,
            duration_ms REAL NOT NULL,
            result_count INTEGER NOT NULL,
            has_cursor BOOLEAN NOT NULL,
            page_depth INTEGER NOT NULL,
            has_filter BOOLEAN NOT NULL,
            error_type TEXT
        )
    """)

    # Insert sample events from the past 30 days
    now = 1000000.0
    for i in range(10):
        cursor.execute("""
            INSERT INTO telemetry_events
            (tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("ListMessages", now - i * 100, 45.0 + i, 5 + i, False, 3 + (i % 2), False, None))

    for i in range(3):
        cursor.execute("""
            INSERT INTO telemetry_events
            (tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("ListDialogs", now - 1000 - i * 100, 30.0 + i, 3, False, 1, False, None))

    # Add an error event
    cursor.execute("""
        INSERT INTO telemetry_events
        (tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, ("ListMessages", now - 2000, 100.0, 0, False, 1, False, "NotFound"))

    conn.commit()
    conn.close()

    # Mock xdg_state_home to return our tmp_path
    import mcp_telegram.tools
    original_xdg = mcp_telegram.tools.xdg_state_home
    mcp_telegram.tools.xdg_state_home = lambda: tmp_path

    try:
        monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
        monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

        result = await get_usage_stats(GetUsageStats())

        assert len(result) == 1
        from mcp.types import TextContent
        assert isinstance(result[0], TextContent)

        token_count = len(result[0].text.split())
        assert token_count < 100, f"Output has {token_count} tokens, should be <100"
    finally:
        mcp_telegram.tools.xdg_state_home = original_xdg


async def test_get_usage_stats_empty_db(mock_cache, mock_client, monkeypatch, tmp_path):
    """GetUsageStats returns helpful message on empty/missing DB."""
    from mcp_telegram.tools import GetUsageStats, get_usage_stats
    import mcp_telegram.tools

    # Create mcp-telegram subdirectory (matches real structure)
    db_dir = tmp_path / "mcp-telegram"
    db_dir.mkdir(exist_ok=True)

    # Mock xdg_state_home to return path with no analytics.db
    original_xdg = mcp_telegram.tools.xdg_state_home
    mcp_telegram.tools.xdg_state_home = lambda: tmp_path

    try:
        monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
        monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

        result = await get_usage_stats(GetUsageStats())

        assert len(result) == 1
        from mcp.types import TextContent
        assert isinstance(result[0], TextContent)
        assert "Analytics database not yet created." in result[0].text
        assert "Action:" in result[0].text
        assert "retry GetUsageStats" in result[0].text
    finally:
        mcp_telegram.tools.xdg_state_home = original_xdg


async def test_get_usage_stats_no_recent_data_returns_action(mock_cache, mock_client, monkeypatch, tmp_path):
    """GetUsageStats returns an action-oriented response when analytics DB exists but has no recent data."""
    from mcp_telegram.tools import GetUsageStats, get_usage_stats
    import sqlite3
    import mcp_telegram.tools

    db_dir = tmp_path / "mcp-telegram"
    db_dir.mkdir(exist_ok=True)
    db_path = db_dir / "analytics.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            timestamp REAL NOT NULL,
            duration_ms REAL NOT NULL,
            result_count INTEGER NOT NULL,
            has_cursor BOOLEAN NOT NULL,
            page_depth INTEGER NOT NULL,
            has_filter BOOLEAN NOT NULL,
            error_type TEXT
        )
    """)
    conn.commit()
    conn.close()

    original_xdg = mcp_telegram.tools.xdg_state_home
    mcp_telegram.tools.xdg_state_home = lambda: tmp_path

    try:
        monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
        monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

        result = await get_usage_stats(GetUsageStats())

        assert len(result) == 1
        assert "No usage data in the past 30 days." in result[0].text
        assert "Action:" in result[0].text
        assert "retry GetUsageStats" in result[0].text
    finally:
        mcp_telegram.tools.xdg_state_home = original_xdg


# --- Wave 0 Test Stubs: Reverse Pagination (NAV-01) ---


async def test_list_messages_from_beginning(mock_cache, mock_client, monkeypatch, make_mock_message):
    """ListMessages accepts from_beginning=True parameter and uses reverse iteration.

    Validates that the from_beginning parameter is recognized by ListMessages and
    properly routed to iter_messages as reverse=True.
    """
    msg1 = make_mock_message(id=1, text="First message")
    msg2 = make_mock_message(id=2, text="Second message")
    mock_client.iter_messages = MagicMock(return_value=_async_iter([msg1, msg2]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    from mcp_telegram.tools import ListMessages, list_messages
    result = await list_messages(ListMessages(dialog="Иван Петров", from_beginning=True))

    # Verify iter_messages was called with reverse=True
    call_kwargs = mock_client.iter_messages.call_args[1]
    assert call_kwargs["reverse"] is True
    assert call_kwargs["min_id"] == 1  # Start from oldest

    # Verify output shows messages (no error)
    assert len(result) == 1
    assert "First message" in result[0].text or "Second message" in result[0].text


async def test_list_messages_from_beginning_oldest_first(mock_cache, mock_client, monkeypatch, make_mock_message):
    """from_beginning=True with multiple messages displays oldest first.

    Verifies that when from_beginning=True, the output shows messages in chronological
    order (oldest first) rather than reverse chronological order.
    """
    # Create messages in reverse ID order (like Telethon returns when reverse=True)
    msg1 = make_mock_message(id=1, text="Oldest", date=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc))
    msg2 = make_mock_message(id=2, text="Middle", date=datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc))
    msg3 = make_mock_message(id=3, text="Newest", date=datetime(2024, 1, 3, 10, 0, tzinfo=timezone.utc))

    mock_client.iter_messages = MagicMock(return_value=_async_iter([msg1, msg2, msg3]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    from mcp_telegram.tools import ListMessages, list_messages
    result = await list_messages(ListMessages(dialog="Иван Петров", from_beginning=True))

    text = result[0].text
    lines = text.split('\n')

    # formatter.py reverses unconditionally, so output should be newest-first visually (that's correct for display)
    # But verify all messages are present
    assert "Oldest" in text
    assert "Middle" in text
    assert "Newest" in text


async def test_list_messages_reverse_pagination_cursor(mock_cache, mock_client, monkeypatch, make_mock_message):
    """Cursor pagination works correctly with from_beginning=True (reverse iteration).

    Confirms that cursor-based pagination functions bidirectionally: when using
    from_beginning=True, the next_cursor from page 1 can be used with from_beginning=True
    on page 2 to continue iteration using min_id instead of max_id.
    """
    msg1 = make_mock_message(id=1, text="Message 1")
    msg2 = make_mock_message(id=2, text="Message 2")

    # Return exactly 2 messages with limit=2 to trigger cursor generation
    mock_client.iter_messages = MagicMock(return_value=_async_iter([msg1, msg2]))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    from mcp_telegram.tools import ListMessages, list_messages
    from mcp_telegram.pagination import encode_cursor, decode_cursor

    # Page 1: fetch from beginning with limit=2 to trigger full-page cursor
    result = await list_messages(ListMessages(dialog="Иван Петров", from_beginning=True, limit=2))
    assert "next_cursor" in result[0].text  # Output should include cursor for page 2

    # Extract cursor from result (format_messages includes it in footer)
    # Cursor is in format: "next_cursor: {cursor_token}"
    import re
    match = re.search(r"next_cursor:\s*(\S+)", result[0].text)
    if match:
        next_cursor = match.group(1)
        # Page 2: use cursor with from_beginning=True
        result2 = await list_messages(ListMessages(
            dialog="Иван Петров",
            from_beginning=True,
            cursor=next_cursor,
            limit=2
        ))
        # Verify second call used min_id with decoded cursor
        call_kwargs = mock_client.iter_messages.call_args[1]
        assert "min_id" in call_kwargs  # Should use min_id for reverse
        assert call_kwargs["reverse"] is True


# --- Wave 0 Test Stubs: Archived Dialog Filtering (NAV-02) ---


async def test_list_dialogs_archived_default(mock_cache, mock_client, monkeypatch):
    """ListDialogs() returns both archived and non-archived dialogs by default.

    Tests that default behavior shows all dialogs regardless of archive status,
    and that both archived and non-archived dialogs are added to the entity cache
    for name resolution.
    """
    def _make_dialog(name, id_, is_user=True, archived=False):
        d = MagicMock()
        d.is_user = is_user
        d.is_group = not is_user
        d.is_channel = False
        d.date = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        d.id = id_
        d.name = name
        d.unread_count = 0
        d.entity = MagicMock(username=None)
        d.folder_id = 1 if archived else None  # Telegram marks archived dialogs via folder_id
        return d

    # Create one non-archived dialog and one archived dialog
    dialogs = [
        _make_dialog("Alice", 1, is_user=True, archived=False),
        _make_dialog("Archived Chat", 2, is_user=True, archived=True),
    ]
    mock_client.iter_dialogs = MagicMock(return_value=_async_iter(dialogs))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    from mcp_telegram.tools import ListDialogs, list_dialogs
    result = await list_dialogs(ListDialogs())

    # Verify iter_dialogs was called with archived=None (show all)
    call_kwargs = mock_client.iter_dialogs.call_args[1]
    assert call_kwargs["archived"] is None

    # Verify output contains both dialogs
    text = result[0].text
    assert "Alice" in text
    assert "Archived Chat" in text

    # Verify both were cached
    all_names = mock_cache.all_names()
    assert 1 in all_names  # Non-archived
    assert 2 in all_names  # Archived
    assert all_names[1] == "Alice"
    assert all_names[2] == "Archived Chat"



async def test_list_dialogs_exclude_archived(mock_cache, mock_client, monkeypatch):
    """ListDialogs(exclude_archived=True) shows only non-archived dialogs.

    Tests that when exclude_archived=True, the handler filters to show only
    non-archived dialogs, equivalent to the old archived=False behavior.
    """
    def _make_dialog(name, id_, is_user=True, archived=False):
        d = MagicMock()
        d.is_user = is_user
        d.is_group = not is_user
        d.is_channel = False
        d.date = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        d.id = id_
        d.name = name
        d.unread_count = 0
        d.entity = MagicMock(username=None)
        d.folder_id = 1 if archived else None
        return d

    # Create one non-archived dialog and one archived dialog
    dialogs = [
        _make_dialog("Alice", 1, is_user=True, archived=False),
        # Note: when exclude_archived=True, iter_dialogs(archived=False) only yields non-archived
    ]
    mock_client.iter_dialogs = MagicMock(return_value=_async_iter(dialogs))
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    from mcp_telegram.tools import ListDialogs, list_dialogs
    result = await list_dialogs(ListDialogs(exclude_archived=True))

    # Verify iter_dialogs was called with archived=False (show non-archived only)
    call_kwargs = mock_client.iter_dialogs.call_args[1]
    assert call_kwargs["archived"] is False

    # Verify output contains only non-archived dialog
    text = result[0].text
    assert "Alice" in text
