"""Usage statistics run in an owned reader without blocking daemon requests."""

import asyncio
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram import daemon_activity_stats as stats_module
from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.sync_db import _RUNTIME_OBSERVATIONS_V54_DDL
from tests.test_daemon_api import (
    _close_tracked_sqlite_connections,  # noqa: F401 -- shared helper connection cleanup fixture
    _make_db_with_entities,
    make_server,
)


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    path = tmp_path / "sync.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_RUNTIME_OBSERVATIONS_V54_DDL.replace("runtime_observations_v54", "runtime_observations"))
    conn.execute("ALTER TABLE runtime_observations ADD COLUMN tool_capability TEXT")
    conn.execute("ALTER TABLE runtime_observations ADD COLUMN contract_version INTEGER")
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO daemon_state VALUES (?, ?)",
        [
            ("runtime_observations_history_started_at_ms", "100000"),
            ("runtime_observations_last_cap_truncation_ms", "200000"),
            ("runtime_observations_legacy_started_at_ms", "1000"),
            ("runtime_observations_legacy_ended_at_ms", "90000"),
        ],
    )
    conn.executemany(
        "INSERT INTO runtime_observations (observed_at_ms, kind, runtime_instance_id, tool_name, "
        "tool_capability, contract_version, reason_code, error_type, page_depth, has_filter, duration_ms) "
        "VALUES (?, ?, 'test', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (100000, "mcp.call", "old", None, None, None, None, 1, 0, 5),
            (200000, "mcp.call", "reading", "history", 2, "denied", None, 4, 1, 10),
            (201000, "mcp.call", "reading", None, None, "", "timeout", 2, 0, 30),
            (202000, "rpc", "ignored", None, None, None, None, 9, 1, 100),
        ],
    )
    conn.commit()
    try:
        yield conn, path
    finally:
        conn.close()


def _server(path: Path) -> DaemonAPIServer:
    server = make_server()
    server._sync_db_path = path
    return server


async def test_file_reader_preserves_complete_projection(archive: tuple[sqlite3.Connection, Path]) -> None:
    conn, path = archive
    expected = {
        "tool_distribution": {"reading": 2},
        "capability_distribution": {"history": 1, "reading": 1},
        "contract_distribution": {"history:v2": 1, "reading:v0": 1},
        "error_distribution": {"denied": 1, "timeout": 1},
        "max_page_depth": 4,
        "total_calls": 2,
        "filter_count": 1,
        "latency_median_ms": 30,
        "latency_p95_ms": 30,
        "history_started_at_ms": 100000,
        "last_cap_truncation_ms": 200000,
        "history_segments": [
            {"kind": "legacy", "started_at_ms": 1000, "ended_at_ms": 90000},
            {"kind": "live", "started_at_ms": 100000, "ended_at_ms": None},
        ],
    }
    result = await _server(path)._dispatch({"method": "get_usage_stats", "since": "200"})
    assert result == {"ok": True, "data": expected}
    assert stats_module._query_usage_stats(conn.cursor(), 200) == expected


