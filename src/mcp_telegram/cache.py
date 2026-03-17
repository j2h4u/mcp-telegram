from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from pathlib import Path

USER_TTL: int = 2_592_000   # 30 days
GROUP_TTL: int = 604_800    # 7 days

_ENTITY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    username   TEXT,
    updated_at INTEGER NOT NULL
);
"""

_ENTITY_UPDATED_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_entities_type_updated
ON entities(type, updated_at)
"""

_ENTITY_USERNAME_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_entities_username
ON entities(username)
"""

_REACTION_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS reaction_metadata (
    message_id INTEGER NOT NULL,
    dialog_id INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    reactor_names TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, dialog_id, emoji)
)
"""

_REACTION_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_reactions_dialog_message
ON reaction_metadata(dialog_id, message_id)
"""

_TOPIC_TABLE_DDL = """
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
"""

_TOPIC_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_topic_metadata_dialog_updated
ON topic_metadata(dialog_id, updated_at)
"""

_ENTITY_REQUIRED_COLUMNS = {
    "name_normalized": "TEXT",
}

_TOPIC_REQUIRED_COLUMNS = {
    "inaccessible_error": "TEXT",
    "inaccessible_at": "INTEGER",
}


def _open_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with the shared busy-timeout policy."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()
    return row is not None


def _entity_columns_ready(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "entities"):
        return False
    rows = conn.execute("PRAGMA table_info(entities)").fetchall()
    existing_columns = {str(row[1]) for row in rows}
    return set(_ENTITY_REQUIRED_COLUMNS).issubset(existing_columns)


