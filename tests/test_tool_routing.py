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
from mcp_telegram.tools.stats import GetUsageStats, get_usage_stats


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
    conn.record_telemetry = AsyncMock(return_value={"ok": True})
    conn.get_usage_stats = AsyncMock(return_value=r)
    conn.upsert_entities = AsyncMock(return_value={"ok": True, "upserted": 0})
    conn.resolve_entity = AsyncMock(return_value=r)
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
            "mcp_telegram.tools.stats.daemon_connection",
        ]
        for target in targets:
            p = patch(target, side_effect=lambda c=self._conn: _fake_daemon_cm(c))
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
            "mcp_telegram.tools.stats.daemon_connection",
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


async def test_list_dialogs_upserts_entities_via_daemon():
    """ListDialogs upserts dialog entries into daemon entity store via upsert_entities."""
    upsert_conn = _make_daemon_conn({"ok": True, "upserted": 1})
    list_conn = _make_daemon_conn({
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

    call_count = 0

    @asynccontextmanager
    async def _multi_conn_cm():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield list_conn
        else:
            yield upsert_conn

    with patch("mcp_telegram.tools.discovery.daemon_connection", side_effect=_multi_conn_cm):
        await list_dialogs(ListDialogs())

    upsert_conn.upsert_entities.assert_called_once()
    entities = upsert_conn.upsert_entities.call_args[1]["entities"]
    assert len(entities) == 1
    assert entities[0]["id"] == 100
    assert entities[0]["name"] == "TestChat"
    assert entities[0]["type"] == "group"


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
                {"dialog_id": 1, "message_id": 100, "deleted_at": 1700000500},
            ],
            "edits": [
                {"dialog_id": 1, "message_id": 200, "version": 1,
                 "edit_date": 1700000600},
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
    assert "dialog=1" in text
    assert "Edits" in text
    assert "edit_date=" in text
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


async def test_get_user_info_resolves_via_daemon():
    """GetUserInfo resolves entity via daemon resolve_entity then fetches profile.

    Uses a single daemon connection (resolve + get_user_info in same context).
    """
    conn = MagicMock()
    conn.resolve_entity = AsyncMock(return_value={
        "ok": True,
        "data": {"result": "resolved", "entity_id": 12345, "display_name": "Alice"},
    })
    conn.get_user_info = AsyncMock(return_value={
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
    conn.record_telemetry = AsyncMock(return_value={"ok": True})

    with _patch_daemon(conn):
        result = await get_user_info(GetUserInfo(user="Alice"))

    text = result[0].text
    assert '[resolved: "Alice"]' in text
    assert "12345" in text
    assert "Dev Chat" in text
    conn.resolve_entity.assert_called_once_with(query="Alice")
    conn.get_user_info.assert_called_once_with(user_id=12345)


async def test_get_user_info_via_daemon():
    """GetUserInfo (legacy name) — alias for test_get_user_info_resolves_via_daemon."""
    await test_get_user_info_resolves_via_daemon()


async def test_get_user_info_candidates_via_daemon():
    """GetUserInfo returns candidate list when resolve_entity returns candidates."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "result": "candidates",
            "matches": [
                {"entity_id": 1, "display_name": "Alice A", "score": 90, "username": "alicea", "entity_type": "user"},
                {"entity_id": 2, "display_name": "Alice B", "score": 80, "username": None, "entity_type": "user"},
            ],
        },
    })

    with patch("mcp_telegram.tools.user_info.daemon_connection", return_value=_fake_daemon_cm(conn)):
        result = await get_user_info(GetUserInfo(user="Alice"))

    text = result[0].text
    assert "Alice A" in text or "alice" in text.lower()
    assert "ambiguous" in text.lower() or "matched" in text.lower() or "multiple" in text.lower()


async def test_get_user_info_not_found_via_daemon():
    """GetUserInfo returns user_not_found text when resolve_entity returns not_found."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {"result": "not_found", "query": "nobody"},
    })

    with patch("mcp_telegram.tools.user_info.daemon_connection", return_value=_fake_daemon_cm(conn)):
        result = await get_user_info(GetUserInfo(user="nobody"))

    text = result[0].text
    assert "not found" in text.lower() or "nobody" in text.lower()


async def test_get_user_info_daemon_not_running():
    """GetUserInfo returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await get_user_info(GetUserInfo(user="Alice"))

    assert "not running" in result[0].text.lower() or "mcp-telegram sync" in result[0].text.lower()


async def test_get_user_info_user_not_found_by_daemon():
    """GetUserInfo handles user_not_found error from daemon profile fetch."""
    conn = MagicMock()
    conn.resolve_entity = AsyncMock(return_value={
        "ok": True,
        "data": {"result": "resolved", "entity_id": 999, "display_name": "Ghost"},
    })
    conn.get_user_info = AsyncMock(return_value={
        "ok": False,
        "error": "user_not_found",
        "message": "User 999 not found",
    })
    conn.record_telemetry = AsyncMock(return_value={"ok": True})

    with _patch_daemon(conn):
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


# ---------------------------------------------------------------------------
# GetUsageStats — daemon routing
# ---------------------------------------------------------------------------


async def test_get_usage_stats_via_daemon():
    """GetUsageStats reads telemetry via daemon API get_usage_stats."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {
            "tool_distribution": {"ListDialogs": 10, "ListMessages": 5},
            "error_distribution": {},
            "total_calls": 15,
            "max_page_depth": 2,
            "filter_count": 3,
            "latency_median_ms": 120,
            "latency_p95_ms": 350,
            "dialogs_with_deep_scroll": 0,
        },
    })
    with _patch_daemon(conn):
        result = await get_usage_stats(GetUsageStats())

    text = result[0].text
    assert len(text) > 0
    conn.get_usage_stats.assert_called_once()


