from __future__ import annotations

import asyncio
import fcntl
import logging
import signal
import sqlite3
from pathlib import Path

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

_CURRENT_SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL constants (mirror cache.py uppercase DDL pattern)
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

    if current < 1:
        conn.execute(_SYNCED_DIALOGS_DDL)
        conn.execute(_MESSAGES_DDL)
        conn.execute(_MESSAGES_INDEX_DDL)
        conn.execute(_MESSAGE_VERSIONS_DDL)
        conn.execute(
            "INSERT INTO schema_version VALUES (1, strftime('%s', 'now'))"
        )

    if current < 2:
        conn.execute(
            "ALTER TABLE synced_dialogs ADD COLUMN access_lost_at INTEGER"
        )
        conn.execute(
            "INSERT INTO schema_version VALUES (2, strftime('%s', 'now'))"
        )

    conn.commit()
    logger.info("sync_db migrations applied through version %d", _CURRENT_SCHEMA_VERSION)


def ensure_sync_schema(db_path: Path) -> None:
    """Ensure sync.db exists and has the current schema.

    Mirrors _ensure_cache_schema from cache.py — probe first, then acquire
    fcntl lock before applying migrations to prevent parallel-process races.
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
            conn.close()
        except Exception:
            logger.exception("sync.db shutdown error")
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    return shutdown_event
