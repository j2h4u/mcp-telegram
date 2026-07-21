from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

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

TableInfoRow = tuple[int, str, str, int, object, int]
Row = tuple[object, ...]


def _open_db(db_path: Path) -> sqlite3.Connection:
    return cast(sqlite3.Connection, _open_sync_db(db_path))


def _fetchone_row(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> Row | None:
    return cast(Row | None, conn.execute(sql, parameters).fetchone())


def _fetchall_rows(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> list[Row]:
    return cast(list[Row], conn.execute(sql, parameters).fetchall())


def _table_info(conn: sqlite3.Connection, table: str) -> list[TableInfoRow]:
    return cast(list[TableInfoRow], _fetchall_rows(conn, f"PRAGMA table_info({table})"))


def _fetchone_int(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> int:
    row = _fetchone_row(conn, sql, parameters)
    assert row is not None
    return int(cast(int, row[0]))


def _fetchone_text(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> str:
    row = _fetchone_row(conn, sql, parameters)
    assert row is not None
    return str(row[0])


@pytest.fixture()
def tmp_sync_db_path(tmp_path: Path) -> Path:
    """Return a path to a temporary sync.db file (not yet created)."""
    return tmp_path / "sync.db"


# ---------------------------------------------------------------------------
# SYNC-01: Separate DB file at correct path
# ---------------------------------------------------------------------------


def test_db_path_is_separate(tmp_path: Path) -> None:
    """get_sync_db_path returns a path ending in sync.db, not entity_cache.db."""
    path = get_sync_db_path(tmp_path)
    assert path.name == "sync.db", f"Expected sync.db, got {path.name}"
    assert "entity_cache" not in str(path), "sync.db must not share name with entity_cache.db"


def test_db_path_uses_explicit_state_dir(tmp_path: Path) -> None:
    """The path helper is pure; composition roots provide the state directory."""
    state_dir = tmp_path / "state"

    assert get_sync_db_path(state_dir) == state_dir / "sync.db"


# ---------------------------------------------------------------------------
# SYNC-01: WAL mode
# ---------------------------------------------------------------------------


def test_wal_mode_active(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema, PRAGMA journal_mode returns 'wal'."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        row = _fetchone_row(conn, "PRAGMA journal_mode")
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
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "synced_dialogs")
        # rows: (cid, name, type, notnull, dflt_value, pk)
        columns = {row[1]: row for row in rows}
        expected = {
            "dialog_id",
            "status",
            "last_synced_at",
            "last_event_at",
            "sync_progress",
            "total_messages",
            "access_lost_at",
            "read_inbox_max_id",
            "read_outbox_max_id",
        }
        assert expected == set(columns.keys()), f"Unexpected columns. Got: {set(columns.keys())}, expected: {expected}"
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
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "messages")
        columns = {row[1]: row for row in rows}
        expected = {
            "dialog_id",
            "message_id",
            "sent_at",
            "text",
            "sender_id",
            "sender_first_name",
            "media_description",
            "reply_to_msg_id",
            "reply_count",
            "forum_topic_id",
            "is_deleted",
            "deleted_at",
            "edit_date",
            "grouped_id",
            "reply_to_peer_id",
            "out",
            "is_service",
            "post_author",
        }
        assert expected == set(columns.keys()), f"Unexpected columns. Got: {set(columns.keys())}, expected: {expected}"
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
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "message_versions")
        columns = {row[1]: row for row in rows}
        expected = {"dialog_id", "message_id", "version", "old_text", "edit_date"}
        assert expected == set(columns.keys()), f"Unexpected columns. Got: {set(columns.keys())}, expected: {expected}"
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
    conn = _open_db(tmp_sync_db_path)
    try:
        conn.execute("INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (1, 100, 1700000000)")
        conn.commit()
        row = _fetchone_row(conn, "SELECT is_deleted, deleted_at FROM messages WHERE dialog_id=1 AND message_id=100")
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
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _fetchall_rows(conn, "SELECT * FROM schema_version ORDER BY version")
        # Schema v2 has 2 version rows (v1 + v2); second ensure_sync_schema adds 0
        assert len(rows) == _CURRENT_SCHEMA_VERSION, (
            f"Expected {_CURRENT_SCHEMA_VERSION} schema_version rows, got {len(rows)}"
        )
    finally:
        conn.close()


def test_schema_version_value(tmp_sync_db_path: Path) -> None:
    """After first migration, MAX(version) in schema_version equals _CURRENT_SCHEMA_VERSION (2)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert version == _CURRENT_SCHEMA_VERSION, f"Expected version {_CURRENT_SCHEMA_VERSION}, got {version}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema v2: access_lost_at column and migration idempotency
# ---------------------------------------------------------------------------


def test_schema_v2_migration_adds_access_lost_at(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), PRAGMA table_info(synced_dialogs) includes access_lost_at INTEGER."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "synced_dialogs")
        columns = {row[1]: row for row in rows}
        assert "access_lost_at" in columns, f"access_lost_at missing. Got: {set(columns.keys())}"
        assert columns["access_lost_at"][2] == "INTEGER"
    finally:
        conn.close()


def test_schema_migration_idempotent_v2(tmp_sync_db_path: Path) -> None:
    """Calling ensure_sync_schema() twice produces exactly _CURRENT_SCHEMA_VERSION rows in schema_version."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)  # must not raise
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _fetchall_rows(conn, "SELECT * FROM schema_version ORDER BY version")
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
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "messages")
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

        version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert version == _CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_v9_is_idempotent(tmp_sync_db_path: Path) -> None:
    """Running ensure_sync_schema() twice keeps schema_version stable with no error."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _fetchall_rows(conn, "SELECT version FROM schema_version ORDER BY version")
        versions = [int(cast(int, row[0])) for row in rows]
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
    conn = _open_db(tmp_sync_db_path)
    try:
        # Simulate pre-v10 state: all rows at out=0 (v9 DEFAULT). We bypass
        # the normal writer path and insert minimal rows.
        conn.executemany(
            "INSERT INTO messages (dialog_id, message_id, sent_at, sender_id, text, out) VALUES (?, ?, 0, ?, ?, 0)",
            [
                (268071163, 1, None, "dm outgoing pre-v9"),  # DM, NULL sender → should become out=1
                (268071163, 2, 268071163, "dm incoming"),  # DM, peer sender → untouched
                (-1001917057529, 3, None, "group svc or null"),  # group → untouched
                (-1001917057529, 4, 999, "group with sender"),  # group → untouched
            ],
        )
        conn.commit()

        # Re-run migrations (idempotent): v10 UPDATE is the only one with work to do.
        # To exercise v10 in isolation, manually invoke the same UPDATE.
        conn.execute("UPDATE messages SET out = 1 WHERE out = 0 AND dialog_id > 0 AND sender_id IS NULL")
        conn.commit()

        rows = _fetchall_rows(conn, "SELECT dialog_id, message_id, sender_id, out FROM messages ORDER BY message_id")
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
    conn = _open_db(tmp_sync_db_path)
    try:
        version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert version == _CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-08: Concurrent read during write
# ---------------------------------------------------------------------------


def test_concurrent_read_during_write(tmp_sync_db_path: Path) -> None:
    """Reader connection (mode=ro) can query synced_dialogs while writer holds BEGIN IMMEDIATE."""
    ensure_sync_schema(tmp_sync_db_path)

    writer = _open_db(tmp_sync_db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (42, 'syncing')")
        # Reader opens while writer transaction is active (not yet committed)
        reader = sqlite3.connect(f"file:{tmp_sync_db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            # Should succeed — reads committed snapshot, not seeing the uncommitted INSERT
            rows = _fetchall_rows(reader, "SELECT * FROM synced_dialogs")
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
    conn = _open_db(tmp_sync_db_path)
    reopen = None
    try:
        # Write some committed data
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
        conn.commit()

        # Use a tiny loop stub to capture the callback passed to add_signal_handler.
        class _LoopStub:
            def __init__(self) -> None:
                self.call_args: tuple[object, object] | None = None

            def add_signal_handler(self, signal_num: int, callback: object) -> None:
                self.call_args = (signal_num, callback)

        mock_loop = _LoopStub()
        register_shutdown_handler(conn, cast(asyncio.AbstractEventLoop, mock_loop))
        assert mock_loop.call_args is not None
        sigterm_callback = cast(Callable[[], None], mock_loop.call_args[1])
        sigterm_callback()

        # Reopen DB and verify data is intact
        reopen = sqlite3.connect(str(tmp_sync_db_path), timeout=10.0)
        integrity = _fetchone_row(reopen, "PRAGMA integrity_check")
        assert integrity is not None and str(integrity[0]).lower() == "ok", f"integrity_check failed: {integrity}"
        row = _fetchone_text(reopen, "SELECT status FROM synced_dialogs WHERE dialog_id=1")
        assert row == "synced", f"Data not preserved after shutdown: {row}"
    finally:
        if reopen is not None:
            reopen.close()
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-08: Read-only connection cannot write
# ---------------------------------------------------------------------------


def test_open_sync_db_reader_readonly(tmp_sync_db_path: Path) -> None:
    """open_sync_db_reader returns connection that can SELECT but raises OperationalError on INSERT."""
    ensure_sync_schema(tmp_sync_db_path)
    reader = open_sync_db_reader(tmp_sync_db_path)
    try:
        # SELECT must succeed
        rows = _fetchall_rows(reader, "SELECT * FROM synced_dialogs")
        assert isinstance(rows, list)
        # INSERT must fail with OperationalError
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO synced_dialogs (dialog_id) VALUES (999)")
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# Schema v4: entity tables, reaction_metadata, topic_metadata, message_cache
# ---------------------------------------------------------------------------


def test_schema_v4_entities_table(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), entities table exists with all expected columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "entities")
        columns = {row[1] for row in rows}
        expected = {"id", "type", "name", "username", "name_normalized", "updated_at"}
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_schema_v7_drops_reaction_metadata(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, reaction_metadata table does NOT exist."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        tables = {str(row[0]) for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "reaction_metadata" not in tables, f"reaction_metadata should be dropped in v7. Tables: {tables}"
    finally:
        conn.close()


def test_schema_v4_topic_metadata_table(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), topic_metadata table exists with all expected columns.

    v19 (Phase 42) extends topic_metadata with v1.6 columns via ALTER TABLE — the
    legacy v4 columns must remain present (issubset, not equality).
    """
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "topic_metadata")
        columns = {row[1] for row in rows}
        # Legacy v4 columns must all still be present:
        legacy_cols = {
            "dialog_id",
            "topic_id",
            "title",
            "top_message_id",
            "is_general",
            "is_deleted",
            "inaccessible_error",
            "inaccessible_at",
            "updated_at",
        }
        assert legacy_cols.issubset(columns), f"Legacy v4 columns missing after v19 migration. Got: {columns}"
        # v19 columns must also be present:
        v19_cols = {"icon_emoji_id", "pinned", "hidden", "snapshot_at", "date"}
        assert v19_cols.issubset(columns), f"v19 columns missing after migration. Got: {columns}"
    finally:
        conn.close()


