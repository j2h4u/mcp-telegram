"""Tests for tools/entity_info.py — MCP tool surface (Phase 47).

SPEC Reqs covered: 1 (registration smoke + tool renders), 3 (common envelope
rendered), 4-7 (per-type rendering), and end-to-end resolver behavior.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.tools.entity_info import GetEntityInfo, get_entity_info


def _resolve_ok(entity_id=42, display_name="Alice"):
    return {
        "ok": True,
        "data": {
            "result": "match",
            "entity_id": entity_id,
            "display_name": display_name,
        },
    }


def _patch_daemon(resolve_response, get_entity_info_response):
    """Patch daemon_connection so the tool sees scripted responses."""
    conn1 = MagicMock()
    conn1.resolve_entity = AsyncMock(return_value=resolve_response)
    conn2 = MagicMock()
    conn2.get_entity_info = AsyncMock(return_value=get_entity_info_response)

    connections = iter([conn1, conn2])

    @asynccontextmanager
    async def fake_daemon_connection():
        try:
            yield next(connections)
        except StopIteration:
            raise AssertionError("more daemon_connection calls than scripted")

    return patch("mcp_telegram.tools.entity_info.daemon_connection", fake_daemon_connection)


@pytest.mark.asyncio
async def test_get_entity_info_user_renders() -> None:
    get_resp = {
        "ok": True,
        "data": {
            "id": 42, "type": "user", "name": "Alice Smith",
            "username": "alice", "about": "QA engineer",
            "my_membership": {"is_member": True, "is_admin": False},
            "avatar_history": [], "avatar_count": 0,
            "first_name": "Alice", "last_name": "Smith", "extra_usernames": [],
            "emoji_status_id": None, "status": {"type": "online"},
            "phone": "+12025551234", "lang_code": "en",
            "contact": True, "mutual_contact": True, "close_friend": False,
            "send_paid_messages_stars": None, "personal_channel_id": None,
            "birthday": None, "verified": False, "premium": True,
            "bot": False, "scam": False, "fake": False, "restricted": False,
            "restriction_reason": [], "blocked": False, "ttl_period": None,
            "private_forward_name": None, "bot_info": None,
            "business_location": None, "business_intro": None,
            "business_work_hours": None, "note": None, "folder_id": None,
            "folder_name": None, "common_chats": [],
        },
    }
    with _patch_daemon(_resolve_ok(42, "Alice Smith"), get_resp):
        result = await get_entity_info(GetEntityInfo(entity="Alice"))
    text = result[0].text
    assert "type=user" in text
    assert "name='Alice Smith'" in text
    assert "username=@alice" in text
    assert "about: QA engineer" in text
    assert "phone: +12025551234 (US)" in text
    assert "Common chats (0):" in text


@pytest.mark.asyncio
async def test_get_entity_info_bot_renders_type_bot() -> None:
    get_resp = {
        "ok": True,
        "data": {
            "id": 1, "type": "bot", "name": "MyBot", "username": "mybot",
            "about": None,
            "my_membership": {"is_member": False, "is_admin": False},
            "avatar_history": [], "avatar_count": 0,
            "first_name": "MyBot", "last_name": None, "extra_usernames": [],
            "emoji_status_id": None, "status": None,
            "phone": None, "lang_code": None,
            "contact": False, "mutual_contact": False, "close_friend": False,
            "send_paid_messages_stars": None, "personal_channel_id": None,
            "birthday": None, "verified": False, "premium": False,
            "bot": True, "scam": False, "fake": False, "restricted": False,
            "restriction_reason": [], "blocked": False, "ttl_period": None,
            "private_forward_name": None,
            "bot_info": {"description": "A test bot", "commands": [
                {"command": "start", "description": "Start"},
            ]},
            "business_location": None, "business_intro": None,
            "business_work_hours": None, "note": None, "folder_id": None,
            "folder_name": None, "common_chats": [],
        },
    }
    with _patch_daemon(_resolve_ok(1, "MyBot"), get_resp):
        result = await get_entity_info(GetEntityInfo(entity="MyBot"))
    text = result[0].text
    assert "type=bot" in text
    assert "flags: bot" in text
    assert "bot_description: A test bot" in text
    assert "bot_commands: /start" in text


@pytest.mark.asyncio
async def test_get_entity_info_channel_renders() -> None:
    get_resp = {
        "ok": True,
        "data": {
            "id": -1001, "type": "channel", "name": "News", "username": "news",
            "about": "Daily news", "my_membership": {"is_member": True, "is_admin": False},
            "avatar_history": [], "avatar_count": 0,
            "subscribers_count": 12345, "linked_chat_id": None,
            "pinned_msg_id": 999, "slow_mode_seconds": 30,
            "available_reactions": {"kind": "some", "emojis": ["👍", "❤"]},
            "restrictions": [], "contacts_subscribed": None,
            "contacts_subscribed_partial": False, "contacts_reason": "not_an_admin",
        },
    }
    with _patch_daemon(_resolve_ok(-1001, "News"), get_resp):
        result = await get_entity_info(GetEntityInfo(entity="News"))
    text = result[0].text
    assert "type=channel" in text
    assert "subscribers_count: 12345" in text
    assert "pinned_msg_id: 999" in text
    assert "slow_mode_seconds: 30" in text
    assert "available_reactions: 👍, ❤" in text
    assert "contacts_subscribed: null (reason: not_an_admin)" in text


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_renders() -> None:
    get_resp = {
        "ok": True,
        "data": {
            "id": -1002, "type": "supergroup", "name": "DevChat", "username": "devchat",
            "about": None, "my_membership": {"is_member": True, "is_admin": True},
            "avatar_history": [], "avatar_count": 0,
            "members_count": 42, "linked_broadcast_id": None,
            "slow_mode_seconds": None, "has_topics": True, "restrictions": [],
            "contacts_subscribed": [{"id": 10, "name": "Anna", "username": "anna"}],
            "contacts_subscribed_partial": False, "contacts_reason": None,
        },
    }
    with _patch_daemon(_resolve_ok(-1002, "DevChat"), get_resp):
        result = await get_entity_info(GetEntityInfo(entity="DevChat"))
    text = result[0].text
    assert "type=supergroup" in text
    assert "members_count: 42" in text
    assert "has_topics: yes" in text
    assert "contacts_subscribed (1):" in text
    assert "id=10" in text and "name='Anna'" in text


@pytest.mark.asyncio
async def test_get_entity_info_group_renders_migrated_to() -> None:
    get_resp = {
        "ok": True,
        "data": {
            "id": -100, "type": "group", "name": "Old Chat", "username": None,
            "about": None, "my_membership": {"is_member": True, "is_admin": True},
            "avatar_history": [], "avatar_count": 0,
            "members_count": 5, "migrated_to": -1002005000000,
            "invite_link": None, "restrictions": [],
            "contacts_subscribed": [], "contacts_subscribed_partial": False,
            "contacts_reason": None,
        },
    }
    with _patch_daemon(_resolve_ok(-100, "Old Chat"), get_resp):
        result = await get_entity_info(GetEntityInfo(entity="Old Chat"))
    text = result[0].text
    assert "type=group" in text
    assert "members_count: 5" in text
    assert "migrated_to: -1002005000000" in text
    assert "re-run GetEntityInfo with this id" in text


@pytest.mark.asyncio
async def test_get_entity_info_resolver_ambiguous() -> None:
    candidates_resp = {
        "ok": True,
        "data": {
            "result": "candidates",
            "matches": [
                {"entity_id": 1, "display_name": "Alice A", "score": 0.9, "username": "alicea", "entity_type": "User"},
                {"entity_id": 2, "display_name": "Alice B", "score": 0.8, "username": "aliceb", "entity_type": "User"},
            ],
        },
    }
    conn1 = MagicMock()
    conn1.resolve_entity = AsyncMock(return_value=candidates_resp)

    @asynccontextmanager
    async def fake_dc():
        yield conn1

    with patch("mcp_telegram.tools.entity_info.daemon_connection", fake_dc):
        result = await get_entity_info(GetEntityInfo(entity="Alice"))
    text = result[0].text
    assert "Multiple entities match" in text
    assert "Alice A" in text and "Alice B" in text
    assert "GetEntityInfo" in text


@pytest.mark.asyncio
async def test_get_entity_info_resolver_not_found() -> None:
    notfound_resp = {"ok": True, "data": {"result": "not_found"}}
    conn1 = MagicMock()
    conn1.resolve_entity = AsyncMock(return_value=notfound_resp)

    @asynccontextmanager
    async def fake_dc():
        yield conn1

    with patch("mcp_telegram.tools.entity_info.daemon_connection", fake_dc):
        result = await get_entity_info(GetEntityInfo(entity="Nobody"))
    text = result[0].text
    assert "No entity matches 'Nobody'" in text
    assert "GetEntityInfo" in text


@pytest.mark.asyncio
async def test_get_entity_info_daemon_not_running() -> None:
    from mcp_telegram.tools._base import DaemonNotRunningError

    @asynccontextmanager
    async def raising_dc():
        raise DaemonNotRunningError("Sync daemon is not running.")
        yield  # pragma: no cover

    with patch("mcp_telegram.tools.entity_info.daemon_connection", raising_dc):
        result = await get_entity_info(GetEntityInfo(entity="Anyone"))
    text = result[0].text
    assert "Telegram backend is not running" in text
