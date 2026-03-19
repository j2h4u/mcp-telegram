from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .models import TopicMetadata
    from .pagination import HistoryDirection

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

_MESSAGE_CACHE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS message_cache (
    dialog_id          INTEGER NOT NULL,
    message_id         INTEGER NOT NULL,
    sent_at            INTEGER NOT NULL,
    text               TEXT,
    sender_id          INTEGER,
    sender_first_name  TEXT,
    media_description  TEXT,
    reply_to_msg_id    INTEGER,
    forum_topic_id     INTEGER,
    edit_date          INTEGER,
    fetched_at         INTEGER NOT NULL,
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID
"""

_MESSAGE_CACHE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_message_cache_dialog_sent
ON message_cache(dialog_id, sent_at DESC)
"""

_MESSAGE_VERSIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS message_versions (
    dialog_id   INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    old_text    TEXT,
    edit_date   INTEGER,
    PRIMARY KEY (dialog_id, message_id, version)
) WITHOUT ROWID
"""

_ENTITY_REQUIRED_COLUMNS = {
    "name_normalized": "TEXT",
}

_TOPIC_REQUIRED_COLUMNS = {
    "inaccessible_error": "TEXT",
    "inaccessible_at": "INTEGER",
}

_ALLOWED_TABLE_NAMES = frozenset({"entities", "topic_metadata", "message_cache", "message_versions"})
_ALLOWED_DDL_TYPES = frozenset({"TEXT", "INTEGER", "REAL", "BLOB"})


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


def _apply_column_upgrades(
    conn: sqlite3.Connection,
    table_name: str,
    required_columns: dict[str, str],
) -> None:
    """Add missing columns to an existing table."""
    if table_name not in _ALLOWED_TABLE_NAMES:
        raise ValueError(f"Table not in allow-list: {table_name}")
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing = {str(row[1]) for row in rows}
    for col_name, col_type in required_columns.items():
        if col_name in existing:
            continue
        if not col_name.isidentifier() or col_type not in _ALLOWED_DDL_TYPES:
            raise ValueError(f"Invalid column spec: {col_name} {col_type}")
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")


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
    if not _table_exists(conn, "message_cache"):
        return True
    if not _index_exists(conn, "idx_message_cache_dialog_sent"):
        return True
    if not _table_exists(conn, "message_versions"):
        return True
    return False


def _apply_topic_column_upgrades(conn: sqlite3.Connection) -> None:
    _apply_column_upgrades(conn, "topic_metadata", _TOPIC_REQUIRED_COLUMNS)


def _bootstrap_cache_schema(conn: sqlite3.Connection) -> None:
    """Apply one-time schema and journal-mode setup on a dedicated connection."""
    import logging
    _logger = logging.getLogger(__name__)

    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        _logger.debug("cache_bootstrap WAL pragma skipped (DB locked), will retry next open")

    conn.execute(_ENTITY_TABLE_DDL)
    _apply_column_upgrades(conn, "entities", _ENTITY_REQUIRED_COLUMNS)
    conn.execute(_ENTITY_UPDATED_INDEX_DDL)
    conn.execute(_ENTITY_USERNAME_INDEX_DDL)
    conn.execute(_REACTION_TABLE_DDL)
    conn.execute(_REACTION_INDEX_DDL)
    conn.execute(_TOPIC_TABLE_DDL)
    _apply_topic_column_upgrades(conn)
    conn.execute(_TOPIC_INDEX_DDL)
    conn.execute(_MESSAGE_CACHE_TABLE_DDL)
    conn.execute(_MESSAGE_CACHE_INDEX_DDL)
    conn.execute(_MESSAGE_VERSIONS_TABLE_DDL)
    conn.execute("PRAGMA optimize")
    conn.commit()
    _logger.info("cache schema bootstrapped: %s", _get_connection_db_path(conn) or "in-memory")


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
        bootstrap_conn = None
        try:
            bootstrap_conn = _open_connection(db_path)
            if _database_bootstrap_required(bootstrap_conn):
                _bootstrap_cache_schema(bootstrap_conn)
        finally:
            if bootstrap_conn is not None:
                bootstrap_conn.close()


def _get_connection_db_path(conn: sqlite3.Connection) -> Path | None:
    rows = conn.execute("PRAGMA database_list").fetchall()
    for row in rows:
        if len(row) < 3:
            continue
        schema_name = str(row[1])
        if schema_name != "main":
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


@dataclass(frozen=True)
class _CachedSender:
    """Stub satisfying SenderLike Protocol for cached messages."""
    first_name: str | None


@dataclass(frozen=True)
class _CachedReplyHeader:
    """Stub satisfying ReplyHeaderLike Protocol for cached messages."""
    reply_to_msg_id: int | None


@dataclass(frozen=True)
class CachedMessage:
    """MessageLike proxy backed by a message_cache row.

    Satisfies formatter.MessageLike Protocol: .id, .date, .message,
    .sender, .reply_to, .reactions, .media all present via structural
    subtyping.

    The edit_date field is stored for Phase 22 (formatter [edited] marker)
    but is not part of MessageLike.
    """

    id: int
    date: datetime
    message: str | None
    sender: _CachedSender | None
    reply_to: _CachedReplyHeader | None
    media: object = None
    reactions: object = None
    edit_date: int | None = None

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> CachedMessage:
        """Construct from a message_cache SELECT * row.

        Row column order (must match _MESSAGE_CACHE_TABLE_DDL):
            0: dialog_id         — not stored (available from query context)
            1: message_id        → .id
            2: sent_at           → .date (UTC datetime from Unix timestamp)
            3: text              → .message (with media_description fallback)
            4: sender_id         — not stored (not needed by formatter)
            5: sender_first_name → .sender (_CachedSender)
            6: media_description → fallback for .message when text is None
            7: reply_to_msg_id   → .reply_to (_CachedReplyHeader)
            8: forum_topic_id    — not stored (available from query context)
            9: edit_date         → .edit_date (for Phase 22)
           10: fetched_at        — not stored (cache bookkeeping only)
        """
        (
            _dialog_id,
            message_id,
            sent_at,
            text,
            _sender_id,
            sender_first_name,
            media_description,
            reply_to_msg_id,
            _forum_topic_id,
            edit_date,
            _fetched_at,
        ) = row
        return cls(
            id=int(cast(int, message_id)),
            date=datetime.fromtimestamp(int(cast(int, sent_at)), tz=timezone.utc),
            message=cast("str | None", text) or cast("str | None", media_description),
            sender=_CachedSender(first_name=cast("str | None", sender_first_name)) if sender_first_name else None,
            reply_to=_CachedReplyHeader(reply_to_msg_id=cast("int | None", reply_to_msg_id)) if reply_to_msg_id else None,
            edit_date=cast("int | None", edit_date),
        )


class MessageCache:
    """Read/write access to the message_cache table for cache-first history reads."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _record_versions_if_changed(
        self,
        dialog_id: int,
        incoming: list[tuple[int, str | None, int | None]],
    ) -> None:
        """Write message_versions rows for messages whose text changed since last cache.

        Args:
            dialog_id: The dialog these messages belong to.
            incoming: List of (message_id, new_text, new_edit_date) tuples.
        """
        if not incoming:
            return

        ids = [row[0] for row in incoming]
        placeholders = ",".join("?" * len(ids))
        existing_rows = self._conn.execute(
            f"SELECT message_id, text, edit_date FROM message_cache "
            f"WHERE dialog_id = ? AND message_id IN ({placeholders})",
            [dialog_id, *ids],
        ).fetchall()
        existing = {int(row[0]): (row[1], row[2]) for row in existing_rows}

        changed_ids: list[int] = []
        for msg_id, new_text, _new_edit_date in incoming:
            cached = existing.get(msg_id)
            if cached is None:
                continue
            old_text, _old_edit_date = cached
            if old_text == new_text:
                continue
            changed_ids.append(msg_id)

        if not changed_ids:
            return

        # Batch fetch max version numbers for all changed messages
        changed_placeholders = ",".join("?" * len(changed_ids))
        max_version_rows = self._conn.execute(
            f"SELECT message_id, MAX(version) FROM message_versions "
            f"WHERE dialog_id = ? AND message_id IN ({changed_placeholders}) "
            f"GROUP BY message_id",
            [dialog_id, *changed_ids],
        ).fetchall()
        max_versions: dict[int, int] = {int(row[0]): int(row[1]) for row in max_version_rows}

        version_rows: list[tuple[object, ...]] = []
        for msg_id, _new_text, _new_edit_date in incoming:
            if msg_id not in changed_ids:
                continue
            cached = existing[msg_id]
            old_text, old_edit_date = cached
            next_version = max_versions.get(msg_id, 0) + 1
            version_rows.append((dialog_id, msg_id, next_version, old_text, old_edit_date))

        if version_rows:
            self._conn.executemany(
                "INSERT INTO message_versions "
                "(dialog_id, message_id, version, old_text, edit_date) "
                "VALUES (?, ?, ?, ?, ?)",
                version_rows,
            )

    def store_messages(self, dialog_id: int, messages: Iterable[object]) -> None:
        """INSERT OR REPLACE messages into message_cache.

        Extracts all 11 structured fields from Telethon message objects.
        Safe to call with CachedMessage objects too (fields gracefully degrade to None).
        """
        now = int(time.time())
        rows: list[tuple[object, ...]] = []
        for msg in messages:
            message_id = int(getattr(msg, "id", 0))
            date = getattr(msg, "date", None)
            sent_at = int(date.timestamp()) if isinstance(date, datetime) else 0

            text = getattr(msg, "message", None)

            sender_id = getattr(msg, "sender_id", None)
            sender = getattr(msg, "sender", None)
            sender_first_name = getattr(sender, "first_name", None) if sender is not None else None

            media = getattr(msg, "media", None)
            if media is None:
                media_description: str | None = None
            elif hasattr(media, "to_dict"):
                media_description = type(media).__name__
            else:
                media_description = type(media).__name__

            reply_to = getattr(msg, "reply_to", None)
            reply_to_msg_id: int | None = None
            forum_topic_id: int | None = None
            if reply_to is not None:
                raw_rtmi = getattr(reply_to, "reply_to_msg_id", None)
                reply_to_msg_id = int(raw_rtmi) if raw_rtmi is not None else None
                if getattr(reply_to, "forum_topic", False):
                    top_id = getattr(reply_to, "reply_to_top_id", None)
                    forum_topic_id = int(top_id) if top_id is not None else 1

            ed = getattr(msg, "edit_date", None)
            if ed is None:
                edit_date: int | None = None
            elif isinstance(ed, datetime):
                edit_date = int(ed.timestamp())
            else:
                edit_date = int(ed)

            rows.append((
                dialog_id,
                message_id,
                sent_at,
                text,
                sender_id,
                sender_first_name,
                media_description,
                reply_to_msg_id,
                forum_topic_id,
                edit_date,
                now,
            ))

        # Record version history for messages whose text changed (EDIT-02)
        incoming_for_version: list[tuple[int, str | None, int | None]] = [
            (int(cast(int, row[1])), cast("str | None", row[3]), cast("int | None", row[9]))
            for row in rows
        ]
        self._record_versions_if_changed(dialog_id, incoming_for_version)

        self._conn.executemany(
            "INSERT OR REPLACE INTO message_cache "
            "(dialog_id, message_id, sent_at, text, sender_id, sender_first_name, "
            "media_description, reply_to_msg_id, forum_topic_id, edit_date, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def try_read_page(
        self,
        dialog_id: int,
        *,
        topic_id: int | None,
        anchor_id: int | None,
        limit: int,
        direction: HistoryDirection,
    ) -> list[CachedMessage] | None:
        """Return a cached page or None on miss (partial coverage).

        Returns None when fewer than `limit` rows match — caller must fetch live.
        """
        from .pagination import HistoryDirection as _HD

        params: list[object] = [dialog_id]
        topic_clause = "forum_topic_id IS NULL" if topic_id is None else "forum_topic_id = ?"
        if topic_id is not None:
            params.append(topic_id)

        if direction == _HD.OLDEST:
            anchor_clause = ""
            if anchor_id is not None:
                anchor_clause = "AND message_id > ?"
                params.append(anchor_id)
            order = "ASC"
        else:
            anchor_clause = ""
            if anchor_id is not None:
                anchor_clause = "AND message_id < ?"
                params.append(anchor_id)
            order = "DESC"

        params.append(limit)

        sql = (
            "SELECT dialog_id, message_id, sent_at, text, sender_id, sender_first_name, "
            "media_description, reply_to_msg_id, forum_topic_id, edit_date, fetched_at "
            f"FROM message_cache "
            f"WHERE dialog_id = ? AND ({topic_clause}) "
            f"{anchor_clause} "
            f"ORDER BY message_id {order} "
            f"LIMIT ?"
        )

        rows = self._conn.execute(sql, params).fetchall()
        if len(rows) < limit:
            return None
        return [CachedMessage.from_row(row) for row in rows]