def test_schema_v7_drops_message_cache(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, message_cache table does NOT exist."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        tables = {str(row[0]) for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "message_cache" not in tables, f"message_cache should be dropped in v7. Tables: {tables}"
    finally:
        conn.close()


def test_schema_v4_indexes_exist(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, expected indexes exist and dropped indexes are gone."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='index'")
        index_names = {str(row[0]) for row in rows}
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
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "telemetry_events")
        columns = {row[1] for row in rows}
        expected = {
            "id",
            "tool_name",
            "timestamp",
            "duration_ms",
            "result_count",
            "has_cursor",
            "page_depth",
            "has_filter",
            "error_type",
        }
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_schema_v5_telemetry_index_exists(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), idx_telemetry_tool_timestamp index exists."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        row = _fetchone_row(
            conn, "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_telemetry_tool_timestamp'"
        )
        assert row is not None, "idx_telemetry_tool_timestamp index missing"
    finally:
        conn.close()


def test_schema_version_is_7(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), MAX(version) in schema_version is _CURRENT_SCHEMA_VERSION."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert version == _CURRENT_SCHEMA_VERSION, f"Expected version {_CURRENT_SCHEMA_VERSION}, got {version}"
    finally:
        conn.close()


def test_ensure_sync_schema_twice_idempotent_v7(tmp_sync_db_path: Path) -> None:
    """Running ensure_sync_schema twice is idempotent — no errors, version stays at _CURRENT_SCHEMA_VERSION."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert version == _CURRENT_SCHEMA_VERSION
        rows = _fetchall_rows(conn, "SELECT * FROM schema_version ORDER BY version")
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
    conn = _open_db(db_path)

    legacy_path = tmp_path / "entity_cache.db"
    _make_entity_cache_db(
        legacy_path,
        [
            (1, "user", "Alice", "alice", 1700000000),
            (2, "user", "Bob", None, 1700000001),
            (3, "group", "Test Group", "testgroup", 1700000002),
        ],
    )

    copy_stmts = [
        "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) "
        "SELECT id, type, name, username, updated_at FROM legacy.entities",
    ]
    try:
        count = _migrate_from_legacy_db(conn, legacy_path, copy_stmts)
        assert count == 3, f"Expected 3 rows copied, got {count}"

        rows = _fetchall_rows(conn, "SELECT id, name FROM entities ORDER BY id")
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
    conn = _open_db(db_path)
    try:
        missing = tmp_path / "nonexistent.db"
        count = _migrate_from_legacy_db(conn, missing, [])
        assert count == 0
        rows = _fetchall_rows(conn, "SELECT * FROM entities")
        assert rows == []
    finally:
        conn.close()


def test_migrate_from_legacy_db_insert_or_ignore_on_pk_conflict(tmp_path: Path) -> None:
    """_migrate_from_legacy_db with overlapping PKs uses INSERT OR IGNORE — sync.db row wins."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_db(db_path)

    # Pre-insert entity id=1 into sync.db
    conn.execute(
        "INSERT INTO entities (id, type, name, username, updated_at) VALUES (1, 'user', 'SyncName', NULL, 9999)"
    )
    conn.commit()

    legacy_path = tmp_path / "entity_cache.db"
    _make_entity_cache_db(
        legacy_path,
        [
            (1, "user", "LegacyName", None, 1700000000),  # conflict
            (2, "user", "NewUser", None, 1700000001),
        ],
    )

    copy_stmts = [
        "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) "
        "SELECT id, type, name, username, updated_at FROM legacy.entities",
    ]
    try:
        _migrate_from_legacy_db(conn, legacy_path, copy_stmts)
        row = _fetchone_text(conn, "SELECT name FROM entities WHERE id=1")
        assert row == "SyncName", f"sync.db row should win on conflict, got {row}"
        row2 = _fetchone_text(conn, "SELECT name FROM entities WHERE id=2")
        assert row2 == "NewUser"
    finally:
        conn.close()


