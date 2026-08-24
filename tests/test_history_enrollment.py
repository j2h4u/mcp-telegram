"""Focused tests for durable full-history intent and coverage transitions."""
# pyright: reportReturnType=false, reportInvalidTypeForm=false

import sqlite3

import pytest

from mcp_telegram.history_enrollment import (
    EnrollmentSource,
    disable_history,
    enable_history,
    ensure_automatic_dm_enrollment,
    full_history_enabled,
    read_intent,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            sync_progress INTEGER,
            delta_refresh_requested_at INTEGER,
            read_position_next_attempt_at INTEGER,
            read_position_attempt_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE full_history_enrollment (
            dialog_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL,
            source TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        ) WITHOUT ROWID;
        """
    )
    yield connection
    connection.close()


@pytest.mark.parametrize("status", [None, "not_synced", "own_only", "fragment", "syncing", "synced", "access_lost"])
def test_enable_disable_matrix(conn: sqlite3.Connection, status: str | None) -> None:
    dialog_id = 100 + (len(status) if status else 0)
    if status is not None:
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, ?)", (dialog_id, status))
    conn.commit()

    enabled = enable_history(conn, dialog_id, now=10)
    assert enabled.enabled is True
    assert full_history_enabled(conn, dialog_id)
    assert enabled.blocked_reason == ("access_lost" if status == "access_lost" else None)

    disabled = disable_history(conn, dialog_id, now=11)
    assert disabled.enabled is False
    assert not full_history_enabled(conn, dialog_id)
    if status == "synced":
        assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
            "synced",
        )


def test_automatic_dm_does_not_resurrect_explicit_disable(conn: sqlite3.Connection) -> None:
    disable_history(conn, 1, now=10)
    outcome = ensure_automatic_dm_enrollment(conn, 1, now=11)
    assert outcome.blocked_reason == "explicit_disable"
    assert read_intent(conn, 1) == read_intent(conn, 1).__class__(1, False, EnrollmentSource.EXPLICIT)


def test_enrollment_transitions_clear_read_position_retry(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs(dialog_id, status, read_position_next_attempt_at, read_position_attempt_count) "
        "VALUES (4, 'synced', 999, 3)"
    )
    conn.commit()

    disable_history(conn, 4, now=10)
    assert conn.execute(
        "SELECT read_position_next_attempt_at, read_position_attempt_count FROM synced_dialogs WHERE dialog_id=4"
    ).fetchone() == (None, 0)

    conn.execute(
        "UPDATE synced_dialogs SET read_position_next_attempt_at = 999, read_position_attempt_count = 3 "
        "WHERE dialog_id = 4"
    )
    conn.commit()
    enable_history(conn, 4, now=11)
    assert conn.execute(
        "SELECT read_position_next_attempt_at, read_position_attempt_count FROM synced_dialogs WHERE dialog_id=4"
    ).fetchone() == (None, 0)


def test_weak_migration_disable_can_be_promoted_for_automatic_dm(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO full_history_enrollment VALUES (2, 0, 'migration', 1)")
    outcome = ensure_automatic_dm_enrollment(conn, 2, now=2)
    assert outcome.enabled is True
    assert read_intent(conn, 2).source is EnrollmentSource.AUTOMATIC


def test_enable_unknown_coverage_fails_closed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (3, 'future_status')")
    outcome = enable_history(conn, 3, now=3)
    assert outcome.action == "unsupported_coverage"
    assert outcome.blocked_reason == "unsupported_coverage"
    assert outcome.full_history_will_be_fetched is False


def test_nested_savepoint_rollback_preserves_outer_transaction(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute("BEGIN")
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (9, 'synced')")

    def reject_store(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.IntegrityError("reject")

    monkeypatch.setattr("mcp_telegram.history_enrollment._store_intent", reject_store)
    with pytest.raises(sqlite3.IntegrityError):
        enable_history(conn, 9, now=1)
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=9").fetchone() == ("synced",)
    conn.rollback()
