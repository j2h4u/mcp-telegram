import asyncio
import fcntl
import logging
import signal
import sqlite3
from pathlib import Path
from typing import cast

from .state import get_state_dir

_CURRENT_SCHEMA_VERSION = 26
_SCHEMA_VERSION_WITH_FTS = 3

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

_DAEMON_STATE_DDL = """
CREATE TABLE IF NOT EXISTS daemon_state (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""

# ---------------------------------------------------------------------------
# DDL for v17: dialogs snapshot table (Phase 40 — v1.6 Local Mirror)
# ---------------------------------------------------------------------------

_DIALOGS_DDL = """
CREATE TABLE IF NOT EXISTS dialogs (
    dialog_id               INTEGER PRIMARY KEY,
    name                    TEXT,
    type                    TEXT,
    archived                INTEGER NOT NULL DEFAULT 0,
    pinned                  INTEGER NOT NULL DEFAULT 0,
    members                 INTEGER,
    created                 INTEGER,
    last_message_at         INTEGER,
    snapshot_at             INTEGER,
    hidden                  INTEGER NOT NULL DEFAULT 0,
    needs_refresh           INTEGER NOT NULL DEFAULT 0,
    unread_mentions_count   INTEGER NOT NULL DEFAULT 0,
    unread_reactions_count  INTEGER NOT NULL DEFAULT 0,
    draft_text              TEXT
)
"""

_DIALOGS_HIDDEN_PINNED_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_dialogs_hidden_pinned
ON dialogs(hidden, pinned DESC)
"""

_DIALOGS_TYPE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_dialogs_type
ON dialogs(type)
"""

_DIALOGS_SNAPSHOT_AT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_dialogs_snapshot_at
ON dialogs(snapshot_at)
"""

_DIALOGS_NEEDS_REFRESH_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_dialogs_needs_refresh_hidden
ON dialogs(needs_refresh, hidden)
"""

# ---------------------------------------------------------------------------
# v19: extend topic_metadata with v1.6 columns (Phase 42 — Local Mirror)
#
# Rationale: keep the existing schema-v4 topic_metadata table (consumed by
# daemon_api.py:573 LEFT JOIN for `topic_title`); add the v1.6 forum_topics
# spec columns via additive ALTER TABLE so Plan 02 / Phase 45 read paths can
# treat topic_metadata as the canonical forum-topic snapshot.
#
# `snapshot_at` cannot be NOT NULL via ALTER TABLE (no constant default
# available); legacy rows keep snapshot_at=NULL. Phase 45 read path tolerates
# NULL via `WHERE snapshot_at IS NULL OR snapshot_at < ...` checks where
# recency matters.
# ---------------------------------------------------------------------------

_TOPIC_METADATA_V19_ALTERS = [
    "ALTER TABLE topic_metadata ADD COLUMN icon_emoji_id INTEGER",
    "ALTER TABLE topic_metadata ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE topic_metadata ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE topic_metadata ADD COLUMN snapshot_at INTEGER",
    "ALTER TABLE topic_metadata ADD COLUMN date INTEGER",
]

# ---------------------------------------------------------------------------
# v21: account trace target-specific coverage fragments (Phase 51)
# ---------------------------------------------------------------------------

_TRACE_COVERAGE_FRAGMENTS_DDL = """
CREATE TABLE IF NOT EXISTS trace_coverage_fragments (
    target_user_id INTEGER NOT NULL,
    dialog_id      INTEGER NOT NULL,
    topic_id       INTEGER NOT NULL DEFAULT 0,
    coverage_kind  TEXT NOT NULL,
    status         TEXT NOT NULL,
    fetched_at     INTEGER,
    checkpoint     TEXT,
    last_error     TEXT,
    next_retry_at  INTEGER,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (target_user_id, dialog_id, topic_id, coverage_kind)
) WITHOUT ROWID
"""

_TRACE_COVERAGE_TARGET_STATUS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_trace_coverage_target_status
ON trace_coverage_fragments(target_user_id, status, next_retry_at)
"""

