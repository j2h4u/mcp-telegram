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
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='message_reactions_freshness'"
            )
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


def test_schema_version_records_11(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row[0] == 11
        assert _CURRENT_SCHEMA_VERSION == 11
    finally:
        conn.close()
