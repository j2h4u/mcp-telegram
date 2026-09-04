from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import telethon.tl.types as tl  # type: ignore[import-untyped]
from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.errors.rpcbaseerrors import BadRequestError  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import (  # type: ignore[import-untyped]
    MessageIdInvalidError,
    MsgIdInvalidError,
    PremiumAccountRequiredError,
)

from mcp_telegram.access_lifecycle import restore_access_after_revalidation, set_access_lost
from mcp_telegram.config import FactHydrationConfig
from mcp_telegram.fact_hydration import MessageFactHydrationWorker
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.history_enrollment import disable_history, enable_history
from mcp_telegram.hydration_queue import HydrationPriority, HydrationQueueRepository
from mcp_telegram.media_hydration import MediaFactHydrationHandler
from mcp_telegram.message_contracts import ExtractedMessage, StoredMessage
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.transcription_hydration import TranscriptionHydrationHandler


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

    async def get_input_entity(self, dialog_id: int) -> object:
        self.calls.append({"dialog_id": dialog_id})
        return SimpleNamespace(dialog_id=dialog_id)

    async def __call__(self, request: object, **kwargs: object) -> object:
        self.calls.append({"request": request, **kwargs})
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


def _seed_voice(conn: sqlite3.Connection, dialog_id: int = 1, message_id: int = 1) -> None:
    conn.execute("INSERT OR IGNORE INTO synced_dialogs(dialog_id, status) VALUES (?, 'synced')", (dialog_id,))
    conn.execute(
        "INSERT OR IGNORE INTO full_history_enrollment(dialog_id, enabled, source, updated_at) "
        "VALUES (?, 1, 'explicit', 1)",
        (dialog_id,),
    )
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (?, ?, 1, NULL, 'voice', '{}')",
        (dialog_id, message_id),
    )
    conn.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at) "
        "VALUES ('transcription', ?, ?, 1, 0, 0, 1)",
        (dialog_id, message_id),
    )
    conn.commit()


def _seed_round_video(conn: sqlite3.Connection, dialog_id: int = 1, message_id: int = 1) -> None:
    conn.execute("INSERT OR IGNORE INTO synced_dialogs(dialog_id, status) VALUES (?, 'synced')", (dialog_id,))
    conn.execute(
        "INSERT OR IGNORE INTO full_history_enrollment(dialog_id, enabled, source, updated_at) "
        "VALUES (?, 1, 'explicit', 1)",
        (dialog_id,),
    )
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (?, ?, 1, NULL, 'video', '{\"round_message\":true}')",
        (dialog_id, message_id),
    )
    conn.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at) "
        "VALUES ('transcription', ?, ?, 1, 0, 1, 1)",
        (dialog_id, message_id),
    )
    conn.commit()


def _worker(
    conn: sqlite3.Connection, client: _Client, policy: FactHydrationConfig | None = None
) -> MessageFactHydrationWorker:
    config = policy or FactHydrationConfig(pause_between_requests_seconds=0.01)
    return MessageFactHydrationWorker(
        client,
        conn,
        asyncio.Event(),
        handlers=(MediaFactHydrationHandler(batch_size=config.batch_size),),
        interval_seconds=config.interval_seconds,
        max_requests_per_cycle=config.max_requests_per_cycle,
        max_jobs_per_cycle=config.max_jobs_per_cycle,
        pause_between_requests_seconds=config.pause_between_requests_seconds,
        retry_delay_seconds=config.retry_delay_seconds,
        circuit_retry_seconds=config.circuit_retry_seconds,
        max_attempts=config.max_attempts,
    )


def _transcription_worker(
    conn: sqlite3.Connection, client: _Client, policy: FactHydrationConfig | None = None
) -> MessageFactHydrationWorker:
    config = policy or FactHydrationConfig(pause_between_requests_seconds=0.01)
    return MessageFactHydrationWorker(
        client,
        conn,
        asyncio.Event(),
        handlers=(TranscriptionHydrationHandler(recheck_delay_seconds=config.transcription_recheck_delay_seconds),),
        interval_seconds=config.interval_seconds,
        max_requests_per_cycle=config.max_requests_per_cycle,
        max_jobs_per_cycle=config.max_jobs_per_cycle,
        retry_delay_seconds=config.retry_delay_seconds,
        circuit_retry_seconds=config.circuit_retry_seconds,
        max_attempts=config.max_attempts,
        pause_between_requests_seconds=config.pause_between_requests_seconds,
    )