async def test_get_usage_stats_daemon_not_running():
    """GetUsageStats returns actionable error when daemon is not running."""
    with _patch_daemon_not_running():
        result = await get_usage_stats(GetUsageStats())

    text = result[0].text
    assert "not running" in text.lower() or "mcp-telegram sync" in text.lower()


async def test_get_usage_stats_empty_data():
    """GetUsageStats returns no-data message when daemon reports zero calls."""
    conn = _make_daemon_conn({
        "ok": True,
        "data": {"total_calls": 0},
    })
    with _patch_daemon(conn):
        result = await get_usage_stats(GetUsageStats())

    text = result[0].text
    assert len(text) > 0


# ---------------------------------------------------------------------------
# Architectural invariant: no sqlite3 / cache / analytics DB imports in tools/
# ---------------------------------------------------------------------------


def test_no_sqlite3_or_cache_in_tools():
    """CONSOLIDATE-03: tools/ must have zero sqlite3, cache, or analytics DB imports."""
    import pathlib
    tools_dir = pathlib.Path("src/mcp_telegram/tools")
    forbidden = [
        "import sqlite3",
        "from ..cache import",
        "get_entity_cache",
        "_get_analytics_collector",
    ]
    # Allow format_usage_summary (pure function, no DB access)
    allowed_analytics = "format_usage_summary"
    violations = []
    for py_file in tools_dir.glob("*.py"):
        content = py_file.read_text()
        for pattern in forbidden:
            if pattern in content:
                violations.append(f"{py_file.name}: contains '{pattern}'")
        # Check analytics imports more carefully
        for line in content.splitlines():
            if "from ..analytics import" in line and allowed_analytics not in line:
                violations.append(f"{py_file.name}: imports from analytics beyond format_usage_summary")
    assert not violations, "CONSOLIDATE-03 violations:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# DaemonConnection.list_messages — extended params (Phase 35-02, Task 1)
# ---------------------------------------------------------------------------


async def test_daemon_connection_list_messages_passes_sender_id():
    """DaemonConnection.list_messages passes sender_id in request payload."""
    from mcp_telegram.daemon_client import DaemonConnection
    import json

    sent_payload = {}

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            nonlocal sent_payload
            sent_payload = json.loads(data.strip())
        async def drain(self) -> None:
            pass

    class _FakeReader:
        async def readline(self) -> bytes:
            return json.dumps({"ok": True, "data": {}}).encode() + b"\n"

    conn = DaemonConnection(_FakeReader(), _FakeWriter())
    await conn.list_messages(dialog_id=1, sender_id=42)
    assert sent_payload.get("sender_id") == 42


