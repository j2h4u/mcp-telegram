"""Current full-history enrollment schema and bootstrap behavior."""

import sqlite3
from contextlib import closing
from pathlib import Path

from mcp_telegram.sync_db import ensure_sync_schema


def test_current_enrollment_decisions_survive_schema_reopen(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(?,?,?)",
            [(1, 1, 101), (2, 0, 202)],
        )
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES(1,3,100,'retained')")

    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute(
            "SELECT dialog_id,enabled,updated_at FROM full_history_enrollment ORDER BY dialog_id"
        ).fetchall() == [
            (1, 1, 101),
            (2, 0, 202),
        ]
        assert conn.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=3").fetchone() == ("retained",)
        assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version=94").fetchone() == (1,)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_full_history_enrollment_enabled'"
        ).fetchone() == (1,)
