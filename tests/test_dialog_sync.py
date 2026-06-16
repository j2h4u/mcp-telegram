"""Tests for DialogsBootstrapWorker — Phase 41 Bootstrap Sweep.

Covers BOOTSTRAP-01..06 + Phase 41 review findings:
- Fresh-start populates dialogs and writes status=complete.
- Idempotent skip when status=complete.
- Resumable cursor: FloodWait mid-sweep + restart picks up offsets.
- D-12 UPSERT recency guard: newer rows survive an older bootstrap.
- D-13 FloodWait sleep is interruptible by shutdown_event.
- RPCError exits without 'complete' AND surfaces "bootstrap sweep stalled (RPCError)" via startup_detail.
- hidden=1 column is preserved across UPSERT (D-11).
- Worker opens its OWN dedicated connection — does not require the caller to pre-open one.
- Malformed daemon_state cursor (corrupt offset_date) is recovered: WARNING + cursor cleared + sweep proceeds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_telegram.dialog_sync import (
    DialogsBootstrapWorker,
    _clear_cursor,
    _decode_offset_peer,
    _encode_offset_peer,
    _get_state,
    _set_state,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema

# ---------------------------------------------------------------------------
# Entity + dialog factories
# ---------------------------------------------------------------------------


def _make_user_entity(
    uid: int,
    *,
    first_name: str = "Test",
    last_name: str = "",
    access_hash: int = 12345,
    bot: bool = False,
) -> Any:
    from telethon.tl.types import User

    return User(
        id=uid,
        access_hash=access_hash,
        first_name=first_name,
        last_name=last_name,
        bot=bot,
    )


def _make_chat_entity(
    cid: int,
    *,
    title: str = "Group",
    participants_count: int | None = 5,
) -> Any:
    from datetime import datetime

    from telethon.tl.types import Chat

    return Chat(
        id=cid,
        title=title,
        photo=None,
        participants_count=participants_count or 0,
        date=datetime(2024, 1, 1, tzinfo=UTC),
        version=1,
    )


def _make_channel_entity(
    cid: int,
    *,
    title: str = "Channel",
    broadcast: bool = True,
    participants_count: int | None = 100,
    access_hash: int = 9999,
    date: Any | None = None,
) -> Any:
    from datetime import datetime

    from telethon.tl.types import Channel

    return Channel(
        id=cid,
        title=title,
        photo=None,
        date=date if date is not None else datetime(2024, 1, 1, tzinfo=UTC),
        broadcast=broadcast,
        megagroup=not broadcast,
        access_hash=access_hash,
        participants_count=participants_count,
    )


def _make_dialog(
    dialog_id: int,
    entity: Any,
    *,
    message_date: Any | None = None,
    pinned: bool = False,
    folder_id: int | None = None,
    unread_mentions_count: int = 0,
    unread_reactions_count: int = 0,
    draft_message: str | None = None,
) -> SimpleNamespace:
    from datetime import datetime

    if message_date is None:
        message_date = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    msg = SimpleNamespace(date=message_date)
    draft = SimpleNamespace(message=draft_message) if draft_message is not None else None
    return SimpleNamespace(
        id=dialog_id,
        entity=entity,
        date=message_date,
        message=msg,
        pinned=pinned,
        folder_id=folder_id,
        unread_mentions_count=unread_mentions_count,
        unread_reactions_count=unread_reactions_count,
        draft=draft,
    )


async def _async_gen(items):
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """On-disk sync.db with v18 schema applied."""
    p = tmp_path / "sync.db"
    ensure_sync_schema(p)
    return p


def _make_worker(
    db_path: Path,
    dialogs: list[Any],
    shutdown_event: asyncio.Event | None = None,
) -> tuple[DialogsBootstrapWorker, MagicMock, asyncio.Event]:
    client = MagicMock()
    client.iter_dialogs = MagicMock(side_effect=lambda **kw: _async_gen(dialogs))
    if shutdown_event is None:
        shutdown_event = asyncio.Event()
    worker = DialogsBootstrapWorker(client, db_path, shutdown_event)
    return worker, client, shutdown_event


# ---------------------------------------------------------------------------
# TestDialogsBootstrapWorker
# ---------------------------------------------------------------------------


class TestDialogsBootstrapWorker:
    @pytest.mark.asyncio
    async def test_fresh_start_populates_dialogs_and_writes_complete(self, db_path):
        # D-08: each entity branch (User/Chat/Channel) yields the right `type`.
        # D-09/D-10: unread/draft fields written from Dialog object directly.
        dialogs = [
            _make_dialog(111, _make_user_entity(111, first_name="Alice")),
            _make_dialog(-2002, _make_chat_entity(2002, title="Family")),
            _make_dialog(-1001234567890, _make_channel_entity(1234567890, title="News")),
        ]
        worker, client, _ = _make_worker(db_path, dialogs)
        n = await worker.run()
        assert n == 3
        # Worker closed its own connection — open a fresh one to inspect.
        conn = _open_sync_db(db_path)
        try:
            assert _get_state(conn, "bootstrap_sweep_status") == "complete"
            rows = list(conn.execute("SELECT dialog_id, name, type FROM dialogs ORDER BY dialog_id"))
            assert len(rows) == 3
            types_by_id = {r[0]: r[2] for r in rows}
            assert types_by_id[111] == "user"
            assert types_by_id[-2002] == "group"
            assert types_by_id[-1001234567890] == "channel"
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_second_run_after_complete_skips(self, db_path):
        dialogs = [_make_dialog(7, _make_user_entity(7))]
        worker1, _, _ = _make_worker(db_path, dialogs)
        await worker1.run()

        # Second worker on same db_path — must skip iter_dialogs entirely.
        worker2, client2, _ = _make_worker(db_path, dialogs)
        n2 = await worker2.run()
        assert n2 == 0
        client2.iter_dialogs.assert_not_called()

    @pytest.mark.asyncio
    async def test_floodwait_mid_sweep_checkpoints_and_returns_partial(self, db_path):
        from telethon.errors import FloodWaitError

        dialogs_before_flood = [
            _make_dialog(11, _make_user_entity(11, first_name="A")),
            _make_dialog(22, _make_user_entity(22, first_name="B")),
        ]

        async def flooding_gen(items):
            for it in items:
                yield it
            raise FloodWaitError(request=None, capture=2)  # type: ignore[call-arg]

        client = MagicMock()
        client.iter_dialogs = MagicMock(side_effect=lambda **kw: flooding_gen(dialogs_before_flood))
        shutdown_event = asyncio.Event()
        worker = DialogsBootstrapWorker(client, db_path, shutdown_event)

        # Trip shutdown asynchronously so the dialogs are processed first,
        # then the FloodWait sleep is interrupted.
        async def trip_after_delay():
            await asyncio.sleep(0.05)
            shutdown_event.set()

        n, _ = await asyncio.gather(worker.run(), trip_after_delay())
        assert n == 2

        conn = _open_sync_db(db_path)
        try:
            assert _get_state(conn, "bootstrap_sweep_status") == "in_progress"
            assert _get_state(conn, "bootstrap_sweep_offset_id") == "22"
            assert _get_state(conn, "bootstrap_sweep_offset_peer") is not None
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_resume_passes_saved_offsets_to_iter_dialogs(self, db_path):
        # Pre-seed daemon_state with a saved cursor.
        seed_conn = _open_sync_db(db_path)
        try:
            with seed_conn:
                _set_state(seed_conn, "bootstrap_sweep_status", "in_progress")
                _set_state(
                    seed_conn,
                    "bootstrap_sweep_offset_date",
                    "2024-06-01T12:00:00+00:00",
                )
                _set_state(seed_conn, "bootstrap_sweep_offset_id", "22")
                _set_state(
                    seed_conn,
                    "bootstrap_sweep_offset_peer",
                    json.dumps({"type": "user", "id": 22, "access_hash": 12345}),
                )
        finally:
            seed_conn.close()

        captured_kwargs: dict[str, Any] = {}
        client = MagicMock()

        def _capture(**kw):
            captured_kwargs.update(kw)
            return _async_gen([_make_dialog(33, _make_user_entity(33))])

        client.iter_dialogs = MagicMock(side_effect=_capture)
        shutdown_event = asyncio.Event()
        worker = DialogsBootstrapWorker(client, db_path, shutdown_event)
        n = await worker.run()
        assert n == 1
        assert captured_kwargs.get("offset_id") == 22
        assert captured_kwargs.get("offset_date") is not None
        assert captured_kwargs.get("offset_peer") is not None

    @pytest.mark.asyncio
    async def test_corrupt_cursor_recovers_by_clearing_and_restarting(self, db_path, caplog):
        # Review MEDIUM: malformed offset_date must NOT brick the daemon —
        # worker logs a WARNING, clears cursor keys, restarts from scratch.
        seed_conn = _open_sync_db(db_path)
        try:
            with seed_conn:
                _set_state(seed_conn, "bootstrap_sweep_status", "in_progress")
                _set_state(seed_conn, "bootstrap_sweep_offset_date", "NOT-AN-ISO-DATE")
                _set_state(seed_conn, "bootstrap_sweep_offset_id", "22")
                _set_state(
                    seed_conn,
                    "bootstrap_sweep_offset_peer",
                    json.dumps({"type": "user", "id": 22, "access_hash": 12345}),
                )
        finally:
            seed_conn.close()

        client = MagicMock()
        client.iter_dialogs = MagicMock(side_effect=lambda **kw: _async_gen([_make_dialog(99, _make_user_entity(99))]))
        shutdown_event = asyncio.Event()
        worker = DialogsBootstrapWorker(client, db_path, shutdown_event)

        with caplog.at_level(logging.WARNING, logger="mcp_telegram.dialog_sync"):
            n = await worker.run()

        # Worker recovered: dialog processed, sweep completed.
        assert n == 1
        # WARNING about corrupt cursor present.
        assert any("cursor corrupt" in rec.message for rec in caplog.records)

        conn = _open_sync_db(db_path)
        try:
            assert _get_state(conn, "bootstrap_sweep_status") == "complete"
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_corrupt_offset_peer_json_recovers(self, db_path, caplog):
        # Same recovery path triggered by JSONDecodeError on offset_peer.
        seed_conn = _open_sync_db(db_path)
        try:
            with seed_conn:
                _set_state(seed_conn, "bootstrap_sweep_offset_peer", "{not-json")
        finally:
            seed_conn.close()

        client = MagicMock()
        client.iter_dialogs = MagicMock(side_effect=lambda **kw: _async_gen([_make_dialog(1, _make_user_entity(1))]))
        shutdown_event = asyncio.Event()
        worker = DialogsBootstrapWorker(client, db_path, shutdown_event)
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.dialog_sync"):
            n = await worker.run()
        assert n == 1
        assert any("cursor corrupt" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_upsert_recency_guard_preserves_newer_row(self, db_path):
        # D-12: UPSERT WHERE dialogs.snapshot_at < excluded.snapshot_at —
        # bootstrap data must NOT clobber a row with a fresher snapshot_at.
        seed_conn = _open_sync_db(db_path)
        try:
            future_ts = int(time.time()) + 10_000
            seed_conn.execute(
                "INSERT INTO dialogs (dialog_id, name, type, snapshot_at) VALUES (?, ?, ?, ?)",
                (555, "FromEvent", "user", future_ts),
            )
            seed_conn.commit()
        finally:
            seed_conn.close()

        worker, _, _ = _make_worker(
            db_path,
            [_make_dialog(555, _make_user_entity(555, first_name="FromBootstrap"))],
        )
        await worker.run()

        conn = _open_sync_db(db_path)
        try:
            row = conn.execute("SELECT name FROM dialogs WHERE dialog_id = ?", (555,)).fetchone()
            assert row[0] == "FromEvent"  # not overwritten
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_floodwait_sleep_is_interruptible(self, db_path):
        # D-13: shutdown_event wakes the worker before the full FloodWait elapses.
        from telethon.errors import FloodWaitError

        async def slow_flooding_gen(items):
            for it in items:
                yield it
            raise FloodWaitError(request=None, capture=120)  # type: ignore[call-arg]

        client = MagicMock()
        client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: slow_flooding_gen([_make_dialog(1, _make_user_entity(1))])
        )
        shutdown_event = asyncio.Event()
        worker = DialogsBootstrapWorker(client, db_path, shutdown_event)

        async def trip_shutdown():
            await asyncio.sleep(0.05)
            shutdown_event.set()

        start = time.monotonic()
        await asyncio.gather(worker.run(), trip_shutdown())
        elapsed = time.monotonic() - start
        # If sleep were uninterruptible the FloodWaitError of 120s would block.
        assert elapsed < 5.0

    @pytest.mark.asyncio
    async def test_rpcerror_aborts_without_complete_and_surfaces_via_startup_detail(self, db_path):
        # Review MEDIUM: RPCError must surface via startup_detail_setter so the
        # operator sees the stall via /health rather than scanning logs.
        from telethon.errors import RPCError

        async def err_gen(items):
            for it in items:
                yield it
            raise RPCError(request=None, message="TEST_RPC_ERROR", code=400)  # type: ignore[call-arg]

        client = MagicMock()
        client.iter_dialogs = MagicMock(side_effect=lambda **kw: err_gen([_make_dialog(7, _make_user_entity(7))]))
        shutdown_event = asyncio.Event()
        captured_detail: list[str] = []
        worker = DialogsBootstrapWorker(
            client,
            db_path,
            shutdown_event,
            startup_detail_setter=captured_detail.append,
        )
        n = await worker.run()
        assert n == 1
        # status NOT 'complete'.
        conn = _open_sync_db(db_path)
        try:
            assert _get_state(conn, "bootstrap_sweep_status") != "complete"
        finally:
            conn.close()
        # startup_detail received the stall message.
        assert any("stalled" in s and "RPCError" in s for s in captured_detail)

    @pytest.mark.asyncio
    async def test_hidden_column_preserved(self, db_path):
        # D-11: hidden is NEVER touched in the UPDATE clause —
        # a Phase 42 handler that sets hidden=1 must survive any bootstrap pass.
        seed_conn = _open_sync_db(db_path)
        try:
            old_ts = 10  # very old timestamp ensures bootstrap is "newer"
            seed_conn.execute(
                "INSERT INTO dialogs (dialog_id, name, type, snapshot_at, hidden) VALUES (?, ?, ?, ?, 1)",
                (888, "Old", "user", old_ts),
            )
            seed_conn.commit()
        finally:
            seed_conn.close()

        worker, _, _ = _make_worker(
            db_path,
            [_make_dialog(888, _make_user_entity(888, first_name="New"))],
        )
        await worker.run()

        conn = _open_sync_db(db_path)
        try:
            row = conn.execute("SELECT hidden, name FROM dialogs WHERE dialog_id = ?", (888,)).fetchone()
            assert row[0] == 1
            assert row[1] == "New"
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_worker_opens_its_own_dedicated_connection(self, db_path):
        # Review HIGH: worker must NOT receive a pre-opened conn — it opens
        # its own via _open_sync_db(db_path). Verifies isolation contract.
        client = MagicMock()
        client.iter_dialogs = MagicMock(side_effect=lambda **kw: _async_gen([_make_dialog(1, _make_user_entity(1))]))
        shutdown_event = asyncio.Event()
        worker = DialogsBootstrapWorker(client, db_path, shutdown_event)
        # Worker has its own connection attribute pointing to a real sqlite3.Connection.
        assert isinstance(worker._conn, sqlite3.Connection)
        await worker.run()
        # After run() returns, the worker's connection is closed (finally block).
        with pytest.raises(sqlite3.ProgrammingError):
            worker._conn.execute("SELECT 1")

    @pytest.mark.asyncio
    async def test_startup_detail_setter_is_optional(self, db_path):
        worker, _, _ = _make_worker(db_path, [_make_dialog(1, _make_user_entity(1))])
        await worker.run()  # must not raise

    @pytest.mark.asyncio
    async def test_startup_detail_setter_invoked_on_complete(self, db_path):
        client = MagicMock()
        client.iter_dialogs = MagicMock(side_effect=lambda **kw: _async_gen([_make_dialog(1, _make_user_entity(1))]))
        shutdown_event = asyncio.Event()
        captured: list[str] = []
        worker = DialogsBootstrapWorker(
            client,
            db_path,
            shutdown_event,
            startup_detail_setter=captured.append,
        )
        await worker.run()
        assert any("complete" in s for s in captured)


# ---------------------------------------------------------------------------
# TestEncodeDecodeOffsetPeer
# ---------------------------------------------------------------------------


class TestEncodeDecodeOffsetPeer:
    def test_user_round_trip(self):
        from telethon.tl.types import InputPeerUser

        e = _make_user_entity(42, access_hash=777)
        encoded = _encode_offset_peer(e)
        assert encoded is not None
        decoded = _decode_offset_peer(encoded)
        assert isinstance(decoded, InputPeerUser)
        assert decoded.user_id == 42
        assert decoded.access_hash == 777

    def test_chat_round_trip(self):
        from telethon.tl.types import InputPeerChat

        e = _make_chat_entity(2002)
        encoded = _encode_offset_peer(e)
        assert encoded is not None
        decoded = _decode_offset_peer(encoded)
        assert isinstance(decoded, InputPeerChat)
        assert decoded.chat_id == 2002

    def test_channel_round_trip(self):
        from telethon.tl.types import InputPeerChannel

        e = _make_channel_entity(33333, access_hash=888)
        encoded = _encode_offset_peer(e)
        assert encoded is not None
        decoded = _decode_offset_peer(encoded)
        assert isinstance(decoded, InputPeerChannel)
        assert decoded.channel_id == 33333
        assert decoded.access_hash == 888

    def test_user_with_none_access_hash_encodes_zero(self):
        e = _make_user_entity(99, access_hash=0)
        e.access_hash = None
        encoded = _encode_offset_peer(e)
        assert encoded is not None
        d = json.loads(encoded)
        assert d["access_hash"] == 0

    def test_unknown_entity_returns_none_and_warns(self, caplog):
        # Review LOW: unknown entity types must NOT fabricate a fake
        # channel-id-0 cursor — return None and log a WARNING instead.
        unknown = SimpleNamespace(id=42)
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.dialog_sync"):
            encoded = _encode_offset_peer(unknown)
        assert encoded is None
        assert any("unknown entity type" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# TestClearCursor
# ---------------------------------------------------------------------------


class TestClearCursor:
    def test_clear_cursor_removes_all_three_keys(self, tmp_path: Path):
        p = tmp_path / "sync.db"
        ensure_sync_schema(p)
        conn = _open_sync_db(p)
        try:
            with conn:
                _set_state(conn, "bootstrap_sweep_offset_date", "2024-06-01T12:00:00+00:00")
                _set_state(conn, "bootstrap_sweep_offset_id", "42")
                _set_state(conn, "bootstrap_sweep_offset_peer", "{}")
            with conn:
                _clear_cursor(conn)
            for k in (
                "bootstrap_sweep_offset_date",
                "bootstrap_sweep_offset_id",
                "bootstrap_sweep_offset_peer",
            ):
                assert _get_state(conn, k) is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# _set_access_lost — RECON-04
# ---------------------------------------------------------------------------


def test_set_access_lost_atomic(db_path: Path) -> None:
    """RECON-04: _set_access_lost writes synced_dialogs and dialogs in one txn."""
    from mcp_telegram.dialog_sync import _set_access_lost

    dialog_id = 12345
    now = 1700000000

    conn = _open_sync_db(db_path)
    try:
        # Seed both tables so we can observe the UPDATEs land.
        with conn:
            conn.execute(
                "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'syncing')",
                (dialog_id,),
            )
            conn.execute(
                "INSERT INTO dialogs (dialog_id, name, type, archived, pinned, "
                "snapshot_at, hidden, needs_refresh) "
                "VALUES (?, 'Test', 'user', 0, 0, ?, 0, 0)",
                (dialog_id, now - 1000),
            )

        _set_access_lost(conn, dialog_id, now)

        row = conn.execute(
            "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone()
        assert row[0] == "access_lost"
        assert row[1] == now

        row = conn.execute(
            "SELECT hidden, snapshot_at FROM dialogs WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == now
    finally:
        conn.close()


def test_set_access_lost_no_op_on_missing_rows(db_path: Path) -> None:
    """Helper does not raise when one or both rows are missing."""
    from mcp_telegram.dialog_sync import _set_access_lost

    conn = _open_sync_db(db_path)
    try:
        # Neither table has dialog_id=99999 — must not raise.
        _set_access_lost(conn, 99999, 1700000000)
    finally:
        conn.close()
