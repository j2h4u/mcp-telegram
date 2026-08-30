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
# pyright: reportArgumentType=false

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import MagicMock, patch

import pytest

from helpers import build_mock_message
from mcp_telegram.delta_sync import (
    AccessProbePolicy,
    DeltaCatchUpPolicy,
    DeltaSyncWorker,
    _DeltaSyncClient,
    _log_probe_budget_exhausted,
    run_delta_catch_up_loop,
)
from mcp_telegram.history_enrollment import disable_history
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from tests.history_enrollment_helpers import seed_full_history_enrollment


class _SQLiteCursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...


class _SQLiteConnection(Protocol):
    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _SQLiteCursor: ...

    def executemany(self, sql: str, seq_of_parameters: list[tuple[object, ...]]) -> _SQLiteCursor: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...


class _Closable(Protocol):
    def close(self) -> None: ...


async def _empty_async_iter() -> AsyncIterator[object]:
    if False:
        yield None


class _MockClient:
    def __init__(self) -> None:
        self.is_connected = MagicMock(return_value=True)
        self.get_messages = MagicMock()
        self.iter_messages = _empty_async_iter


def _access_probe_policy(
    *,
    interval_seconds: float = 86_400.0,
    max_dialogs_per_cycle: int = 3,
    cooldown_seconds: int = 604_800,
    probe_pause_seconds: float = 0.0,
) -> AccessProbePolicy:
    return AccessProbePolicy(
        interval_seconds=interval_seconds,
        max_dialogs_per_cycle=max_dialogs_per_cycle,
        cooldown_seconds=cooldown_seconds,
        probe_pause_seconds=probe_pause_seconds,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_db(tmp_path: Path) -> Iterator[_SQLiteConnection]:
    """Create a real sync.db in tmp_path and return an open connection."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = cast(_SQLiteConnection, _open_sync_db(db_path))
    yield conn
    conn.close()


@pytest.fixture()
def mock_client() -> _MockClient:
    """Return a mock TelegramClient with async iter_messages support."""
    return _MockClient()


@pytest.fixture()
def shutdown_event() -> asyncio.Event:
    """Return an unset asyncio.Event (worker should process normally)."""
    return asyncio.Event()


def make_worker(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> DeltaSyncWorker:
    return DeltaSyncWorker(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
    )


@pytest.mark.asyncio
async def test_delta_disable_race_discards_fetched_body_and_checkpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "race.db"
    ensure_sync_schema(db_path)
    first = cast(sqlite3.Connection, _open_sync_db(db_path))
    second = cast(sqlite3.Connection, _open_sync_db(db_path))
    first.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (78, 'synced')")
    first.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (78, 1, 1)")
    first.execute("INSERT INTO full_history_enrollment VALUES (78, 1, 'explicit', 1)")
    first.commit()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(**_kwargs: object):
        entered.set()
        await release.wait()
        yield build_mock_message(id=2, text="stale")

    client = _MockClient()
    client.iter_messages = fetch
    worker = make_worker(client, first, asyncio.Event())
    task = asyncio.create_task(worker.fetch_delta_for_dialog(78))
    await entered.wait()
    disable_history(second, 78, now=2)
    second.commit()
    release.set()
    assert await task == 0
    assert first.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=78").fetchone() == (1,)
    assert first.execute("SELECT status FROM synced_dialogs WHERE dialog_id=78").fetchone() == ("synced",)
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_delta_stale_access_error_after_disable_does_not_mark_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "access-race.db"
    ensure_sync_schema(db_path)
    first = cast(sqlite3.Connection, _open_sync_db(db_path))
    second = cast(sqlite3.Connection, _open_sync_db(db_path))
    first.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (80, 'synced')")
    first.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (80, 1, 1)")
    first.execute("INSERT INTO full_history_enrollment VALUES (80, 1, 'explicit', 1)")
    first.commit()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail(**_kwargs: object):
        entered.set()
        await release.wait()
        raise RuntimeError("stale access error")
        yield

    monkeypatch.setattr("mcp_telegram.delta_sync.ACCESS_LOST_ERRORS", (RuntimeError,))
    client = _MockClient()
    client.iter_messages = fail
    task = asyncio.create_task(make_worker(client, first, asyncio.Event()).fetch_delta_for_dialog(80))
    await entered.wait()
    disable_history(second, 80, now=2)
    second.commit()
    release.set()
    assert await task == 0
    assert first.execute("SELECT status FROM synced_dialogs WHERE dialog_id=80").fetchone() == ("synced",)
    first.close()
    second.close()


# ---------------------------------------------------------------------------
# DAEMON-12: Forward gap-fill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delta_fills_gap(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with max message_id=100 in DB; iter_messages returns 3 newer messages — all 3 stored."""
    dialog_id = 1001

    # Set up synced dialog with max known message_id=100
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
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

    async def _iter_messages(**kwargs: object):
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
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog where Telegram returns no messages newer than max_known_id — returns 0, no DB changes."""
    dialog_id = 1002

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 50, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    async def _iter_messages(**kwargs: object):
        return
        yield  # empty async generator

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 0

    row = cast(
        tuple[int] | None, sync_db.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=?", (dialog_id,)).fetchone()
    )
    assert row is not None
    count = row[0]
    assert count == 1  # only the original message


@pytest.mark.asyncio
async def test_delta_no_baseline_skips(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with 0 messages in DB (max_known_id=0) — skip, returns 0."""
    dialog_id = 1003

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.commit()

    # iter_messages should NOT be called for a dialog with no baseline
    calls: list[object] = []

    async def _iter_messages(**kwargs: object):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 0
    assert len(calls) == 0, "iter_messages must not be called for dialog with no baseline"


@pytest.mark.asyncio
async def test_delta_no_baseline_clears_refresh_without_faking_history_sync(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """A malformed synced row with no local baseline must not keep a permanent refresh request."""
    dialog_id = 10031

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at, delta_refresh_requested_at) "
        "VALUES (?, 'synced', NULL, 1000)",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.commit()

    calls: list[object] = []

    async def _iter_messages(**kwargs: object):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.fetch_delta_for_dialog(dialog_id)

    row = cast(
        tuple[int | None, int | None, int | None] | None,
        sync_db.execute(
            "SELECT last_synced_at, last_delta_checked_at, delta_refresh_requested_at "
            "FROM synced_dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    assert calls == []
    assert row is not None
    assert row[0] is None
    assert isinstance(row[1], int)
    assert row[2] is None


@pytest.mark.asyncio
async def test_delta_uses_min_id_and_reverse(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Verify iter_messages called with min_id=max_known_id and reverse=True."""
    dialog_id = 1004

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 200, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    captured_kwargs: dict[str, object] = {}

    async def _iter_messages(**kwargs: object):
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
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """FloodWaitError during iter_messages triggers interruptible wait, returns 0 for that dialog."""
    from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

    dialog_id = 1005

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    err = FloodWaitError(request=None)
    err.seconds = 3

    async def _iter_messages(**kwargs: object):
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    slept_for: list[float] = []

    async def _mock_wait_for(coro: _Closable, timeout: float) -> None:
        slept_for.append(timeout)
        coro.close()
        raise TimeoutError

    worker = make_worker(mock_client, sync_db, shutdown_event)

    with patch("mcp_telegram.delta_sync.asyncio.wait_for", side_effect=_mock_wait_for):
        total = await worker.run_delta_catch_up()

    assert total == 0
    assert slept_for, "asyncio.wait_for should have been called for FloodWait sleep"
    assert slept_for[0] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_delta_access_lost_handled(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Access-loss RPCError during delta sets status='access_lost' + access_lost_at, returns 0."""
    from telethon.errors import ChannelPrivateError  # type: ignore[import-untyped]

    dialog_id = 1006

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    err = ChannelPrivateError(request=None)

    async def _iter_messages(**kwargs: object):
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
    event = sync_db.execute(
        "SELECT kind, dialog_id FROM daemon_events WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert event == ("access_lost", dialog_id)


@pytest.mark.asyncio
async def test_delta_iterates_all_synced_dialogs(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """With 3 'synced' dialogs with baselines, run_delta_catch_up fetches for all 3."""
    dialog_ids = [2001, 2002, 2003]

    for dialog_id in dialog_ids:
        sync_db.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
            (dialog_id,),
        )
        seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 50, 1704067200)",
            (dialog_id,),
        )
    sync_db.commit()

    called_for: list[object] = []

    async def _iter_messages(**kwargs: object):
        called_for.append(kwargs["entity"])
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert len(called_for) == 3, f"Expected 3 fetch calls, got {len(called_for)}"


@pytest.mark.asyncio
async def test_delta_skips_syncing_dialogs(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='syncing' is NOT included in delta catch-up."""
    # 'synced' dialog
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (3001, 'synced')",
    )
    seed_full_history_enrollment(sync_db, 3001, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (3001, 10, 1704067200)",
    )
    # 'syncing' dialog — should be skipped
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (3002, 'syncing')",
    )
    seed_full_history_enrollment(sync_db, 3002, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (3002, 20, 1704067200)",
    )
    sync_db.commit()

    called_for: list[object] = []

    async def _iter_messages(**kwargs: object):
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
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """If shutdown_event is set before run_delta_catch_up loops, it breaks early."""
    for dialog_id in [4001, 4002, 4003]:
        sync_db.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
            (dialog_id,),
        )
        seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
            (dialog_id,),
        )
    sync_db.commit()

    # Set shutdown before starting
    shutdown_event.set()

    called_count = 0

    async def _iter_messages(**kwargs: object):
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
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """After run_delta_catch_up(), messages_fts has rows for each gap-fill message."""
    dialog_id = 5001

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 100, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    new_msgs = [
        build_mock_message(id=101, text="написал сообщение"),
        build_mock_message(id=102, text="hello world"),
    ]

    async def _iter_messages(**kwargs: object):
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
async def test_probe_restores_access_after_gap_fill(
    sync_db: _SQLiteConnection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
) -> None:
    """Probe does gap-fill FIRST, then resets status to syncing only on success."""
    from unittest.mock import AsyncMock

    from helpers import MockTotalList
    from mcp_telegram.delta_sync import DeltaSyncWorker, _probe_access_lost_dialogs

    dialog_id = 9001
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1000)",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "UPDATE full_history_enrollment SET enabled = 1, source = 'explicit', updated_at = 1 WHERE dialog_id = ?",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO dialogs (dialog_id, name, hidden, needs_refresh, snapshot_at) VALUES (?, 'lost', 1, 0, 1000)",
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
    async def _empty_iter(**kwargs: object):
        return
        yield

    mock_client.iter_messages = _empty_iter

    delta_worker = DeltaSyncWorker(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
    )
    restored = await _probe_access_lost_dialogs(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
        delta_worker,
        _access_probe_policy(),
    )

    assert restored == 1
    dialog_row = cast(
        tuple[int, int, int | None] | None,
        sync_db.execute(
            "SELECT hidden, needs_refresh, snapshot_at FROM dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    row = cast(
        tuple[object | None, ...] | None,
        sync_db.execute(
            "SELECT status, access_lost_at, total_messages FROM synced_dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    assert row is not None
    assert row[0] == "syncing"
    assert row[1] is None  # access_lost_at cleared
    assert row[2] == 200  # total_messages set from probe
    assert dialog_row is not None
    assert dialog_row[0] == 0  # visible again after access recovery
    assert dialog_row[1] == 1  # queued for reconciliation refresh
    assert dialog_row[2] != 1000
    event = sync_db.execute(
        "SELECT kind, dialog_id FROM daemon_events WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert event == ("access_restored", dialog_id)


@pytest.mark.asyncio
async def test_probe_gap_fill_failure_keeps_access_lost(
    sync_db: _SQLiteConnection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
) -> None:
    """If gap-fill fails after successful probe, status stays access_lost."""
    from unittest.mock import AsyncMock

    from helpers import MockTotalList
    from mcp_telegram.delta_sync import DeltaSyncWorker, _probe_access_lost_dialogs

    dialog_id = 9010
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1000)",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "UPDATE full_history_enrollment SET enabled = 1, source = 'explicit', updated_at = 1 WHERE dialog_id = ?",
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
    async def _failing_iter(**kwargs: object):
        raise OSError("connection reset during gap-fill")
        yield  # pragma: no cover

    mock_client.iter_messages = _failing_iter

    delta_worker = DeltaSyncWorker(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
    )
    restored = await _probe_access_lost_dialogs(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
        delta_worker,
        _access_probe_policy(),
    )

    assert restored == 0  # not restored because gap-fill failed
    row = cast(
        tuple[object | None, ...] | None,
        sync_db.execute(
            "SELECT status, access_lost_at, access_last_revalidated_at, access_next_revalidate_at "
            "FROM synced_dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    assert row is not None
    assert row[0] == "access_lost"  # status unchanged
    assert row[1] == 1000  # access_lost_at unchanged
    assert isinstance(row[2], int)
    assert isinstance(row[3], int)
    assert row[3] >= row[2]


@pytest.mark.asyncio
async def test_probe_still_lost_stamps_next_revalidation(
    sync_db: _SQLiteConnection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
) -> None:
    """Probe leaves status unchanged and schedules a cold retry."""
    from unittest.mock import AsyncMock

    from telethon.errors import ChannelPrivateError

    from mcp_telegram.delta_sync import DeltaSyncWorker, _probe_access_lost_dialogs

    dialog_id = 9002
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1000)",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=False)
    sync_db.commit()

    mock_client.get_messages = AsyncMock(side_effect=ChannelPrivateError(request=None))

    delta_worker = DeltaSyncWorker(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
    )
    restored = await _probe_access_lost_dialogs(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
        delta_worker,
        _access_probe_policy(),
    )

    assert restored == 0
    row = sync_db.execute(
        "SELECT status, access_lost_at, access_last_revalidated_at, access_next_revalidate_at "
        "FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "access_lost"
    assert row[1] == 1000  # unchanged
    assert isinstance(row[2], int)
    assert isinstance(row[3], int)
    assert row[3] == row[2] + 604_800


@pytest.mark.asyncio
async def test_probe_restores_access_creates_missing_dialog_row(
    sync_db: _SQLiteConnection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
) -> None:
    """Restored access must create a visible dialogs row when none existed before."""
    from unittest.mock import AsyncMock

    from helpers import MockTotalList
    from mcp_telegram.delta_sync import DeltaSyncWorker, _probe_access_lost_dialogs

    dialog_id = 9011
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1000)",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "UPDATE full_history_enrollment SET enabled = 1, source = 'explicit', updated_at = 1 WHERE dialog_id = ?",
        (dialog_id,),
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, 100, 1000, 'old')",
        (dialog_id,),
    )
    sync_db.commit()
    mock_client.get_messages = AsyncMock(return_value=MockTotalList([], total=200))

    async def _empty_iter(**kwargs: object):
        return
        yield

    mock_client.iter_messages = _empty_iter

    delta_worker = DeltaSyncWorker(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
    )
    restored = await _probe_access_lost_dialogs(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
        delta_worker,
        _access_probe_policy(),
    )

    row = cast(
        tuple[int, int, int | None] | None,
        sync_db.execute(
            "SELECT hidden, needs_refresh, snapshot_at FROM dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    assert restored == 1
    assert row is not None
    assert row[0] == 0
    assert row[1] == 1
    assert row[2] is not None


@pytest.mark.asyncio
async def test_probe_loop_runs_immediately_then_shutdown(shutdown_event: asyncio.Event) -> None:
    """Probe loop runs immediately (initial_delay=0) then exits on shutdown."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from mcp_telegram.delta_sync import DeltaSyncWorker, run_access_probe_loop

    class _ProbeClient:
        def __init__(self) -> None:
            self.get_messages: AsyncMock = AsyncMock()
            self.iter_messages = _empty_async_iter

    client = _ProbeClient()
    conn = MagicMock()
    conn.execute = MagicMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))
    delta_worker = MagicMock(spec=DeltaSyncWorker)

    # Set shutdown after one iteration
    async def _set_shutdown_after_probe(*args: object, **kwargs: object):
        shutdown_event.set()

    with patch(
        "mcp_telegram.delta_sync._probe_access_lost_dialogs",
        new=AsyncMock(side_effect=_set_shutdown_after_probe),
    ) as mock_probe:
        await run_access_probe_loop(
            cast(_DeltaSyncClient, client),
            cast(sqlite3.Connection, conn),
            shutdown_event,
            delta_worker,
            _access_probe_policy(),
            initial_delay=0.0,
        )
        # Probe was called exactly once (immediate run, then shutdown)
        mock_probe.assert_called_once()


@pytest.mark.asyncio
async def test_probe_selects_only_due_access_lost_with_cycle_budget(
    sync_db: _SQLiteConnection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
) -> None:
    """Cold access revalidation must not probe every lost dialog in one pass."""
    from unittest.mock import AsyncMock

    from telethon.errors import ChannelPrivateError

    from mcp_telegram.delta_sync import DeltaSyncWorker, _probe_access_lost_dialogs

    due_first = 9101
    due_second = 9102
    not_due = 9103
    future = int(time.time()) + 86_400
    sync_db.executemany(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at, access_next_revalidate_at) "
        "VALUES (?, 'access_lost', ?, ?)",
        [
            (due_first, 1000, None),
            (due_second, 1001, None),
            (not_due, 1002, future),
        ],
    )
    for dialog_id in (due_first, due_second, not_due):
        seed_full_history_enrollment(sync_db, dialog_id, enabled=False)
    sync_db.commit()
    mock_client.get_messages = AsyncMock(side_effect=ChannelPrivateError(request=None))
    delta_worker = DeltaSyncWorker(
        cast(_DeltaSyncClient, mock_client), cast(sqlite3.Connection, sync_db), shutdown_event
    )

    restored = await _probe_access_lost_dialogs(
        cast(_DeltaSyncClient, mock_client),
        cast(sqlite3.Connection, sync_db),
        shutdown_event,
        delta_worker,
        _access_probe_policy(max_dialogs_per_cycle=1),
    )

    assert restored == 0
    cast(AsyncMock, mock_client.get_messages).assert_called_once()
    assert cast(AsyncMock, mock_client.get_messages).call_args.kwargs["entity"] == due_first
    rows = dict(
        cast(
            list[tuple[int, int | None]],
            sync_db.execute(
                "SELECT dialog_id, access_last_revalidated_at FROM synced_dialogs ORDER BY dialog_id"
            ).fetchall(),
        )
    )
    assert rows[due_first] is not None
    assert rows[due_second] is None
    assert rows[not_due] is None


@pytest.mark.asyncio
async def test_probe_flood_wait_stops_account_pass(
    sync_db: _SQLiteConnection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
) -> None:
    """FloodWait is account-global: do not continue to the next lost dialog."""
    from unittest.mock import AsyncMock, patch

    from telethon.errors import FloodWaitError

    from mcp_telegram.delta_sync import DeltaSyncWorker, _probe_access_lost_dialogs

    sync_db.executemany(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', ?)",
        [(9201, 1000), (9202, 1001)],
    )
    seed_full_history_enrollment(sync_db, 9201, enabled=False)
    seed_full_history_enrollment(sync_db, 9202, enabled=False)
    sync_db.commit()
    err = FloodWaitError(request=None)
    err.seconds = 30
    mock_client.get_messages = AsyncMock(side_effect=err)
    delta_worker = DeltaSyncWorker(
        cast(_DeltaSyncClient, mock_client), cast(sqlite3.Connection, sync_db), shutdown_event
    )

    with patch("mcp_telegram.delta_sync.sleep_through_flood", new=AsyncMock(return_value=False)):
        restored = await _probe_access_lost_dialogs(
            cast(_DeltaSyncClient, mock_client),
            cast(sqlite3.Connection, sync_db),
            shutdown_event,
            delta_worker,
            _access_probe_policy(max_dialogs_per_cycle=2),
        )

    assert restored == 0
    cast(AsyncMock, mock_client.get_messages).assert_called_once()


@pytest.mark.asyncio
async def test_probe_loop_shutdown_during_initial_delay(shutdown_event: asyncio.Event) -> None:
    """Probe loop exits cleanly when shutdown fires during non-zero initial delay."""
    from unittest.mock import AsyncMock, MagicMock

    from mcp_telegram.delta_sync import DeltaSyncWorker, run_access_probe_loop

    class _ProbeClient:
        def __init__(self) -> None:
            self.get_messages: AsyncMock = AsyncMock()
            self.iter_messages = _empty_async_iter

    client = _ProbeClient()
    conn = MagicMock()
    delta_worker = MagicMock(spec=DeltaSyncWorker)

    shutdown_event.set()  # immediate shutdown

    await run_access_probe_loop(
        cast(_DeltaSyncClient, client),
        cast(sqlite3.Connection, conn),
        shutdown_event,
        delta_worker,
        _access_probe_policy(),
        initial_delay=10.0,
    )
    # Should return without error — no probes performed
    client.get_messages.assert_not_called()


# ---------------------------------------------------------------------------
# Backfill total_messages tests (via daemon._backfill_total_messages)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_total_messages_fills_null_rows(
    sync_db: _SQLiteConnection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
) -> None:
    """_backfill_total_messages populates total_messages for NULL rows."""
    import importlib
    from unittest.mock import AsyncMock

    from helpers import MockTotalList

    daemon_mod = importlib.import_module("mcp_telegram.daemon")
    _backfill = cast(
        Callable[[_MockClient, sqlite3.Connection, asyncio.Event], Awaitable[int]],
        daemon_mod._backfill_total_messages,
    )

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages) VALUES (?, 'synced', NULL)",
        (8001,),
    )
    seed_full_history_enrollment(sync_db, 8001, enabled=True)
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages) "
        "VALUES (?, 'syncing', 500)",  # already has total — should be skipped
        (8002,),
    )
    seed_full_history_enrollment(sync_db, 8002, enabled=True)
    sync_db.commit()

    mock_client.get_messages = AsyncMock(return_value=MockTotalList([], total=999))

    filled = await _backfill(mock_client, cast(sqlite3.Connection, sync_db), shutdown_event)

    assert filled == 1
    row = cast(
        tuple[int | None] | None,
        sync_db.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id = ?", (8001,)).fetchone(),
    )
    assert row is not None
    assert row[0] == 999
    # 8002 unchanged
    row2 = cast(
        tuple[int | None] | None,
        sync_db.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id = ?", (8002,)).fetchone(),
    )
    assert row2 is not None
    assert row2[0] == 500