def _mixed_worker(conn: sqlite3.Connection, client: _Client, policy: FactHydrationConfig) -> MessageFactHydrationWorker:
    return MessageFactHydrationWorker(
        client,
        conn,
        asyncio.Event(),
        handlers=(
            MediaFactHydrationHandler(batch_size=policy.batch_size),
            TranscriptionHydrationHandler(recheck_delay_seconds=policy.transcription_recheck_delay_seconds),
        ),
        interval_seconds=policy.interval_seconds,
        max_requests_per_cycle=policy.max_requests_per_cycle,
        max_jobs_per_cycle=policy.max_jobs_per_cycle,
        pause_between_requests_seconds=policy.pause_between_requests_seconds,
        retry_delay_seconds=policy.retry_delay_seconds,
        circuit_retry_seconds=policy.circuit_retry_seconds,
        max_attempts=policy.max_attempts,
    )


def _hydration_message(dialog_id: int, message_id: int, media_kind: str) -> ExtractedMessage:
    return ExtractedMessage(
        message=StoredMessage(
            dialog_id=dialog_id,
            message_id=message_id,
            sent_at=1,
            text=None,
            sender_id=None,
            sender_first_name=None,
            reply_to_msg_id=None,
            forum_topic_id=None,
            edit_date=None,
            grouped_id=None,
            reply_to_peer_id=None,
            out=0,
            is_service=0,
            post_author=None,
            media_kind=media_kind,
            media_payload="{}",
        ),
        reply_count=0,
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
    policy = FactHydrationConfig(batch_size=2, max_requests_per_cycle=2, pause_between_requests_seconds=0.01)
    await _worker(db, client, policy).run_cycle(now=1)
    assert [call["ids"] for call in client.calls] == [[1, 2], [3, 4]]
    assert db.execute("SELECT message_id FROM hydration_jobs ORDER BY message_id").fetchall() == [(3,), (4,), (5,)]


@pytest.mark.asyncio
async def test_job_and_request_caps_apply_across_multiple_dialogs(db: sqlite3.Connection) -> None:
    """Global cycle caps bound work even when due jobs span several dialogs."""
    for dialog_id in (1, 2):
        for message_id in (1, 2):
            _seed(db, dialog_id=dialog_id, message_id=message_id)
    client = _EchoClient()
    policy = FactHydrationConfig(
        batch_size=1,
        max_jobs_per_cycle=3,
        max_requests_per_cycle=4,
        pause_between_requests_seconds=0.01,
    )

    result = await _worker(db, client, policy).run_cycle(now=1)

    assert result.requests == 3
    assert [call["entity"] for call in client.calls] == [1, 1, 2]
    assert [call["ids"] for call in client.calls] == [[1], [2], [1]]
    assert db.execute("SELECT dialog_id, message_id FROM hydration_jobs ORDER BY dialog_id, message_id").fetchall() == [
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
    policy = FactHydrationConfig(
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
    policy = FactHydrationConfig(
        batch_size=1,
        max_jobs_per_cycle=2,
        max_requests_per_cycle=2,
        pause_between_requests_seconds=0.01,
    )

    await _worker(db, client, policy).run_cycle(now=1)

    assert [(call["entity"], call["ids"]) for call in client.calls] == [(1, [1]), (2, [1])]
    assert db.execute("SELECT dialog_id FROM hydration_jobs").fetchall() == []


@pytest.mark.asyncio
async def test_transient_retries_then_caps_after_durable_attempts(db: sqlite3.Connection) -> None:
    _seed(db)
    client = _Client(error=RuntimeError("opaque"))
    policy = FactHydrationConfig(retry_delay_seconds=10, max_attempts=2, pause_between_requests_seconds=0.01)
    worker = _worker(db, client, policy)
    await worker.run_cycle(now=1)
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 11)
    await worker.run_cycle(now=11)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (1,)
    assert db.execute("SELECT terminal FROM hydration_jobs").fetchone() == (1,)


@pytest.mark.asyncio
async def test_flood_wait_stops_without_sleep_and_reschedules(db: sqlite3.Connection) -> None:
    _seed(db)
    client = _Client(error=TelegramRpcThrottled(retry_after_seconds=7))
    result = await _worker(db, client).run_cycle(now=1)
    assert result.stopped
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 8)


@pytest.mark.asyncio
async def test_flood_wait_uses_owned_retry_duration_without_observation(db: sqlite3.Connection) -> None:
    _seed(db)
    client = _Client(error=TelegramRpcThrottled(retry_after_seconds=7))
    result = await _worker(db, client).run_cycle(now=1)

    assert result.stopped
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 8)


