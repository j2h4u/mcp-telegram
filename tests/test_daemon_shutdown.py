from __future__ import annotations

import asyncio
import signal
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

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
    shutdown_event = register_shutdown_handler(cast(asyncio.AbstractEventLoop, loop))
    assert not shutdown_event.is_set()
    assert loop.call_args is not None
    assert loop.call_args[0] == signal.SIGTERM
    cast(Callable[[], None], loop.call_args[1])()
    assert shutdown_event.is_set()


def test_repeated_shutdown_signal_is_idempotent() -> None:
    loop = _LoopStub()
    shutdown_event = register_shutdown_handler(cast(asyncio.AbstractEventLoop, loop))
    assert loop.call_args is not None
    callback = cast(Callable[[], None], loop.call_args[1])
    callback()
    callback()
    assert shutdown_event.is_set()
