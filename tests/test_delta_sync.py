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
from datetime import datetime, timezone
from types import SimpleNamespace
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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        return
        yield  # empty async generator

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 0

    count = sync_db.execute(
        "SELECT COUNT(*) FROM messages WHERE dialog_id=?", (dialog_id,)
    ).fetchone()[0]
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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        captured_kwargs.update(kwargs)
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.run_delta_catch_up()

    assert captured_kwargs.get("min_id") == 200, (
        f"Expected min_id=200, got {captured_kwargs.get('min_id')}"
    )
    assert captured_kwargs.get("reverse") is True, (
        f"Expected reverse=True, got {captured_kwargs.get('reverse')}"
    )


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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    slept_for: list[float] = []

    async def _mock_wait_for(coro: Any, timeout: float) -> None:  # noqa: ANN401
        slept_for.append(timeout)
        raise asyncio.TimeoutError

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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
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

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        for m in new_msgs:
            yield m

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    total = await worker.run_delta_catch_up()

    assert total == 2

    fts_rows = sync_db.execute(
        "SELECT message_id, stemmed_text FROM messages_fts "
        "WHERE dialog_id = ? ORDER BY message_id",
        (dialog_id,),
    ).fetchall()
    assert len(fts_rows) == 2, f"Expected 2 FTS rows for gap messages, got {len(fts_rows)}"
    for row in fts_rows:
        assert row[1] != "", "stemmed_text must be non-empty for gap-filled messages with text"
