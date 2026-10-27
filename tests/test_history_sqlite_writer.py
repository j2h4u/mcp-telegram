"""History orchestration uses the real owner with read-only loop connections."""

import asyncio
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Never, cast
from unittest.mock import AsyncMock

import pytest

from mcp_telegram import activity_peer_sweep, activity_sync, delta_sync, sync_worker
from mcp_telegram.activity_substrate import ActivityClient
from mcp_telegram.delta_sync import DeltaSyncWorker
from mcp_telegram.message_history.contracts import ForwardGapPage, FullHistoryPage, MessageHistoryAccessLostError
from mcp_telegram.message_history.ports import ForwardGapPagePort, FullHistoryPagePort
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.sync_worker import FullSyncWorker
from tests.helpers import build_mock_message
from tests.history_enrollment_helpers import seed_full_history_enrollment

_PREPARATION_PATHS = ["full", "delta", "peer", "backfill", "incremental"]


@pytest.fixture
async def prepared_db(tmp_path: Path, kind: str) -> AsyncIterator[tuple[Path, sqlite3.Connection, SQLiteWriter]]:
    path = tmp_path / "prepared.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as setup:
        setup.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(1,'not_synced')")
        setup.executemany(
            "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES(?,?)",
            [("backfill_complete", "1" if kind == "incremental" else "0"), ("last_sync_at", "100")],
        )
        setup.commit()
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path, capacity=1)
    try:
        yield path, reader, writer
    finally:
        await writer.aclose()
        reader.close()


class _AcquisitionReachedError(Exception):
    """Stop at the first data acquisition, before unrelated result persistence."""


def _operation(
    kind: str,
    reader: sqlite3.Connection,
    writer: SQLiteWriter,
    monkeypatch: pytest.MonkeyPatch,
    on_rpc: Callable[[], None],
) -> Callable[[], Coroutine[object, object, object]]:
    async def rpc(*_args: object, **_kwargs: object) -> Never:
        on_rpc()
        raise _AcquisitionReachedError

    port = SimpleNamespace(fetch_page=AsyncMock(side_effect=rpc))
    if kind == "full":
        full = FullSyncWorker(cast(FullHistoryPagePort, port), reader, asyncio.Event(), writer=writer)
        return lambda: full._fetch_batch_page(1, 0)
    if kind == "delta":
        delta = DeltaSyncWorker(cast(ForwardGapPagePort, port), reader, asyncio.Event(), writer=writer)
        return lambda: delta._collect_delta_slice(1, 0, 0)
    client = cast(ActivityClient, object())
    if kind == "peer":
        # Peer resolution precedes the preparation being consolidated; data acquisition does not.
        monkeypatch.setattr(activity_peer_sweep, "_resolve_peer_for_sweep", AsyncMock(return_value=object()))
        monkeypatch.setattr(activity_peer_sweep, "_search_self_messages", rpc)
        request = activity_peer_sweep.PeerSweepRequest(client, reader, 1, 0, 0, 1, 1, writer=writer)
        return lambda: activity_peer_sweep.sweep_peer_once(request)
    if kind == "backfill":
        monkeypatch.setattr(activity_sync, "_search_backfill_batch", rpc)
        adapter = activity_sync.ArchiveBackfillDemandAdapter(
            client, reader, asyncio.Event(), timeout_s=1, writer=writer, account_id=1
        )
        status = adapter.status(0.0)
        assert status is not None and status.selected is not None
        expected_revision = status.selected.revision
        return lambda: activity_sync._run_backfill_slice(
            client, reader, asyncio.Event(), timeout_s=1, writer=writer, expected_revision=expected_revision
        )
    assert kind == "incremental"
    monkeypatch.setattr(activity_sync, "_search_incremental_batch", rpc)
    adapter = activity_sync.ArchiveIncrementalDemandAdapter(
        client, reader, asyncio.Event(), interval_s=1, timeout_s=1, writer=writer, account_id=1
    )
    status = adapter.status(0.0)
    assert status is not None and status.selected is not None
    expected_revision = status.selected.revision
    return lambda: activity_sync._run_incremental_slice(
        client, reader, asyncio.Event(), timeout_s=1, writer=writer, expected_revision=expected_revision
    )


