from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import time

from mcp_telegram.sync_db import (
    _CURRENT_SCHEMA_VERSION,
    _migrate_from_legacy_db,
    _open_sync_db,
    ensure_sync_schema,
    get_sync_db_path,
    migrate_legacy_databases,
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
            "access_lost_at",
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
    """Calling ensure_sync_schema twice raises no error — second call is a no-op.

    Schema v2 produces 2 rows in schema_version (one per version).
    Second call to ensure_sync_schema must not add more rows.
    """
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)  # second call must be a no-op
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
        # Schema v2 has 2 version rows (v1 + v2); second ensure_sync_schema adds 0
        assert len(rows) == _CURRENT_SCHEMA_VERSION, (
            f"Expected {_CURRENT_SCHEMA_VERSION} schema_version rows, got {len(rows)}"
        )
    finally:
        conn.close()


def test_schema_version_value(tmp_sync_db_path: Path) -> None:
    """After first migration, MAX(version) in schema_version equals _CURRENT_SCHEMA_VERSION (2)."""
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
# Schema v2: access_lost_at column and migration idempotency
# ---------------------------------------------------------------------------


def test_schema_v2_migration_adds_access_lost_at(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), PRAGMA table_info(synced_dialogs) includes access_lost_at INTEGER."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(synced_dialogs)").fetchall()
        columns = {str(row[1]): row for row in rows}
        assert "access_lost_at" in columns, f"access_lost_at missing. Got: {set(columns.keys())}"
        assert columns["access_lost_at"][2] == "INTEGER"
    finally:
        conn.close()


def test_schema_migration_idempotent_v2(tmp_sync_db_path: Path) -> None:
    """Calling ensure_sync_schema() twice produces exactly _CURRENT_SCHEMA_VERSION rows in schema_version."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)  # must not raise
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
        assert len(rows) == _CURRENT_SCHEMA_VERSION
        assert rows[0][0] == 1
        assert rows[1][0] == 2
        assert rows[2][0] == 3
        assert rows[3][0] == 4
        assert rows[4][0] == 5
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

    # Use a mock loop to capture the callback passed to add_signal_handler
    mock_loop = MagicMock()
    register_shutdown_handler(conn, mock_loop)

    mock_loop.add_signal_handler.assert_called_once()
    sigterm_callback = mock_loop.add_signal_handler.call_args[0][1]
    sigterm_callback()

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


# ---------------------------------------------------------------------------
# Schema v4: entity tables, reaction_metadata, topic_metadata, message_cache
# ---------------------------------------------------------------------------


def test_schema_v4_entities_table(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), entities table exists with all expected columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(entities)").fetchall()
        columns = {str(row[1]) for row in rows}
        expected = {"id", "type", "name", "username", "name_normalized", "updated_at"}
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_schema_v4_reaction_metadata_table(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), reaction_metadata table exists with all expected columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(reaction_metadata)").fetchall()
        columns = {str(row[1]) for row in rows}
        expected = {"message_id", "dialog_id", "emoji", "reactor_names", "fetched_at"}
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_schema_v4_topic_metadata_table(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), topic_metadata table exists with all expected columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(topic_metadata)").fetchall()
        columns = {str(row[1]) for row in rows}
        expected = {
            "dialog_id", "topic_id", "title", "top_message_id",
            "is_general", "is_deleted", "inaccessible_error",
            "inaccessible_at", "updated_at",
        }
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_schema_v4_message_cache_table(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), message_cache table exists with all expected columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(message_cache)").fetchall()
        columns = {str(row[1]) for row in rows}
        expected = {
            "dialog_id", "message_id", "sent_at", "text", "sender_id",
            "sender_first_name", "media_description", "reply_to_msg_id",
            "forum_topic_id", "edit_date", "fetched_at",
        }
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_schema_v4_indexes_exist(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), all v4 indexes exist in sqlite_master."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        index_names = {str(r[0]) for r in rows}
        expected_indexes = {
            "idx_entities_type_updated",
            "idx_entities_username",
            "idx_reactions_dialog_message",
            "idx_topic_metadata_dialog_updated",
            "idx_message_cache_dialog_sent",
        }
        for idx in expected_indexes:
            assert idx in index_names, f"Missing index: {idx}. Got: {index_names}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema v5: telemetry_events table
# ---------------------------------------------------------------------------


def test_schema_v5_telemetry_events_table(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), telemetry_events table exists with all expected columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(telemetry_events)").fetchall()
        columns = {str(row[1]) for row in rows}
        expected = {
            "id", "tool_name", "timestamp", "duration_ms", "result_count",
            "has_cursor", "page_depth", "has_filter", "error_type",
        }
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_schema_v5_telemetry_index_exists(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), idx_telemetry_tool_timestamp index exists."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_telemetry_tool_timestamp'"
        ).fetchone()
        assert row is not None, "idx_telemetry_tool_timestamp index missing"
    finally:
        conn.close()


def test_schema_version_is_5(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), MAX(version) in schema_version is 5."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row is not None and row[0] == 5, f"Expected version 5, got {row[0]}"
    finally:
        conn.close()


def test_ensure_sync_schema_twice_idempotent_v5(tmp_sync_db_path: Path) -> None:
    """Running ensure_sync_schema twice is idempotent — no errors, version stays 5."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row is not None and row[0] == 5
        rows = conn.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
        assert len(rows) == 5, f"Expected 5 schema_version rows, got {len(rows)}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _migrate_from_legacy_db tests
