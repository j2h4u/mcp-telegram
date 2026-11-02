from __future__ import annotations

import asyncio
import signal
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.daemon_shutdown import register_shutdown_handler
from mcp_telegram.sqlite_checkpoint import checkpoint_sqlite_connection
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


class _LoopStub:
    def __init__(self) -> None:
        self.call_args: tuple[int, object] | None = None

    def add_signal_handler(self, signal_num: int, callback: object) -> None:
        self.call_args = (signal_num, callback)


def test_checkpoint_rolls_back_pending_work_and_preserves_integrity(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
        conn.commit()
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (2, 'syncing')")

        checkpoint_sqlite_connection(conn)

        reopened = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            integrity = cast(tuple[object, ...] | None, reopened.execute("PRAGMA integrity_check").fetchone())
            assert integrity is not None and str(integrity[0]).lower() == "ok"
            rows = reopened.execute("SELECT dialog_id, status FROM synced_dialogs ORDER BY dialog_id").fetchall()
            assert rows == [(1, "synced")]
        finally:
            reopened.close()
    finally:
        conn.close()


def test_signal_requests_shutdown_without_touching_sqlite() -> None:
    loop = _LoopStub()
    shutdown = register_shutdown_handler(cast(asyncio.AbstractEventLoop, loop))
    assert not shutdown.event.is_set()
    assert loop.call_args is not None
    assert loop.call_args[0] == signal.SIGTERM
    cast(Callable[[], None], loop.call_args[1])()
    assert shutdown.event.is_set()


def test_repeated_shutdown_signal_is_idempotent() -> None:
    loop = _LoopStub()
    shutdown = register_shutdown_handler(cast(asyncio.AbstractEventLoop, loop))
    assert loop.call_args is not None
    callback = cast(Callable[[], None], loop.call_args[1])
    callback()
    callback()
    assert shutdown.event.is_set()


def test_first_observed_cause_wins() -> None:
    loop = _LoopStub()
    shutdown = register_shutdown_handler(cast(asyncio.AbstractEventLoop, loop))
    shutdown.request("critical_task_failed")
    assert loop.call_args is not None
    cast(Callable[[], None], loop.call_args[1])()
    assert shutdown.reason_code == "critical_task_failed"


async def test_signal_during_bootstrap_waits_and_skips_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from mcp_telegram import daemon

    entered = asyncio.Event()
    release = asyncio.Event()
    shutdown = AsyncMock()
    start = AsyncMock()
    loop = asyncio.get_running_loop()
    callbacks: dict[int, Callable[[], None]] = {}
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: callbacks.update({sig: cb}))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)

    async def build(_request: object) -> object:
        entered.set()
        await release.wait()
        return object()

    monkeypatch.setattr(daemon, "_build_sync_main_context", build)
    monkeypatch.setattr(daemon, "_shutdown_sync_main_context", shutdown)
    monkeypatch.setattr(daemon, "_connect_telegram", start)
    task = asyncio.create_task(daemon.sync_main())
    await entered.wait()
    callbacks[signal.SIGTERM]()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    await task
    shutdown.assert_awaited_once()
    start.assert_not_awaited()
