"""RPC budget enforcement tests for GetEntityInfo (HIGH-C from 47-REVIEWS.md cycle 2).

Asserts the SPEC rate-limit bound for channel and supergroup paths. Each
path performs one entity lookup, one channel profile observation, one bounded
contact page, and one avatar-history search, for at most four RPCs.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.tl.types import Channel as TelethonChannel  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.entity_profile.contracts import (
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChannelReference,
    ProjectionStatus,
)
from mcp_telegram.entity_profile.ports import ChannelProfilePort
from tests.daemon_api_policy import make_daemon_api_policy
from tests.helpers import LoudGroupProfilePort, LoudUserProfilePort
from tests.reaction_helpers import make_reaction_freshener

_TEST_DBS: list[sqlite3.Connection] = []


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
    conn.executescript(
        """
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'not_synced',
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
        """
    )
    _TEST_DBS.append(conn)
    return conn


class _CountingClient:
    """Counting wrapper around AsyncMock that tracks MTProto-shaped calls.

    Captures:
    - ``await client.get_entity(...)``  counted via get_entity property
    - ``await client(<Request>)``       counted via __call__
    ``total_rpc_count`` = entity_calls + call_count
    """

    def __init__(self) -> None:
        self.entity_calls = 0
        self.get_entity = AsyncMock()
        self.call_count = 0
        self._call_responses: list[object] = []

    # --- configuration helpers ---

    def set_entity(self, entity: object) -> None:
        async def _get_entity(*_args: object, **_kwargs: object) -> object:
            self.entity_calls += 1
            return entity

        self.get_entity.side_effect = _get_entity

    def set_call_responses(self, responses: list[object]) -> None:
        self._call_responses = list(responses)

    # --- protocol ---

    async def __call__(self, request: object) -> object:
        """Count every ``await client(<Request>)`` call."""
        self.call_count += 1
        if not self._call_responses:
            raise AssertionError(
                f"unexpected client(...) call #{self.call_count} — no more responses queued for request {request!r}"
            )
        return self._call_responses.pop(0)

    @property
    def total_rpc_count(self) -> int:
        return self.entity_calls + self.call_count


class _CountingChannelProfilePort:
    def __init__(self, channel_id: int, members: int) -> None:
        self.rpc_count = 0
        self.profile = ChannelProfileObservation(
            channel_id=channel_id,
            about=None,
            participants_count=members,
            linked_chat_id=None,
            pinned_msg_id=None,
            slow_mode_seconds=None,
            available_reactions={"kind": "none", "emojis": []},
            current_photo=None,
            observation_started_at=100,
            observation_completed_at=100,
        )
        self.overlap = ChannelContactOverlapObservation(
            channel_id=channel_id,
            contact_ids=(),
            status=ProjectionStatus.PARTIAL,
            reason="bounded_contacts_page",
            observation_started_at=100,
            observation_completed_at=100,
        )

    def get_channel_reference(self, channel_id: int) -> ChannelReference | None:
        canonical_id = channel_id if channel_id <= -1_000_000_000_001 else -1_000_000_000_000 - abs(channel_id)
        return ChannelReference(canonical_id, 0)

    async def fetch_channel_profile(self, reference: ChannelReference) -> ChannelProfileObservation:
        self.rpc_count += 1
        return replace(self.profile, channel_id=reference.channel_id)

    async def fetch_channel_contact_overlap(self, reference: ChannelReference) -> ChannelContactOverlapObservation:
        self.rpc_count += 1
        return replace(self.overlap, channel_id=reference.channel_id)


def _make_supergroup_mock(*, id_: int, members: int, is_admin: bool = True) -> MagicMock:
    ch = MagicMock(spec=TelethonChannel)
    ch.id = id_
    ch.title = "RPC budget test group"
    ch.username = None
    ch.access_hash = 0
    ch.min = False
    ch.megagroup = True
    ch.broadcast = False
    ch.forum = False
    ch.creator = is_admin
    ch.admin_rights = None
    ch.left = False
    ch.restriction_reason = []
    ch.hidden_members = False
    ch.noforwards = False
    return ch


def _make_channel_mock(*, id_: int, members: int, is_admin: bool = True) -> MagicMock:
    ch = MagicMock(spec=TelethonChannel)
    ch.id = id_
    ch.title = "RPC budget test channel"
    ch.username = None
    ch.access_hash = 0
    ch.min = False
    ch.megagroup = False
    ch.broadcast = True
    ch.forum = False
    ch.creator = is_admin
    ch.admin_rights = None
    ch.left = False
    ch.restriction_reason = []
    ch.hidden_members = False
    ch.noforwards = False
    return ch


def _empty_search() -> MagicMock:
    s = MagicMock()
    s.count = 0
    s.messages = []
    return s


def _make_server(
    conn: sqlite3.Connection | None = None,
    client: object | None = None,
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
        policy=make_daemon_api_policy(),
    )
    server._ready = True
    return server


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_small_rpc_count_le_9() -> None:
    """HIGH-C from 47-REVIEWS.md cycle 2: small-group enumeration path
    for a 1000-member supergroup must NOT exceed 9 MTProto RPCs total.

    Composition:
      1  get_entity
      1  channels.GetFullChannel
      1  channels.GetParticipants (bounded contacts page)
      1  messages.Search(ChatPhotos)
    ---
      8  total  (well within the <=9 SPEC bound)
    """
    client = _CountingClient()
    sg = _make_supergroup_mock(id_=-1001000000001, members=1000, is_admin=True)
    client.set_entity(sg)

    # The profile port owns one profile RPC and one bounded contact-page RPC.
    client.set_call_responses([_empty_search()])
    channel_profile_port = _CountingChannelProfilePort(-1001000000001, 1000)

    with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        server = _make_server(client=client, channel_profile_port=channel_profile_port)
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001000000001})

    assert r["ok"] is True, f"expected ok=True, got {r!r}"
    total = client.total_rpc_count + channel_profile_port.rpc_count
    assert total <= 4, f"bounded channel profile path made {total} RPCs"
    assert channel_profile_port.rpc_count == 2


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_large_rpc_count_le_4() -> None:
    """HIGH-C: a 50000-member supergroup must stay within the bounded
    four-RPC composition.

    Composition:
      1  get_entity
      1  channels.GetFullChannel
      1  channels.GetParticipants (bounded contacts page)
      1  messages.Search(ChatPhotos)
    ---
      4  total (exactly at the <=4 SPEC bound)
    """
    client = _CountingClient()
    sg = _make_supergroup_mock(id_=-1001000000002, members=50000, is_admin=True)
    client.set_entity(sg)

    client.set_call_responses([_empty_search()])
    channel_profile_port = _CountingChannelProfilePort(-1001000000002, 50000)

    with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        server = _make_server(client=client, channel_profile_port=channel_profile_port)
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001000000002})

    assert r["ok"] is True, f"expected ok=True, got {r!r}"
    total = client.total_rpc_count + channel_profile_port.rpc_count
    assert total <= 4, f"bounded channel profile path made {total} RPCs"
    assert channel_profile_port.rpc_count == 2


@pytest.mark.asyncio
async def test_get_entity_info_broadcast_channel_small_rpc_count_le_9() -> None:
    """HIGH-C from 47-REVIEWS.md cycle 2: broadcast Channel admin-path
    small-group enumeration (Plan 03 Task 3) shares the supergroup <=9
    budget. Guards against regressions where the broadcast path adds RPCs.

    Composition mirrors the supergroup <=1000 path:
      1  get_entity
      1  channels.GetFullChannel
      1  channels.GetParticipants (bounded contacts page)
      1  messages.Search(ChatPhotos)
    ---
      8  total (within <=9)
    """
    client = _CountingClient()
    ch = _make_channel_mock(id_=-1009999999999, members=1000, is_admin=True)
    client.set_entity(ch)

    client.set_call_responses([_empty_search()])
    channel_profile_port = _CountingChannelProfilePort(-1009999999999, 1000)

    with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        server = _make_server(client=client, channel_profile_port=channel_profile_port)
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1009999999999})

    assert r["ok"] is True, f"expected ok=True, got {r!r}"
    total = client.total_rpc_count + channel_profile_port.rpc_count
    assert total <= 4, f"bounded channel profile path made {total} RPCs"
    assert channel_profile_port.rpc_count == 2