@pytest.mark.asyncio
async def test_backfill_skips_on_error(
    sync_db: _SQLiteConnection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
) -> None:
    """_backfill_total_messages skips dialogs that raise exceptions."""
    import importlib
    from unittest.mock import AsyncMock

    daemon_mod = importlib.import_module("mcp_telegram.daemon")
    _backfill = cast(
        Callable[[_MockClient, sqlite3.Connection, asyncio.Event], Awaitable[int]],
        daemon_mod._backfill_total_messages,
    )

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages) VALUES (?, 'synced', NULL)",
        (8003,),
    )
    seed_full_history_enrollment(sync_db, 8003, enabled=True)
    sync_db.commit()

    mock_client.get_messages = AsyncMock(side_effect=Exception("network error"))

    filled = await _backfill(mock_client, cast(sqlite3.Connection, sync_db), shutdown_event)

    assert filled == 0  # skipped, not crashed
    row = cast(
        tuple[int | None] | None,
        sync_db.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id = ?", (8003,)).fetchone(),
    )
    assert row is not None
    assert row[0] is None  # still NULL


@pytest.mark.asyncio
async def test_backfill_respects_shutdown(
    sync_db: _SQLiteConnection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
) -> None:
    """_backfill_total_messages exits early when shutdown_event is set."""
    import importlib

    daemon_mod = importlib.import_module("mcp_telegram.daemon")
    _backfill = cast(
        Callable[[_MockClient, sqlite3.Connection, asyncio.Event], Awaitable[int]],
        daemon_mod._backfill_total_messages,
    )

    for i in range(5):
        sync_db.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, total_messages) VALUES (?, 'synced', NULL)",
            (8010 + i,),
        )
        seed_full_history_enrollment(sync_db, 8010 + i, enabled=True)
    sync_db.commit()

    shutdown_event.set()  # immediate shutdown

    filled = await _backfill(mock_client, cast(sqlite3.Connection, sync_db), shutdown_event)
    assert filled == 0  # exited before processing any


