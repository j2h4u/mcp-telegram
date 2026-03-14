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
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
        self._conn.execute("PRAGMA busy_timeout=30000")
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
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


class TopicMetadataCache:
    """SQLite-backed cache for dialog-scoped forum topic metadata with TTL support."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialize the topic_metadata table and supporting index."""
        self._conn = conn
        self._init_table()

    def _init_table(self) -> None:
        """Create topic_metadata table and dialog lookup index if they don't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS topic_metadata (
                dialog_id      INTEGER NOT NULL,
                topic_id       INTEGER NOT NULL,
                title          TEXT NOT NULL,
                top_message_id INTEGER,
                is_general     INTEGER NOT NULL,
                is_deleted     INTEGER NOT NULL,
                inaccessible_error TEXT,
                inaccessible_at INTEGER,
                updated_at     INTEGER NOT NULL,
                PRIMARY KEY (dialog_id, topic_id)
            )
        """)
        self._ensure_columns(
            required_columns={
                "inaccessible_error": "TEXT",
                "inaccessible_at": "INTEGER",
            }
        )
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_topic_metadata_dialog_updated
            ON topic_metadata(dialog_id, updated_at)
        """)
        self._conn.commit()

    def _ensure_columns(self, required_columns: dict[str, str]) -> None:
        """Add missing topic_metadata columns for forward-compatible cache upgrades."""
        rows = self._conn.execute("PRAGMA table_info(topic_metadata)").fetchall()
        existing_columns = {str(row[1]) for row in rows}
        for column_name, column_type in required_columns.items():
            if column_name in existing_columns:
                continue
            self._conn.execute(
                f"ALTER TABLE topic_metadata ADD COLUMN {column_name} {column_type}"
            )
        self._conn.commit()

    def get_dialog_topics(
        self,
        dialog_id: int,
        ttl_seconds: int,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, int | str | bool | None]] | None:
        """Return fresh topic metadata for one dialog or None on cache miss."""
        now = int(time.time())
        rows = self._conn.execute(
            """SELECT topic_id, title, top_message_id, is_general, is_deleted,
                      inaccessible_error, inaccessible_at
               FROM topic_metadata
               WHERE dialog_id = ? AND updated_at >= ?
               ORDER BY topic_id ASC""",
            (dialog_id, now - ttl_seconds),
        ).fetchall()
        if not rows:
            return None

        topics = [self._row_to_topic(row) for row in rows]
        if include_deleted:
            return topics

        active_topics = [topic for topic in topics if not topic["is_deleted"]]
        return active_topics

    def get_topic(
        self,
        dialog_id: int,
        topic_id: int,
        ttl_seconds: int,
        *,
        allow_stale: bool = False,
    ) -> dict[str, int | str | bool | None] | None:
        """Return one fresh topic record or None on cache miss/expiry."""
        row = self._conn.execute(
            """SELECT topic_id, title, top_message_id, is_general, is_deleted,
                      inaccessible_error, inaccessible_at, updated_at
               FROM topic_metadata
               WHERE dialog_id = ? AND topic_id = ?""",
            (dialog_id, topic_id),
        ).fetchone()
        if row is None:
            return None

        updated_at = row[7]
        if not allow_stale and int(time.time()) - updated_at > ttl_seconds:
            return None

        return self._row_to_topic(row[:7])

    def upsert_topics(
        self,
        dialog_id: int,
        topics: list[dict[str, int | str | bool | None]],
    ) -> None:
        """Insert or replace topic metadata rows for a single dialog."""
        now = int(time.time())
        self._conn.executemany(
            """INSERT OR REPLACE INTO topic_metadata
               (dialog_id, topic_id, title, top_message_id, is_general, is_deleted,
                inaccessible_error, inaccessible_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    dialog_id,
                    int(topic["topic_id"]),
                    str(topic["title"]),
                    topic["top_message_id"],
                    int(bool(topic["is_general"])),
                    int(bool(topic["is_deleted"])),
                    topic.get("inaccessible_error"),
                    topic.get("inaccessible_at"),
                    now,
                )
                for topic in topics
            ],
        )
        self._conn.commit()

    def mark_topic_inaccessible(
        self,
        dialog_id: int,
        topic_id: int,
        error: str,
    ) -> None:
        """Persist one topic-level access failure without changing its title or anchor."""
        now = int(time.time())
        self._conn.execute(
            """UPDATE topic_metadata
               SET inaccessible_error = ?, inaccessible_at = ?, updated_at = ?
               WHERE dialog_id = ? AND topic_id = ?""",
            (error, now, now, dialog_id, topic_id),
        )
        self._conn.commit()

    def clear_topic_inaccessible(
        self,
        dialog_id: int,
        topic_id: int,
    ) -> None:
        """Clear prior access-failure state after a topic becomes readable again."""
        now = int(time.time())
        self._conn.execute(
            """UPDATE topic_metadata
               SET inaccessible_error = NULL, inaccessible_at = NULL, updated_at = ?
               WHERE dialog_id = ? AND topic_id = ?""",
            (now, dialog_id, topic_id),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_topic(
        row: tuple[int, str, int | None, int, int, str | None, int | None],
    ) -> dict[str, int | str | bool | None]:
        """Convert one SQLite row into the canonical topic metadata shape."""
        topic_id, title, top_message_id, is_general, is_deleted, inaccessible_error, inaccessible_at = row
        topic = {
            "topic_id": topic_id,
            "title": title,
            "top_message_id": top_message_id,
            "is_general": bool(is_general),
            "is_deleted": bool(is_deleted),
        }
        if inaccessible_error is not None:
            topic["inaccessible_error"] = inaccessible_error
        if inaccessible_at is not None:
            topic["inaccessible_at"] = inaccessible_at
        return topic
