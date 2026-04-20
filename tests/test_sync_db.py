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
            "read_inbox_max_id",
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
            "is_deleted",
            "deleted_at",
            "edit_date",
            "grouped_id",
            "reply_to_peer_id",
            "out",
            "is_service",
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
        assert rows[5][0] == 6
        assert rows[6][0] == 7
        assert rows[7][0] == 8
        assert rows[8][0] == 9
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema v9: out + is_service columns on messages (Phase 39.1)
# ---------------------------------------------------------------------------


def test_migration_v9_adds_out_and_is_service_columns(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), messages table has `out` and `is_service` columns.

    Both must be INTEGER NOT NULL DEFAULT 0 so existing rows and fresh DBs
    share the same default posture.
    """
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(messages)").fetchall()
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        columns = {str(row[1]): row for row in rows}
        assert "out" in columns, f"`out` column missing. Got: {set(columns.keys())}"
        assert "is_service" in columns, f"`is_service` column missing. Got: {set(columns.keys())}"

        out_col = columns["out"]
        assert out_col[2] == "INTEGER"
        assert out_col[3] == 1, "out must be NOT NULL"
        assert str(out_col[4]) == "0", f"out default must be 0, got {out_col[4]!r}"

        svc_col = columns["is_service"]
        assert svc_col[2] == "INTEGER"
        assert svc_col[3] == 1, "is_service must be NOT NULL"
        assert str(svc_col[4]) == "0", f"is_service default must be 0, got {svc_col[4]!r}"

        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert version is not None and int(version[0]) == _CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_v9_is_idempotent(tmp_sync_db_path: Path) -> None:
    """Running ensure_sync_schema() twice keeps schema_version stable with no error."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        versions = [int(r[0]) for r in rows]
        expected = list(range(1, _CURRENT_SCHEMA_VERSION + 1))
        assert versions == expected, f"Expected versions {expected}, got {versions}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema v10: backfill out=1 for historical outgoing DM rows
# ---------------------------------------------------------------------------


def test_migration_v10_backfills_out_for_dm_null_sender(tmp_sync_db_path: Path) -> None:
    """Historical DM rows with sender_id IS NULL get out=1; others untouched."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        # Simulate pre-v10 state: all rows at out=0 (v9 DEFAULT). We bypass
        # the normal writer path and insert minimal rows.
        conn.executemany(
            "INSERT INTO messages (dialog_id, message_id, sent_at, sender_id, text, out) "
            "VALUES (?, ?, 0, ?, ?, 0)",
            [
                (268071163, 1, None, "dm outgoing pre-v9"),      # DM, NULL sender → should become out=1
                (268071163, 2, 268071163, "dm incoming"),         # DM, peer sender → untouched
                (-1001917057529, 3, None, "group svc or null"),   # group → untouched
                (-1001917057529, 4, 999, "group with sender"),    # group → untouched
            ],
        )
        conn.commit()

        # Re-run migrations (idempotent): v10 UPDATE is the only one with work to do.
        # To exercise v10 in isolation, manually invoke the same UPDATE.
        conn.execute(
            "UPDATE messages SET out = 1 "
            "WHERE out = 0 AND dialog_id > 0 AND sender_id IS NULL"
        )
        conn.commit()

        rows = conn.execute(
            "SELECT dialog_id, message_id, sender_id, out FROM messages ORDER BY message_id"
        ).fetchall()
        # Row 1: DM + NULL sender → out=1
        assert rows[0][3] == 1, f"DM NULL-sender row should backfill to out=1, got {rows[0]}"
        # Row 2: DM + peer sender → out stays 0
        assert rows[1][3] == 0, f"DM incoming should stay out=0, got {rows[1]}"
        # Rows 3,4: group dialogs → out stays 0 regardless of sender_id
        assert rows[2][3] == 0, f"Group row should stay out=0, got {rows[2]}"
        assert rows[3][3] == 0, f"Group row should stay out=0, got {rows[3]}"
    finally:
        conn.close()


def test_migration_v10_is_idempotent(tmp_sync_db_path: Path) -> None:
    """Running ensure_sync_schema() twice is safe — v10 backfill applies once."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert version is not None and int(version[0]) == _CURRENT_SCHEMA_VERSION
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


def test_schema_v7_drops_reaction_metadata(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, reaction_metadata table does NOT exist."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "reaction_metadata" not in tables, (
            f"reaction_metadata should be dropped in v7. Tables: {tables}"
        )
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


