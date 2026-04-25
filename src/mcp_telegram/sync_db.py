import asyncio
import fcntl
import logging
import signal
import sqlite3
from pathlib import Path

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

_CURRENT_SCHEMA_VERSION = 16

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
# DDL for v14: activity_comments and activity_sync_state tables (Phase 999.1)
# ---------------------------------------------------------------------------

# synced_dialogs.status accepted values:
#   'not_synced'  — default; no bulk fetch has been attempted
#   'own_only'    — only outgoing messages (out=1) via activity_sync_loop (Phase 999.1.1)
#   'fragment'    — no full sync; point-fetched snippets only (Phase 999.1)
#   'syncing'     — FullSyncWorker in progress
#   'synced'      — bulk fetch complete, real-time events active
#   'access_lost' — account was removed; read-only metadata

_ACTIVITY_SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS activity_sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
)
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
    # Enable FK enforcement on every connection. SQLite defaults foreign_keys
    # to OFF per connection; without this the entity_details ON DELETE CASCADE
    # added in v16 silently does nothing in production.
    conn.execute("PRAGMA foreign_keys = ON")
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
        return row is not None and row[0] is not None and int(row[0]) >= _CURRENT_SCHEMA_VERSION
    except sqlite3.OperationalError:
        return False


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply WAL mode and all pending schema migrations in version order."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        logger.debug("sync_db WAL pragma skipped (DB locked), will retry next open")

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

    _migrate(
        4,
        [
            _ENTITY_TABLE_DDL,
            _ENTITY_UPDATED_INDEX_DDL,
            _ENTITY_USERNAME_INDEX_DDL,
            _REACTION_TABLE_DDL,
            _REACTION_INDEX_DDL,
            _TOPIC_TABLE_DDL,
            _TOPIC_INDEX_DDL,
            (
                "CREATE TABLE IF NOT EXISTS message_cache ("
                "dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sent_at INTEGER NOT NULL, "
                "text TEXT, sender_id INTEGER, sender_first_name TEXT, media_description TEXT, "
                "reply_to_msg_id INTEGER, forum_topic_id INTEGER, edit_date INTEGER, "
                "fetched_at INTEGER NOT NULL, PRIMARY KEY (dialog_id, message_id)) WITHOUT ROWID"
            ),
            "CREATE INDEX IF NOT EXISTS idx_message_cache_dialog_sent ON message_cache(dialog_id, sent_at DESC)",
        ],
    )

    _migrate(5, [_TELEMETRY_EVENTS_DDL, _TELEMETRY_EVENTS_INDEX_DDL])

    _migrate(
        6,
        [
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
        ],
    )

    _migrate(
        7,
        [
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
        ],
    )

    _migrate(
        8,
        [
            "ALTER TABLE synced_dialogs ADD COLUMN read_inbox_max_id INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_synced_dialogs_status_read_position "
            "ON synced_dialogs(status, read_inbox_max_id)",
        ],
    )

    # v9: DM sender discriminators — direction (out) and service-flag (is_service).
    # Phase 39's "sender_id IS NULL → System" rule was over-broad for DMs;
    # these columns let the read path distinguish outgoing DMs (out=1) from
    # true service messages (is_service=1). ADD COLUMN with DEFAULT is O(1)
    # metadata in SQLite — no row rewrite on large messages tables.
    _migrate(
        9,
        [
            "ALTER TABLE messages ADD COLUMN out INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN is_service INTEGER NOT NULL DEFAULT 0",
        ],
    )

    # v10: backfill out=1 for historical outgoing DM rows. Pre-v9 writes had no
    # 'out' column, so v9 DEFAULT 0 left all historical rows at out=0. In DMs
    # (dialog_id > 0), the original bug shape was "outgoing row arrives with
    # sender_id IS NULL". Incoming DM rows always carry sender_id=peer_id, so
    # NULL sender_id in a DM is a reliable marker for outgoing. Idempotent:
    # subsequent runs find no matching rows (already out=1 or already labelled).
    _migrate(
        10,
        [
            "UPDATE messages SET out = 1 WHERE out = 0 AND dialog_id > 0 AND sender_id IS NULL",
        ],
    )

    # v11 per CONTEXT.md §Scope#4: per-message freshness side-table chosen
    # over dialog-level timestamp (Codex HIGH: slice-bounded refresh +
    # dialog-level TTL = false freshness) and over column-on-messages
    # (keeps row width stable; separation of concerns). Missing row =
    # "never freshened" — Plan 02 JIT path triggers naturally.
    _migrate(
        11,
        [
            "CREATE TABLE IF NOT EXISTS message_reactions_freshness ("
            "    dialog_id INTEGER NOT NULL, "
            "    message_id INTEGER NOT NULL, "
            "    checked_at INTEGER NOT NULL, "
            "    PRIMARY KEY (dialog_id, message_id)"
            ") WITHOUT ROWID",
        ],
    )

    # v12 (Phase 39.3 R1): outbox-side read cursor symmetric to read_inbox_max_id
    # (Phase 38). Nullable; bootstrap (Plan 02) fills existing rows from
    # GetPeerDialogs — the same API call that already populates the inbox
    # cursor, so zero additional Telegram traffic. SQLite's ALTER TABLE ADD
    # COLUMN has no IF NOT EXISTS form; idempotency is enforced by the
    # surrounding _migrate framework checking schema_version first. No
    # companion index — synced_dialogs is small (a few hundred rows); add
    # idx_synced_dialogs_status_outbox_null if it grows past a few thousand.
    _migrate(
        12,
        [
            "ALTER TABLE synced_dialogs ADD COLUMN read_outbox_max_id INTEGER",
        ],
    )

    # v13: store channel post author signature. Message.post_author is set when
    # a channel allows authors to sign their posts (multiple contributors). NULL
    # for all other message types. ADD COLUMN is O(1) metadata in SQLite.
    _migrate(
        13,
        [
            "ALTER TABLE messages ADD COLUMN post_author TEXT",
        ],
    )

    # v14: own-message archive for Phase 999.1 (track group messages for replies
    # and reactions). activity_comments stores messages sent by the account owner
    # across all chats (via messages.Search global own-message query). Separate
    # from the main messages table — not FTS-indexed (not a user-searchable corpus).
    # activity_sync_state is a key/value table tracking backfill progress:
    #   backfill_complete — '1' when full history scan is done, '0' otherwise
    #   backfill_offset_id — Telegram message_id pagination anchor (exclusive upper bound)
    #   last_sync_at — Unix timestamp of most recent sync run (NULL = never run)
    _migrate(
        14,
        [
            (
                "CREATE TABLE IF NOT EXISTS activity_comments ("
                "dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sent_at INTEGER NOT NULL, "
                "text TEXT, reactions TEXT, reply_count INTEGER NOT NULL DEFAULT 0, "
                "last_synced_at INTEGER, PRIMARY KEY (dialog_id, message_id))"
            ),
            "CREATE INDEX IF NOT EXISTS idx_activity_comments_sent_at ON activity_comments(sent_at DESC)",
            _ACTIVITY_SYNC_STATE_DDL,
            "INSERT OR IGNORE INTO activity_sync_state (key, value) VALUES ('backfill_complete', '0')",
            "INSERT OR IGNORE INTO activity_sync_state (key, value) VALUES ('backfill_offset_id', '0')",
            "INSERT OR IGNORE INTO activity_sync_state (key, value) VALUES ('last_sync_at', NULL)",
        ],
    )

    # v15 (Phase 999.1.1): unify messages table. Merge own-only messages from
    # activity_comments into messages (with out=1), enroll orphan dialogs in
    # synced_dialogs with status='own_only', then drop activity_comments and
    # the message_cache zombie (dropped in v7 but DDL constant survived until
    # this migration removed it).
    #
    # FTS COVERAGE NOTE (review finding from Codex + OpenCode, 2026-04-24):
    # This migration does NOT insert rows into messages_fts. The FTS gap is
    # closed at the next daemon startup by `backfill_fts_index()` in fts.py,
    # which sweeps the entire messages table and re-populates messages_fts
    # for any (dialog_id, message_id) missing from it. In practice this means
    # migrated own-only messages become searchable via SearchMessages ~one
    # daemon restart after upgrade (the same restart that runs the v15
    # migration, because daemon.py runs ensure_sync_schema → backfill_fts_index
    # on boot). Plan 03 Task 4 verifies this end-to-end via a live MCP
    # SearchMessages call against a migrated message.
    #
    # SPARSE COLUMNS NOTE (review finding, 2026-04-24):
    # activity_comments stored only 7 semantically useful columns
    # (dialog_id, message_id, sent_at, text, reactions, reply_count,
    # last_synced_at). The messages schema has ~17 columns. Migrated rows
    # therefore have NULL for sender_id, sender_first_name,
    # media_description, reply_to_msg_id, forum_topic_id, edit_date,
    # grouped_id, reply_to_peer_id, post_author. This is acceptable because
    # all migrated rows are authored by the account owner (out=1) — the
    # sender IS the user themself, which ListMessages can render as "me"
    # without looking at entities. Reactions and reply_count stored in
    # activity_comments are dropped (no destination column in messages;
    # message_reactions child table is populated only for rows ingested via
    # the canonical pipeline going forward).
    _migrate(
        15,
        [
            # 1. Bring over own-only messages that are not already in messages.
            #    activity_comments has only 4 semantically useful columns for
            #    this migration (dialog_id, message_id, sent_at, text); fill
            #    the remaining NOT NULL / defaulted columns with conservative
            #    values. out=1 is the invariant — every row from
            #    activity_comments was authored by the account owner.
            (
                "INSERT OR IGNORE INTO messages "
                "(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
                "SELECT dialog_id, message_id, sent_at, text, 1, 0, 0 "
                "FROM activity_comments "
                "WHERE (dialog_id, message_id) NOT IN "
                "(SELECT dialog_id, message_id FROM messages)"
            ),
            # 2. Enroll own-only dialogs the FullSyncWorker never touched.
            #    INSERT OR IGNORE: never overwrites 'syncing'/'synced'/
            #    'fragment'/'access_lost'. Status only escalates.
            (
                "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) "
                "SELECT DISTINCT dialog_id, 'own_only' FROM activity_comments "
                "WHERE dialog_id NOT IN (SELECT dialog_id FROM synced_dialogs)"
            ),
            # 3. Drop activity_comments (superseded by messages WHERE out=1).
            "DROP TABLE IF EXISTS activity_comments",
            # 4. Drop message_cache zombie. It was dropped in v7 but its DDL
            #    constant survived in v4's migration stmts, so every fresh DB
            #    recreated the table. v15 makes the drop permanent.
            "DROP TABLE IF EXISTS message_cache",
        ],
    )

    # v16 (Phase 47): entity_details sibling table for the new GetEntityInfo
    # tool. Per CONTEXT D-01: a JSON-blob cache keyed on entity_id with a
    # FETCHED_AT TTL stamp, foreign-keyed to entities(id) with ON DELETE
    # CASCADE so dropping an entity row also drops the cached detail. Mirrors
    # the message_reactions_freshness sibling-with-fetched_at precedent at v11.
    #
    # SCHEMA DISCRIMINATOR (D-02): the JSON payload itself carries a top-level
    # "schema": 1 field so future Telethon-driven shape changes are detectable
    # in code without another ALTER TABLE. The migration does NOT enforce or
    # validate this; the orchestrator (daemon_api._get_entity_info) writes it.
    #
    # CACHE-MISS SEMANTICS (D-03): entity_details rows are absent for the v6
    # backfill tombstones in entities (rows that exist for FK-target reasons
    # only). The orchestrator treats "entities row exists, entity_details row
    # missing" as a normal cache miss → live fetch + write back, NOT as an
    # error. No backfill is performed by this migration.
    #
    # FETCHED_AT INDEX (D-04): cheap to add at table creation; lets a future
    # phase implement cache eviction sweeps without a schema bump.
    _migrate(
        16,
        [
            (
                "CREATE TABLE IF NOT EXISTS entity_details ("
                "    entity_id   INTEGER PRIMARY KEY, "
                "    detail_json TEXT NOT NULL, "
                "    fetched_at  INTEGER NOT NULL, "
                "    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE"
                ") WITHOUT ROWID"
            ),
            "CREATE INDEX IF NOT EXISTS idx_entity_details_fetched_at ON entity_details(fetched_at)",
        ],
    )

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