# ---------------------------------------------------------------------------
# DDL for v23: per-peer self-search substrate tables (Phase 53)
#
# activity_dialog_state is the durable work/cursor table for Tier A (HotSweep)
# and Tier B (ColdBackfill) per-peer own-message sweeps. It is keyed by the
# resolved supergroup/discussion-group peer id (dialog_id). Retry/error
# bookkeeping is split per tier so a cold full-history FloodWait can NEVER
# suppress Tier-A hot sweeps (concern 5 fix).
#
# activity_channel_resolution is a second tiny table keyed by the broadcast
# channel_id (which IS known at GetFullChannelRequest flood time, before the
# linked discussion peer is resolved). It stores the durable resolver-path
# backoff so FloodWaits longer than the scheduler cadence survive daemon
# restarts (cycle-4 HIGH — concern 5 residual).
# ---------------------------------------------------------------------------

_ACTIVITY_DIALOG_STATE_DDL = """
CREATE TABLE IF NOT EXISTS activity_dialog_state (
    dialog_id           INTEGER PRIMARY KEY,
                        -- the -100… peer id; PK enforces D-03 dedup intrinsically
    source              TEXT NOT NULL,
                        -- enrollment origin: 'supergroup' | 'linked_chat'
    last_activity_at    INTEGER,
                        -- newest authored-activity epoch; drives Tier-A ≤30d eligibility
                        -- (populated by build_working_set from dialogs.last_message_at — plan 02)
    hot_cursor          INTEGER,
                        -- Tier-A newest-side message_id high-water mark
                        -- (NULL = never swept; HotSweep advances forward and persists max(batch_ids))
    hot_last_sync_at    INTEGER,
                        -- epoch of last successful Tier-A pass for this peer
    hot_next_retry_at   INTEGER,
                        -- Tier-A durable backoff (NULL = due now); set ONLY by HotSweep
    hot_last_error      TEXT,
                        -- sanitized Tier-A error class (no content)
    cold_offset_id      INTEGER,
                        -- Tier-B backward-walk message_id cursor
                        -- (NULL = start from newest; ColdBackfill advances downward and persists min(batch_ids))
    cold_status         TEXT NOT NULL DEFAULT 'pending',
                        -- Tier-B state machine: 'pending' | 'running' | 'complete'
    cold_next_retry_at  INTEGER,
                        -- Tier-B durable backoff (NULL = due now); set ONLY by ColdBackfill —
                        -- this is the single owner of FloodWait retry for full-history walks (concern 5)
    cold_last_error     TEXT,
                        -- sanitized Tier-B error class (no content)
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
) WITHOUT ROWID
"""

_ACTIVITY_DIALOG_STATE_HOT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_activity_dialog_state_hot
ON activity_dialog_state(last_activity_at, hot_next_retry_at)
"""
# Tier-A selection: recency-bounded due peers
# WHERE last_activity_at >= :cutoff AND (hot_next_retry_at IS NULL OR hot_next_retry_at <= :now)

_ACTIVITY_DIALOG_STATE_COLD_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_activity_dialog_state_cold
ON activity_dialog_state(cold_status, cold_next_retry_at)
"""
# Tier-B selection: pending/running due peers
# WHERE cold_status IN ('pending', 'running') AND (cold_next_retry_at IS NULL OR cold_next_retry_at <= :now)

# ---------------------------------------------------------------------------
# DDL for v24: linked-chat columns on dialogs (Phase 54)
#
# Two NULL-able columns are added to dialogs to promote linked-chat resolution
# from the polled entity_details cache to a first-class, event-maintained
# contract:
#
#   linked_chat_id INTEGER NULL
#     The discussion group's -100… peer id.  NULL means "no linked chat exists".
#     A NOT NULL value is the canonical answer; resolver (plan 02) normalises the
#     id to -100… form via Telethon's get_peer_id(PeerChannel(…)) before writing.
#
#   linked_chat_resolved_at INTEGER NULL
#     Unix-seconds timestamp of the last authoritative answer received from
#     GetFullChannelRequest or an UpdateChannel event.
#     NULL = never asked → resolver cold path must fire on next access.
#     NOT NULL = authoritative answer on record; linked_chat_id may still be NULL
#               (meaning: we asked, and the channel has no discussion group).
# ---------------------------------------------------------------------------

