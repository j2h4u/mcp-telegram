"""Tests for DeltaSyncWorker — TDD RED phase.

Covers DAEMON-12 (forward gap-fill on reconnect) behaviors:
- Fills gap messages newer than max known message_id per dialog
- No-op when dialog is up-to-date (no gap)
- Skips dialogs with no baseline (max_known_id=0)
- Uses min_id + reverse=True for forward fetch
- Handles FloodWait interruptibly
- Classifies access-loss errors same as FullSyncWorker
- Iterates all 'synced' dialogs; skips 'syncing'
- Respects shutdown_event
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from helpers import build_mock_message

from mcp_telegram.delta_sync import DeltaSyncWorker
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_db(tmp_path: Any) -> sqlite3.Connection:
    """Create a real sync.db in tmp_path and return an open connection."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def mock_client() -> MagicMock:
    """Return a mock TelegramClient with async iter_messages support."""
    client = MagicMock()
    client.is_connected.return_value = True
    return client


@pytest.fixture()
def shutdown_event() -> asyncio.Event:
    """Return an unset asyncio.Event (worker should process normally)."""
    return asyncio.Event()


def make_worker(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> DeltaSyncWorker:
    return DeltaSyncWorker(mock_client, sync_db, shutdown_event)


# ---------------------------------------------------------------------------
# DAEMON-12: Forward gap-fill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_fills_gap(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with max message_id=100 in DB; iter_messages returns 3 newer messages — all 3 stored."""
    dialog_id = 1001

    # Set up synced dialog with max known message_id=100
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 100, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    new_msgs = [
        build_mock_message(id=101, text="msg 101"),
        build_mock_message(id=102, text="msg 102"),
        build_mock_message(id=103, text="msg 103"),
    ]

    async def _iter_messages(**kwargs: Any):
        for m in new_msgs:
            yield m

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 3

    rows = sync_db.execute(
        "SELECT message_id FROM messages WHERE dialog_id=? ORDER BY message_id",
        (dialog_id,),
    ).fetchall()
    ids = [r[0] for r in rows]
    assert ids == [100, 101, 102, 103]


@pytest.mark.asyncio
async def test_delta_no_gap_returns_zero(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog where Telegram returns no messages newer than max_known_id — returns 0, no DB changes."""
    dialog_id = 1002

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 50, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    async def _iter_messages(**kwargs: Any):
        return
        yield  # empty async generator

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 0

    count = sync_db.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=?", (dialog_id,)).fetchone()[0]
    assert count == 1  # only the original message


@pytest.mark.asyncio
async def test_delta_no_baseline_skips(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with 0 messages in DB (max_known_id=0) — skip, returns 0."""
    dialog_id = 1003

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    sync_db.commit()

    # iter_messages should NOT be called for a dialog with no baseline
    calls: list[Any] = []

    async def _iter_messages(**kwargs: Any):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 0
    assert len(calls) == 0, "iter_messages must not be called for dialog with no baseline"


@pytest.mark.asyncio
async def test_delta_uses_min_id_and_reverse(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Verify iter_messages called with min_id=max_known_id and reverse=True."""
    dialog_id = 1004

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 200, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    captured_kwargs: dict[str, Any] = {}

    async def _iter_messages(**kwargs: Any):
        captured_kwargs.update(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert captured_kwargs.get("min_id") == 200, f"Expected min_id=200, got {captured_kwargs.get('min_id')}"
    assert captured_kwargs.get("reverse") is True, f"Expected reverse=True, got {captured_kwargs.get('reverse')}"


@pytest.mark.asyncio
async def test_delta_floodwait_handled(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """FloodWaitError during iter_messages triggers interruptible wait, returns 0 for that dialog."""
    from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

    dialog_id = 1005

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    err = FloodWaitError(request=None)
    err.seconds = 3

    async def _iter_messages(**kwargs: Any):
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    slept_for: list[float] = []

    async def _mock_wait_for(coro: Any, timeout: float) -> None:
        slept_for.append(timeout)
        raise TimeoutError

    worker = make_worker(mock_client, sync_db, shutdown_event)

    with patch("mcp_telegram.delta_sync.asyncio.wait_for", side_effect=_mock_wait_for):
        total = await worker.run_delta_catch_up()

    assert total == 0
    assert slept_for, "asyncio.wait_for should have been called for FloodWait sleep"
    assert slept_for[0] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_delta_access_lost_handled(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Access-loss RPCError during delta sets status='access_lost' + access_lost_at, returns 0."""
    from telethon.errors import ChannelPrivateError  # type: ignore[import-untyped]

    dialog_id = 1006

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    err = ChannelPrivateError(request=None)

    async def _iter_messages(**kwargs: Any):
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 0

    row = sync_db.execute(
        "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "access_lost"
    assert row[1] is not None


@pytest.mark.asyncio
async def test_delta_iterates_all_synced_dialogs(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """With 3 'synced' dialogs with baselines, run_delta_catch_up fetches for all 3."""
    dialog_ids = [2001, 2002, 2003]

    for dialog_id in dialog_ids:
        sync_db.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
            (dialog_id,),
        )
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 50, 1704067200)",
            (dialog_id,),
        )
    sync_db.commit()

    called_for: list[Any] = []

    async def _iter_messages(**kwargs: Any):
        called_for.append(kwargs.get("entity") or kwargs.get(0))
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert len(called_for) == 3, f"Expected 3 fetch calls, got {len(called_for)}"


@pytest.mark.asyncio
async def test_delta_skips_syncing_dialogs(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='syncing' is NOT included in delta catch-up."""
    # 'synced' dialog
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (3001, 'synced')",
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (3001, 10, 1704067200)",
    )
    # 'syncing' dialog — should be skipped
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (3002, 'syncing')",
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (3002, 20, 1704067200)",
    )
    sync_db.commit()

    called_for: list[Any] = []

    async def _iter_messages(**kwargs: Any):
        # record first positional-like arg (entity/dialog_id)
        for v in kwargs.values():
            if isinstance(v, int):
                called_for.append(v)
                break
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    # Only 3001 should be fetched
    assert 3002 not in called_for, f"'syncing' dialog 3002 must not be fetched, got {called_for}"


@pytest.mark.asyncio
async def test_delta_respects_shutdown(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """If shutdown_event is set before run_delta_catch_up loops, it breaks early."""
    for dialog_id in [4001, 4002, 4003]:
        sync_db.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
            (dialog_id,),
        )
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
            (dialog_id,),
        )
    sync_db.commit()

    # Set shutdown before starting
    shutdown_event.set()

    called_count = 0

    async def _iter_messages(**kwargs: Any):
        nonlocal called_count
        called_count += 1
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    # With shutdown set, no dialogs should be processed
    assert called_count == 0, f"Expected 0 calls after shutdown, got {called_count}"


# ---------------------------------------------------------------------------
# Phase 29-02: FTS population in DeltaSyncWorker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_catch_up_populates_fts(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """After run_delta_catch_up(), messages_fts has rows for each gap-fill message."""
    dialog_id = 5001

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 100, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    new_msgs = [
        build_mock_message(id=101, text="написал сообщение"),
        build_mock_message(id=102, text="hello world"),
    ]

    async def _iter_messages(**kwargs: Any):
        for m in new_msgs:
            yield m

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 2

    fts_rows = sync_db.execute(
        "SELECT message_id, stemmed_text FROM messages_fts WHERE dialog_id = ? ORDER BY message_id",
        (dialog_id,),
    ).fetchall()
    assert len(fts_rows) == 2, f"Expected 2 FTS rows for gap messages, got {len(fts_rows)}"
    for row in fts_rows:
        assert row[1] != "", "stemmed_text must be non-empty for gap-filled messages with text"


# ---------------------------------------------------------------------------
# Probe-worker tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_restores_access_after_gap_fill(sync_db, mock_client, shutdown_event):
    """Probe does gap-fill FIRST, then resets status to syncing only on success."""
    from unittest.mock import AsyncMock

    from helpers import MockTotalList

    from mcp_telegram.delta_sync import DeltaSyncWorker, _probe_access_lost_dialogs

    dialog_id = 9001
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1000)",
        (dialog_id,),
    )
    # Need a baseline message for delta worker
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, 100, 1000, 'old')",
        (dialog_id,),
    )
    sync_db.commit()

    mock_client.get_messages = AsyncMock(return_value=MockTotalList([], total=200))

    # iter_messages for delta gap-fill returns empty
    async def _empty_iter(**kwargs):
        return
        yield

    mock_client.iter_messages = _empty_iter

    delta_worker = DeltaSyncWorker(mock_client, sync_db, shutdown_event)
    restored = await _probe_access_lost_dialogs(mock_client, sync_db, shutdown_event, delta_worker)

    assert restored == 1
    row = sync_db.execute(
        "SELECT status, access_lost_at, total_messages FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row[0] == "syncing"
    assert row[1] is None  # access_lost_at cleared
    assert row[2] == 200  # total_messages set from probe


@pytest.mark.asyncio
async def test_probe_gap_fill_failure_keeps_access_lost(sync_db, mock_client, shutdown_event):
    """If gap-fill fails after successful probe, status stays access_lost."""
    from unittest.mock import AsyncMock

    from helpers import MockTotalList

    from mcp_telegram.delta_sync import DeltaSyncWorker, _probe_access_lost_dialogs

    dialog_id = 9010
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1000)",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, 100, 1000, 'old')",
        (dialog_id,),
    )
    sync_db.commit()

    # Probe succeeds (get_messages returns OK)
    mock_client.get_messages = AsyncMock(return_value=MockTotalList([], total=200))

    # But gap-fill fails with a network error (not caught by fetch_delta_for_dialog)
    async def _failing_iter(**kwargs):
        raise OSError("connection reset during gap-fill")
        yield  # pragma: no cover

    mock_client.iter_messages = _failing_iter

    delta_worker = DeltaSyncWorker(mock_client, sync_db, shutdown_event)
    restored = await _probe_access_lost_dialogs(mock_client, sync_db, shutdown_event, delta_worker)

    assert restored == 0  # not restored because gap-fill failed
    row = sync_db.execute(
        "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row[0] == "access_lost"  # status unchanged
    assert row[1] == 1000  # access_lost_at unchanged


@pytest.mark.asyncio
async def test_probe_still_lost_unchanged(sync_db, mock_client, shutdown_event):
    """Probe leaves status unchanged when access is still lost."""
    from unittest.mock import AsyncMock

    from telethon.errors import ChannelPrivateError

    from mcp_telegram.delta_sync import DeltaSyncWorker, _probe_access_lost_dialogs

    dialog_id = 9002
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1000)",
        (dialog_id,),
    )
    sync_db.commit()

    mock_client.get_messages = AsyncMock(side_effect=ChannelPrivateError(request=None))

    delta_worker = DeltaSyncWorker(mock_client, sync_db, shutdown_event)
    restored = await _probe_access_lost_dialogs(mock_client, sync_db, shutdown_event, delta_worker)

    assert restored == 0
    row = sync_db.execute(
        "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row[0] == "access_lost"
    assert row[1] == 1000  # unchanged


@pytest.mark.asyncio
async def test_probe_loop_runs_immediately_then_shutdown(shutdown_event):
    """Probe loop runs immediately (initial_delay=0) then exits on shutdown."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from mcp_telegram.delta_sync import DeltaSyncWorker, run_access_probe_loop

    client = MagicMock()
    conn = MagicMock()
    conn.execute = MagicMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))
    delta_worker = MagicMock(spec=DeltaSyncWorker)

    # Set shutdown after one iteration
    async def _set_shutdown_after_probe(*args, **kwargs):
        shutdown_event.set()

    with patch(
        "mcp_telegram.delta_sync._probe_access_lost_dialogs",
        new=AsyncMock(side_effect=_set_shutdown_after_probe),
    ) as mock_probe:
        await run_access_probe_loop(
            client,
            conn,
            shutdown_event,
            delta_worker,
            initial_delay=0.0,
            interval=86400.0,
        )
        # Probe was called exactly once (immediate run, then shutdown)
        mock_probe.assert_called_once()


@pytest.mark.asyncio
async def test_probe_loop_shutdown_during_initial_delay(shutdown_event):
    """Probe loop exits cleanly when shutdown fires during non-zero initial delay."""
    from unittest.mock import MagicMock

    from mcp_telegram.delta_sync import DeltaSyncWorker, run_access_probe_loop

    client = MagicMock()
    conn = MagicMock()
    delta_worker = MagicMock(spec=DeltaSyncWorker)

    shutdown_event.set()  # immediate shutdown

    await run_access_probe_loop(
        client,
        conn,
        shutdown_event,
        delta_worker,
        initial_delay=10.0,
        interval=86400.0,
    )
    # Should return without error — no probes performed
    client.get_messages.assert_not_called()


# ---------------------------------------------------------------------------
# Backfill total_messages tests (via daemon._backfill_total_messages)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_total_messages_fills_null_rows(sync_db, mock_client, shutdown_event):
    """_backfill_total_messages populates total_messages for NULL rows."""
    import importlib
    from unittest.mock import AsyncMock

    from helpers import MockTotalList

    daemon_mod = importlib.import_module("mcp_telegram.daemon")
    _backfill = daemon_mod._backfill_total_messages

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages) VALUES (?, 'synced', NULL)",
        (8001,),
    )
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages) "
        "VALUES (?, 'syncing', 500)",  # already has total — should be skipped
        (8002,),
    )
    sync_db.commit()

    mock_client.get_messages = AsyncMock(return_value=MockTotalList([], total=999))

    filled = await _backfill(mock_client, sync_db, shutdown_event)

    assert filled == 1
    row = sync_db.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id = ?", (8001,)).fetchone()
    assert row[0] == 999
    # 8002 unchanged
    row2 = sync_db.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id = ?", (8002,)).fetchone()
    assert row2[0] == 500