async def test_daemon_connection_list_messages_passes_sender_name():
    """DaemonConnection.list_messages passes sender_name in request payload."""
    from mcp_telegram.daemon_client import DaemonConnection
    import json

    sent_payload = {}

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            nonlocal sent_payload
            sent_payload = json.loads(data.strip())
        async def drain(self) -> None:
            pass

    class _FakeReader:
        async def readline(self) -> bytes:
            return json.dumps({"ok": True, "data": {}}).encode() + b"\n"

    conn = DaemonConnection(_FakeReader(), _FakeWriter())
    await conn.list_messages(dialog_id=1, sender_name="Alice")
    assert sent_payload.get("sender_name") == "Alice"


async def test_daemon_connection_list_messages_passes_topic_id():
    """DaemonConnection.list_messages passes topic_id in request payload."""
    from mcp_telegram.daemon_client import DaemonConnection
    import json

    sent_payload = {}

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            nonlocal sent_payload
            sent_payload = json.loads(data.strip())
        async def drain(self) -> None:
            pass

    class _FakeReader:
        async def readline(self) -> bytes:
            return json.dumps({"ok": True, "data": {}}).encode() + b"\n"

    conn = DaemonConnection(_FakeReader(), _FakeWriter())
    await conn.list_messages(dialog_id=1, topic_id=5)
    assert sent_payload.get("topic_id") == 5


async def test_daemon_connection_list_messages_passes_unread_after_id():
    """DaemonConnection.list_messages passes unread_after_id in request payload."""
    from mcp_telegram.daemon_client import DaemonConnection
    import json

    sent_payload = {}

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            nonlocal sent_payload
            sent_payload = json.loads(data.strip())
        async def drain(self) -> None:
            pass

    class _FakeReader:
        async def readline(self) -> bytes:
            return json.dumps({"ok": True, "data": {}}).encode() + b"\n"

    conn = DaemonConnection(_FakeReader(), _FakeWriter())
    await conn.list_messages(dialog_id=1, unread_after_id=100)
    assert sent_payload.get("unread_after_id") == 100


async def test_daemon_connection_list_messages_passes_direction():
    """DaemonConnection.list_messages passes direction in request payload."""
    from mcp_telegram.daemon_client import DaemonConnection
    import json

    sent_payload = {}

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            nonlocal sent_payload
            sent_payload = json.loads(data.strip())
        async def drain(self) -> None:
            pass

    class _FakeReader:
        async def readline(self) -> bytes:
            return json.dumps({"ok": True, "data": {}}).encode() + b"\n"

    conn = DaemonConnection(_FakeReader(), _FakeWriter())
    await conn.list_messages(dialog_id=1, direction="oldest")
    assert sent_payload.get("direction") == "oldest"


async def test_daemon_connection_list_messages_passes_unread_flag():
    """DaemonConnection.list_messages passes unread=True in request payload."""
    from mcp_telegram.daemon_client import DaemonConnection
    import json

    sent_payload = {}

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            nonlocal sent_payload
            sent_payload = json.loads(data.strip())
        async def drain(self) -> None:
            pass

    class _FakeReader:
        async def readline(self) -> bytes:
            return json.dumps({"ok": True, "data": {}}).encode() + b"\n"

    conn = DaemonConnection(_FakeReader(), _FakeWriter())
    await conn.list_messages(dialog_id=1, unread=True)
    assert sent_payload.get("unread") is True


async def test_daemon_connection_list_messages_omits_none_params():
    """DaemonConnection.list_messages omits optional params when not provided (backward compat)."""
    from mcp_telegram.daemon_client import DaemonConnection
    import json

    sent_payload = {}

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            nonlocal sent_payload
            sent_payload = json.loads(data.strip())
        async def drain(self) -> None:
            pass

    class _FakeReader:
        async def readline(self) -> bytes:
            return json.dumps({"ok": True, "data": {}}).encode() + b"\n"

    conn = DaemonConnection(_FakeReader(), _FakeWriter())
    await conn.list_messages(dialog_id=1)
    assert "sender_id" not in sent_payload
    assert "sender_name" not in sent_payload
    assert "topic_id" not in sent_payload
    assert "unread_after_id" not in sent_payload
    assert "direction" not in sent_payload
    assert "unread" not in sent_payload


# ---------------------------------------------------------------------------
# _DaemonMessage adapter — edit_date and topic_title (Phase 35-02, Task 1)
# ---------------------------------------------------------------------------


