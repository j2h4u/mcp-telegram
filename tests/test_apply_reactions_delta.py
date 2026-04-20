"""Tests for sync_worker.apply_reactions_delta — per-message reaction primitive.

Per Phase 39.2-01 Task 1. Helper is the shared per-message primitive used by
event handlers and JIT freshen path. FullSyncWorker's batched executemany
path is intentionally NOT refactored.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_worker import apply_reactions_delta


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "sync.db"
    ensure_sync_schema(db)
    c = _open_sync_db(db)
    c.execute(
        "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (12345,),
    )
    c.commit()
    yield c
    c.close()


def _rows(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> list[tuple]:
    return list(
        conn.execute(
            "SELECT emoji, count FROM message_reactions "
            "WHERE dialog_id=? AND message_id=? ORDER BY emoji",
            (dialog_id, message_id),
        )
    )


def test_apply_reactions_delta_inserts_fresh_rows(conn: sqlite3.Connection) -> None:
    rows = [(12345, 100, "👍", 3), (12345, 100, "❤", 1)]
    with conn:
        apply_reactions_delta(conn, 12345, 100, rows)
    assert _rows(conn, 12345, 100) == [("❤", 1), ("👍", 3)]


def test_apply_reactions_delta_replaces_existing(conn: sqlite3.Connection) -> None:
    with conn:
        apply_reactions_delta(conn, 12345, 100, [(12345, 100, "👍", 3), (12345, 100, "❤", 1)])
    with conn:
        apply_reactions_delta(conn, 12345, 100, [(12345, 100, "🔥", 5)])
    assert _rows(conn, 12345, 100) == [("🔥", 5)]


def test_apply_reactions_delta_empty_rows_deletes_existing(conn: sqlite3.Connection) -> None:
    with conn:
        apply_reactions_delta(conn, 12345, 100, [(12345, 100, "👍", 3)])
    with conn:
        apply_reactions_delta(conn, 12345, 100, [])
    assert _rows(conn, 12345, 100) == []


def test_apply_reactions_delta_idempotent(conn: sqlite3.Connection) -> None:
    rows = [(12345, 100, "👍", 3), (12345, 100, "❤", 1)]
    with conn:
        apply_reactions_delta(conn, 12345, 100, rows)
    with conn:
        apply_reactions_delta(conn, 12345, 100, rows)
    assert _rows(conn, 12345, 100) == [("❤", 1), ("👍", 3)]


def test_apply_reactions_delta_scoped_to_message_id(conn: sqlite3.Connection) -> None:
    with conn:
        apply_reactions_delta(conn, 12345, 100, [(12345, 100, "👍", 3)])
        apply_reactions_delta(conn, 12345, 101, [(12345, 101, "❤", 7)])
    # Now mutate only msg 100; msg 101 must be untouched.
    with conn:
        apply_reactions_delta(conn, 12345, 100, [(12345, 100, "🔥", 1)])
    assert _rows(conn, 12345, 100) == [("🔥", 1)]
    assert _rows(conn, 12345, 101) == [("❤", 7)]