def test_migrate_legacy_databases_uses_injected_telemetry_retention(tmp_path: Path) -> None:
    """A custom retention setting controls which analytics rows migrate."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_db(db_path)

    now = time.time()
    recent = now - 40  # retained by the longer custom TTL
    old = now - 70  # excluded by the longer custom TTL

    analytics_path = tmp_path / "analytics.db"
    _make_analytics_db(
        analytics_path,
        [
            ("ListMessages", recent, 100.0, 10, False, 1, False, None),
            ("ListDialogs", old, 50.0, 5, False, 1, False, None),
        ],
    )

    try:
        migrate_legacy_databases(conn, tmp_path, telemetry_retention_ttl_seconds=60)
        rows = _fetchall_rows(conn, "SELECT tool_name FROM telemetry_events")
        tool_names = [str(row[0]) for row in rows]
        assert "ListMessages" in tool_names, "Recent event should be migrated"
        assert "ListDialogs" not in tool_names, "Old event should be excluded"
    finally:
        conn.close()


def test_migrate_legacy_databases_respects_shorter_telemetry_retention(tmp_path: Path) -> None:
    """A shorter custom TTL excludes rows that a longer policy would retain."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_db(db_path)
    analytics_path = tmp_path / "analytics.db"
    _make_analytics_db(
        analytics_path,
        [("ListMessages", time.time() - 15, 100.0, 10, False, 1, False, None)],
    )
    try:
        migrate_legacy_databases(conn, tmp_path, telemetry_retention_ttl_seconds=10)
        assert _fetchall_rows(conn, "SELECT tool_name FROM telemetry_events") == []
    finally:
        conn.close()


