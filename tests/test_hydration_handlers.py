from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from telethon.errors import RPCError
from telethon.errors.rpcerrorlist import ChatForbiddenError  # type: ignore[import-untyped]
from telethon.tl import types

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.fact_hydration import (
    FactHydrationDemandAdapter,
    HydrationDropObservation,
    HydrationHandler,
    MediaPublication,
    MessageFactHydrationWorker,
)
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.hydration_queue import (
    MEDIA_METADATA_KIND,
    RICH_COMPOSITION_KIND,
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationJob,
    HydrationPriority,
    HydrationQueueRepository,
)
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.media_hydration import MediaFactHydrationHandler, RichCompositionHydrationHandler
from mcp_telegram.messages.sqlite_bundle import mark_message_deleted, persist_received_messages
from mcp_telegram.messages.sqlite_hydration import apply_message_transcription
from mcp_telegram.messages.sqlite_hydration_jobs import repair_rich_composition_hydration_jobs
from mcp_telegram.messages.telegram_adapter import extract_received_messages
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.peer_access import current_peer_target
from mcp_telegram.reactions.contracts import ReactionAggregateSource
from mcp_telegram.reactions.persistence import allocate_observation_boundary
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcReadTimeoutError
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    current_rpc_scope,
    rpc_scope,
)
from mcp_telegram.transcription_hydration import TranscriptionHydrationHandler
from tests.sqlite_writer_helpers import inline_writer


class _ScopeClient:
    def __init__(self) -> None:
        self.cell = ScopeCell()
        assert self.cell.publish(TelegramAuthScope(1, 42, 2, 99))

    def capture_image_scope(self) -> ScopeToken | None:
        return self.cell.capture()

    def claim_image_scope(self, token: ScopeToken) -> bool:
        return self.cell.claim(token)


def _received_message(message_id: int, *, media: types.MessageMediaUnsupported | None = None) -> types.Message:
    return types.Message(
        id=message_id, peer_id=types.PeerUser(1), date=datetime.fromtimestamp(1, UTC), message="", media=media
    )


def _publication(conn: sqlite3.Connection, response: types.Message) -> MediaPublication:
    client = _ScopeClient()
    scope = client.capture_image_scope()
    assert scope is not None
    order = allocate_observation_order(conn)
    boundary = allocate_observation_boundary(conn, ReactionAggregateSource.HISTORY)
    accepted = persist_received_messages(
        conn,
        extract_received_messages([response]),
        scope=scope,
        claim_scope=client.claim_image_scope,
        observation_order=order,
        reaction_boundary=boundary,
    )
    return MediaPublication(response, frozenset({response.id}) if accepted else frozenset())


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _seed_dialog(conn: sqlite3.Connection, dialog_id: int = 1, *, status: str = "synced") -> None:
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, ?)", (dialog_id, status))
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, updated_at) VALUES (?, 1, 1)",
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
    conn.commit()
    with write_transaction(conn):
        HydrationQueueRepository(conn).enqueue(job)
    return job


