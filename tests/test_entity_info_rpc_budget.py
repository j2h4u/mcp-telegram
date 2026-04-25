"""RPC budget enforcement tests for GetEntityInfo (HIGH-C from 47-REVIEWS.md cycle 2).

Asserts the SPEC Constraints ``**Rate-limit budget:**`` bound for the
non-User paths:
  - small-group enumeration path (members_count <= 1000): <= 9 MTProto requests
  - above-threshold path (members_count > 1000):          <= 4 MTProto requests

The cycle-2 review correction (opencode + codex consensus, 2026-04-25)
raised the small-group bound from a wrongly-claimed <=6 to the realistic
<=9, accounting for Telethon's iter_participants(limit=1000) pagination
at the 200/page server-side default.

Call sequence for the supergroup <=1000 path:
  1. client.get_entity(entity_id)          — _get_entity_info orchestrator
  2. client(GetFullChannelRequest)         — _fetch_supergroup_detail
  3. client.iter_participants(...)         — _fetch_supergroup_detail D-14 branch
     (ceil(1000/200) = 5 pages => 5 MTProto requests)
  4. client(MessagesSearchRequest)        — _search_chat_photo_history

Total: 1 + 1 + 5 + 1 = 8 RPCs (well within the <=9 bound).

Call sequence for the supergroup >1000 path:
  1. client.get_entity(entity_id)          — _get_entity_info orchestrator
  2. client(GetFullChannelRequest)         — _fetch_supergroup_detail
  3. client(GetParticipantsRequest)        — _fetch_supergroup_detail D-15 branch
  4. client(MessagesSearchRequest)        — _search_chat_photo_history

Total: 1 + 1 + 1 + 1 = 4 RPCs (exactly at the <=4 bound).
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer
from telethon.tl.types import Channel as TelethonChannel  # type: ignore[import-untyped]


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
    return conn


class _CountingClient:
    """Counting wrapper around AsyncMock that tracks MTProto-shaped calls.

    Captures:
    - ``await client.get_entity(...)``  counted via get_entity property
    - ``await client(<Request>)``       counted via __call__
    - ``async for ... in client.iter_participants(...)``
      counted as ceil(total_yields / 200) pages

    ``total_rpc_count`` = call_count + iter_pages
    """

    def __init__(self) -> None:
        self._get_entity_mock = AsyncMock()
        self.call_count = 0
        self.iter_pages = 0
        self._call_responses: list = []
        self._iter_participants_yields: list[int] = []

    # --- configuration helpers ---

    def set_entity(self, entity) -> None:
        self._get_entity_mock.return_value = entity

    def set_call_responses(self, responses: list) -> None:
        self._call_responses = list(responses)

    def set_iter_participants(self, ids: list[int]) -> None:
        self._iter_participants_yields = list(ids)

    # --- protocol ---

    @property
    def get_entity(self) -> AsyncMock:
        """Return the AsyncMock so that ``await client.get_entity(id)`` works.

        We wrap it to count the call.
        """
        original = self._get_entity_mock

        async def _counted(*args, **kwargs):
            self.call_count += 1
            return await original(*args, **kwargs)

        # Return a plain coroutine function that the caller can await.
        # Bind it as an attribute so repeated access returns the same wrapper.
        return _counted  # type: ignore[return-value]

    async def __call__(self, request):
        """Count every ``await client(<Request>)`` call."""
        self.call_count += 1
        if not self._call_responses:
            raise AssertionError(
                f"unexpected client(...) call #{self.call_count} — "
                f"no more responses queued for request {request!r}"
            )
        return self._call_responses.pop(0)

    def iter_participants(self, *args, **kwargs):
        """Return an async generator; count pages as ceil(yields / 200)."""
        ids = self._iter_participants_yields
        self.iter_pages = (len(ids) + 199) // 200 if ids else 0

        async def _iter():
            for pid in ids:
                p = MagicMock()
                p.id = pid
                yield p

        return _iter()

    @property
    def total_rpc_count(self) -> int:
        return self.call_count + self.iter_pages


def _make_supergroup_mock(*, id_: int, members: int, is_admin: bool = True) -> MagicMock:
    ch = MagicMock(spec=TelethonChannel)
    ch.id = id_
    ch.title = "RPC budget test group"
    ch.username = None
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


def _full_chat_mock(*, participants_count: int) -> MagicMock:
    """Return a GetFullChannelRequest result mock with all fields set to concrete values.

    Using explicit None/int assignments prevents MagicMock from auto-creating
    attributes that json.dumps would refuse to serialize.
    """
    full = MagicMock()
    fc = MagicMock()
    fc.participants_count = participants_count
    fc.linked_chat_id = None
    fc.pinned_msg_id = None
    fc.slowmode_seconds = None
    fc.about = None
    fc.chat_photo = None
    fc.available_reactions = None  # triggers the ChatReactionsNone branch
    full.full_chat = fc
    return full


def _empty_search() -> MagicMock:
    s = MagicMock()
    s.count = 0
    s.messages = []
    return s


def _make_server(conn=None, client=None) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    shutdown_event = asyncio.Event()
    server = DaemonAPIServer(conn, client, shutdown_event)
    server._ready = True
    return server


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_small_rpc_count_le_9() -> None:
    """HIGH-C from 47-REVIEWS.md cycle 2: small-group enumeration path
    for a 1000-member supergroup must NOT exceed 9 MTProto RPCs total.

    Composition:
      1  get_entity
      1  GetFullChannelRequest
      5  channels.GetParticipants pages  (iter_participants(limit=1000) at 200/page)
      1  messages.Search(ChatPhotos)
    ---
      8  total  (well within the <=9 SPEC bound)
    """
    client = _CountingClient()
    sg = _make_supergroup_mock(id_=-1001000000001, members=1000, is_admin=True)
    client.set_entity(sg)

    # __call__ sequence: GetFullChannelRequest -> MessagesSearchRequest
    full = _full_chat_mock(participants_count=1000)
    client.set_call_responses([full, _empty_search()])

    # iter_participants yields 1000 participants => 5 pages at 200/page.
    client.set_iter_participants(list(range(1, 1001)))

    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"), \
         patch("mcp_telegram.daemon_api.InputMessagesFilterChatPhotos"):
        server = _make_server(client=client)
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001000000001})

    assert r["ok"] is True, f"expected ok=True, got {r!r}"
    assert client.total_rpc_count <= 9, (
        f"HIGH-C budget violation: small-group path made {client.total_rpc_count} RPCs "
        f"(call_count={client.call_count}, iter_pages={client.iter_pages}); "
        "SPEC bound is <=9."
    )
    # iter_participants MUST have been called on the <=1000 path.
    assert client.iter_pages > 0, (
        "small-group path must call iter_participants "
        f"(got iter_pages={client.iter_pages})"
    )


@pytest.mark.asyncio
async def test_get_entity_info_supergroup_large_rpc_count_le_4() -> None:
    """HIGH-C from 47-REVIEWS.md cycle 2: above-threshold path for a
    50000-member supergroup must NOT exceed 4 MTProto RPCs total and
    must NOT call iter_participants.

    Composition:
      1  get_entity
      1  GetFullChannelRequest
      1  GetParticipants(filter=ChannelParticipantsContacts)
      1  messages.Search(ChatPhotos)
    ---
      4  total (exactly at the <=4 SPEC bound)
    """
    client = _CountingClient()
    sg = _make_supergroup_mock(id_=-1001000000002, members=50000, is_admin=True)
    client.set_entity(sg)

    # __call__ sequence: GetFullChannelRequest -> GetParticipantsRequest -> MessagesSearchRequest
    full = _full_chat_mock(participants_count=50000)
    gp_result = MagicMock()
    gp_result.users = []
    client.set_call_responses([full, gp_result, _empty_search()])

    # iter_participants must NOT be invoked on the >1000 path.
    client.set_iter_participants([])

    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.GetParticipantsRequest"), \
         patch("mcp_telegram.daemon_api.ChannelParticipantsContacts"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"), \
         patch("mcp_telegram.daemon_api.InputMessagesFilterChatPhotos"):
        server = _make_server(client=client)
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001000000002})

    assert r["ok"] is True, f"expected ok=True, got {r!r}"
    assert client.iter_pages == 0, (
        "HIGH-C: iter_participants must not run on the >1000 path "
        f"(observed iter_pages={client.iter_pages})"
    )
    assert client.total_rpc_count <= 4, (
        f"HIGH-C budget violation: above-threshold path made {client.total_rpc_count} RPCs "
        f"(call_count={client.call_count}, iter_pages={client.iter_pages}); "
        "SPEC bound is <=4."
    )


@pytest.mark.asyncio
async def test_get_entity_info_broadcast_channel_small_rpc_count_le_9() -> None:
    """HIGH-C from 47-REVIEWS.md cycle 2: broadcast Channel admin-path
    small-group enumeration (Plan 03 Task 3) shares the supergroup <=9
    budget. Guards against regressions where the broadcast path adds RPCs.

    Composition mirrors the supergroup <=1000 path:
      1  get_entity
      1  GetFullChannelRequest
      5  channels.GetParticipants pages  (iter_participants at 200/page)
      1  messages.Search(ChatPhotos)
    ---
      8  total (within <=9)
    """
    client = _CountingClient()
    ch = _make_channel_mock(id_=-1009999999999, members=1000, is_admin=True)
    client.set_entity(ch)

    full = _full_chat_mock(participants_count=1000)
    client.set_call_responses([full, _empty_search()])
    client.set_iter_participants(list(range(1, 1001)))

    with patch("mcp_telegram.daemon_api.GetFullChannelRequest"), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"), \
         patch("mcp_telegram.daemon_api.InputMessagesFilterChatPhotos"):
        server = _make_server(client=client)
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1009999999999})

    assert r["ok"] is True, f"expected ok=True, got {r!r}"
    assert client.total_rpc_count <= 9, (
        f"HIGH-C: broadcast Channel admin small-group path made "
        f"{client.total_rpc_count} RPCs; SPEC bound is <=9."
    )
