from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

from mcp_telegram.access_lifecycle import restore_access_after_revalidation
from mcp_telegram.config import MediaHydrationConfig
from mcp_telegram.media_hydration import MediaHydrationWorker
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.telegram_rpc import TelegramRpcCircuitOpenError


class _Client:
    def __init__(self, response: object | None = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def get_messages(self, *_args: object, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _EchoClient(_Client):
    """Return one media-less object for each requested id."""

    async def get_messages(self, *_args: object, **kwargs: object) -> object:
        self.calls.append(kwargs)
        raw_ids = kwargs.get("ids", [])
        ids: list[int] = []
        if not isinstance(raw_ids, list):
            raise AssertionError("message ids must be a list")
        for message_id in raw_ids:
            if not isinstance(message_id, int):
                raise AssertionError("message ids must be integers")
            ids.append(message_id)
        return [SimpleNamespace(id=int(message_id), media=None) for message_id in ids]


@pytest.fixture
def db(tmp_path: Path):
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        yield conn
    finally:
        conn.close()


def _seed(conn: sqlite3.Connection, dialog_id: int = 1, message_id: int = 1) -> None:
    conn.execute("INSERT OR IGNORE INTO synced_dialogs(dialog_id, status) VALUES (?, 'synced')", (dialog_id,))
    conn.execute(
        "INSERT OR IGNORE INTO full_history_enrollment(dialog_id, enabled, source, updated_at) "
        "VALUES (?, 1, 'explicit', 1)",
        (dialog_id,),
    )
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) VALUES (?, ?, 1, 'keep', 'other', '{}')",
        (dialog_id, message_id),
    )
    conn.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts) VALUES ('media_metadata', ?, ?, 1, 0)",
        (dialog_id, message_id),
    )
    conn.commit()


def _worker(
    conn: sqlite3.Connection, client: _Client, policy: MediaHydrationConfig | None = None
) -> MediaHydrationWorker:
    config = policy or MediaHydrationConfig(pause_between_requests_seconds=0.01)
    return MediaHydrationWorker(
        client,
        conn,
        asyncio.Event(),
        interval_seconds=config.interval_seconds,
        max_requests_per_cycle=config.max_requests_per_cycle,
        max_jobs_per_cycle=config.max_jobs_per_cycle,
        batch_size=config.batch_size,
        pause_between_requests_seconds=config.pause_between_requests_seconds,
        retry_delay_seconds=config.retry_delay_seconds,
        circuit_retry_seconds=config.circuit_retry_seconds,
        max_attempts=config.max_attempts,
    )


@pytest.mark.asyncio
async def test_authoritative_media_update_does_not_touch_text_or_fts(db: sqlite3.Connection) -> None:
    _seed(db)
    before = cast(
        tuple[str] | None,
        db.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=1 AND message_id=1").fetchone(),
    )
    client = _Client([SimpleNamespace(id=1, media=SimpleNamespace())])
    result = await _worker(db, client).run_cycle(now=1)
    assert result.completed == 1
    assert db.execute(
        "SELECT text, media_kind, media_payload FROM messages WHERE dialog_id=1 AND message_id=1"
    ).fetchone() == ("keep", "other", '{"type":"SimpleNamespace"}')
    assert db.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=1 AND message_id=1").fetchone() == before
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_missing_and_no_media_are_terminal(db: sqlite3.Connection) -> None:
    _seed(db)
    client = _Client([SimpleNamespace(id=99, media=SimpleNamespace())])
    await _worker(db, client).run_cycle(now=1)
    assert db.execute(
        "SELECT media_kind, media_payload FROM messages WHERE dialog_id=1 AND message_id=1"
    ).fetchone() == ("other", "{}")
    _seed(db, message_id=2)
    client.response = [SimpleNamespace(id=2, media=None)]
    await _worker(db, client).run_cycle(now=1)
    assert db.execute(
        "SELECT media_kind, media_payload FROM messages WHERE dialog_id=1 AND message_id=2"
    ).fetchone() == (None, None)


@pytest.mark.asyncio
async def test_batch_and_request_cap_are_deterministic(db: sqlite3.Connection) -> None:
    for message_id in range(1, 6):
        _seed(db, message_id=message_id)
    client = _Client([SimpleNamespace(id=1, media=None), SimpleNamespace(id=2, media=None)])
    policy = MediaHydrationConfig(batch_size=2, max_requests_per_cycle=2, pause_between_requests_seconds=0.01)
    await _worker(db, client, policy).run_cycle(now=1)
    assert [call["ids"] for call in client.calls] == [[1, 2], [3, 4]]
    assert db.execute("SELECT message_id FROM hydration_jobs ORDER BY message_id").fetchall() == [(5,)]