@pytest.mark.asyncio
async def test_backfill_skips_on_error(sync_db, mock_client, shutdown_event):
    """_backfill_total_messages skips dialogs that raise exceptions."""
    import importlib
    from unittest.mock import AsyncMock

    daemon_mod = importlib.import_module("mcp_telegram.daemon")
    _backfill = daemon_mod._backfill_total_messages

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages) VALUES (?, 'synced', NULL)",
        (8003,),
    )
    sync_db.commit()

    mock_client.get_messages = AsyncMock(side_effect=Exception("network error"))

    filled = await _backfill(mock_client, sync_db, shutdown_event)

    assert filled == 0  # skipped, not crashed
    row = sync_db.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id = ?", (8003,)).fetchone()
    assert row[0] is None  # still NULL


@pytest.mark.asyncio
async def test_backfill_respects_shutdown(sync_db, mock_client, shutdown_event):
    """_backfill_total_messages exits early when shutdown_event is set."""
    import importlib

    daemon_mod = importlib.import_module("mcp_telegram.daemon")
    _backfill = daemon_mod._backfill_total_messages

    for i in range(5):
        sync_db.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, total_messages) VALUES (?, 'synced', NULL)",
            (8010 + i,),
        )
    sync_db.commit()

    shutdown_event.set()  # immediate shutdown

    filled = await _backfill(mock_client, sync_db, shutdown_event)
    assert filled == 0  # exited before processing any