def test_rich_repair_resumes_bounded_scan_and_skips_ineligible(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    for message_id in range(1, 5):
        _seed_message(db, message_id)
    db.execute("UPDATE messages SET is_deleted=1 WHERE message_id=2")
    db.commit()
    with write_transaction(db):
        assert repair_rich_composition_hydration_jobs(db, due_at=10, max_jobs=1)
    assert db.execute("SELECT message_id FROM hydration_jobs WHERE kind=?", (RICH_COMPOSITION_KIND,)).fetchall() == [
        (4,)
    ]
    with write_transaction(db):
        assert repair_rich_composition_hydration_jobs(db, due_at=10, max_jobs=1)
    assert db.execute(
        "SELECT message_id FROM hydration_jobs WHERE kind=? ORDER BY message_id", (RICH_COMPOSITION_KIND,)
    ).fetchall() == [(3,), (4,)]
    with write_transaction(db):
        assert not repair_rich_composition_hydration_jobs(db, due_at=10, max_jobs=1)
    assert db.execute(
        "SELECT message_id FROM hydration_jobs WHERE kind=? ORDER BY message_id", (RICH_COMPOSITION_KIND,)
    ).fetchall() == [(1,), (3,), (4,)]


def test_rich_repair_rejects_invalid_saved_cursor(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO daemon_state(key,value) VALUES('rich_composition_hydration_cursor',?)",
        ('{"cursor":false,"done":false}',),
    )
    db.commit()
    with pytest.raises(ValueError, match="invalid rich composition repair cursor"), write_transaction(db):
        repair_rich_composition_hydration_jobs(db, due_at=10, max_jobs=1)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_rich_partial_fetch_uses_full_message_response() -> None:
    partial = SimpleNamespace(id=1, peer_id=types.PeerUser(1), rich_message=SimpleNamespace(part=True))
    full = SimpleNamespace(id=1, peer_id=types.PeerUser(1), rich_message=SimpleNamespace(part=False))

    class Client:
        async def get_messages(self, **kwargs: object) -> object:
            return [partial]

        async def get_input_entity(self, dialog_id: int) -> object:
            return types.InputPeerSelf()

        async def __call__(self, request: object, **kwargs: object) -> object:
            return SimpleNamespace(messages=[full])

    job = HydrationJob(RICH_COMPOSITION_KIND, 1, 1, due_at=1, attempts=0)
    assert await RichCompositionHydrationHandler(batch_size=1).request(Client(), [job]) == [full]


@pytest.mark.asyncio
async def test_rich_expansion_rejects_another_peer() -> None:
    partial = SimpleNamespace(id=1, peer_id=types.PeerUser(2), rich_message=SimpleNamespace(part=True))

    class Client:
        async def get_messages(self, **kwargs: object) -> object:
            return [partial]

    job = HydrationJob(RICH_COMPOSITION_KIND, 1, 1, due_at=1, attempts=0)
    with pytest.raises(ValueError, match="different peer"):
        await RichCompositionHydrationHandler(batch_size=1).request(Client(), [job])


def test_ingested_partial_rich_message_enqueues_existing_hydration_owner(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    db.commit()
    message = _received_message(7)
    message.rich_message = types.RichMessage(blocks=[], photos=[], documents=[], part=True)
    with write_transaction(db):
        assert _publication(db, message).accepted_message_ids == frozenset({7})
    row = cast(
        tuple[int] | None,
        db.execute(
            "SELECT terminal FROM hydration_jobs WHERE kind=? AND dialog_id=1 AND message_id=7",
            (RICH_COMPOSITION_KIND,),
        ).fetchone(),
    )
    assert row == (0,)


@pytest.mark.asyncio
async def test_partial_rich_owner_publishes_full_response_to_canonical_message(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    db.commit()
    partial = _received_message(7)
    partial.rich_message = types.RichMessage(blocks=[], photos=[], documents=[], part=True)
    with write_transaction(db):
        _publication(db, partial)
    full = _received_message(7)
    full.rich_message = types.RichMessage(
        blocks=[types.PageBlockParagraph(text=types.TextPlain(text="recovered"))], photos=[], documents=[]
    )

    class Client(_ScopeClient):
        async def get_messages(self, **kwargs: object) -> object:
            return [partial]

        async def get_input_entity(self, dialog_id: int) -> object:
            return types.InputPeerSelf()

        async def __call__(self, request: object, **kwargs: object) -> object:
            return SimpleNamespace(messages=[full])

    client = Client()
    handler = RichCompositionHydrationHandler(batch_size=1)
    worker = MessageFactHydrationWorker(
        client,
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=3,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
    )
    job = HydrationQueueRepository(db).due_jobs(9999999999, 1, kind=RICH_COMPOSITION_KIND)[0]

    async def request(_handler: object, jobs: list[HydrationJob], **_kwargs: object) -> object:
        return await handler.request(client, jobs)

    with patch.object(worker, "_request_batch", side_effect=request):
        outcome = await worker._process_batch(handler, [job], 20)
    assert outcome.completed == 1
    assert db.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=7").fetchone() == ("recovered",)
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind=?", (RICH_COMPOSITION_KIND,)).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [True, False])
async def test_batch_keeps_partial_jobs_for_individual_expansion(db: sqlite3.Connection, partial: bool) -> None:
    _seed_dialog(db)
    for message_id in (1, 2, 3):
        _seed_message(db, message_id)
    db.commit()
    with write_transaction(db):
        for message_id in (1, 2, 3):
            HydrationQueueRepository(db).enqueue(
                HydrationJob(RICH_COMPOSITION_KIND, 1, message_id, 1, 0, 1, HydrationPriority.BACKFILL)
            )
    messages = [_received_message(i) for i in (1, 2, 3)]
    for message in messages[:2]:
        message.rich_message = types.RichMessage(blocks=[], photos=[], documents=[], part=partial)
    calls: list[int] = []

    class Client(_ScopeClient):
        async def get_messages(self, **_kwargs: object) -> object:
            calls.append(1)
            return messages

    client = Client()
    handler = RichCompositionHydrationHandler(batch_size=3)
    worker = MessageFactHydrationWorker(
        client,
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=3,
        max_jobs_per_cycle=3,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
        clock=lambda: 100.0,
    )
    adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL, account_id=42)
    status = adapter.status(100)
    assert status is not None and status.selected is not None

    async def request(_handler: object, jobs: list[HydrationJob], **_kwargs: object) -> object:
        assert len(jobs) == 3
        return await handler.request(client, jobs)

    with patch.object(worker, "_request_batch", side_effect=request):
        await adapter.run_slice(RpcAttemptBudget(limit=3, selected=status.selected))
    assert len(calls) == 1
    rows = db.execute(
        "SELECT message_id FROM hydration_jobs WHERE kind=? ORDER BY message_id", (RICH_COMPOSITION_KIND,)
    )
    assert rows.fetchall() == ([(1,), (2,)] if partial else [])
    if not partial:
        assert db.execute("SELECT count(*) FROM messages WHERE rich_message IS NOT NULL").fetchone() == (3,)
        return
    assert db.execute(
        "SELECT message_id, json_extract(rich_message, '$.part') FROM messages WHERE message_id IN (1,2) ORDER BY message_id"
    ).fetchall() == [(1, 1), (2, 1)]
    assert db.execute(
        "SELECT message_id, attempts FROM hydration_jobs WHERE kind=? ORDER BY message_id", (RICH_COMPOSITION_KIND,)
    ).fetchall() == [(1, 0), (2, 0)]
    selected = adapter.status(100)
    assert selected is not None and selected.selected is not None
    job = adapter._selected_job(selected.selected)
    assert job is not None
    assert worker._select_batch(HydrationPriority.BACKFILL, RpcAttemptBudget(3), job, 100) == [job]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_lost_selected_lease_does_not_inherit_batch_result(db: sqlite3.Connection, terminal: bool) -> None:
    _seed_dialog(db)
    for message_id in (1, 2):
        _seed_message(db, message_id)
    db.commit()
    with write_transaction(db):
        for message_id in (1, 2):
            HydrationQueueRepository(db).enqueue(
                HydrationJob(RICH_COMPOSITION_KIND, 1, message_id, 1, 0, 1, HydrationPriority.BACKFILL)
            )
    handler = RichCompositionHydrationHandler(batch_size=2)
    worker = MessageFactHydrationWorker(
        _ScopeClient(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=3,
        max_jobs_per_cycle=2,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
        clock=lambda: 100.0,
    )
    adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL, account_id=42)
    status = adapter.status(100)
    assert status is not None and status.selected is not None
    selected_id = int(status.selected.key.target.rsplit(":", 1)[1])

    async def request(_handler: object, jobs: list[HydrationJob], **_kwargs: object) -> object:
        assert len(jobs) == 2
        with write_transaction(db):
            db.execute(
                "UPDATE hydration_jobs SET due_at=99 WHERE kind=? AND message_id=?",
                (RICH_COMPOSITION_KIND, selected_id),
            )
        if terminal:
            raise RPCError(request=None, message="MESSAGE_ID_INVALID", code=400)
        return [_received_message(job.message_id) for job in jobs]

    with patch.object(worker, "_request_batch", side_effect=request):
        outcome = await adapter.run_slice(RpcAttemptBudget(3, selected=status.selected))
    assert outcome.kind is SliceOutcomeKind.NO_PROGRESS
    assert db.execute(
        "SELECT due_at, terminal FROM hydration_jobs WHERE kind=? AND message_id=?",
        (RICH_COMPOSITION_KIND, selected_id),
    ).fetchone() == (99, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transient", "flood", "cancel"])
async def test_batch_failure_preserves_all_queued_facts(db: sqlite3.Connection, failure: str) -> None:
    _seed_dialog(db)
    for message_id in (1, 2):
        _seed_message(db, message_id)
    db.commit()
    with write_transaction(db):
        for message_id in (1, 2):
            HydrationQueueRepository(db).enqueue(
                HydrationJob(RICH_COMPOSITION_KIND, 1, message_id, 1, 0, 1, HydrationPriority.BACKFILL)
            )
    handler = RichCompositionHydrationHandler(batch_size=2)
    worker = MessageFactHydrationWorker(
        _ScopeClient(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=3,
        max_jobs_per_cycle=2,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
        clock=lambda: 100.0,
    )
    adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL, account_id=42)
    status = adapter.status(100)
    assert status is not None and status.selected is not None

    async def request(_handler: object, jobs: list[HydrationJob], **_kwargs: object) -> object:
        assert len(jobs) == 2
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "flood":
            raise TelegramRpcThrottled(retry_after_seconds=17)
        raise TimeoutError

    with patch.object(worker, "_request_batch", side_effect=request):
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await adapter.run_slice(RpcAttemptBudget(3, selected=status.selected))
        elif failure == "flood":
            with pytest.raises(TelegramRpcThrottled):
                await adapter.run_slice(RpcAttemptBudget(3, selected=status.selected))
        else:
            await adapter.run_slice(RpcAttemptBudget(3, selected=status.selected))
    assert db.execute("SELECT message_id FROM messages WHERE is_deleted=0 ORDER BY message_id").fetchall() == [
        (1,),
        (2,),
    ]
    assert db.execute("SELECT message_id FROM hydration_jobs WHERE terminal=0 ORDER BY message_id").fetchall() == [
        (1,),
        (2,),
    ]


@pytest.mark.asyncio
async def test_empty_queue_repair_demand_seeds_and_finishes_without_idle_writes(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    _seed_message(db, 1)
    db.commit()
    handler = RichCompositionHydrationHandler(batch_size=1)
    worker = MessageFactHydrationWorker(
        _ScopeClient(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=60,
        max_requests_per_cycle=3,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        clock=lambda: 100.0,
        profile_owner_account_id=42,
    )
    adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL, account_id=42)
    changes = db.total_changes
    status = adapter.status(100)
    assert status is not None and status.selected is not None
    assert status.selected.key.target == "rich-repair"
    assert db.total_changes == changes
    worker.next_repair_at = 200
    with write_transaction(db):
        HydrationQueueRepository(db).enqueue(
            HydrationJob(RICH_COMPOSITION_KIND, 1, 1, 1, 0, 1, HydrationPriority.BACKFILL)
        )
    adapter._next_kind = 1
    assert adapter.status(100).selected.key.target == "rich_composition:1:1"  # type: ignore[union-attr]
    with write_transaction(db):
        db.execute("DELETE FROM hydration_jobs")
    worker.next_repair_at = 100
    await adapter.run_slice(RpcAttemptBudget(limit=3, selected=status.selected))
    assert db.execute("SELECT message_id FROM hydration_jobs WHERE kind=?", (RICH_COMPOSITION_KIND,)).fetchall() == [
        (1,)
    ]
    db.execute("DELETE FROM hydration_jobs")
    db.commit()
    changes = db.total_changes
    assert adapter.status(1000) is None
    assert db.total_changes == changes


@pytest.mark.asyncio
@pytest.mark.parametrize("dialog_id", [42, -42, -1000000000042])
async def test_media_request_carries_dialog_target_not_message_id(dialog_id: int) -> None:
    class Client:
        async def get_messages(self, *, entity: int, ids: list[int]) -> object:
            assert entity == dialog_id
            assert ids == [999, 1000]
            assert current_peer_target() == dialog_id
            return []

    jobs = [
        HydrationJob(MEDIA_METADATA_KIND, dialog_id, message_id, due_at=1, attempts=0) for message_id in (999, 1000)
    ]
    assert await MediaFactHydrationHandler(batch_size=2).request(Client(), jobs) == []
    assert current_peer_target() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [MEDIA_METADATA_KIND, TRANSCRIPTION_HYDRATION_KIND])
@pytest.mark.parametrize("newer", ["edit", "replace", "delete"])
@pytest.mark.parametrize("valid_result", [True, False, "pending"])
async def test_worker_reserves_before_request_and_rejects_late_fact_and_queue_effects(
    db: sqlite3.Connection,
    kind: str,
    newer: str,
    valid_result: bool | str,
) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice" if kind == TRANSCRIPTION_HYDRATION_KIND else "other")
    job = _enqueue(db, kind, 1, attempts=2)
    clock = [100.0]
    handler = (
        TranscriptionHydrationHandler(recheck_delay_seconds=30)
        if kind == TRANSCRIPTION_HYDRATION_KIND
        else MediaFactHydrationHandler(batch_size=1)
    )
    worker = MessageFactHydrationWorker(
        _ScopeClient(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=2,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        clock=lambda: clock[0],
        profile_owner_account_id=42,
    )

    async def request(*args: object, **kwargs: object) -> object:
        clock[0] += 300
        assert db.execute("SELECT value FROM daemon_state WHERE key='canonical_observation_sequence'").fetchone() == (
            "1",
        )
        assert not db.in_transaction
        with write_transaction(db):
            if newer == "delete":
                mark_message_deleted(db, 1, 1, 30)
            else:
                sequence = allocate_observation_order(db)
                db.execute(
                    "INSERT INTO message_observations(dialog_id,message_id,observation_order,is_deleted) VALUES (1, 1, ?, 0)",
                    (sequence,),
                )
                db.execute("UPDATE messages SET text='newer', edit_date=30 WHERE dialog_id=1 AND message_id=1")
                if newer == "replace":
                    queue = HydrationQueueRepository(db)
                    queue.remove(job)
                    queue.enqueue(HydrationJob(kind, 1, 1, due_at=700, attempts=0))
        if not valid_result:
            return None
        if kind == TRANSCRIPTION_HYDRATION_KIND:
            return SimpleNamespace(text="older transcription", pending=valid_result == "pending", transcription_id=5)
        return _received_message(1)

    with patch.object(worker, "_request_batch", side_effect=request):
        outcome = await worker._process_batch(handler, [job], 20)
    assert outcome.hydrated == 0
    assert db.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == (0,)
    if newer == "delete":
        assert db.execute("SELECT is_deleted FROM messages").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    elif newer == "replace":
        assert db.execute("SELECT due_at,attempts,terminal,last_outcome FROM hydration_jobs").fetchone() == (
            700,
            0,
            0,
            "queued",
        )
    else:
        assert db.execute("SELECT text, media_payload FROM messages").fetchone() == ("newer", "{}")
        assert db.execute("SELECT due_at,attempts,terminal,last_outcome FROM hydration_jobs").fetchone() == (
            job.due_at,
            2,
            0,
            "not_applied",
        )
        queue = HydrationQueueRepository(db)
        assert queue.due_jobs(job.due_at - 1, 10) == []
        # The central Telegram gate owns acquisition cooldown; the stale local
        # result must not invent a second hydration retry deadline.
        assert queue.due_jobs(job.due_at, 10)
        if kind == TRANSCRIPTION_HYDRATION_KIND:
            with write_transaction(db):
                assert apply_message_transcription(
                    db, 1, 1, transcribed_text="final words", transcription_id=8, received_at=410
                )
            assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "terminal",
        "transient",
        "flood",
        "admission",
        "saturated",
        "expired",
        "closed",
        "access_lost",
        "latched",
        "undispatched",
        "read_timeout",
    ],
)
async def test_old_request_error_refunds_original_and_preserves_replacement(  # noqa: PLR0915
    db: sqlite3.Connection, failure: str, recreate: bool
) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice")
    job = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    handler = TranscriptionHydrationHandler(recheck_delay_seconds=30)
    worker = MessageFactHydrationWorker(
        object(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=2,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        clock=lambda: 100.0,
    )
    budget = RpcAttemptBudget(limit=2)
    replacement: list[tuple[object, ...]] = []

    async def request(*args: object, **kwargs: object) -> object:
        with write_transaction(db):
            sequence = allocate_observation_order(db)
            db.execute(
                "INSERT INTO message_observations(dialog_id,message_id,observation_order) VALUES (1,1,?)", (sequence,)
            )
            db.execute("UPDATE messages SET text='new voice',edit_date=30 WHERE dialog_id=1 AND message_id=1")
            if recreate:
                queue = HydrationQueueRepository(db)
                queue.remove(job)
                queue.enqueue(HydrationJob(TRANSCRIPTION_HYDRATION_KIND, 1, 1, due_at=73, attempts=2))
        replacement.append(cast(tuple[object, ...], db.execute("SELECT * FROM hydration_jobs").fetchone()))
        if failure != "undispatched":
            budget.debit()
        if failure == "terminal":
            raise RPCError(request=None, message="MSG_VOICE_MISSING", code=400)
        if failure == "read_timeout":
            raise RpcReadTimeoutError
        if failure == "transient":
            raise TimeoutError
        if failure == "flood":
            raise TelegramRpcThrottled(retry_after_seconds=17)
        if failure in {"latched", "undispatched"}:
            raise TelegramRpcThrottled(latched=True)
        if failure == "admission":
            raise TelegramRpcAdmissionDeferred(retry_after_seconds=17)
        if failure == "access_lost":
            raise ChatForbiddenError(request=None)
        error_type = {
            "closed": RpcAdmissionClosedError,
            "expired": RpcAdmissionExpiredError,
            "saturated": RpcAdmissionSaturatedError,
        }[failure]
        with rpc_scope(TelegramRpcSource.FACT_HYDRATION_LIVE):
            raise error_type(current_rpc_scope(), "test admission failure")

    with patch.object(worker, "_request_batch", side_effect=request):
        if failure in {"flood", "latched", "undispatched", "closed"}:
            expected_error = RpcAdmissionClosedError if failure == "closed" else TelegramRpcThrottled
            with pytest.raises(expected_error):
                await worker._process_batch(handler, [job], 20, attempt_budget=budget)
        else:
            result = await worker._process_batch(handler, [job], 20, attempt_budget=budget)
            if failure == "read_timeout":
                assert result.stopped and isinstance(result.deferred_error, RpcReadTimeoutError)
    if recreate:
        assert cast(tuple[object, ...], db.execute("SELECT * FROM hydration_jobs").fetchone()) == replacement[0]
    else:
        assert db.execute("SELECT due_at,attempts,terminal,last_outcome FROM hydration_jobs").fetchone() == (
            job.due_at,
            0,
            0,
            "not_applied",
        )
    assert db.execute("SELECT status FROM synced_dialogs").fetchone() == ("synced",)
    assert not db.in_transaction


@pytest.mark.asyncio
async def test_pending_attempts_remain_bounded_and_final_event_can_complete(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice")
    original = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    handler = TranscriptionHydrationHandler(recheck_delay_seconds=86_400)
    clock = [100.0]
    worker = MessageFactHydrationWorker(
        object(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=2,
        max_jobs_per_cycle=1,
        retry_delay_seconds=21_600,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        clock=lambda: clock[0],
    )
    queue = HydrationQueueRepository(db)
    with patch.object(worker, "_request_batch", return_value=SimpleNamespace(pending=True)) as request:
        for attempt in range(1, 4):
            job = queue.due_jobs(int(clock[0]), 1)[0]
            await worker._process_batch(handler, [job], int(clock[0]))
            assert db.execute("SELECT attempts,terminal FROM hydration_jobs").fetchone() == (attempt, int(attempt == 3))
            with write_transaction(db):
                queue.enqueue(original)
            assert queue.due_jobs(int(clock[0]), 1) == []
            clock[0] += 86_400
        assert request.call_count == 3
    assert db.execute("SELECT last_outcome FROM hydration_jobs").fetchone() == ("exhausted",)
    with write_transaction(db):
        assert apply_message_transcription(
            db, 1, 1, transcribed_text="final speech", transcription_id=4, received_at=int(clock[0])
        )
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


def test_media_apply_maps_single_result_and_persists_empty_and_unknown_facts(
    db: sqlite3.Connection,
) -> None:
    _seed_dialog(db)
    _seed_message(db, 1)
    _seed_message(db, 2)
    first = _enqueue(db, MEDIA_METADATA_KIND, 1)
    second = _enqueue(db, MEDIA_METADATA_KIND, 2)
    db.commit()

    db.commit()
    with write_transaction(db):
        applied = MediaFactHydrationHandler(batch_size=2).apply(
            db,
            HydrationQueueRepository(db),
            [first, second],
            _publication(db, _received_message(1, media=types.MessageMediaUnsupported())),
            now=20,
        )

    assert applied.hydrated == 1
    assert applied.completed == 1
    assert applied.dropped == 1
    assert applied.drop_observations[0].reason == "missing_response"
    assert db.execute("SELECT media_kind, media_payload FROM messages WHERE message_id = 1").fetchone() == (
        "other",
        '{"type":"MessageMediaUnsupported"}',
    )
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

    db.commit()
    with write_transaction(db):
        missing = handler.apply(
            db,
            queue,
            [first],
            MediaPublication([_received_message(99)], frozenset()),
            now=20,
        )
    db.commit()
    with write_transaction(db):
        invalid = handler.apply(db, queue, [second], MediaPublication(123, frozenset()), now=20)

    assert missing.dropped == 1
    assert missing.drop_observations[0].reason == "missing_response"
    assert invalid.dropped == 1
    assert invalid.drop_observations[0].reason == "invalid_result"
    assert db.execute(
        "SELECT terminal, last_outcome, last_error_code FROM hydration_jobs ORDER BY message_id"
    ).fetchall() == [
        (1, "terminal_error", "missing_response"),
        (1, "terminal_error", "invalid_result"),
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

    db.commit()
    with write_transaction(db):
        applied = MediaFactHydrationHandler(batch_size=1).apply(
            db,
            HydrationQueueRepository(db),
            [job],
            MediaPublication(result, frozenset()),
            now=20,
        )

    assert applied == applied.__class__(
        dropped=1, drop_observations=(HydrationDropObservation("invalid_result", 1, MEDIA_METADATA_KIND, 1, 0),)
    )


@pytest.mark.asyncio
async def test_media_worker_drops_job_when_access_is_lost(db: sqlite3.Connection) -> None:
    _seed_dialog(db, status="access_lost")
    _seed_message(db, 1)
    job = _enqueue(db, MEDIA_METADATA_KIND, 1)
    handler = MediaFactHydrationHandler(batch_size=1)
    worker = MessageFactHydrationWorker(
        _ScopeClient(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=1,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
    )
    with patch.object(worker, "_request_batch") as request:
        applied = await worker._process_batch(handler, [job], 20)
    assert applied.completed == 0
    assert applied.dropped == 1
    request.assert_not_called()
    assert db.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    assert db.execute("SELECT media_kind,media_payload FROM messages").fetchone() == ("other", "{}")


def test_transcription_apply_persists_final_result(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice", media_payload="{}")
    job = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    db.commit()

    db.commit()
    with write_transaction(db):
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

    db.commit()
    with write_transaction(db):
        pending = handler.apply(db, queue, [pending_job], SimpleNamespace(pending=True), now=20)
    db.execute(
        "INSERT INTO message_transcriptions(dialog_id, message_id, text, transcription_id, received_at) "
        "VALUES (1, 1, 'event fact', 8, 19)"
    )
    db.commit()
    db.commit()
    with write_transaction(db):
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

    db.commit()
    with write_transaction(db):
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

    db.commit()
    with write_transaction(db):
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
    with write_transaction(db):
        queue = HydrationQueueRepository(db)
        first_started, second_started = queue.start(first), queue.start(second)
        assert first_started is not None and second_started is not None
        first, second = first_started, second_started
    worker = MessageFactHydrationWorker(
        object(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
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
        db,
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


@pytest.mark.asyncio
async def test_todo_delivered_during_refresh_fences_late_snapshot_and_indexes_retry(db: sqlite3.Connection) -> None:
    from unittest.mock import MagicMock

    from tests.sqlite_writer_helpers import event_manager

    _seed_dialog(db)
    _seed_message(db, 1, media_kind="todo", media_payload='{"retained":true}')
    job = _enqueue(db, MEDIA_METADATA_KIND, 1)
    handler = MediaFactHydrationHandler(batch_size=1)
    worker = MessageFactHydrationWorker(
        _ScopeClient(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=2,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        clock=lambda: 100.0,
        profile_owner_account_id=42,
    )
    manager = event_manager(MagicMock(), db, asyncio.Event())
    manager.bind_demand_sink(MagicMock())

    def snapshot(title: str) -> types.Message:
        return types.Message(
            id=1,
            peer_id=types.PeerUser(1),
            date=datetime.fromtimestamp(1, UTC),
            message="",
            edit_date=datetime.fromtimestamp(999, UTC),
            media=types.MessageMediaToDo(types.TodoList(types.TextWithEntities(title, []), [])),
        )

    async def delayed(*args: object, **kwargs: object) -> object:
        event = types.MessageService(
            id=2,
            peer_id=types.PeerUser(1),
            from_id=types.PeerUser(3),
            date=datetime.fromtimestamp(2, UTC),
            action=types.MessageActionTodoCompletions([1], []),
            reply_to=types.MessageReplyHeader(reply_to_msg_id=1),
        )
        await manager.on_raw_topic_message(types.UpdateNewMessage(event, 1, 1))
        return [snapshot("stale")]

    with patch.object(worker, "_request_batch", side_effect=delayed):
        await worker._process_batch(handler, [job], 20)
    assert db.execute("SELECT media_payload FROM messages WHERE message_id=1").fetchone() == ('{"retained":true}',)
    queued = HydrationQueueRepository(db).due_jobs(2**31, 10, kind=MEDIA_METADATA_KIND)
    assert len(queued) == 1 and queued[0].attempts == 0
    with patch.object(worker, "_request_batch", return_value=[snapshot("fresh checklist")]):
        await worker._process_batch(handler, queued, 30)
    assert db.execute("SELECT text FROM messages WHERE message_id=1").fetchone() == ("fresh checklist",)
    assert db.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'fresh'").fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "request", "account", "apply", "failure", "flood"])
async def test_cancelled_hydration_settles_owned_writer_transaction(  # noqa: PLR0915
    db: sqlite3.Connection, phase: str
) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice")
    job = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    db.commit()
    database_entry = cast(tuple[int, str, str], db.execute("PRAGMA database_list").fetchone())
    path = Path(database_entry[2])
    writer = await SQLiteWriter.open(path)
    entered = threading.Event()
    release = threading.Event()
    handler = TranscriptionHydrationHandler(recheck_delay_seconds=30)
    worker = MessageFactHydrationWorker(
        object(),
        db,
        asyncio.Event(),
        writer=writer,
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=2,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
        clock=lambda: 100.0,
    )
    budget = RpcAttemptBudget(limit=2)
    original_start = worker._start_batch
    original_apply = worker._apply_batch_result
    original_error = worker._handle_batch_error

    def pause(callback: object, *args: object, **kwargs: object) -> object:
        entered.set()
        assert release.wait(10)
        return callback(*args, **kwargs)  # type: ignore[operator]

    async def request(*args: object, **kwargs: object) -> object:
        if phase in {"request", "account"}:
            if phase == "account":
                db.execute("UPDATE dialog_directory_state SET account_id=99 WHERE singleton=1")
                db.commit()
            entered.set()
            await asyncio.Event().wait()
        if phase == "failure":
            raise ValueError("private text")
        if phase == "flood":
            budget.debit()
            raise TelegramRpcThrottled(retry_after_seconds=17)
        return SimpleNamespace(text="speech", transcription_id=7)

    try:
        with (
            patch.object(worker, "_request_batch", side_effect=request),
            patch.object(
                worker,
                "_start_batch",
                side_effect=lambda *a, **k: (
                    pause(original_start, *a, **k) if phase == "start" else original_start(*a, **k)
                ),
            ),
            patch.object(
                worker,
                "_apply_batch_result",
                side_effect=lambda *a, **k: (
                    pause(original_apply, *a, **k) if phase == "apply" else original_apply(*a, **k)
                ),
            ),
            patch.object(
                worker,
                "_handle_batch_error",
                side_effect=lambda *a, **k: (
                    pause(original_error, *a, **k) if phase in {"failure", "flood"} else original_error(*a, **k)
                ),
            ),
        ):
            task = asyncio.create_task(worker._process_batch(handler, [job], 100, attempt_budget=budget))
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        row = cast(
            tuple[object, ...] | None,
            db.execute("SELECT attempts, last_outcome, last_error_code FROM hydration_jobs").fetchone(),
        )
        if phase in {"start", "request"}:
            assert row == (0, "interrupted", None)
            reopened = _open_sync_db(path)
            try:
                assert reopened.execute("SELECT attempts,last_outcome FROM hydration_jobs").fetchone() == (
                    0,
                    "interrupted",
                )
            finally:
                reopened.close()
            with patch.object(
                worker, "_request_batch", return_value=SimpleNamespace(text="speech", transcription_id=7)
            ):
                await worker._process_batch(handler, [job], 100)
            assert db.execute("SELECT text FROM message_transcriptions").fetchone() == ("speech",)
        elif phase == "account":
            assert row == (1, "processing", None)
        elif phase == "failure":
            assert row == (1, "temporary_failure", "ValueError")
        elif phase == "flood":
            assert row == (1, "temporary_failure", "TelegramRpcThrottled")
        else:
            assert row is None
            assert db.execute("SELECT text FROM message_transcriptions WHERE message_id=1").fetchone() == ("speech",)
    finally:
        release.set()
        await writer.aclose()


@pytest.mark.asyncio
async def test_current_transient_failures_exhaust_attempt_budget(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice")
    job = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    handler = TranscriptionHydrationHandler(recheck_delay_seconds=30)
    worker = MessageFactHydrationWorker(
        object(),
        db,
        asyncio.Event(),
        writer=inline_writer(db),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=2,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        clock=lambda: 100.0,
    )
    with patch.object(worker, "_request_batch", side_effect=ValueError("private")):
        for attempt in range(1, 4):
            await worker._process_batch(handler, [job], 100)
            assert db.execute("SELECT attempts,terminal,last_error_code FROM hydration_jobs").fetchone() == (
                attempt,
                int(attempt == 3),
                "ValueError",
            )


def test_invalid_transcription_result_persists_safe_reason(db: sqlite3.Connection) -> None:
    _seed_dialog(db)
    _seed_message(db, 1, media_kind="voice")
    job = _enqueue(db, TRANSCRIPTION_HYDRATION_KIND, 1)
    with write_transaction(db):
        result = TranscriptionHydrationHandler(recheck_delay_seconds=30).apply(
            db, HydrationQueueRepository(db), [job], None, now=100
        )
    assert result.dropped == 1
    assert db.execute("SELECT last_error_code FROM hydration_jobs").fetchone() == ("invalid_result",)
    assert db.execute("SELECT COUNT(*) FROM messages WHERE is_deleted=0").fetchone() == (1,)