# ---------------------------------------------------------------------------
# D-01: Checkpoint skip + last_synced_at stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_skip_recent_dialog(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
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
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    calls: list[object] = []

    async def _iter_messages(**kwargs: object):
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
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with last_synced_at=NULL is NOT skipped — must be probed."""
    dialog_id = 6002

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', NULL)",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    calls: list[object] = []

    async def _iter_messages(**kwargs: object):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert len(calls) == 1, "iter_messages MUST be called for dialog with NULL last_synced_at"


@pytest.mark.asyncio
async def test_checkpoint_skip_stale_last_synced_at(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
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
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    calls: list[object] = []

    async def _iter_messages(**kwargs: object):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert len(calls) == 1, "iter_messages MUST be called for stale dialog (last_synced_at > threshold)"


@pytest.mark.asyncio
async def test_checkpoint_skip_access_lost_not_selected(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
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
    seed_full_history_enrollment(sync_db, dialog_id, enabled=False)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    calls: list[object] = []

    async def _iter_messages(**kwargs: object):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert len(calls) == 0, "access_lost dialog must never be selected for delta catch-up"


@pytest.mark.asyncio
async def test_fetch_delta_stamps_last_synced_at_on_success(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """fetch_delta_for_dialog stamps last_synced_at on success (both no-gap and gap paths)."""
    import time as _time

    dialog_id = 6005

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', NULL)",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    # Empty iterator — no-gap path
    async def _iter_messages(**kwargs: object):
        return
        yield

    mock_client.iter_messages = _iter_messages

    before = int(_time.time())
    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.fetch_delta_for_dialog(dialog_id)
    after = int(_time.time())

    row = cast(
        tuple[int | None, int | None] | None,
        sync_db.execute(
            "SELECT last_synced_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    assert row is not None
    assert row[0] is not None, "last_synced_at must be set on success"
    assert row[1] == row[0], "last_delta_checked_at must be stamped with the delta checkpoint"
    assert before <= row[0] <= after + 2, f"last_synced_at={row[0]} not in [{before}, {after + 2}]"


@pytest.mark.asyncio
async def test_fetch_delta_stamps_last_synced_at_on_gap_filled(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """fetch_delta_for_dialog stamps last_synced_at when messages are actually fetched."""
    import time as _time

    dialog_id = 6006

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', NULL)",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    # Returns one message — gap-filled path
    async def _iter_messages(**kwargs: object):
        yield build_mock_message(id=11, text="new msg")

    mock_client.iter_messages = _iter_messages

    before = int(_time.time())
    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.fetch_delta_for_dialog(dialog_id)
    after = int(_time.time())

    assert result == 1
    row = cast(
        tuple[int | None, int | None] | None,
        sync_db.execute(
            "SELECT last_synced_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    assert row is not None
    assert row[0] is not None, "last_synced_at must be set after gap fill"
    assert row[1] == row[0], "last_delta_checked_at must be stamped with the delta checkpoint"
    assert before <= row[0] <= after + 2


@pytest.mark.asyncio
async def test_fetch_delta_stamps_on_floodwait(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
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
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    err = _FloodWaitError(request=None)
    err.seconds = 1

    async def _iter_messages(**kwargs: object):
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)

    async def _fast_wait_for(coro: _Closable, timeout: float) -> None:
        coro.close()
        raise TimeoutError

    before = int(_time.time())
    with patch("mcp_telegram.delta_sync.asyncio.wait_for", side_effect=_fast_wait_for):
        await worker.fetch_delta_for_dialog(dialog_id)
    after = int(_time.time())

    row = cast(
        tuple[int | None, int | None] | None,
        sync_db.execute(
            "SELECT last_synced_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    assert row is not None
    assert row[0] is not None and row[0] >= before and row[0] <= after, (
        f"last_synced_at must be stamped to ~now on FloodWait; got {row[0]} "
        f"(window {before}..{after}), original was {original_ts}"
    )
    assert row[1] == row[0], "FloodWait checkpoint must also update last_delta_checked_at"


@pytest.mark.asyncio
async def test_checkpoint_skip_emits_log(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run_delta_catch_up emits delta_catch_up_skip log for skipped dialogs."""
    import time as _time

    dialog_id = 6008
    now = int(_time.time())

    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', ?)",
        (dialog_id, now - 30),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
        (dialog_id,),
    )
    sync_db.commit()

    async def _iter_messages(**kwargs: object):
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    import logging

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.delta_sync"):
        await worker.run_delta_catch_up()

    skip_logs = [r for r in caplog.records if "delta_catch_up_skip" in r.getMessage()]
    assert len(skip_logs) == 1, f"Expected 1 skip log, got {len(skip_logs)}: {[r.getMessage() for r in caplog.records]}"
    assert f"dialog_id={dialog_id}" in skip_logs[0].getMessage()