_DIALOGS_V24_ADD_LINKED_CHAT_ID = "ALTER TABLE dialogs ADD COLUMN linked_chat_id INTEGER"

_DIALOGS_V24_ADD_LINKED_CHAT_RESOLVED_AT = "ALTER TABLE dialogs ADD COLUMN linked_chat_resolved_at INTEGER"

_DIALOGS_V24_BACKFILL_LINKED_CHAT = """
UPDATE dialogs
SET linked_chat_id = (
        SELECT json_extract(ed.detail_json, '$.linked_chat_id')
        FROM entity_details ed
        JOIN entities e ON e.id = ed.entity_id
        WHERE ed.entity_id = dialogs.dialog_id
          AND e.type = 'channel'
          AND json_type(ed.detail_json, '$.linked_chat_id') IS NOT NULL
    ),
    linked_chat_resolved_at = (
        SELECT ed.fetched_at
        FROM entity_details ed
        JOIN entities e ON e.id = ed.entity_id
        WHERE ed.entity_id = dialogs.dialog_id
          AND e.type = 'channel'
          AND json_type(ed.detail_json, '$.linked_chat_id') IS NOT NULL
    )
WHERE dialogs.type = 'channel'
  AND EXISTS (
        SELECT 1 FROM entity_details ed
        JOIN entities e ON e.id = ed.entity_id
        WHERE ed.entity_id = dialogs.dialog_id
          AND e.type = 'channel'
          AND json_type(ed.detail_json, '$.linked_chat_id') IS NOT NULL
    )
"""

_ENTITY_DETAILS_V24_STRIP_LINKED_CHAT = """
UPDATE entity_details
SET detail_json = json_remove(detail_json, '$.linked_chat_id')
WHERE entity_id IN (SELECT id FROM entities WHERE type = 'channel')
  AND json_type(detail_json, '$.linked_chat_id') IS NOT NULL
"""

_DROP_ACTIVITY_CHANNEL_RESOLUTION = "DROP TABLE IF EXISTS activity_channel_resolution"

# ---------------------------------------------------------------------------
# v25 (Bug #1 orphan own_only fix): one-shot backfill of thin dialogs rows.
#
# Phase 53's enroll_activity_dialog wrote only synced_dialogs(status='own_only')
# + activity_dialog_state, never a dialogs row. Result: ~88 of 192 own_only peers
# have no dialogs row and surface as raw numeric IDs in get_my_recent_activity.
#
# This INSERT...SELECT materialises a thin dialogs row (needs_refresh=1, name NULL)
# for every own_only peer lacking one. INSERT OR IGNORE is belt-and-suspenders: the
# WHERE d.dialog_id IS NULL already excludes resolved peers, and OR IGNORE guarantees
# no clobber of name/type/needs_refresh even under a concurrent enroll. The existing
# DialogReconciler.run_light_pass (WHERE needs_refresh=1 AND hidden=0) fills
# name/type/members/created on its hourly cycle — no new resolution machinery.
# ---------------------------------------------------------------------------

_DIALOGS_V25_BACKFILL_ORPHAN_OWN_ONLY = """
INSERT OR IGNORE INTO dialogs
    (dialog_id, needs_refresh, snapshot_at, archived, pinned, hidden,
     unread_mentions_count, unread_reactions_count)
SELECT s.dialog_id, 1, strftime('%s','now'), 0, 0, 0, 0, 0
FROM synced_dialogs s
LEFT JOIN dialogs d ON d.dialog_id = s.dialog_id
WHERE s.status = 'own_only' AND d.dialog_id IS NULL
"""


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def get_sync_db_path() -> Path:
    """Return the canonical path for sync.db under configured state."""
    return get_state_dir() / "sync.db"


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


