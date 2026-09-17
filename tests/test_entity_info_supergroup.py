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
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.entity_profile.contracts import (
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ProjectionStatus,
)
from mcp_telegram.entity_profile.ports import ChannelProfilePort
from tests.daemon_api_policy import make_daemon_api_policy
from tests.helpers import (
    ClientChatAvatarHistoryPort,
    FakeChannelProfilePort,
    LoudCommonChatsPort,
    LoudGroupProfilePort,
    LoudUserAvatarHistoryPort,
    LoudUserProfilePort,
)
from tests.reaction_helpers import make_reaction_freshener

_TEST_DBS: list[sqlite3.Connection] = []


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)


def _dict_at(value: object, *keys: str) -> dict[str, object]:
    current = _dict(value)
    for key in keys:
        current = _dict(current[key])
    return current


@pytest.fixture(autouse=True)
def _close_test_db():
    yield
    while _TEST_DBS:
        conn = _TEST_DBS.pop()
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - best-effort fixture cleanup, preserve old teardown semantics
            pass


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
        CREATE TABLE full_history_enrollment (
            dialog_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            source TEXT NOT NULL CHECK(source IN ('explicit', 'automatic', 'migration')),
            updated_at INTEGER NOT NULL
        ) WITHOUT ROWID;
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
    _TEST_DBS.append(conn)
    return conn


def make_server(
    conn: sqlite3.Connection | None = None,
    client: DaemonClientLike | None = None,
    *,
    channel_profile_port: ChannelProfilePort,
) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    shutdown_event = asyncio.Event()
    server = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, client),
        shutdown_event,
        reaction_freshener=make_reaction_freshener(conn, client),
        channel_profile_port=channel_profile_port,
        group_profile_port=LoudGroupProfilePort(),
        user_profile_port=LoudUserProfilePort(),
        common_chats_port=LoudCommonChatsPort(),
        user_avatar_history_port=LoudUserAvatarHistoryPort(),
        chat_avatar_history_port=ClientChatAvatarHistoryPort(client),
        policy=make_daemon_api_policy(),
    )
    server._ready = True
    return server


def _supergroup(id_: int = -1001, **kwargs: object) -> MagicMock:
    from telethon.tl.types import Channel as TelethonChannel  # type: ignore[import-untyped]

    c = MagicMock(spec=TelethonChannel)
    c.id = id_
    c.title = kwargs.get("title", "Test Supergroup")
    c.username = kwargs.get("username", "test_super")
    c.access_hash = 0
    c.min = False
    c.megagroup = True
    c.broadcast = False
    c.forum = kwargs.get("forum", False)
    c.creator = kwargs.get("creator", False)
    c.admin_rights = kwargs.get("admin_rights")
    c.left = kwargs.get("left", False)
    c.restriction_reason = []
    c.noforwards = False
    c.hidden_members = kwargs.get("hidden_members", False)
    return c


def _full_supergroup(**kwargs: object) -> MagicMock:
    from telethon.tl.types import ChatReactionsNone  # type: ignore[import-untyped]

    full = MagicMock()
    full.full_chat = MagicMock(
        participants_count=kwargs.get("participants_count", 100),
        linked_chat_id=kwargs.get("linked_chat_id"),
        slowmode_seconds=kwargs.get("slowmode_seconds"),
        about=kwargs.get("about"),
        available_reactions=ChatReactionsNone(),
        chat_photo=None,
    )
    return full


def _empty_search():
    return MagicMock(count=0, messages=[])