async def test_reader_owns_open_query_close_and_rejects_writes(
    archive: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path = archive
    main_thread = threading.get_ident()
    threads: list[int] = []
    connect = sqlite3.connect
    query = stats_module._query_usage_stats

    class TrackedConnection(sqlite3.Connection):
        def close(self) -> None:
            threads.append(threading.get_ident())
            super().close()

    def tracked_open(db_path: Path) -> sqlite3.Connection:
        threads.append(threading.get_ident())
        return connect(f"file:{db_path}?mode=ro", uri=True, factory=TrackedConnection)

    def checked_query(cursor: sqlite3.Cursor, since: int) -> dict[str, object]:
        threads.append(threading.get_ident())
        assert cursor.connection.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            cursor.execute("DELETE FROM runtime_observations")
        return query(cursor, since)

    monkeypatch.setattr(stats_module, "open_sync_db_reader", tracked_open)
    monkeypatch.setattr(stats_module, "_query_usage_stats", checked_query)
    assert (await _server(path)._dispatch({"method": "get_usage_stats", "since": 200}))["ok"]
    assert len(threads) == 3
    assert len(set(threads)) == 1
    assert threads[0] != main_thread


async def test_usage_projection_keeps_one_snapshot_during_wal_write(
    archive: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, path = archive
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    query = stats_module._query_usage_stats
    expected = query(writer.cursor(), 200)
    main_thread = threading.get_ident()

    def paused_query(cursor: sqlite3.Cursor, since: int) -> dict[str, object]:
        assert threading.get_ident() != main_thread
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            cursor.execute("DELETE FROM runtime_observations")
        cursor.execute("SELECT COUNT(*) FROM runtime_observations").fetchone()
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return query(cursor, since)

    monkeypatch.setattr(stats_module, "_query_usage_stats", paused_query)
    task = asyncio.create_task(_server(path)._dispatch({"method": "get_usage_stats", "since": 200}))
    try:
        await asyncio.wait_for(started.wait(), 2)
        writer.execute("DELETE FROM runtime_observations")
        writer.execute("UPDATE daemon_state SET value='999999'")
        writer.commit()
    finally:
        release.set()
        result = await asyncio.wait_for(task, 2)
    assert result == {"ok": True, "data": expected}
    assert query(writer.cursor(), 200) != expected


@pytest.mark.parametrize("cancel", [False, True])
async def test_slow_usage_keeps_daemon_responsive_and_closes_after_completion(
    archive: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    _, path = archive
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    started, closed, heartbeat = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = threading.Event()
    query = stats_module._query_usage_stats
    connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            loop.call_soon_threadsafe(closed.set)

    def tracked_open(db_path: Path) -> sqlite3.Connection:
        return connect(f"file:{db_path}?mode=ro", uri=True, factory=TrackedConnection)

    def paused_query(cursor: sqlite3.Cursor, since: int) -> dict[str, object]:
        assert threading.get_ident() != main_thread
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return query(cursor, since)

    monkeypatch.setattr(stats_module, "open_sync_db_reader", tracked_open)
    monkeypatch.setattr(stats_module, "_query_usage_stats", paused_query)
    server = _server(path)
    task = asyncio.create_task(server._dispatch({"method": "get_usage_stats", "since": 200}))
    try:
        await asyncio.wait_for(started.wait(), 2)
        loop.call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 2)
        response = await asyncio.wait_for(server._dispatch({"method": "get_account_protection"}), 2)
        assert response["ok"] is True
        assert not task.done()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not closed.is_set()
    finally:
        release.set()
        await asyncio.wait_for(closed.wait(), 2)
        if not task.cancelled():
            assert (await asyncio.wait_for(task, 2))["ok"]


@pytest.mark.parametrize("failure", ["open", "begin", "query"])
async def test_usage_failure_preserves_error_envelope_and_closes_reader(
    archive: tuple[sqlite3.Connection, Path], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _, path = archive
    closed: list[bool] = []

    class TrackedConnection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                self.execute("SELECT 1")
            closed.append(True)

    def failing_open(db_path: Path) -> sqlite3.Connection:
        if failure == "open":
            raise sqlite3.OperationalError("private database detail")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, factory=TrackedConnection)
        if failure == "begin":
            conn.set_authorizer(lambda *_args: sqlite3.SQLITE_DENY)
        return conn

    def failing_query(_cursor: sqlite3.Cursor, _since: int) -> dict[str, object]:
        raise sqlite3.OperationalError("private database detail")

    monkeypatch.setattr(stats_module, "open_sync_db_reader", failing_open)
    if failure == "query":
        monkeypatch.setattr(stats_module, "_query_usage_stats", failing_query)
    result = await _server(path)._dispatch({"method": "get_usage_stats", "since": 200})
    assert result == {"ok": False, "error": "internal", "message": "internal error"}
    assert closed == ([] if failure == "open" else [True])


async def test_in_memory_usage_keeps_existing_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_reader(_path: Path) -> sqlite3.Connection:
        pytest.fail("in-memory calls must not open a file reader")

    monkeypatch.setattr(stats_module, "open_sync_db_reader", unexpected_reader)
    result = await make_server(_make_db_with_entities())._dispatch({"method": "get_usage_stats", "since": "invalid"})
    assert result["ok"] is True
    assert cast(dict[str, object], result["data"])["total_calls"] == 0
