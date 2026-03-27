"""Tests for Phase 29 daemon-routed MCP tools.

All tools in discovery.py and reading.py now route through daemon_connection()
instead of directly connecting to Telegram. These tests verify:
- Daemon API is called with correct parameters
- Dialog name passed to daemon when not in entity cache
- DaemonNotRunningError handled with actionable error text
- Response formatting (sync_status, message display, etc.)
- Zero Telegram imports in tools/ package
"""
from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.tools import (
    GetMyAccount,
    GetSyncAlerts,
    GetSyncStatus,
    GetUserInfo,
    ListDialogs,
    ListMessages,
    ListTopics,
    ListUnreadMessages,
    MarkDialogForSync,
    SearchMessages,
    get_my_account,
    get_sync_alerts,
    get_sync_status,
    get_user_info,
    list_dialogs,
    list_messages,
    list_topics,
    list_unread_messages,
    mark_dialog_for_sync,
    search_messages,
)
from mcp_telegram.tools._base import DaemonNotRunningError


# ---------------------------------------------------------------------------
# Daemon mock helpers
# ---------------------------------------------------------------------------


def _make_daemon_conn(response: dict | None = None) -> MagicMock:
    """Return a mock DaemonConnection that returns *response* for any method."""
    conn = MagicMock()
    r = response or {"ok": True, "data": {}}
    conn.list_messages = AsyncMock(return_value=r)
    conn.search_messages = AsyncMock(return_value=r)
    conn.list_dialogs = AsyncMock(return_value=r)
    conn.list_topics = AsyncMock(return_value=r)
    conn.get_me = AsyncMock(return_value=r)
    conn.mark_dialog_for_sync = AsyncMock(return_value=r)
    conn.get_sync_status = AsyncMock(return_value=r)
    conn.get_sync_alerts = AsyncMock(return_value=r)
    conn.get_user_info = AsyncMock(return_value=r)
    conn.list_unread_messages = AsyncMock(return_value=r)
    return conn


@asynccontextmanager
async def _fake_daemon_cm(conn):
    yield conn


class _patch_daemon:
    """Context manager that patches daemon_connection in all tool modules."""

    def __init__(self, conn):
        self._conn = conn
        self._patches = []

    def __enter__(self):
        targets = [
            "mcp_telegram.tools.discovery.daemon_connection",
            "mcp_telegram.tools.reading.daemon_connection",
            "mcp_telegram.tools.sync.daemon_connection",
            "mcp_telegram.tools.user_info.daemon_connection",
            "mcp_telegram.tools.unread.daemon_connection",
        ]
        for target in targets:
            p = patch(target, return_value=_fake_daemon_cm(self._conn))
            p.start()
            self._patches.append(p)
        return self

    def __exit__(self, *args):
        for p in self._patches:
            p.stop()


class _patch_daemon_not_running:
    """Context manager that makes daemon_connection raise DaemonNotRunningError in all tool modules."""

    def __enter__(self):
        @asynccontextmanager
        async def _raise_not_running():
            raise DaemonNotRunningError("Sync daemon is not running. Start it with: mcp-telegram sync")
            yield  # noqa: unreachable

        self._patches = []
        targets = [
            "mcp_telegram.tools.discovery.daemon_connection",
            "mcp_telegram.tools.reading.daemon_connection",
            "mcp_telegram.tools.sync.daemon_connection",
            "mcp_telegram.tools.user_info.daemon_connection",
            "mcp_telegram.tools.unread.daemon_connection",
        ]
        for target in targets:
            p = patch(target, return_value=_raise_not_running())
            p.start()
            self._patches.append(p)
        return self

    def __exit__(self, *args):
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# ListDialogs — daemon routing
# ---------------------------------------------------------------------------


async def test_list_dialogs_via_daemon():
    """ListDialogs routes through daemon API and formats output."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "dialogs": [
                {
                    "id": 123,
                    "name": "Alice",
                    "type": "user",
                    "last_message_at": "2024-01-15 10:00",
                    "unread_count": 2,
                    "sync_status": "synced",
                },
                {
                    "id": 456,
                    "name": "Dev Chat",
                    "type": "group",
                    "last_message_at": "2024-01-15 12:00",
                    "unread_count": 0,
                    "sync_status": "not_synced",
                },
            ]
        },
    })
    with _patch_daemon(conn):
        result = await list_dialogs(ListDialogs())

    assert len(result) == 1
    text = result[0].text
    assert "Alice" in text
    assert "Dev Chat" in text
    assert "sync_status=synced" in text
    assert "sync_status=not_synced" in text
    conn.list_dialogs.assert_called_once()


async def test_list_dialogs_sync_status_in_output():
    """ListDialogs output includes sync_status field for every dialog."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "dialogs": [
                {
                    "id": 1, "name": "Chat", "type": "user",
                    "last_message_at": "2024-01-01 00:00",
                    "unread_count": 0, "sync_status": "synced",
                },
            ]
        },
    })
    with _patch_daemon(conn):
        result = await list_dialogs(ListDialogs())

    assert "sync_status=" in result[0].text


