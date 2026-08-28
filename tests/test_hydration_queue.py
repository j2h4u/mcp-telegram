from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import cast

import pytest

from mcp_telegram.hydration_queue import (
    _REGISTERED_HYDRATION_KINDS,
    _SUMMARY_AGGREGATE_SQL,
    MEDIA_METADATA_KIND,
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationJob,
    HydrationPriority,
    HydrationQueueRepository,
)


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
            message_sent_at INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0 CHECK (priority IN (0, 1)),
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


def _job(  # noqa: PLR0913
    kind: str,
    dialog_id: int,
    message_id: int,
    due_at: int,
    attempts: int = 0,
    message_sent_at: int = 0,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
) -> HydrationJob:
    return HydrationJob(
        kind,
        dialog_id,
        message_id,
        due_at,
        attempts,
        message_sent_at=message_sent_at,
        priority=priority,
    )


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


def test_due_jobs_prioritize_foreground_then_newest_message(db: sqlite3.Connection) -> None:
    repository = HydrationQueueRepository(db)
    jobs = [
        _job("media", 1, 1, 1, message_sent_at=100, priority=HydrationPriority.BACKFILL),
        _job("media", 1, 2, 1, message_sent_at=300, priority=HydrationPriority.BACKFILL),
        _job("media", 2, 1, 1, message_sent_at=200),
        _job("media", 3, 1, 1, message_sent_at=50),
    ]
    for job in jobs:
        repository.enqueue(job)

    assert repository.due_jobs(1, 10) == [jobs[2], jobs[3], jobs[1], jobs[0]]


def test_enqueue_promotes_existing_backfill_without_resetting_attempts(db: sqlite3.Connection) -> None:
    repository = HydrationQueueRepository(db)
    backfill = _job("media", 1, 1, 100, priority=HydrationPriority.BACKFILL)
    repository.enqueue(backfill)
    started = repository.start(backfill)
    assert started is not None and started.attempts == 1

    repository.enqueue(_job("media", 1, 1, 50))
    assert repository.due_jobs(50, 10) == [_job("media", 1, 1, 50, attempts=1)]

    repository.enqueue(_job("media", 1, 1, 25, priority=HydrationPriority.BACKFILL))
    assert repository.due_jobs(25, 10) == [_job("media", 1, 1, 25, attempts=1)]


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


def test_summarize_for_dialog_includes_future_jobs_and_bounds_message_ids(db: sqlite3.Connection) -> None:
    repository = HydrationQueueRepository(db)
    for message_id in range(1, 41):
        repository.enqueue(_job(MEDIA_METADATA_KIND, 42, message_id, 9_999, attempts=message_id % 4))
    for message_id in (1, 2):
        repository.enqueue(_job(TRANSCRIPTION_HYDRATION_KIND, 42, message_id, 9_999, attempts=7))
    repository.enqueue(_job(MEDIA_METADATA_KIND, 99, 1, 9_999, attempts=99))

    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        summaries = repository.summarize_for_dialog(42)
    finally:
        db.set_trace_callback(None)
    media = summaries[0]
    transcription = summaries[1]
    assert media.kind == MEDIA_METADATA_KIND
    assert media.dialog_id == 42
    assert media.job_count == 40
    assert media.message_ids == tuple(range(1, 33))
    assert media.attempts_min == 0
    assert media.attempts_max == 3
    assert transcription.kind == TRANSCRIPTION_HYDRATION_KIND
    assert transcription.job_count == 2
    assert transcription.message_ids == (1, 2)
    assert transcription.attempts_min == transcription.attempts_max == 7
    id_queries = [statement for statement in statements if "SELECT message_id FROM hydration_jobs" in statement]
    assert len(id_queries) == len(_REGISTERED_HYDRATION_KINDS)
    assert all("WHERE kind = " in statement for statement in id_queries)
    assert all("ORDER BY message_id LIMIT 32" in statement for statement in id_queries)
    assert not any("SELECT kind, message_id FROM hydration_jobs" in statement for statement in statements)

    for kind in _REGISTERED_HYDRATION_KINDS:
        plan_rows = cast(
            list[tuple[object, ...]],
            db.execute("EXPLAIN QUERY PLAN " + _SUMMARY_AGGREGATE_SQL, (kind, 42)).fetchall(),
        )
        details = [str(row[3]) for row in plan_rows]
        assert any("SEARCH hydration_jobs USING PRIMARY KEY (kind=? AND dialog_id=?)" in detail for detail in details)
        assert not any("SCAN hydration_jobs" in detail for detail in details)


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