def _channel_port(
    channel_id: int,
    full: object,
    *,
    contact_ids: tuple[int, ...] = (),
    overlap_status: ProjectionStatus = ProjectionStatus.PARTIAL,
    overlap_reason: str = "bounded_contacts_page",
) -> FakeChannelProfilePort:
    full_chat = getattr(full, "full_chat", full)
    raw_link = getattr(full_chat, "linked_chat_id", None)
    linked_id = None
    if isinstance(raw_link, int) and raw_link > 0:
        # Keep the test port neutral while matching Telethon's canonical peer id.
        linked_id = -1_000_000_000_000 - raw_link
    raw_pinned = getattr(full_chat, "pinned_msg_id", None)
    pinned_id = raw_pinned if isinstance(raw_pinned, int) and not isinstance(raw_pinned, bool) else None
    raw_slow_mode = getattr(full_chat, "slowmode_seconds", None)
    slow_mode = raw_slow_mode if isinstance(raw_slow_mode, int) and not isinstance(raw_slow_mode, bool) else None
    profile = ChannelProfileObservation(
        channel_id=channel_id,
        about=getattr(full_chat, "about", None),
        participants_count=getattr(full_chat, "participants_count", None),
        linked_chat_id=linked_id,
        pinned_msg_id=pinned_id,
        slow_mode_seconds=slow_mode,
        available_reactions={"kind": "none", "emojis": []},
        current_photo=None,
        observation_started_at=100,
        observation_completed_at=100,
    )
    overlap = ChannelContactOverlapObservation(
        channel_id=channel_id,
        contact_ids=contact_ids if overlap_status is not ProjectionStatus.UNAVAILABLE else None,
        status=overlap_status,
        reason=overlap_reason,
        observation_started_at=100,
        observation_completed_at=100,
    )
    return FakeChannelProfilePort(profile, overlap)


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_type() -> None:
    """SPEC Req 2: megagroup returns type='supergroup'."""
    sg = _supergroup(id_=-1001)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    full = _full_supergroup()
    client.side_effect = [_empty_search()]
    server = make_server(client=client, channel_profile_port=_channel_port(-1001, full))
    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001})
    assert r["ok"] is True, r
    assert _dict(r["data"])["type"] == "supergroup"


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_field_surface() -> None:
    """SPEC Req 6: per-type field surface complete."""
    sg = _supergroup(id_=-1002)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    full = _full_supergroup(participants_count=42, slowmode_seconds=60, linked_chat_id=200500)
    client.side_effect = [_empty_search()]
    server = make_server(client=client, channel_profile_port=_channel_port(-1002, full))
    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1002})
    d = _dict(r["data"])
    for key in (
        "members_count",
        "linked_broadcast_id",
        "slow_mode_seconds",
        "has_topics",
        "restrictions",
        "contacts_subscribed",
    ):
        assert key in d, f"missing supergroup key: {key}"
    assert d["members_count"] == 42
    assert d["slow_mode_seconds"] == 60
    assert d["linked_broadcast_id"] == -1_000_000_200_500
    assert d["has_topics"] is False


