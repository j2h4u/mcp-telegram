import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import mcp_telegram.sync_transactions as sync_transactions
from mcp_telegram.sync_transactions import (
    enable_runtime_writes,
    require_write_transaction,
    write_savepoint,
    write_transaction,
)


def _query_only(conn: sqlite3.Connection) -> bool:
    return bool(cast(tuple[int], conn.execute("PRAGMA query_only").fetchone())[0])


def _create_db(path: Path) -> None:
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE items (value INTEGER)")
        conn.executemany("INSERT INTO items VALUES (?)", [(1,), (2,), (3,)])


def test_writer_reserved_before_body_reads(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    _create_db(path)
    with (
        closing(sqlite3.connect(path, timeout=0)) as conn,
        closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as competitor,
    ):
        enable_runtime_writes(conn)
        with write_transaction(conn):
            require_write_transaction(conn)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("BEGIN IMMEDIATE")
            assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3
            conn.execute("INSERT INTO items VALUES (4)")
            conn.execute("SAVEPOINT nested")
            require_write_transaction(conn)
            conn.execute("INSERT INTO items VALUES (5)")
            conn.execute("RELEASE nested")
        assert not conn.in_transaction
        assert _query_only(conn)
        assert competitor.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 5
        with pytest.raises(RuntimeError, match="owned"):
            require_write_transaction(conn)


@pytest.mark.parametrize("foreign_begin", ["BEGIN", "SAVEPOINT foreign_unit"])
def test_foreign_transaction_is_rejected_untouched(foreign_begin: str) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE items (value INTEGER)")
        conn.execute(foreign_begin)
        conn.execute("INSERT INTO items VALUES (1)")
        with pytest.raises(RuntimeError, match="idle"):
            with write_transaction(conn):
                pytest.fail("Foreign transaction must not be joined")
        with pytest.raises(RuntimeError, match="owned"):
            require_write_transaction(conn)
        with pytest.raises(RuntimeError, match="during"):
            enable_runtime_writes(conn)
        assert conn.in_transaction
        assert not _query_only(conn)
        assert conn.isolation_level == ""
        assert conn.execute("SELECT value FROM items").fetchone()[0] == 1
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt, SystemExit])
def test_base_exception_rolls_back_and_restores_original_guard(guarded: bool, error_type: type[BaseException]) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE items (value INTEGER)")
        if guarded:
            enable_runtime_writes(conn)
        failure = error_type("abort")
        with pytest.raises(error_type) as caught:
            with write_transaction(conn):
                conn.execute("INSERT INTO items VALUES (1)")
                raise failure
        assert caught.value is failure
        assert not conn.in_transaction
        assert _query_only(conn) is guarded
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        with write_transaction(conn):
            conn.execute("INSERT INTO items VALUES (2)")
        assert _query_only(conn) is guarded