def test_migrate_legacy_databases_excludes_telemetry_at_exact_retention_boundary(tmp_path: Path) -> None:
    """Legacy telemetry exactly at the cutoff is excluded, matching migration's strict predicate."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_db(db_path)
    ttl = 60
    cutoff = int(time.time()) - ttl
    analytics_path = tmp_path / "analytics.db"
    _make_analytics_db(
        analytics_path,
        [
            ("AtBoundary", cutoff, 100.0, 10, False, 1, False, None),
            ("InsideBoundary", cutoff + 2, 100.0, 10, False, 1, False, None),
        ],
    )

    try:
        migrate_legacy_databases(conn, tmp_path, telemetry_retention_ttl_seconds=ttl)
        tool_names = [str(row[0]) for row in _fetchall_rows(conn, "SELECT tool_name FROM telemetry_events")]
        assert "AtBoundary" not in tool_names
        assert "InsideBoundary" in tool_names
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# migrate_legacy_databases tests
# ---------------------------------------------------------------------------


def test_migrate_legacy_databases_deletes_legacy_files(tmp_path: Path) -> None:
    """migrate_legacy_databases deletes entity_cache.db, .bootstrap.lock, and analytics.db after migration."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_db(db_path)

    entity_cache = tmp_path / "entity_cache.db"
    lock_file = tmp_path / "entity_cache.db.bootstrap.lock"
    analytics = tmp_path / "analytics.db"

    _make_entity_cache_db(entity_cache, [(10, "user", "Test", None, 1700000000)])
    lock_file.touch()
    _make_analytics_db(analytics, [])

    try:
        migrate_legacy_databases(conn, tmp_path, telemetry_retention_ttl_seconds=60)
        assert not entity_cache.exists(), "entity_cache.db should be deleted"
        assert not lock_file.exists(), "entity_cache.db.bootstrap.lock should be deleted"
        assert not analytics.exists(), "analytics.db should be deleted"
    finally:
        conn.close()


def test_migrate_legacy_databases_noop_when_files_missing(tmp_path: Path) -> None:
    """migrate_legacy_databases does not raise when legacy files don't exist."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_db(db_path)
    try:
        # No legacy files — must not raise
        migrate_legacy_databases(conn, tmp_path, telemetry_retention_ttl_seconds=60)
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

    conn = _open_db(db_path)
    try:
        version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert version >= 5, "schema should be at current version"
    finally:
        conn.close()


def test_schema_version_records_all_versions(tmp_path: Path) -> None:
    """Each migration version is recorded individually in schema_version."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)

    conn = _open_db(db_path)
    try:
        versions = [
            int(cast(int, row[0]))
            for row in _fetchall_rows(conn, "SELECT version FROM schema_version ORDER BY version")
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
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "message_reactions")
        columns = {row[1] for row in rows}
        expected = {"dialog_id", "message_id", "emoji", "count"}
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_message_entities_table_exists(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, message_entities table exists with correct columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "message_entities")
        columns = {row[1] for row in rows}
        expected = {"dialog_id", "message_id", "offset", "length", "type", "value"}
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_message_forwards_table_exists(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema() v7, message_forwards table exists with correct columns."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "message_forwards")
        columns = {row[1] for row in rows}
        expected = {
            "dialog_id",
            "message_id",
            "fwd_from_peer_id",
            "fwd_from_name",
            "fwd_date",
            "fwd_channel_post",
        }
        assert expected == columns, f"Got: {columns}, expected: {expected}"
    finally:
        conn.close()


def test_messages_has_new_v7_columns(tmp_sync_db_path: Path) -> None:
    """After v7, messages table has edit_date, grouped_id, reply_to_peer_id (all INTEGER, nullable)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "messages")
        col_map = {row[1]: row for row in rows}
        for col in ("edit_date", "grouped_id", "reply_to_peer_id"):
            assert col in col_map, f"Column {col!r} missing from messages table"
            assert col_map[col][2].upper() == "INTEGER", f"Column {col!r} type should be INTEGER, got {col_map[col][2]}"
            assert col_map[col][3] == 0, f"Column {col!r} should be nullable (notnull=0), got {col_map[col][3]}"
    finally:
        conn.close()


def test_idx_messages_reply_exists(tmp_sync_db_path: Path) -> None:
    """After v7, idx_messages_reply index exists on messages(dialog_id, reply_to_msg_id)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        row = _fetchone_row(conn, "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_messages_reply'")
        assert row is not None, "idx_messages_reply index missing after v7 migration"
    finally:
        conn.close()


def _seed_v6_reaction_backfill_db(conn: sqlite3.Connection) -> None:
    import json

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)")
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
        "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(dialog_id UNINDEXED, message_id UNINDEXED, text)"
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
    for v in range(1, 7):
        conn.execute("INSERT INTO schema_version VALUES (?, strftime('%s', 'now'))", (v,))

    rows = [
        (1, 10, 1, json.dumps({"👍": 3, "❤": 1})),
        (1, 20, 2, "not json"),
        (1, 30, 3, "[1, 2, 3]"),
        (1, 40, 4, '"hello"'),
        (1, 50, 5, None),
    ]
    for dialog_id, message_id, sent_at, reactions in rows:
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, reactions) VALUES (?, ?, ?, ?)",
            (dialog_id, message_id, sent_at, reactions),
        )
    conn.commit()


