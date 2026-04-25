"""Tests for GetEntityInfo — Broadcast Channel kind.

SPEC Reqs covered: 2 (type=channel discriminator), 3 (common envelope),
5 (Channel field surface: subscribers_count, linked_chat_id, pinned_msg_id,
slow_mode_seconds, available_reactions, restrictions, contacts_subscribed),
9 (privacy: non-admin → contacts_subscribed=null + reason='not_an_admin'),
10 (no download keys).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# HIGH-2 from 47-REVIEWS.md cycle 3 (codex 2026-04-25): Module-level import
# of `TelethonChannel` so that tests appended by Plan 03 Task 3 (broadcast
# admin enumeration: small/large/ChatAdminRequiredError) — which reference
# `MagicMock(spec=TelethonChannel)` directly in their bodies — do not
# NameError. The `_broadcast_channel()` helper below also uses this name.
from telethon.tl.types import Channel as TelethonChannel  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import DaemonAPIServer


@pytest.fixture(autouse=True)
def _patch_get_peer_id():
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda entity: int(getattr(entity, "id", 0)),
    ):
        yield


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'not_synced',
            last_synced_at INTEGER, last_event_at INTEGER,
            sync_progress INTEGER DEFAULT 0, total_messages INTEGER,
            access_lost_at INTEGER, read_inbox_max_id INTEGER, read_outbox_max_id INTEGER
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT,
            username TEXT, name_normalized TEXT, updated_at INTEGER NOT NULL
        );
        CREATE TABLE entity_details (
            entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL,
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        ) WITHOUT ROWID;
        CREATE INDEX idx_entity_details_fetched_at ON entity_details(fetched_at);
        """
    )
    return conn


def make_server(conn=None, client=None) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    shutdown_event = asyncio.Event()
    server = DaemonAPIServer(conn, client, shutdown_event)
    server._ready = True
    return server


def _broadcast_channel(id_=-1001, **kwargs):
    # `TelethonChannel` is now imported at module scope (see top of file)
    # per HIGH-2 from 47-REVIEWS.md cycle 3 — Plan 03 Task 3 appends
    # broadcast-admin tests that reference this name in their bodies.
    c = MagicMock(spec=TelethonChannel)
    c.id = id_
    c.title = kwargs.get("title", "Test Channel")
    c.username = kwargs.get("username", "test_channel")
    c.megagroup = False
    c.broadcast = True
    c.forum = False
    c.creator = kwargs.get("creator", False)
    c.admin_rights = kwargs.get("admin_rights", None)
    c.left = kwargs.get("left", False)
    c.restriction_reason = []
    return c


def _full_channel(**kwargs):
    from telethon.tl.types import ChatReactionsNone  # type: ignore[import-untyped]
    full = MagicMock()
    full.full_chat = MagicMock(
        participants_count=kwargs.get("participants_count", 1000),
        linked_chat_id=kwargs.get("linked_chat_id", None),
        pinned_msg_id=kwargs.get("pinned_msg_id", None),
        slowmode_seconds=kwargs.get("slowmode_seconds", None),
        about=kwargs.get("about", None),
        available_reactions=kwargs.get("available_reactions", ChatReactionsNone()),
        chat_photo=kwargs.get("chat_photo", None),
    )
    return full


@pytest.mark.asyncio
async def test_get_entity_info_channel_type() -> None:
    """SPEC Req 2: Broadcast channel returns type='channel'."""
    chan = _broadcast_channel(id_=-1001)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    full = _full_channel()
    search = MagicMock(count=0, messages=[])
    client.side_effect = [full, search]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001})
    assert r["ok"] is True, f"got {r}"
    assert r["data"]["type"] == "channel"


@pytest.mark.asyncio
async def test_get_entity_info_channel_common_envelope() -> None:
    """SPEC Req 3: Channel response carries the common envelope keys."""
    chan = _broadcast_channel(id_=-1002, title="News", username="news_chan")
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    full = _full_channel(about="Channel about text")
    search = MagicMock(count=0, messages=[])
    client.side_effect = [full, search]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1002})
    d = r["data"]
    for key in ("id", "type", "name", "username", "about", "my_membership",
                "avatar_history", "avatar_count"):
        assert key in d
    assert d["name"] == "News"
    assert d["username"] == "news_chan"
    assert d["about"] == "Channel about text"


