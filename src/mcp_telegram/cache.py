from __future__ import annotations

import sqlite3
import time
from pathlib import Path

USER_TTL: int = 2_592_000   # 30 days
GROUP_TTL: int = 604_800    # 7 days

_DDL = """
CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    username   TEXT,
    updated_at INTEGER NOT NULL
);
"""


class EntityCache:
    """SQLite-backed cache for Telegram entity metadata with TTL support."""

    def __init__(self, db_path: Path) -> None:
        """Open (or create) the SQLite database at db_path and ensure the schema exists."""
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.isolation_level = ""  # back to transactional
        self._conn.execute(_DDL)
        self._conn.commit()

    def upsert(
        self,
        entity_id: int,
        entity_type: str,
        name: str,
        username: str | None,
    ) -> None:
        """Insert or replace entity metadata, updating updated_at to now."""
        self._conn.execute(
            "INSERT OR REPLACE INTO entities (id, type, name, username, updated_at) VALUES (?, ?, ?, ?, ?)",
            (entity_id, entity_type, name, username, int(time.time())),
        )
        self._conn.commit()

    def get(self, entity_id: int, ttl_seconds: int) -> dict | None:
        """Return entity dict or None if not found or TTL expired.

        Dict keys: id, type, name, username.
        """
        row = self._conn.execute(
            "SELECT id, type, name, username, updated_at FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        entity_id_db, entity_type, name, username, updated_at = row
        if int(time.time()) - updated_at > ttl_seconds:
            return None
        return {
            "id": entity_id_db,
            "type": entity_type,
            "name": name,
            "username": username,
        }

    def all_names(self) -> dict[int, str]:
        """Return {entity_id: name} for all records (no TTL filtering — caller decides)."""
        rows = self._conn.execute("SELECT id, name FROM entities").fetchall()
        return {row[0]: row[1] for row in rows}

    def all_names_with_ttl(self, user_ttl: int, group_ttl: int) -> dict[int, str]:
        """Return {entity_id: name} filtered by type-specific TTL.

        Users: excluded if updated_at < now - user_ttl.
        Groups/channels: excluded if updated_at < now - group_ttl.
        """
        now = int(time.time())
        rows = self._conn.execute(
            """SELECT id, name FROM entities
               WHERE (type = 'user' AND updated_at >= ?)
                  OR (type != 'user' AND updated_at >= ?)""",
            (now - user_ttl, now - group_ttl),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