@pytest.mark.asyncio
async def test_circuit_open_stops_without_sleep_and_keeps_jobs_paused(db: sqlite3.Connection) -> None:
    _seed(db)
    client = _Client(error=TelegramRpcThrottled(latched=True, detail="closed"))
    policy = FactHydrationConfig(circuit_retry_seconds=20, pause_between_requests_seconds=0.01)
    result = await _worker(db, client, policy).run_cycle(now=1)
    assert result.stopped
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["flood", "circuit"])
async def test_multi_job_flood_or_circuit_failure_stops_cycle(failure: str, db: sqlite3.Connection) -> None:
    """A finite throttle reschedules the batch; a latched throttle pauses it."""
    for message_id in (1, 2):
        _seed(db, dialog_id=1, message_id=message_id)
    _seed(db, dialog_id=2, message_id=1)
    error: BaseException
    retry_at: int
    if failure == "flood":
        error = TelegramRpcThrottled(retry_after_seconds=7)
        retry_at = 8
    else:
        error = TelegramRpcThrottled(latched=True, detail="closed")
        retry_at = 1
    client = _Client(error=error)
    policy = FactHydrationConfig(
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
async def test_multi_job_missing_response_emits_one_bounded_drop_record(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(db, message_id=1)
    _seed(db, message_id=2)
    client = _Client(response=[])
    policy = FactHydrationConfig(batch_size=2, pause_between_requests_seconds=0.01)

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        result = await _worker(db, client, policy).run_cycle(now=1)

    records = [record for record in caplog.records if "message_fact_hydration_drop" in record.message]
    assert result.dropped == 2
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert "reason=missing_response" in records[0].message
    assert "job_count=2" in records[0].message
    assert "message_ids=(1, 2)" in records[0].message


@pytest.mark.asyncio
async def test_preflight_drop_has_no_rpc_descriptor_when_later_job_fails(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(db, message_id=1)
    _seed(db, message_id=2)
    db.execute("UPDATE messages SET is_deleted = 1 WHERE dialog_id = 1 AND message_id = 1")
    db.commit()
    monkeypatch.setattr(MediaFactHydrationHandler, "is_terminal_error", lambda _self, _exc: True)
    client = _Client(error=BadRequestError(None, "MSG_VOICE_TOO_LONG"))

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        result = await _worker(db, client).run_cycle(now=1)

    records = [record for record in caplog.records if "message_fact_hydration_drop" in record.message]
    assert result.dropped == 2
    assert len(records) == 2
    preflight_args = cast(tuple[object, ...], next(record.args for record in records if "ineligible" in record.message))
    terminal_args = cast(
        tuple[object, ...], next(record.args for record in records if "terminal_rpc" in record.message)
    )
    assert preflight_args[7:] == (None, None, None)
    assert terminal_args[7:] == ("BadRequestError", 400, "MSG_VOICE_TOO_LONG")
    assert all(record.exc_info is None for record in records)


@pytest.mark.asyncio
async def test_invalid_result_drop_is_warning_without_telegram_payload(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_voice(db)
    client = _Client(response=SimpleNamespace(pending=False, text=object(), transcription_id=True))

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        await _transcription_worker(db, client).run_cycle(now=10)

    records = [record for record in caplog.records if "message_fact_hydration_drop" in record.message]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "reason=invalid_result" in records[0].message
    assert "speech" not in records[0].message


@pytest.mark.asyncio
async def test_malicious_rpc_message_is_never_logged(db: sqlite3.Connection, caplog: pytest.LogCaptureFixture) -> None:
    _seed_voice(db)
    secret = "secret=telegram-token\nrequest-payload"
    client = _Client(error=BadRequestError(None, secret))
    policy = FactHydrationConfig(max_attempts=1, pause_between_requests_seconds=0.01)

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        await _transcription_worker(db, client, policy).run_cycle(now=10)

    assert all(secret not in record.message for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_terminal_rpc_drop_is_info_with_safe_descriptor_fields(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_voice(db)
    client = _Client(error=BadRequestError(None, "MSG_VOICE_TOO_LONG"))

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        await _transcription_worker(db, client).run_cycle(now=10)

    records = [record for record in caplog.records if "message_fact_hydration_drop" in record.message]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "reason=terminal_rpc" in records[0].message
    assert "error_type=BadRequestError" in records[0].message
    assert "rpc_code=400" in records[0].message
    assert "rpc_symbol=MSG_VOICE_TOO_LONG" in records[0].message
    assert records[0].exc_info is None


@pytest.mark.asyncio
async def test_attempt_limit_drop_is_warning(db: sqlite3.Connection, caplog: pytest.LogCaptureFixture) -> None:
    _seed_voice(db)
    client = _Client(response=SimpleNamespace(pending=True, text="", transcription_id=7))
    policy = FactHydrationConfig(max_attempts=1, pause_between_requests_seconds=0.01)

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        await _transcription_worker(db, client, policy).run_cycle(now=10)

    records = [record for record in caplog.records if "message_fact_hydration_drop" in record.message]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "reason=attempt_limit" in records[0].message


@pytest.mark.asyncio
async def test_access_loss_purges_and_restore_reenqueues_unresolved_jobs(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(db)
    db.execute(
        "INSERT INTO dialogs(dialog_id, hidden, needs_refresh, snapshot_at, archived, pinned, unread_mentions_count, unread_reactions_count) VALUES (1, 0, 0, 1, 0, 0, 0, 0)"
    )
    db.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (1, 2, 2, NULL, 'voice', '{}')"
    )
    db.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload, is_deleted) "
        "VALUES (1, 3, 3, NULL, 'voice', '{}', 1)"
    )
    db.commit()
    monkeypatch.setattr("mcp_telegram.fact_hydration.ACCESS_LOST_ERRORS", (RuntimeError,))
    client = _Client(error=RuntimeError("private"))
    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        await _worker(db, client).run_cycle(now=10)
    records = [record for record in caplog.records if "message_fact_hydration_drop" in record.message]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "reason=access_lost" in records[0].message
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    restore_access_after_revalidation(db, 1, 20)
    assert db.execute(
        "SELECT kind, dialog_id, message_id, due_at, attempts FROM hydration_jobs ORDER BY kind"
    ).fetchall() == [
        ("media_metadata", 1, 1, 20, 0),
        ("transcription", 1, 2, 20, 0),
    ]


@pytest.mark.asyncio
async def test_terminal_transcription_survives_reconcile_and_repair_until_fact_invalidation(
    db: sqlite3.Connection,
) -> None:
    _seed_voice(db)
    db.execute("UPDATE hydration_jobs SET terminal = 1, attempts = 3 WHERE kind = 'transcription'")
    db.commit()

    set_access_lost(db, 1, 10)
    with db:
        insert_messages_with_fts(
            db,
            [_hydration_message(1, 1, "voice")],
            priority=HydrationPriority.BACKFILL,
        )
    restore_access_after_revalidation(db, 1, 20)

    client = _Client(SimpleNamespace(pending=False, text="speech words", transcription_id=7))
    result = await _transcription_worker(db, client).run_cycle(now=21)

    assert result.requests == 0
    assert client.calls == []
    assert db.execute("SELECT attempts, terminal FROM hydration_jobs WHERE kind = 'transcription'").fetchone() == (3, 1)

    with db:
        insert_messages_with_fts(
            db,
            [_hydration_message(1, 1, "other")],
            priority=HydrationPriority.BACKFILL,
        )
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind = 'transcription'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_disable_reconcile_reenable_restart_repair_preserves_terminal_tombstone(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        _seed_voice(conn)
        conn.execute("UPDATE hydration_jobs SET terminal = 1, attempts = 3 WHERE kind = 'transcription'")
        conn.commit()
        disable_history(conn, 1, now=10)
        with conn:
            insert_messages_with_fts(conn, [_hydration_message(1, 1, "voice")], priority=HydrationPriority.BACKFILL)
        enable_history(conn, 1, now=20)
        conn.commit()
        conn.close()

        conn = _open_sync_db(path)
        client = _Client(SimpleNamespace(pending=False, text="speech words", transcription_id=7))
        result = await _transcription_worker(conn, client).run_cycle(now=21)

        assert result.requests == 0
        assert client.calls == []
        assert conn.execute(
            "SELECT attempts, terminal FROM hydration_jobs WHERE kind = 'transcription'"
        ).fetchone() == (3, 1)
    finally:
        conn.close()


def test_disable_history_purges_active_jobs_but_preserves_terminal_suppression(db: sqlite3.Connection) -> None:
    _seed_voice(db)
    db.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, terminal) "
        "VALUES ('transcription', 1, 2, 1, 3, 1)"
    )
    db.commit()

    disable_history(db, 1, now=10)

    assert db.execute("SELECT dialog_id, message_id, terminal FROM hydration_jobs").fetchall() == [(1, 2, 1)]


@pytest.mark.asyncio
async def test_access_loss_logs_all_kinds_with_bounded_queue_summary(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for message_id in range(1, 36):
        _seed(db, message_id=message_id)
    _seed_voice(db, message_id=100)
    db.execute("UPDATE hydration_jobs SET due_at = 999 WHERE dialog_id = 1 AND message_id > 1")
    db.commit()
    monkeypatch.setattr("mcp_telegram.fact_hydration.ACCESS_LOST_ERRORS", (RuntimeError,))
    client = _Client(error=RuntimeError("private"))
    policy = FactHydrationConfig(max_jobs_per_cycle=100, max_requests_per_cycle=10, pause_between_requests_seconds=0.01)

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        result = await _mixed_worker(db, client, policy).run_cycle(now=1)

    records = [record for record in caplog.records if "message_fact_hydration_drop" in record.message]
    assert result.dropped == 36
    assert len(records) == 2
    by_kind: dict[str, tuple[object, ...]] = {}
    for record in records:
        args = cast(tuple[object, ...], record.args)
        by_kind[cast(str, args[1])] = args
    media_args = by_kind["media_metadata"]
    transcription_args = by_kind["transcription"]
    assert media_args[3:6] == (35, tuple(range(1, 33)), 0)
    assert transcription_args[3:6] == (1, (100,), 0)
    assert all(cast(tuple[object, ...], record.args)[7:] == ("RuntimeError", None, None) for record in records)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


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


@pytest.mark.asyncio
async def test_transcription_immediate_final_persists_text_and_fts(db: sqlite3.Connection) -> None:
    _seed_voice(db)
    client = _Client(SimpleNamespace(pending=False, text="speech words", transcription_id=7))

    result = await _transcription_worker(db, client).run_cycle(now=10)

    assert result.hydrated == 1
    assert result.completed == 1
    assert result.requests == 2
    assert ["dialog_id" in call for call in client.calls] == [True, False]
    assert db.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=1").fetchone() == ("speech words",)
    assert db.execute("SELECT text, transcription_id FROM message_transcriptions").fetchone() == ("speech words", 7)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    assert db.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=1 AND message_id=1").fetchone() == (
        "speech words",
    )


@pytest.mark.asyncio
async def test_two_voice_jobs_are_scalar_and_sequential_with_budget_four(db: sqlite3.Connection) -> None:
    _seed_voice(db, message_id=1)
    _seed_voice(db, message_id=2)
    client = _Client(SimpleNamespace(pending=False, text="speech words", transcription_id=7))
    policy = FactHydrationConfig(max_requests_per_cycle=4, pause_between_requests_seconds=0.01)

    result = await _transcription_worker(db, client, policy).run_cycle(now=10)

    assert result.requests == 4
    assert result.completed == 2
    assert ["dialog_id" in call for call in client.calls] == [True, False, True, False]
    assert all(
        not isinstance(call["request"], (list, tuple, set, dict, range)) for call in client.calls if "request" in call
    )


@pytest.mark.asyncio
async def test_voice_foreground_beats_large_media_backlog(db: sqlite3.Connection) -> None:
    for message_id in range(1, 9):
        _seed(db, dialog_id=1, message_id=message_id)
        db.execute(
            "DELETE FROM hydration_jobs WHERE kind = 'media_metadata' AND dialog_id = 1 AND message_id = ?",
            (message_id,),
        )
        with db:
            insert_messages_with_fts(
                db,
                [_hydration_message(1, message_id, "other")],
                priority=HydrationPriority.FOREGROUND,
            )

    _seed_voice(db, dialog_id=2)
    db.execute("DELETE FROM hydration_jobs WHERE kind = 'transcription' AND dialog_id = 2")
    with db:
        insert_messages_with_fts(
            db,
            [_hydration_message(2, 1, "voice")],
            priority=HydrationPriority.FOREGROUND,
        )
    db.execute("UPDATE hydration_jobs SET due_at = 1")
    db.commit()

    media_priorities = db.execute(
        "SELECT priority FROM hydration_jobs WHERE kind = 'media_metadata' ORDER BY message_id"
    ).fetchall()
    assert media_priorities == [(int(HydrationPriority.BACKFILL),)] * 8
    assert db.execute("SELECT priority FROM hydration_jobs WHERE kind = 'transcription'").fetchone() == (
        int(HydrationPriority.FOREGROUND),
    )
    assert HydrationQueueRepository(db).due_jobs(1, 100)[0].kind == "transcription"

    client = _Client(SimpleNamespace(pending=False, text="speech words", transcription_id=7))
    policy = FactHydrationConfig(
        batch_size=1,
        max_jobs_per_cycle=100,
        max_requests_per_cycle=4,
        pause_between_requests_seconds=0.01,
    )
    result = await _mixed_worker(db, client, policy).run_cycle(now=1)

    assert result.completed == 1
    assert ["dialog_id" in call for call in client.calls] == [True, False, False, False]
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind = 'media_metadata'").fetchone() == (8,)


@pytest.mark.asyncio
async def test_foreground_batches_exhaust_before_backfill_floodwait(
    db: sqlite3.Connection,
) -> None:
    _seed(db, message_id=1)
    _seed(db, message_id=2)
    db.execute("UPDATE hydration_jobs SET priority = 1")
    _seed_voice(db, dialog_id=2)
    db.execute("UPDATE hydration_jobs SET priority = 0 WHERE kind = 'transcription'")
    db.commit()

    class _ForegroundThenFloodClient(_Client):
        async def get_messages(self, *_args: object, **kwargs: object) -> object:
            self.calls.append(kwargs)
            ids = kwargs.get("ids")
            assert isinstance(ids, list)
            return [SimpleNamespace(id=message_id, media=None) for message_id in ids]

        async def __call__(self, request: object, **kwargs: object) -> object:
            self.calls.append({"request": request, **kwargs})
            raise TelegramRpcThrottled(retry_after_seconds=7)

    client = _ForegroundThenFloodClient()
    policy = FactHydrationConfig(
        batch_size=1,
        max_jobs_per_cycle=3,
        max_requests_per_cycle=5,
        pause_between_requests_seconds=0.01,
    )
    result = await _mixed_worker(db, client, policy).run_cycle(now=1)

    assert result.stopped is True
    assert ["ids" in call for call in client.calls] == [True, True, False, False]
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind = 'media_metadata'").fetchone() == (0,)
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs WHERE kind = 'transcription'").fetchone() == (1, 8)


@pytest.mark.asyncio
async def test_early_stop_logs_later_kind_as_selected_without_request(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(db, message_id=1)
    db.execute("UPDATE hydration_jobs SET priority = 1")
    _seed_voice(db, dialog_id=2)
    db.commit()
    client = _Client(error=TelegramRpcThrottled(retry_after_seconds=7))
    policy = FactHydrationConfig(
        batch_size=1, max_jobs_per_cycle=2, max_requests_per_cycle=3, pause_between_requests_seconds=0.01
    )

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        result = await _mixed_worker(db, client, policy).run_cycle(now=1)

    assert result.stopped is True
    transcription_logs = [record.message for record in caplog.records if "kind=transcription" in record.message]
    assert transcription_logs
    assert all(record.levelno == logging.DEBUG for record in caplog.records if "kind=transcription" in record.message)
    assert "selected=1" in transcription_logs[-1]
    assert "requests=0" in transcription_logs[-1]
    assert "queue_active=1" in transcription_logs[-1]
    assert "queue_ready=1" in transcription_logs[-1]
    assert "queue_deferred=0" in transcription_logs[-1]
    cycle_logs = [record.message for record in caplog.records if "message_fact_hydration cycle" in record.message]
    assert cycle_logs
    assert "queue_active=2" in cycle_logs[-1]
    assert "queue_ready=1" in cycle_logs[-1]
    assert "queue_deferred=1" in cycle_logs[-1]
    assert "queue_terminal=0" in cycle_logs[-1]


@pytest.mark.asyncio
async def test_same_tier_skew_still_runs_both_kinds_with_weighted_budget(db: sqlite3.Connection) -> None:
    for message_id in range(1, 302):
        _seed(db, message_id=message_id)
    _seed_voice(db, dialog_id=2)
    db.commit()

    class _WeightedClient(_Client):
        async def get_messages(self, *_args: object, **kwargs: object) -> object:
            self.calls.append(kwargs)
            ids = kwargs.get("ids")
            assert isinstance(ids, list) and len(ids) == 1
            return [SimpleNamespace(id=ids[0], media=None)]

    client = _WeightedClient(SimpleNamespace(pending=False, text="speech words", transcription_id=7))
    policy = FactHydrationConfig(
        batch_size=1, max_jobs_per_cycle=2, max_requests_per_cycle=3, pause_between_requests_seconds=0.01
    )
    result = await _mixed_worker(db, client, policy).run_cycle(now=1)

    assert result.requests == 3
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind = 'transcription'").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind = 'media_metadata'").fetchone() == (300,)


def test_worker_rejects_duplicate_handler_kinds(db: sqlite3.Connection) -> None:
    config = FactHydrationConfig(max_jobs_per_cycle=2, max_requests_per_cycle=2, pause_between_requests_seconds=0.01)
    with pytest.raises(ValueError, match="handler kind must be unique"):
        MessageFactHydrationWorker(
            _Client(),
            db,
            asyncio.Event(),
            handlers=(MediaFactHydrationHandler(batch_size=1), MediaFactHydrationHandler(batch_size=1)),
            interval_seconds=config.interval_seconds,
            max_requests_per_cycle=config.max_requests_per_cycle,
            max_jobs_per_cycle=config.max_jobs_per_cycle,
            retry_delay_seconds=config.retry_delay_seconds,
            circuit_retry_seconds=config.circuit_retry_seconds,
            max_attempts=config.max_attempts,
            pause_between_requests_seconds=config.pause_between_requests_seconds,
        )


@pytest.mark.asyncio
async def test_voice_jobs_request_configured_pause_without_waiting(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_voice(db, message_id=1)
    _seed_voice(db, message_id=2)
    requested_pauses: list[float] = []

    async def capture_pause(worker: MessageFactHydrationWorker) -> bool:
        requested_pauses.append(worker._pause_between_requests_seconds)
        return False

    monkeypatch.setattr(MessageFactHydrationWorker, "_pause_between_requests", capture_pause)
    client = _Client(SimpleNamespace(pending=False, text="speech words", transcription_id=7))
    policy = FactHydrationConfig(max_requests_per_cycle=4, pause_between_requests_seconds=5.0)

    result = await _transcription_worker(db, client, policy).run_cycle(now=10)

    assert result.completed == 2
    assert requested_pauses == [5.0]


@pytest.mark.asyncio
async def test_transcription_pending_is_low_frequency_reschedule_without_text(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_voice(db)
    client = _Client(SimpleNamespace(pending=True, text="", transcription_id=7))
    policy = FactHydrationConfig(transcription_recheck_delay_seconds=123, pause_between_requests_seconds=0.01)

    with caplog.at_level(logging.INFO, logger="mcp_telegram.fact_hydration"):
        result = await _transcription_worker(db, client, policy).run_cycle(now=10)

    assert result.pending == 1
    assert result.retried == 0
    cycle_log = next(record.message for record in caplog.records if "message_fact_hydration cycle" in record.message)
    assert "selected=1" in cycle_log
    assert "pending=1" in cycle_log
    assert "retried=0" in cycle_log
    assert "queue_ready=0" in cycle_log
    assert "queue_deferred=1" in cycle_log
    assert "queue_outcomes=telegram_pending:1" in cycle_log
    assert db.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=1").fetchone() == (None,)
    assert db.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == (0,)
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 133)
    assert db.execute("SELECT last_outcome, last_error_code FROM hydration_jobs").fetchone() == (
        "telegram_pending",
        None,
    )


@pytest.mark.asyncio
async def test_round_video_transcription_persists_text_fts_fact_and_removes_job(db: sqlite3.Connection) -> None:
    _seed_round_video(db)
    client = _Client(SimpleNamespace(pending=False, text="round speech", transcription_id=7))

    result = await _transcription_worker(db, client).run_cycle(now=10)

    assert result.hydrated == 1
    assert db.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=1").fetchone() == ("round speech",)
    assert db.execute("SELECT text, transcription_id FROM message_transcriptions").fetchone() == ("round speech", 7)
    assert db.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=1 AND message_id=1").fetchone() == (
        "round speech",
    )
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_plain_video_transcription_job_is_dropped_without_rpc(db: sqlite3.Connection) -> None:
    _seed_round_video(db)
    db.execute("UPDATE messages SET media_payload='{}'")
    db.commit()
    client = _Client(SimpleNamespace(pending=False, text="must not be used", transcription_id=7))

    result = await _transcription_worker(db, client).run_cycle(now=10)

    assert result.requests == 0
    assert result.dropped == 1
    assert client.calls == []
    assert db.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=1").fetchone() == (None,)
    assert db.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_repaired_round_video_enqueues_one_backfill_transcription(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
    db.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (1, 1, 'explicit', 1)"
    )
    db.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (1, 1, 123, NULL, 'video', '{\"duration\":5}')"
    )
    db.commit()
    media = tl.MessageMediaDocument(video=True, round=True)
    client = _Client([SimpleNamespace(id=1, media=media)])

    result = await _worker(db, client).run_cycle(now=10)

    assert result.repaired_media_metadata_jobs == 1
    assert db.execute("SELECT kind, priority, message_sent_at FROM hydration_jobs").fetchall() == [
        ("transcription", int(HydrationPriority.BACKFILL), 123)
    ]


@pytest.mark.asyncio
async def test_repaired_plain_video_does_not_enqueue_transcription(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
    db.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (1, 1, 'explicit', 1)"
    )
    db.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (1, 1, 123, NULL, 'video', '{\"duration\":5}')"
    )
    db.commit()
    media = tl.MessageMediaDocument(video=True, round=False)
    client = _Client([SimpleNamespace(id=1, media=media)])

    await _worker(db, client).run_cycle(now=10)

    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_transcription_retry_after_missed_update_can_complete(db: sqlite3.Connection) -> None:
    _seed_voice(db)
    client = _Client(SimpleNamespace(pending=True, text="", transcription_id=7))
    policy = FactHydrationConfig(transcription_recheck_delay_seconds=10, pause_between_requests_seconds=0.01)
    worker = _transcription_worker(db, client, policy)
    await worker.run_cycle(now=10)
    client.response = SimpleNamespace(pending=False, text="event was missed", transcription_id=8)

    result = await worker.run_cycle(now=20)

    assert result.completed == 1
    assert db.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=1").fetchone() == (
        "event was missed",
    )
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_transcription_realtime_event_wins_worker_race(db: sqlite3.Connection) -> None:
    _seed_voice(db)

    class _EventWinsClient(_Client):
        async def __call__(self, request: object, **kwargs: object) -> object:
            self.calls.append({"request": request, **kwargs})
            from mcp_telegram.messages.sqlite_hydration import apply_message_transcription

            apply_message_transcription(
                db,
                1,
                1,
                transcribed_text="realtime fact",
                transcription_id=8,
                received_at=10,
            )
            db.commit()
            return SimpleNamespace(pending=False, text="stale worker result", transcription_id=9)

    result = await _transcription_worker(db, _EventWinsClient()).run_cycle(now=10)

    assert result.completed == 1
    assert result.dropped == 0
    assert db.execute("SELECT text, transcription_id FROM message_transcriptions").fetchone() == (
        "realtime fact",
        8,
    )
    assert db.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=1").fetchone() == ("realtime fact",)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["deleted", "changed"])
