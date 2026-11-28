"""Contract tests for the dedicated SQLite writer."""

import asyncio
import logging
import sqlite3
import threading
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram import sqlite_writer
from mcp_telegram.sqlite_writer import SQLiteWriter, SQLiteWriterContractError
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import enable_runtime_writes, write_savepoint, write_transaction


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE writer_values (value INTEGER PRIMARY KEY)")
    finally:
        conn.close()
    return path


@pytest.mark.asyncio
async def test_writer_owns_thread_and_commits_for_readers(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    try:
        thread_name = await writer.run(lambda _conn: threading.current_thread().name)
        await writer.run(lambda conn: _insert(conn, 7))
        reader = sqlite3.connect(path)
        try:
            assert reader.execute("SELECT value FROM writer_values").fetchall() == [(7,)]
        finally:
            reader.close()
        assert thread_name == "sync-db-writer"
    finally:
        await writer.aclose()


def _insert(conn: sqlite3.Connection, value: int) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO writer_values VALUES (?)", (value,))


def _values(conn: sqlite3.Connection, sql: str) -> list[int]:
    rows = cast(list[sqlite3.Row], conn.execute(sql).fetchall())
    return [cast(int, row[0]) for row in rows]


@pytest.mark.asyncio
async def test_callback_error_rolls_back_and_writer_survives(tmp_path: Path) -> None:
    writer = await SQLiteWriter.open(_database(tmp_path))
    try:

        def fail(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                conn.execute("INSERT INTO writer_values VALUES (1)")
                raise ValueError("expected")

        with pytest.raises(ValueError, match="expected"):
            await writer.run(fail)
        await writer.run(lambda conn: _insert(conn, 2))
        values = await writer.run(lambda conn: _values(conn, "SELECT value FROM writer_values"))
        assert values == [2]
    finally:
        await writer.aclose()


@pytest.mark.asyncio
async def test_savepoint_failure_rolls_back_only_savepoint(tmp_path: Path) -> None:
    writer = await SQLiteWriter.open(_database(tmp_path))
    try:

        def write(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                conn.execute("INSERT INTO writer_values VALUES (1)")
                with pytest.raises(ValueError):
                    with write_savepoint(conn):
                        conn.execute("INSERT INTO writer_values VALUES (2)")
                        raise ValueError("savepoint")

        await writer.run(write)
        values = await writer.run(lambda conn: _values(conn, "SELECT value FROM writer_values"))
        assert values == [1]
    finally:
        await writer.aclose()


@pytest.mark.asyncio
async def test_blocked_sqlite_write_does_not_block_event_loop(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    blocker = sqlite3.connect(path, timeout=2)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        task = asyncio.create_task(writer.run(lambda conn: _insert(conn, 3)))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert await asyncio.wait_for(asyncio.sleep(0), timeout=0.1) is None
        blocker.rollback()
        await task
    finally:
        blocker.close()
        await writer.aclose()


@pytest.mark.asyncio
async def test_admission_is_bounded_and_cancellation_waits_for_commit(tmp_path: Path) -> None:
    writer = await SQLiteWriter.open(_database(tmp_path), capacity=1)
    started, finish = threading.Event(), threading.Event()

    def slow(conn: sqlite3.Connection) -> int:
        started.set()
        finish.wait(2)
        _insert(conn, 4)
        return 4

    first = asyncio.create_task(writer.run(slow))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        second = asyncio.create_task(writer.run(lambda conn: _insert(conn, 5)))
        await asyncio.sleep(0.03)
        assert not second.done()
        first.cancel()
        await asyncio.sleep(0.03)
        assert not first.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        values = await writer.run(lambda conn: _values(conn, "SELECT value FROM writer_values ORDER BY value"))
        assert values == [4, 5]
    finally:
        finish.set()
        await writer.aclose()


@pytest.mark.asyncio
async def test_close_waits_for_accepted_work_and_rejects_waiters(tmp_path: Path) -> None:
    writer = await SQLiteWriter.open(_database(tmp_path), capacity=1)
    started, finish = threading.Event(), threading.Event()

    def slow(conn: sqlite3.Connection) -> None:
        started.set()
        finish.wait(2)
        _insert(conn, 8)

    accepted = asyncio.create_task(writer.run(slow))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        waiting = asyncio.create_task(writer.run(lambda _conn: None))
        await asyncio.sleep(0.02)
        closing = asyncio.create_task(writer.aclose())
        await asyncio.sleep(0.02)
        finish.set()
        await accepted
        with pytest.raises(RuntimeError, match="unavailable"):
            await waiting
        await closing
        with pytest.raises(RuntimeError, match="unavailable"):
            await writer.run(lambda _conn: None)
    finally:
        finish.set()
        await writer.aclose()


@pytest.mark.asyncio
async def test_unclosed_transaction_is_rolled_back_and_reported(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    try:

        def leak(conn: sqlite3.Connection) -> None:
            conn.execute("PRAGMA query_only = OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO writer_values VALUES (9)")

        with pytest.raises(SQLiteWriterContractError, match="transaction open"):
            await writer.run(leak)
        reader = sqlite3.connect(path)
        try:
            assert reader.execute("SELECT value FROM writer_values").fetchall() == []
        finally:
            reader.close()
        with pytest.raises(RuntimeError, match="unavailable"):
            await writer.run(lambda conn: _insert(conn, 10))
    finally:
        await writer.aclose()


@pytest.mark.asyncio
async def test_closed_connection_retires_writer_and_rejects_pending(tmp_path: Path) -> None:
    writer = await SQLiteWriter.open(_database(tmp_path), capacity=2)
    started, finish = threading.Event(), threading.Event()

    def retire(conn: sqlite3.Connection) -> None:
        started.set()
        finish.wait(2)
        conn.close()

    cancelled_watcher = asyncio.create_task(writer.wait_failed())
    await asyncio.sleep(0)
    cancelled_watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_watcher
    watcher = asyncio.create_task(writer.wait_failed())
    retiring = asyncio.create_task(writer.run(retire))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        pending = asyncio.create_task(writer.run(lambda _conn: None))
        await asyncio.sleep(0.02)
        finish.set()
        with pytest.raises(sqlite3.ProgrammingError):
            await retiring
        failure = await asyncio.wait_for(watcher, timeout=1)
        assert isinstance(failure, sqlite3.ProgrammingError)
        assert writer.failure is failure
        with pytest.raises(RuntimeError, match="retired"):
            await pending
        with pytest.raises(RuntimeError, match="unavailable"):
            await writer.run(lambda _conn: None)
    finally:
        finish.set()
        await writer.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["dequeued", "executing", "recovering", "settled"])
async def test_worker_failure_settles_owned_and_queued_requests(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path, capacity=2)
    started, finish = threading.Event(), threading.Event()
    original_timing = sqlite_writer.current_timing
    original_execute = writer._execute

    def fail() -> None:
        started.set()
        assert finish.wait(2)
        raise RuntimeError("injected worker failure")

    def timing() -> object:
        if stage == "dequeued" and threading.current_thread().name == "sync-db-writer":
            fail()
        return original_timing()

    def execute(*args: object) -> bool:
        if stage == "executing":
            fail()
        outcome = original_execute(*args)  # type: ignore[arg-type]
        if stage == "settled":
            fail()
        return outcome

    monkeypatch.setattr(sqlite_writer, "current_timing", timing)
    monkeypatch.setattr(writer, "_execute", execute)

    def callback(conn: sqlite3.Connection) -> None:
        _insert(conn, 42)
        if stage == "recovering":
            raise ValueError("after commit")

    if stage == "recovering":
        monkeypatch.setattr(writer, "_recover", lambda *_args: fail())
    current = asyncio.create_task(writer.run(callback))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        queued = asyncio.create_task(writer.run(lambda _conn: None))
        waiting = asyncio.create_task(writer.run(lambda _conn: None))
        await asyncio.sleep(0.02)
        finish.set()
        if stage == "settled":
            await asyncio.wait_for(current, 1)
        else:
            with pytest.raises(RuntimeError, match="injected worker failure"):
                await asyncio.wait_for(current, 1)
        with pytest.raises(RuntimeError, match="retired"):
            await asyncio.wait_for(queued, 1)
        with pytest.raises(RuntimeError, match="unavailable"):
            await asyncio.wait_for(waiting, 1)
        with pytest.raises(RuntimeError, match="injected worker failure"):
            await asyncio.wait_for(writer.aclose(), 1)
        await asyncio.sleep(0)
        assert writer._slots._value == 2
        with closing(sqlite3.connect(path)) as reader:
            rows = reader.execute("SELECT value FROM writer_values").fetchall()
        assert rows == ([(42,)] if stage in {"recovering", "settled"} else [])
    finally:
        finish.set()


@pytest.mark.asyncio
async def test_worker_failure_closes_transaction_before_reporting_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path, capacity=2)
    cleanup_started, finish_cleanup = threading.Event(), threading.Event()
    original_finish = writer._finish_worker

    def fail_recovery(*_args: object) -> bool:
        raise RuntimeError("recovery failed")

    def delayed_finish(
        conn: sqlite3.Connection | None, error: BaseException | None, result: Future[object] | None
    ) -> None:
        cleanup_started.set()
        assert finish_cleanup.wait(2)
        original_finish(conn, error, result)

    def leave_transaction_open(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA query_only = OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO writer_values VALUES (77)")
        raise ValueError("callback failed")

    monkeypatch.setattr(writer, "_recover", fail_recovery)
    monkeypatch.setattr(writer, "_finish_worker", delayed_finish)
    current = asyncio.create_task(writer.run(leave_transaction_open))
    try:
        assert await asyncio.to_thread(cleanup_started.wait, 1)
        assert not current.done()
        assert isinstance(writer.failure, RuntimeError)
        with pytest.raises(RuntimeError, match="unavailable"):
            await writer.run(lambda _conn: None)
        finish_cleanup.set()
        with pytest.raises(RuntimeError, match="recovery failed"):
            await asyncio.wait_for(current, 1)
        with closing(sqlite3.connect(path)) as reader:
            assert reader.execute("SELECT value FROM writer_values").fetchall() == []
        with pytest.raises(RuntimeError, match="recovery failed"):
            await writer.aclose()
    finally:
        finish_cleanup.set()


@pytest.mark.asyncio
async def test_cancelled_startup_still_closes_worker_after_repeated_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _database(tmp_path)
    original_open = sqlite_writer.open_runtime_sync_db
    started, finish = threading.Event(), threading.Event()

    def slow_open(db_path: Path) -> sqlite3.Connection:
        started.set()
        finish.wait(2)
        return original_open(db_path)

    monkeypatch.setattr(sqlite_writer, "open_runtime_sync_db", slow_open)
    opening = asyncio.create_task(SQLiteWriter.open(path))
    assert await asyncio.to_thread(started.wait, 1)
    opening.cancel()
    await asyncio.sleep(0)
    opening.cancel()
    await asyncio.sleep(0)
    assert not opening.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert not any(thread.name == "sync-db-writer" and thread.is_alive() for thread in threading.enumerate())


@pytest.mark.asyncio
async def test_cancelled_startup_consumes_factory_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _database(tmp_path)
    started, finish = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, object]] = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    def failing_open(_db_path: Path) -> sqlite3.Connection:
        started.set()
        finish.wait(2)
        raise OSError("startup failed")

    monkeypatch.setattr(sqlite_writer, "open_runtime_sync_db", failing_open)
    opening = asyncio.create_task(SQLiteWriter.open(path))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        opening.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        await asyncio.sleep(0)
        assert loop_errors == []
    finally:
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_factory_failure_is_reported_by_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _database(tmp_path)

    def failing_open(_db_path: Path) -> sqlite3.Connection:
        raise OSError("startup failed")

    monkeypatch.setattr(sqlite_writer, "open_runtime_sync_db", failing_open)
    with pytest.raises(OSError, match="startup failed"):
        await SQLiteWriter.open(path)


@pytest.mark.asyncio
async def test_cancelled_close_waits_for_worker_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _database(tmp_path)
    original_open = sqlite_writer.open_runtime_sync_db
    started, finish = threading.Event(), threading.Event()

    def slow_checkpoint(action: int, arg1: str | None, _arg2: str | None, _db: str | None, _trigger: str | None) -> int:
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "wal_checkpoint":
            started.set()
            finish.wait(2)
        return sqlite3.SQLITE_OK

    def open_writer(db_path: Path) -> sqlite3.Connection:
        conn = original_open(db_path)
        conn.set_authorizer(slow_checkpoint)
        return conn

    monkeypatch.setattr(sqlite_writer, "open_runtime_sync_db", open_writer)
    writer = await SQLiteWriter.open(path)
    closing = asyncio.create_task(writer.aclose())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
    finally:
        finish.set()


@pytest.mark.asyncio
async def test_slow_queue_wait_is_logged_without_callback_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = await SQLiteWriter.open(_database(tmp_path), capacity=2)

    class FakeClock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = FakeClock()
    monkeypatch.setattr(sqlite_writer, "time", clock)
    first_started, finish_first = threading.Event(), threading.Event()

    def hold_worker(_conn: sqlite3.Connection) -> None:
        first_started.set()
        finish_first.wait(2)

    first = asyncio.create_task(writer.run(hold_worker))
    try:
        assert await asyncio.to_thread(first_started.wait, 1)
        second = asyncio.create_task(writer.run(lambda _conn: None))
        await asyncio.sleep(0.02)
        clock.now = 101.25
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.sqlite_writer"):
            finish_first.set()
            await first
            await second
        records = [record for record in caplog.records if record.name == "mcp_telegram.sqlite_writer"]
        assert len(records) == 1
        assert records[0].message == "sqlite_writer_queue_slow queue_wait_seconds=1.250"
    finally:
        finish_first.set()
        await writer.aclose()


@pytest.mark.asyncio
async def test_checkpoint_failure_is_reported_and_connection_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _database(tmp_path)

    def open_writer(db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        enable_runtime_writes(conn)
        conn.set_authorizer(
            lambda action, arg1, _arg2, _db, _trigger: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and arg1 == "wal_checkpoint"
                else sqlite3.SQLITE_OK
            ),
        )
        return conn

    monkeypatch.setattr(sqlite_writer, "open_runtime_sync_db", open_writer)
    writer = await SQLiteWriter.open(path)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        await writer.aclose()
