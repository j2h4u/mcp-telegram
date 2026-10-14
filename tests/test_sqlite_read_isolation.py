"""File-backed inbox/activity projections leave the daemon writer and loop free."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from mcp_telegram import daemon_activity_stats
from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.reading import service as reading_service
from test_daemon_api import _make_db_with_activity, _seed_unread_dialog, _seed_unread_state, make_server


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, sqlite3.Connection, Path]]:
    source = _make_db_with_activity()
    now = int(time.time())
    for dialog_id, kind, members in ((101, "user", None), (-102, "supergroup", 20), (-103, "channel", None)):
        _seed_unread_state(source, dialog_id, 0, entity_type=kind)
        _seed_unread_dialog(source, dialog_id, name=f"Peer {dialog_id}", type_=kind, members=members)
        source.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at,text,sender_id,out,is_service,is_deleted) "
            "VALUES (?,?,?,?,?,?,0,0)",
            [(dialog_id, 1, now - 30, "incoming", dialog_id, 0), (dialog_id, 2, now - 20, "own", 999, 1)],
        )
    _seed_unread_state(source, 104, None, entity_type="user")
    _seed_unread_dialog(source, 104, name="Pending", type_="user")
    source.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
    source.commit()
    path = tmp_path / "sync.db"
    writer = sqlite3.connect(path)
    source.backup(writer)
    writer.execute("PRAGMA journal_mode=WAL")
    try:
        yield source, writer, path
    finally:
        source.close()
        writer.close()


def _operation(server: DaemonAPIServer, method: str) -> Callable[[dict], Coroutine[None, None, dict]]:
    if method == "get_inbox":
        return server._get_reading_service().list_unread_messages
    return server._get_activity_stats_service().get_my_recent_activity


def _projection_target(method: str) -> tuple[ModuleType, type, str]:
    if method == "get_inbox":
        return reading_service, reading_service.ReadingService, "_list_unread_messages_sync"
    return daemon_activity_stats, daemon_activity_stats.DaemonActivityStatsService, "_recent_activity_sync"


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("get_inbox", {"limit": 1, "dialogs_per_page": 1}),
        ("get_inbox", {"page": 2, "dialogs_per_page": 1}),
        ("get_inbox", {"include_dialog_types": ["channel"]}),
        ("get_inbox", {"include_dialog_types": ["user"], "since_utc": "2000-01-01T00:00:00Z"}),
        ("get_my_recent_activity", {"limit": 1}),
        ("get_my_recent_activity", {"dialog_kinds": ["all"]}),
        ("get_my_recent_activity", {"dialog_kinds": ["user"], "text_query": "own"}),
    ],
)
async def test_file_read_matches_memory_without_writer_connection(
    archive: tuple[sqlite3.Connection, sqlite3.Connection, Path], method: str, arguments: dict
) -> None:
    source, writer, _path = archive
    expected = await _operation(make_server(source), method)(arguments)
    operation = _operation(make_server(writer), method)
    writer.close()
    assert await operation(arguments) == expected


@pytest.mark.parametrize("method", ["get_inbox", "get_my_recent_activity"])
async def test_memory_projection_does_not_spawn_reader(
    archive: tuple[sqlite3.Connection, sqlite3.Connection, Path], method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _writer, _path = archive

    async def unexpected_reader(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("in-memory projection must stay on its connection")

    monkeypatch.setattr(asyncio, "to_thread", unexpected_reader)
    result = await _operation(make_server(source), method)({})
    assert result["ok"] is True


@pytest.mark.parametrize("method", ["get_inbox", "get_my_recent_activity"])
async def test_reader_failure_closes_readonly_snapshot(
    archive: tuple[sqlite3.Connection, sqlite3.Connection, Path], method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, writer, _path = archive
    _module, service_type, projection_name = _projection_target(method)
    readers: list[sqlite3.Connection] = []

    def fail_projection(conn: sqlite3.Connection, _request: object) -> dict:
        readers.append(conn)
        assert conn.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM messages")
        raise RuntimeError("projection failed")

    monkeypatch.setattr(service_type, projection_name, staticmethod(fail_projection))
    with pytest.raises(RuntimeError, match="projection failed"):
        await _operation(make_server(writer), method)({})
    assert len(readers) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        _ = readers[0].in_transaction


@pytest.mark.parametrize("method", ["get_inbox", "get_my_recent_activity"])
async def test_projection_uses_one_snapshot_while_daemon_writes(
    archive: tuple[sqlite3.Connection, sqlite3.Connection, Path], method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, writer, _path = archive
    expected = await _operation(make_server(source), method)({})
    server = make_server(writer)
    _module, service_type, projection_name = _projection_target(method)
    inbox_projection = server._get_reading_service()._list_unread_messages_sync
    activity_projection = server._get_activity_stats_service()._recent_activity_sync
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def project_snapshot(conn: sqlite3.Connection, request: object) -> dict:
        conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        if isinstance(request, daemon_activity_stats._RecentActivityRequest):
            return activity_projection(conn, request)
        assert isinstance(request, dict)
        return inbox_projection(conn, cast(dict[str, object], request))

    monkeypatch.setattr(service_type, projection_name, staticmethod(project_snapshot))
    task = asyncio.create_task(_operation(server, method)({}))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        writer.execute("UPDATE messages SET text='concurrent change'")
        writer.commit()
        release.set()
        assert await task == expected
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.parametrize("method", ["get_inbox", "get_my_recent_activity"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_slow_reader_keeps_loop_responsive_and_closes_after_cancellation(
    archive: tuple[sqlite3.Connection, sqlite3.Connection, Path],
    method: str,
    cancel: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, writer, path = archive
    module, service_type, projection_name = _projection_target(method)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    closed = threading.Event()
    loop_thread = threading.get_ident()

    class TrackedConnection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            closed.set()

    def reader(db_path: Path) -> sqlite3.Connection:
        assert db_path == path
        assert threading.get_ident() != loop_thread
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, factory=TrackedConnection)

    def slow_projection(conn: sqlite3.Connection, _request: object) -> dict:
        assert conn.in_transaction
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return {"ok": True}

    monkeypatch.setattr(module, "open_sync_db_reader", reader)
    monkeypatch.setattr(service_type, projection_name, staticmethod(slow_projection))
    task = asyncio.create_task(_operation(make_server(writer), method)({}))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0)
        assert not task.done()
        assert not closed.is_set()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()
        if not cancel:
            assert await task == {"ok": True}
        assert await asyncio.to_thread(closed.wait, 2)
    finally:
        release.set()
        if not task.done():
            await task


def test_activity_peer_discovery_searches_recent_own_index(
    archive: tuple[sqlite3.Connection, sqlite3.Connection, Path],
) -> None:
    _source, writer, _path = archive
    writer.execute(
        "CREATE INDEX idx_messages_own_activity_sent ON messages(sent_at DESC,dialog_id DESC,message_id DESC) "
        "WHERE out=1 AND is_service=0 AND is_deleted=0"
    )
    writer.execute(
        "CREATE INDEX idx_messages_dialog_summary ON messages(dialog_id,is_deleted,is_service,out,message_id)"
    )
    plans: list[str] = []

    def collect_plan(statement: str) -> None:
        if statement.startswith("SELECT DISTINCT m.dialog_id"):
            rows = cast(list[tuple[int, int, int, str]], writer.execute("EXPLAIN QUERY PLAN " + statement).fetchall())
            plans.extend(row[3] for row in rows)

    writer.set_trace_callback(collect_plan)
    try:
        ids = daemon_activity_stats._matching_activity_dialog_ids(
            writer,
            "m.out=1 AND m.is_service=0 AND m.is_deleted=0 AND m.sent_at>=?",
            [int(time.time()) - 3600],
        )
    finally:
        writer.set_trace_callback(None)
    assert set(ids) == {101, -102, -103}
    assert any("SEARCH m USING INDEX idx_messages_own_activity_sent (sent_at>?)" in plan for plan in plans)
    assert not any("SCAN m" in plan for plan in plans)
