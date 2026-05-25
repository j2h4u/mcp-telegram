"""Tests for DialogReconciliationWorker — Phase 43 (RECON-01..05)."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    PeerIdInvalidError,
)
from telethon.tl import types

from mcp_telegram.dialog_sync import (
    DialogReconciliationWorker,
    run_reconciliation_loop,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema

# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def sync_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def shutdown_event() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock()
    client.is_connected.return_value = True
    client.get_entity = AsyncMock()
    return client


# --- helpers ----------------------------------------------------------------


def _seed_dialog_row(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    name: str = "Old",
    type_: str = "user",
    needs_refresh: int = 0,
    hidden: int = 0,
    snapshot_at: int = 1700000000,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO dialogs (dialog_id, name, type, archived, pinned, "
            "snapshot_at, hidden, needs_refresh) "
            "VALUES (?, ?, ?, 0, 0, ?, ?, ?)",
            (dialog_id, name, type_, snapshot_at, hidden, needs_refresh),
        )


def _seed_synced_dialog(
    conn: sqlite3.Connection, dialog_id: int, status: str = "syncing"
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, ?)",
            (dialog_id, status),
        )


def _make_user(uid: int, *, first_name: str = "Alice") -> Any:
    return types.User(id=uid, access_hash=1, first_name=first_name)


def _make_dialog(uid: int, *, name: str = "Alice") -> Any:
    # Minimal Dialog-like object compatible with _extract_dialog_row.
    d = MagicMock()
    d.id = uid
    d.entity = _make_user(uid, first_name=name)
    d.message = None
    d.draft = None
    d.folder_id = None
    d.pinned = False
    d.unread_mentions_count = 0
    d.unread_reactions_count = 0
    return d


def _async_iter(items: list[Any]):
    async def _gen():
        for item in items:
            yield item
    return _gen()


def _make_flood(seconds: int) -> FloodWaitError:
    """Construct a real FloodWaitError instance (mirrors tests/test_dialog_sync.py:228).

    FloodWaitError is a BaseException subclass — Python's `raise` requires a
    real instance, so MagicMock(spec=FloodWaitError) does NOT work here.
    Use `capture=N` to set the .seconds attribute via Telethon's own ctor.
    """
    return FloodWaitError(request=None, capture=seconds)  # type: ignore[call-arg]


# --- light pass tests -------------------------------------------------------


@pytest.mark.asyncio
async def test_recon_light_pass_resets_needs_refresh(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    _seed_dialog_row(sync_db, 100, name="Old", needs_refresh=1)
    mock_client.get_entity.return_value = _make_user(100, first_name="NewName")

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    count = await worker.run_light_pass()

    assert count == 1
    row = sync_db.execute(
        "SELECT name, needs_refresh FROM dialogs WHERE dialog_id=?", (100,)
    ).fetchone()
    assert row[0] == "NewName"
    assert row[1] == 0


@pytest.mark.asyncio
async def test_recon_light_pass_skips_hidden(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    _seed_dialog_row(sync_db, 100, needs_refresh=1, hidden=1)

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    await worker.run_light_pass()

    mock_client.get_entity.assert_not_called()


@pytest.mark.asyncio
async def test_recon_light_pass_skips_clean(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    _seed_dialog_row(sync_db, 100, needs_refresh=0)

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    await worker.run_light_pass()

    mock_client.get_entity.assert_not_called()


@pytest.mark.asyncio
async def test_recon_light_pass_never_calls_iter_dialogs(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    """RECON-02 explicit gate: run_light_pass MUST NOT touch iter_dialogs."""
    _seed_dialog_row(sync_db, 100, needs_refresh=1)
    _seed_dialog_row(sync_db, 200, needs_refresh=1)
    mock_client.get_entity.side_effect = [
        _make_user(100), _make_user(200),
    ]
    # Make iter_dialogs explosive — any call should fail the test loudly.
    mock_client.iter_dialogs = MagicMock(
        side_effect=AssertionError("light pass must not call iter_dialogs")
    )

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    await worker.run_light_pass()

    mock_client.iter_dialogs.assert_not_called()


@pytest.mark.asyncio
async def test_recon_light_pass_flood_wait_advances_to_next_dialog(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    """FloodWait semantics: sleep then ADVANCE to next dialog (no retry)."""
    _seed_dialog_row(sync_db, 100, needs_refresh=1)
    _seed_dialog_row(sync_db, 200, needs_refresh=1)

    # First dialog raises FloodWait, second succeeds.
    mock_client.get_entity.side_effect = [
        _make_flood(1),
        _make_user(200, first_name="B"),
    ]

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    count = await worker.run_light_pass()

    # 2 entity calls: one floods, one succeeds.
    assert mock_client.get_entity.call_count == 2
    # Only the second dialog was refreshed (count=1, not 2).
    assert count == 1
    # Dialog 100 still dirty (will retry next cycle); dialog 200 clean.
    rows = {
        r[0]: r[1]
        for r in sync_db.execute(
            "SELECT dialog_id, needs_refresh FROM dialogs"
        ).fetchall()
    }
    assert rows[100] == 1  # NOT cleared — FloodWait advanced past it
    assert rows[200] == 0  # cleared


@pytest.mark.asyncio
async def test_recon_light_pass_flood_wait_returns_on_shutdown(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    _seed_dialog_row(sync_db, 100, needs_refresh=1)
    # Long FloodWait — but shutdown fires immediately, so wait_for returns
    # before the flood timer expires.
    mock_client.get_entity.side_effect = _make_flood(3600)
    shutdown_event.set()

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    count = await worker.run_light_pass()

    assert count == 0
    row = sync_db.execute(
        "SELECT needs_refresh FROM dialogs WHERE dialog_id=?", (100,)
    ).fetchone()
    assert row[0] == 1  # still dirty — no UPDATE happened


@pytest.mark.asyncio
async def test_recon_light_pass_access_lost_sets_hidden(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    _seed_dialog_row(sync_db, 100, needs_refresh=1)
    _seed_synced_dialog(sync_db, 100, status="syncing")

    # ChannelPrivateError takes the same Telethon-error kwargs as FloodWaitError.
    mock_client.get_entity.side_effect = ChannelPrivateError(request=None)

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    await worker.run_light_pass()

    synced = sync_db.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id=?", (100,)
    ).fetchone()
    assert synced[0] == "access_lost"
    dialog = sync_db.execute(
        "SELECT hidden FROM dialogs WHERE dialog_id=?", (100,)
    ).fetchone()
    assert dialog[0] == 1


@pytest.mark.asyncio
async def test_recon_light_pass_peer_invalid_leaves_dirty(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PeerIdInvalidError: leave needs_refresh=1, log distinctly, continue."""
    _seed_dialog_row(sync_db, 100, needs_refresh=1)
    mock_client.get_entity.side_effect = PeerIdInvalidError(request=None)

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    with caplog.at_level(logging.WARNING):
        count = await worker.run_light_pass()

    assert count == 0
    row = sync_db.execute(
        "SELECT needs_refresh, hidden FROM dialogs WHERE dialog_id=?", (100,)
    ).fetchone()
    assert row[0] == 1  # still dirty
    assert row[1] == 0  # NOT hidden (this is not access loss)
    assert any("recon_light_pass_peer_invalid" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_recon_light_pass_emits_complete_log(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plan 03 UAT depends on this log line being emitted on every run."""
    _seed_dialog_row(sync_db, 100, needs_refresh=1)
    mock_client.get_entity.return_value = _make_user(100)

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    with caplog.at_level(logging.INFO):
        await worker.run_light_pass()

    assert any(
        "recon_light_pass_complete count=" in r.message for r in caplog.records
    ), f"missing recon_light_pass_complete log; saw: {[r.message for r in caplog.records]}"


# --- full pass tests --------------------------------------------------------


@pytest.mark.asyncio
async def test_recon_full_pass_upserts_returned(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    _seed_dialog_row(sync_db, 100, name="A_old")
    _seed_dialog_row(sync_db, 200, name="B_old")
    mock_client.iter_dialogs = MagicMock(
        return_value=_async_iter([_make_dialog(100, name="A_new"), _make_dialog(200, name="B_new")])
    )

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    count, completed = await worker.run_full_pass()

    assert count == 2
    assert completed is True
    rows = sync_db.execute(
        "SELECT dialog_id, name, hidden FROM dialogs ORDER BY dialog_id"
    ).fetchall()
    assert rows[0][1] == "A_new" and rows[0][2] == 0
    assert rows[1][1] == "B_new" and rows[1][2] == 0


@pytest.mark.asyncio
async def test_recon_full_pass_hides_missing(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    _seed_dialog_row(sync_db, 100)
    _seed_dialog_row(sync_db, 200)
    _seed_dialog_row(sync_db, 300)
    # iter_dialogs returns only 100 and 200; 300 must be soft-deleted.
    mock_client.iter_dialogs = MagicMock(
        return_value=_async_iter([_make_dialog(100), _make_dialog(200)])
    )

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    await worker.run_full_pass()

    hidden = {
        row[0]: row[1]
        for row in sync_db.execute("SELECT dialog_id, hidden FROM dialogs").fetchall()
    }
    assert hidden[100] == 0
    assert hidden[200] == 0
    assert hidden[300] == 1


@pytest.mark.asyncio
async def test_recon_full_pass_flood_wait_skips_soft_delete(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    """Mid-stream FloodWait: soft-delete branch never runs when flood interrupts.

    The generator yields dialog 100 (which gets UPSERTed, count=1), then raises
    FloodWait with a 3600s wait. We use call_later(0.02) to fire shutdown_event
    during the asyncio.wait_for sleep, making it return immediately. The function
    returns count=1 without reaching the soft-delete block.

    We do NOT pre-set shutdown_event before the call (that would make the
    async-for shutdown guard fire before the UPSERT happens, giving count=0).
    """
    _seed_dialog_row(sync_db, 100)
    _seed_dialog_row(sync_db, 200)

    async def _gen():
        yield _make_dialog(100)
        raise _make_flood(3600)  # long wait — will be cut short by shutdown

    mock_client.iter_dialogs = MagicMock(return_value=_gen())
    # Fire shutdown_event after a short delay — well into the wait_for sleep,
    # not before the UPSERT body runs.
    asyncio.get_event_loop().call_later(0.02, shutdown_event.set)

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    count, completed = await worker.run_full_pass()

    # 1 row UPSERTed before flood; soft-delete branch did NOT run.
    assert count == 1
    assert completed is False
    hidden = {
        row[0]: row[1]
        for row in sync_db.execute("SELECT dialog_id, hidden FROM dialogs").fetchall()
    }
    assert hidden[200] == 0  # NOT hidden — soft-delete branch never ran


# --- loop tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_reconciliation_loop_respects_pre_set_shutdown(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
) -> None:
    event = asyncio.Event()
    event.set()  # shut down before loop starts

    await run_reconciliation_loop(mock_client, sync_db, event)

    mock_client.get_entity.assert_not_called()
    # iter_dialogs should also not be called (the while-condition gates it)
    if hasattr(mock_client, "iter_dialogs") and isinstance(mock_client.iter_dialogs, MagicMock):
        mock_client.iter_dialogs.assert_not_called()


@pytest.mark.asyncio
async def test_run_reconciliation_loop_runs_full_pass_first(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
) -> None:
    # Empty dialogs table; iter_dialogs yields nothing.
    mock_client.iter_dialogs = MagicMock(return_value=_async_iter([]))

    event = asyncio.Event()

    # Run the loop briefly: short hourly interval + shutdown after one cycle.
    task = asyncio.create_task(
        run_reconciliation_loop(
            mock_client, sync_db, event,
            hourly_interval=0.01, daily_interval=86400.0,
        )
    )
    await asyncio.sleep(0.05)
    event.set()
    await asyncio.wait_for(task, timeout=1.0)

    # First iteration always runs full pass (last_full_pass=0.0).
    assert mock_client.iter_dialogs.called


@pytest.mark.asyncio
async def test_recon_loop_full_pass_failure_does_not_advance_last_full_pass(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """43-REVIEWS.md Codex MEDIUM: if run_full_pass raises, the next hourly
    tick must retry the full pass instead of waiting a full day."""
    from mcp_telegram import dialog_sync

    full_call_count = 0
    light_call_count = 0

    async def _fake_full(self) -> tuple[int, bool]:
        nonlocal full_call_count
        full_call_count += 1
        raise RuntimeError("simulated full pass failure")

    async def _fake_light(self):
        nonlocal light_call_count
        light_call_count += 1
        return 0

    monkeypatch.setattr(
        dialog_sync.DialogReconciliationWorker, "run_full_pass", _fake_full
    )
    monkeypatch.setattr(
        dialog_sync.DialogReconciliationWorker, "run_light_pass", _fake_light
    )

    event = asyncio.Event()
    # Use small interval so we get multiple iterations quickly, but daily
    # interval is also small so the second iteration's "now - last_full_pass"
    # check will still trigger run_full_pass.
    task = asyncio.create_task(
        run_reconciliation_loop(
            mock_client, sync_db, event,
            hourly_interval=0.01, daily_interval=0.01,
        )
    )
    await asyncio.sleep(0.1)  # enough for several iterations
    event.set()
    await asyncio.wait_for(task, timeout=1.0)

    # Full pass should have been attempted MORE THAN ONCE — proves
    # last_full_pass did not advance after the first (failed) attempt.
    assert full_call_count >= 2, (
        f"run_full_pass attempted only {full_call_count} time(s) — "
        "last_full_pass advanced despite the failure"
    )
    # Light pass also runs every iteration.
    assert light_call_count >= 2


# --- _refresh_forum_topics tests --------------------------------------------


@pytest.mark.asyncio
async def test_refresh_forum_topics_upserts(
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """_refresh_forum_topics writes topics into topic_metadata via upsert."""
    # Build a mock entity with forum=True (a Channel)
    entity = MagicMock()
    entity.forum = True

    # Build a mock ForumTopic
    topic = MagicMock()
    topic.id = 1
    topic.title = "General"
    topic.icon_emoji_id = None
    topic.date = MagicMock()
    topic.date.timestamp = MagicMock(return_value=1700000000.0)

    mock_result = MagicMock()
    mock_result.topics = [topic]

    # _refresh_forum_topics calls await self._client(GetForumTopicsRequest(...))
    # Use AsyncMock for the client so the call is awaitable
    client = AsyncMock()
    client.return_value = mock_result

    worker = DialogReconciliationWorker(client, sync_db, shutdown_event)
    count = await worker._refresh_forum_topics(dialog_id=999, entity=entity)

    assert count == 1
    row = sync_db.execute(
        "SELECT title FROM topic_metadata WHERE dialog_id=999 AND topic_id=1"
    ).fetchone()
    assert row is not None, "topic_metadata row must exist after refresh"
    assert row[0] == "General"


@pytest.mark.asyncio
async def test_light_pass_refreshes_forum_topics(
    sync_db: sqlite3.Connection,
    mock_client: MagicMock,
    shutdown_event: asyncio.Event,
) -> None:
    """run_light_pass calls _refresh_forum_topics for forum=True dialogs."""
    # Seed a forum dialog with needs_refresh=1
    _seed_dialog_row(sync_db, 777, name="Forum Group", needs_refresh=1)

    # get_entity returns an entity with forum=True
    forum_entity = MagicMock()
    forum_entity.forum = True
    forum_entity.first_name = None
    forum_entity.last_name = None
    forum_entity.title = "Forum Group"
    forum_entity.username = None
    forum_entity.participants_count = None
    forum_entity.date = None
    mock_client.get_entity = AsyncMock(return_value=forum_entity)

    # client(GetForumTopicsRequest(...)) returns empty topics
    mock_result = MagicMock()
    mock_result.topics = []
    mock_client.return_value = mock_result
    mock_client.side_effect = None

    worker = DialogReconciliationWorker(mock_client, sync_db, shutdown_event)
    with patch.object(
        worker, "_refresh_forum_topics", wraps=worker._refresh_forum_topics
    ) as spy:
        await worker.run_light_pass()

    spy.assert_called_once_with(777, forum_entity)