@pytest.mark.asyncio
async def test_job_and_request_caps_apply_across_multiple_dialogs(db: sqlite3.Connection) -> None:
    """Global cycle caps bound work even when due jobs span several dialogs."""
    for dialog_id in (1, 2):
        for message_id in (1, 2):
            _seed(db, dialog_id=dialog_id, message_id=message_id)
    client = _EchoClient()
    policy = MediaHydrationConfig(
        batch_size=1,
        max_jobs_per_cycle=3,
        max_requests_per_cycle=2,
        pause_between_requests_seconds=0.01,
    )

    result = await _worker(db, client, policy).run_cycle(now=1)

    assert result.requests == 2
    assert [call["entity"] for call in client.calls] == [1, 1]
    assert [call["ids"] for call in client.calls] == [[1], [2]]
    assert db.execute("SELECT dialog_id, message_id FROM hydration_jobs ORDER BY dialog_id, message_id").fetchall() == [
        (2, 1),
        (2, 2),
    ]


@pytest.mark.asyncio
async def test_batching_preserves_newest_first_across_dialogs(db: sqlite3.Connection) -> None:
    _seed(db, dialog_id=1, message_id=1)
    _seed(db, dialog_id=1, message_id=2)
    _seed(db, dialog_id=2, message_id=1)
    db.execute("UPDATE messages SET sent_at = 400 WHERE dialog_id = 1 AND message_id = 1")
    db.execute("UPDATE messages SET sent_at = 100 WHERE dialog_id = 1 AND message_id = 2")
    db.execute("UPDATE messages SET sent_at = 300 WHERE dialog_id = 2 AND message_id = 1")
    db.execute("UPDATE hydration_jobs SET message_sent_at = 400 WHERE dialog_id = 1 AND message_id = 1")
    db.execute("UPDATE hydration_jobs SET message_sent_at = 100 WHERE dialog_id = 1 AND message_id = 2")
    db.execute("UPDATE hydration_jobs SET message_sent_at = 300 WHERE dialog_id = 2 AND message_id = 1")
    db.commit()
    client = _EchoClient()
    policy = MediaHydrationConfig(
        batch_size=1,
        max_jobs_per_cycle=3,
        max_requests_per_cycle=2,
        pause_between_requests_seconds=0.01,
    )

    await _worker(db, client, policy).run_cycle(now=1)

    assert [(call["entity"], call["ids"]) for call in client.calls] == [(1, [1]), (2, [1])]
    assert db.execute("SELECT dialog_id, message_id FROM hydration_jobs").fetchall() == [(1, 2)]


@pytest.mark.asyncio
async def test_foreground_job_preempts_newer_backfill(db: sqlite3.Connection) -> None:
    _seed(db, dialog_id=1, message_id=1)
    _seed(db, dialog_id=2, message_id=1)
    db.execute("UPDATE messages SET sent_at = 100 WHERE dialog_id = 1")
    db.execute("UPDATE messages SET sent_at = 500 WHERE dialog_id = 2")
    db.execute("UPDATE hydration_jobs SET message_sent_at = 100 WHERE dialog_id = 1")
    db.execute("UPDATE hydration_jobs SET message_sent_at = 500 WHERE dialog_id = 2")
    db.execute("UPDATE hydration_jobs SET priority = 1 WHERE dialog_id = 1")
    db.commit()
    client = _EchoClient()
    policy = MediaHydrationConfig(
        batch_size=1,
        max_jobs_per_cycle=2,
        max_requests_per_cycle=1,
        pause_between_requests_seconds=0.01,
    )

    await _worker(db, client, policy).run_cycle(now=1)

    assert [(call["entity"], call["ids"]) for call in client.calls] == [(1, [1])]
    assert db.execute("SELECT dialog_id FROM hydration_jobs").fetchall() == [(2,)]


@pytest.mark.asyncio
async def test_transient_retries_then_caps_after_durable_attempts(db: sqlite3.Connection) -> None:
    _seed(db)
    client = _Client(error=RuntimeError("opaque"))
    policy = MediaHydrationConfig(retry_delay_seconds=10, max_attempts=2, pause_between_requests_seconds=0.01)
    worker = _worker(db, client, policy)
    await worker.run_cycle(now=1)
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 11)
    await worker.run_cycle(now=11)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_flood_wait_stops_without_sleep_and_reschedules(db: sqlite3.Connection) -> None:
    _seed(db)
    client = _Client(error=FloodWaitError(request=None, capture=7))
    result = await _worker(db, client).run_cycle(now=1)
    assert result.stopped
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 8)


