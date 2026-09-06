from __future__ import annotations

import sqlite3

import pytest

from mcp_telegram.access_lifecycle import restore_access_after_revalidation, set_access_lost, stamp_access_revalidation
from tests.history_enrollment_helpers import seed_full_history_enrollment


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """CREATE TABLE synced_dialogs (
             dialog_id INTEGER PRIMARY KEY, status TEXT, access_lost_at INTEGER,
             delta_refresh_requested_at INTEGER, access_last_revalidated_at INTEGER,
             access_next_revalidate_at INTEGER, total_messages INTEGER,
             read_position_next_attempt_at INTEGER, read_position_attempt_count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE full_history_enrollment (
             dialog_id INTEGER PRIMARY KEY,
             enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
             source TEXT NOT NULL CHECK(source IN ('explicit', 'automatic', 'migration')),
             updated_at INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE dialogs (
             dialog_id INTEGER PRIMARY KEY, hidden INTEGER, needs_refresh INTEGER,
             snapshot_at INTEGER, archived INTEGER, pinned INTEGER,
             unread_mentions_count INTEGER, unread_reactions_count INTEGER, name TEXT);
        CREATE TABLE sync_alert_events (
             seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, occurred_at INTEGER,
             dialog_id INTEGER, message_id INTEGER, version INTEGER);
        CREATE TABLE runtime_events (
             id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL,
             kind TEXT NOT NULL, runtime_instance_id TEXT NOT NULL, operation_id TEXT,
             outcome TEXT, reason_code TEXT, dialog_id INTEGER, duration_ms REAL,
             tool_name TEXT, result_count INTEGER, has_cursor INTEGER, page_depth INTEGER,
             has_filter INTEGER, error_type TEXT, payload_json TEXT NOT NULL DEFAULT '{}');
        """
    )
    return conn


def test_nested_lifecycle_savepoint_preserves_outer_write() -> None:
    conn = _db()
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
    seed_full_history_enrollment(conn, 1, enabled=True)
    conn.execute("INSERT INTO dialogs VALUES (1, 0, 0, 1, 0, 0, 0, 0, 'x')")
    conn.execute("CREATE TABLE unrelated (value INTEGER)")
    conn.commit()
    conn.execute("INSERT INTO unrelated VALUES (7)")

    try:
        set_access_lost(conn, 1, 10)
        stamp_access_revalidation(conn, 1, 11, 20)
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM unrelated").fetchone() == (0,)
        assert conn.execute("SELECT status FROM synced_dialogs").fetchone() == ("synced",)
    finally:
        conn.close()


def test_access_restore_clears_read_position_retry() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_position_next_attempt_at, read_position_attempt_count) "
        "VALUES (2, 'access_lost', 999, 3)"
    )
    seed_full_history_enrollment(conn, 2, enabled=True)
    conn.execute("INSERT INTO dialogs VALUES (2, 1, 0, 1, 0, 0, 0, 0, 'x')")
    conn.commit()
    try:
        restore_access_after_revalidation(conn, 2, 10)
        assert conn.execute(
            "SELECT read_position_next_attempt_at, read_position_attempt_count FROM synced_dialogs WHERE dialog_id=2"
        ).fetchone() == (None, 0)
    finally:
        conn.close()


def test_access_loss_clears_read_position_retry() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_position_next_attempt_at, read_position_attempt_count) "
        "VALUES (3, 'synced', 999, 3)"
    )
    conn.execute("INSERT INTO dialogs VALUES (3, 0, 0, 1, 0, 0, 0, 0, 'x')")
    conn.commit()
    try:
        set_access_lost(conn, 3, 10)
        assert conn.execute(
            "SELECT status, read_position_next_attempt_at, read_position_attempt_count "
            "FROM synced_dialogs WHERE dialog_id=3"
        ).fetchone() == ("access_lost", None, 0)
    finally:
        conn.close()


def test_runtime_event_failure_does_not_roll_back_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db()
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
    seed_full_history_enrollment(conn, 1, enabled=True)
    conn.execute("INSERT INTO dialogs VALUES (1, 0, 0, 1, 0, 0, 0, 0, 'x')")
    conn.execute("CREATE TABLE unrelated (value INTEGER)")
    conn.commit()
    conn.execute("INSERT INTO unrelated VALUES (7)")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected")

    try:
        monkeypatch.setattr("mcp_telegram.access_lifecycle.record_runtime_event", fail)
        set_access_lost(conn, 1, 10)
        assert conn.execute("SELECT status FROM synced_dialogs").fetchone() == ("access_lost",)
        assert conn.execute("SELECT hidden FROM dialogs").fetchone() == (1,)
        assert conn.execute("SELECT value FROM unrelated").fetchone() == (7,)
        conn.rollback()
    finally:
        conn.close()