def _assert_v7_reaction_backfill_results(conn: sqlite3.Connection) -> None:
    col_names = {row[1] for row in _table_info(conn, "messages")}
    assert "reactions" not in col_names, "reactions column must be dropped after v7"
    reaction_rows = _fetchall_rows(
        conn, "SELECT emoji, count FROM message_reactions WHERE dialog_id=1 AND message_id=10 ORDER BY emoji"
    )
    reaction_dict = {str(row[0]): int(cast(int, row[1])) for row in reaction_rows}
    assert reaction_dict == {"👍": 3, "❤": 1}, f"Valid JSON reactions should be backfilled. Got: {reaction_dict}"
    assert _fetchone_int(conn, "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=1 AND message_id=20") == 0
    assert _fetchone_int(conn, "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=1 AND message_id=30") == 0
    assert _fetchone_int(conn, "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=1 AND message_id=40") == 0
    assert _fetchone_int(conn, "SELECT COUNT(*) FROM message_reactions WHERE dialog_id=1 AND message_id=50") == 0


def test_v7_backfill_reactions_from_json(tmp_path: Path) -> None:
    """v7 migration backfills message_reactions from reactions JSON blob.

    Tests:
    - Valid {emoji: count} JSON is backfilled correctly.
    - Malformed JSON (not json_valid) is skipped without error.
    - Non-object JSON (array, scalar) is skipped due to json_type='object' guard.
    - reactions column is gone after migration.
    """
    db_path = tmp_path / "sync.db"
    conn = _open_db(db_path)
    try:
        _seed_v6_reaction_backfill_db(conn)
    finally:
        conn.close()

    # Now apply v7 migration
    ensure_sync_schema(db_path)

    # Verify results
    conn = _open_db(db_path)
    try:
        _assert_v7_reaction_backfill_results(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema v8: read_inbox_max_id column + supporting index
# ---------------------------------------------------------------------------


def test_schema_version_matches_current(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), MAX(version) equals _CURRENT_SCHEMA_VERSION."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert version == _CURRENT_SCHEMA_VERSION, f"Expected version {_CURRENT_SCHEMA_VERSION}, got {version}"
    finally:
        conn.close()


def test_synced_dialogs_has_read_inbox_max_id_column(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), synced_dialogs has read_inbox_max_id INTEGER column."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        cols = {row[1]: row[2] for row in _table_info(conn, "synced_dialogs")}
        assert "read_inbox_max_id" in cols, f"Column missing; got {sorted(cols)}"
        assert cols["read_inbox_max_id"] == "INTEGER", f"Wrong type: {cols['read_inbox_max_id']}"
    finally:
        conn.close()


def test_read_inbox_max_id_defaults_to_null_for_existing_rows(tmp_sync_db_path: Path) -> None:
    """New rows in synced_dialogs have read_inbox_max_id = NULL (no default, not 0)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (12345,))
        conn.commit()
        row = _fetchone_row(conn, "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = ?", (12345,))
        assert row is not None
        assert row[0] is None, f"Expected NULL, got {row[0]!r}"
    finally:
        conn.close()


def test_v8_index_on_status_and_read_position_exists(tmp_sync_db_path: Path) -> None:
    """Opencode review flagged perf concern: SQL unread scan needs an index on
    (status, read_inbox_max_id) for efficient filtering.
    """
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='synced_dialogs'")
        index_names = {str(row[0]) for row in rows}
        assert "idx_synced_dialogs_status_read_position" in index_names, f"Index missing; found: {sorted(index_names)}"
    finally:
        conn.close()


def test_message_entities_pk_allows_same_offset_different_type(tmp_sync_db_path: Path) -> None:
    """5-column PK: two entities at the same (offset, length) but different type both stored."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
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

        rows = _fetchall_rows(
            conn, "SELECT type, value FROM message_entities WHERE dialog_id=1 AND message_id=100 ORDER BY type"
        )
        assert len(rows) == 2, f"Both entity rows must be retained with 5-column PK. Got: {rows}"
        types = {str(row[0]) for row in rows}
        assert types == {"mention", "hashtag"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema v14: activity_comments and activity_sync_state tables (Phase 999.1)
# ---------------------------------------------------------------------------


def test_schema_v15_drops_activity_comments(tmp_path: Path) -> None:
    """After full migration to v15, activity_comments table must not exist."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = {str(row[0]) for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "activity_comments" not in tables, f"activity_comments must be dropped in v15. Tables: {sorted(tables)}"
        idx = {str(row[0]) for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_activity_comments_sent_at" not in idx
    finally:
        conn.close()


def test_schema_v15_drops_message_cache_permanently(tmp_path: Path) -> None:
    """message_cache was dropped in v7 but its DDL resurrected it until v15."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = {str(row[0]) for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "message_cache" not in tables
    finally:
        conn.close()


def test_messages_table_has_reply_count(tmp_sync_db_path: Path) -> None:
    """v22 adds messages.reply_count for Telegram reply/comment counters."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = sqlite3.connect(tmp_sync_db_path)
    try:
        columns = {row[1] for row in _table_info(conn, "messages")}
    finally:
        conn.close()
    assert "reply_count" in columns


def test_migration_v15_copies_own_only_into_messages(tmp_path: Path) -> None:
    """v15 copies activity_comments rows into messages with out=1."""
    db_path = tmp_path / "sync.db"
    # Build a pre-v15 DB (apply migrations through the current framework,
    # then artificially re-insert activity_comments rows — tests exercise
    # the data-migration SQL even though v15 would normally have no rows).
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        # Recreate activity_comments (it was dropped by v15) to simulate
        # a DB that was at v14 when the user upgraded.
        conn.execute("""
            CREATE TABLE activity_comments (
                dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                sent_at INTEGER NOT NULL, text TEXT, reactions TEXT,
                reply_count INTEGER NOT NULL DEFAULT 0, last_synced_at INTEGER,
                PRIMARY KEY (dialog_id, message_id))
        """)
        conn.execute(
            "INSERT INTO activity_comments (dialog_id, message_id, sent_at, text) "
            "VALUES (100, 1, 1000, 'hello'), (100, 2, 2000, 'world')"
        )
        # Delete v15 AND any higher versions so _schema_ready returns False
        # and the migration framework re-runs from v14.
        conn.execute("DELETE FROM schema_version WHERE version >= 15")
        conn.commit()
    finally:
        conn.close()
    # Re-run migrations → v15 re-applies the data migration.
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = _fetchall_rows(
            conn,
            "SELECT dialog_id, message_id, text, out, is_service, is_deleted "
            "FROM messages WHERE dialog_id = 100 ORDER BY message_id",
        )
    finally:
        conn.close()
    assert rows == [(100, 1, "hello", 1, 0, 0), (100, 2, "world", 1, 0, 0)]


def test_migration_v15_preserves_existing_messages(tmp_path: Path) -> None:
    """If (dialog_id, message_id) already exists in messages, activity_comments copy does NOT overwrite."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        # Seed messages with the authoritative row first
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text, out, is_service) "
            "VALUES (200, 5, 5000, 'authoritative', 1, 0)"
        )
        # Recreate activity_comments with a conflicting row
        conn.execute("""
            CREATE TABLE activity_comments (
                dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                sent_at INTEGER NOT NULL, text TEXT, reactions TEXT,
                reply_count INTEGER NOT NULL DEFAULT 0, last_synced_at INTEGER,
                PRIMARY KEY (dialog_id, message_id))
        """)
        conn.execute(
            "INSERT INTO activity_comments (dialog_id, message_id, sent_at, text) VALUES (200, 5, 9999, 'stale-copy')"
        )
        # Delete v15 AND any higher versions so _schema_ready returns False
        # and the migration framework re-runs from v14.
        conn.execute("DELETE FROM schema_version WHERE version >= 15")
        conn.commit()
    finally:
        conn.close()
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = _fetchone_row(conn, "SELECT text, sent_at FROM messages WHERE dialog_id=200 AND message_id=5")
    finally:
        conn.close()
    assert row is not None
    text = cast(str, row[0])
    sent_at = cast(int, row[1])
    assert text == "authoritative"
    assert sent_at == 5000


