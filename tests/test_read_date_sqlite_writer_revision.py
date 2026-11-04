"""Read-date revisions use durable values on the actual SQLite writer."""

import sqlite3
from collections.abc import Callable
from pathlib import Path

from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_fact_queries import persist_read_at


async def test_read_date_revision_is_stable_on_sqlite_writer(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    try:

        def enroll(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")

        await writer.run(enroll)

        def store() -> Callable[[sqlite3.Connection], str | None]:
            return lambda conn: persist_read_at(conn, 55, 7, read_at=123, checked_at=200, status="complete")

        assert await writer.run(store()) == repr(("complete", 123, "resolved"))
        assert await writer.run(store()) is None
    finally:
        await writer.aclose()
