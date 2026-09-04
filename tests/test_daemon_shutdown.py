from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

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


def test_shutdown_checkpoints_sync_before_feedback_and_sets_event_after_attempts() -> None:
    sync_conn = MagicMock(spec=sqlite3.Connection)
    feedback_conn = MagicMock(spec=sqlite3.Connection)
    loop = _LoopStub()
    order: list[object] = []

    def checkpoint(conn: sqlite3.Connection) -> None:
        order.append(conn)

    with patch("mcp_telegram.daemon_shutdown.checkpoint_sqlite_connection", side_effect=checkpoint):
        shutdown_event = register_shutdown_handler(
            sync_conn,
            cast(asyncio.AbstractEventLoop, loop),
            feedback_conn=feedback_conn,
        )
        assert loop.call_args is not None
        cast(Callable[[], None], loop.call_args[1])()

    assert order == [sync_conn, feedback_conn]
    assert shutdown_event.is_set()


def test_shutdown_checkpoint_failures_are_isolated_and_event_is_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sync_conn = MagicMock(spec=sqlite3.Connection)
    feedback_conn = MagicMock(spec=sqlite3.Connection)
    loop = _LoopStub()
    checkpoint_calls: list[object] = []

    def checkpoint(conn: sqlite3.Connection) -> None:
        checkpoint_calls.append(conn)
        raise sqlite3.OperationalError("busy")

    with (
        patch("mcp_telegram.daemon_shutdown.checkpoint_sqlite_connection", side_effect=checkpoint),
        caplog.at_level(logging.ERROR, logger="mcp_telegram.daemon_shutdown"),
    ):
        shutdown_event = register_shutdown_handler(
            sync_conn,
            cast(asyncio.AbstractEventLoop, loop),
            feedback_conn=feedback_conn,
        )
        assert loop.call_args is not None
        cast(Callable[[], None], loop.call_args[1])()

    assert checkpoint_calls == [sync_conn, feedback_conn]
    assert shutdown_event.is_set()
    assert "sync.db shutdown error" in caplog.messages
    assert "feedback.db shutdown error (suppressed — shutdown continues)" in caplog.messages


def test_shutdown_without_feedback_only_checkpoints_sync() -> None:
    sync_conn = MagicMock(spec=sqlite3.Connection)
    loop = _LoopStub()

    with patch("mcp_telegram.daemon_shutdown.checkpoint_sqlite_connection") as checkpoint:
        shutdown_event = register_shutdown_handler(sync_conn, cast(asyncio.AbstractEventLoop, loop))
        assert loop.call_args is not None
        cast(Callable[[], None], loop.call_args[1])()

    checkpoint.assert_called_once_with(sync_conn)
    assert shutdown_event.is_set()