def _should_try_cache(navigation: str | None, *, unread: bool) -> bool:
    """Decide whether to attempt a cache read before hitting the Telegram API.

    Returns False (always live) for:
    - navigation=None or "newest"  (BYP-01: first page must be fresh)
    - unread=True                  (BYP-02: read state changes in real time)

    Returns True (try cache) for:
    - navigation="oldest"          (historical data, immutable)
    - navigation=<base64 token>    (page 2+, cacheable continuation)
    """
    if unread:
        return False
    if navigation is None or navigation == "newest":
        return False
    return True


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
        """Batch insert or replace entity metadata rows in a single transaction.

        Each tuple: (entity_id, entity_type, name, username).
        """
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
        Returns empty dict when cache has no fresh entries (callers may treat this as a miss).
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
        """Return cached display name if within GROUP_TTL (7d) or USER_TTL (30d).

        Single query — returns the row regardless of type, then checks the
        appropriate TTL threshold in Python.
        Returns None if both TTLs expired, entity not found, or name is empty.
        """
        row = self._conn.execute(
            "SELECT type, name, updated_at FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        entity_type, name, updated_at = row
        age = int(time.time()) - updated_at
        ttl = GROUP_TTL if entity_type != "user" else USER_TTL
        if age > ttl:
            return None
        return name if isinstance(name, str) and name else None

    def get_by_username(self, username: str) -> tuple[int, str] | None:
        """Return (entity_id, name) for entity with matching username, or None."""
        row = self._conn.execute(
            "SELECT id, name FROM entities WHERE username = ?",
            (username,)
        ).fetchone()
        return row if row else None

    @property
    def connection(self) -> sqlite3.Connection:
        """Shared SQLite connection for sibling caches (ReactionMetadataCache, TopicMetadataCache)."""
        return self._conn

    def close(self) -> None:
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
        _apply_column_upgrades(self._conn, "topic_metadata", required_columns)
        self._conn.commit()

    def get_dialog_topics(
        self,
        dialog_id: int,
        ttl_seconds: int,
        *,
        include_deleted: bool = False,
    ) -> list[TopicMetadata] | None:
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
    ) -> TopicMetadata | None:
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
        topics: list[TopicMetadata],
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
    ) -> TopicMetadata:
        """Convert one SQLite row into the canonical topic metadata shape."""
        topic_id, title, top_message_id, is_general, is_deleted, inaccessible_error, inaccessible_at = row
        topic: dict[str, object] = {
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
        return cast("TopicMetadata", topic)