def _row_first_int(row: tuple[object | None, ...] | None) -> int:
    if row is None:
        return 0
    value = row[0]
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


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
    row = cast(tuple[object | None, ...] | None, conn.execute("PRAGMA journal_mode").fetchone())
    if row is None or str(row[0]).lower() != "wal":
        return False
    try:
        row = cast(tuple[object | None, ...] | None, conn.execute("SELECT MAX(version) FROM schema_version").fetchone())
        return _row_first_int(row) >= _CURRENT_SCHEMA_VERSION
    except sqlite3.OperationalError:
        return False


def _apply_migration(
    conn: sqlite3.Connection,
    current: int,
    version: int,
    stmts: list[str],
    *,
    ignore_duplicate_column: bool = False,
) -> int:
    """Apply one migration version atomically and record it.

    When `ignore_duplicate_column=True`, each statement is executed
    individually and `OperationalError: duplicate column name` is
    silently swallowed. This is necessary for ALTER TABLE ADD COLUMN
    migrations that may re-run after a manual `DELETE FROM schema_version`
    in tests, or on databases where a partial migration already added the
    column. All other errors still propagate and roll back.
    """
    if current >= version:
        return current
    try:
        for stmt in stmts:
            if ignore_duplicate_column:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" in str(exc).lower():
                        logger.debug("sync_db v%d: column already exists, skipping: %s", version, exc)
                    else:
                        raise
            else:
                conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version VALUES (?, strftime('%s', 'now'))",
            (version,),
        )
        conn.commit()
        return version
    except Exception:
        conn.rollback()
        logger.error("sync_db migration to version %d failed", version, exc_info=True)
        raise


