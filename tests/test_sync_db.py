from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from mcp_telegram.sync_db import (
    _CURRENT_SCHEMA_VERSION,
    _open_sync_db,
    ensure_sync_schema,
    get_sync_db_path,
    open_sync_db_reader,
    register_shutdown_handler,
)


@pytest.fixture()
def tmp_sync_db_path(tmp_path: Path) -> Path:
    """Return a path to a temporary sync.db file (not yet created)."""
    return tmp_path / "sync.db"


# ---------------------------------------------------------------------------
# SYNC-01: Separate DB file at correct path
# ---------------------------------------------------------------------------


def test_db_path_is_separate() -> None:
    """get_sync_db_path returns a path ending in sync.db, not entity_cache.db."""
    path = get_sync_db_path()
    assert path.name == "sync.db", f"Expected sync.db, got {path.name}"
    assert "entity_cache" not in str(path), "sync.db must not share name with entity_cache.db"
    assert "mcp-telegram" in str(path), "path must be under mcp-telegram state dir"


# ---------------------------------------------------------------------------
# SYNC-01: WAL mode
# ---------------------------------------------------------------------------


def test_wal_mode_active(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema, PRAGMA journal_mode returns 'wal'."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert str(row[0]).lower() == "wal", f"Expected WAL mode, got {row[0]}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-02: synced_dialogs table schema
# ---------------------------------------------------------------------------


def test_synced_dialogs_schema(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema, synced_dialogs table exists with expected columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(synced_dialogs)").fetchall()
        # rows: (cid, name, type, notnull, dflt_value, pk)
        columns = {str(row[1]): row for row in rows}
        expected = {
            "dialog_id",
            "status",
            "last_synced_at",
            "last_event_at",
            "sync_progress",
            "total_messages",
        }
        assert expected == set(columns.keys()), (
            f"Unexpected columns. Got: {set(columns.keys())}, expected: {expected}"
        )
        # dialog_id is primary key
        assert columns["dialog_id"][5] == 1, "dialog_id must be PRIMARY KEY"
        # status default is 'not_synced'
        default_val = columns["status"][4]
        assert default_val is not None and "not_synced" in str(default_val), (
            f"status default should be 'not_synced', got {default_val!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-03: messages table schema
# ---------------------------------------------------------------------------


def test_messages_schema(tmp_sync_db_path: Path) -> None:
    """messages table exists with all required columns and composite PK (dialog_id, message_id)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(messages)").fetchall()
        columns = {str(row[1]): row for row in rows}
        expected = {
            "dialog_id",
            "message_id",
            "sent_at",
            "text",
            "sender_id",
            "sender_first_name",
            "media_description",
            "reply_to_msg_id",
            "forum_topic_id",
            "reactions",
            "is_deleted",
            "deleted_at",
        }
        assert expected == set(columns.keys()), (
            f"Unexpected columns. Got: {set(columns.keys())}, expected: {expected}"
        )
        # Both dialog_id and message_id are part of composite PK (pk > 0)
        assert columns["dialog_id"][5] > 0, "dialog_id must be part of PRIMARY KEY"
        assert columns["message_id"][5] > 0, "message_id must be part of PRIMARY KEY"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-04: message_versions table schema
# ---------------------------------------------------------------------------


def test_message_versions_schema(tmp_sync_db_path: Path) -> None:
    """message_versions table exists with expected columns and composite PK."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(message_versions)").fetchall()
        columns = {str(row[1]): row for row in rows}
        expected = {"dialog_id", "message_id", "version", "old_text", "edit_date"}
        assert expected == set(columns.keys()), (
            f"Unexpected columns. Got: {set(columns.keys())}, expected: {expected}"
        )
        # dialog_id, message_id, version are all part of composite PK
        assert columns["dialog_id"][5] > 0, "dialog_id must be part of PRIMARY KEY"
        assert columns["message_id"][5] > 0, "message_id must be part of PRIMARY KEY"
        assert columns["version"][5] > 0, "version must be part of PRIMARY KEY"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-05: is_deleted and deleted_at defaults
# ---------------------------------------------------------------------------


def test_is_deleted_columns(tmp_sync_db_path: Path) -> None:
    """Insert a message row with only required fields; is_deleted defaults to 0, deleted_at is NULL."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (1, 100, 1700000000)"
        )
        conn.commit()
        row = conn.execute(
            "SELECT is_deleted, deleted_at FROM messages WHERE dialog_id=1 AND message_id=100"
        ).fetchone()
        assert row is not None, "Inserted row not found"
        is_deleted, deleted_at = row
        assert is_deleted == 0, f"is_deleted should default to 0, got {is_deleted}"
        assert deleted_at is None, f"deleted_at should default to NULL, got {deleted_at}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-06: Migration idempotency
# ---------------------------------------------------------------------------


def test_migration_idempotent(tmp_sync_db_path: Path) -> None:
    """Calling ensure_sync_schema twice raises no error and produces exactly one schema_version row."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)  # second call must be a no-op
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("SELECT * FROM schema_version").fetchall()
        assert len(rows) == 1, f"Expected exactly 1 schema_version row, got {len(rows)}"
    finally:
        conn.close()


def test_schema_version_value(tmp_sync_db_path: Path) -> None:
    """After first migration, MAX(version) in schema_version equals _CURRENT_SCHEMA_VERSION (1)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row is not None and row[0] is not None, "schema_version has no rows"
        assert int(row[0]) == _CURRENT_SCHEMA_VERSION, (
            f"Expected version {_CURRENT_SCHEMA_VERSION}, got {row[0]}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-08: Concurrent read during write
# ---------------------------------------------------------------------------


def test_concurrent_read_during_write(tmp_sync_db_path: Path) -> None:
    """Reader connection (mode=ro) can query synced_dialogs while writer holds BEGIN IMMEDIATE."""
    ensure_sync_schema(tmp_sync_db_path)

    writer = _open_sync_db(tmp_sync_db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (42, 'syncing')"
        )
        # Reader opens while writer transaction is active (not yet committed)
        reader = sqlite3.connect(
            f"file:{tmp_sync_db_path}?mode=ro", uri=True, timeout=5.0
        )
        try:
            # Should succeed — reads committed snapshot, not seeing the uncommitted INSERT
            rows = reader.execute("SELECT * FROM synced_dialogs").fetchall()
            # No OperationalError raised — concurrent read works
            assert isinstance(rows, list)
        finally:
            reader.close()
        # Rollback the writer transaction
        writer.rollback()
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# SYNC-08: SIGTERM checkpoint
# ---------------------------------------------------------------------------


def test_sigterm_checkpoint(tmp_sync_db_path: Path) -> None:
    """Write data, invoke shutdown callback directly, reopen DB, verify integrity."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)

    # Write some committed data
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
    conn.commit()

    # Set up shutdown handler and capture the callback via a new event loop
    loop = asyncio.new_event_loop()
    try:
        shutdown_event = register_shutdown_handler(conn, loop)
        # Invoke SIGTERM callback directly (not via signal — test environment)
        # The handler is registered with loop.add_signal_handler; we need to call _on_sigterm directly.
        # We get the callback by calling the handler-registered function.
        # Since we can't easily extract it from the loop, we re-implement the call:
        # Instead, get the handler from the loop's signal handlers:
        import signal as _signal

        sigterm_handle = loop._signal_handlers.get(_signal.SIGTERM)  # type: ignore[attr-defined]
        assert sigterm_handle is not None, "SIGTERM handler not registered"
        # asyncio stores signal handlers as Handle objects — invoke via _run()
        sigterm_handle._run()  # type: ignore[attr-defined]
    finally:
        loop.close()

    # Reopen DB and verify data is intact
    reopen = sqlite3.connect(str(tmp_sync_db_path), timeout=10.0)
    try:
        integrity = reopen.execute("PRAGMA integrity_check").fetchone()
        assert integrity is not None and str(integrity[0]).lower() == "ok", (
            f"integrity_check failed: {integrity}"
        )
        row = reopen.execute(
            "SELECT status FROM synced_dialogs WHERE dialog_id=1"
        ).fetchone()
        assert row is not None and row[0] == "synced", (
            f"Data not preserved after shutdown: {row}"
        )
    finally:
        reopen.close()


# ---------------------------------------------------------------------------
# SYNC-08: Read-only connection cannot write
# ---------------------------------------------------------------------------


def test_open_sync_db_reader_readonly(tmp_sync_db_path: Path) -> None:
    """open_sync_db_reader returns connection that can SELECT but raises OperationalError on INSERT."""
    ensure_sync_schema(tmp_sync_db_path)
    reader = open_sync_db_reader(tmp_sync_db_path)
    try:
        # SELECT must succeed
        rows = reader.execute("SELECT * FROM synced_dialogs").fetchall()
        assert isinstance(rows, list)
        # INSERT must fail with OperationalError
        with pytest.raises(sqlite3.OperationalError):
            reader.execute(
                "INSERT INTO synced_dialogs (dialog_id) VALUES (999)"
            )
    finally:
        reader.close()
