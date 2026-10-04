import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

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


def test_deferred_foreign_key_commit_failure_rolls_back() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (parent_id REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
        enable_runtime_writes(conn)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            with write_transaction(conn):
                conn.execute("INSERT INTO child VALUES (42)")
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


def test_escaped_read_cursor_busy_snapshot_entry_restores_guard(tmp_path: Path) -> None:
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
        with pytest.raises(sqlite3.OperationalError) as caught:
            with write_transaction(conn):
                pytest.fail("Stale retained cursor must fail at entry")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
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


def test_rollback_failure_retires_connection_preserving_primary() -> None:
    conn = sqlite3.connect(":memory:", factory=_RollbackFailure)
    enable_runtime_writes(conn)
    failure = KeyboardInterrupt("abort")
    with pytest.raises(KeyboardInterrupt) as caught:
        with write_transaction(conn):
            raise failure
    assert caught.value is failure
    assert "rollback failed" in failure.__notes__[0]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def _deny_guard_restore(action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None) -> int:
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "query_only" and arg2 == "1":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@pytest.mark.parametrize("abort", [False, True])
def test_restoration_failure_retires_connection(abort: bool) -> None:
    conn = sqlite3.connect(":memory:")
    enable_runtime_writes(conn)
    failure = KeyboardInterrupt("abort")
    expected = KeyboardInterrupt if abort else sqlite3.DatabaseError
    with pytest.raises(expected) as caught:
        with write_transaction(conn):
            conn.set_authorizer(_deny_guard_restore)
            if abort:
                raise failure
    if abort:
        assert caught.value is failure
        assert "Restoring" in failure.__notes__[0]
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