@pytest.mark.asyncio
async def test_get_entity_info_channel_field_surface() -> None:
    """SPEC Req 5: Channel response carries all per-type fields."""
    chan = _broadcast_channel(id_=-1003)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    full = _full_channel(participants_count=12345, pinned_msg_id=999, slowmode_seconds=30)
    search = MagicMock(count=0, messages=[])
    client.side_effect = [full, search]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1003})
    d = r["data"]
    for key in ("subscribers_count", "linked_chat_id", "pinned_msg_id",
                "slow_mode_seconds", "available_reactions", "restrictions",
                "contacts_subscribed"):
        assert key in d, f"missing Channel-specific key: {key}"
    assert d["subscribers_count"] == 12345
    assert d["pinned_msg_id"] == 999
    assert d["slow_mode_seconds"] == 30


@pytest.mark.asyncio
async def test_get_entity_info_channel_non_admin_contacts_null() -> None:
    """SPEC Req 9: non-admin call on a broadcast → contacts_subscribed=null + reason='not_an_admin'."""
    chan = _broadcast_channel(id_=-1004, creator=False, admin_rights=None)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    full = _full_channel()
    search = MagicMock(count=0, messages=[])
    client.side_effect = [full, search]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1004})
    d = r["data"]
    assert d["contacts_subscribed"] is None
    assert d["contacts_reason"] == "not_an_admin"


@pytest.mark.asyncio
async def test_get_entity_info_channel_available_reactions_some() -> None:
    """RESEARCH Pitfall 5: available_reactions normalized to {kind, emojis}."""
    from telethon.tl.types import ChatReactionsSome  # type: ignore[import-untyped]
    emoji_obj = MagicMock()
    emoji_obj.emoticon = "👍"
    reactions = ChatReactionsSome(reactions=[emoji_obj])
    chan = _broadcast_channel(id_=-1005)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    full = _full_channel(available_reactions=reactions)
    search = MagicMock(count=0, messages=[])
    client.side_effect = [full, search]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1005})
    ar = r["data"]["available_reactions"]
    assert ar == {"kind": "some", "emojis": ["👍"]}


@pytest.mark.asyncio
async def test_get_entity_info_no_download_keys_channel() -> None:
    """SPEC Req 10: Channel response contains no file_id / file_reference / download_*."""
    chan = _broadcast_channel(id_=-1006)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    full = _full_channel()
    search = MagicMock(count=0, messages=[])
    client.side_effect = [full, search]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1006})

    def _walk_keys(o):
        if isinstance(o, dict):
            for k in o.keys():
                yield k
                yield from _walk_keys(o[k])
        elif isinstance(o, list):
            for it in o:
                yield from _walk_keys(it)

    forbidden = re.compile(r"^(file_id|file_reference|download_)")
    bad = [k for k in _walk_keys(r["data"]) if forbidden.match(str(k))]
    assert not bad, f"forbidden download keys present: {bad}"


@pytest.mark.asyncio
async def test_get_entity_info_channel_avatar_search_fails_d20_fallback() -> None:
    """HIGH-3 from 47-REVIEWS.md cycle 3 (codex 2026-04-25): D-20 fallback
    contract — when messages.Search(ChatPhotos) raises but full_chat.chat_photo
    is known, the response surfaces that one current photo with avatar_count=1.

    Without the search_failed-flag fix, D-19 reconciliation would prepend
    chat_photo into avatar_history, and the old D-20 block (`if not
    avatar_history`) would skip — leaving avatar_count == 0 while a
    current photo is present. That contradicts D-20.
    """
    from datetime import datetime, timezone

    chan = _broadcast_channel(id_=-1007)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)

    # full_chat.chat_photo is set (current avatar known).
    chat_photo = MagicMock()
    chat_photo.id = 99999
    chat_photo.date = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    full = _full_channel()
    full.full_chat.chat_photo = chat_photo

    # messages.Search(ChatPhotos) RAISES — search_failed branch.
    search_exc = RuntimeError("flood wait simulated")

    client.side_effect = [full, search_exc]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1007})

    d = r["data"]
    # D-19 still places the current photo in avatar_history.
    assert len(d["avatar_history"]) == 1
    assert d["avatar_history"][0]["photo_id"] == 99999
    # D-20 contract: avatar_count = 1 even though Search raised — this is
    # the bug HIGH-3 fixes (was 0 before the search_failed flag).
    assert d["avatar_count"] == 1, (
        "D-20 violation: chat_photo present but avatar_count == 0; "
        "search_failed flag fix from cycle-3 HIGH-3 missing"
    )