# ---------------------------------------------------------------------------


def _make_entity_cache_db(path: Path, rows: list[tuple]) -> None:
    """Create a minimal entity_cache.db with entities table and given rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entities ("
            "id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, "
            "username TEXT, updated_at INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO entities (id, type, name, username, updated_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _make_analytics_db(path: Path, rows: list[tuple]) -> None:
    """Create a minimal analytics.db with telemetry_events table and given rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS telemetry_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, tool_name TEXT NOT NULL, "
            "timestamp REAL NOT NULL, duration_ms REAL NOT NULL, "
            "result_count INTEGER NOT NULL, has_cursor BOOLEAN NOT NULL, "
            "page_depth INTEGER NOT NULL, has_filter BOOLEAN NOT NULL, "
            "error_type TEXT)"
        )
        conn.executemany(
            "INSERT INTO telemetry_events "
            "(tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_migrate_from_legacy_db_copies_entities(tmp_path: Path) -> None:
    """_migrate_from_legacy_db copies 3 entity rows from entity_cache.db into sync.db entities table."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)

    legacy_path = tmp_path / "entity_cache.db"
    _make_entity_cache_db(legacy_path, [
        (1, "user", "Alice", "alice", 1700000000),
        (2, "user", "Bob", None, 1700000001),
        (3, "group", "Test Group", "testgroup", 1700000002),
    ])

    copy_stmts = [
        "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) "
        "SELECT id, type, name, username, updated_at FROM legacy.entities",
    ]
    try:
        count = _migrate_from_legacy_db(conn, legacy_path, copy_stmts)
        assert count == 3, f"Expected 3 rows copied, got {count}"

        rows = conn.execute("SELECT id, name FROM entities ORDER BY id").fetchall()
        assert len(rows) == 3
        assert rows[0] == (1, "Alice")
        assert rows[1] == (2, "Bob")
        assert rows[2] == (3, "Test Group")
    finally:
        conn.close()


def test_migrate_from_legacy_db_noop_when_missing(tmp_path: Path) -> None:
    """_migrate_from_legacy_db is a no-op when legacy path does not exist."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        missing = tmp_path / "nonexistent.db"
        count = _migrate_from_legacy_db(conn, missing, [])
        assert count == 0
        rows = conn.execute("SELECT * FROM entities").fetchall()
        assert rows == []
    finally:
        conn.close()


def test_migrate_from_legacy_db_insert_or_ignore_on_pk_conflict(tmp_path: Path) -> None:
    """_migrate_from_legacy_db with overlapping PKs uses INSERT OR IGNORE — sync.db row wins."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)

    # Pre-insert entity id=1 into sync.db
    conn.execute(
        "INSERT INTO entities (id, type, name, username, updated_at) VALUES (1, 'user', 'SyncName', NULL, 9999)"
    )
    conn.commit()

    legacy_path = tmp_path / "entity_cache.db"
    _make_entity_cache_db(legacy_path, [
        (1, "user", "LegacyName", None, 1700000000),  # conflict
        (2, "user", "NewUser", None, 1700000001),
    ])

    copy_stmts = [
        "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) "
        "SELECT id, type, name, username, updated_at FROM legacy.entities",
    ]
    try:
        _migrate_from_legacy_db(conn, legacy_path, copy_stmts)
        row = conn.execute("SELECT name FROM entities WHERE id=1").fetchone()
        assert row is not None and row[0] == "SyncName", (
            f"sync.db row should win on conflict, got {row[0]}"
        )
        row2 = conn.execute("SELECT name FROM entities WHERE id=2").fetchone()
        assert row2 is not None and row2[0] == "NewUser"
    finally:
        conn.close()


def test_migrate_from_legacy_db_telemetry_30day_filter(tmp_path: Path) -> None:
    """_migrate_from_legacy_db copies only telemetry rows within last 30 days."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)

    now = time.time()
    recent = now - 86400       # 1 day ago — should be copied
    old = now - 40 * 86400    # 40 days ago — should be excluded

    analytics_path = tmp_path / "analytics.db"
    _make_analytics_db(analytics_path, [
        ("ListMessages", recent, 100.0, 10, False, 1, False, None),
        ("ListDialogs", old, 50.0, 5, False, 1, False, None),
    ])

    copy_stmts = [
        "INSERT OR IGNORE INTO telemetry_events "
        "(tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type) "
        "SELECT tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type "
        "FROM legacy.telemetry_events "
        "WHERE timestamp >= strftime('%s', 'now') - 2592000",
    ]
    try:
        count = _migrate_from_legacy_db(conn, analytics_path, copy_stmts)
        # Only the recent row should be copied (rowcount may report differently per driver)
        rows = conn.execute("SELECT tool_name FROM telemetry_events").fetchall()
        tool_names = [r[0] for r in rows]
        assert "ListMessages" in tool_names, "Recent event should be migrated"
        assert "ListDialogs" not in tool_names, "Old event should be excluded"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# migrate_legacy_databases tests
# ---------------------------------------------------------------------------


def test_migrate_legacy_databases_deletes_legacy_files(tmp_path: Path) -> None:
    """migrate_legacy_databases deletes entity_cache.db, .bootstrap.lock, and analytics.db after migration."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)

    entity_cache = tmp_path / "entity_cache.db"
    lock_file = tmp_path / "entity_cache.db.bootstrap.lock"
    analytics = tmp_path / "analytics.db"

    _make_entity_cache_db(entity_cache, [(10, "user", "Test", None, 1700000000)])
    lock_file.touch()
    _make_analytics_db(analytics, [])

    try:
        migrate_legacy_databases(conn, tmp_path)
        assert not entity_cache.exists(), "entity_cache.db should be deleted"
        assert not lock_file.exists(), "entity_cache.db.bootstrap.lock should be deleted"
        assert not analytics.exists(), "analytics.db should be deleted"
    finally:
        conn.close()


def test_migrate_legacy_databases_noop_when_files_missing(tmp_path: Path) -> None:
    """migrate_legacy_databases does not raise when legacy files don't exist."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        # No legacy files — must not raise
        migrate_legacy_databases(conn, tmp_path)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration atomicity (L-4)
# ---------------------------------------------------------------------------


def test_ensure_sync_schema_idempotent(tmp_path: Path) -> None:
    """Calling ensure_sync_schema twice doesn't raise or corrupt schema."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row[0] >= 5, "schema should be at current version"
    finally:
        conn.close()


def test_schema_version_records_all_versions(tmp_path: Path) -> None:
    """Each migration version is recorded individually in schema_version."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)
    try:
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5], f"expected all 5 versions, got {versions}"
    finally:
        conn.close()