async def _trace_jobs(writer: SQLiteWriter, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    trace: list[str] = []
    jobs: list[str] = []
    await writer.run(lambda db: db.set_trace_callback(trace.append))
    run = writer.run

    async def counted_run[T](callback: Callable[[sqlite3.Connection], T]) -> T:
        def counted(db: sqlite3.Connection) -> T:
            jobs.append(callback.__name__)
            return callback(db)

        return await run(counted)

    monkeypatch.setattr(writer, "run", counted_run)
    return trace, jobs


def _sequence(reader: sqlite3.Connection) -> int:
    row = cast(
        tuple[str] | None,
        reader.execute("SELECT value FROM daemon_state WHERE key='canonical_observation_sequence'").fetchone(),
    )
    return 0 if row is None else int(row[0])


@pytest.mark.parametrize("kind", _PREPARATION_PATHS)
async def test_preparation_is_one_committed_job_before_acquisition(
    prepared_db: tuple[Path, sqlite3.Connection, SQLiteWriter], kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, reader, writer = prepared_db
    trace, jobs = await _trace_jobs(writer, monkeypatch)

    def rpc() -> None:
        assert _sequence(reader) == 1
        assert len(jobs) == 1
        assert sum(sql.startswith("BEGIN") for sql in trace) == 1
        assert trace.count("COMMIT") == 1
        assert not reader.in_transaction
        if kind in {"full", "delta", "peer"}:
            assert reader.execute("SELECT needs_refresh FROM dialogs WHERE dialog_id=1").fetchone() == (1,)
        elif kind == "backfill":
            activity_state = dict(reader.execute("SELECT key,value FROM activity_sync_state"))
            assert activity_state == {
                "backfill_complete": "0",
                "last_sync_at": "100",
                "backfill_offset_id": "0",
            }
        else:
            activity_state = dict(reader.execute("SELECT key,value FROM activity_sync_state"))
            assert activity_state == {
                "backfill_complete": "1",
                "last_sync_at": "100",
                "backfill_offset_id": "0",
            }

    with pytest.raises(_AcquisitionReachedError):
        await _operation(kind, reader, writer, monkeypatch, rpc)()


@pytest.mark.parametrize("kind", _PREPARATION_PATHS)
@pytest.mark.parametrize("failure", ["body", "commit"])
async def test_preparation_failure_rolls_back_and_never_acquires(
    prepared_db: tuple[Path, sqlite3.Connection, SQLiteWriter],
    kind: str,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, reader, writer = prepared_db
    state = cast(
        list[tuple[str, str | None]],
        reader.execute("SELECT key,value FROM activity_sync_state ORDER BY key").fetchall(),
    )
    if failure == "commit":
        await writer.run(
            lambda db: db.set_authorizer(
                lambda action, value, *_args: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_TRANSACTION and value == "COMMIT"
                    else sqlite3.SQLITE_OK
                )
            )
        )
    else:

        def fail(db: sqlite3.Connection) -> int:
            allocate_observation_order(db)
            raise ValueError("preparation failed")

        module = {"full": sync_worker, "delta": delta_sync, "peer": activity_peer_sweep}.get(kind, activity_sync)
        monkeypatch.setattr(module, "allocate_observation_order", fail)
    reached: list[bool] = []
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        await _operation(kind, reader, writer, monkeypatch, lambda: reached.append(True))()
    assert reached == []
    assert _sequence(reader) == 0
    assert reader.execute("SELECT dialog_id FROM dialogs").fetchall() == []
    assert reader.execute("SELECT key,value FROM activity_sync_state ORDER BY key").fetchall() == state
    assert writer.failure is None


@pytest.mark.parametrize("kind", ["backfill", "incremental"])
@pytest.mark.parametrize("stopped", [False, True])
async def test_no_action_does_not_reserve_an_order(
    prepared_db: tuple[Path, sqlite3.Connection, SQLiteWriter], kind: str, stopped: bool
) -> None:
    _, reader, writer = prepared_db

    def prepare(db: sqlite3.Connection) -> object:
        with write_transaction(db):
            if not stopped and kind == "backfill":
                db.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
            elif not stopped:
                db.execute("UPDATE activity_sync_state SET value='0' WHERE key='last_sync_at'")
        callback = (
            activity_sync._prepare_backfill_slice if kind == "backfill" else activity_sync._prepare_incremental_slice
        )
        return callback(db, stopped)

    assert await writer.run(prepare) is None
    assert _sequence(reader) == 0


@pytest.mark.parametrize("kind", _PREPARATION_PATHS)
async def test_merged_orders_survive_reverse_completion_and_reopen(
    prepared_db: tuple[Path, sqlite3.Connection, SQLiteWriter], kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, reader, writer = prepared_db
    operation = _operation(kind, reader, writer, monkeypatch, lambda: None)
    with pytest.raises(_AcquisitionReachedError):
        await operation()
    older = _sequence(reader)
    with pytest.raises(_AcquisitionReachedError):
        await operation()
    newer = _sequence(reader)
    assert newer > older > 0
    base = extract_message_row(1, build_mock_message(id=77, text="newer"))

    def complete_in_reverse(db: sqlite3.Connection) -> None:
        with write_transaction(db):
            insert_messages_with_fts(db, [replace(base, observation_order=newer)])
            insert_messages_with_fts(
                db, [replace(base, observation_order=older, message=replace(base.message, text="older"))]
            )

    await writer.run(complete_in_reverse)
    assert reader.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=77").fetchone() == ("newer",)
    await writer.aclose()
    reopened = await SQLiteWriter.open(path)
    try:
        assert await reopened.run(allocate_observation_order) > newer
    finally:
        await reopened.aclose()


@pytest.mark.parametrize("kind", _PREPARATION_PATHS)
@pytest.mark.parametrize("admitted", [False, True])
async def test_cancelled_preparation_settles_without_acquisition(
    prepared_db: tuple[Path, sqlite3.Connection, SQLiteWriter],
    kind: str,
    admitted: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, reader, initial_writer = prepared_db
    await initial_writer.aclose()
    writer = await SQLiteWriter.open(path, capacity=2 if admitted else 1)
    trace, jobs = await _trace_jobs(writer, monkeypatch)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def hold(_db: sqlite3.Connection) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)

    holding = asyncio.create_task(writer.run(hold))
    task: asyncio.Task[object] | None = None
    reached: list[bool] = []
    try:
        await asyncio.wait_for(entered.wait(), 10)
        jobs.clear()
        task = asyncio.create_task(_operation(kind, reader, writer, monkeypatch, lambda: reached.append(True))())
        await asyncio.sleep(0)
        assert writer._queue.qsize() == int(admitted)
        task.cancel()
        await asyncio.sleep(0)
        if admitted:
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        await asyncio.wait_for(holding, 10)
        assert reached == []
        assert _sequence(reader) == int(admitted)
        assert len(jobs) == int(admitted)
        assert trace.count("COMMIT") == int(admitted)
    finally:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(holding, *([task] if task is not None else []), return_exceptions=True), 10
        )
        await writer.aclose()


@pytest.mark.parametrize("kind", ["full", "delta"])
async def test_reaction_anchor_is_after_prep_but_before_commit(
    prepared_db: tuple[Path, sqlite3.Connection, SQLiteWriter], kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, reader, writer = prepared_db
    clock = [100]
    baseline = sync_worker.ensure_dialog_identity_baseline

    def prepare(db: sqlite3.Connection, dialog_id: int) -> int:
        revision = baseline(db, dialog_id)
        clock[0] = 200
        return revision

    def trace(sql: str) -> None:
        if sql == "COMMIT":
            clock[0] = 300

    def acquired() -> None:
        assert clock[0] == 300
        assert _sequence(reader) == 1
        assert reader.execute("SELECT dialog_id FROM dialogs WHERE dialog_id=1").fetchone() == (1,)

    monkeypatch.setattr(sync_worker if kind == "full" else delta_sync, "ensure_dialog_identity_baseline", prepare)
    monkeypatch.setattr(sync_worker.time, "time", lambda: clock[0])
    await writer.run(lambda db: db.set_trace_callback(trace))

    class FullPort:
        async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
            acquired()
            return FullHistoryPage((), 0, None)

    class DeltaPort:
        async def fetch_page(
            self, dialog_id: int, *, after_message_id: int, should_stop: Callable[[], bool]
        ) -> ForwardGapPage:
            acquired()
            return ForwardGapPage((), complete=True)

    if kind == "full":
        result = await FullSyncWorker(FullPort(), reader, asyncio.Event(), writer=writer)._fetch_batch_page(1, 0)
    else:
        result = await DeltaSyncWorker(DeltaPort(), reader, asyncio.Event(), writer=writer)._collect_delta_slice(
            1, 0, 0
        )
    assert result.reaction_observed_at == 200


@pytest.mark.asyncio
async def test_history_workers_commit_through_owner_with_readonly_readers(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with _open_sync_db(path) as setup:
        setup.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'not_synced')")
        seed_full_history_enrollment(setup, 1, enabled=True)
    setup.close()
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)

    class FullPort:
        async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
            if before_message_id == 0:
                return FullHistoryPage((extract_message_row(dialog_id, build_mock_message(id=2, text="full")),), 1, 2)
            return FullHistoryPage((), 0, None)

    class DeltaPort:
        async def fetch_page(
            self, dialog_id: int, *, after_message_id: int, should_stop: Callable[[], bool]
        ) -> ForwardGapPage:
            return ForwardGapPage(
                (extract_message_row(dialog_id, build_mock_message(id=3, text="delta")),), complete=True
            )

    try:
        full = FullSyncWorker(FullPort(), reader, asyncio.Event(), writer=writer)
        assert not await full.process_one_batch()
        assert await full.process_one_batch()
        assert reader.execute("SELECT status FROM synced_dialogs WHERE dialog_id=1").fetchone() == ("synced",)
        delta = DeltaSyncWorker(DeltaPort(), reader, asyncio.Event(), writer=writer)
        assert await delta.fetch_delta_slice_for_dialog(1) == 1
        assert delta._last_delta_slice_succeeded
        assert reader.execute("SELECT last_delta_checked_at FROM synced_dialogs WHERE dialog_id=1").fetchone()[0]
        assert reader.execute("SELECT message_id FROM messages ORDER BY message_id").fetchall() == [(2,), (3,)]
        assert reader.execute("SELECT delta_message_id FROM synced_dialogs WHERE dialog_id=1").fetchone() == (3,)
        assert reader.execute(
            "SELECT value FROM daemon_state WHERE key='canonical_observation_sequence'"
        ).fetchone() == ("3",)
    finally:
        await writer.aclose()
        reader.close()


@pytest.mark.asyncio
async def test_delta_access_loss_is_committed_before_reporting_it(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with _open_sync_db(path) as setup:
        setup.execute("INSERT INTO synced_dialogs(dialog_id,status,delta_refresh_requested_at) VALUES (1,'synced',1)")
        seed_full_history_enrollment(setup, 1, enabled=True)
    setup.close()
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)

    class LostAccessPort:
        async def fetch_page(
            self, dialog_id: int, *, after_message_id: int, should_stop: Callable[[], bool]
        ) -> ForwardGapPage:
            raise MessageHistoryAccessLostError("private", reason_code="ChannelPrivateError")

    try:
        worker = DeltaSyncWorker(LostAccessPort(), reader, asyncio.Event(), writer=writer)
        assert await worker.fetch_delta_slice_for_dialog(1) == 0
        assert worker._last_delta_slice_access_lost
        assert not worker._last_delta_slice_succeeded
        assert reader.execute("SELECT status FROM synced_dialogs WHERE dialog_id=1").fetchone() == ("access_lost",)
        assert reader.execute("SELECT reason_code FROM conversation_history_events WHERE dialog_id=1").fetchone() == (
            "ChannelPrivateError",
        )
    finally:
        await writer.aclose()
        reader.close()
