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
