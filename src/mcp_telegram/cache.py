from __future__ import annotations

import json
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

        # Create indexes for performance optimization
        # idx_entities_type_updated: for TTL filtering in all_names_with_ttl()
        # Allows efficient seeks by (type, updated_at) instead of full table scan
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entities_type_updated
            ON entities(type, updated_at)
        """)

        # idx_entities_username: for username lookups in get_by_username()
        # Allows efficient seeks by username instead of full table scan
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entities_username
            ON entities(username)
        """)

        self._conn.commit()

        # Rebuild statistics so query planner uses the new indexes
        self._conn.execute("PRAGMA optimize")
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
        _, entity_type, name, username, updated_at = row
        if int(time.time()) - updated_at > ttl_seconds:
            return None
        return {
            "id": entity_id,
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

    def get_by_username(self, username: str) -> tuple[int, str] | None:
        """Return (entity_id, name) for entity with matching username, or None."""
        row = self._conn.execute(
            "SELECT id, name FROM entities WHERE username = ?",
            (username,)
        ).fetchone()
        return row if row else None

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


class ReactionMetadataCache:
    """SQLite-backed cache for reaction metadata (reactor names) per message with TTL support."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialize the reaction_metadata table and index on the given connection.

        Args:
            conn: Shared SQLite connection (from EntityCache._conn).
        """
        self._conn = conn
        self._init_table()

    def _init_table(self) -> None:
        """Create reaction_metadata table and index if they don't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS reaction_metadata (
                message_id INTEGER NOT NULL,
                dialog_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                reactor_names TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                PRIMARY KEY (message_id, dialog_id, emoji)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reactions_dialog_message
            ON reaction_metadata(dialog_id, message_id)
        """)
        self._conn.commit()

    def get(self, message_id: int, dialog_id: int, ttl_seconds: int = 600) -> dict[str, list[str]] | None:
        """Return cached reactions {emoji: [names]} if fresh, else None.

        Args:
            message_id: Telegram message ID.
            dialog_id: Telegram dialog/chat ID.
            ttl_seconds: Time-to-live in seconds (default 600 = 10 min).

        Returns:
            {emoji: [reactor_names]} dict if cache hit and fresh, else None.
        """
        now = int(time.time())
        rows = self._conn.execute(
            """SELECT emoji, reactor_names FROM reaction_metadata
               WHERE message_id = ? AND dialog_id = ? AND fetched_at >= ?""",
            (message_id, dialog_id, now - ttl_seconds),
        ).fetchall()
        if not rows:
            return None
        return {emoji: json.loads(names) for emoji, names in rows}

    def upsert(
        self, message_id: int, dialog_id: int, reactions_by_emoji: dict[str, list[str]]
    ) -> None:
        """Cache reaction names for a message.

        Args:
            message_id: Telegram message ID.
            dialog_id: Telegram dialog/chat ID.
            reactions_by_emoji: {emoji: [reactor_names, ...], ...} dict to cache.
        """
        now = int(time.time())
        self._conn.executemany(
            """INSERT OR REPLACE INTO reaction_metadata
               (message_id, dialog_id, emoji, reactor_names, fetched_at)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (message_id, dialog_id, emoji, json.dumps(names), now)
                for emoji, names in reactions_by_emoji.items()
            ],
        )
        self._conn.commit()
