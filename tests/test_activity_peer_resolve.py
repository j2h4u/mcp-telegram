"""Unit tests for activity_peer_resolve.py.

Covers:
  (a) resolve_input_peer returns the input entity for a known dialog_id.
  (b) resolve_input_peer returns None (not raises) when get_input_entity
      raises an access-loss error.
  (c) resolve_linked_chat_id reads from dialogs.linked_chat_resolved_at
      WITHOUT calling GetFullChannel (dialogs cache hit).
  (d) On cold path (no dialogs row or NULL resolved_at) it calls
      GetFullChannel exactly once, UPSERTs result into dialogs, preserves
      sibling fields (about, subscribers_count) in entity_details but does
      NOT write linked_chat_id into detail_json, normalizes to -100… form.
  (e) A channel with no linked chat returns
      LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None).
  (f) When GetFullChannel raises FloodWaitError(seconds=N), the resolver
      returns LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=N)
      WITHOUT sleeping, WITHOUT raising, and WITHOUT touching dialogs.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_telegram.activity_peer_resolve import (
    _ENTITY_DETAIL_TTL_SECONDS,
    LinkedChatResolution,
    resolve_input_peer,
    resolve_linked_chat_id,
)
from mcp_telegram.sync_db import _apply_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


def _insert_entity(conn: sqlite3.Connection, entity_id: int) -> None:
    """Insert a minimal entities row so entity_details FK is satisfied."""
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, type, name, updated_at) VALUES (?, 'channel', 'test', ?)",
        (entity_id, int(time.time())),
    )
    conn.commit()


def _write_entity_details(
    conn: sqlite3.Connection,
    entity_id: int,
    blob: dict,
    fetched_at: int | None = None,
) -> None:
    """Write a row to entity_details for the given entity_id."""
    _insert_entity(conn, entity_id)
    if fetched_at is None:
        fetched_at = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
        (entity_id, json.dumps(blob), fetched_at),
    )
    conn.commit()


def _read_entity_details(conn: sqlite3.Connection, entity_id: int) -> dict | None:
    row = conn.execute(
        "SELECT detail_json FROM entity_details WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _write_dialogs_row(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    linked_chat_id: int | None = None,
    linked_chat_resolved_at: int | None = None,
    name: str | None = None,
    type_: str | None = None,
) -> None:
    """Insert a minimal dialogs row for resolver tests."""
    conn.execute(
        "INSERT OR REPLACE INTO dialogs "
        "(dialog_id, name, type, linked_chat_id, linked_chat_resolved_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (dialog_id, name, type_, linked_chat_id, linked_chat_resolved_at),
    )
    conn.commit()


def _read_dialogs_row(conn: sqlite3.Connection, dialog_id: int) -> dict | None:
    """Read a dialogs row as a dict, or None if absent."""
    row = conn.execute(
        "SELECT dialog_id, linked_chat_id, linked_chat_resolved_at, name, type, hidden "
        "FROM dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "dialog_id": row[0],
        "linked_chat_id": row[1],
        "linked_chat_resolved_at": row[2],
        "name": row[3],
        "type": row[4],
        "hidden": row[5],
    }


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------

class _FakeClient:
    """Minimal fake TelegramClient."""

    def __init__(
        self,
        *,
        input_entity: Any = None,
        input_entity_error: Exception | None = None,
        full_channel_result: Any = None,
        full_channel_error: Exception | None = None,
    ):
        self._input_entity = input_entity
        self._input_entity_error = input_entity_error
        self._full_channel_result = full_channel_result
        self._full_channel_error = full_channel_error
        self.get_input_entity_calls: list[Any] = []
        self.call_calls: list[Any] = []

    async def get_input_entity(self, dialog_id: Any) -> Any:
        self.get_input_entity_calls.append(dialog_id)
        if self._input_entity_error is not None:
            raise self._input_entity_error
        return self._input_entity

    async def __call__(self, request: Any) -> Any:
        self.call_calls.append(request)
        if self._full_channel_error is not None:
            raise self._full_channel_error
        return self._full_channel_result


def _fake_full_channel_result(linked_chat_id: int | None, **kwargs: Any) -> Any:
    """Build a fake GetFullChannelRequest result."""
    full_chat = MagicMock()
    full_chat.linked_chat_id = linked_chat_id
    full_chat.participants_count = kwargs.get("participants_count")
    full_chat.pinned_msg_id = kwargs.get("pinned_msg_id")
    full_chat.about = kwargs.get("about")
    result = MagicMock()
    result.full_chat = full_chat
    return result


# ---------------------------------------------------------------------------
# (a) resolve_input_peer: returns input entity for a known dialog_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_input_peer_returns_entity():
    fake_peer = MagicMock(name="InputPeerChannel")
    client = _FakeClient(input_entity=fake_peer)
    result = await resolve_input_peer(client, -100123456789)
    assert result is fake_peer
    assert len(client.get_input_entity_calls) == 1


# ---------------------------------------------------------------------------
# (b) resolve_input_peer: returns None on access-loss, never raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_input_peer_returns_none_on_access_loss():
    client = _FakeClient(input_entity_error=ValueError("No user has id=-100123"))
    result = await resolve_input_peer(client, -100123456789)
    assert result is None, "Expected None on access-loss, got a value"


@pytest.mark.asyncio
async def test_resolve_input_peer_returns_none_on_key_error():
    """Any exception from get_input_entity must return None, not propagate."""
    client = _FakeClient(input_entity_error=KeyError("session miss"))
    result = await resolve_input_peer(client, -100111111111)
    assert result is None


# ---------------------------------------------------------------------------
# (c) resolve_linked_chat_id: dialogs cache hit — no GetFullChannel call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_linked_chat_id_cache_hit_no_live_call():
    """A dialogs row with non-NULL linked_chat_resolved_at serves the answer
    without calling GetFullChannel (Phase 54: dialogs-first cache substrate)."""
    conn = _make_db()
    channel_id = -100200000001
    linked_id = -100300000001

    # Seed the authoritative answer into dialogs (not entity_details).
    # Any non-NULL linked_chat_resolved_at is the authority signal.
    _write_dialogs_row(
        conn, channel_id,
        linked_chat_id=linked_id,
        linked_chat_resolved_at=int(time.time()) - 99999,  # deliberately old — no TTL
    )

    client = _FakeClient(input_entity=MagicMock())
    result = await resolve_linked_chat_id(client, conn, channel_id)

    assert result.linked_chat_id == linked_id
    assert result.flood_wait_seconds is None
    # GetFullChannel was NOT called
    assert len(client.call_calls) == 0, (
        f"Cache hit must not call GetFullChannel, got {len(client.call_calls)} calls"
    )


# ---------------------------------------------------------------------------
# (d) resolve_linked_chat_id: cache miss — calls GetFullChannel once, merges blob
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_linked_chat_id_cache_miss_calls_once_and_merges():
    """On cache miss: GetFullChannel called once, result merged into existing blob."""
    conn = _make_db()
    channel_id = -100200000002
    raw_linked = 555666777  # positive bare id
    expected_linked = -1002555666777  # -100{raw}

    # Pre-existing blob with an 'about' key that must survive the merge
    _write_entity_details(
        conn, channel_id,
        {"about": "original about text", "subscribers_count": 9999},
        fetched_at=0,  # expired (age = now - 0 >> TTL)
    )

    full_result = _fake_full_channel_result(
        linked_chat_id=raw_linked,
        about="updated about",
        participants_count=10000,
    )
    client = _FakeClient(input_entity=MagicMock(), full_channel_result=full_result)

    result = await resolve_linked_chat_id(client, conn, channel_id)

    # GetFullChannel called exactly once
    assert len(client.call_calls) == 1

    # Normalized to -100… form
    assert result.linked_chat_id is not None
    assert str(result.linked_chat_id).startswith("-100"), (
        f"Expected -100… form, got {result.linked_chat_id}"
    )
    assert result.flood_wait_seconds is None

    # entity_details blob was written back with sibling fields preserved.
    # Phase 54: linked_chat_id is now owned by dialogs — it must NOT appear
    # in entity_details.detail_json.
    written = _read_entity_details(conn, channel_id)
    assert written is not None
    assert "linked_chat_id" not in written, (
        "linked_chat_id must NOT be written into entity_details (Phase 54 contract)"
    )
    # The merge preserved the about field (updated from the fresh result)
    assert "about" in written
    # The live result wrote its row into dialogs instead
    dialogs_row = _read_dialogs_row(conn, channel_id)
    assert dialogs_row is not None
    assert dialogs_row["linked_chat_id"] is not None
    assert str(dialogs_row["linked_chat_id"]).startswith("-100")


@pytest.mark.asyncio
async def test_resolve_linked_chat_id_preserves_existing_keys():
    """Blob merge must not clobber keys NOT returned by GetFullChannel."""
    conn = _make_db()
    channel_id = -100200000003

    # Pre-existing blob with a custom key
    _write_entity_details(
        conn, channel_id,
        {"subscribers_count": 1234, "some_extra_key": "preserved"},
        fetched_at=0,  # expired
    )

    full_result = _fake_full_channel_result(linked_chat_id=None)
    client = _FakeClient(input_entity=MagicMock(), full_channel_result=full_result)
    await resolve_linked_chat_id(client, conn, channel_id)

    written = _read_entity_details(conn, channel_id)
    assert written is not None
    assert "some_extra_key" in written, "Pre-existing key must survive blob merge"
    assert written["some_extra_key"] == "preserved"


# ---------------------------------------------------------------------------
# (e) Channel with no discussion group → linked_chat_id=None, flood_wait=None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_linked_chat_id_no_discussion_group():
    """Channel with no linked chat returns linked_chat_id=None, flood_wait_seconds=None."""
    conn = _make_db()
    channel_id = -100200000004

    full_result = _fake_full_channel_result(linked_chat_id=None)
    client = _FakeClient(input_entity=MagicMock(), full_channel_result=full_result)

    result = await resolve_linked_chat_id(client, conn, channel_id)

    assert result.linked_chat_id is None
    assert result.flood_wait_seconds is None


# ---------------------------------------------------------------------------
# (f) FloodWaitError → returns flood_wait_seconds, no sleep, no raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_linked_chat_id_flood_wait_no_sleep():
    """FloodWaitError returns flood_wait_seconds set, does NOT sleep, does NOT raise."""
    from telethon.errors import FloodWaitError

    conn = _make_db()
    channel_id = -100200000005

    flood_error = FloodWaitError(request=None, capture=120)
    client = _FakeClient(
        input_entity=MagicMock(),
        full_channel_error=flood_error,
    )

    # Should not raise, and should not sleep (asyncio.sleep not patched —
    # if it's called, the test will block or fail in CI timeout)
    result = await resolve_linked_chat_id(client, conn, channel_id)

    assert result.linked_chat_id is None
    assert result.flood_wait_seconds == 120, (
        f"Expected flood_wait_seconds=120, got {result.flood_wait_seconds}"
    )


@pytest.mark.asyncio
async def test_resolve_linked_chat_id_flood_wait_distinct_from_no_group():
    """FloodWait is distinguishable from 'no discussion group' by flood_wait_seconds field."""
    from telethon.errors import FloodWaitError

    conn = _make_db()
    channel_id = -100200000006
    flood_error = FloodWaitError(request=None, capture=60)
    client = _FakeClient(input_entity=MagicMock(), full_channel_error=flood_error)

    flood_result = await resolve_linked_chat_id(client, conn, channel_id)

    # No-discussion-group result
    channel_id2 = -100200000007
    full_result = _fake_full_channel_result(linked_chat_id=None)
    client2 = _FakeClient(input_entity=MagicMock(), full_channel_result=full_result)
    no_group_result = await resolve_linked_chat_id(client2, conn, channel_id2)

    assert flood_result.flood_wait_seconds is not None
    assert no_group_result.flood_wait_seconds is None
    # Both have linked_chat_id=None
    assert flood_result.linked_chat_id is None
    assert no_group_result.linked_chat_id is None


# ---------------------------------------------------------------------------
# LinkedChatResolution dataclass
# ---------------------------------------------------------------------------

def test_linked_chat_resolution_fields():
    r = LinkedChatResolution(linked_chat_id=-100123, flood_wait_seconds=None)
    assert r.linked_chat_id == -100123
    assert r.flood_wait_seconds is None

    r2 = LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=30)
    assert r2.linked_chat_id is None
    assert r2.flood_wait_seconds == 30


# ---------------------------------------------------------------------------
# TTL constant
# ---------------------------------------------------------------------------

def test_entity_detail_ttl_constant():
    assert isinstance(_ENTITY_DETAIL_TTL_SECONDS, int)
    assert _ENTITY_DETAIL_TTL_SECONDS > 0


# ---------------------------------------------------------------------------
# Phase 54 TDD tests: dialogs cache substrate
# ---------------------------------------------------------------------------

# Task 4: dialogs cache hit does NOT call GetFullChannelRequest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_linked_chat_id_dialogs_cache_hit_returns_without_telethon_call():
    """Pre-resolved channel: resolver returns the dialogs row without any Telethon call."""
    conn = _make_db()
    channel_id = -1001234567890
    expected_linked = -1009876543210

    _write_dialogs_row(
        conn, channel_id,
        linked_chat_id=expected_linked,
        linked_chat_resolved_at=int(time.time()) - 99999,  # deliberately old — no TTL
    )

    # _FakeClient records all calls; on a cache hit, none should be made
    client = _FakeClient(input_entity=MagicMock())

    result = await resolve_linked_chat_id(client, conn, channel_id)

    assert result == LinkedChatResolution(
        linked_chat_id=expected_linked, flood_wait_seconds=None
    )
    assert len(client.get_input_entity_calls) == 0, (
        "get_input_entity must not be called on a dialogs cache hit"
    )
    assert len(client.call_calls) == 0, (
        "GetFullChannelRequest must not be called on a dialogs cache hit"
    )


@pytest.mark.asyncio
async def test_resolve_linked_chat_id_dialogs_cache_hit_null_linked_chat():
    """dialogs row with linked_chat_id=NULL and non-NULL resolved_at = definitively no
    discussion group. Returns linked_chat_id=None, flood_wait_seconds=None with zero
    Telethon calls."""
    conn = _make_db()
    channel_id = -1001234567891

    # NULL linked_chat_id + NOT-NULL resolved_at = "we asked, no linked chat exists"
    _write_dialogs_row(
        conn, channel_id,
        linked_chat_id=None,
        linked_chat_resolved_at=int(time.time()) - 50,
    )

    client = _FakeClient(input_entity=MagicMock())

    result = await resolve_linked_chat_id(client, conn, channel_id)

    assert result == LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)
    assert len(client.get_input_entity_calls) == 0, (
        "get_input_entity must not be called on a dialogs cache hit"
    )
    assert len(client.call_calls) == 0, (
        "GetFullChannelRequest must not be called on a dialogs cache hit"
    )


# Task 5: cold path UPSERTs into dialogs and does not pollute detail_json
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_linked_chat_id_cold_path_upserts_dialogs():
    """Cold path (no dialogs row): live fetch UPSERTs into dialogs; linked_chat_id
    absent from entity_details.detail_json; sibling fields present in detail_json."""
    conn = _make_db()
    channel_id = -1001111111111
    raw_linked = 2222222222  # positive bare id → resolver normalises to -1002222222222

    full_result = _fake_full_channel_result(
        linked_chat_id=raw_linked,
        participants_count=5000,
        about="Test channel about",
        pinned_msg_id=42,
    )
    # No chats list → no title/username lookup for entities row
    full_result.chats = []

    client = _FakeClient(input_entity=MagicMock(), full_channel_result=full_result)

    before_call = int(time.time())
    result = await resolve_linked_chat_id(client, conn, channel_id)
    after_call = int(time.time())

    # Return value
    assert result.linked_chat_id == -1002222222222
    assert result.flood_wait_seconds is None

    # dialogs row was UPSERTed
    dr = _read_dialogs_row(conn, channel_id)
    assert dr is not None, "dialogs row must be created by cold-path UPSERT"
    assert dr["linked_chat_id"] == -1002222222222
    assert dr["linked_chat_resolved_at"] is not None
    assert before_call <= dr["linked_chat_resolved_at"] <= after_call + 1, (
        f"resolved_at={dr['linked_chat_resolved_at']} not within 5s of now"
    )

    # Resolver only owns the two linked-chat columns — other columns at defaults
    assert dr["name"] is None, "resolver must not write name into dialogs"
    assert dr["type"] is None, "resolver must not write type into dialogs"
    assert dr["hidden"] == 0, "resolver must not write hidden into dialogs"

    # entity_details: sibling fields present, linked_chat_id absent
    ed = _read_entity_details(conn, channel_id)
    assert ed is not None
    assert "linked_chat_id" not in ed, (
        "linked_chat_id must NOT appear in entity_details (Phase 54 contract)"
    )
    assert "subscribers_count" in ed
    assert "about" in ed
    assert "pinned_msg_id" in ed

    # Second call: dialogs cache now hot — zero further GetFullChannelRequest calls
    call_count_before = len(client.call_calls)
    result2 = await resolve_linked_chat_id(client, conn, channel_id)
    assert result2 == result
    assert len(client.call_calls) == call_count_before, (
        "Second call must use dialogs cache — no further GetFullChannelRequest"
    )


# Task 6: schema-floor assertion raises RuntimeError on sub-v24 connections
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_linked_chat_id_schema_floor_raises_on_v23():
    """A connection with schema_version < 24 raises RuntimeError with a greppable message."""
    import sqlite3 as _sqlite3

    raw_conn = _sqlite3.connect(":memory:")
    # Create schema_version table and insert a sub-v24 version
    raw_conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)")
    raw_conn.execute("INSERT INTO schema_version VALUES (23, 1000000)")
    raw_conn.commit()

    # The assertion fires before any Telethon call — client is never touched
    client = _FakeClient(input_entity=MagicMock())

    with pytest.raises(RuntimeError, match=r"requires schema v24\+"):
        await resolve_linked_chat_id(client, raw_conn, -1004444444444)

    # Confirm client was never touched
    assert len(client.get_input_entity_calls) == 0
    assert len(client.call_calls) == 0


@pytest.mark.asyncio
async def test_resolve_linked_chat_id_schema_floor_passes_on_v24():
    """A connection migrated to v24 via _apply_migrations does NOT raise RuntimeError."""
    conn = _make_db()  # _apply_migrations → schema_version includes v24

    channel_id = -1005555555555
    # Seed a resolved dialogs row so the resolver returns without a Telethon call
    _write_dialogs_row(
        conn, channel_id,
        linked_chat_id=-1006666666666,
        linked_chat_resolved_at=int(time.time()),
    )

    client = _FakeClient(input_entity=MagicMock())

    # Must not raise
    result = await resolve_linked_chat_id(client, conn, channel_id)
    assert result.linked_chat_id == -1006666666666
    assert result.flood_wait_seconds is None


# Task 7: FloodWait leaves dialogs untouched (retry signal preserved)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_linked_chat_id_flood_wait_leaves_dialogs_untouched():
    """FloodWaitError must NOT touch dialogs; resolved_at stays NULL so the next
    sweep pass retries naturally (D-08 contract)."""
    from telethon.errors import FloodWaitError

    conn = _make_db()
    channel_id = -1003333333333

    # Seed a dialogs row in "never asked" state (NULL, NULL)
    _write_dialogs_row(
        conn, channel_id,
        linked_chat_id=None,
        linked_chat_resolved_at=None,
    )

    flood_error = FloodWaitError(request=None, capture=42)
    client = _FakeClient(
        input_entity=MagicMock(),
        full_channel_error=flood_error,
    )

    result = await resolve_linked_chat_id(client, conn, channel_id)

    # Return value
    assert result.linked_chat_id is None
    assert result.flood_wait_seconds == 42

    # dialogs row UNCHANGED — resolved_at still NULL (the retry signal)
    dr = _read_dialogs_row(conn, channel_id)
    assert dr is not None
    assert dr["linked_chat_id"] is None, (
        "FloodWait must NOT write linked_chat_id into dialogs"
    )
    assert dr["linked_chat_resolved_at"] is None, (
        "FloodWait must NOT set resolved_at — NULL IS the retry signal (D-08)"
    )

    # No additional rows inserted
    row_count = conn.execute(
        "SELECT COUNT(*) FROM dialogs WHERE dialog_id = ?", (channel_id,)
    ).fetchone()[0]
    assert row_count == 1, "exactly one dialogs row for this channel"
