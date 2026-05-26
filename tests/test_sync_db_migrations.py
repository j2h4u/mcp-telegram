"""Tests for sync_db migrations — Phase 39.2-01 Task 3.

Covers v11: message_reactions_freshness side-table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.sync_db import _CURRENT_SCHEMA_VERSION, _open_sync_db, ensure_sync_schema


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sync.db"


def test_migration_v11_creates_freshness_table(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        rows = list(
            conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='message_reactions_freshness'")
        )
        assert rows == [("message_reactions_freshness",)]
        cols = list(conn.execute("PRAGMA table_info(message_reactions_freshness)"))
        # Each row: (cid, name, type, notnull, dflt_value, pk)
        col_map = {c[1]: (c[2], c[3], c[5]) for c in cols}
        assert col_map["dialog_id"] == ("INTEGER", 1, 1)
        assert col_map["message_id"] == ("INTEGER", 1, 2)
        assert col_map["checked_at"] == ("INTEGER", 1, 0)
    finally:
        conn.close()


def test_migration_v11_idempotent(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)  # second call: must not raise
    conn = _open_sync_db(db_path)
    try:
        cols_before = list(conn.execute("PRAGMA table_info(message_reactions_freshness)"))
        assert len(cols_before) == 3
    finally:
        conn.close()


def test_migration_v11_without_rowid(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='message_reactions_freshness'"
        ).fetchone()
        assert row is not None
        assert "WITHOUT ROWID" in row[0].upper()
    finally:
        conn.close()


def test_migration_v11_does_not_touch_synced_dialogs(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(synced_dialogs)")]
        assert "reactions_reconciled_at" not in cols
    finally:
        conn.close()


def test_schema_version_records_current_v11(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row[0] == _CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v12: synced_dialogs.read_outbox_max_id (Phase 39.3-01 Task 3)
# ---------------------------------------------------------------------------


def _col_info(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    # PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk)
    return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_v12_adds_outbox_column(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        cols = _col_info(conn, "synced_dialogs")
        assert "read_outbox_max_id" in cols
        # (cid, name, type, notnull, dflt_value, pk)
        _, _, col_type, notnull, _, _ = cols["read_outbox_max_id"]
        assert col_type == "INTEGER"
        assert notnull == 0  # nullable
    finally:
        conn.close()


def test_migration_v12_existing_rows_have_null_outbox(db_path: Path, tmp_path: Path) -> None:
    # Build a v11-shaped DB by bootstrapping current schema then proving
    # that if we pre-insert a row prior to a re-run, the outbox is NULL.
    # Re-applying ensure_sync_schema is a no-op beyond current version, so
    # instead we exercise the "pre-existing row after migration" scenario:
    # insert a row after schema exists and confirm NULL is the default state.
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', ?)",
            (4242, 5),
        )
        conn.commit()
        row = conn.execute("SELECT read_outbox_max_id FROM synced_dialogs WHERE dialog_id=?", (4242,)).fetchone()
        assert row[0] is None, "new rows default read_outbox_max_id to NULL"
    finally:
        conn.close()


def test_migration_v12_idempotent(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    # Second call must not raise (SQLite ALTER TABLE ADD COLUMN would otherwise
    # fail with 'duplicate column name'; the _migrate framework guards via
    # schema_version and must skip already-applied versions).
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        cols = _col_info(conn, "synced_dialogs")
        assert "read_outbox_max_id" in cols
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row[0] == _CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_schema_version_records_current_v12(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row[0] == _CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_v12_does_not_drop_inbox_column(db_path: Path) -> None:
    """Regression guard: v12 adds the outbox column without touching inbox."""
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        cols = _col_info(conn, "synced_dialogs")
        assert "read_inbox_max_id" in cols
        # Inbox column is still writable via the existing monotonic primitive.
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', ?)",
            (7777, 123),
        )
        conn.commit()
        row = conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id=?", (7777,)).fetchone()
        assert row[0] == 123
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v16: entity_details sibling table (Phase 47-01)
# ---------------------------------------------------------------------------


def test_schema_v16_creates_entity_details(tmp_path: Path) -> None:
    """v16 creates the entity_details sibling table per CONTEXT D-01."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        cols = {(r[1], r[2]) for r in conn.execute(
            "PRAGMA table_info(entity_details)"
        ).fetchall()}
    assert cols == {
        ("entity_id", "INTEGER"),
        ("detail_json", "TEXT"),
        ("fetched_at", "INTEGER"),
    }, f"entity_details columns mismatch: {cols}"