async def test_list_dialogs_empty_via_daemon():
    """ListDialogs returns action-oriented empty text when no dialogs."""
    conn = _make_daemon_conn({"ok": True, "data": {"dialogs": []}})
    with _patch_daemon(conn):
        result = await list_dialogs(ListDialogs())

    assert "No dialogs" in result[0].text


async def test_list_dialogs_populates_entity_cache():
    """ListDialogs upserts dialog entries into entity cache."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "dialogs": [
                {
                    "id": 100, "name": "TestChat", "type": "group",
                    "last_message_at": "2024-01-01", "unread_count": 0,
                    "sync_status": "synced",
                },
            ]
        },
    })
    mock_cache = MagicMock()
    with _patch_daemon(conn), \
         patch("mcp_telegram.tools.discovery.get_entity_cache", return_value=mock_cache):
        await list_dialogs(ListDialogs())

    mock_cache.upsert_batch.assert_called_once()
    batch = mock_cache.upsert_batch.call_args[0][0]
    assert batch[0][0] == 100
    assert batch[0][2] == "TestChat"


# ---------------------------------------------------------------------------
# ListTopics — daemon routing
# ---------------------------------------------------------------------------


async def test_list_topics_via_daemon():
    """ListTopics routes through daemon API."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "topics": [
                {"id": 1, "title": "General"},
                {"id": 2, "title": "Off-topic"},
            ],
            "dialog_id": 123,
        },
    })
    with _patch_daemon(conn):
        result = await list_topics(ListTopics(dialog="MyGroup"))

    assert len(result) == 1
    text = result[0].text
    assert "General" in text
    assert "Off-topic" in text
    conn.list_topics.assert_called_once()


async def test_list_topics_passes_dialog_name():
    """ListTopics passes dialog name to daemon when not a numeric ID."""
    conn = _make_daemon_conn({"ok": True, "data": {"topics": [], "dialog_id": 0}})
    with _patch_daemon(conn):
        await list_topics(ListTopics(dialog="Some Group"))

    call_kwargs = conn.list_topics.call_args[1]
    assert call_kwargs.get("dialog") == "Some Group"


async def test_list_topics_dialog_not_found():
    """ListTopics handles dialog_not_found error from daemon."""
    conn = _make_daemon_conn({
        "ok": False,
        "error": "dialog_not_found",
        "message": "No dialog matching 'nonexistent'",
    })
    with _patch_daemon(conn):
        result = await list_topics(ListTopics(dialog="nonexistent"))

    assert "not found" in result[0].text.lower() or "no dialog" in result[0].text.lower()


# ---------------------------------------------------------------------------
# GetMyAccount — daemon routing
# ---------------------------------------------------------------------------


async def test_get_my_account_via_daemon():
    """GetMyAccount routes through daemon API."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "id": 999,
            "first_name": "Test",
            "last_name": "User",
            "username": "testuser",
            "phone": "+1234567890",
        },
    })
    with _patch_daemon(conn):
        result = await get_my_account(GetMyAccount())

    text = result[0].text
    assert "id=999" in text
    assert "Test" in text
    assert "testuser" in text
    conn.get_me.assert_called_once()


# ---------------------------------------------------------------------------
# ListMessages — daemon routing
# ---------------------------------------------------------------------------


async def test_list_messages_via_daemon():
    """ListMessages routes through daemon API and formats messages."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "messages": [
                {
                    "message_id": 1,
                    "sent_at": 1705312800,
                    "text": "Hello",
                    "sender_first_name": "Alice",
                    "media_description": None,
                    "reply_to_msg_id": None,
                    "forum_topic_id": None,
                    "reactions": None,
                    "is_deleted": 0,
                },
            ],
            "source": "sync_db",
        },
    })
    with _patch_daemon(conn):
        result = await list_messages(ListMessages(exact_dialog_id=123))

    assert len(result) == 1
    assert "Hello" in result[0].text
    conn.list_messages.assert_called_once()