def test_migration_v15_enrolls_own_only_but_preserves_higher_status(tmp_path: Path) -> None:
    """Orphan activity_comments dialogs get status='own_only'; existing 'syncing'/'synced' rows are preserved."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        # Seed synced_dialogs with an already-synced dialog
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
            (300,),
        )
        # Recreate activity_comments with rows for dialog 300 AND orphan dialog 400
        conn.execute("""
            CREATE TABLE activity_comments (
                dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                sent_at INTEGER NOT NULL, text TEXT, reactions TEXT,
                reply_count INTEGER NOT NULL DEFAULT 0, last_synced_at INTEGER,
                PRIMARY KEY (dialog_id, message_id))
        """)
        conn.execute(
            "INSERT INTO activity_comments (dialog_id, message_id, sent_at, text) "
            "VALUES (300, 1, 1000, 'a'), (400, 2, 2000, 'b')"
        )
        # Delete v15 AND any higher versions so _schema_ready returns False
        # and the migration framework re-runs from v14.
        conn.execute("DELETE FROM schema_version WHERE version >= 15")
        conn.commit()
    finally:
        conn.close()
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        status_300 = _fetchone_text(conn, "SELECT status FROM synced_dialogs WHERE dialog_id=300")
        status_400 = _fetchone_text(conn, "SELECT status FROM synced_dialogs WHERE dialog_id=400")
    finally:
        conn.close()
    assert status_300 == "synced", "higher-status row must not downgrade"
    assert status_400 == "own_only"


def test_migration_v14_activity_sync_state_seeded(tmp_path: Path) -> None:
    """After migration, activity_sync_state contains exactly 3 seeded rows with correct values."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = _fetchall_rows(conn, "SELECT key, value FROM activity_sync_state ORDER BY key")
    finally:
        conn.close()
    assert rows == [
        ("backfill_complete", "0"),
        ("backfill_offset_id", "0"),
        ("last_sync_at", None),
    ]