def test_deferred_foreign_key_commit_failure_rolls_back(caplog: pytest.LogCaptureFixture) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (parent_id REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
        enable_runtime_writes(conn)
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.sync_transactions"):
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                with write_transaction(conn):
                    conn.execute("INSERT INTO child VALUES (42)")
        record = next(record for record in caplog.records if record.name == "mcp_telegram.sync_transactions")
        assert "stage=commit" in record.message
        assert "sqlite_errorcode=787 sqlite_errorname=SQLITE_CONSTRAINT_FOREIGNKEY" in record.message
        assert not conn.in_transaction
        assert _query_only(conn)
        assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
        with write_transaction(conn):
            conn.execute("INSERT INTO parent VALUES (42)")
            conn.execute("INSERT INTO child VALUES (42)")


def test_idle_guard_rejects_cached_dml_and_ddl() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE items (value INTEGER)")
        conn.execute("INSERT INTO items VALUES (1)")
        conn.commit()
        enable_runtime_writes(conn)
        for sql in ("INSERT INTO items VALUES (1)", "CREATE TABLE forbidden (value INTEGER)"):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute(sql)
            assert not conn.in_transaction
        with write_transaction(conn):
            conn.execute("INSERT INTO items VALUES (1)")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO items VALUES (1)")


def test_competing_writer_entry_failure_restores_guard(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    _create_db(path)
    with (
        closing(sqlite3.connect(path, timeout=0)) as conn,
        closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as competitor,
    ):
        enable_runtime_writes(conn)
        competitor.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with write_transaction(conn):
                pytest.fail("Cannot enter while another writer holds the lock")
        assert not conn.in_transaction
        assert _query_only(conn)
        competitor.rollback()
        with write_transaction(conn):
            require_write_transaction(conn)


def test_fast_busy_entry_failure_is_debug_without_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "sync.db"
    _create_db(path)
    with (
        closing(sqlite3.connect(path, timeout=0)) as conn,
        closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as competitor,
    ):
        enable_runtime_writes(conn)
        competitor.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.DEBUG, logger="mcp_telegram.sync_transactions"):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                with write_transaction(conn):
                    pytest.fail("Cannot enter while another writer holds the lock")
        records = [record for record in caplog.records if record.name == "mcp_telegram.sync_transactions"]
        assert records
        assert records[-1].levelno == logging.DEBUG
        assert "stage=begin" in records[-1].message
        assert "sqlite_errorcode=5" in records[-1].message
        assert "sqlite_errorname=SQLITE_BUSY" in records[-1].message
        competitor.rollback()


def test_slow_write_logs_owner_and_timings(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    clock = iter((100.0, 100.0, 101.25, 101.25))
    monkeypatch.setattr(sync_transactions, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with closing(sqlite3.connect(":memory:")) as conn:
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.sync_transactions"):
            with write_transaction(conn):
                pass
    records = [record for record in caplog.records if record.name == "mcp_telegram.sync_transactions"]
    assert len(records) == 1
    message = records[0].message
    assert records[0].levelno == logging.WARNING
    assert message.startswith("sqlite_write_slow stage=commit")
    assert "caller_module=tests.test_sync_transactions" in message
    assert "caller_function=test_slow_write_logs_owner_and_timings" in message
    assert "wait_seconds=0.000 held_seconds=1.250" in message
    assert "thread_name=MainThread" in message


def test_slow_lock_wait_logs_after_transaction(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = iter((200.0, 201.25, 201.25, 201.25))
    monkeypatch.setattr(sync_transactions, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with closing(sqlite3.connect(":memory:")) as conn:
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.sync_transactions"):
            with write_transaction(conn):
                pass
    records = [record for record in caplog.records if record.name == "mcp_telegram.sync_transactions"]
    assert len(records) == 1
    assert records[0].message.startswith("sqlite_write_slow stage=begin")
    assert "wait_seconds=1.250 held_seconds=0.000" in records[0].message


def test_slow_wait_logs_when_body_fails(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    clock = iter((300.0, 301.25, 301.25, 301.25, 301.25))
    monkeypatch.setattr(sync_transactions, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with closing(sqlite3.connect(":memory:")) as conn:
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.sync_transactions"):
            with pytest.raises(ValueError, match="body failure"):
                with write_transaction(conn):
                    raise ValueError("body failure")
    record = next(record for record in caplog.records if record.name == "mcp_telegram.sync_transactions")
    assert record.message.startswith("sqlite_write_slow stage=body")
    assert "wait_seconds=1.250" in record.message
    assert "error_type=ValueError" in record.message


def test_busy_after_long_hold_is_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sync.db"
    _create_db(path)
    clock = iter((400.0, 400.0, 401.25, 401.25, 401.25))
    monkeypatch.setattr(sync_transactions, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with (
        closing(sqlite3.connect(path, timeout=0)) as conn,
        closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as competitor,
    ):
        enable_runtime_writes(conn)
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.sync_transactions"):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                with write_transaction(conn):
                    competitor.execute("BEGIN IMMEDIATE")
        record = next(record for record in caplog.records if record.name == "mcp_telegram.sync_transactions")
        assert record.levelno == logging.WARNING
        assert "stage=body" in record.message
        assert "sqlite_errorcode=5 sqlite_errorname=SQLITE_BUSY" in record.message
        assert "held_seconds=1.250" in record.message


def test_failing_logger_does_not_replace_primary_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((500.0, 500.0, 501.25, 501.25, 501.25))
    monkeypatch.setattr(sync_transactions, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    logger_transaction_states: list[bool] = []

    class _FailingLogger:
        def log(self, *_args: object, **_kwargs: object) -> None:
            logger_transaction_states.append(conn.in_transaction)
            raise RuntimeError("logger failure")

    monkeypatch.setattr(sync_transactions, "logger", _FailingLogger())
    with closing(sqlite3.connect(":memory:")) as conn:
        failure = ValueError("primary failure")
        with pytest.raises(ValueError) as caught:
            with write_transaction(conn):
                raise failure
        assert caught.value is failure
        assert _query_only(conn) is False
        assert logger_transaction_states == [False]


def test_sqlite_failure_logs_stage_classification_without_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.sync_transactions"):
            with pytest.raises(sqlite3.OperationalError):
                with write_transaction(conn):
                    conn.execute("INSERT INTO private_table VALUES (1)")
    records = [record for record in caplog.records if record.name == "mcp_telegram.sync_transactions"]
    assert len(records) == 1
    message = records[0].message
    assert message.startswith("sqlite_write_failure stage=body")
    assert "error_type=OperationalError" in message
    assert "sqlite_errorcode=1 sqlite_errorname=SQLITE_ERROR" in message
    assert "private_table" not in message
    assert "no such table" not in message


def test_escaped_read_cursor_busy_snapshot_entry_restores_guard(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sync.db"
    _create_db(path)
    with (
        closing(sqlite3.connect(path, timeout=0)) as conn,
        closing(sqlite3.connect(path, isolation_level=None)) as competitor,
    ):
        enable_runtime_writes(conn)
        cursor = conn.execute("SELECT value FROM items")
        assert cursor.fetchone()[0] == 1
        competitor.execute("INSERT INTO items VALUES (4)")
        with caplog.at_level(logging.WARNING, logger="mcp_telegram.sync_transactions"):
            with pytest.raises(sqlite3.OperationalError) as caught:
                with write_transaction(conn):
                    pytest.fail("Stale retained cursor must fail at entry")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        record = next(record for record in caplog.records if record.name == "mcp_telegram.sync_transactions")
        assert record.levelno == logging.WARNING
        assert "sqlite_errorcode=517 sqlite_errorname=SQLITE_BUSY_SNAPSHOT" in record.message
        assert not conn.in_transaction
        assert _query_only(conn)
        cursor.close()
        with write_transaction(conn):
            conn.execute("INSERT INTO items VALUES (5)")


def test_nested_write_unit_rejected_without_disrupting_owner() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        enable_runtime_writes(conn)
        with write_transaction(conn):
            with pytest.raises(RuntimeError, match="idle"):
                with write_transaction(conn):
                    pytest.fail("Nested write unit must be rejected")
            require_write_transaction(conn)
            assert not _query_only(conn)
        assert _query_only(conn)


class _RollbackFailure(sqlite3.Connection):
    def rollback(self) -> None:
        raise sqlite3.OperationalError("injected rollback failure")


def test_rollback_failure_retires_connection_preserving_primary(caplog: pytest.LogCaptureFixture) -> None:
    conn = sqlite3.connect(":memory:", factory=_RollbackFailure)
    enable_runtime_writes(conn)
    failure = KeyboardInterrupt("abort")
    with caplog.at_level(logging.WARNING, logger="mcp_telegram.sync_transactions"):
        with pytest.raises(KeyboardInterrupt) as caught:
            with write_transaction(conn):
                raise failure
    assert caught.value is failure
    assert "rollback failed" in failure.__notes__[0]
    record = next(record for record in caplog.records if record.name == "mcp_telegram.sync_transactions")
    assert "stage=rollback" in record.message
    assert "error_type=OperationalError" in record.message
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def _deny_guard_restore(action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None) -> int:
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "query_only" and arg2 == "1":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@pytest.mark.parametrize("abort", [False, True])
def test_restoration_failure_retires_connection(abort: bool, caplog: pytest.LogCaptureFixture) -> None:
    conn = sqlite3.connect(":memory:")
    enable_runtime_writes(conn)
    failure = KeyboardInterrupt("abort")
    expected = KeyboardInterrupt if abort else sqlite3.DatabaseError
    with caplog.at_level(logging.WARNING, logger="mcp_telegram.sync_transactions"):
        with pytest.raises(expected) as caught:
            with write_transaction(conn):
                conn.set_authorizer(_deny_guard_restore)
                if abort:
                    raise failure
    if abort:
        assert caught.value is failure
        assert "Restoring" in failure.__notes__[0]
    record = next(record for record in caplog.records if record.name == "mcp_telegram.sync_transactions")
    assert "stage=guard_restore" in record.message
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_nested_savepoints_rollback_only_failed_unit() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE items (value INTEGER)")
        enable_runtime_writes(conn)
        with write_transaction(conn):
            conn.execute("INSERT INTO items VALUES (1)")
            with write_savepoint(conn):
                conn.execute("INSERT INTO items VALUES (2)")
                with pytest.raises(KeyboardInterrupt):
                    with write_savepoint(conn):
                        conn.execute("INSERT INTO items VALUES (3)")
                        raise KeyboardInterrupt("abort nested")
                require_write_transaction(conn)
                assert conn.execute("SELECT value FROM items").fetchall() == [(1,), (2,)]
            with pytest.raises(ValueError):
                with write_savepoint(conn):
                    conn.execute("INSERT INTO items VALUES (4)")
                    raise ValueError("abort sibling")
            require_write_transaction(conn)
            conn.execute("INSERT INTO items VALUES (5)")
        assert _query_only(conn)
        assert conn.execute("SELECT value FROM items").fetchall() == [(1,), (2,), (5,)]


def test_standalone_savepoint_owns_write_transaction() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE items (value INTEGER)")
        enable_runtime_writes(conn)
        with write_savepoint(conn):
            require_write_transaction(conn)
            conn.execute("INSERT INTO items VALUES (1)")
        assert _query_only(conn)
        assert not conn.in_transaction
        with pytest.raises(SystemExit):
            with write_savepoint(conn):
                conn.execute("INSERT INTO items VALUES (2)")
                raise SystemExit("abort")
        assert _query_only(conn)
        assert conn.execute("SELECT value FROM items").fetchall() == [(1,)]


def test_savepoint_rejects_foreign_savepoint_untouched() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE items (value INTEGER)")
        conn.execute("SAVEPOINT caller_owned")
        conn.execute("INSERT INTO items VALUES (1)")
        with pytest.raises(RuntimeError, match="idle"):
            with write_savepoint(conn):
                pytest.fail("Foreign savepoint must not be joined")
        assert conn.in_transaction
        assert not _query_only(conn)
        assert conn.execute("SELECT value FROM items").fetchall() == [(1,)]
        conn.execute("ROLLBACK TO caller_owned")
        conn.execute("RELEASE caller_owned")


def test_ended_owned_transaction_fails_closed() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        enable_runtime_writes(conn)
        with pytest.raises(RuntimeError, match="owned"):
            with write_transaction(conn):
                conn.rollback()
                with pytest.raises(RuntimeError, match="idle"):
                    with write_transaction(conn):
                        pytest.fail("Ended outer unit remains registered")
                with pytest.raises(RuntimeError, match="owned"):
                    with write_savepoint(conn):
                        pytest.fail("Cannot join an ended outer unit")
        assert not conn.in_transaction
        assert _query_only(conn)
        with write_savepoint(conn):
            require_write_transaction(conn)


def _deny_savepoint_cleanup(
    action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None
) -> int:
    if action == sqlite3.SQLITE_SAVEPOINT and arg1 in {"ROLLBACK", "RELEASE"}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@pytest.mark.parametrize("abort", [False, True])
def test_savepoint_cleanup_failure_retires_connection(abort: bool) -> None:
    conn = sqlite3.connect(":memory:")
    enable_runtime_writes(conn)
    failure = KeyboardInterrupt("abort")
    expected = KeyboardInterrupt if abort else sqlite3.DatabaseError
    with pytest.raises(expected) as caught:
        with write_transaction(conn):
            with write_savepoint(conn):
                conn.set_authorizer(_deny_savepoint_cleanup)
                if abort:
                    raise failure
    if abort:
        assert caught.value is failure
        assert "savepoint cleanup failed" in failure.__notes__[0]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
