from __future__ import annotations
import pytest
import mcp_telegram.tools as tools_module
from mcp_telegram.tools import ListDialogs, ListMessages, SearchMessages
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone


async def _async_iter(items):
    """Async generator yielding items from a list — local helper for test_tools.py."""
    for item in items:
        yield item


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


async def test_list_messages_not_found(mock_cache, mock_client, monkeypatch):
    """ListMessages with unresolved name returns TextContent with 'not found'."""
    from mcp_telegram.tools import ListMessages, list_messages
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)
    result = await list_messages(ListMessages(dialog="nobody_xyz"))
    assert len(result) == 1
    assert "not found" in result[0].text.lower()


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
    assert "Ambiguous" in result[0].text or "ambiguous" in result[0].text.lower()


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
    assert call_kwargs.get("limit") == 100  # args.limit default, unread_count no longer caps it


# --- TOOL-06: SearchMessages context window ---


async def test_search_messages_context(mock_cache, mock_client, monkeypatch, make_mock_message):
    """SearchMessages returns hit messages formatted (no context window)."""
    from mcp_telegram.tools import SearchMessages, search_messages
    hit = make_mock_message(id=50, text="the hit")

    async def _fake_iter_messages(entity, **kwargs):
        yield hit

    mock_client.iter_messages = _fake_iter_messages
    monkeypatch.setattr("mcp_telegram.tools.create_client", lambda: mock_client)
    monkeypatch.setattr("mcp_telegram.tools.get_entity_cache", lambda: mock_cache)

    result = await search_messages(SearchMessages(dialog="Иван Петров", query="hit"))
    assert len(result) == 1
    assert "the hit" in result[0].text


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
    text = result[0].text.lower()
    assert "not authenticated" in text or "not logged in" in text


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
    assert "not found" in result[0].text.lower()


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
    assert "Ambiguous" in result[0].text or "ambiguous" in result[0].text.lower()


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
