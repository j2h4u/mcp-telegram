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
    anchor_ts = 1_700_000_000
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.execute("INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES ('last_sync_at', ?)",
                     (str(anchor_ts),))
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (?, ?, ?, ?, 1, 0, 0)",
            (42, 50, anchor_ts - 100, "old"),
        )

    # Incremental uses SearchRequest(min_date=anchor_ts-60) — message sent_at=anchor_ts+100
    # is newer and must be returned. First batch: id=51. Second batch: empty → done.
    new_msg = _msg(51, 42, anchor_ts + 100, text="new")
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
    """W5: backfill complete but last_sync_at not set — incremental must no-op."""
    conn = _make_db(tmp_path)
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
    # Client should never be called — last_sync_at == 0.
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
    # Anchor is now timestamp-based (min_date), not min_id. A message in a
    # different dialog with a low per-chat message_id but a recent sent_at
    # must be captured — this was the original failure mode.
    anchor_ts = 1_700_000_000
    conn = _make_db(tmp_path)
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.execute("INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES ('last_sync_at', ?)",
                     (str(anchor_ts),))
        # Seed an old own-message in dialog 42.
        conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 50, 1700000000, 'mine-old', 1, 0, 0)",
        )
        # Seed an incoming row with a high message_id in dialog 42.
        # Under the old min_id anchor this would have blocked id=51 from being fetched.
        # Under the new min_date anchor it is irrelevant.
        conn.execute(
            "INSERT INTO messages "
            "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
            "VALUES (42, 99999, 1700000010, 'incoming-high-id', 0, 0, 0)",
        )
    # New own-message in dialog 99 with message_id=3 (low per-chat id, newer by date).
    # Old min_id logic would skip it (3 < 50). Timestamp logic must find it.
    client = _FakeClient(batches=[
        FakeSearchResult(messages=[_msg(3, 99, anchor_ts + 100)]),
        FakeSearchResult(messages=[]),
    ])
    shutdown = asyncio.Event()
    await _run_incremental(client, conn, shutdown)
    own_ids = sorted(
        r[0] for r in conn.execute(
            "SELECT message_id FROM messages WHERE out = 1"
        ).fetchall()
    )
    assert own_ids == [3, 50], (
        f"Incremental must capture id=3 from dialog 99 despite low per-chat id; got {own_ids}"
    )
    decoy_still_present = conn.execute(
        "SELECT out FROM messages WHERE dialog_id=42 AND message_id=99999"
    ).fetchone()
    assert decoy_still_present is not None and decoy_still_present[0] == 0, (
        "Incoming row must remain unchanged"
    )