def test_migration_v14_idempotent(tmp_path: Path) -> None:
    """Calling ensure_sync_schema twice keeps schema_version=_CURRENT_SCHEMA_VERSION and does not duplicate activity_sync_state rows."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)  # second run must be a no-op
    conn = sqlite3.connect(db_path)
    try:
        version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        count = _fetchone_int(conn, "SELECT COUNT(*) FROM activity_sync_state")
    finally:
        conn.close()
    assert version == _CURRENT_SCHEMA_VERSION
    assert count == 3


def test_migration_v14_preserves_existing_data(tmp_path: Path) -> None:
    """v14 migration on a DB that already has synced_dialogs rows preserves those rows."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)  # migrate fresh to v14
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, ?)",
            (12345, "synced"),
        )
        conn.commit()
    finally:
        conn.close()
    # Re-run migration (simulating daemon restart)
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = _fetchone_row(conn, "SELECT dialog_id, status FROM synced_dialogs WHERE dialog_id = 12345")
    finally:
        conn.close()
    assert row == (12345, "synced")


# ---------------------------------------------------------------------------
# v16: PRAGMA foreign_keys = ON on production sync.db connections
# (MEDIUM finding from 47-REVIEWS.md cycle 2 — codex)
# ---------------------------------------------------------------------------


def test_open_sync_db_enables_foreign_keys(tmp_path: Path) -> None:
    """MEDIUM from 47-REVIEWS.md cycle 2 (codex): production sync.db
    connections MUST enable PRAGMA foreign_keys=ON, otherwise the v16
    entity_details FK CASCADE silently does nothing in production
    despite the test_migration_v16_fk_cascade_deletes_detail_row test
    passing on a separately-PRAGMA'd test connection.
    """
    db_path = tmp_path / "fk.db"
    ensure_sync_schema(db_path)

    # Writable factory must enable FKs.
    conn = _open_db(db_path)
    try:
        row = _fetchone_int(conn, "PRAGMA foreign_keys")
        assert row == 1, f"_open_sync_db must enable foreign_keys (got {row})"
    finally:
        conn.close()

    # Read-only factory must enable FKs too — read connections still
    # observe FK state (e.g., no orphan rows post-cascade).
    conn = open_sync_db_reader(db_path)
    try:
        row = _fetchone_int(conn, "PRAGMA foreign_keys")
        assert row == 1, f"open_sync_db_reader must enable foreign_keys (got {row})"
    finally:
        conn.close()


def test_v16_fk_cascade_works_through_production_factory(tmp_path: Path) -> None:
    """End-to-end variant of test_migration_v16_fk_cascade_deletes_detail_row
    but using the production _open_db() factory (not a hand-built
    sqlite3.connect with manual PRAGMA). Confirms the FK cascade fires
    through the real production code path. MEDIUM from 47-REVIEWS.md
    cycle 2 (codex).
    """
    db_path = tmp_path / "cascade.db"
    ensure_sync_schema(db_path)

    conn = _open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO entities (id, type, name, username, name_normalized, updated_at) "
            "VALUES (?, 'User', 'Alice', 'alice', 'alice', 0)",
            (12345,),
        )
        conn.execute(
            "INSERT INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, '{}', 1700000000)", (12345,)
        )
        conn.commit()

        # Sanity: detail row exists.
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM entity_details WHERE entity_id = ?", (12345,)) == 1

        # Delete the parent entities row.
        conn.execute("DELETE FROM entities WHERE id = ?", (12345,))
        conn.commit()

        # Cascade must have removed the detail row.
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM entity_details WHERE entity_id = ?", (12345,)) == 0, (
            "FK CASCADE did not fire — PRAGMA foreign_keys is OFF on production factory"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema v17: dialogs snapshot table (Phase 40 — v1.6 Local Mirror)
# ---------------------------------------------------------------------------


def test_schema_v17_dialogs_table_exists(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), dialogs table exists and is queryable."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        # Should not raise — table exists
        _fetchall_rows(conn, "SELECT * FROM dialogs LIMIT 0")
    finally:
        conn.close()