async def test_transcription_does_not_project_stale_result_after_message_changes(
    mutation: str, db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_voice(db)

    class _ChangingClient(_Client):
        async def __call__(self, request: object, **kwargs: object) -> object:
            self.calls.append({"request": request, **kwargs})
            if mutation == "deleted":
                db.execute("UPDATE messages SET is_deleted = 1 WHERE dialog_id = 1 AND message_id = 1")
            else:
                db.execute(
                    "UPDATE messages SET media_kind = 'other', media_payload = '{}' WHERE dialog_id = 1 AND message_id = 1"
                )
            db.commit()
            return SimpleNamespace(pending=False, text="stale text", transcription_id=9)

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
        result = await _transcription_worker(db, _ChangingClient()).run_cycle(now=10)

    assert result.dropped == 1
    assert db.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == (0,)
    assert db.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=1").fetchone() == (None,)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    records = [record for record in caplog.records if "message_fact_hydration_drop" in record.message]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert "reason=not_applied" in records[0].message


@pytest.mark.parametrize(
    "symbol",
    [
        "MSG_ID_INVALID",
        "MSG_VOICE_MISSING",
        "MSG_VOICE_TOO_LONG",
        "PEER_ID_INVALID",
        "PREMIUM_ACCOUNT_REQUIRED",
        "TRANSCRIPTION_FAILED",
    ],
)
def test_transcription_documented_permanent_error_symbols_are_terminal(symbol: str) -> None:
    error = RPCError(None, symbol)
    assert TranscriptionHydrationHandler(recheck_delay_seconds=10).is_terminal_error(error)


@pytest.mark.parametrize("error_type", [MsgIdInvalidError, MessageIdInvalidError])
def test_transcription_generated_message_id_errors_are_terminal(error_type: type[RPCError]) -> None:
    error = error_type(None)  # type: ignore[call-arg]

    assert TranscriptionHydrationHandler(recheck_delay_seconds=10).is_terminal_error(error)


def test_transcription_pending_drops_at_attempt_limit(db: sqlite3.Connection) -> None:
    _seed_voice(db)
    client = _Client(SimpleNamespace(pending=True, text="", transcription_id=7))
    policy = FactHydrationConfig(
        max_attempts=1, transcription_recheck_delay_seconds=123, pause_between_requests_seconds=0.01
    )

    result = asyncio.run(_transcription_worker(db, client, policy).run_cycle(now=10))

    assert result.retried == 0
    assert result.dropped == 1
    assert db.execute("SELECT terminal FROM hydration_jobs").fetchone() == (1,)


@pytest.mark.asyncio
async def test_transcription_permanent_error_is_terminal_without_churn(db: sqlite3.Connection) -> None:
    _seed_voice(db)
    client = _Client(error=PremiumAccountRequiredError(request=None))
    result = await _transcription_worker(db, client).run_cycle(now=10)

    assert result.dropped == 1
    assert db.execute("SELECT terminal FROM hydration_jobs").fetchone() == (1,)


@pytest.mark.asyncio
async def test_transcription_floodwait_stops_and_reschedules(db: sqlite3.Connection) -> None:
    _seed_voice(db)
    client = _Client(error=TelegramRpcThrottled(retry_after_seconds=7))

    result = await _transcription_worker(db, client).run_cycle(now=10)

    assert result.stopped
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 17)


