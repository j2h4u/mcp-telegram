"""Tests for GetEntityInfo — Supergroup (megagroup, including forum) kind.

SPEC Reqs covered: 2 (type=supergroup), 3 (common envelope), 6 (Supergroup
field surface: members_count, linked_broadcast_id, slow_mode_seconds,
has_topics, restrictions, contacts_subscribed), 9 (≤1000 enumerate /
>1000 contact-filter / hidden-members null), 10 (no download keys).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    conn.execute("PRAGMA foreign_keys = ON")
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


def _supergroup(id_=-1001, **kwargs):
    from telethon.tl.types import Channel as TelethonChannel  # type: ignore[import-untyped]
    c = MagicMock(spec=TelethonChannel)
    c.id = id_
    c.title = kwargs.get("title", "Test Supergroup")
    c.username = kwargs.get("username", "test_super")
    c.megagroup = True
    c.broadcast = False
    c.forum = kwargs.get("forum", False)
    c.creator = kwargs.get("creator", False)
    c.admin_rights = kwargs.get("admin_rights", None)
    c.left = kwargs.get("left", False)
    c.restriction_reason = []
    c.noforwards = False
    c.hidden_members = kwargs.get("hidden_members", False)
    return c


def _full_supergroup(**kwargs):
    from telethon.tl.types import ChatReactionsNone  # type: ignore[import-untyped]
    full = MagicMock()
    full.full_chat = MagicMock(
        participants_count=kwargs.get("participants_count", 100),
        linked_chat_id=kwargs.get("linked_chat_id", None),
        slowmode_seconds=kwargs.get("slowmode_seconds", None),
        about=kwargs.get("about", None),
        available_reactions=ChatReactionsNone(),
        chat_photo=None,
    )
    return full


def _empty_search():
    return MagicMock(count=0, messages=[])


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_type() -> None:
    """SPEC Req 2: megagroup returns type='supergroup'."""
    sg = _supergroup(id_=-1001)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    async def empty_iter(*args, **kwargs):
        if False:
            yield None  # make this a generator
    client.iter_participants = MagicMock(side_effect=empty_iter)
    client.side_effect = [_full_supergroup(), _empty_search()]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001})
    assert r["ok"] is True, r
    assert r["data"]["type"] == "supergroup"


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_field_surface() -> None:
    """SPEC Req 6: per-type field surface complete."""
    sg = _supergroup(id_=-1002)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    async def empty_iter(*args, **kwargs):
        if False:
            yield None
    client.iter_participants = MagicMock(side_effect=empty_iter)
    client.side_effect = [
        _full_supergroup(participants_count=42, slowmode_seconds=60, linked_chat_id=200500),
        _empty_search(),
    ]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1002})
    d = r["data"]
    for key in ("members_count", "linked_broadcast_id", "slow_mode_seconds",
                "has_topics", "restrictions", "contacts_subscribed"):
        assert key in d, f"missing supergroup key: {key}"
    assert d["members_count"] == 42
    assert d["slow_mode_seconds"] == 60
    # linked_chat_id 200500 → peer-id form via telethon_utils.get_peer_id(PeerChannel(200500)).
    # MEDIUM from 47-REVIEWS.md cycle 2 (codex): the prior test asserted
    # `-100200500` (the brittle `int(f"-100{raw}")` string-concat result),
    # which is WRONG under the canonical Telethon helper — the helper
    # returns -1000000200500 (channel ids are zero-padded to a fixed width
    # in peer-id form). The assertion is updated to derive the expected
    # value from the same helper the production code uses, so the test
    # is robust to any future Telethon peer-id encoding tweaks.
    from telethon.tl.types import PeerChannel
    from telethon import utils as telethon_utils
    assert d["linked_broadcast_id"] == int(telethon_utils.get_peer_id(PeerChannel(200500)))
    assert d["has_topics"] is False


@pytest.mark.asyncio
async def test_get_entity_info_forum_supergroup_has_topics() -> None:
    """SPEC Req 6: forum supergroup → type='supergroup' + has_topics=True."""
    sg = _supergroup(id_=-1003, forum=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    async def empty_iter(*args, **kwargs):
        if False:
            yield None
    client.iter_participants = MagicMock(side_effect=empty_iter)
    client.side_effect = [_full_supergroup(), _empty_search()]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1003})
    assert r["ok"] is True
    assert r["data"]["type"] == "supergroup"
    assert r["data"]["has_topics"] is True


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_small_enumerates_dm_intersection() -> None:
    """SPEC Req 9 + CONTEXT D-14: members_count<=1000 → intersect iter_participants with DM peers.

    Setup: 3 participants (ids 10, 20, 30); DM-peer set (synced_dialogs) has {10, 30, 999}.
    Expected: contacts_subscribed = entries for ids 10 and 30, partial=False.
    """
    conn = _make_db()
    # Seed DM-peer set
    for did in (10, 30, 999):
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (did,)
        )
    # Seed entities for name enrichment
    conn.executemany(
        "INSERT INTO entities (id, type, name, username, updated_at) VALUES (?, 'user', ?, ?, 1000)",
        [(10, "Alice", "alice"), (30, "Charlie", "charlie")],
    )
    conn.commit()

    sg = _supergroup(id_=-1004, creator=True)  # creator → is_admin=True

    async def iter_three(*args, **kwargs):
        for pid in (10, 20, 30):
            p = MagicMock(); p.id = pid
            yield p

    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)
    client.iter_participants = MagicMock(side_effect=iter_three)
    client.side_effect = [_full_supergroup(participants_count=3), _empty_search()]
    server = make_server(conn=conn, client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1004})
    d = r["data"]
    assert d["contacts_subscribed_partial"] is False
    ids = {entry["id"] for entry in d["contacts_subscribed"]}
    assert ids == {10, 30}, f"expected {{10,30}}, got {ids}"
    # Names enriched from entities table
    names = {entry["id"]: entry["name"] for entry in d["contacts_subscribed"]}
    assert names == {10: "Alice", 30: "Charlie"}


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_large_uses_contact_filter() -> None:
    """SPEC Req 9 + CONTEXT D-15: members_count>1000 → ChannelParticipantsContacts intersect; partial=True."""
    conn = _make_db()
    for did in (50, 60, 70):
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (did,)
        )
    conn.executemany(
        "INSERT INTO entities (id, type, name, username, updated_at) VALUES (?, 'user', ?, ?, 1000)",
        [(50, "U50", "u50"), (60, "U60", "u60")],
    )
    conn.commit()

    sg = _supergroup(id_=-1005, creator=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)
    # iter_participants must NOT be called on the >1000 path; raise if it is.
    client.iter_participants = MagicMock(side_effect=AssertionError(
        "iter_participants must not be called on the >1000 path"
    ))
    # Raw GetParticipantsRequest returns 2 contacts (50 and 60) — only 50/60 are in DM peers
    gp_users = [MagicMock(), MagicMock()]
    gp_users[0].id = 50; gp_users[1].id = 60
    gp_result = MagicMock(); gp_result.users = gp_users
    client.side_effect = [
        _full_supergroup(participants_count=5000),
        gp_result,           # GetParticipantsRequest call
        _empty_search(),     # MessagesSearchRequest call (avatar history)
    ]
    server = make_server(conn=conn, client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.GetParticipantsRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1005})
    d = r["data"]
    assert d["contacts_subscribed_partial"] is True
    assert d["contacts_reason"] == "too_large"
    ids = {entry["id"] for entry in d["contacts_subscribed"]}
    assert ids == {50, 60}


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_hidden_members_null() -> None:
    """SPEC Req 9: non-admin + hidden_members → contacts_subscribed=null + reason='hidden_by_admin'."""
    sg = _supergroup(id_=-1006, hidden_members=True, creator=False, admin_rights=None)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    async def must_not_iter(*args, **kwargs):
        raise AssertionError("must not enumerate when hidden_members and non-admin")
    client.iter_participants = MagicMock(side_effect=must_not_iter)
    client.side_effect = [_full_supergroup(), _empty_search()]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1006})
    assert r["ok"]
    assert r["data"]["contacts_subscribed"] is None
    assert r["data"]["contacts_reason"] == "hidden_by_admin"


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_chat_admin_required_treated_as_hidden() -> None:
    """HIGH-3 from 47-REVIEWS.md: when channel.hidden_members is absent/False
    but the API still rejects enumeration with ChatAdminRequiredError, treat
    the rejection as ground-truth hidden membership and return
    contacts_subscribed=null + reason='hidden_by_admin'.
    """
    from telethon.errors import ChatAdminRequiredError
    # No explicit hidden_members attribute set (defaults to False in _supergroup),
    # but iter_participants raises ChatAdminRequiredError — the ground-truth case.
    sg = _supergroup(id_=-1011, hidden_members=False, creator=False, admin_rights=None)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    async def raise_admin_required(*args, **kwargs):
        raise ChatAdminRequiredError(request=None)
        yield None  # pragma: no cover — make this an async generator
    client.iter_participants = MagicMock(side_effect=raise_admin_required)
    client.side_effect = [_full_supergroup(participants_count=500), _empty_search()]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1011})
    assert r["ok"]
    assert r["data"]["contacts_subscribed"] is None
    assert r["data"]["contacts_reason"] == "hidden_by_admin"


@pytest.mark.asyncio
async def test_get_entity_info_no_download_keys_supergroup() -> None:
    """SPEC Req 10: Supergroup response has no file_id / file_reference / download_*."""
    sg = _supergroup(id_=-1007, creator=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    async def empty_iter(*args, **kwargs):
        if False:
            yield None
    client.iter_participants = MagicMock(side_effect=empty_iter)
    client.side_effect = [_full_supergroup(), _empty_search()]
    server = make_server(client=client)
    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1007})

    def _walk(o):
        if isinstance(o, dict):
            for k in o.keys():
                yield k
                yield from _walk(o[k])
        elif isinstance(o, list):
            for it in o:
                yield from _walk(it)

    forbidden = re.compile(r"^(file_id|file_reference|download_)")
    bad = [k for k in _walk(r["data"]) if forbidden.match(str(k))]
    assert not bad