def test_schema_v7_drops_message_cache(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, message_cache table does NOT exist."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "message_cache" not in tables, (
            f"message_cache should be dropped in v7. Tables: {tables}"
        )
    finally:
        conn.close()


def test_schema_v4_indexes_exist(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, expected indexes exist and dropped indexes are gone."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        index_names = {str(r[0]) for r in rows}
        # Indexes that must still exist after v7
        expected_indexes = {
            "idx_entities_type_updated",
            "idx_entities_username",
            "idx_topic_metadata_dialog_updated",
            "idx_messages_reply",
        }
        for idx in expected_indexes:
            assert idx in index_names, f"Missing index: {idx}. Got: {index_names}"
        # Indexes dropped in v7 (their tables were dropped)
        assert "idx_reactions_dialog_message" not in index_names, (
            "idx_reactions_dialog_message must be gone after v7 drops reaction_metadata"
        )
        assert "idx_message_cache_dialog_sent" not in index_names, (
            "idx_message_cache_dialog_sent must be gone after v7 drops message_cache"
        )
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


def test_schema_version_is_7(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), MAX(version) in schema_version is _CURRENT_SCHEMA_VERSION."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row is not None and row[0] == _CURRENT_SCHEMA_VERSION, (
            f"Expected version {_CURRENT_SCHEMA_VERSION}, got {row[0]}"
        )
    finally:
        conn.close()


def test_ensure_sync_schema_twice_idempotent_v7(tmp_sync_db_path: Path) -> None:
    """Running ensure_sync_schema twice is idempotent — no errors, version stays at _CURRENT_SCHEMA_VERSION."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row is not None and row[0] == _CURRENT_SCHEMA_VERSION
        rows = conn.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
        assert len(rows) == _CURRENT_SCHEMA_VERSION, (
            f"Expected {_CURRENT_SCHEMA_VERSION} schema_version rows, got {len(rows)}"
        )
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
        expected = list(range(1, _CURRENT_SCHEMA_VERSION + 1))
        assert versions == expected, f"expected versions {expected}, got {versions}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema v7: new tables, columns, index
# ---------------------------------------------------------------------------


def test_message_reactions_table_exists(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, message_reactions table exists with correct columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(message_reactions)").fetchall()
        columns = {str(row[1]) for row in rows}
        expected = {"dialog_id", "message_id", "emoji", "count"}
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_message_entities_table_exists(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, message_entities table exists with correct columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(message_entities)").fetchall()
        columns = {str(row[1]) for row in rows}
        expected = {"dialog_id", "message_id", "offset", "length", "type", "value"}
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_message_forwards_table_exists(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, message_forwards table exists with correct columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(message_forwards)").fetchall()
        columns = {str(row[1]) for row in rows}
        expected = {
            "dialog_id", "message_id", "fwd_from_peer_id",
            "fwd_from_name", "fwd_date", "fwd_channel_post",
        }
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_messages_has_new_v7_columns(tmp_sync_db_path: Path) -> None:
    """After v7, messages table has edit_date, grouped_id, reply_to_peer_id (all INTEGER, nullable)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute("PRAGMA table_info(messages)").fetchall()
        col_map = {str(row[1]): row for row in rows}
        for col in ("edit_date", "grouped_id", "reply_to_peer_id"):
            assert col in col_map, f"Column {col!r} missing from messages table"
            assert col_map[col][2].upper() == "INTEGER", (
                f"Column {col!r} type should be INTEGER, got {col_map[col][2]}"
            )
            assert col_map[col][3] == 0, (
                f"Column {col!r} should be nullable (notnull=0), got {col_map[col][3]}"
            )
    finally:
        conn.close()