@pytest.mark.asyncio
async def test_delta_catch_up_respects_probe_budget(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_ids = [7001, 7002, 7003]
    for dialog_id in dialog_ids:
        sync_db.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', ?)",
            (dialog_id, 1000),
        )
        seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
            (dialog_id,),
        )
    sync_db.commit()

    calls: list[int] = []

    async def _iter_messages(**kwargs: object):
        calls.append(cast(int, kwargs["entity"]))
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up(
        policy=DeltaCatchUpPolicy(
            interval_seconds=300.0,
            max_probes_per_cycle=2,
            probe_pause_seconds=0.01,
        )
    )

    assert calls == dialog_ids[:2]


def test_delta_catch_up_probe_budget_exhausted_is_debug(caplog: pytest.LogCaptureFixture) -> None:
    """Expected bounded-cycle exhaustion remains observable without INFO noise."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.delta_sync"):
        _log_probe_budget_exhausted(
            DeltaCatchUpPolicy(interval_seconds=300.0, max_probes_per_cycle=2, probe_pause_seconds=0.0),
            total_rows=10,
            skipped=3,
            probed=2,
        )

    records = [record for record in caplog.records if "delta_catch_up_probe_budget_exhausted" in record.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert "remaining=5" in records[0].getMessage()


@pytest.mark.asyncio
async def test_delta_catch_up_orders_by_oldest_delta_check(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Bounded cycles probe the stalest delta-check rows first instead of stable table order."""
    rows = [
        (7001, 3000),
        (7002, 2000),
        (7003, 1000),
    ]
    for dialog_id, last_delta_checked_at in rows:
        sync_db.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at, last_delta_checked_at) "
            "VALUES (?, 'synced', ?, ?)",
            (dialog_id, 1000, last_delta_checked_at),
        )
        seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
            (dialog_id,),
        )
    sync_db.commit()

    calls: list[int] = []

    async def _iter_messages(**kwargs: object):
        calls.append(cast(int, kwargs["entity"]))
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up(
        policy=DeltaCatchUpPolicy(
            interval_seconds=300.0,
            max_probes_per_cycle=2,
            probe_pause_seconds=0.01,
        )
    )

    assert calls == [7003, 7002]