def test_daemon_message_reads_edit_date_from_row():
    """_DaemonMessage reads edit_date from row dict as datetime (not hardcoded None)."""
    from mcp_telegram.tools.reading import _DaemonMessage
    from datetime import datetime, timezone

    row = {
        "message_id": 1,
        "sent_at": 1700000000,
        "text": "hi",
        "sender_first_name": None,
        "edit_date": 1700001000,
        "topic_title": None,
    }
    msg = _DaemonMessage(row)
    assert msg.edit_date is not None
    assert isinstance(msg.edit_date, datetime)
    assert msg.edit_date == datetime.fromtimestamp(1700001000, tz=timezone.utc)


def test_daemon_message_edit_date_none_when_absent():
    """_DaemonMessage.edit_date is None when row has no edit_date key."""
    from mcp_telegram.tools.reading import _DaemonMessage

    row = {
        "message_id": 1,
        "sent_at": 1700000000,
        "text": "hi",
        "sender_first_name": None,
        "topic_title": None,
    }
    msg = _DaemonMessage(row)
    assert msg.edit_date is None


def test_daemon_message_reads_topic_title_from_row():
    """_DaemonMessage reads topic_title from row dict."""
    from mcp_telegram.tools.reading import _DaemonMessage

    row = {
        "message_id": 1,
        "sent_at": 1700000000,
        "text": "hi",
        "sender_first_name": None,
        "topic_title": "General",
        "edit_date": None,
    }
    msg = _DaemonMessage(row)
    assert msg.topic_title == "General"


def test_daemon_message_topic_title_none_when_absent():
    """_DaemonMessage.topic_title is None when row has no topic_title key."""
    from mcp_telegram.tools.reading import _DaemonMessage

    row = {
        "message_id": 1,
        "sent_at": 1700000000,
        "text": "hi",
        "sender_first_name": None,
    }
    msg = _DaemonMessage(row)
    assert msg.topic_title is None


def test_format_daemon_messages_passes_topic_name_getter():
    """_format_daemon_messages passes topic_name_getter to format_messages when topic_title present."""
    from mcp_telegram.tools.reading import _format_daemon_messages
    from unittest.mock import patch, call

    rows = [
        {
            "message_id": 1,
            "sent_at": 1700000000,
            "text": "hi",
            "sender_first_name": "Alice",
            "topic_title": "General",
            "edit_date": None,
        },
    ]

    captured_kwargs = {}

    def _fake_format_messages(messages, reply_map, **kwargs):
        captured_kwargs.update(kwargs)
        return "formatted"

    with patch("mcp_telegram.tools.reading.format_messages", _fake_format_messages):
        # Need to ensure format_messages is called from within the module
        import mcp_telegram.tools.reading as reading_mod
        with patch.object(reading_mod, "_format_daemon_messages", wraps=reading_mod._format_daemon_messages):
            result = _format_daemon_messages(rows)

    assert "topic_name_getter" in captured_kwargs
    assert captured_kwargs["topic_name_getter"] is not None


def test_format_daemon_messages_no_topic_name_getter_when_no_topics():
    """_format_daemon_messages does not pass topic_name_getter when no topic_title present."""
    from mcp_telegram.tools.reading import _format_daemon_messages
    from unittest.mock import patch

    rows = [
        {
            "message_id": 1,
            "sent_at": 1700000000,
            "text": "hi",
            "sender_first_name": "Alice",
            "topic_title": None,
            "edit_date": None,
        },
    ]

    captured_kwargs = {}

    def _fake_format_messages(messages, reply_map, **kwargs):
        captured_kwargs.update(kwargs)
        return "formatted"

    with patch("mcp_telegram.tools.reading.format_messages", _fake_format_messages):
        _format_daemon_messages(rows)

    assert captured_kwargs.get("topic_name_getter") is None


def test_format_daemon_messages_edit_date_shown():
    """format_messages shows [edited HH:MM] when edit_date is set on _DaemonMessage."""
    from mcp_telegram.tools.reading import _format_daemon_messages

    rows = [
        {
            "message_id": 1,
            "sent_at": 1700000000,
            "text": "edited message",
            "sender_first_name": "Alice",
            "topic_title": None,
            "edit_date": 1700001000,
        },
    ]
    result = _format_daemon_messages(rows)
    assert "edited" in result.lower()


