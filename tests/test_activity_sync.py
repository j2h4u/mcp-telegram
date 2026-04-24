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


def _msg(msg_id: int, user_id: int, ts: int, text: str = "hi", replies: int = 0) -> FakeMessage:
    return FakeMessage(
        id=msg_id,
        date=datetime.fromtimestamp(ts, tz=timezone.utc),
        message=text,
        peer_id=PeerUser(user_id=user_id),
        replies=FakeReplies(replies=replies) if replies else None,
        reactions=None,
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

    rows = conn.execute("SELECT message_id, dialog_id, sent_at FROM activity_comments ORDER BY message_id").fetchall()
    assert rows == [(99, 42, 1_700_000_090), (100, 42, 1_700_000_100)]
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
    # Mark backfill complete, pre-seed one row
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.execute(
            "INSERT INTO activity_comments (dialog_id, message_id, sent_at, text, reactions, reply_count, last_synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (42, 50, 1000, "old", None, 0, 1000),
        )

    new_msg = _msg(51, 42, 2000, text="new")
    old_msg = _msg(49, 42, 900, text="skip")  # should break the loop
    client = _FakeClient(batches=[], iter_msgs=[new_msg, old_msg])
    shutdown = asyncio.Event()
    await _run_incremental(client, conn, shutdown)

    ids = [r[0] for r in conn.execute("SELECT message_id FROM activity_comments ORDER BY message_id").fetchall()]
    assert ids == [50, 51]


@pytest.mark.asyncio
async def test_incremental_skipped_before_backfill_complete(tmp_path):
    conn = _make_db(tmp_path)
    client = _FakeClient(batches=[], iter_msgs=[_msg(100, 42, 1_700_000_000)])
    shutdown = asyncio.Event()
    await _run_incremental(client, conn, shutdown)
    count = conn.execute("SELECT COUNT(*) FROM activity_comments").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_incremental_skipped_when_activity_empty(tmp_path):
    """W5: backfill may complete with zero own messages — incremental must no-op."""
    conn = _make_db(tmp_path)
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
    # iter_messages should never be awaited — verify by supplying an iterator that
    # would insert a row if touched.
    client = _FakeClient(batches=[], iter_msgs=[_msg(100, 42, 1_700_000_000)])
    shutdown = asyncio.Event()
    await _run_incremental(client, conn, shutdown)
    count = conn.execute("SELECT COUNT(*) FROM activity_comments").fetchone()[0]
    assert count == 0


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
