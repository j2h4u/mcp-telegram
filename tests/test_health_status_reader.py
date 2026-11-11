import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.auth_scope import TelegramAuthScope
from tests.test_daemon_api import _make_db, make_server


@pytest.mark.asyncio
async def test_status_reader_stays_owned_after_cancellation_and_shutdown(tmp_path: Path) -> None:
    conn = _make_db(db_path=tmp_path / "sync.db")
    conn.commit()
    server = make_server(conn)
    entered = threading.Event()
    release = threading.Event()
    original = server._get_history_sync_status
    calls: list[int] = []
    loop_thread = threading.get_ident()

    def slow_read(db: sqlite3.Connection, req: dict[str, object]) -> tuple[dict, int | None, int]:
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return original(db, req)

    server._get_history_sync_status = slow_read  # type: ignore[method-assign]

    def snapshot(_channel: int | None) -> dict[str, object]:
        assert threading.get_ident() == loop_thread
        return {"observed_at_ms": 0, "account": {}}

    server._message_delivery_snapshot = snapshot
    first = asyncio.create_task(server._get_sync_status({"dialog_id": 1}))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(server._get_sync_status({"dialog_id": 2}))
    await asyncio.sleep(0.01)
    assert len(calls) == 1
    assert server._get_account_protection({})["ok"]
    closing = asyncio.create_task(server.shutdown())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    with pytest.raises(RuntimeError, match="shutting down"):
        await second
    assert calls[0] != loop_thread


@pytest.mark.asyncio
async def test_status_reader_rejects_account_change_before_delivery(tmp_path: Path) -> None:
    conn = _make_db(db_path=tmp_path / "sync.db")
    conn.commit()
    server = make_server(conn)
    server.self_id = 1
    server._auth_scope = TelegramAuthScope(1, 1, 1, 1)
    entered = threading.Event()
    release = threading.Event()
    original = server._get_history_sync_status

    def slow_read(db: sqlite3.Connection, req: dict[str, object]) -> tuple[dict, int | None, int]:
        entered.set()
        assert release.wait(10)
        return original(db, req)

    server._get_history_sync_status = slow_read  # type: ignore[method-assign]
    server._message_delivery_snapshot = lambda _channel: pytest.fail("delivery must not run after account change")
    task = asyncio.create_task(server._get_sync_status({"dialog_id": 1}))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    server.self_id = 2
    release.set()
    result = await task
    assert result["error"] == "account_unverified"
    await server.shutdown()


@pytest.mark.asyncio
async def test_status_reads_different_dialogs_separately_on_worker(tmp_path: Path) -> None:
    conn = _make_db(db_path=tmp_path / "sync.db")
    conn.commit()
    server = make_server(conn)
    entered, release = threading.Event(), threading.Event()
    original = server._get_history_sync_status
    reads: list[object] = []
    loop_thread = threading.get_ident()
    callbacks: list[int] = []

    def slow_read(db: sqlite3.Connection, req: dict[str, object]) -> tuple[dict, int | None, int]:
        reads.append(req["dialog_id"])
        if len(reads) == 1:
            entered.set()
            assert release.wait(10)
        return original(db, req)

    def snapshot(_channel: int | None) -> dict[str, object]:
        callbacks.append(threading.get_ident())
        return {"observed_at_ms": 0, "account": {}}

    server._get_history_sync_status = slow_read  # type: ignore[method-assign]
    server._message_delivery_snapshot = snapshot
    first = asyncio.create_task(server._get_sync_status({"dialog_id": 1}))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    second = asyncio.create_task(server._get_sync_status({"dialog_id": 2}))
    await asyncio.sleep(0.01)
    assert reads == [1]
    release.set()
    one, two = await asyncio.gather(first, second)
    assert one["data"]["dialog_id"] == 1
    assert two["data"]["dialog_id"] == 2
    assert reads == [1, 2]
    assert callbacks == [loop_thread, loop_thread]
    await server.shutdown()


@pytest.mark.asyncio
async def test_status_reads_current_database_with_real_writer(tmp_path: Path) -> None:
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.sync_db import ensure_sync_schema

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    writer = await SQLiteWriter.open(path)
    server = make_server(conn, writer=writer)
    loop_thread = threading.get_ident()

    def snapshot(_channel: int | None) -> dict[str, object]:
        assert threading.get_ident() == loop_thread
        return {"observed_at_ms": 0, "account": {}}

    server._message_delivery_snapshot = snapshot
    try:
        result = await server._dispatch({"method": "get_sync_status"})
        assert result["ok"] is True
        data = cast(dict[str, object], result["data"])
        assert data["dialog_id"] == 0
        assert cast(dict[str, object], data["message_delivery"])["observed_at_ms"] == 0
    finally:
        await server.shutdown()
        await writer.aclose()
        conn.close()