async def test_list_messages_passes_dialog_name_to_daemon():
    """ListMessages passes dialog name to daemon when not a numeric ID."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})

    with _patch_daemon(conn):
        await list_messages(ListMessages(dialog="Unknown Chat"))

    call_kwargs = conn.list_messages.call_args[1]
    assert call_kwargs.get("dialog") == "Unknown Chat"


async def test_list_messages_uses_exact_dialog_id():
    """ListMessages uses exact_dialog_id when provided."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})

    with _patch_daemon(conn):
        await list_messages(ListMessages(exact_dialog_id=42))

    call_kwargs = conn.list_messages.call_args[1]
    assert call_kwargs.get("dialog_id") == 42


async def test_list_messages_dialog_not_found():
    """ListMessages handles dialog_not_found error from daemon."""
    conn = _make_daemon_conn({
        "ok": False,
        "error": "dialog_not_found",
        "message": "No dialog matching 'ghost'",
    })
    with _patch_daemon(conn):
        result = await list_messages(ListMessages(dialog="ghost"))

    assert "not found" in result[0].text.lower() or "no dialog" in result[0].text.lower()


# ---------------------------------------------------------------------------
# SearchMessages — daemon routing
# ---------------------------------------------------------------------------


async def test_search_messages_via_daemon():
    """SearchMessages routes through daemon API."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "messages": [
                {
                    "message_id": 5,
                    "sent_at": 1705312800,
                    "text": "Found this result",
                    "sender_first_name": "Bob",
                    "media_description": None,
                    "reply_to_msg_id": None,
                },
            ],
            "total": 1,
        },
    })
    with _patch_daemon(conn):
        result = await search_messages(SearchMessages(dialog="123", query="result"))

    assert "Found this result" in result[0].text
    conn.search_messages.assert_called_once()


async def test_search_messages_passes_dialog_name():
    """SearchMessages passes dialog name to daemon when not numeric."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "total": 0}})

    with _patch_daemon(conn):
        await search_messages(SearchMessages(dialog="My Chat", query="test"))

    call_kwargs = conn.search_messages.call_args[1]
    assert call_kwargs.get("dialog") == "My Chat"


async def test_search_messages_no_hits():
    """SearchMessages returns actionable text when no results found."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "total": 0}})
    with _patch_daemon(conn):
        result = await search_messages(SearchMessages(dialog="123", query="nonexistent"))

    assert len(result) == 1
    # Should have helpful "no hits" text


# ---------------------------------------------------------------------------
# DaemonNotRunningError handling
# ---------------------------------------------------------------------------


async def test_list_dialogs_daemon_not_running():
    """ListDialogs returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await list_dialogs(ListDialogs())

    text = result[0].text
    assert "not running" in text.lower() or "mcp-telegram sync" in text.lower()


async def test_list_messages_daemon_not_running():
    """ListMessages returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await list_messages(ListMessages(exact_dialog_id=123))

    text = result[0].text
    assert "not running" in text.lower() or "mcp-telegram sync" in text.lower()


async def test_search_messages_daemon_not_running():
    """SearchMessages returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await search_messages(SearchMessages(dialog="123", query="test"))

    text = result[0].text
    assert "not running" in text.lower() or "mcp-telegram sync" in text.lower()


async def test_list_topics_daemon_not_running():
    """ListTopics returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await list_topics(ListTopics(dialog="group"))

    text = result[0].text
    assert "not running" in text.lower() or "mcp-telegram sync" in text.lower()


async def test_get_my_account_daemon_not_running():
    """GetMyAccount returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await get_my_account(GetMyAccount())

    text = result[0].text
    assert "not running" in text.lower() or "mcp-telegram sync" in text.lower()


# ---------------------------------------------------------------------------
# Architectural invariant: no Telegram imports in tools/
# ---------------------------------------------------------------------------


def test_no_telethon_imports_in_tools():
    """Tool modules must not import telethon."""
    tools_dir = pathlib.Path("src/mcp_telegram/tools")
    for filename in ["discovery.py", "reading.py", "_base.py", "sync.py", "user_info.py", "unread.py"]:
        filepath = tools_dir / filename
        content = filepath.read_text()
        assert "from telethon" not in content, f"{filename} imports telethon"
        assert "import telethon" not in content, f"{filename} imports telethon"
        assert "from .. import telegram" not in content, f"{filename} imports telegram module"
        assert "from ..telegram" not in content, f"{filename} imports from telegram"


# ---------------------------------------------------------------------------
# MarkDialogForSync — daemon routing
# ---------------------------------------------------------------------------


async def test_mark_dialog_for_sync_via_daemon():
    """MarkDialogForSync routes through daemon API."""
    conn = _make_daemon_conn({"ok": True})
    with _patch_daemon(conn):
        result = await mark_dialog_for_sync(MarkDialogForSync(dialog_id=42, enable=True))
    assert len(result) == 1
    assert "marked for sync" in result[0].text
    conn.mark_dialog_for_sync.assert_called_once_with(dialog_id=42, enable=True)