@pytest.mark.asyncio
async def test_delta_catch_up_prioritizes_requested_refresh(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Explicit refresh requests bypass recent-skip once and are selected before stale background work."""
    import time as _time

    now = int(_time.time())
    requested_dialog = 7101
    stale_dialog = 7102
    sync_db.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, last_synced_at, last_delta_checked_at, delta_refresh_requested_at) "
        "VALUES (?, 'synced', ?, ?, ?)",
        (requested_dialog, now - 60, now - 60, now - 30),
    )
    seed_full_history_enrollment(sync_db, requested_dialog, enabled=True)
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at, last_delta_checked_at) "
        "VALUES (?, 'synced', ?, ?)",
        (stale_dialog, 1000, 1000),
    )
    seed_full_history_enrollment(sync_db, stale_dialog, enabled=True)
    for dialog_id in (requested_dialog, stale_dialog):
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
            (dialog_id,),
        )
    sync_db.commit()

    calls: list[int] = []

    async def _iter_messages(**kwargs: object):
        calls.append(cast(int, kwargs["entity"]))
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up(
        policy=DeltaCatchUpPolicy(
            interval_seconds=300.0,
            max_probes_per_cycle=1,
            probe_pause_seconds=0.01,
        )
    )

    row = cast(
        tuple[int | None] | None,
        sync_db.execute(
            "SELECT delta_refresh_requested_at FROM synced_dialogs WHERE dialog_id = ?",
            (requested_dialog,),
        ).fetchone(),
    )
    assert calls == [requested_dialog]
    assert row is not None
    assert row[0] is None


@pytest.mark.asyncio
async def test_delta_catch_up_complete_log_includes_watermark(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cycle summary exposes enough aggregate state to audit multi-day catch-up progress."""
    import logging

    checked_dialog = 7201
    never_checked_dialog = 7202
    requested_dialog = 7203
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at, last_delta_checked_at) "
        "VALUES (?, 'synced', ?, ?)",
        (checked_dialog, 1000, 1000),
    )
    seed_full_history_enrollment(sync_db, checked_dialog, enabled=True)
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, last_synced_at) VALUES (?, 'synced', ?)",
        (never_checked_dialog, 1000),
    )
    seed_full_history_enrollment(sync_db, never_checked_dialog, enabled=True)
    sync_db.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, last_synced_at, last_delta_checked_at, delta_refresh_requested_at) "
        "VALUES (?, 'synced', ?, ?, ?)",
        (requested_dialog, 1000, 1000, 2000),
    )
    seed_full_history_enrollment(sync_db, requested_dialog, enabled=True)
    for dialog_id in (checked_dialog, never_checked_dialog, requested_dialog):
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, 10, 1704067200)",
            (dialog_id,),
        )
    sync_db.commit()

    async def _iter_messages(**kwargs: object):
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    with caplog.at_level(logging.INFO, logger="mcp_telegram.delta_sync"):
        await worker.run_delta_catch_up(
            policy=DeltaCatchUpPolicy(
                interval_seconds=300.0,
                max_probes_per_cycle=1,
                probe_pause_seconds=0.01,
            )
        )

    complete_logs = [
        record.getMessage() for record in caplog.records if "delta_catch_up complete" in record.getMessage()
    ]
    assert len(complete_logs) == 1
    summary = complete_logs[-1]
    assert "total_synced=3" in summary
    assert "checked_total=2" in summary
    assert "never_checked=1" in summary
    assert "pending_refresh=0" in summary
    assert "oldest_delta_checked_age_s=" in summary