@pytest.mark.asyncio
async def test_transcription_circuit_open_stops_and_keeps_job_paused(db: sqlite3.Connection) -> None:
    _seed_voice(db)
    client = _Client(error=TelegramRpcThrottled(latched=True, detail="open"))
    policy = FactHydrationConfig(circuit_retry_seconds=31, pause_between_requests_seconds=0.01)

    result = await _transcription_worker(db, client, policy).run_cycle(now=10)

    assert result.stopped
    assert db.execute("SELECT attempts, due_at FROM hydration_jobs").fetchone() == (1, 1)


@pytest.mark.asyncio
async def test_transcription_backfill_is_newest_first_and_rpc_budgeted(db: sqlite3.Connection) -> None:
    _seed_voice(db, message_id=1)
    _seed_voice(db, message_id=2)
    db.execute("UPDATE messages SET sent_at = 100 WHERE message_id = 1")
    db.execute("UPDATE messages SET sent_at = 200 WHERE message_id = 2")
    db.execute("UPDATE hydration_jobs SET message_sent_at = 100 WHERE message_id = 1")
    db.execute("UPDATE hydration_jobs SET message_sent_at = 200 WHERE message_id = 2")
    db.commit()
    client = _Client(SimpleNamespace(pending=False, text="newest", transcription_id=9))
    policy = FactHydrationConfig(max_requests_per_cycle=2, max_jobs_per_cycle=2, pause_between_requests_seconds=0.01)

    result = await _transcription_worker(db, client, policy).run_cycle(now=10)

    assert result.completed == 1
    assert db.execute("SELECT message_id FROM hydration_jobs").fetchall() == [(1,)]
    assert db.execute("SELECT text FROM messages WHERE message_id=2").fetchone() == ("newest",)
