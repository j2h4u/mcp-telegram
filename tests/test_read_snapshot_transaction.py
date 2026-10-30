"""Read snapshots retain their own boundaries on real SQLite connections."""

import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.sync_transactions import read_snapshot


def test_read_only_snapshot_is_stable_and_releases_its_transaction(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.sqlite"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE facts(value INTEGER)")
    writer.execute("INSERT INTO facts VALUES (1)")
    writer.commit()
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with read_snapshot(reader):
            assert reader.in_transaction
            assert reader.execute("SELECT value FROM facts").fetchone() == (1,)
            writer.execute("UPDATE facts SET value=2")
            writer.commit()
            assert reader.execute("SELECT value FROM facts").fetchone() == (1,)
        assert not reader.in_transaction
        assert reader.execute("SELECT value FROM facts").fetchone() == (2,)
        with pytest.raises(ValueError, match="stop"), read_snapshot(reader):
            raise ValueError("stop")
        assert not reader.in_transaction
    finally:
        reader.close()
        writer.close()


def test_caller_transaction_is_never_rolled_back_even_on_error() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE facts(value INTEGER)")
        conn.execute("INSERT INTO facts VALUES (1)")
        with pytest.raises(ValueError, match="stop"), read_snapshot(conn):
            raise ValueError("stop")
        assert conn.in_transaction
        assert conn.execute("SELECT value FROM facts").fetchone() == (1,)
        conn.rollback()
        assert conn.execute("SELECT value FROM facts").fetchall() == []
    finally:
        conn.close()