@pytest.mark.asyncio
async def test_delta_catch_up_loop_does_not_emit_redundant_info_completion(
    shutdown_event: asyncio.Event, caplog: pytest.LogCaptureFixture
) -> None:
    class _Worker:
        async def run_delta_catch_up(self, *, policy: DeltaCatchUpPolicy) -> int:
            shutdown_event.set()
            return 3

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.delta_sync"):
        await run_delta_catch_up_loop(
            _Worker(),
            shutdown_event,
            DeltaCatchUpPolicy(interval_seconds=300.0, max_probes_per_cycle=1, probe_pause_seconds=0.01),
        )

    cycle_logs = [record for record in caplog.records if "delta_catch_up_cycle complete" in record.getMessage()]
    assert len(cycle_logs) == 1
    assert cycle_logs[0].levelno == logging.DEBUG


@pytest.mark.asyncio
async def test_delta_catch_up_loop_can_be_disabled(
    mock_client: _MockClient,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    calls: list[object] = []

    async def _iter_messages(**kwargs: object):
        calls.append(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await run_delta_catch_up_loop(
        worker,
        shutdown_event,
        DeltaCatchUpPolicy(
            interval_seconds=300.0,
            max_probes_per_cycle=0,
            probe_pause_seconds=1.0,
        ),
    )

    assert calls == []
