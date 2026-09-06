from __future__ import annotations

import sqlite3

import pytest

from mcp_telegram.access_lifecycle import (
    AccessLossEvidence,
    restore_access_after_revalidation,
    set_access_lost,
    stamp_access_revalidation,
)
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
        CREATE TABLE conversation_history_events (
             seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, occurred_at INTEGER,
             time_basis TEXT, dialog_id INTEGER, message_id INTEGER, version INTEGER,
             reason_code TEXT, previous_status TEXT,
             source_namespace TEXT, source_event_id INTEGER,
             access_change_cause TEXT, actor_id INTEGER);
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


def test_durable_event_failure_rolls_back_lifecycle() -> None:
    conn = _db()
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
    seed_full_history_enrollment(conn, 1, enabled=True)
    conn.execute("INSERT INTO dialogs VALUES (1, 0, 0, 1, 0, 0, 0, 0, 'x')")
    conn.commit()
    conn.execute(
        "CREATE TRIGGER reject_history BEFORE INSERT ON conversation_history_events "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )

    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            set_access_lost(conn, 1, 10)
        assert conn.execute("SELECT status FROM synced_dialogs").fetchone() == ("synced",)
        assert conn.execute("SELECT hidden FROM dialogs").fetchone() == (0,)
    finally:
        conn.close()


def test_lifecycle_history_is_ordered_and_deduplicated() -> None:
    conn = _db()
    try:
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
        conn.execute("INSERT INTO dialogs VALUES (1, 0, 0, 1, 0, 0, 0, 0, 'x')")
        conn.commit()

        assert set_access_lost(
            conn,
            1,
            10,
            evidence=AccessLossEvidence("UpdateChannelParticipant", "removed_by_admin", 99),
        )
        assert not set_access_lost(conn, 1, 11)
        assert restore_access_after_revalidation(conn, 1, 12)
        assert not restore_access_after_revalidation(conn, 1, 13)
        conn.execute("UPDATE synced_dialogs SET status = 'syncing' WHERE dialog_id = 1")
        assert set_access_lost(conn, 1, 14)
        assert conn.execute(
            "SELECT kind, occurred_at, reason_code, previous_status, access_change_cause, actor_id "
            "FROM conversation_history_events ORDER BY seq"
        ).fetchall() == [
            ("access_lost", 10, "UpdateChannelParticipant", "synced", "removed_by_admin", 99),
            ("access_restored", 12, None, "access_lost", None, None),
            ("access_lost", 14, None, "syncing", None, None),
        ]
    finally:
        conn.close()