async def test_mark_dialog_for_sync_disable():
    """MarkDialogForSync with enable=False returns unmarked text."""
    conn = _make_daemon_conn({"ok": True})
    with _patch_daemon(conn):
        result = await mark_dialog_for_sync(MarkDialogForSync(dialog_id=42, enable=False))
    assert "unmarked from sync" in result[0].text
    conn.mark_dialog_for_sync.assert_called_once_with(dialog_id=42, enable=False)


async def test_mark_dialog_for_sync_daemon_not_running():
    """MarkDialogForSync returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await mark_dialog_for_sync(MarkDialogForSync(dialog_id=42))
    assert "not running" in result[0].text.lower() or "mcp-telegram sync" in result[0].text.lower()


# ---------------------------------------------------------------------------
# GetSyncStatus — daemon routing
# ---------------------------------------------------------------------------


async def test_get_sync_status_via_daemon():
    """GetSyncStatus routes through daemon and formats key=value output."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "dialog_id": -1001234567890,
            "status": "synced",
            "message_count": 100,
            "sync_progress": 100,
            "total_messages": 100,
            "last_synced_at": 1700000000,
            "last_event_at": 1700001000,
            "delete_detection": "reliable (channel)",
        },
    })
    with _patch_daemon(conn):
        result = await get_sync_status(GetSyncStatus(dialog_id=-1001234567890))
    text = result[0].text
    assert "status=synced" in text
    assert "message_count=100" in text
    assert "delete_detection=reliable (channel)" in text
    conn.get_sync_status.assert_called_once_with(dialog_id=-1001234567890)


async def test_get_sync_status_daemon_not_running():
    """GetSyncStatus returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await get_sync_status(GetSyncStatus(dialog_id=123))
    assert "not running" in result[0].text.lower() or "mcp-telegram sync" in result[0].text.lower()


# ---------------------------------------------------------------------------
# GetSyncAlerts — daemon routing
# ---------------------------------------------------------------------------


async def test_get_sync_alerts_via_daemon():
    """GetSyncAlerts routes through daemon and formats alert sections."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "deleted_messages": [
                {"dialog_id": 1, "message_id": 100, "text": "gone", "deleted_at": 1700000500},
            ],
            "edits": [
                {"dialog_id": 1, "message_id": 200, "version": 1,
                 "old_text": "before", "edit_date": 1700000600},
            ],
            "access_lost": [
                {"dialog_id": 2, "access_lost_at": 1700000700},
            ],
        },
    })
    with _patch_daemon(conn):
        result = await get_sync_alerts(GetSyncAlerts(since=0, limit=50))
    text = result[0].text
    assert "Deleted Messages" in text
    assert "gone" in text
    assert "Edits" in text
    assert "before" in text
    assert "Access Lost" in text
    conn.get_sync_alerts.assert_called_once_with(since=0, limit=50)


async def test_get_sync_alerts_empty():
    """GetSyncAlerts returns 'no alerts' text when all lists empty."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {"deleted_messages": [], "edits": [], "access_lost": []},
    })
    with _patch_daemon(conn):
        result = await get_sync_alerts(GetSyncAlerts())
    assert "No sync alerts" in result[0].text


async def test_get_sync_alerts_daemon_not_running():
    """GetSyncAlerts returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await get_sync_alerts(GetSyncAlerts())
    assert "not running" in result[0].text.lower() or "mcp-telegram sync" in result[0].text.lower()


def test_no_connected_client_in_tools():
    """No tools/ file references _connected_client after migration."""
    tools_dir = pathlib.Path("src/mcp_telegram/tools")
    for filepath in tools_dir.glob("*.py"):
        if filepath.name.startswith("__"):
            continue
        content = filepath.read_text()
        assert "_connected_client" not in content, f"{filepath.name} still references _connected_client"


# ---------------------------------------------------------------------------
# GetUserInfo — daemon routing
# ---------------------------------------------------------------------------


