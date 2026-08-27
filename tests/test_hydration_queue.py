from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from mcp_telegram.hydration_queue import HydrationJob, HydrationQueueRepository


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE hydration_jobs (
            kind TEXT NOT NULL,
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            due_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL,
            PRIMARY KEY (kind, dialog_id, message_id)
        ) WITHOUT ROWID
        """
    )
    return conn


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = _make_db()
    try:
        yield conn
    finally:
        conn.close()


def _job(kind: str, dialog_id: int, message_id: int, due_at: int, attempts: int = 0) -> HydrationJob:
    return HydrationJob(kind, dialog_id, message_id, due_at, attempts)


def test_enqueue_is_idempotent_moves_due_time_earlier_and_preserves_attempts(db: sqlite3.Connection) -> None:
    conn = db
    repository = HydrationQueueRepository(conn)
    repository.enqueue(_job("media", -100, 7, 400))
    started = repository.start(_job("media", -100, 7, 400))
    assert started == _job("media", -100, 7, 400, attempts=1)

    repository.enqueue(_job("media", -100, 7, 300, attempts=99))
    assert repository.due_jobs(299, 10) == []
    assert repository.due_jobs(300, 10) == [_job("media", -100, 7, 300, attempts=1)]

    repository.enqueue(_job("media", -100, 7, 500, attempts=99))
    assert repository.due_jobs(499, 10) == [_job("media", -100, 7, 300, attempts=1)]


def test_due_jobs_are_deterministically_ordered_and_limited(db: sqlite3.Connection) -> None:
    conn = db
    repository = HydrationQueueRepository(conn)
    for job in (
        _job("z", 1, 1, 10),
        _job("a", 2, 2, 10),
        _job("a", 1, 3, 10),
        _job("a", 1, 2, 9),
        _job("future", 1, 1, 11),
    ):
        repository.enqueue(job)

    assert repository.due_jobs(10, 3) == [
        _job("a", 1, 2, 9),
        _job("a", 1, 3, 10),
        _job("a", 2, 2, 10),
    ]
    assert repository.due_jobs(10, 0) == []


def test_start_increments_attempts_atomically_and_missing_job_returns_none(db: sqlite3.Connection) -> None:
    conn = db
    repository = HydrationQueueRepository(conn)
    queued = _job("read", 42, 8, 500, attempts=3)
    repository.enqueue(queued)

    assert repository.start(queued) == _job("read", 42, 8, 500, attempts=4)
    assert repository.start(_job("missing", 42, 8, 500)) is None


def test_reschedule_and_remove_only_touch_existing_identity(db: sqlite3.Connection) -> None:
    conn = db
    repository = HydrationQueueRepository(conn)
    queued = _job("media", 42, 9, 100)
    repository.enqueue(queued)

    assert repository.reschedule(queued, 900)
    assert repository.due_jobs(899, 10) == []
    assert repository.due_jobs(900, 10) == [_job("media", 42, 9, 900)]
    assert repository.remove(queued)
    assert repository.due_jobs(1_000, 10) == []
    assert not repository.remove(queued)


def test_repository_never_commits_callers_transaction(db: sqlite3.Connection) -> None:
    conn = db
    repository = HydrationQueueRepository(conn)
    conn.execute("BEGIN")
    repository.enqueue(_job("media", 42, 1, 100))
    assert conn.in_transaction
    conn.rollback()
    assert repository.due_jobs(100, 10) == []


@pytest.mark.parametrize(
    ("kind", "dialog_id", "message_id"),
    [("", 1, 1), ("media", 0, 1), ("media", -1, 0)],
)
def test_job_rejects_invalid_boundary_identity(kind: str, dialog_id: int, message_id: int) -> None:
    with pytest.raises(ValueError):
        _job(kind, dialog_id, message_id, 1)