@pytest.mark.asyncio
async def test_flood_wait_uses_shared_account_observer(monkeypatch: pytest.MonkeyPatch, db: sqlite3.Connection) -> None:
    _seed(db)
    client = _Client(error=FloodWaitError(request=None, capture=7))
    observed: list[tuple[BaseException, str]] = []

    def _observe(exc: BaseException, *, source: str) -> int:
        observed.append((exc, source))
        return 13

    monkeypatch.setattr("mcp_telegram.media_hydration.flood_seconds", _observe)
    result = await _worker(db, client).run_cycle(now=1)

    assert result.stopped
    assert observed and observed[0][1] == "media_hydration"
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 14)


@pytest.mark.asyncio
async def test_circuit_open_stops_without_sleep_and_uses_circuit_delay(db: sqlite3.Connection) -> None:
    _seed(db)
    client = _Client(error=TelegramRpcCircuitOpenError("closed"))
    policy = MediaHydrationConfig(circuit_retry_seconds=20, pause_between_requests_seconds=0.01)
    result = await _worker(db, client, policy).run_cycle(now=1)
    assert result.stopped
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 21)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["flood", "circuit"])
async def test_multi_job_flood_or_circuit_failure_stops_cycle(failure: str, db: sqlite3.Connection) -> None:
    """A governed failure reschedules the whole batch and prevents later RPCs."""
    for message_id in (1, 2):
        _seed(db, dialog_id=1, message_id=message_id)
    _seed(db, dialog_id=2, message_id=1)
    error: BaseException
    retry_at: int
    if failure == "flood":
        error = FloodWaitError(request=None, capture=7)
        retry_at = 8
    else:
        error = TelegramRpcCircuitOpenError("closed")
        retry_at = 21
    client = _Client(error=error)
    policy = MediaHydrationConfig(
        batch_size=2,
        max_requests_per_cycle=3,
        circuit_retry_seconds=20,
        pause_between_requests_seconds=0.01,
    )

    result = await _worker(db, client, policy).run_cycle(now=1)

    assert result.stopped is True
    assert result.requests == 1
    assert len(client.calls) == 1
    assert client.calls[0]["entity"] == 1
    assert client.calls[0]["ids"] == [1, 2]
    assert db.execute(
        "SELECT dialog_id, message_id, attempts, due_at FROM hydration_jobs ORDER BY dialog_id, message_id"
    ).fetchall() == [(1, 1, 1, retry_at), (1, 2, 1, retry_at), (2, 1, 0, 1)]


@pytest.mark.asyncio
async def test_access_loss_purges_and_restore_reenqueues_unresolved_jobs(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db)
    db.execute(
        "INSERT INTO dialogs(dialog_id, hidden, needs_refresh, snapshot_at, archived, pinned, unread_mentions_count, unread_reactions_count) VALUES (1, 0, 0, 1, 0, 0, 0, 0)"
    )
    db.commit()
    monkeypatch.setattr("mcp_telegram.media_hydration.ACCESS_LOST_ERRORS", (RuntimeError,))
    client = _Client(error=RuntimeError("private"))
    await _worker(db, client).run_cycle(now=10)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    restore_access_after_revalidation(db, 1, 20)
    assert db.execute("SELECT kind, dialog_id, message_id, due_at, attempts FROM hydration_jobs").fetchone() == (
        "media_metadata",
        1,
        1,
        20,
        0,
    )


@pytest.mark.asyncio
async def test_worker_drops_queued_job_when_dialog_is_no_longer_eligible(db: sqlite3.Connection) -> None:
    _seed(db)
    db.execute("UPDATE synced_dialogs SET status = 'not_synced' WHERE dialog_id = 1")
    db.commit()
    client = _Client([SimpleNamespace(id=1, media=SimpleNamespace())])
    result = await _worker(db, client).run_cycle(now=1)
    assert result.requests == 0
    assert client.calls == []
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_authoritative_write_rechecks_eligibility_after_rpc_starts(db: sqlite3.Connection) -> None:
    _seed(db)

    class _RacingClient(_Client):
        async def get_messages(self, *_args: object, **kwargs: object) -> object:
            self.calls.append(kwargs)
            self_conn = db
            self_conn.execute("UPDATE synced_dialogs SET status = 'access_lost' WHERE dialog_id = 1")
            self_conn.commit()
            return [SimpleNamespace(id=1, media=SimpleNamespace())]

    client = _RacingClient()
    result = await _worker(db, client).run_cycle(now=1)
    assert result.dropped == 1
    assert db.execute(
        "SELECT media_kind, media_payload FROM messages WHERE dialog_id=1 AND message_id=1"
    ).fetchone() == (
        "other",
        "{}",
    )
