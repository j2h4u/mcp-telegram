
import asyncio
import fcntl
import logging
import signal
import sqlite3
from pathlib import Path

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

_CURRENT_SCHEMA_VERSION = 8

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL constants
# ---------------------------------------------------------------------------

_SYNCED_DIALOGS_DDL = """
CREATE TABLE IF NOT EXISTS synced_dialogs (
    dialog_id       INTEGER PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'not_synced',
    last_synced_at  INTEGER,
    last_event_at   INTEGER,
    sync_progress   INTEGER DEFAULT 0,
    total_messages  INTEGER
)
"""

_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    dialog_id           INTEGER NOT NULL,
    message_id          INTEGER NOT NULL,
    sent_at             INTEGER NOT NULL,
    text                TEXT,
    sender_id           INTEGER,
    sender_first_name   TEXT,
    media_description   TEXT,
    reply_to_msg_id     INTEGER,
    forum_topic_id      INTEGER,
    reactions           TEXT,
    is_deleted          INTEGER NOT NULL DEFAULT 0,
    deleted_at          INTEGER,
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID
"""

_MESSAGES_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_dialog_sent
ON messages(dialog_id, sent_at DESC)
"""

_MESSAGE_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS message_versions (
    dialog_id   INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    old_text    TEXT,
    edit_date   INTEGER,
    PRIMARY KEY (dialog_id, message_id, version)
) WITHOUT ROWID
"""

# ---------------------------------------------------------------------------
# DDL for v4: entity cache tables
# ---------------------------------------------------------------------------

_ENTITY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS entities (
    id              INTEGER PRIMARY KEY,
    type            TEXT NOT NULL,
    name            TEXT,
    username        TEXT,
    name_normalized TEXT,
    updated_at      INTEGER NOT NULL
)
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

# ---------------------------------------------------------------------------
# DDL for v5: telemetry_events table
# ---------------------------------------------------------------------------

_TELEMETRY_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    timestamp REAL NOT NULL,
    duration_ms REAL NOT NULL,
    result_count INTEGER NOT NULL,
    has_cursor BOOLEAN NOT NULL,
    page_depth INTEGER NOT NULL,
    has_filter BOOLEAN NOT NULL,
    error_type TEXT
)
"""

_TELEMETRY_EVENTS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_telemetry_tool_timestamp
ON telemetry_events(tool_name, timestamp)
"""


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def get_sync_db_path() -> Path:
    """Return the canonical path for sync.db under XDG state home."""
    db_dir = xdg_state_home() / "mcp-telegram"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "sync.db"


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def _open_sync_db(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection to sync.db with busy_timeout=10s policy."""
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    else:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def open_sync_db_reader(db_path: Path) -> sqlite3.Connection:
    """Open sync.db read-only for MCP server process.

    Returns a connection that can SELECT but raises OperationalError on any write.
    Caller is responsible for closing the connection.
    """
    return _open_sync_db(db_path, read_only=True)


