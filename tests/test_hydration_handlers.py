from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.errors.rpcerrorlist import ChatForbiddenError  # type: ignore[import-untyped]

from mcp_telegram.fact_hydration import (
    HydrationDropObservation,
    HydrationHandler,
    MessageFactHydrationWorker,
)
from mcp_telegram.hydration_queue import (
    MEDIA_METADATA_KIND,
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationJob,
    HydrationQueueRepository,
)
from mcp_telegram.media_hydration import MediaFactHydrationHandler
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.transcription_hydration import TranscriptionHydrationHandler


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        yield conn
    finally:
        conn.close()


def _seed_dialog(conn: sqlite3.Connection, dialog_id: int = 1, *, status: str = "synced") -> None:
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, ?)", (dialog_id, status))
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (?, 1, 'explicit', 1)",
        (dialog_id,),
    )


def _seed_message(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    dialog_id: int = 1,
    media_kind: str | None = "other",
    media_payload: str | None = "{}",
) -> None:
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (?, ?, 1, NULL, ?, ?)",
        (dialog_id, message_id, media_kind, media_payload),
    )


def _enqueue(
    conn: sqlite3.Connection,
    kind: str,
    message_id: int,
    *,
    dialog_id: int = 1,
    attempts: int = 0,
) -> HydrationJob:
    job = HydrationJob(kind, dialog_id, message_id, due_at=1, attempts=attempts)
    HydrationQueueRepository(conn).enqueue(job)
    return job


def test_media_apply_maps_single_result_and_persists_empty_and_unknown_facts(
    db: sqlite3.Connection,
) -> None:
    _seed_dialog(db)
    _seed_message(db, 1)
    _seed_message(db, 2)
    first = _enqueue(db, MEDIA_METADATA_KIND, 1)
    second = _enqueue(db, MEDIA_METADATA_KIND, 2)
    db.commit()

    applied = MediaFactHydrationHandler(batch_size=2).apply(
        db,
        HydrationQueueRepository(db),
        [first, second],
        SimpleNamespace(id=1, media=SimpleNamespace()),
        now=20,
    )

    assert applied.hydrated == 1
    assert applied.completed == 1
    assert applied.dropped == 1
    assert applied.drop_observations[0].reason == "missing_response"
    assert db.execute(
        "SELECT media_kind, media_payload FROM messages WHERE message_id = 1"
    ).fetchone() == ("other", '{"type":"SimpleNamespace"}')
    assert db.execute("SELECT media_kind, media_payload FROM messages WHERE message_id = 2").fetchone() == (
        "other",
        "{}",
    )
    assert db.execute("SELECT message_id FROM hydration_jobs ORDER BY message_id").fetchall() == [(2,)]