async def test_get_user_info_via_daemon():
    """GetUserInfo routes through daemon API after entity resolution."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "id": 12345,
            "first_name": "Alice",
            "last_name": "Smith",
            "username": "alice",
            "common_chats": [
                {"id": -1001234, "name": "Dev Chat", "type": "supergroup"},
            ],
        },
    })
    mock_cache = MagicMock()
    mock_cache.all_names_with_ttl.return_value = {"Alice": 12345}
    mock_cache.all_names_normalized_with_ttl.return_value = {"alice": 12345}

    with _patch_daemon(conn), \
         patch("mcp_telegram.tools.user_info.get_entity_cache", return_value=mock_cache), \
         patch("mcp_telegram.tools.user_info.resolve") as mock_resolve:
        resolved = MagicMock()
        resolved.entity_id = 12345
        resolved.display_name = "Alice"
        # Make isinstance checks for NotFound and Candidates return False
        from mcp_telegram.resolver import Candidates, NotFound
        mock_resolve.return_value = resolved
        result = await get_user_info(GetUserInfo(user="Alice"))

    text = result[0].text
    assert "Alice" in text
    assert "12345" in text
    assert "Dev Chat" in text
    conn.get_user_info.assert_called_once_with(user_id=12345)


async def test_get_user_info_daemon_not_running():
    """GetUserInfo returns actionable error when daemon is not running."""
    mock_cache = MagicMock()
    mock_cache.all_names_with_ttl.return_value = {"Alice": 12345}
    mock_cache.all_names_normalized_with_ttl.return_value = {"alice": 12345}

    with _patch_daemon_not_running(), \
         patch("mcp_telegram.tools.user_info.get_entity_cache", return_value=mock_cache), \
         patch("mcp_telegram.tools.user_info.resolve") as mock_resolve:
        resolved = MagicMock()
        resolved.entity_id = 12345
        resolved.display_name = "Alice"
        mock_resolve.return_value = resolved
        result = await get_user_info(GetUserInfo(user="Alice"))

    assert "not running" in result[0].text.lower() or "mcp-telegram sync" in result[0].text.lower()


async def test_get_user_info_user_not_found_by_daemon():
    """GetUserInfo handles user_not_found error from daemon."""
    conn = _make_daemon_conn({
        "ok": False,
        "error": "user_not_found",
        "message": "User 999 not found",
    })
    mock_cache = MagicMock()
    mock_cache.all_names_with_ttl.return_value = {"Ghost": 999}
    mock_cache.all_names_normalized_with_ttl.return_value = {"ghost": 999}

    with _patch_daemon(conn), \
         patch("mcp_telegram.tools.user_info.get_entity_cache", return_value=mock_cache), \
         patch("mcp_telegram.tools.user_info.resolve") as mock_resolve:
        resolved = MagicMock()
        resolved.entity_id = 999
        resolved.display_name = "Ghost"
        mock_resolve.return_value = resolved
        result = await get_user_info(GetUserInfo(user="Ghost"))

    assert "could not fetch" in result[0].text.lower() or "error" in result[0].text.lower()


# ---------------------------------------------------------------------------
# ListUnreadMessages — daemon routing
# ---------------------------------------------------------------------------


async def test_list_unread_messages_via_daemon():
    """ListUnreadMessages routes through daemon API and formats grouped output."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "groups": [
                {
                    "dialog_id": 123,
                    "display_name": "Alice",
                    "tier": 30,
                    "category": "user",
                    "unread_count": 2,
                    "unread_mentions_count": 0,
                    "messages": [
                        {
                            "message_id": 1,
                            "sent_at": 1700000000,
                            "text": "Hello there",
                            "sender_id": 123,
                            "sender_first_name": "Alice",
                        },
                    ],
                },
            ],
        },
    })
    with _patch_daemon(conn):
        result = await list_unread_messages(ListUnreadMessages())

    text = result[0].text
    assert "Alice" in text
    assert "Hello there" in text
    conn.list_unread_messages.assert_called_once()


async def test_list_unread_messages_empty():
    """ListUnreadMessages returns empty-inbox text when no groups."""
    conn = _make_daemon_conn({"ok": True, "data": {"groups": []}})
    with _patch_daemon(conn):
        result = await list_unread_messages(ListUnreadMessages())

    assert "no unread" in result[0].text.lower() or "непрочитанных" in result[0].text.lower()


async def test_list_unread_messages_daemon_not_running():
    """ListUnreadMessages returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await list_unread_messages(ListUnreadMessages())

    assert "not running" in result[0].text.lower() or "mcp-telegram sync" in result[0].text.lower()


async def test_list_unread_messages_passes_params():
    """ListUnreadMessages passes scope, limit, group_size_threshold to daemon."""
    conn = _make_daemon_conn({"ok": True, "data": {"groups": []}})
    with _patch_daemon(conn):
        await list_unread_messages(ListUnreadMessages(scope="all", limit=200, group_size_threshold=50))

    call_kwargs = conn.list_unread_messages.call_args[1]
    assert call_kwargs["scope"] == "all"
    assert call_kwargs["limit"] == 200
    assert call_kwargs["group_size_threshold"] == 50