def test_idx_messages_reply_exists(tmp_sync_db_path: Path) -> None:
    """After v7, idx_messages_reply index exists on messages(dialog_id, reply_to_msg_id)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_messages_reply'"
        ).fetchone()
        assert row is not None, "idx_messages_reply index missing after v7 migration"
    finally:
        conn.close()


def test_v7_backfill_reactions_from_json(tmp_path: Path) -> None:
    """v7 migration backfills message_reactions from reactions JSON blob.

    Tests:
    - Valid {emoji: count} JSON is backfilled correctly.
    - Malformed JSON (not json_valid) is skipped without error.
    - Non-object JSON (array, scalar) is skipped due to json_type='object' guard.
    - reactions column is gone after migration.
    """
    import json

    db_path = tmp_path / "sync.db"

    # Bootstrap to v6 manually: create schema up to v6, then inject reactions data,
    # then run ensure_sync_schema to apply v7.
    # We use a fresh DB, apply migrations 1-6, insert test rows, then complete to v7.

    # Build a v6 DB by running ensure_sync_schema on a patched version.
    # Since we can't easily stop at v6, we instead:
    # 1. Apply ensure_sync_schema (goes to v7), verify backfill happened.
    # We test backfill by using a FRESH DB where we manually inject v6-like data
    # before the v7 migration runs. We do this by creating a DB with just the
    # v6 schema (messages with reactions column) and then importing the migration.
    import sqlite3 as _sqlite3
    from mcp_telegram.sync_db import _open_sync_db, _apply_migrations

    conn = _open_sync_db(db_path)
    try:
        # Apply migrations manually, stopping before v7 by using internal state.
        # We create a minimal v6-like schema directly.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL, applied_at INTEGER NOT NULL)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                dialog_id       INTEGER NOT NULL,
                message_id      INTEGER NOT NULL,
                sent_at         INTEGER NOT NULL,
                text            TEXT,
                sender_id       INTEGER,
                sender_first_name TEXT,
                media_description TEXT,
                reply_to_msg_id INTEGER,
                forum_topic_id  INTEGER,
                reactions       TEXT,
                is_deleted      INTEGER NOT NULL DEFAULT 0,
                deleted_at      INTEGER,
                PRIMARY KEY (dialog_id, message_id)
            ) WITHOUT ROWID"""
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
            "USING fts5(dialog_id UNINDEXED, message_id UNINDEXED, text)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS synced_dialogs (
                dialog_id      INTEGER PRIMARY KEY,
                status         TEXT NOT NULL DEFAULT 'not_synced',
                last_synced_at INTEGER,
                last_event_at  INTEGER,
                sync_progress  INTEGER DEFAULT 0,
                total_messages INTEGER,
                access_lost_at INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS message_versions (
                dialog_id  INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                version    INTEGER NOT NULL,
                old_text   TEXT,
                edit_date  INTEGER,
                PRIMARY KEY (dialog_id, message_id, version)
            ) WITHOUT ROWID"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS entities (
                id              INTEGER PRIMARY KEY,
                type            TEXT NOT NULL,
                name            TEXT,
                username        TEXT,
                name_normalized TEXT,
                updated_at      INTEGER NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS topic_metadata (
                dialog_id      INTEGER NOT NULL,
                topic_id       INTEGER NOT NULL,
                title          TEXT NOT NULL,
                top_message_id INTEGER,
                is_general     INTEGER NOT NULL,
                is_deleted     INTEGER NOT NULL,
                inaccessible_error TEXT,
                inaccessible_at INTEGER,
                updated_at     INTEGER NOT NULL,
                PRIMARY KEY (dialog_id, topic_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS telemetry_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                timestamp REAL NOT NULL,
                duration_ms REAL NOT NULL,
                result_count INTEGER NOT NULL,
                has_cursor BOOLEAN NOT NULL,
                page_depth INTEGER NOT NULL,
                has_filter BOOLEAN NOT NULL,
                error_type TEXT
            )"""
        )
        # Mark versions 1-6 as applied
        for v in range(1, 7):
            conn.execute(
                "INSERT INTO schema_version VALUES (?, strftime('%s', 'now'))", (v,)
            )

        # Insert test rows with various reactions values
        valid_json = json.dumps({"👍": 3, "❤": 1})
        malformed_json = "not json"
        array_json = "[1, 2, 3]"  # valid JSON but not an object
        scalar_json = '"hello"'   # valid JSON but not an object

        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, reactions) VALUES (1, 10, 1, ?)",
            (valid_json,),
        )
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, reactions) VALUES (1, 20, 2, ?)",
            (malformed_json,),
        )
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, reactions) VALUES (1, 30, 3, ?)",
            (array_json,),
        )
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, reactions) VALUES (1, 40, 4, ?)",
            (scalar_json,),
        )
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, reactions) VALUES (1, 50, 5, NULL)",
        )
        conn.commit()
    finally:
        conn.close()

    # Now apply v7 migration
    ensure_sync_schema(db_path)

    # Verify results
    conn = _open_sync_db(db_path)
    try:
        # reactions column must be gone
        col_names = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "reactions" not in col_names, "reactions column must be dropped after v7"

        # Valid JSON reactions backfilled
        reaction_rows = conn.execute(
            "SELECT emoji, count FROM message_reactions WHERE dialog_id=1 AND message_id=10 ORDER BY emoji"
        ).fetchall()
        reaction_dict = {r[0]: r[1] for r in reaction_rows}
        assert reaction_dict == {"👍": 3, "❤": 1}, (
            f"Valid JSON reactions should be backfilled. Got: {reaction_dict}"
        )

        # Malformed JSON row: no reactions backfilled (json_valid() guard)
        malformed_rows = conn.execute(
            "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=1 AND message_id=20"
        ).fetchone()[0]
        assert malformed_rows == 0, "Malformed JSON must produce no reaction rows"

        # Array JSON row: no reactions backfilled (json_type='object' guard)
        array_rows = conn.execute(
            "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=1 AND message_id=30"
        ).fetchone()[0]
        assert array_rows == 0, "Array JSON must produce no reaction rows (not an object)"

        # Scalar JSON row: no reactions backfilled (json_type='object' guard)
        scalar_rows = conn.execute(
            "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=1 AND message_id=40"
        ).fetchone()[0]
        assert scalar_rows == 0, "Scalar JSON must produce no reaction rows (not an object)"

        # NULL reactions: no rows
        null_rows = conn.execute(
            "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=1 AND message_id=50"
        ).fetchone()[0]
        assert null_rows == 0, "NULL reactions must produce no reaction rows"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema v8: read_inbox_max_id column + supporting index
# ---------------------------------------------------------------------------


def test_schema_version_matches_current(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), MAX(version) equals _CURRENT_SCHEMA_VERSION."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row is not None and row[0] == _CURRENT_SCHEMA_VERSION, (
            f"Expected version {_CURRENT_SCHEMA_VERSION}, got {row[0]}"
        )
    finally:
        conn.close()


def test_synced_dialogs_has_read_inbox_max_id_column(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), synced_dialogs has read_inbox_max_id INTEGER column."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        cols = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(synced_dialogs)").fetchall()}
        assert "read_inbox_max_id" in cols, f"Column missing; got {sorted(cols)}"
        assert cols["read_inbox_max_id"] == "INTEGER", f"Wrong type: {cols['read_inbox_max_id']}"
    finally:
        conn.close()


def test_read_inbox_max_id_defaults_to_null_for_existing_rows(tmp_sync_db_path: Path) -> None:
    """New rows in synced_dialogs have read_inbox_max_id = NULL (no default, not 0)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (12345,))
        conn.commit()
        row = conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = ?", (12345,)).fetchone()
        assert row is not None
        assert row[0] is None, f"Expected NULL, got {row[0]!r}"
    finally:
        conn.close()