def test_schema_v16_creates_fetched_at_index(tmp_path: Path) -> None:
    """v16 adds an index on entity_details.fetched_at for future eviction sweeps (D-04)."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='entity_details'"
        ).fetchall()}
    assert "idx_entity_details_fetched_at" in idx


def test_migration_v16_fk_cascade_deletes_detail_row(tmp_path: Path) -> None:
    """Deleting an entities row CASCADES to delete the matching entity_details row."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")  # SQLite defaults FKs OFF per connection
        conn.execute(
            "INSERT INTO entities (id, type, name, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (42, 'user', 'Alice', 1000),
        )
        conn.execute(
            "INSERT INTO entity_details (entity_id, detail_json, fetched_at) "
            "VALUES (?, ?, ?)",
            (42, '{"schema": 1, "type": "user"}', 1000),
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_details WHERE entity_id=42"
        ).fetchone()[0] == 1
        conn.execute("DELETE FROM entities WHERE id = 42")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_details WHERE entity_id=42"
        ).fetchone()[0] == 0, "FK CASCADE failed"


def test_migration_v16_idempotent(tmp_path: Path) -> None:
    """Running ensure_sync_schema twice is a no-op — schema_version has _CURRENT_SCHEMA_VERSION rows, MAX=_CURRENT_SCHEMA_VERSION."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)  # second call must be a no-op
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        max_v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert count == _CURRENT_SCHEMA_VERSION
    assert max_v == _CURRENT_SCHEMA_VERSION


def test_migration_v16_does_not_touch_entities_columns(tmp_path: Path) -> None:
    """SPEC Constraint #4: v16 does NOT widen the entities table.

    The exact column set must remain {id, type, name, username, name_normalized, updated_at}.
    """
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(entities)"
        ).fetchall()}
    assert cols == {"id", "type", "name", "username", "name_normalized", "updated_at"}, (
        f"entities columns must not change in v16; got {cols}"
    )


# ---------------------------------------------------------------------------
# v18: daemon_state KV table (Phase 41 — bootstrap sweep cursor + flags)
# ---------------------------------------------------------------------------


def test_migration_v18_creates_daemon_state_table(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        rows = list(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='daemon_state'"
            )
        )
        assert rows == [("daemon_state",)]
        cols = list(conn.execute("PRAGMA table_info(daemon_state)"))
        col_map = {c[1]: (c[2], c[3], c[5]) for c in cols}
        # name -> (type, notnull, pk)
        assert col_map["key"] == ("TEXT", 0, 1)
        assert col_map["value"] == ("TEXT", 0, 0)
    finally:
        conn.close()


def test_migration_v18_daemon_state_empty_after_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM daemon_state").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_migration_v18_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "INSERT INTO daemon_state(key, value) VALUES ('probe', 'value')"
        )
        conn.commit()
    finally:
        conn.close()

    ensure_sync_schema(db_path)  # second call: must not raise or wipe data

    conn = _open_sync_db(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM daemon_state WHERE key = 'probe'"
        ).fetchone()
        assert row == ("value",)
    finally:
        conn.close()


def test_schema_version_records_current_v18(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        max_version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert max_version == _CURRENT_SCHEMA_VERSION
        assert _CURRENT_SCHEMA_VERSION == 22  # Phase 52 follow-up lock — flips when next migration ships
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v19: topic_metadata augmentation with v1.6 columns (Phase 42)
# ---------------------------------------------------------------------------


def test_migration_v19_adds_v1_6_columns_to_topic_metadata(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        cols = {c[1] for c in conn.execute("PRAGMA table_info(topic_metadata)")}
        # Legacy v4 columns retained:
        assert {"dialog_id", "topic_id", "title", "top_message_id",
                "is_general", "is_deleted", "inaccessible_error",
                "inaccessible_at", "updated_at"}.issubset(cols)
        # New v19 columns:
        assert {"icon_emoji_id", "pinned", "hidden",
                "snapshot_at", "date"}.issubset(cols)
    finally:
        conn.close()


def test_migration_v19_preserves_legacy_topic_metadata_columns(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        # Insert a legacy-shaped row (only legacy columns set explicitly).
        conn.execute(
            "INSERT INTO topic_metadata "
            "(dialog_id, topic_id, title, top_message_id, is_general, is_deleted, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (-1001234, 1, "General", 1, 1, 0, 1700000000),
        )
        conn.commit()
        row = conn.execute(
            "SELECT title, is_general, is_deleted, pinned, hidden FROM topic_metadata "
            "WHERE dialog_id=? AND topic_id=?", (-1001234, 1),
        ).fetchone()
        assert row[0] == "General"
        assert row[1] == 1
        assert row[2] == 0
        # New columns default to 0 (NOT NULL DEFAULT 0):
        assert row[3] == 0
        assert row[4] == 0
    finally:
        conn.close()


def test_migration_v19_idempotent(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)   # second call must not raise
    conn = _open_sync_db(db_path)
    try:
        cols = {c[1] for c in conn.execute("PRAGMA table_info(topic_metadata)")}
        assert {"icon_emoji_id", "pinned", "hidden",
                "snapshot_at", "date"}.issubset(cols)
    finally:
        conn.close()


def test_migration_v19_pinned_default_zero(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "INSERT INTO topic_metadata "
            "(dialog_id, topic_id, title, is_general, is_deleted, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (-1009999, 1, "T", 0, 0, 1700000000),
        )
        conn.commit()
        row = conn.execute(
            "SELECT pinned, hidden FROM topic_metadata "
            "WHERE dialog_id=-1009999 AND topic_id=1"
        ).fetchone()
        assert row == (0, 0)
    finally:
        conn.close()


def test_migration_v19_does_not_break_existing_left_join(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "INSERT INTO topic_metadata "
            "(dialog_id, topic_id, title, is_general, is_deleted, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (-1001234, 5, "General Discussion", 0, 0, 1700000000),
        )
        conn.commit()
        # Mirror the daemon_api.py:573 LEFT JOIN expression in isolation:
        row = conn.execute(
            "SELECT tm.title FROM topic_metadata tm "
            "WHERE tm.dialog_id = ? AND tm.topic_id = ?",
            (-1001234, 5),
        ).fetchone()
        assert row[0] == "General Discussion"
    finally:
        conn.close()


def test_schema_version_records_v19(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        # v19 must be present in the version history (migration ran).
        row = conn.execute(
            "SELECT version FROM schema_version WHERE version = 19"
        ).fetchone()
        assert row is not None, "v19 migration did not run"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v21: trace_coverage_fragments (Phase 51 — Account Trace)
# ---------------------------------------------------------------------------


def test_migration_v21_creates_trace_coverage_fragments(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        rows = list(
            conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='trace_coverage_fragments'"
            )
        )
        assert rows == [("trace_coverage_fragments",)]
        cols = list(conn.execute("PRAGMA table_info(trace_coverage_fragments)"))
        col_map = {c[1]: (c[2], c[3], c[4], c[5]) for c in cols}
        assert col_map["target_user_id"] == ("INTEGER", 1, None, 1)
        assert col_map["dialog_id"] == ("INTEGER", 1, None, 2)
        assert col_map["topic_id"] == ("INTEGER", 1, "0", 3)
        assert col_map["coverage_kind"] == ("TEXT", 1, None, 4)
        assert col_map["status"] == ("TEXT", 1, None, 0)
        assert col_map["created_at"] == ("INTEGER", 1, None, 0)
        assert col_map["updated_at"] == ("INTEGER", 1, None, 0)
    finally:
        conn.close()


def test_migration_v21_creates_target_status_index(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='trace_coverage_fragments'"
            )
        }
        assert "idx_trace_coverage_target_status" in indexes
    finally:
        conn.close()


def test_migration_v21_accepts_dialog_level_trace_fragment(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO trace_coverage_fragments
                (target_user_id, dialog_id, topic_id, coverage_kind, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (101, -100123, 0, "authored_message", "pending", 1700000000, 1700000001),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT target_user_id, dialog_id, topic_id, status, created_at, updated_at
            FROM trace_coverage_fragments
            WHERE target_user_id = 101
            """
        ).fetchone()
        assert row == (101, -100123, 0, "pending", 1700000000, 1700000001)
    finally:
        conn.close()


def test_migration_v21_topic_zero_reserved_for_dialog_level(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "INSERT INTO topic_metadata "
            "(dialog_id, topic_id, title, is_general, is_deleted, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (-100123, 1, "General", 1, 0, 1700000000),
        )
        conn.commit()
        topic_ids = [
            row[0]
            for row in conn.execute(
                "SELECT topic_id FROM topic_metadata WHERE dialog_id = -100123"
            )
        ]
        assert topic_ids == [1]
        assert 0 not in topic_ids
    finally:
        conn.close()


def test_migration_v21_runs_from_v20_database(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE schema_version (
                version INTEGER NOT NULL,
                applied_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                dialog_id  INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sent_at    INTEGER NOT NULL,
                PRIMARY KEY (dialog_id, message_id)
            ) WITHOUT ROWID
            """
        )
        conn.execute("INSERT INTO schema_version VALUES (20, 1700000000)")
        conn.commit()

    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='trace_coverage_fragments'"
        ).fetchone() == ("trace_coverage_fragments",)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "reply_count" in columns
        max_version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert max_version == 22
    finally:
        conn.close()