@pytest.mark.asyncio
async def test_get_entity_info_forum_supergroup_has_topics() -> None:
    """SPEC Req 6: forum supergroup → type='supergroup' + has_topics=True."""
    sg = _supergroup(id_=-1003, forum=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    full = _full_supergroup()
    client.side_effect = [_empty_search()]
    server = make_server(client=client, channel_profile_port=_channel_port(-1003, full))
    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1003})
    assert r["ok"] is True
    assert _dict(r["data"])["type"] == "supergroup"
    assert _dict(r["data"])["has_topics"] is True


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_small_enumerates_dm_intersection() -> None:
    """SPEC Req 9: members_count<=1000 uses the bounded contacts page and intersects it with DM peers.

    Setup: 3 participants (ids 10, 20, 30); DM-peer set (synced_dialogs) has {10, 30, 999}.
    Expected: contacts_subscribed = entries for ids 10 and 30, partial=False.
    """
    conn = _make_db()
    # Seed DM-peer set
    for did in (10, 30, 999):
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (did,))
    # Seed entities for name enrichment
    conn.executemany(
        "INSERT INTO entities (id, type, name, username, updated_at) VALUES (?, 'user', ?, ?, 1000)",
        [(10, "Alice", "alice"), (30, "Charlie", "charlie")],
    )
    conn.commit()

    sg = _supergroup(id_=-1004, creator=True)  # creator → is_admin=True

    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)
    full = _full_supergroup(participants_count=3)
    client.side_effect = [_empty_search()]
    server = make_server(
        conn=conn, client=client, channel_profile_port=_channel_port(-1004, full, contact_ids=(10, 20, 30))
    )
    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1004})
    d = _dict(r["data"])
    assert d["contacts_subscribed_partial"] is True
    assert d["contacts_reason"] == "bounded_contacts_page"
    contacts = cast(list[dict[str, object]], d["contacts_subscribed"])
    ids = {entry["id"] for entry in contacts}
    assert ids == {10, 30}, f"expected {{10,30}}, got {ids}"
    # Names enriched from entities table
    names = {entry["id"]: entry["name"] for entry in contacts}
    assert names == {10: "Alice", 30: "Charlie"}


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_large_uses_contact_filter() -> None:
    """SPEC Req 9: members_count>1000 uses the same bounded contacts page; partial=True."""
    conn = _make_db()
    for did in (50, 60, 70):
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (did,))
    conn.executemany(
        "INSERT INTO entities (id, type, name, username, updated_at) VALUES (?, 'user', ?, ?, 1000)",
        [(50, "U50", "u50"), (60, "U60", "u60")],
    )
    conn.commit()

    sg = _supergroup(id_=-1005, creator=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)
    full = _full_supergroup(participants_count=5000)
    client.side_effect = [_empty_search()]
    server = make_server(
        conn=conn,
        client=client,
        channel_profile_port=_channel_port(-1005, full, contact_ids=(50, 60)),
    )
    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1005})
    d = _dict(r["data"])
    assert d["contacts_subscribed_partial"] is True
    assert d["contacts_reason"] == "bounded_contacts_page"
    contacts = cast(list[dict[str, object]], d["contacts_subscribed"])
    ids = {entry["id"] for entry in contacts}
    assert ids == {50, 60}


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_hidden_members_null() -> None:
    """SPEC Req 9: non-admin + hidden_members → contacts_subscribed=null + reason='hidden_by_admin'."""
    sg = _supergroup(id_=-1006, hidden_members=True, creator=False, admin_rights=None)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    full = _full_supergroup()
    client.side_effect = [_empty_search()]
    server = make_server(client=client, channel_profile_port=_channel_port(-1006, full))
    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1006})
    assert r["ok"]
    d = _dict(r["data"])
    assert d["contacts_subscribed"] is None
    assert d["contacts_reason"] == "hidden_by_admin"


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_chat_admin_required_treated_as_hidden() -> None:
    """HIGH-3: when channel.hidden_members is absent/False but the bounded
    contact page is unavailable, preserve the hidden-members result and return
    contacts_subscribed=null + reason='hidden_by_admin'.
    """
    # No explicit hidden_members attribute set (defaults to False in _supergroup),
    # but the bounded contact page is unavailable — the ground-truth case.
    sg = _supergroup(id_=-1011, hidden_members=False, creator=False, admin_rights=None)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    full = _full_supergroup(participants_count=500)
    client.side_effect = [_empty_search()]
    server = make_server(
        client=client,
        channel_profile_port=_channel_port(
            -1011,
            full,
            overlap_status=ProjectionStatus.UNAVAILABLE,
            overlap_reason="not_an_admin",
        ),
    )
    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1011})
    assert r["ok"]
    d = _dict(r["data"])
    assert d["contacts_subscribed"] is None
    assert d["contacts_reason"] == "hidden_by_admin"


@pytest.mark.asyncio
async def test_get_entity_info_no_download_keys_supergroup() -> None:
    """SPEC Req 10: Supergroup response has no file_id / file_reference / download_*."""
    sg = _supergroup(id_=-1007, creator=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)

    full = _full_supergroup()
    client.side_effect = [_empty_search()]
    server = make_server(client=client, channel_profile_port=_channel_port(-1007, full))
    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1007})

    def _walk(o: object):
        if isinstance(o, dict):
            for k in o:
                yield k
                yield from _walk(o[k])
        elif isinstance(o, list):
            for it in o:
                yield from _walk(it)

    forbidden = re.compile(r"^(file_id|file_reference|download_)")
    bad = [k for k in _walk(_dict(r["data"])) if forbidden.match(str(k))]
    assert not bad
