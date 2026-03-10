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
    assert call_kwargs.get("limit") == 3


# --- TOOL-06: SearchMessages context window ---


async def test_search_messages_context(mock_cache, mock_client, make_mock_message):
    """SearchMessages output contains context messages before and after each hit."""
    pytest.fail("not implemented")


# --- TOOL-07: SearchMessages offset pagination ---


async def test_search_messages_next_offset(mock_cache, mock_client, make_mock_message):
    """SearchMessages full page returns next_offset in output."""
    pytest.fail("not implemented")


async def test_search_messages_no_next_offset(mock_cache, mock_client, make_mock_message):
    """SearchMessages last page (fewer than limit) has no next_offset in output."""
    pytest.fail("not implemented")


# --- CLNP-01, CLNP-02: Removed tools ---


def test_get_dialog_removed():
    """GetDialog class does not exist in tools module."""
    assert not hasattr(tools_module, "GetDialog"), "GetDialog should be removed from tools.py"


def test_get_message_removed():
    """GetMessage class does not exist in tools module."""
    assert not hasattr(tools_module, "GetMessage"), "GetMessage should be removed from tools.py"
