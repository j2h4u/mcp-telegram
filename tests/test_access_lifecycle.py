from __future__ import annotations

import logging
import sqlite3

import pytest

from mcp_telegram.access_lifecycle import (
    AccessLossEvidence,
    complete_access_revalidation,
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


def test_access_restore_rearms_terminal_reaction_details() -> None:
    conn = _db()
    conn.execute(
        "CREATE TABLE message_reaction_event_status ("
        "dialog_id INTEGER, message_id INTEGER, status TEXT, checked_at INTEGER, next_offset TEXT, "
        "next_attempt_at INTEGER, staged_count INTEGER, failure_kind TEXT, display_generation INTEGER, "
        "PRIMARY KEY(dialog_id, message_id))"
    )
    conn.execute(
        "CREATE TABLE message_reaction_events ("
        "event_id INTEGER PRIMARY KEY, dialog_id INTEGER, message_id INTEGER, display_generation INTEGER)"
    )
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (9, 'access_lost')")
    seed_full_history_enrollment(conn, 9, enabled=True)
    conn.execute("INSERT INTO dialogs VALUES (9, 1, 0, 1, 0, 0, 0, 0, 'x')")
    conn.executemany(
        "INSERT INTO message_reaction_event_status VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (9, 1, "unavailable", 5, "old", None, 2, "access_lost", 4),
            (9, 2, "unavailable", 5, None, None, 0, "invalid_target", 6),
        ],
    )
    conn.execute("INSERT INTO message_reaction_events VALUES (1, 9, 2, 0)")
    conn.commit()
    try:
        assert restore_access_after_revalidation(conn, 9, 12)
        assert conn.execute(
            "SELECT status, checked_at, next_offset, next_attempt_at, staged_count, failure_kind, display_generation "
            "FROM message_reaction_event_status WHERE dialog_id=9 AND message_id=1"
        ).fetchone() == ("stale", 12, None, 12, 0, None, 4)
        assert conn.execute(
            "SELECT status, next_attempt_at, failure_kind, display_generation "
            "FROM message_reaction_event_status WHERE dialog_id=9 AND message_id=2"
        ).fetchone() == ("stale", 12, None, 6)
        assert conn.execute(
            "SELECT COUNT(*) FROM message_reaction_events WHERE dialog_id=9 AND message_id=2 AND display_generation=0"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_complete_access_revalidation_keeps_access_lost_and_clears_retry() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, access_lost_at, access_next_revalidate_at) VALUES (4, 'access_lost', 10, 20)"
    )
    conn.commit()
    try:
        complete_access_revalidation(conn, 4, 30)
        assert conn.execute(
            "SELECT status, access_lost_at, access_last_revalidated_at, access_next_revalidate_at "
            "FROM synced_dialogs WHERE dialog_id=4"
        ).fetchone() == ("access_lost", 10, 30, None)
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


def test_lifecycle_history_and_operational_logs_are_ordered_and_deduplicated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = _db()
    try:
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
        conn.execute("INSERT INTO dialogs VALUES (1, 0, 0, 1, 0, 0, 0, 0, 'x')")
        conn.commit()

        with caplog.at_level(logging.INFO, logger="mcp_telegram.access_lifecycle"):
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
        lifecycle_logs = [record.getMessage() for record in caplog.records]
        assert lifecycle_logs == [
            "access_lost dialog_id=1 reason_code=UpdateChannelParticipant",
            "access_restored dialog_id=1 total_messages=None",
            "access_lost dialog_id=1 reason_code=None",
        ]
    finally:
        conn.close()