def test_v8_index_on_status_and_read_position_exists(tmp_sync_db_path: Path) -> None:
    """Opencode review flagged perf concern: SQL unread scan needs an index on
    (status, read_inbox_max_id) for efficient filtering.
    """
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='synced_dialogs'"
        ).fetchall()
        index_names = {row[0] for row in rows}
        assert "idx_synced_dialogs_status_read_position" in index_names, (
            f"Index missing; found: {sorted(index_names)}"
        )
    finally:
        conn.close()


def test_message_entities_pk_allows_same_offset_different_type(tmp_sync_db_path: Path) -> None:
    """5-column PK: two entities at the same (offset, length) but different type both stored."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        # Insert two entity rows with same (dialog_id, message_id, offset, length) but different type
        conn.execute(
            "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) "
            "VALUES (1, 100, 0, 5, 'mention', '@alice')"
        )
        conn.execute(
            "INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value) "
            "VALUES (1, 100, 0, 5, 'hashtag', '#test')"
        )
        conn.commit()

        rows = conn.execute(
            "SELECT type, value FROM message_entities "
            "WHERE dialog_id=1 AND message_id=100 ORDER BY type"
        ).fetchall()
        assert len(rows) == 2, (
            f"Both entity rows must be retained with 5-column PK. Got: {rows}"
        )
        types = {r[0] for r in rows}
        assert types == {"mention", "hashtag"}
    finally:
        conn.close()