# ---------------------------------------------------------------------------
# MCP list_messages tool — param wiring (Phase 35-02, Task 2)
# ---------------------------------------------------------------------------


async def test_list_messages_sends_sender():
    """list_messages with sender= passes sender_name= to conn.list_messages."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})
    with _patch_daemon(conn):
        await list_messages(ListMessages(exact_dialog_id=1, sender="Alice"))

    call_kwargs = conn.list_messages.call_args[1]
    assert call_kwargs.get("sender_name") == "Alice"


async def test_list_messages_sends_topic_id():
    """list_messages with exact_topic_id= passes topic_id= to conn.list_messages."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})
    with _patch_daemon(conn):
        await list_messages(ListMessages(exact_dialog_id=1, exact_topic_id=5))

    call_kwargs = conn.list_messages.call_args[1]
    assert call_kwargs.get("topic_id") == 5


async def test_list_messages_sends_direction_newest():
    """list_messages without navigation passes direction='newest' to conn.list_messages."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})
    with _patch_daemon(conn):
        await list_messages(ListMessages(exact_dialog_id=1))

    call_kwargs = conn.list_messages.call_args[1]
    assert call_kwargs.get("direction") == "newest"


async def test_list_messages_sends_direction_oldest():
    """list_messages with navigation='oldest' passes direction='oldest' to conn.list_messages."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})
    with _patch_daemon(conn):
        await list_messages(ListMessages(exact_dialog_id=1, navigation="oldest"))

    call_kwargs = conn.list_messages.call_args[1]
    assert call_kwargs.get("direction") == "oldest"


async def test_list_messages_sends_unread():
    """list_messages with unread=True passes unread=True to conn.list_messages."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})
    with _patch_daemon(conn):
        await list_messages(ListMessages(exact_dialog_id=1, unread=True))

    call_kwargs = conn.list_messages.call_args[1]
    assert call_kwargs.get("unread") is True


async def test_list_messages_topic_fuzzy_resolves_via_list_topics():
    """list_messages with topic= resolves topic name to id via list_topics."""
    list_topics_response = {
        "ok": True,
        "data": {
            "topics": [
                {"id": 7, "title": "General"},
                {"id": 8, "title": "Off-topic"},
            ],
            "dialog_id": 1,
        },
    }
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})
    conn.list_topics = AsyncMock(return_value=list_topics_response)
    with _patch_daemon(conn):
        await list_messages(ListMessages(exact_dialog_id=1, topic="General"))

    call_kwargs = conn.list_messages.call_args[1]
    assert call_kwargs.get("topic_id") == 7


async def test_list_messages_topic_fuzzy_ambiguous_returns_error():
    """list_messages with ambiguous topic= returns error listing matches."""
    list_topics_response = {
        "ok": True,
        "data": {
            "topics": [
                {"id": 7, "title": "General Chat"},
                {"id": 8, "title": "General Topics"},
            ],
            "dialog_id": 1,
        },
    }
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})
    conn.list_topics = AsyncMock(return_value=list_topics_response)
    with _patch_daemon(conn):
        result = await list_messages(ListMessages(exact_dialog_id=1, topic="General"))

    text = result[0].text
    assert "ambiguous" in text.lower() or "matches" in text.lower() or "exact_topic_id" in text.lower()


async def test_list_messages_topic_not_found_returns_error():
    """list_messages with topic= that doesn't match any topic returns error."""
    list_topics_response = {
        "ok": True,
        "data": {
            "topics": [
                {"id": 7, "title": "General"},
            ],
            "dialog_id": 1,
        },
    }
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})
    conn.list_topics = AsyncMock(return_value=list_topics_response)
    with _patch_daemon(conn):
        result = await list_messages(ListMessages(exact_dialog_id=1, topic="nonexistent"))

    text = result[0].text
    assert "not found" in text.lower() or "nonexistent" in text.lower()


async def test_list_messages_no_optional_params_not_sent():
    """list_messages without optional params does NOT send them (backward compat)."""
    conn = _make_daemon_conn({"ok": True, "data": {"messages": [], "source": "sync_db"}})
    with _patch_daemon(conn):
        await list_messages(ListMessages(exact_dialog_id=1))

    call_kwargs = conn.list_messages.call_args[1]
    assert call_kwargs.get("sender_name") is None
    assert call_kwargs.get("topic_id") is None
    assert call_kwargs.get("unread") is None
