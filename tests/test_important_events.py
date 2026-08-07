"""Important events read-model tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp_telegram.important_events.read_model import list_important_events
from mcp_telegram.sync_db import ensure_sync_schema, record_daemon_event


def test_list_important_events_returns_recent_access_events_with_titles(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO entities (id, type, name, updated_at) VALUES (?, ?, ?, ?)",
            (123, "Channel", "Work Chat", 1_700_000_000),
        )
        record_daemon_event(conn, kind="access_lost", dialog_id=123, occurred_at=1_700_000_000)
        record_daemon_event(conn, kind="access_restored", dialog_id=123, occurred_at=1_700_003_600)
        record_daemon_event(conn, kind="irrelevant", dialog_id=123, occurred_at=1_700_003_700)
        conn.commit()

        assert list_important_events(conn, last_hours=2, timezone="Asia/Almaty", now=1_700_003_700) == [
            {
                "time": "2023-11-15T05:13:20+06:00",
                "time_basis": "observed",
                "type": "access_restored",
                "summary": "Access restored",
                "dialog_id": 123,
                "dialog_title": "Work Chat",
                "message_id": None,
            },
            {
                "time": "2023-11-15T04:13:20+06:00",
                "time_basis": "observed",
                "type": "access_lost",
                "summary": "Access lost",
                "dialog_id": 123,
                "dialog_title": "Work Chat",
                "message_id": None,
            },
        ]
    finally:
        conn.close()
