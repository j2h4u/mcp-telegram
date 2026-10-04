"""A retired SQLite writer must stop accepting and report its primary failure."""

import sqlite3
import threading
from pathlib import Path

import pytest

from mcp_telegram import runtime_observations
from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.runtime_observations import RuntimeObservationSink
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import enable_runtime_writes


class _RollbackFailure(sqlite3.Connection):
    def rollback(self) -> None:
        raise sqlite3.OperationalError("injected rollback failure")


def _deny_guard_restore(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "query_only" and arg2 == "1":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@pytest.mark.parametrize(
    ("retirement", "error_type"),
    [
        ("rollback", ValueError),
        ("rollback", sqlite3.OperationalError),
        ("guard_restore", ValueError),
        ("guard_restore", sqlite3.OperationalError),
        ("guard_restore", None),
    ],
)
def test_actual_retirement_stops_writer_and_preserves_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    retirement: str,
    error_type: type[Exception] | None,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE app_work (value INTEGER)")
    setup.close()
    primary = (
        error_type("database is locked" if error_type is sqlite3.OperationalError else "injected primary failure")
        if error_type is not None
        else None
    )
    connections: list[sqlite3.Connection] = []

    def open_writer(_sink: RuntimeObservationSink) -> sqlite3.Connection:
        factory = _RollbackFailure if retirement == "rollback" else sqlite3.Connection
        conn = sqlite3.connect(path, factory=factory)
        enable_runtime_writes(conn)
        connections.append(conn)
        return conn

    def fail(conn: sqlite3.Connection, **_fields: object) -> int:
        conn.execute("INSERT INTO app_work VALUES (1)")
        if retirement == "guard_restore":
            conn.set_authorizer(_deny_guard_restore)
        if primary is not None:
            raise primary
        return 1

    monkeypatch.setattr(RuntimeObservationSink, "_open_writer_connection", open_writer)
    monkeypatch.setattr(runtime_observations, "record_runtime_observation", fail)
    sink = RuntimeObservationSink(path, retention_ttl_seconds=5, policy=RuntimeObservationConfig())
    try:
        assert sink.record(kind="telegram.rpc_admission")
        sink._writer.join(timeout=2)
        assert not sink._writer.is_alive()
        if primary is not None:
            assert sink._writer_error is primary
            assert sink.writer_error == str(primary)
        else:
            assert isinstance(sink._writer_error, sqlite3.DatabaseError)
            assert sink.writer_error is not None
        assert sink._accepting is False
        assert "runtime_observation_writer_failed" in caplog.text
        with pytest.raises(RuntimeError, match="writer failed"):
            sink.record(kind="telegram.rpc_admission")
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            _ = connections[0].in_transaction
        with sqlite3.connect(path) as check:
            assert check.execute("SELECT value FROM app_work").fetchall() == ([(1,)] if primary is None else [])
        check.close()
    finally:
        sink.close()


def test_interrupt_tolerates_retirement_before_writer_reference_is_cleared() -> None:
    conn = sqlite3.connect(":memory:")
    conn.close()
    sink = object.__new__(RuntimeObservationSink)
    sink._connection_lock = threading.Lock()
    sink._writer_connection = conn
    sink._interrupt_writer()
