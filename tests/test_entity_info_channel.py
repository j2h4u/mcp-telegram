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


# ---------------------------------------------------------------------------
# Plan 03 Task 3: Broadcast Channel admin-path enumeration tests
# (HIGH-A from 47-REVIEWS.md cycle 2 — admin branch replaces Plan 02 stub)
# ---------------------------------------------------------------------------

def _full_broadcast_channel(*, participants_count: int) -> MagicMock:
    full = MagicMock()
    full.participants_count = participants_count
    full.linked_chat_id = None
    full.pinned_msg_id = None
    full.slowmode_seconds = None
    full.about = None
    full.available_reactions = None
    full.chat_photo = None
    return full


@pytest.mark.asyncio
async def test_get_entity_info_channel_admin_enumerates_subscribers_small() -> None:
    """HIGH-A from 47-REVIEWS.md cycle 2: broadcast Channel with admin caller
    and subscribers_count <= 1000 returns a real contacts_subscribed list
    (DM-peer ∩ participant ids), NOT the Plan 02 stub
    contacts_reason='enumeration_owned_by_plan_03'.
    """
    client = AsyncMock()
    # Resolve broadcast channel (megagroup=False, admin via creator=True)
    ch = MagicMock(spec=TelethonChannel)
    ch.id = -1001234567890
    ch.title = "Broadcast Admin"
    ch.username = "broadcast_admin"
    ch.megagroup = False
    ch.broadcast = True
    ch.creator = True              # is_admin=True via creator
    ch.admin_rights = None
    ch.left = False
    ch.restriction_reason = None
    client.get_entity = AsyncMock(return_value=ch)

    # GetFullChannelRequest returns subscribers_count=42 (≤1000 path).
    full = MagicMock()
    full.full_chat = _full_broadcast_channel(participants_count=42)
    client.side_effect = [full]    # one MTProto call before iter_participants

    # iter_participants yields 3 participant objects with ids 111, 222, 333.
    async def _iter(*args, **kwargs):
        for pid in (111, 222, 333):
            p = MagicMock()
            p.id = pid
            yield p
    client.iter_participants = _iter

    server = make_server(client=client)
    # Seed _dm_peer_ids: operator has DMed 111 and 333 (NOT 222).
    server._conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (111,)
    )
    server._conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (333,)
    )
    # Names for the contact ids (entity rows for enrichment).
    server._conn.execute(
        "INSERT INTO entities (id, type, name, username, name_normalized, updated_at) "
        "VALUES (?, 'User', 'Alice', 'alice', 'alice', 0)", (111,)
    )
    server._conn.execute(
        "INSERT INTO entities (id, type, name, username, name_normalized, updated_at) "
        "VALUES (?, 'User', 'Carol', 'carol', 'carol', 0)", (333,)
    )

    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001234567890})
    d = r["data"]
    assert d["type"] == "channel"
    assert d["contacts_subscribed_partial"] is False
    assert d["contacts_reason"] is None
    # Real enumeration result — must be {111, 333} (222 is not a DM peer).
    ids = sorted(c["id"] for c in d["contacts_subscribed"])
    assert ids == [111, 333], f"expected [111, 333], got {ids}"
    # The Plan 02 stub MUST NOT survive into production responses.
    assert d.get("contacts_reason") != "enumeration_owned_by_plan_03"


@pytest.mark.asyncio
async def test_get_entity_info_channel_admin_enumerates_subscribers_large() -> None:
    """HIGH-A from 47-REVIEWS.md cycle 2: broadcast Channel with admin caller
    and subscribers_count > 1000 uses ChannelParticipantsContacts filter
    and returns contacts_subscribed_partial=True, reason='too_large'.
    """
    client = AsyncMock()
    ch = MagicMock(spec=TelethonChannel)
    ch.id = -1009876543210
    ch.title = "Big Broadcast"
    ch.username = "big_broadcast"
    ch.megagroup = False
    ch.broadcast = True
    ch.creator = True
    ch.admin_rights = None
    ch.left = False
    ch.restriction_reason = None
    client.get_entity = AsyncMock(return_value=ch)

    full = MagicMock()
    full.full_chat = _full_broadcast_channel(participants_count=50000)

    # GetParticipantsRequest(filter=ChannelParticipantsContacts) returns 2 contacts.
    gp_result = MagicMock()
    u1 = MagicMock(); u1.id = 111
    u2 = MagicMock(); u2.id = 222
    gp_result.users = [u1, u2]
    client.side_effect = [full, gp_result]

    # iter_participants MUST NOT be called on the >1000 path.
    async def _iter_should_not_be_called(*args, **kwargs):
        raise AssertionError("iter_participants must not run on >1000 broadcast path")
        yield  # pragma: no cover
    client.iter_participants = _iter_should_not_be_called

    server = make_server(client=client)
    server._conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (111,)
    )
    server._conn.execute(
        "INSERT INTO entities (id, type, name, username, name_normalized, updated_at) "
        "VALUES (?, 'User', 'Alice', 'alice', 'alice', 0)", (111,)
    )

    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1009876543210})
    d = r["data"]
    assert d["contacts_subscribed_partial"] is True
    assert d["contacts_reason"] == "too_large"
    ids = sorted(c["id"] for c in d["contacts_subscribed"])
    assert ids == [111]


@pytest.mark.asyncio
async def test_get_entity_info_channel_admin_chat_admin_required_falls_back_to_not_an_admin() -> None:
    """HIGH-A from 47-REVIEWS.md cycle 2: defensive ChatAdminRequiredError
    handler — if Telegram raises ChatAdminRequiredError on the enumeration
    call (e.g. admin rights revoked between cache and call), the response
    falls back to contacts_subscribed=null, reason='not_an_admin' rather
    than crashing or surfacing the exception.
    """
    from telethon.errors import ChatAdminRequiredError

    client = AsyncMock()
    ch = MagicMock(spec=TelethonChannel)
    ch.id = -1001112223334
    ch.title = "Revoked Admin"
    ch.username = None
    ch.megagroup = False
    ch.broadcast = True
    ch.creator = True
    ch.admin_rights = None
    ch.left = False
    ch.restriction_reason = None
    client.get_entity = AsyncMock(return_value=ch)

    full = MagicMock()
    full.full_chat = _full_broadcast_channel(participants_count=42)
    client.side_effect = [full]

    async def _iter_raises(*args, **kwargs):
        raise ChatAdminRequiredError(request=None)
        yield  # pragma: no cover
    client.iter_participants = _iter_raises

    server = make_server(client=client)
    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001112223334})
    d = r["data"]
    assert d["contacts_subscribed"] is None
    assert d["contacts_reason"] == "not_an_admin"
