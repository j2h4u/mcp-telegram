"""Tests for the shared `apply_read_cursor` primitive (Phase 39.3-01 Task 1).

Covers single-owner monotonic-write behaviour for both `inbox` and `outbox`
read cursors on `synced_dialogs`. See 39.3-01-PLAN.md <tasks> for the full
behavioural contract.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator

import pytest


def _create_synced_dialogs(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE synced_dialogs (
            dialog_id           INTEGER PRIMARY KEY,
            read_inbox_max_id   INTEGER,
            read_outbox_max_id  INTEGER,
            status              TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, read_inbox_max_id, read_outbox_max_id, status) "
        "VALUES (?, NULL, NULL, 'synced')",
        (111,),
    )
    conn.commit()


@pytest.fixture()
def mem_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    _create_synced_dialogs(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def file_db_path() -> Iterator[Path]:
    fd, name = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _read_cursors(conn: sqlite3.Connection, dialog_id: int) -> tuple[object, object]:
    row = conn.execute(
        "SELECT read_inbox_max_id, read_outbox_max_id FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else (None, None)


def testapply_read_cursor_inbox_writes_value(mem_conn: sqlite3.Connection) -> None:
    from mcp_telegram.read_state import apply_read_cursor

    apply_read_cursor(mem_conn, 111, "inbox", 42)
    mem_conn.commit()
    inbox, outbox = _read_cursors(mem_conn, 111)
    assert inbox == 42
    assert outbox is None


def testapply_read_cursor_outbox_writes_value(mem_conn: sqlite3.Connection) -> None:
    from mcp_telegram.read_state import apply_read_cursor

    apply_read_cursor(mem_conn, 111, "outbox", 99)
    mem_conn.commit()
    inbox, outbox = _read_cursors(mem_conn, 111)
    assert inbox is None
    assert outbox == 99


def testapply_read_cursor_inbox_does_not_touch_outbox(mem_conn: sqlite3.Connection) -> None:
    from mcp_telegram.read_state import apply_read_cursor

    apply_read_cursor(mem_conn, 111, "inbox", 5)
    mem_conn.commit()
    _, outbox = _read_cursors(mem_conn, 111)
    assert outbox is None


def testapply_read_cursor_outbox_does_not_touch_inbox(mem_conn: sqlite3.Connection) -> None:
    from mcp_telegram.read_state import apply_read_cursor

    apply_read_cursor(mem_conn, 111, "outbox", 7)
    mem_conn.commit()
    inbox, _ = _read_cursors(mem_conn, 111)
    assert inbox is None


def testapply_read_cursor_monotonic_inbox(mem_conn: sqlite3.Connection) -> None:
    from mcp_telegram.read_state import apply_read_cursor

    apply_read_cursor(mem_conn, 111, "inbox", 100)
    apply_read_cursor(mem_conn, 111, "inbox", 50)
    mem_conn.commit()
    inbox, _ = _read_cursors(mem_conn, 111)
    assert inbox == 100  # regression rejected


def testapply_read_cursor_monotonic_outbox(mem_conn: sqlite3.Connection) -> None:
    from mcp_telegram.read_state import apply_read_cursor

    apply_read_cursor(mem_conn, 111, "outbox", 100)
    apply_read_cursor(mem_conn, 111, "outbox", 33)
    mem_conn.commit()
    _, outbox = _read_cursors(mem_conn, 111)
    assert outbox == 100


def testapply_read_cursor_null_then_value_inbox(mem_conn: sqlite3.Connection) -> None:
    from mcp_telegram.read_state import apply_read_cursor

    inbox_before, _ = _read_cursors(mem_conn, 111)
    assert inbox_before is None
    apply_read_cursor(mem_conn, 111, "inbox", 42)
    mem_conn.commit()
    inbox_after, _ = _read_cursors(mem_conn, 111)
    assert inbox_after == 42


def testapply_read_cursor_bad_kind_raises(mem_conn: sqlite3.Connection) -> None:
    from mcp_telegram.read_state import apply_read_cursor

    with pytest.raises(KeyError):
        apply_read_cursor(mem_conn, 111, "garbage", 1)  # type: ignore[arg-type]


def testapply_read_cursor_unknown_dialog_id_is_noop(mem_conn: sqlite3.Connection) -> None:
    from mcp_telegram.read_state import apply_read_cursor

    # UPDATE on missing row: affects 0 rows, no exception.
    apply_read_cursor(mem_conn, 999_999, "inbox", 10)
    mem_conn.commit()
    row = mem_conn.execute(
        "SELECT dialog_id FROM synced_dialogs WHERE dialog_id=?", (999_999,)
    ).fetchone()
    assert row is None


def testapply_read_cursor_caller_controls_transaction(file_db_path: Path) -> None:
    """File-backed two-connection test: helper must NOT auto-commit.

    Connection A calls the helper and does NOT commit. Connection B (separate
    sqlite3.connect) must still see the OLD value. After A commits, B sees new.
    This proves the caller owns the transaction boundary.
    """
    from mcp_telegram.read_state import apply_read_cursor

    # Seed schema + row with initial value via a dedicated connection.
    seeder = sqlite3.connect(str(file_db_path), timeout=5.0)
    try:
        _create_synced_dialogs(seeder)
        seeder.execute(
            "UPDATE synced_dialogs SET read_inbox_max_id = 10 WHERE dialog_id=?",
            (111,),
        )
        seeder.commit()
    finally:
        seeder.close()

    conn_a = sqlite3.connect(str(file_db_path), timeout=5.0, isolation_level="DEFERRED")
    conn_b = sqlite3.connect(str(file_db_path), timeout=5.0)
    try:
        # Connection A writes via helper — does NOT commit.
        apply_read_cursor(conn_a, 111, "inbox", 77)

        # Connection B sees the OLD value (uncommitted write is invisible).
        row = conn_b.execute(
            "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id=?",
            (111,),
        ).fetchone()
        assert row[0] == 10, "helper must not auto-commit — B should see old value"

        # Now A commits — B sees the new value on a fresh read.
        conn_a.commit()
        # Start a new read txn on B to force re-read.
        conn_b.rollback()
        row = conn_b.execute(
            "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id=?",
            (111,),
        ).fetchone()
        assert row[0] == 77
    finally:
        conn_a.close()
        conn_b.close()


def test_read_state_module_importable_by_daemon_and_event_handlers() -> None:
    """Import-graph regression: both consumers can import the helper.

    Phase 39.3-01 Task 2 wires daemon.py + event_handlers.py through this
    module. A circular import would surface here as ImportError.
    """
    from mcp_telegram.read_state import apply_read_cursor  # noqa: F401
    from mcp_telegram import daemon, event_handlers  # noqa: F401
