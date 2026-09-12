"""Focused tests for the remaining dialog entity reconciliation surface."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


@pytest.fixture
def sync_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        yield conn
    finally:
        conn.close()


def test_set_access_lost_atomic(sync_db: sqlite3.Connection) -> None:
    """Access loss updates the synced and projected dialog rows together."""
    from mcp_telegram.access_lifecycle import set_access_lost

    dialog_id = 12345
    now = 1700000000
    with sync_db:
        sync_db.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'syncing')", (dialog_id,))
        sync_db.execute(
            "INSERT INTO dialogs (dialog_id, name, type, archived, pinned, snapshot_at, hidden, needs_refresh) "
            "VALUES (?, 'Test', 'user', 0, 0, ?, 0, 0)",
            (dialog_id, now - 1000),
        )

    set_access_lost(sync_db, dialog_id, now)

    assert sync_db.execute(
        "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == (
        "access_lost",
        now,
    )
    assert sync_db.execute("SELECT hidden, snapshot_at FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        1,
        now,
    )


def test_set_access_lost_no_op_on_missing_rows(sync_db: sqlite3.Connection) -> None:
    from mcp_telegram.access_lifecycle import set_access_lost

    set_access_lost(sync_db, 99999, 1700000000)