# ---------------------------------------------------------------------------
# D-01: Checkpoint skip + last_synced_at stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_skip_recent_dialog(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with last_synced_at within threshold is skipped — no iter_messages call."""
    import time as _time

    dialog_id = 6001
    now = int(_time.time())

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', ?)",
        (dialog_id, now - 60),  # 60s ago — well within 300s threshold
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    calls: list[Any] = []

    async def _iter_messages(**kwargs: Any):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 0
    assert len(calls) == 0, "iter_messages must NOT be called for recently-synced dialog"


@pytest.mark.asyncio
async def test_checkpoint_skip_null_last_synced_at(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with last_synced_at=NULL is NOT skipped — must be probed."""
    dialog_id = 6002

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', NULL)",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    calls: list[Any] = []

    async def _iter_messages(**kwargs: Any):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert len(calls) == 1, "iter_messages MUST be called for dialog with NULL last_synced_at"


@pytest.mark.asyncio
async def test_checkpoint_skip_stale_last_synced_at(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with last_synced_at older than threshold is NOT skipped — must be probed."""
    import time as _time

    dialog_id = 6003
    now = int(_time.time())

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', ?)",
        (dialog_id, now - 7200),  # 2 hours ago — well beyond skip threshold
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    calls: list[Any] = []

    async def _iter_messages(**kwargs: Any):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert len(calls) == 1, "iter_messages MUST be called for stale dialog (last_synced_at > threshold)"


@pytest.mark.asyncio
async def test_checkpoint_skip_access_lost_not_selected(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """status='access_lost' dialog is never selected by delta catch-up (existing behavior)."""
    import time as _time

    dialog_id = 6004
    now = int(_time.time())

    # access_lost with very old last_synced_at — even if selected, should not be
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'access_lost', ?)",
        (dialog_id, now - 9999),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    calls: list[Any] = []

    async def _iter_messages(**kwargs: Any):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert len(calls) == 0, "access_lost dialog must never be selected for delta catch-up"


@pytest.mark.asyncio
async def test_fetch_delta_stamps_last_synced_at_on_success(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """fetch_delta_for_dialog stamps last_synced_at on success (both no-gap and gap paths)."""
    import time as _time

    dialog_id = 6005

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', NULL)",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    # Empty iterator — no-gap path
    async def _iter_messages(**kwargs: Any):
        return
        yield

    mock_client.iter_messages = _iter_messages

    before = int(_time.time())
    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.fetch_delta_for_dialog(dialog_id)
    after = int(_time.time())

    row = sync_db.execute(
        "SELECT last_synced_at FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is not None, "last_synced_at must be set on success"
    assert before <= row[0] <= after + 2, f"last_synced_at={row[0]} not in [{before}, {after+2}]"


@pytest.mark.asyncio
async def test_fetch_delta_stamps_last_synced_at_on_gap_filled(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """fetch_delta_for_dialog stamps last_synced_at when messages are actually fetched."""
    import time as _time

    dialog_id = 6006

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', NULL)",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    # Returns one message — gap-filled path
    async def _iter_messages(**kwargs: Any):
        yield build_mock_message(id=11, text="new msg")

    mock_client.iter_messages = _iter_messages

    before = int(_time.time())
    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.fetch_delta_for_dialog(dialog_id)
    after = int(_time.time())

    assert result == 1
    row = sync_db.execute(
        "SELECT last_synced_at FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is not None, "last_synced_at must be set after gap fill"
    assert before <= row[0] <= after + 2


@pytest.mark.asyncio
async def test_fetch_delta_stamps_on_floodwait(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """fetch_delta_for_dialog stamps last_synced_at=now on FloodWait so the
    checkpoint skip catches the dialog on the next cold restart instead of
    repeatedly hitting FloodWait on the same hot dialogs every boot."""
    import time as _time
    from telethon.errors import FloodWaitError as _FloodWaitError

    dialog_id = 6007
    original_ts = 1000

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', ?)",
        (dialog_id, original_ts),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    err = _FloodWaitError(request=None)
    err.seconds = 1

    async def _iter_messages(**kwargs: Any):
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)

    async def _fast_wait_for(coro: Any, timeout: float) -> None:
        raise TimeoutError

    before = int(_time.time())
    with patch("mcp_telegram.delta_sync.asyncio.wait_for", side_effect=_fast_wait_for):
        await worker.fetch_delta_for_dialog(dialog_id)
    after = int(_time.time())

    row = sync_db.execute(
        "SELECT last_synced_at FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row[0] is not None and row[0] >= before and row[0] <= after, (
        f"last_synced_at must be stamped to ~now on FloodWait; got {row[0]} "
        f"(window {before}..{after}), original was {original_ts}"
    )


@pytest.mark.asyncio
async def test_checkpoint_skip_emits_log(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    caplog: Any,
) -> None:
    """run_delta_catch_up emits delta_catch_up_skip log for skipped dialogs."""
    import time as _time

    dialog_id = 6008
    now = int(_time.time())

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', ?)",
        (dialog_id, now - 30),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    async def _iter_messages(**kwargs: Any):
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    import logging
    with caplog.at_level(logging.INFO, logger="mcp_telegram.delta_sync"):
        await worker.run_delta_catch_up()

    skip_logs = [r for r in caplog.records if "delta_catch_up_skip" in r.getMessage()]
    assert len(skip_logs) == 1, f"Expected 1 skip log, got {len(skip_logs)}: {[r.getMessage() for r in caplog.records]}"
    assert f"dialog_id={dialog_id}" in skip_logs[0].getMessage()
