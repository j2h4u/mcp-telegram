"""Image schema migration preserves ordered historical upgrades and facts."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import mcp_telegram.sync_db as sync_db
from mcp_telegram.sync_db import ensure_sync_schema


@pytest.mark.parametrize("version", [60, 61, 62, 84, 92])
def test_historical_schema_cap_then_upgrade_preserves_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", version)
        ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall() == [
            (number,) for number in range(1, version + 1)
        ]
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'message_image_%'").fetchall()
            == []
        )
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES(42,7,100,'retained')")
        conn.commit()
    ensure_sync_schema(path)
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall() == [
            (number,) for number in range(1, sync_db._CURRENT_SCHEMA_VERSION + 1)
        ]
        assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=7").fetchone() == ("retained",)
        for table in ("message_image_sources", "message_image_jobs", "message_image_artifacts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