# ---------------------------------------------------------------------------
# Schema migration helpers
# ---------------------------------------------------------------------------


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """Return True if sync.db schema is at current version and WAL mode is active."""
    row = conn.execute("PRAGMA journal_mode").fetchone()
    if row is None or str(row[0]).lower() != "wal":
        return False
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return (
            row is not None
            and row[0] is not None
            and int(row[0]) >= _CURRENT_SCHEMA_VERSION
        )
    except sqlite3.OperationalError:
        return False


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply WAL mode and all pending schema migrations in version order."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        logger.debug(
            "sync_db WAL pragma skipped (DB locked), will retry next open"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER NOT NULL,
            applied_at INTEGER NOT NULL
        )
        """
    )

    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row is not None and row[0] is not None else 0

    def _migrate(version: int, stmts: list[str]) -> None:
        """Apply one migration version atomically and record it."""
        nonlocal current
        if current >= version:
            return
        try:
            for stmt in stmts:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version VALUES (?, strftime('%s', 'now'))",
                (version,),
            )
            conn.commit()
            current = version
        except Exception:
            conn.rollback()
            logger.error("sync_db migration to version %d failed", version, exc_info=True)
            raise

    _migrate(1, [_SYNCED_DIALOGS_DDL, _MESSAGES_DDL, _MESSAGES_INDEX_DDL, _MESSAGE_VERSIONS_DDL])

    _migrate(2, ["ALTER TABLE synced_dialogs ADD COLUMN access_lost_at INTEGER"])

    if current < 3:
        from .fts import MESSAGES_FTS_DDL
        _migrate(3, [MESSAGES_FTS_DDL])

    _migrate(4, [
        _ENTITY_TABLE_DDL, _ENTITY_UPDATED_INDEX_DDL, _ENTITY_USERNAME_INDEX_DDL,
        _REACTION_TABLE_DDL, _REACTION_INDEX_DDL,
        _TOPIC_TABLE_DDL, _TOPIC_INDEX_DDL,
        _MESSAGE_CACHE_TABLE_DDL, _MESSAGE_CACHE_INDEX_DDL,
    ])

    _migrate(5, [_TELEMETRY_EVENTS_DDL, _TELEMETRY_EVENTS_INDEX_DDL])

    _migrate(6, [
        # SQLite cannot ALTER COLUMN to drop NOT NULL — recreate with nullable name.
        """CREATE TABLE entities_new (
            id              INTEGER PRIMARY KEY,
            type            TEXT NOT NULL,
            name            TEXT,
            username        TEXT,
            name_normalized TEXT,
            updated_at      INTEGER NOT NULL
        )""",
        "INSERT INTO entities_new SELECT id, type, name, username, name_normalized, updated_at FROM entities",
        "DROP TABLE entities",
        "ALTER TABLE entities_new RENAME TO entities",
        "CREATE INDEX IF NOT EXISTS idx_entities_type_updated ON entities(type, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_entities_username ON entities(username)",
        # Backfill tombstone rows for enrolled dialogs that have no entity row yet.
        (
            "INSERT OR IGNORE INTO entities (id, type, updated_at) "
            "SELECT dialog_id, 'user', strftime('%s', 'now') "
            "FROM synced_dialogs "
            "WHERE dialog_id NOT IN (SELECT id FROM entities)"
        ),
    ])

    _migrate(7, [
        # 1. Drop dead tables
        "DROP TABLE IF EXISTS reaction_metadata",
        "DROP TABLE IF EXISTS message_cache",
        # 2. Add new columns to messages
        "ALTER TABLE messages ADD COLUMN edit_date INTEGER",
        "ALTER TABLE messages ADD COLUMN grouped_id INTEGER",
        "ALTER TABLE messages ADD COLUMN reply_to_peer_id INTEGER",
        # 3. Create message_reactions (WITHOUT ROWID -- composite PK)
        """CREATE TABLE IF NOT EXISTS message_reactions (
    dialog_id   INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    emoji       TEXT NOT NULL,
    count       INTEGER NOT NULL,
    PRIMARY KEY (dialog_id, message_id, emoji)
) WITHOUT ROWID""",
        # 4. Backfill reactions from JSON blob (runs before DROP COLUMN)
        # json_valid() + json_type() guards: skip corrupted/malformed JSON and
        # non-object shapes (arrays, scalars) that would produce bad rows.
        (
            "INSERT OR IGNORE INTO message_reactions "
            "SELECT dialog_id, message_id, j.key, CAST(j.value AS INTEGER) "
            "FROM messages, json_each(reactions) j "
            "WHERE reactions IS NOT NULL AND json_valid(reactions) "
            "AND json_type(reactions) = 'object'"
        ),
        # 5. Drop reactions column (SQLite 3.35+, confirmed 3.46.1)
        "ALTER TABLE messages DROP COLUMN reactions",
        # 6. Create message_entities
        # 5-column PK (dialog_id, message_id, offset, length, type) prevents
        # silent data loss when two entity types share the same byte offset.
        """CREATE TABLE IF NOT EXISTS message_entities (
    dialog_id   INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    offset      INTEGER NOT NULL,
    length      INTEGER NOT NULL,
    type        TEXT NOT NULL,
    value       TEXT,
    PRIMARY KEY (dialog_id, message_id, offset, length, type)
) WITHOUT ROWID""",
        # 7. Create message_forwards
        """CREATE TABLE IF NOT EXISTS message_forwards (
    dialog_id        INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    fwd_from_peer_id INTEGER,
    fwd_from_name    TEXT,
    fwd_date         INTEGER,
    fwd_channel_post INTEGER,
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID""",
        # 8. Reply-chain index
        "CREATE INDEX IF NOT EXISTS idx_messages_reply ON messages(dialog_id, reply_to_msg_id)",
    ])

    _migrate(8, [
        "ALTER TABLE synced_dialogs ADD COLUMN read_inbox_max_id INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_synced_dialogs_status_read_position "
        "ON synced_dialogs(status, read_inbox_max_id)",
    ])

    logger.info("sync_db migrations applied through version %d", _CURRENT_SCHEMA_VERSION)


def ensure_sync_schema(db_path: Path) -> None:
    """Ensure sync.db exists and has the current schema.

    Probes the DB first, then acquires fcntl lock before applying migrations
    to prevent parallel-process races.
    """
    probe_conn = _open_sync_db(db_path)
    try:
        if _schema_ready(probe_conn):
            return
    finally:
        probe_conn.close()

    lock_path = db_path.with_suffix(f"{db_path.suffix}.bootstrap.lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        bootstrap_conn = None
        try:
            bootstrap_conn = _open_sync_db(db_path)
            if not _schema_ready(bootstrap_conn):
                _apply_migrations(bootstrap_conn)
        finally:
            if bootstrap_conn is not None:
                bootstrap_conn.close()


# ---------------------------------------------------------------------------
# Legacy DB migration
# ---------------------------------------------------------------------------


def _migrate_from_legacy_db(
    conn: sqlite3.Connection,
    legacy_path: Path,
    copy_stmts: list[str],
) -> int:
    """Attach legacy DB, run copy_stmts, detach. Returns rows copied. No-op if path missing."""
    if not legacy_path.exists():
        return 0
    conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy_path),))
    rows_copied = 0
    try:
        for stmt in copy_stmts:
            cursor = conn.execute(stmt)
            rows_copied += cursor.rowcount
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE legacy")
    return rows_copied


def migrate_legacy_databases(conn: sqlite3.Connection, state_dir: Path) -> None:
    """One-shot migration from entity_cache.db and analytics.db into sync.db.

    Called once at daemon startup after ensure_sync_schema(). Idempotent —
    INSERT OR IGNORE skips existing rows. Deletes legacy files after success.
    """
    entity_cache_path = state_dir / "entity_cache.db"
    entity_lock_path = state_dir / "entity_cache.db.bootstrap.lock"
    analytics_path = state_dir / "analytics.db"

    # Migrate entities (only entities table — reaction_metadata, topic_metadata,
    # message_cache are cache-layer data with TTL, not worth migrating)
    entity_stmts = [
        "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) "
        "SELECT id, type, name, username, updated_at FROM legacy.entities",
    ]
    copied_entities = _migrate_from_legacy_db(conn, entity_cache_path, entity_stmts)
    if copied_entities:
        logger.info("migrated %d entities from entity_cache.db", copied_entities)

    # Migrate telemetry events (30-day retention filter)
    telemetry_stmts = [
        "INSERT OR IGNORE INTO telemetry_events "
        "(tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type) "
        "SELECT tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type "
        "FROM legacy.telemetry_events "
        "WHERE timestamp >= strftime('%s', 'now') - 2592000",
    ]
    copied_telemetry = _migrate_from_legacy_db(conn, analytics_path, telemetry_stmts)
    if copied_telemetry:
        logger.info("migrated %d telemetry events from analytics.db", copied_telemetry)

    # Delete legacy files
    for path in [entity_cache_path, entity_lock_path, analytics_path]:
        if path.exists():
            path.unlink()
            logger.info("deleted legacy file: %s", path)


# ---------------------------------------------------------------------------
# Graceful shutdown handler
# ---------------------------------------------------------------------------


def register_shutdown_handler(
    conn: sqlite3.Connection,
    loop: asyncio.AbstractEventLoop,
) -> asyncio.Event:
    """Register a SIGTERM handler that checkpoints sync.db before exit.

    Returns an asyncio.Event that will be set when SIGTERM is received.
    The caller (sync daemon) should await this event to know when to stop.
    """
    shutdown_event = asyncio.Event()

    def _on_sigterm() -> None:
        logger.info("SIGTERM received — checkpointing sync.db")
        try:
            conn.rollback()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logger.exception("sync.db shutdown error")
        finally:
            shutdown_event.set()  # signal AFTER checkpoint so handlers don't race

    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    return shutdown_event