def test_media_apply_marks_missing_and_invalid_results_terminal(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    _seed_message(db, 1)
    _seed_message(db, 2)
    first = _enqueue(db, MEDIA_METADATA_KIND, 1)
    second = _enqueue(db, MEDIA_METADATA_KIND, 2)
    db.commit()
    handler = MediaFactHydrationHandler(batch_size=2)
    queue = HydrationQueueRepository(db)

    missing = handler.apply(
        db,
        queue,
        [first],
        [SimpleNamespace(id=0), SimpleNamespace(id=99)],
        now=20,
    )
    invalid = handler.apply(db, queue, [second], 123, now=20)

    assert missing.dropped == 1
    assert missing.drop_observations[0].reason == "missing_response"
    assert invalid.dropped == 1
    assert invalid.drop_observations[0].reason == "invalid_result"
    assert db.execute("SELECT terminal, last_outcome FROM hydration_jobs ORDER BY message_id").fetchall() == [
        (1, "terminal_error"),
        (1, "terminal_error"),
    ]


@pytest.mark.parametrize("result", [None, {}, "telegram-result"])
def test_media_apply_rejects_non_collection_results(
    db: sqlite3.Connection,
    result: object,
) -> None:
    _seed_dialog(db)
    _seed_message(db, 1)
    job = _enqueue(db, MEDIA_METADATA_KIND, 1)
    db.commit()

    applied = MediaFactHydrationHandler(batch_size=1).apply(
        db,
        HydrationQueueRepository(db),
        [job],
        result,
        now=20,
    )

    assert applied == applied.__class__(dropped=1, drop_observations=(
        HydrationDropObservation("invalid_result", 1, MEDIA_METADATA_KIND, 1, 0),
    ))


def test_media_apply_reports_not_applied_when_access_is_lost(db: sqlite3.Connection) -> None:
    _seed_dialog(db, status="access_lost")
    _seed_message(db, 1)
    job = _enqueue(db, MEDIA_METADATA_KIND, 1)
    db.commit()

    applied = MediaFactHydrationHandler(batch_size=1).apply(
        db,
        HydrationQueueRepository(db),
        [job],
        SimpleNamespace(id=1, media=None),
        now=20,
    )

    assert applied.completed == 0
    assert applied.dropped == 1
    assert applied.drop_observations[0].reason == "not_applied"
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


def test_transcription_apply_persists_final_result(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice", media_payload="{}")
    job = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    db.commit()

    applied = TranscriptionHydrationHandler(recheck_delay_seconds=30).apply(
        db,
        HydrationQueueRepository(db),
        [job],
        SimpleNamespace(pending=False, text="  speech words  ", transcription_id=7),
        now=20,
    )

    assert applied.hydrated == 1
    assert applied.completed == 1
    assert db.execute("SELECT text, transcription_id, received_at FROM message_transcriptions").fetchone() == (
        "speech words",
        7,
        20,
    )
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


def test_transcription_apply_handles_pending_and_existing_fact(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice", media_payload="{}")
    pending_job = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    db.commit()
    handler = TranscriptionHydrationHandler(recheck_delay_seconds=30)
    queue = HydrationQueueRepository(db)

    pending = handler.apply(
        db, queue, [pending_job], SimpleNamespace(pending=True), now=20
    )
    db.execute(
        "INSERT INTO message_transcriptions(dialog_id, message_id, text, transcription_id, received_at) "
        "VALUES (1, 1, 'event fact', 8, 19)"
    )
    db.commit()
    existing = handler.apply(
        db,
        queue,
        [pending_job],
        SimpleNamespace(pending=False, text="worker fact", transcription_id=9),
        now=20,
    )

    assert pending.pending is True
    assert existing == existing.__class__(completed=1)
    assert db.execute("SELECT text, transcription_id FROM message_transcriptions").fetchone() == ("event fact", 8)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


def test_transcription_apply_removes_job_when_message_is_no_longer_eligible(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="document", media_payload="{}")
    job = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    db.commit()

    applied = TranscriptionHydrationHandler(recheck_delay_seconds=30).apply(
        db,
        HydrationQueueRepository(db),
        [job],
        SimpleNamespace(pending=False, text="stale result", transcription_id=9),
        now=20,
    )

    assert applied.dropped == 1
    assert applied.drop_observations[0].reason == "not_applied"
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(pending=False, text=object(), transcription_id=1),
        SimpleNamespace(pending=False, text="", transcription_id=1),
        SimpleNamespace(pending=False, text="speech", transcription_id=True),
        SimpleNamespace(pending=False, text="speech", transcription_id="7"),
    ],
)
def test_transcription_apply_marks_malformed_results_terminal(
    db: sqlite3.Connection,
    result: object,
) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice", media_payload="{}")
    job = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    db.commit()

    applied = TranscriptionHydrationHandler(recheck_delay_seconds=30).apply(
        db,
        HydrationQueueRepository(db),
        [job],
        result,
        now=20,
    )

    assert applied.dropped == 1
    assert applied.drop_observations[0].reason == "invalid_result"
    assert db.execute("SELECT terminal FROM hydration_jobs").fetchone() == (1,)


def test_access_loss_marks_dialogs_and_purges_all_active_hydration_jobs(
    db: sqlite3.Connection,
) -> None:
    _seed_dialog(db, 1)
    _seed_dialog(db, 2)
    _seed_message(db, 1, dialog_id=1)
    _seed_message(db, 2, dialog_id=1)
    _seed_message(db, 3, dialog_id=2)
    first = _enqueue(db, MEDIA_METADATA_KIND, 1, dialog_id=1, attempts=1)
    second = _enqueue(db, MEDIA_METADATA_KIND, 2, dialog_id=1, attempts=2)
    third = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 3, dialog_id=2, attempts=1)
    db.commit()
    worker = MessageFactHydrationWorker(
        object(),
        db,
        asyncio.Event(),
        handlers=(),
        interval_seconds=1,
        max_requests_per_cycle=0,
        max_jobs_per_cycle=0,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
    )

    outcome = worker._handle_access_lost(
        cast(HydrationHandler, SimpleNamespace(kind=MEDIA_METADATA_KIND)),
        [first, second, third],
        [first, second, third],
        (
            HydrationDropObservation("ineligible", 99, TRANSCRIPTION_HYDRATION_KIND, 2, 0),
            HydrationDropObservation("ineligible", 100, None, 2, 0),
        ),
        ChatForbiddenError(request=None),
        20,
    )

    assert outcome.dropped == 5
    assert dict(outcome.dropped_by_kind) == {
        MEDIA_METADATA_KIND: 2,
        TRANSCRIPTION_HYDRATION_KIND: 2,
    }
    assert db.execute("SELECT dialog_id, status, access_lost_at FROM synced_dialogs ORDER BY dialog_id").fetchall() == [
        (1, "access_lost", 20),
        (2, "access_lost", 20),
    ]
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs WHERE terminal = 0").fetchone() == (0,)