def _apply_migrations_1_to_5(conn: sqlite3.Connection, current: int) -> int:
    current = _apply_migration(
        conn, current, 1, [_SYNCED_DIALOGS_DDL, _MESSAGES_DDL, _MESSAGES_INDEX_DDL, _MESSAGE_VERSIONS_DDL]
    )
    current = _apply_migration(conn, current, 2, ["ALTER TABLE synced_dialogs ADD COLUMN access_lost_at INTEGER"])

    if current < _SCHEMA_VERSION_WITH_FTS:
        from .fts import MESSAGES_FTS_DDL

        current = _apply_migration(conn, current, 3, [MESSAGES_FTS_DDL])

    current = _apply_migration(
        conn,
        current,
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
    return _apply_migration(conn, current, 5, [_TELEMETRY_EVENTS_DDL, _TELEMETRY_EVENTS_INDEX_DDL])


def _apply_migrations_6_to_10(conn: sqlite3.Connection, current: int) -> int:
    current = _apply_migration(
        conn,
        current,
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

    current = _apply_migration(
        conn,
        current,
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

    current = _apply_migration(
        conn,
        current,
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
    current = _apply_migration(
        conn,
        current,
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
    return _apply_migration(
        conn,
        current,
        10,
        [
            "UPDATE messages SET out = 1 WHERE out = 0 AND dialog_id > 0 AND sender_id IS NULL",
        ],
    )


def _apply_migrations_11_to_15(conn: sqlite3.Connection, current: int) -> int:
    # v11 per CONTEXT.md §Scope#4: per-message freshness side-table chosen
    # over dialog-level timestamp (Codex HIGH: slice-bounded refresh +
    # dialog-level TTL = false freshness) and over column-on-messages
    # (keeps row width stable; separation of concerns). Missing row =
    # "never freshened" — Plan 02 JIT path triggers naturally.
    current = _apply_migration(
        conn,
        current,
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
    current = _apply_migration(
        conn,
        current,
        12,
        [
            "ALTER TABLE synced_dialogs ADD COLUMN read_outbox_max_id INTEGER",
        ],
    )

    # v13: store channel post author signature. Message.post_author is set when
    # a channel allows authors to sign their posts (multiple contributors). NULL
    # for all other message types. ADD COLUMN is O(1) metadata in SQLite.
    current = _apply_migration(
        conn,
        current,
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
    current = _apply_migration(
        conn,
        current,
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
    return _apply_migration(
        conn,
        current,
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


def _apply_migrations_16_to_20(conn: sqlite3.Connection, current: int) -> int:
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
    current = _apply_migration(
        conn,
        current,
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

    # v17 (Phase 40): dialogs snapshot table for v1.6 Local Mirror milestone.
    # Separate from synced_dialogs (sync machinery) and entities (sender data) — MIRROR-03.
    # unread_count is intentionally absent — computed from local read cursor (MIRROR-05).
    # Phase 41 bootstrap populates rows; Phase 42 event handlers update them in real time.
    current = _apply_migration(
        conn,
        current,
        17,
        [
            _DIALOGS_DDL,
            _DIALOGS_HIDDEN_PINNED_INDEX_DDL,
            _DIALOGS_TYPE_INDEX_DDL,
            _DIALOGS_SNAPSHOT_AT_INDEX_DDL,
        ],
    )

    # v18 (Phase 41): generic key/value daemon_state table (D-01).
    # Bootstrap sweep cursor (D-02: offset_date / offset_id / offset_peer) and
    # completion flag (D-03: bootstrap_sweep_status) live here. No seed rows —
    # absence of bootstrap_sweep_status is the canonical "not run yet" state (D-04).
    current = _apply_migration(conn, current, 18, [_DAEMON_STATE_DDL])

    # v19 (Phase 42): augment topic_metadata with v1.6 forum_topics columns.
    # Plan 02 event handlers UPSERT title / icon_emoji_id / hidden here; the
    # dedicated UpdatePinnedForumTopic handler toggles pinned. Phase 45
    # ListTopics reads from this same table.
    current = _apply_migration(conn, current, 19, _TOPIC_METADATA_V19_ALTERS, ignore_duplicate_column=True)

    # v20 (Phase 43 / RECON-02): composite index gating the hourly light pass.
    # Plan 02's _SELECT_DIRTY_DIALOGS_SQL filters
    # `WHERE needs_refresh = 1 AND hidden = 0`. Without this index it is a full
    # table scan every hour; with it, the planner uses the index leftmost-prefix
    # on needs_refresh and drops the dialog count to roughly the dirty set size.
    return _apply_migration(conn, current, 20, [_DIALOGS_NEEDS_REFRESH_INDEX_DDL])


def _apply_migrations_21_to_26(conn: sqlite3.Connection, current: int) -> int:
    # v21 (Phase 51): target-specific trace coverage. synced_dialogs.status
    # describes broad dialog lifecycle; account traces need per-target,
    # per-dialog/topic coverage attempts to avoid false completeness claims.
    # topic_id=0 is reserved as the dialog-level sentinel; real forum topic ids
    # in topic_metadata are >= 1.
    current = _apply_migration(
        conn,
        current,
        21,
        [
            _TRACE_COVERAGE_FRAGMENTS_DDL,
            _TRACE_COVERAGE_TARGET_STATUS_INDEX_DDL,
        ],
    )

    # v22: persist Telegram's aggregate reply/comment counter on message rows.
    # Telethon exposes this as Message.replies.replies. It is a count of replies,
    # not a unique replier count; historical rows default to 0 until refreshed.
    current = _apply_migration(
        conn,
        current,
        22,
        [
            "ALTER TABLE messages ADD COLUMN reply_count INTEGER NOT NULL DEFAULT 0",
        ],
        ignore_duplicate_column=True,
    )

    # v23 (Phase 53): per-peer own-message sweep substrate tables.
    # activity_dialog_state: durable work/cursor table for Tier A (HotSweep) and
    # Tier B (ColdBackfill); per-tier retry/error columns — no shared next_retry_at.
    # NOTE: activity_channel_resolution was originally created here but is dropped
    # by v24. Its DDL constant has been removed; a fresh install on v24 never creates
    # the table, and existing v23 deployments have it removed by the v24 DROP.
    current = _apply_migration(
        conn,
        current,
        23,
        [
            _ACTIVITY_DIALOG_STATE_DDL,
            _ACTIVITY_DIALOG_STATE_HOT_INDEX_DDL,
            _ACTIVITY_DIALOG_STATE_COLD_INDEX_DDL,
        ],
    )

    # v24 (Phase 54): promote linked-chat resolution to first-class dialogs columns.
    #
    # (i)  Pure SQL — entity_details already has 128/128 broadcast-channel coverage
    #      from the Phase-53 resolver passes (99 with linked_chat_id, 29 with the key
    #      explicitly set to JSON null, 0 missing). No Telethon calls during migration.
    #
    # (ii) json_type(detail_json, '$.linked_chat_id') IS NOT NULL is the correct
    #      SQLite predicate for "key present". json_extract returns SQL NULL for both
    #      "key absent" and "key present with JSON null value"; json_type returns the
    #      string 'null' (non-NULL) when the key is present with JSON null, and SQL
    #      NULL only when the key is absent entirely. Using json_type preserves the
    #      29 explicitly-resolved-none channels by setting linked_chat_resolved_at to
    #      a real timestamp while linked_chat_id stays SQL NULL.
    #
    # (iii) The DROP is safe: the new event-driven model in plans 02–04 recreates
    #      resolution state implicitly via dialogs.linked_chat_resolved_at. The backoff
    #      table is moot once resolved_at IS NULL is the retry signal.
    #
    # (iv) Forward-compat: removing _ACTIVITY_CHANNEL_RESOLUTION_DDL from the v23
    #      list means a fresh install on v24 never creates the table; existing v23
    #      deployments have it removed by the DROP below.
    current = _apply_migration(
        conn,
        current,
        24,
        [
            _DIALOGS_V24_ADD_LINKED_CHAT_ID,
            _DIALOGS_V24_ADD_LINKED_CHAT_RESOLVED_AT,
            _DIALOGS_V24_BACKFILL_LINKED_CHAT,
            _ENTITY_DETAILS_V24_STRIP_LINKED_CHAT,
            _DROP_ACTIVITY_CHANNEL_RESOLUTION,
        ],
        ignore_duplicate_column=True,
    )

    # v25 (Bug #1 orphan own_only fix): one-shot backfill of thin dialogs rows for
    # the ~88 pre-existing own_only peers that Phase 53 never wrote to dialogs.
    #
    # (i)  Pure SQL INSERT...SELECT — no Telethon calls. Each materialised row carries
    #      needs_refresh=1, name/type NULL; DialogReconciler.run_light_pass then fills
    #      name/type/members/created on its hourly cycle (the Lazy approach — reuses the
    #      Phase 43 light-reconciliation path, zero new resolution code).
    #
    # (ii) No ignore_duplicate_column: this is an INSERT...SELECT (not ALTER TABLE), so
    #      the default error-propagating path is correct.
    #
    # (iii) FloodWait: ~88 net-new candidates enter the light-pass queue at once. Shipped
    #      un-capped per operator decision — observe the backfill catch-up logs for a
    #      sustained burst. The cap/stagger mitigation (needs_refresh tier) is DEFERRED
    #      to a follow-up only if observation shows it is needed.
    current = _apply_migration(
        conn,
        current,
        25,
        [
            _DIALOGS_V25_BACKFILL_ORPHAN_OWN_ONLY,
        ],
    )

    # v26 (forward-source marked-id normalisation): store message_forwards.fwd_from_peer_id
    # as a MARKED id (-100… channel, -id legacy chat, +id user) — same convention as
    # dialogs.dialog_id / entities.id — so the column is JOINable and unambiguous about peer
    # kind. The write path now emits marked ids; this migrates pre-existing bare rows.
    #
    # Pure SQL, no Telethon calls: a bare positive int alone cannot reveal peer kind, so we
    # only remark rows whose marked form is a peer we already know locally (present in
    # `dialogs`). Known users keep bare == marked (no row touched). Forwards from channels we
    # are NOT a member of stay bare here and are re-derived by a separate one-shot re-scan
    # that reads the message's typed from_id (network, FloodWait-aware) — deliberately kept
    # out of startup migration.
    #
    # Order matters: the channel UPDATE turns matched rows negative; the chat UPDATE then
    # only sees still-positive rows. No row can match both (distinct dialog_ids).
    return _apply_migration(
        conn,
        current,
        26,
        [
            # Defensive no-op in real DBs (message_forwards exists since v7); guarantees the
            # table is present so the UPDATEs below never hit "no such table" on partial DBs.
            """CREATE TABLE IF NOT EXISTS message_forwards (
    dialog_id        INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    fwd_from_peer_id INTEGER,
    fwd_from_name    TEXT,
    fwd_date         INTEGER,
    fwd_channel_post INTEGER,
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID""",
            # channel/supergroup: bare -> -1000000000000 - bare when that marked id is a known dialog
            "UPDATE message_forwards SET fwd_from_peer_id = -1000000000000 - fwd_from_peer_id "
            "WHERE fwd_from_peer_id > 0 AND EXISTS (SELECT 1 FROM dialogs d "
            "WHERE d.dialog_id = -1000000000000 - message_forwards.fwd_from_peer_id)",
            # legacy chat: bare -> -bare when -bare is a known dialog
            "UPDATE message_forwards SET fwd_from_peer_id = -fwd_from_peer_id "
            "WHERE fwd_from_peer_id > 0 AND EXISTS (SELECT 1 FROM dialogs d "
            "WHERE d.dialog_id = -message_forwards.fwd_from_peer_id)",
        ],
    )


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

    row = cast(tuple[object | None, ...] | None, conn.execute("SELECT MAX(version) FROM schema_version").fetchone())
    current = _row_first_int(row)
    current = _apply_migrations_1_to_5(conn, current)
    current = _apply_migrations_6_to_10(conn, current)
    current = _apply_migrations_11_to_15(conn, current)
    current = _apply_migrations_16_to_20(conn, current)
    current = _apply_migrations_21_to_26(conn, current)

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
    feedback_conn: sqlite3.Connection | None = None,
) -> asyncio.Event:
    """Register a SIGTERM handler that checkpoints sync.db (and feedback.db if provided) before exit.

    Both checkpoint blocks are isolated in their own try/except so a failure
    in either CANNOT prevent shutdown_event.set() — the set() is what unblocks
    sync_main and lets the process exit. Order: sync.db first (production-
    critical), feedback.db second (low-value). The default None keeps every
    existing call site working without modification.

    Returns an asyncio.Event that will be set when SIGTERM is received.
    The caller (sync daemon) should await this event to know when to stop.
    """
    shutdown_event = asyncio.Event()

    def _on_sigterm() -> None:
        logger.info("SIGTERM received — checkpointing sync.db")
        # ── sync.db checkpoint — isolated ────────────────────────────────────
        try:
            conn.rollback()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logger.exception("sync.db shutdown error")
        # ── feedback.db checkpoint — isolated, MUST NOT block shutdown ───────
        if feedback_conn is not None:
            try:
                feedback_conn.rollback()
                feedback_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                # Swallow: feedback.db corruption / lock cannot be allowed to
                # delay shutdown_event.set(). Logged for postmortem only.
                logger.exception("feedback.db shutdown error (suppressed — shutdown continues)")
        # ── Always set, even if both checkpoints raised above ─────────────────
        shutdown_event.set()  # signal AFTER checkpoints so handlers don't race

    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    return shutdown_event