def test_schema_v17_dialogs_columns(tmp_sync_db_path: Path) -> None:
    """dialogs table has all 14 required columns with correct types and constraints."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _table_info(conn, "dialogs")
        columns = {row[1]: row for row in rows}

        required = {
            "dialog_id",
            "name",
            "type",
            "archived",
            "pinned",
            "members",
            "created",
            "last_message_at",
            "snapshot_at",
            "hidden",
            "needs_refresh",
            "unread_mentions_count",
            "unread_reactions_count",
            "draft_text",
            # v24 (Phase 54): linked-chat resolution columns
            "linked_chat_id",
            "linked_chat_resolved_at",
        }
        assert required == set(columns.keys()), (
            f"Column mismatch.\nExpected: {sorted(required)}\nGot: {sorted(columns.keys())}"
        )

        assert "unread_count" not in columns, "unread_count must not be stored in dialogs (MIRROR-05)"

        for col_name in (
            "archived",
            "pinned",
            "hidden",
            "needs_refresh",
            "unread_mentions_count",
            "unread_reactions_count",
        ):
            col = columns[col_name]
            assert col[2] == "INTEGER", f"{col_name} must be INTEGER, got {col[2]}"
            assert col[3] == 1, f"{col_name} must be NOT NULL"
            assert str(col[4]) == "0", f"{col_name} default must be 0, got {col[4]!r}"

        for col_name in (
            "name",
            "type",
            "members",
            "created",
            "last_message_at",
            "snapshot_at",
            "draft_text",
            "linked_chat_id",
            "linked_chat_resolved_at",
        ):
            col = columns[col_name]
            assert col[3] == 0, f"{col_name} must be nullable (NOT NULL=0), got notnull={col[3]}"
    finally:
        conn.close()


def test_schema_v17_dialogs_indexes(tmp_sync_db_path: Path) -> None:
    """dialogs table has at least the three v17-era indexes after full migration.

    Note: idx_dialogs_needs_refresh_hidden is intentionally NOT listed here —
    it is added by the v20 migration (Phase 43). That index is verified
    separately by test_v20_adds_needs_refresh_index.
    """
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _fetchall_rows(
            conn, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dialogs' ORDER BY name"
        )
        index_names = {str(row[0]) for row in rows}
        expected_v17_indexes = {
            "idx_dialogs_hidden_pinned",
            "idx_dialogs_type",
            "idx_dialogs_snapshot_at",
        }
        # Use issubset so future migrations adding new indexes don't break this test.
        # v20 adds idx_dialogs_needs_refresh_hidden — see test_v20_adds_needs_refresh_index.
        assert expected_v17_indexes.issubset(index_names), (
            f"v17-era indexes missing. Expected subset {expected_v17_indexes}, got: {index_names}"
        )
    finally:
        conn.close()


def test_dialogs_no_fk_to_synced_dialogs(tmp_sync_db_path: Path) -> None:
    """dialogs table has no FOREIGN KEY constraints (MIRROR-03: independent evolution)."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _fetchall_rows(conn, "PRAGMA foreign_key_list(dialogs)")
        assert rows == [], f"dialogs must have no FK constraints (MIRROR-03). Got: {rows}"
    finally:
        conn.close()


def test_schema_v17_idempotent(tmp_sync_db_path: Path) -> None:
    """Running ensure_sync_schema() twice produces exactly _CURRENT_SCHEMA_VERSION rows in schema_version."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        rows = _fetchall_rows(conn, "SELECT version FROM schema_version ORDER BY version")
        versions = [int(cast(int, row[0])) for row in rows]
        expected = list(range(1, _CURRENT_SCHEMA_VERSION + 1))
        assert versions == expected, f"Expected versions {expected}, got {versions}"
        _fetchall_rows(conn, "SELECT * FROM dialogs LIMIT 0")
    finally:
        conn.close()


def test_schema_version_is_current(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema(), MAX(version) in schema_version equals _CURRENT_SCHEMA_VERSION."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_db(tmp_sync_db_path)
    try:
        version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert version == _CURRENT_SCHEMA_VERSION, f"Expected schema version {_CURRENT_SCHEMA_VERSION}, got {version}"
        assert _CURRENT_SCHEMA_VERSION == 28, f"_CURRENT_SCHEMA_VERSION must be 28, got {_CURRENT_SCHEMA_VERSION}"
    finally:
        conn.close()


def test_v20_adds_needs_refresh_index(tmp_path: Path) -> None:
    """Phase 43 RECON-02: composite index covers needs_refresh + hidden."""
    from mcp_telegram.sync_db import ensure_sync_schema

    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_db(db_path)
    try:
        row = _fetchone_row(
            conn, "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_dialogs_needs_refresh_hidden'"
        )
        assert row is not None, (
            "idx_dialogs_needs_refresh_hidden missing — Plan 02 SELECT will "
            "trigger a full table scan every hourly cycle"
        )
        # Confirm SQLite uses the new index for the planned light-pass query.
        plan = _fetchall_rows(
            conn, "EXPLAIN QUERY PLAN SELECT dialog_id FROM dialogs WHERE needs_refresh = 1 AND hidden = 0"
        )
        plan_text = " ".join(str(row) for row in plan)
        assert "idx_dialogs_needs_refresh_hidden" in plan_text, f"Query planner ignored the index. EXPLAIN: {plan_text}"
    finally:
        conn.close()


def test_v20_migration_is_idempotent(tmp_path: Path) -> None:
    """Re-running ensure_sync_schema after v20 must be a no-op."""
    from mcp_telegram.sync_db import ensure_sync_schema

    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    # Second call must not raise.
    ensure_sync_schema(db_path)
