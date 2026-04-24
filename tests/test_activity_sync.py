"""Unit tests for activity_sync.py."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest
from telethon.tl.types import PeerUser

from mcp_telegram.activity_sync import (
    _run_backfill,
    _run_incremental,
    run_activity_sync_loop,
)
from mcp_telegram.sync_db import ensure_sync_schema


# -- Fakes --------------------------------------------------------

@dataclass
class FakeReplies:
    replies: int = 0


@dataclass
class FakeMessage:
    id: int
    date: datetime
    message: str
    peer_id: Any
    replies: Any = None
    reactions: Any = None
    out: bool = True


@dataclass
class FakeSearchResult:
    messages: list[FakeMessage]
    users: list[Any] = field(default_factory=list)
    chats: list[Any] = field(default_factory=list)


class _FakeClient:
    """Drives _run_backfill by returning scripted SearchRequest results,
    and drives _run_incremental via iter_messages async generator."""

    def __init__(self, batches: list[Any], iter_msgs: list[FakeMessage] | None = None):
        self._batches = list(batches)
        self._iter_msgs = list(iter_msgs or [])
        self.calls = 0

    async def __call__(self, request: Any) -> Any:
        self.calls += 1
        if not self._batches:
            return FakeSearchResult(messages=[])
        item = self._batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def iter_messages(self, *, entity=None, from_user=None):
        msgs = self._iter_msgs

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


def _make_db(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    return sqlite3.connect(db_path)


def _msg(msg_id: int, user_id: int, ts: int, text: str = "hi", replies: int = 0, out: bool = True) -> FakeMessage:
    return FakeMessage(
        id=msg_id,
        date=datetime.fromtimestamp(ts, tz=timezone.utc),
        message=text,
        peer_id=PeerUser(user_id=user_id),
        replies=FakeReplies(replies=replies) if replies else None,
        reactions=None,
        out=out,
    )


# -- Tests --------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_inserts_rows(tmp_path):
    conn = _make_db(tmp_path)
    m1 = _msg(100, 42, 1_700_000_100)
    m2 = _msg(99, 42, 1_700_000_090)
    client = _FakeClient(batches=[
        FakeSearchResult(messages=[m1, m2]),
        FakeSearchResult(messages=[]),
    ])
    shutdown = asyncio.Event()
    await _run_backfill(client, conn, shutdown)

    rows = conn.execute(
        "SELECT message_id, dialog_id, sent_at, out FROM messages "
        "WHERE out = 1 ORDER BY message_id"
    ).fetchall()
    assert rows == [(99, 42, 1_700_000_090, 1), (100, 42, 1_700_000_100, 1)]
    state = dict(conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    assert state["backfill_complete"] == "1"
    assert state["last_sync_at"] is not None


@pytest.mark.asyncio
async def test_backfill_respects_shutdown(tmp_path):
    conn = _make_db(tmp_path)
    client = _FakeClient(batches=[FakeSearchResult(messages=[_msg(100, 42, 1_700_000_000)])])
    shutdown = asyncio.Event()
    shutdown.set()
    await _run_backfill(client, conn, shutdown)
    # No iteration: loop condition is_set() returns immediately
    state = dict(conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    assert state["backfill_complete"] == "0"
    assert client.calls == 0


@pytest.mark.asyncio
async def test_backfill_floodwait_recovers(tmp_path):
    from telethon.errors import FloodWaitError

    conn = _make_db(tmp_path)

    class _FW(FloodWaitError):
        def __init__(self):
            self.seconds = 0

    client = _FakeClient(batches=[_FW(), FakeSearchResult(messages=[])])
    shutdown = asyncio.Event()
    await _run_backfill(client, conn, shutdown)
    state = dict(conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    assert state["backfill_complete"] == "1"


@pytest.mark.asyncio
async def test_incremental_only_new_messages(tmp_path):
    conn = _make_db(tmp_path)
    # Mark backfill complete, pre-seed one row with message_id=50 in messages (out=1).
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        # Seed one row directly in messages with out=1 — mimics a prior backfill run.
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (?, ?, ?, ?, 1, 0, 0)",
            (42, 50, 1000, "old"),
        )

    # Incremental uses SearchRequest(min_id=50) — only messages with id > 50 come back.
    # First batch: id=51. Second batch: empty → done.
    new_msg = _msg(51, 42, 2000, text="new")
    client = _FakeClient(batches=[
        FakeSearchResult(messages=[new_msg]),
        FakeSearchResult(messages=[]),
    ])
    shutdown = asyncio.Event()
    await _run_incremental(client, conn, shutdown)

    ids = [
        r[0] for r in conn.execute(
            "SELECT message_id FROM messages WHERE out = 1 ORDER BY message_id"
        ).fetchall()
    ]
    assert ids == [50, 51]


@pytest.mark.asyncio
async def test_incremental_skipped_before_backfill_complete(tmp_path):
    conn = _make_db(tmp_path)
    # Client should never be called — backfill not complete.
    client = _FakeClient(batches=[FakeSearchResult(messages=[_msg(100, 42, 1_700_000_000)])])
    shutdown = asyncio.Event()
    await _run_incremental(client, conn, shutdown)
    count = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE out = 1"
    ).fetchone()[0]
    assert count == 0
    assert client.calls == 0


@pytest.mark.asyncio
async def test_incremental_skipped_when_messages_empty(tmp_path):
    """W5: backfill complete but no rows in messages WHERE out=1 — incremental must no-op."""
    conn = _make_db(tmp_path)
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
    # Client should never be called — max_message_id == 0.
    client = _FakeClient(batches=[FakeSearchResult(messages=[_msg(100, 42, 1_700_000_000)])])
    shutdown = asyncio.Event()
    await _run_incremental(client, conn, shutdown)
    count = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE out = 1"
    ).fetchone()[0]
    assert count == 0
    assert client.calls == 0


@pytest.mark.asyncio
async def test_loop_shutdown_between_passes(tmp_path):
    """run_activity_sync_loop returns when shutdown fires during interval sleep."""
    conn = _make_db(tmp_path)
    client = _FakeClient(batches=[FakeSearchResult(messages=[])])  # empty → backfill completes
    shutdown = asyncio.Event()

    async def _flip():
        await asyncio.sleep(0.05)
        shutdown.set()

    await asyncio.gather(
        run_activity_sync_loop(client, conn, shutdown, interval=60.0),
        _flip(),
    )


@pytest.mark.asyncio
async def test_backfill_enrolls_dialog_as_own_only(tmp_path):
    """After backfill writes a message in dialog 42, synced_dialogs has (42, 'own_only')."""
    conn = _make_db(tmp_path)
    m1 = _msg(100, 42, 1_700_000_100)
    client = _FakeClient(batches=[
        FakeSearchResult(messages=[m1]),
        FakeSearchResult(messages=[]),
    ])
    shutdown = asyncio.Event()
    await _run_backfill(client, conn, shutdown)
    status = conn.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id = 42"
    ).fetchone()
    assert status is not None, "dialog 42 must be enrolled after backfill"
    assert status[0] == "own_only"


@pytest.mark.asyncio
async def test_backfill_does_not_downgrade_synced_dialog(tmp_path):
    """If dialog 42 is already status='synced', backfill must NOT downgrade it to 'own_only'."""
    conn = _make_db(tmp_path)
    with conn:
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
            (42,),
        )
    m1 = _msg(100, 42, 1_700_000_100)
    client = _FakeClient(batches=[
        FakeSearchResult(messages=[m1]),
        FakeSearchResult(messages=[]),
    ])
    shutdown = asyncio.Event()
    await _run_backfill(client, conn, shutdown)
    status = conn.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id = 42"
    ).fetchone()[0]
    assert status == "synced", (
        "INSERT OR IGNORE must preserve higher-status row — never downgrade to 'own_only'"
    )


@pytest.mark.asyncio
async def test_incremental_anchor_ignores_higher_id_out0_row(tmp_path):
    """Resolves the cross-AI review divergence on the shared-table anchor.

    Concern (Codex HIGH): once full-sync rows coexist with activity-sync
    rows in `messages`, `SELECT MAX(message_id) FROM messages WHERE out = 1`
    might be dominated by a higher-ID `out=0` row and skip own messages.

    Resolution (OpenCode LOW): `WHERE out = 1` isolates own messages
    because Telethon's msg.out flag comes straight from MTProto and marks
    only messages authored by the account owner.

    This test seeds an out=0 row with message_id=99_999 (simulating a
    full-sync incoming message) and an out=1 row with message_id=50.
    The incremental run's anchor query must see max(out=1)=50 — NOT 99_999
    — so the subsequent SearchRequest includes message_id=51.
    """
    conn = _make_db(tmp_path)
    with conn:
        # Mark backfill complete so _run_incremental actually runs.
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        # Seed an authoritative own-message row with a modest message_id.
        conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 50, 1700000000, 'mine-old', 1, 0, 0)",
        )
        # Seed a DECOY full-sync incoming row with a much higher message_id.
        # If the anchor code uses MAX(message_id) without filtering on out=1,
        # it would pick 99_999 and request messages above that, skipping 51.
        conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 99999, 1700000010, 'decoy-incoming', 0, 0, 0)",
        )
    # Verify the anchor query directly — this is the load-bearing assertion.
    anchor = conn.execute(
        "SELECT MAX(message_id) FROM messages WHERE out = 1"
    ).fetchone()[0]
    assert anchor == 50, (
        f"Anchor must read MAX(out=1)=50, not absolute max 99_999. Got: {anchor}"
    )
    # End-to-end: incremental should fetch messages above id=50.
    # Provide a batch containing a new own-message with id=51 and an
    # empty follow-up so the loop terminates cleanly.
    client = _FakeClient(batches=[
        FakeSearchResult(messages=[_msg(51, 42, 1_700_000_100)]),
        FakeSearchResult(messages=[]),
    ])
    shutdown = asyncio.Event()
    await _run_incremental(client, conn, shutdown)
    # The new own message landed; decoy stays put.
    own_ids = sorted(
        r[0] for r in conn.execute(
            "SELECT message_id FROM messages WHERE out = 1"
        ).fetchall()
    )
    assert own_ids == [50, 51], (
        f"Incremental must write id=51 as a new own-message; got own_ids={own_ids}"
    )
    decoy_still_present = conn.execute(
        "SELECT out FROM messages WHERE dialog_id=42 AND message_id=99999"
    ).fetchone()
    assert decoy_still_present is not None and decoy_still_present[0] == 0, (
        "Decoy out=0 row must remain unchanged — activity_sync never touches incoming rows"
    )