def _apply_entity_column_upgrades(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(entities)").fetchall()
    existing_columns = {str(row[1]) for row in rows}
    for column_name, column_type in _ENTITY_REQUIRED_COLUMNS.items():
        if column_name in existing_columns:
            continue
        assert (
            column_name in _ENTITY_REQUIRED_COLUMNS
            and _ENTITY_REQUIRED_COLUMNS[column_name] == column_type
        ), f"Unexpected column: {column_name} {column_type}"
        conn.execute(
            f"ALTER TABLE entities ADD COLUMN {column_name} {column_type}"
        )


def _topic_columns_ready(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "topic_metadata"):
        return False

    rows = conn.execute("PRAGMA table_info(topic_metadata)").fetchall()
    existing_columns = {str(row[1]) for row in rows}
    return set(_TOPIC_REQUIRED_COLUMNS).issubset(existing_columns)


def _database_bootstrap_required(conn: sqlite3.Connection) -> bool:
    journal_mode_row = conn.execute("PRAGMA journal_mode").fetchone()
    if journal_mode_row is None or str(journal_mode_row[0]).lower() != "wal":
        return True

    if not _table_exists(conn, "entities"):
        return True
    if not _index_exists(conn, "idx_entities_type_updated"):
        return True
    if not _index_exists(conn, "idx_entities_username"):
        return True
    if not _entity_columns_ready(conn):
        return True
    if not _table_exists(conn, "reaction_metadata"):
        return True
    if not _index_exists(conn, "idx_reactions_dialog_message"):
        return True
    if not _topic_columns_ready(conn):
        return True
    if not _index_exists(conn, "idx_topic_metadata_dialog_updated"):
        return True
    return False


def _apply_topic_column_upgrades(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(topic_metadata)").fetchall()
    existing_columns = {str(row[1]) for row in rows}
    for column_name, column_type in _TOPIC_REQUIRED_COLUMNS.items():
        if column_name in existing_columns:
            continue
        assert (
            column_name in _TOPIC_REQUIRED_COLUMNS
            and _TOPIC_REQUIRED_COLUMNS[column_name] == column_type
        ), f"Unexpected column: {column_name} {column_type}"
        conn.execute(
            f"ALTER TABLE topic_metadata ADD COLUMN {column_name} {column_type}"
        )


def _bootstrap_cache_schema(conn: sqlite3.Connection) -> None:
    """Apply one-time schema and journal-mode setup on a dedicated connection."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise

    conn.execute(_ENTITY_TABLE_DDL)
    _apply_entity_column_upgrades(conn)
    conn.execute(_ENTITY_UPDATED_INDEX_DDL)
    conn.execute(_ENTITY_USERNAME_INDEX_DDL)
    conn.execute(_REACTION_TABLE_DDL)
    conn.execute(_REACTION_INDEX_DDL)
    conn.execute(_TOPIC_TABLE_DDL)
    _apply_topic_column_upgrades(conn)
    conn.execute(_TOPIC_INDEX_DDL)
    conn.commit()


def _ensure_cache_schema(db_path: Path) -> None:
    """Serialize one-time cache bootstrap so normal opens stay read-safe."""
    probe_conn = _open_connection(db_path)
    try:
        if not _database_bootstrap_required(probe_conn):
            return
    finally:
        probe_conn.close()

    lock_path = db_path.with_suffix(f"{db_path.suffix}.bootstrap.lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        bootstrap_conn = _open_connection(db_path)
        try:
            if _database_bootstrap_required(bootstrap_conn):
                _bootstrap_cache_schema(bootstrap_conn)
        finally:
            bootstrap_conn.close()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _get_connection_db_path(conn: sqlite3.Connection) -> Path | None:
    rows = conn.execute("PRAGMA database_list").fetchall()
    for row in rows:
        if len(row) < 3:
            continue
        database_name = str(row[1])
        if database_name != "main":
            continue
        filename = str(row[2])
        if not filename:
            return None
        return Path(filename)
    return None


def _ensure_connection_schema(conn: sqlite3.Connection) -> None:
    """Ensure supporting cache tables exist for a shared or standalone connection."""
    if not _database_bootstrap_required(conn):
        return

    db_path = _get_connection_db_path(conn)
    if db_path is None:
        _bootstrap_cache_schema(conn)
        return

    _ensure_cache_schema(db_path)


class EntityCache:
    """SQLite-backed cache for Telegram entity metadata with TTL support."""

    def __init__(self, db_path: Path) -> None:
        """Open (or create) the SQLite database at db_path after shared bootstrap checks."""
        _ensure_cache_schema(db_path)
        self._conn = _open_connection(db_path)

    def upsert(
        self,
        entity_id: int,
        entity_type: str,
        name: str,
        username: str | None,
    ) -> None:
        """Insert or replace entity metadata, updating updated_at to now."""
        from .resolver import latinize

        self._conn.execute(
            "INSERT OR REPLACE INTO entities (id, type, name, username, name_normalized, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (entity_id, entity_type, name, username, latinize(name), int(time.time())),
        )
        self._conn.commit()

    def upsert_batch(
        self,
        entities: list[tuple[int, str, str, str | None]],
    ) -> None:
        """Batch insert or replace entity metadata rows in a single transaction."""
        from .resolver import latinize

        now = int(time.time())
        self._conn.executemany(
            "INSERT OR REPLACE INTO entities (id, type, name, username, name_normalized, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(eid, etype, name, username, latinize(name), now) for eid, etype, name, username in entities],
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

    def all_names_normalized_with_ttl(self, user_ttl: int, group_ttl: int) -> dict[int, str]:
        """Return {entity_id: name_normalized} filtered by type-specific TTL."""
        now = int(time.time())
        rows = self._conn.execute(
            """SELECT id, name_normalized FROM entities
               WHERE name_normalized IS NOT NULL
                 AND ((type = 'user' AND updated_at >= ?)
                   OR (type != 'user' AND updated_at >= ?))""",
            (now - user_ttl, now - group_ttl),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def get_name(self, entity_id: int) -> str | None:
        """Return cached display name, trying group then user TTL."""
        entity = self.get(entity_id, GROUP_TTL)
        if entity is None:
            entity = self.get(entity_id, USER_TTL)
        if entity is None:
            return None
        name = entity.get("name")
        return name if isinstance(name, str) and name else None

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
        """Ensure reaction cache schema exists without rerunning hot-path bootstrap work."""
        _ensure_connection_schema(self._conn)

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
        """Ensure topic cache schema exists without rerunning hot-path bootstrap work."""
        _ensure_connection_schema(self._conn)

    def _ensure_columns(self, required_columns: dict[str, str]) -> None:
        """Add missing topic_metadata columns for forward-compatible cache upgrades."""
        rows = self._conn.execute("PRAGMA table_info(topic_metadata)").fetchall()
        existing_columns = {str(row[1]) for row in rows}
        for column_name, column_type in required_columns.items():
            if column_name in existing_columns:
                continue
            assert (
                column_name in _TOPIC_REQUIRED_COLUMNS
                and _TOPIC_REQUIRED_COLUMNS[column_name] == column_type
            ), f"Unexpected column: {column_name} {column_type}"
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
