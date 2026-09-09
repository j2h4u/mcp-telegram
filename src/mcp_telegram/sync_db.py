import fcntl
import logging
import math
import sqlite3
from pathlib import Path
from typing import cast

from .alert_policy import incoming_human_dm_sql
from .dialog_classification import (
    SERVICE_DIALOG_TYPE,
    is_bot_dialog_type,
    is_reserved_replies_username,
)

_CURRENT_SCHEMA_VERSION = 60
_SCHEMA_VERSION_WITH_FTS = 3
_EVENT_STORE_MIGRATION_51 = 51
_MESSAGE_ORIGIN_MIGRATION_52 = 52
_MESSAGE_HISTORY_MIGRATION_53 = 53
_EVENT_NAMES_MIGRATION_54 = 54
_ACCESS_CAUSE_MIGRATION_55 = 55
_TOOL_CAPABILITY_MIGRATION_56 = 56
_ENTITY_PROFILE_SECTIONS_MIGRATION_57 = 57
_ACCOUNT_TRACE_INDEXES_MIGRATION_58 = 58
_SCHEDULED_RECONCILIATION_MIGRATION_59 = 59
_DOMAIN_RESUME_STATE_MIGRATION_60 = 60

_ACCOUNT_COOLDOWN_UNTIL_UTC_KEY = "telegram_account_cooldown_until_utc"
_SELF_PROFILE_LAST_SUCCESS_AT_KEY = "self_profile_last_success_at"

# Product-owned scheduled reconciliation targets.  Keep these values here so
# schema bootstrap and the legacy worker cannot drift apart.
SCHEDULED_ACTIVE_REPAIR_SECONDS = 15 * 60
SCHEDULED_QUIET_DISCOVERY_SECONDS = 24 * 60 * 60

logger = logging.getLogger(__name__)

type SyncDatabaseConnection = sqlite3.Connection

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

_MESSAGES_OWN_ACTIVITY_SENT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_own_activity_sent
ON messages(sent_at DESC, dialog_id DESC, message_id DESC)
WHERE out = 1 AND is_service = 0 AND is_deleted = 0
"""

_MESSAGES_DIALOG_SUMMARY_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_dialog_summary
ON messages(dialog_id, is_deleted, is_service, out, message_id)
"""

_MESSAGES_ACCOUNT_TRACE_SENDER_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_account_trace_sender
ON messages(sender_id, sent_at DESC, dialog_id DESC, message_id DESC)
WHERE is_deleted = 0 AND is_service = 0
"""

_MESSAGES_ACCOUNT_TRACE_POST_AUTHOR_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_account_trace_post_author
ON messages(post_author, sent_at DESC, dialog_id DESC, message_id DESC)
WHERE is_deleted = 0 AND is_service = 0 AND post_author IS NOT NULL
"""

_MESSAGE_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS message_versions (
    dialog_id   INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    old_text    TEXT,
    edit_date   INTEGER,
    origin      TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK (origin IN ('telegram_edit', 'transcription', 'legacy_unknown')),
    PRIMARY KEY (dialog_id, message_id, version)
) WITHOUT ROWID
"""

_MESSAGE_TRANSCRIPTIONS_DDL = """
CREATE TABLE IF NOT EXISTS message_transcriptions (
    dialog_id        INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    text             TEXT NOT NULL CHECK (trim(text) <> ''),
    transcription_id INTEGER NOT NULL,
    received_at      INTEGER NOT NULL,
    PRIMARY KEY (dialog_id, message_id)
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
    icon_emoji_id  INTEGER,
    icon_emoji     TEXT,
    icon_color     INTEGER,
    pinned         INTEGER NOT NULL DEFAULT 0,
    hidden         INTEGER NOT NULL DEFAULT 0,
    snapshot_at    INTEGER,
    date           INTEGER,
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
    outcome TEXT NOT NULL DEFAULT 'success',
    error_code TEXT,
    error_type TEXT
)
"""

# Mid-chain fixture/upgrade databases may have a schema_version row without
# having replayed v5. v47 creates this legacy-compatible base before adding
# its own columns; an actually malformed existing table still fails loudly.
_TELEMETRY_EVENTS_BASE_DDL = """
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

_DAEMON_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS daemon_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    dialog_id   INTEGER,
    occurred_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
)
"""

_DAEMON_EVENTS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_daemon_events_kind_time
ON daemon_events(kind, occurred_at DESC)
"""

_RUNTIME_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS runtime_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at_ms      INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    runtime_instance_id TEXT NOT NULL,
    operation_id        TEXT,
    outcome             TEXT,
    reason_code         TEXT,
    dialog_id           INTEGER,
    duration_ms         REAL,
    tool_name           TEXT,
    result_count        INTEGER,
    has_cursor          INTEGER,
    page_depth          INTEGER,
    has_filter          INTEGER,
    error_type          TEXT,
    payload_json        TEXT NOT NULL DEFAULT '{}'
)
"""

_RUNTIME_EVENTS_INDEXES_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_runtime_events_time ON runtime_events(observed_at_ms DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_events_kind_time ON runtime_events(kind, observed_at_ms DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_events_dialog_time ON runtime_events(dialog_id, observed_at_ms DESC, id DESC) WHERE dialog_id IS NOT NULL",
)

_SYNC_ALERT_EVENTS_V51_DDL = """
CREATE TABLE sync_alert_events_v51 (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('deleted_message', 'edit', 'access_lost')),
    occurred_at INTEGER NOT NULL,
    dialog_id   INTEGER NOT NULL,
    message_id  INTEGER,
    version     INTEGER,
    CHECK (
        (kind = 'deleted_message' AND message_id IS NOT NULL AND version IS NULL)
        OR (kind = 'edit' AND message_id IS NOT NULL AND version IS NOT NULL)
        OR (kind = 'access_lost' AND message_id IS NULL AND version IS NULL)
    )
)
"""


# Historical alert reconstruction requires positive evidence for every part of
# an incoming human DM.  In particular, a missing entity row is unknown rather
# than proof that a dialog is a user; service messages are never user alerts.
def _strict_human_dm_alert_sql(alias: str) -> str:
    return incoming_human_dm_sql(alias)


_HUMAN_DM_ALERT_PREDICATE = _strict_human_dm_alert_sql("NEW")
_LEGACY_HUMAN_DM_ALERT_PREDICATE = _strict_human_dm_alert_sql("m")


_SYNC_ALERT_V51_TRIGGERS = (
    f"""CREATE TRIGGER sync_alert_events_message_insert_deleted
        AFTER INSERT ON messages
        WHEN NEW.is_deleted = 1 AND NEW.deleted_at IS NOT NULL
         AND {_HUMAN_DM_ALERT_PREDICATE}
        BEGIN
          INSERT OR IGNORE INTO sync_alert_events(kind, occurred_at, dialog_id, message_id)
          VALUES ('deleted_message', NEW.deleted_at, NEW.dialog_id, NEW.message_id);
        END""",
    f"""CREATE TRIGGER sync_alert_events_message_delete_transition
        AFTER UPDATE OF is_deleted, deleted_at ON messages
        WHEN OLD.is_deleted = 0 AND NEW.is_deleted = 1 AND NEW.deleted_at IS NOT NULL
         AND {_HUMAN_DM_ALERT_PREDICATE}
        BEGIN
          INSERT OR IGNORE INTO sync_alert_events(kind, occurred_at, dialog_id, message_id)
          VALUES ('deleted_message', NEW.deleted_at, NEW.dialog_id, NEW.message_id);
        END""",
)

_RUNTIME_OBSERVATIONS_V54_DDL = """
CREATE TABLE runtime_observations_v54 (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at_ms      INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    runtime_instance_id TEXT NOT NULL,
    operation_id        TEXT,
    outcome             TEXT,
    reason_code         TEXT,
    dialog_id           INTEGER,
    duration_ms         REAL,
    tool_name           TEXT,
    result_count        INTEGER,
    has_cursor          INTEGER,
    page_depth          INTEGER,
    has_filter          INTEGER,
    error_type          TEXT,
    source_namespace    TEXT,
    source_event_id     INTEGER,
    payload_json        TEXT NOT NULL DEFAULT '{}'
)
"""

_CONVERSATION_HISTORY_EVENTS_V54_DDL = """
CREATE TABLE conversation_history_events_v54 (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL CHECK (kind IN ('deleted_message', 'edit', 'access_lost', 'access_restored')),
    occurred_at      INTEGER NOT NULL,
    time_basis       TEXT NOT NULL CHECK (time_basis IN ('telegram', 'observed')),
    dialog_id        INTEGER NOT NULL,
    message_id       INTEGER,
    version          INTEGER,
    reason_code      TEXT,
    previous_status  TEXT,
    source_namespace TEXT,
    source_event_id  INTEGER,
    CHECK (
        (kind = 'deleted_message' AND message_id IS NOT NULL AND version IS NULL)
        OR (kind = 'edit' AND message_id IS NOT NULL AND version IS NOT NULL)
        OR (kind IN ('access_lost', 'access_restored') AND message_id IS NULL AND version IS NULL)
    )
)
"""

_CONVERSATION_HISTORY_INDEXES_V54_DDL = (
    "CREATE UNIQUE INDEX idx_conversation_history_deleted ON conversation_history_events(dialog_id, message_id) WHERE kind = 'deleted_message'",
    "CREATE UNIQUE INDEX idx_conversation_history_edit ON conversation_history_events(dialog_id, message_id, version) WHERE kind = 'edit'",
    "CREATE INDEX idx_conversation_history_lifecycle ON conversation_history_events(dialog_id, seq DESC) WHERE kind IN ('access_lost', 'access_restored')",
    "CREATE UNIQUE INDEX idx_conversation_history_source ON conversation_history_events(source_namespace, source_event_id) WHERE source_namespace IS NOT NULL AND source_event_id IS NOT NULL",
)

_EVENT_RECOVERY_LEDGER_DDL = """
CREATE TABLE event_recovery_ledger (
    source_fingerprint TEXT PRIMARY KEY,
    imported_at INTEGER NOT NULL,
    legacy_observation_count INTEGER NOT NULL,
    enriched_history_count INTEGER NOT NULL
)
"""

_CONVERSATION_HISTORY_TRIGGERS_V54 = (
    f"""CREATE TRIGGER conversation_history_message_insert_deleted
        AFTER INSERT ON messages
        WHEN NEW.is_deleted = 1 AND NEW.deleted_at IS NOT NULL
         AND {_HUMAN_DM_ALERT_PREDICATE}
        BEGIN
          INSERT OR IGNORE INTO conversation_history_events(
              kind, occurred_at, time_basis, dialog_id, message_id
          ) VALUES ('deleted_message', NEW.deleted_at, 'observed', NEW.dialog_id, NEW.message_id);
        END""",
    f"""CREATE TRIGGER conversation_history_message_delete_transition
        AFTER UPDATE OF is_deleted, deleted_at ON messages
        WHEN OLD.is_deleted = 0 AND NEW.is_deleted = 1 AND NEW.deleted_at IS NOT NULL
         AND {_HUMAN_DM_ALERT_PREDICATE}
        BEGIN
          INSERT OR IGNORE INTO conversation_history_events(
              kind, occurred_at, time_basis, dialog_id, message_id
          ) VALUES ('deleted_message', NEW.deleted_at, 'observed', NEW.dialog_id, NEW.message_id);
        END""",
    """CREATE TRIGGER conversation_history_no_update
        BEFORE UPDATE ON conversation_history_events
        BEGIN SELECT RAISE(ABORT, 'conversation history is append-only'); END""",
    """CREATE TRIGGER conversation_history_no_delete
        BEFORE DELETE ON conversation_history_events
        BEGIN SELECT RAISE(ABORT, 'conversation history is append-only'); END""",
)

_SYNC_ALERT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS sync_alert_events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN ('deleted_message', 'edit', 'access_lost')),
    occurred_at     INTEGER NOT NULL,
    dialog_id       INTEGER NOT NULL,
    message_id      INTEGER,
    version         INTEGER,
    daemon_event_id INTEGER,
    CHECK (
        (kind = 'deleted_message' AND message_id IS NOT NULL AND version IS NULL AND daemon_event_id IS NULL)
        OR (kind = 'edit' AND message_id IS NOT NULL AND version IS NOT NULL AND daemon_event_id IS NULL)
        OR (kind = 'access_lost' AND message_id IS NULL AND version IS NULL AND daemon_event_id IS NOT NULL)
    )
)
"""

_SYNC_ALERT_DELETED_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_alert_events_deleted_source
ON sync_alert_events(dialog_id, message_id) WHERE kind = 'deleted_message'
"""

_SYNC_ALERT_EDIT_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_alert_events_edit_source
ON sync_alert_events(dialog_id, message_id, version) WHERE kind = 'edit'
"""

_SYNC_ALERT_ACCESS_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_alert_events_access_source
ON sync_alert_events(daemon_event_id) WHERE kind = 'access_lost'
"""

_SYNC_ALERT_DELETED_INSERT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS sync_alert_events_message_insert_deleted
AFTER INSERT ON messages
WHEN NEW.is_deleted = 1 AND NEW.deleted_at IS NOT NULL
BEGIN
    INSERT OR IGNORE INTO sync_alert_events(kind, occurred_at, dialog_id, message_id)
    VALUES ('deleted_message', NEW.deleted_at, NEW.dialog_id, NEW.message_id);
END
"""

_SYNC_ALERT_DELETED_UPDATE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS sync_alert_events_message_delete_transition
AFTER UPDATE OF is_deleted, deleted_at ON messages
WHEN OLD.is_deleted = 0 AND NEW.is_deleted = 1 AND NEW.deleted_at IS NOT NULL
BEGIN
    INSERT OR IGNORE INTO sync_alert_events(kind, occurred_at, dialog_id, message_id)
    VALUES ('deleted_message', NEW.deleted_at, NEW.dialog_id, NEW.message_id);
END
"""

_SYNC_ALERT_EDIT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS sync_alert_events_message_edit
AFTER INSERT ON message_versions
WHEN NEW.edit_date IS NOT NULL
BEGIN
    INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, message_id, version)
    VALUES ('edit', NEW.edit_date, NEW.dialog_id, NEW.message_id, NEW.version);
END
"""

_SYNC_ALERT_ACCESS_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS sync_alert_events_access_lost
AFTER INSERT ON daemon_events
WHEN NEW.kind = 'access_lost'
BEGIN
    INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, daemon_event_id)
    VALUES ('access_lost', NEW.occurred_at, NEW.dialog_id, NEW.id);
END
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

# v34: durable authorization for full-history work.  Coverage/work status
# remains in synced_dialogs; absence here means no operator decision.
_FULL_HISTORY_ENROLLMENT_DDL = """
CREATE TABLE IF NOT EXISTS full_history_enrollment (
    dialog_id  INTEGER PRIMARY KEY,
    enabled    INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    source     TEXT NOT NULL CHECK (source IN ('explicit', 'automatic', 'migration')),
    updated_at INTEGER NOT NULL
) WITHOUT ROWID
"""

_FULL_HISTORY_ENROLLMENT_ENABLED_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_full_history_enrollment_enabled
ON full_history_enrollment(enabled, updated_at)
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
    unread_count            INTEGER,
    unread_mark             INTEGER,
    unread_count_observed_at INTEGER,
    unread_mark_observed_at INTEGER,
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
# DDL for v60: restart-safe domain cursors used by durable demand adapters.
#
# The tables remain owned by their domains.  They intentionally do not form a
# generic job/lifecycle store.
# ---------------------------------------------------------------------------

_DIALOG_FULL_RECONCILIATION_STATE_DDL = """
CREATE TABLE IF NOT EXISTS dialog_full_reconciliation_state (
    singleton       INTEGER PRIMARY KEY CHECK(singleton = 1),
    generation      INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    status          TEXT NOT NULL DEFAULT 'idle' CHECK(status IN ('idle', 'in_progress')),
    offset_date     TEXT,
    offset_id       INTEGER NOT NULL DEFAULT 0,
    offset_peer     TEXT,
    started_at      INTEGER,
    observed_count  INTEGER NOT NULL DEFAULT 0 CHECK(observed_count >= 0)
)
"""

_DIALOG_FULL_RECONCILIATION_BASELINE_DDL = """
CREATE TABLE IF NOT EXISTS dialog_full_reconciliation_baseline (
    generation        INTEGER NOT NULL,
    dialog_id         INTEGER NOT NULL,
    baseline_revision INTEGER NOT NULL CHECK(baseline_revision >= 0),
    seen              INTEGER NOT NULL DEFAULT 0 CHECK(seen IN (0, 1)),
    PRIMARY KEY(generation, dialog_id)
) WITHOUT ROWID
"""

_DIALOG_FULL_RECONCILIATION_UNSEEN_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_dialog_full_reconciliation_unseen
ON dialog_full_reconciliation_baseline(generation, seen, dialog_id)
"""

_DIALOGS_REVISION_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS dialogs_revision_after_update
AFTER UPDATE ON dialogs
WHEN NEW.revision = OLD.revision
BEGIN
    UPDATE dialogs
       SET revision = OLD.revision + 1
     WHERE dialog_id = NEW.dialog_id;
END
"""

_DELTA_ACCESS_RECOVERY_STATE_DDL = """
CREATE TABLE IF NOT EXISTS delta_access_recovery_state (
    dialog_id          INTEGER PRIMARY KEY,
    stage              TEXT NOT NULL CHECK(stage = 'gap_fill'),
    total_messages     INTEGER,
    probe_succeeded_at INTEGER NOT NULL,
    retry_at           INTEGER,
    updated_at         INTEGER NOT NULL
) WITHOUT ROWID
"""

_DELTA_ACCESS_RECOVERY_DUE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_delta_access_recovery_due
ON delta_access_recovery_state(retry_at, probe_succeeded_at, dialog_id)
"""

_DELTA_ACCESS_RECOVERY_CLEAR_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS synced_dialogs_clear_access_recovery
AFTER UPDATE OF status ON synced_dialogs
WHEN NEW.status = 'access_lost'
BEGIN
    DELETE FROM delta_access_recovery_state WHERE dialog_id = NEW.dialog_id;
END
"""

_ENTITY_PROFILE_REFRESH_STATE_V60_DDL = """
CREATE TABLE entity_profile_refresh_state (
    entity_id          INTEGER PRIMARY KEY,
    status             TEXT NOT NULL CHECK(status IN ('failed', 'pending', 'rejected')),
    retry_at           INTEGER,
    reason             TEXT,
    updated_at         INTEGER NOT NULL,
    next_section       TEXT NOT NULL DEFAULT 'full_profile' CHECK(next_section IN (
        'full_profile', 'common_chats', 'contact_overlap', 'avatar_history', 'personal_channel'
    )),
    acquisition_cursor INTEGER NOT NULL DEFAULT 0 CHECK(acquisition_cursor >= 0)
) WITHOUT ROWID
"""

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

_OWN_ONLY_DIALOGS_DDL = """
CREATE TABLE IF NOT EXISTS own_only_dialogs (
    dialog_id       INTEGER PRIMARY KEY,
    inclusion_basis TEXT NOT NULL,
    updated_at      INTEGER NOT NULL
)
"""

# ---------------------------------------------------------------------------
# v27: scheduled-message mirror
#
# Scheduled message IDs belong to a queue-local sequence and must never share
# the sent-history table.  The table intentionally has no FTS, unread, or
# message-version companion.  Rows are retained after leaving the queue as
# evidence; only message_state='scheduled' is visible to explicit scheduled
# read paths.
# ---------------------------------------------------------------------------

_SCHEDULED_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_messages (
    dialog_id                   INTEGER NOT NULL,
    message_id                  INTEGER NOT NULL,
    scheduled_at                INTEGER,
    text                        TEXT,
    sender_id                   INTEGER,
    sender_first_name           TEXT,
    media_description           TEXT,
    reply_to_msg_id             INTEGER,
    forum_topic_id              INTEGER,
    edit_date                   INTEGER,
    grouped_id                  INTEGER,
    reply_to_peer_id            INTEGER,
    out                         INTEGER NOT NULL DEFAULT 1,
    is_service                  INTEGER NOT NULL DEFAULT 0,
    post_author                 TEXT,
    schedule_repeat_period     INTEGER,
    message_state               TEXT NOT NULL DEFAULT 'scheduled'
                                CHECK (message_state IN ('scheduled', 'unknown_missing', 'cancelled', 'published')),
    visibility                  TEXT NOT NULL DEFAULT 'author_only'
                                CHECK (visibility IN ('author_only', 'chat_visible', 'unknown')),
    unpublished                 INTEGER NOT NULL DEFAULT 1 CHECK (unpublished IN (0, 1)),
    unseen                      INTEGER NOT NULL DEFAULT 1 CHECK (unseen IN (0, 1)),
    publication_hint_message_id INTEGER,
    published_message_id        INTEGER,
    publication_verified_at     INTEGER,
    published_at                INTEGER,
    deleted_at                  INTEGER,
    first_seen_at               INTEGER NOT NULL,
    updated_at                  INTEGER NOT NULL,
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID
"""

# v28: daemon-owned Telegram event facts. Aggregate reaction counters remain in
# message_reactions; these side tables retain individual reaction observations
# and the checked/available state of outbox read-date probes independently of
# message rows, so legacy/custom read projections remain compatible.
_MESSAGE_REACTION_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS message_reaction_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_id   INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    reactor_id  INTEGER,
    emoji       TEXT NOT NULL,
    reacted_at  INTEGER,
    fetched_at  INTEGER NOT NULL
)
"""

_MESSAGE_REACTION_EVENTS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_message_reaction_events_message
ON message_reaction_events(dialog_id, message_id, fetched_at)
"""

_MESSAGE_REACTION_EVENT_STATUS_DDL = """
CREATE TABLE IF NOT EXISTS message_reaction_event_status (
    dialog_id     INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    checked_at    INTEGER NOT NULL,
    status        TEXT NOT NULL,
    returned_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID
"""

_MESSAGE_READ_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS message_read_facts (
    dialog_id  INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    read_at    INTEGER,
    checked_at INTEGER NOT NULL,
    status     TEXT NOT NULL,
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID
"""

_MESSAGE_READ_FACTS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_message_read_facts_checked
ON message_read_facts(dialog_id, checked_at)
"""

_SCHEDULED_MESSAGES_ACTIVE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_scheduled_messages_active
ON scheduled_messages(dialog_id, scheduled_at)
WHERE message_state = 'scheduled'
"""

_SCHEDULED_MESSAGES_STATE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_scheduled_messages_state_updated
ON scheduled_messages(message_state, updated_at)
"""

_SCHEDULED_MESSAGES_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS scheduled_messages_fts "
    "USING fts5(dialog_id UNINDEXED, message_id UNINDEXED, stemmed_text, "
    "tokenize='unicode61')"
)

_SCHEDULED_SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_sync_state (
    key             TEXT PRIMARY KEY,
    next_retry_at   INTEGER,
    last_snapshot_at INTEGER,
    last_error      TEXT
)
"""

_SCHEDULED_SYNC_STATE_SEED = """
INSERT OR IGNORE INTO scheduled_sync_state (key) VALUES ('account')
"""

_SCHEDULED_RECONCILIATION_STATE_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_reconciliation_state (
    dialog_id             INTEGER PRIMARY KEY,
    repair_due_at         INTEGER,
    discovery_due_at      INTEGER NOT NULL,
    dirty_since           INTEGER,
    dirty_generation      INTEGER NOT NULL DEFAULT 0 CHECK(dirty_generation >= 0),
    -- Retained for compatibility with the first unreleased v59 shape.  It is
    -- an audit timestamp, not a scheduling input.
    updated_at            INTEGER NOT NULL,
    CHECK(dirty_since IS NULL OR repair_due_at IS NOT NULL)
) WITHOUT ROWID
"""

_SCHEDULED_RECONCILIATION_REPAIR_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_scheduled_reconciliation_repair_due
ON scheduled_reconciliation_state(repair_due_at, dialog_id)
WHERE repair_due_at IS NOT NULL
"""

_SCHEDULED_RECONCILIATION_DISCOVERY_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_scheduled_reconciliation_discovery_due
ON scheduled_reconciliation_state(discovery_due_at, dialog_id)
"""

# v37: durable media-fact hydration scheduling.  The queue identity includes
# the fact kind so future hydration workers can use separate policies without
# introducing another table or a nullable discriminator.
_HYDRATION_JOBS_V37_DDL = """
CREATE TABLE IF NOT EXISTS hydration_jobs (
    kind       TEXT NOT NULL,
    dialog_id  INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    due_at     INTEGER NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, dialog_id, message_id)
) WITHOUT ROWID
"""

_HYDRATION_JOBS_V37_DUE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_hydration_jobs_due
ON hydration_jobs(due_at, kind, dialog_id, message_id)
"""

# v40: exactly two service classes. Newly observed or explicitly requested
# work is foreground; the migration-created historical remainder is backfill.
_HYDRATION_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS hydration_jobs (
    kind       TEXT NOT NULL,
    dialog_id  INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    due_at     INTEGER NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    priority   INTEGER NOT NULL DEFAULT 0 CHECK (priority IN (0, 1)),
    message_sent_at INTEGER NOT NULL DEFAULT 0,
    terminal   INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0, 1)),
    PRIMARY KEY (kind, dialog_id, message_id)
) WITHOUT ROWID
"""

_HYDRATION_JOBS_SCHEDULE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_hydration_jobs_schedule
ON hydration_jobs(kind, terminal, priority DESC, message_sent_at DESC, due_at, dialog_id, message_id)
"""

_VOICE_TRANSCRIPTION_REPAIR_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_voice_undeleted_sent
ON messages(sent_at DESC, dialog_id, message_id)
WHERE media_kind = 'voice' AND is_deleted = 0
"""

_TRANSCRIBABLE_TRANSCRIPTION_REPAIR_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_transcribable_undeleted_sent
ON messages(sent_at DESC, dialog_id, message_id)
WHERE is_deleted = 0 AND json_valid(media_payload)
  AND json_type(CASE WHEN json_valid(media_payload) THEN media_payload ELSE '{}' END) = 'object'
  AND (media_kind = 'voice'
       OR (media_kind = 'video'
           AND json_type(CASE WHEN json_valid(media_payload) THEN media_payload ELSE '{}' END, '$.round_message') = 'true'))
"""

_MEDIA_METADATA_UNRESOLVED_CONTACT_OTHER_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_media_unresolved_contact_other
ON messages(sent_at DESC, dialog_id, message_id)
WHERE is_deleted = 0 AND media_kind IN ('contact', 'other') AND media_payload = '{}'
"""

_MEDIA_METADATA_UNRESOLVED_VIDEO_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_media_unresolved_video
ON messages(sent_at DESC, dialog_id, message_id)
WHERE is_deleted = 0 AND media_kind = 'video' AND json_valid(media_payload)
  AND json_type(media_payload) = 'object'
  AND json_type(media_payload, '$.round_message') IS NULL
"""

_HYDRATION_JOBS_SEED_SQL = """
INSERT OR IGNORE INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts)
SELECT 'media_metadata', m.dialog_id, m.message_id, CAST(strftime('%s', 'now') AS INTEGER), 0
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
WHERE sd.status IN ('syncing', 'synced')
  AND m.is_deleted = 0 AND m.media_kind IN ('contact', 'other') AND m.media_payload = '{}'
"""

_TRANSCRIPTION_HYDRATION_JOBS_SEED_SQL = """
INSERT OR IGNORE INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at)
SELECT 'transcription', m.dialog_id, m.message_id, CAST(strftime('%s', 'now') AS INTEGER), 0, 0, m.sent_at
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
LEFT JOIN message_transcriptions mt ON mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id
WHERE sd.status IN ('syncing', 'synced')
  AND m.is_deleted = 0 AND m.media_kind = 'voice' AND mt.message_id IS NULL
"""


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


def load_account_cooldown_until_utc(conn: sqlite3.Connection) -> float | None:
    """Load the persisted finite account cooldown as a Unix UTC deadline."""
    row = cast(
        tuple[object | None] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key = ?", (_ACCOUNT_COOLDOWN_UNTIL_UTC_KEY,)).fetchone(),
    )
    if row is None or row[0] is None:
        return None
    try:
        deadline = float(cast(str | bytes | int | float, row[0]))
    except TypeError, ValueError:
        logger.warning("invalid persisted Telegram account cooldown deadline")
        return None
    if not math.isfinite(deadline) or deadline < 0:
        logger.warning("invalid persisted Telegram account cooldown deadline")
        return None
    return deadline


def save_account_cooldown_until_utc(conn: sqlite3.Connection, deadline_utc: float | None) -> None:
    """Atomically replace or clear the finite account cooldown UTC deadline."""
    if deadline_utc is None:
        with conn:
            conn.execute("DELETE FROM daemon_state WHERE key = ?", (_ACCOUNT_COOLDOWN_UNTIL_UTC_KEY,))
        return
    if (
        isinstance(deadline_utc, bool)
        or not isinstance(deadline_utc, (int, float))
        or not math.isfinite(float(deadline_utc))
        or deadline_utc < 0
    ):
        raise ValueError("deadline_utc must be a finite non-negative Unix timestamp or None")
    with conn:
        conn.execute(
            "INSERT INTO daemon_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_ACCOUNT_COOLDOWN_UNTIL_UTC_KEY, repr(float(deadline_utc))),
        )


def load_self_profile_last_success_at(conn: sqlite3.Connection) -> float | None:
    """Load the last successful account self-profile refresh epoch."""
    row = cast(
        tuple[object | None] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key = ?", (_SELF_PROFILE_LAST_SUCCESS_AT_KEY,)).fetchone(),
    )
    if row is None or row[0] is None:
        return None
    try:
        completed_at = float(cast(str | bytes | int | float, row[0]))
    except TypeError, ValueError:
        logger.warning("invalid persisted self-profile success timestamp")
        return None
    if not math.isfinite(completed_at) or completed_at < 0:
        logger.warning("invalid persisted self-profile success timestamp")
        return None
    return completed_at


def save_self_profile_last_success_at(conn: sqlite3.Connection, completed_at: float) -> None:
    """Atomically persist a finite account self-profile refresh epoch."""
    if (
        isinstance(completed_at, bool)
        or not isinstance(completed_at, (int, float))
        or not math.isfinite(float(completed_at))
        or completed_at < 0
    ):
        raise ValueError("completed_at must be a finite non-negative Unix timestamp")
    with conn:
        conn.execute(
            "INSERT INTO daemon_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_SELF_PROFILE_LAST_SUCCESS_AT_KEY, repr(float(completed_at))),
        )


# ---------------------------------------------------------------------------
# Schema migration helpers
# ---------------------------------------------------------------------------


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """Return True if sync.db schema is at current version and WAL mode is active."""
    row = cast(tuple[object | None, ...] | None, conn.execute("PRAGMA journal_mode").fetchone())
    if row is None or str(row[0]).lower() != "wal":
        return False
    try:
        row = cast(
            tuple[object | None, ...] | None,
            conn.execute("SELECT MAX(version), COUNT(DISTINCT version) FROM schema_version").fetchone(),
        )
        return bool(
            row and _row_first_int(row) >= _CURRENT_SCHEMA_VERSION and int(cast(int, row[1])) == _CURRENT_SCHEMA_VERSION
        )
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
            "INSERT OR IGNORE INTO schema_version VALUES (?, strftime('%s', 'now'))",
            (version,),
        )
        conn.commit()
        return version
    except Exception:
        conn.rollback()
        logger.exception("sync_db migration to version %d failed", version)
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
            (
                "CREATE INDEX IF NOT EXISTS idx_synced_dialogs_status_read_position "
                "ON synced_dialogs(status, read_inbox_max_id)"
            ),
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
            (
                "CREATE TABLE IF NOT EXISTS message_reactions_freshness ("
                "    dialog_id INTEGER NOT NULL, "
                "    message_id INTEGER NOT NULL, "
                "    checked_at INTEGER NOT NULL, "
                "    PRIMARY KEY (dialog_id, message_id)"
                ") WITHOUT ROWID"
            ),
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
    # Telegram-authoritative unread facts are added by v33; Phase 41 bootstrap
    # and Phase 43 reconciliation populate them, while raw event handlers keep
    # them current. NULL remains the unknown state.
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


def _apply_migrations_21_to_28(conn: sqlite3.Connection, current: int) -> int:
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
    current = _apply_migration(
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
            (
                "UPDATE message_forwards SET fwd_from_peer_id = -1000000000000 - fwd_from_peer_id "
                "WHERE fwd_from_peer_id > 0 AND EXISTS (SELECT 1 FROM dialogs d "
                "WHERE d.dialog_id = -1000000000000 - message_forwards.fwd_from_peer_id)"
            ),
            # legacy chat: bare -> -bare when -bare is a known dialog
            (
                "UPDATE message_forwards SET fwd_from_peer_id = -fwd_from_peer_id "
                "WHERE fwd_from_peer_id > 0 AND EXISTS (SELECT 1 FROM dialogs d "
                "WHERE d.dialog_id = -message_forwards.fwd_from_peer_id)"
            ),
        ],
    )

    # v27: scheduled-message mirror. Scheduled messages are queue-local,
    # mutable future objects; keeping them out of messages/FTS/unread avoids
    # presenting unpublished content as sent history. The sync-state row stores
    # account-level FloodWait backoff for snapshot reconciliation.
    current = _apply_migration(
        conn,
        current,
        27,
        [
            _SCHEDULED_MESSAGES_DDL,
            _SCHEDULED_MESSAGES_ACTIVE_INDEX_DDL,
            _SCHEDULED_MESSAGES_STATE_INDEX_DDL,
            _SCHEDULED_MESSAGES_FTS_DDL,
            _SCHEDULED_SYNC_STATE_DDL,
            _SCHEDULED_SYNC_STATE_SEED,
        ],
    )

    # v28: individual reaction events and outbox read-date probe cache. Event
    # availability is explicit in companion status/read-fact rows; NULL event
    # timestamps mean Telegram omitted the date, never a local fallback.
    return _apply_migration(
        conn,
        current,
        28,
        [
            _MESSAGE_REACTION_EVENTS_DDL,
            _MESSAGE_REACTION_EVENTS_INDEX_DDL,
            _MESSAGE_REACTION_EVENT_STATUS_DDL,
            _MESSAGE_READ_FACTS_DDL,
            _MESSAGE_READ_FACTS_INDEX_DDL,
        ],
    )


def _apply_migration_29(conn: sqlite3.Connection, current: int) -> int:
    """Create the minimal many-to-many Telegram custom-folder snapshot."""
    return _apply_migration(
        conn,
        current,
        29,
        [
            """CREATE TABLE IF NOT EXISTS telegram_folders (
    folder_id INTEGER PRIMARY KEY,
    title     TEXT NOT NULL
)""",
            """CREATE TABLE IF NOT EXISTS telegram_folder_members (
    folder_id INTEGER NOT NULL,
    dialog_id INTEGER NOT NULL,
    PRIMARY KEY (folder_id, dialog_id),
    FOREIGN KEY (folder_id) REFERENCES telegram_folders(folder_id) ON DELETE CASCADE
) WITHOUT ROWID""",
            """CREATE INDEX IF NOT EXISTS idx_telegram_folder_members_dialog
ON telegram_folder_members(dialog_id, folder_id)""",
        ],
    )


def _apply_migration_30(conn: sqlite3.Connection, current: int) -> int:
    """Track local delta-probe recency and explicit refresh requests.

    These are synchronizer timestamps, not Telegram event timestamps. They are
    used to make bounded catch-up fair and to let mark_dialog_for_sync request a
    refresh for an already-synced dialog without forcing a full re-sync.
    """
    return _apply_migration(
        conn,
        current,
        30,
        [
            "ALTER TABLE synced_dialogs ADD COLUMN last_synced_at INTEGER",
            "ALTER TABLE synced_dialogs ADD COLUMN last_event_at INTEGER",
            "ALTER TABLE synced_dialogs ADD COLUMN last_delta_checked_at INTEGER",
            "ALTER TABLE synced_dialogs ADD COLUMN delta_refresh_requested_at INTEGER",
            (
                "CREATE INDEX IF NOT EXISTS idx_synced_dialogs_delta_fairness "
                "ON synced_dialogs(status, delta_refresh_requested_at, last_delta_checked_at, last_synced_at)"
            ),
        ],
        ignore_duplicate_column=True,
    )


def _apply_migration_31(conn: sqlite3.Connection, current: int) -> int:
    """Track cold access-lost revalidation pacing and daemon event journal.

    These timestamps are synchronizer metadata, not Telegram event facts. They
    prevent access-lost archives from becoming a one-way latch while keeping
    recovery probes out of hot sync paths. ``daemon_events`` is an append-only
    local journal for important daemon-observed lifecycle events; it is not a
    Telegram update log.
    """
    return _apply_migration(
        conn,
        current,
        31,
        [
            "ALTER TABLE synced_dialogs ADD COLUMN access_lost_at INTEGER",
            "ALTER TABLE synced_dialogs ADD COLUMN access_last_revalidated_at INTEGER",
            "ALTER TABLE synced_dialogs ADD COLUMN access_next_revalidate_at INTEGER",
            (
                "CREATE INDEX IF NOT EXISTS idx_synced_dialogs_access_revalidate "
                "ON synced_dialogs(status, access_next_revalidate_at, access_lost_at)"
            ),
            _DAEMON_EVENTS_DDL,
            _DAEMON_EVENTS_INDEX_DDL,
        ],
        ignore_duplicate_column=True,
    )


def _apply_migration_32(conn: sqlite3.Connection, current: int) -> int:
    """Persist agent-readable topic icon facts from Telegram."""
    return _apply_migration(
        conn,
        current,
        32,
        [
            _TOPIC_TABLE_DDL,
            "ALTER TABLE topic_metadata ADD COLUMN icon_emoji TEXT",
            "ALTER TABLE topic_metadata ADD COLUMN icon_color INTEGER",
        ],
        ignore_duplicate_column=True,
    )


def _apply_migration_33(conn: sqlite3.Connection, current: int) -> int:
    """Persist nullable, Telegram-authoritative unread facts on dialog snapshots."""
    return _apply_migration(
        conn,
        current,
        33,
        [
            "ALTER TABLE dialogs ADD COLUMN unread_count INTEGER",
            "ALTER TABLE dialogs ADD COLUMN unread_mark INTEGER",
            "ALTER TABLE dialogs ADD COLUMN unread_count_observed_at INTEGER",
            "ALTER TABLE dialogs ADD COLUMN unread_mark_observed_at INTEGER",
        ],
        ignore_duplicate_column=True,
    )


def _apply_migration_34(conn: sqlite3.Connection, current: int) -> int:
    """Separate durable full-history intent from observed coverage status."""
    return _apply_migration(
        conn,
        current,
        34,
        [
            _FULL_HISTORY_ENROLLMENT_DDL,
            _FULL_HISTORY_ENROLLMENT_ENABLED_INDEX_DDL,
            """INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at)
               SELECT dialog_id,
                      CASE WHEN status IN ('syncing', 'synced') THEN 1 ELSE 0 END,
                      'migration',
                      strftime('%s', 'now')
               FROM synced_dialogs
               WHERE status IN ('not_synced', 'own_only', 'fragment', 'syncing', 'synced', 'access_lost')
               ON CONFLICT(dialog_id) DO NOTHING""",
        ],
    )


def _apply_migration_35(conn: sqlite3.Connection, current: int) -> int:
    """Persist fair, durable read-position retry pacing metadata.

    These are synchronizer timestamps, not Telegram event times. A due
    timestamp lets bounded reconciliation defer an unresolved peer or a
    Telegram response with NULL cursors without starving later dialogs.
    """
    return _apply_migration(
        conn,
        current,
        35,
        [
            "ALTER TABLE synced_dialogs ADD COLUMN read_position_next_attempt_at INTEGER",
            "ALTER TABLE synced_dialogs ADD COLUMN read_position_attempt_count INTEGER NOT NULL DEFAULT 0",
            (
                "CREATE INDEX IF NOT EXISTS idx_synced_dialogs_read_position_retry "
                "ON synced_dialogs(status, read_position_next_attempt_at, read_position_attempt_count, dialog_id)"
            ),
        ],
        ignore_duplicate_column=True,
    )


def _apply_migration_36(conn: sqlite3.Connection, current: int) -> int:
    """Store the generic media discriminator separately from its description."""
    return _apply_migration(
        conn,
        current,
        36,
        [
            "ALTER TABLE messages ADD COLUMN media_kind TEXT CHECK (media_kind IN ('contact', 'other'))",
            "ALTER TABLE scheduled_messages ADD COLUMN media_kind TEXT CHECK (media_kind IN ('contact', 'other'))",
        ],
        ignore_duplicate_column=True,
    )


_MEDIA_KIND_CHECK = "'photo', 'video', 'audio', 'voice', 'document', 'animation', 'sticker', 'custom_emoji', 'poll', 'location', 'venue', 'contact', 'link_preview', 'game', 'invoice', 'dice', 'story', 'other'"


def _apply_migration_37(conn: sqlite3.Connection, current: int) -> int:
    """Replace presentation descriptions with normalized media facts.

    SQLite cannot drop/reorder columns in place.  Rebuilding both composite
    ``WITHOUT ROWID`` tables in this one migration keeps the operation atomic,
    preserves every non-FTS row, and leaves FTS tables untouched.  Historical
    descriptions are deliberately not parsed: only the v36 discriminator is
    trusted, and all other legacy media becomes ``other/{}``.
    """
    # Capture every ordinary index definition before the source tables are
    # dropped.  This includes operator-created indexes in addition to the
    # project's canonical indexes; FTS virtual-table indexes are excluded.
    index_rows = cast(
        list[tuple[str, str]],
        conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NOT NULL "
            "AND tbl_name IN ('messages', 'scheduled_messages')"
        ).fetchall(),
    )

    # Indexes whose key used the removed presentation column cannot survive
    # the new physical schema.  Keep all unrelated (including custom) indexes
    # and intentionally discard only those obsolete definitions.
    index_stmts = [sql for _name, sql in index_rows if "media_description" not in sql.lower()]
    return _apply_migration(
        conn,
        current,
        37,
        [
            "ALTER TABLE messages RENAME TO messages_v36",
            f"""CREATE TABLE messages_v37 (
    dialog_id           INTEGER NOT NULL,
    message_id          INTEGER NOT NULL,
    sent_at             INTEGER NOT NULL,
    text                TEXT,
    sender_id           INTEGER,
    sender_first_name   TEXT,
    media_kind          TEXT CHECK (media_kind IN ({_MEDIA_KIND_CHECK})),
    media_payload       TEXT CHECK (media_payload IS NULL OR (json_valid(media_payload) AND json_type(media_payload) = 'object')),
    reply_to_msg_id     INTEGER,
    forum_topic_id      INTEGER,
    edit_date           INTEGER,
    grouped_id          INTEGER,
    reply_to_peer_id    INTEGER,
    out                 INTEGER NOT NULL DEFAULT 0,
    is_service          INTEGER NOT NULL DEFAULT 0,
    post_author         TEXT,
    reply_count         INTEGER NOT NULL DEFAULT 0,
    is_deleted          INTEGER NOT NULL DEFAULT 0,
    deleted_at          INTEGER,
    PRIMARY KEY (dialog_id, message_id),
    CHECK ((media_kind IS NULL AND media_payload IS NULL) OR (media_kind IS NOT NULL AND media_payload IS NOT NULL))
) WITHOUT ROWID""",
            """INSERT INTO messages_v37 (
    dialog_id, message_id, sent_at, text, sender_id, sender_first_name,
    media_kind, media_payload, reply_to_msg_id, forum_topic_id, edit_date,
    grouped_id, reply_to_peer_id, out, is_service, post_author, reply_count,
    is_deleted, deleted_at
)
SELECT dialog_id, message_id, sent_at, text, sender_id, sender_first_name,
       CASE
         WHEN media_kind = 'contact' THEN 'contact'
         WHEN media_kind IS NULL AND media_description IS NULL THEN NULL
         ELSE 'other'
       END,
       CASE
         WHEN media_kind IS NULL AND media_description IS NULL THEN NULL
         ELSE '{}'
       END,
       reply_to_msg_id, forum_topic_id, edit_date, grouped_id, reply_to_peer_id,
       out, is_service, post_author, reply_count, is_deleted, deleted_at
FROM messages_v36""",
            "DROP TABLE messages_v36",
            "ALTER TABLE messages_v37 RENAME TO messages",
            "ALTER TABLE scheduled_messages RENAME TO scheduled_messages_v36",
            f"""CREATE TABLE scheduled_messages_v37 (
    dialog_id                   INTEGER NOT NULL,
    message_id                  INTEGER NOT NULL,
    scheduled_at                INTEGER,
    text                        TEXT,
    sender_id                   INTEGER,
    sender_first_name           TEXT,
    media_kind                  TEXT CHECK (media_kind IN ({_MEDIA_KIND_CHECK})),
    media_payload               TEXT CHECK (media_payload IS NULL OR (json_valid(media_payload) AND json_type(media_payload) = 'object')),
    reply_to_msg_id             INTEGER,
    forum_topic_id              INTEGER,
    edit_date                   INTEGER,
    grouped_id                  INTEGER,
    reply_to_peer_id            INTEGER,
    out                         INTEGER NOT NULL DEFAULT 1,
    is_service                  INTEGER NOT NULL DEFAULT 0,
    post_author                 TEXT,
    schedule_repeat_period     INTEGER,
    message_state               TEXT NOT NULL DEFAULT 'scheduled' CHECK (message_state IN ('scheduled', 'unknown_missing', 'cancelled', 'published')),
    visibility                  TEXT NOT NULL DEFAULT 'author_only' CHECK (visibility IN ('author_only', 'chat_visible', 'unknown')),
    unpublished                 INTEGER NOT NULL DEFAULT 1 CHECK (unpublished IN (0, 1)),
    unseen                      INTEGER NOT NULL DEFAULT 1 CHECK (unseen IN (0, 1)),
    publication_hint_message_id INTEGER,
    published_message_id        INTEGER,
    publication_verified_at     INTEGER,
    published_at                INTEGER,
    deleted_at                  INTEGER,
    first_seen_at               INTEGER NOT NULL,
    updated_at                  INTEGER NOT NULL,
    PRIMARY KEY (dialog_id, message_id),
    CHECK ((media_kind IS NULL AND media_payload IS NULL) OR (media_kind IS NOT NULL AND media_payload IS NOT NULL))
) WITHOUT ROWID""",
            """INSERT INTO scheduled_messages_v37 (
    dialog_id, message_id, scheduled_at, text, sender_id, sender_first_name,
    media_kind, media_payload, reply_to_msg_id, forum_topic_id, edit_date,
    grouped_id, reply_to_peer_id, out, is_service, post_author,
    schedule_repeat_period, message_state, visibility, unpublished, unseen,
    publication_hint_message_id, published_message_id, publication_verified_at,
    published_at, deleted_at, first_seen_at, updated_at
)
SELECT dialog_id, message_id, scheduled_at, text, sender_id, sender_first_name,
       CASE
         WHEN media_kind = 'contact' THEN 'contact'
         WHEN media_kind IS NULL AND media_description IS NULL THEN NULL
         ELSE 'other'
       END,
       CASE
         WHEN media_kind IS NULL AND media_description IS NULL THEN NULL
         ELSE '{}'
       END,
       reply_to_msg_id, forum_topic_id, edit_date, grouped_id, reply_to_peer_id,
       out, is_service, post_author, schedule_repeat_period, message_state,
       visibility, unpublished, unseen, publication_hint_message_id,
       published_message_id, publication_verified_at, published_at, deleted_at,
       first_seen_at, updated_at
FROM scheduled_messages_v36""",
            "DROP TABLE scheduled_messages_v36",
            "ALTER TABLE scheduled_messages_v37 RENAME TO scheduled_messages",
            *index_stmts,
            _HYDRATION_JOBS_V37_DDL,
            _HYDRATION_JOBS_V37_DUE_INDEX_DDL,
            _HYDRATION_JOBS_SEED_SQL,
        ],
    )


def _apply_migration_38(conn: sqlite3.Connection, current: int) -> int:
    """Persist final Telegram transcription facts without historical backfill."""
    return _apply_migration(conn, current, 38, [_MESSAGE_TRANSCRIPTIONS_DDL])


def _apply_migration_39(conn: sqlite3.Connection, current: int) -> int:
    """Add durable adaptive HotSweep cadence state."""
    return _apply_migration(
        conn,
        current,
        39,
        [
            "ALTER TABLE activity_dialog_state ADD COLUMN hot_next_due_at INTEGER",
            "ALTER TABLE activity_dialog_state ADD COLUMN hot_empty_streak INTEGER NOT NULL DEFAULT 0 CHECK (hot_empty_streak >= 0)",
            (
                "CREATE INDEX IF NOT EXISTS idx_activity_dialog_state_hot_due "
                "ON activity_dialog_state(hot_next_due_at, dialog_id, hot_next_retry_at)"
            ),
        ],
    )


def _apply_migration_40(conn: sqlite3.Connection, current: int) -> int:
    """Prioritize foreground hydration ahead of historical backfill."""
    return _apply_migration(
        conn,
        current,
        40,
        [
            "DROP INDEX IF EXISTS idx_hydration_jobs_due",
            ("ALTER TABLE hydration_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0 CHECK (priority IN (0, 1))"),
            "ALTER TABLE hydration_jobs ADD COLUMN message_sent_at INTEGER NOT NULL DEFAULT 0",
            (
                "UPDATE hydration_jobs SET message_sent_at = COALESCE(("
                "SELECT sent_at FROM messages WHERE messages.dialog_id = hydration_jobs.dialog_id "
                "AND messages.message_id = hydration_jobs.message_id), 0)"
            ),
        ],
        ignore_duplicate_column=True,
    )


def _apply_migration_41(conn: sqlite3.Connection, current: int) -> int:
    """Seed low-priority transcription hydration for existing voice messages."""
    return _apply_migration(conn, current, 41, [_TRANSCRIPTION_HYDRATION_JOBS_SEED_SQL])


def _apply_migration_42(conn: sqlite3.Connection, current: int) -> int:
    """Make failed transcription work terminal and index voice repair scans."""
    return _apply_migration(
        conn,
        current,
        42,
        [
            "ALTER TABLE hydration_jobs ADD COLUMN terminal INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0, 1))",
            "DROP INDEX IF EXISTS idx_hydration_jobs_due",
            _VOICE_TRANSCRIPTION_REPAIR_INDEX_DDL,
            _HYDRATION_JOBS_SCHEDULE_INDEX_DDL,
        ],
        ignore_duplicate_column=True,
    )


def _apply_migration_43(conn: sqlite3.Connection, current: int) -> int:
    """Index bounded repair candidates for unresolved media metadata."""
    return _apply_migration(
        conn,
        current,
        43,
        [
            _MEDIA_METADATA_UNRESOLVED_CONTACT_OTHER_INDEX_DDL,
            _MEDIA_METADATA_UNRESOLVED_VIDEO_INDEX_DDL,
        ],
    )


def _apply_migration_44(conn: sqlite3.Connection, current: int) -> int:
    """Allow custom-emoji facts in the canonical message tables.

    SQLite cannot alter a CHECK constraint in place. Rebuild only the two
    media-bearing tables, carrying every row across and recreating all
    ordinary indexes captured before the swap. FTS tables and hydration jobs
    are separate tables and are intentionally left untouched.
    """
    index_rows = cast(
        list[tuple[str, str]],
        conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NOT NULL "
            "AND tbl_name IN ('messages', 'scheduled_messages')"
        ).fetchall(),
    )
    index_stmts = [sql for _name, sql in index_rows]
    return _apply_migration(
        conn,
        current,
        44,
        [
            "ALTER TABLE messages RENAME TO messages_v43",
            f"""CREATE TABLE messages_v44 (
    dialog_id           INTEGER NOT NULL,
    message_id          INTEGER NOT NULL,
    sent_at             INTEGER NOT NULL,
    text                TEXT,
    sender_id           INTEGER,
    sender_first_name   TEXT,
    media_kind          TEXT CHECK (media_kind IN ({_MEDIA_KIND_CHECK})),
    media_payload       TEXT CHECK (media_payload IS NULL OR (json_valid(media_payload) AND json_type(media_payload) = 'object')),
    reply_to_msg_id     INTEGER,
    forum_topic_id      INTEGER,
    edit_date           INTEGER,
    grouped_id          INTEGER,
    reply_to_peer_id    INTEGER,
    out                 INTEGER NOT NULL DEFAULT 0,
    is_service          INTEGER NOT NULL DEFAULT 0,
    post_author         TEXT,
    reply_count         INTEGER NOT NULL DEFAULT 0,
    is_deleted          INTEGER NOT NULL DEFAULT 0,
    deleted_at          INTEGER,
    PRIMARY KEY (dialog_id, message_id),
    CHECK ((media_kind IS NULL AND media_payload IS NULL) OR (media_kind IS NOT NULL AND media_payload IS NOT NULL))
) WITHOUT ROWID""",
            """INSERT INTO messages_v44 (
    dialog_id, message_id, sent_at, text, sender_id, sender_first_name,
    media_kind, media_payload, reply_to_msg_id, forum_topic_id, edit_date,
    grouped_id, reply_to_peer_id, out, is_service, post_author, reply_count,
    is_deleted, deleted_at
)
SELECT dialog_id, message_id, sent_at, text, sender_id, sender_first_name,
       media_kind, media_payload, reply_to_msg_id, forum_topic_id, edit_date,
       grouped_id, reply_to_peer_id, out, is_service, post_author, reply_count,
       is_deleted, deleted_at
FROM messages_v43""",
            "DROP TABLE messages_v43",
            "ALTER TABLE messages_v44 RENAME TO messages",
            "ALTER TABLE scheduled_messages RENAME TO scheduled_messages_v43",
            f"""CREATE TABLE scheduled_messages_v44 (
    dialog_id                   INTEGER NOT NULL,
    message_id                  INTEGER NOT NULL,
    scheduled_at                INTEGER,
    text                        TEXT,
    sender_id                   INTEGER,
    sender_first_name           TEXT,
    media_kind                  TEXT CHECK (media_kind IN ({_MEDIA_KIND_CHECK})),
    media_payload               TEXT CHECK (media_payload IS NULL OR (json_valid(media_payload) AND json_type(media_payload) = 'object')),
    reply_to_msg_id             INTEGER,
    forum_topic_id              INTEGER,
    edit_date                   INTEGER,
    grouped_id                  INTEGER,
    reply_to_peer_id            INTEGER,
    out                         INTEGER NOT NULL DEFAULT 1,
    is_service                  INTEGER NOT NULL DEFAULT 0,
    post_author                 TEXT,
    schedule_repeat_period     INTEGER,
    message_state               TEXT NOT NULL DEFAULT 'scheduled' CHECK (message_state IN ('scheduled', 'unknown_missing', 'cancelled', 'published')),
    visibility                  TEXT NOT NULL DEFAULT 'author_only' CHECK (visibility IN ('author_only', 'chat_visible', 'unknown')),
    unpublished                 INTEGER NOT NULL DEFAULT 1 CHECK (unpublished IN (0, 1)),
    unseen                      INTEGER NOT NULL DEFAULT 1 CHECK (unseen IN (0, 1)),
    publication_hint_message_id INTEGER,
    published_message_id        INTEGER,
    publication_verified_at    INTEGER,
    published_at                INTEGER,
    deleted_at                  INTEGER,
    first_seen_at               INTEGER NOT NULL,
    updated_at                  INTEGER NOT NULL,
    PRIMARY KEY (dialog_id, message_id),
    CHECK ((media_kind IS NULL AND media_payload IS NULL) OR (media_kind IS NOT NULL AND media_payload IS NOT NULL))
) WITHOUT ROWID""",
            """INSERT INTO scheduled_messages_v44 (
    dialog_id, message_id, scheduled_at, text, sender_id, sender_first_name,
    media_kind, media_payload, reply_to_msg_id, forum_topic_id, edit_date,
    grouped_id, reply_to_peer_id, out, is_service, post_author,
    schedule_repeat_period, message_state, visibility, unpublished, unseen,
    publication_hint_message_id, published_message_id, publication_verified_at,
    published_at, deleted_at, first_seen_at, updated_at
)
SELECT dialog_id, message_id, scheduled_at, text, sender_id, sender_first_name,
       media_kind, media_payload, reply_to_msg_id, forum_topic_id, edit_date,
       grouped_id, reply_to_peer_id, out, is_service, post_author,
       schedule_repeat_period, message_state, visibility, unpublished, unseen,
       publication_hint_message_id, published_message_id, publication_verified_at,
       published_at, deleted_at, first_seen_at, updated_at
FROM scheduled_messages_v43""",
            "DROP TABLE scheduled_messages_v43",
            "ALTER TABLE scheduled_messages_v44 RENAME TO scheduled_messages",
            *index_stmts,
        ],
    )


def _apply_migration_45(conn: sqlite3.Connection, current: int) -> int:
    """Index all canonical undeleted media eligible for transcription."""
    return _apply_migration(
        conn,
        current,
        45,
        [
            _TRANSCRIBABLE_TRANSCRIPTION_REPAIR_INDEX_DDL,
            "DROP INDEX IF EXISTS idx_messages_voice_undeleted_sent",
        ],
    )


def _apply_migration_46(conn: sqlite3.Connection, current: int) -> int:
    """Persist the last privacy-safe hydration outcome for operations."""
    return _apply_migration(
        conn,
        current,
        46,
        [
            "ALTER TABLE hydration_jobs ADD COLUMN last_outcome TEXT NOT NULL DEFAULT 'queued'",
            "ALTER TABLE hydration_jobs ADD COLUMN last_error_code TEXT",
            (
                "UPDATE hydration_jobs SET last_outcome = CASE "
                "WHEN terminal = 1 THEN 'terminal_unknown' "
                "WHEN attempts > 0 THEN 'deferred_unknown' ELSE 'queued' END"
            ),
        ],
        ignore_duplicate_column=True,
    )


def _apply_migration_47(conn: sqlite3.Connection, current: int) -> int:
    """Add the boundary outcome and safe machine error code fields."""
    existing_table = (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'telemetry_events'").fetchone()
        is not None
    )
    existing_columns = (
        {
            str(row[1])
            for row in cast(
                list[tuple[object, ...]],
                conn.execute("PRAGMA table_info(telemetry_events)").fetchall(),
            )
        }
        if existing_table
        else set()
    )
    outcome_added = "outcome" not in existing_columns
    error_code_added = "error_code" not in existing_columns
    statements = []
    if not existing_table:
        statements.extend([_TELEMETRY_EVENTS_BASE_DDL, _TELEMETRY_EVENTS_INDEX_DDL])
    else:
        statements.append(_TELEMETRY_EVENTS_INDEX_DDL)
    if outcome_added:
        statements.append("ALTER TABLE telemetry_events ADD COLUMN outcome TEXT NOT NULL DEFAULT 'success'")
    if error_code_added:
        statements.append("ALTER TABLE telemetry_events ADD COLUMN error_code TEXT")
    # Keep these updates on every v47 retry. A process can crash after either
    # ALTER and before the backfill while schema_version is still 46.
    statements.extend(
        [
            ("UPDATE telemetry_events SET outcome = 'exception' WHERE outcome = 'success' AND error_type IS NOT NULL"),
            (
                "UPDATE telemetry_events SET error_code = 'exception' "
                "WHERE error_code IS NULL AND error_type IS NOT NULL"
            ),
        ]
    )
    return _apply_migration(
        conn,
        current,
        47,
        statements,
        ignore_duplicate_column=True,
    )


def _apply_migration_48(conn: sqlite3.Connection, current: int) -> int:
    """Create the immutable observed-order alert projection and its writers."""
    alert_column_rows = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(sync_alert_events)").fetchall())
    alert_columns = {str(row[1]) for row in alert_column_rows}
    runtime_exists = cast(
        tuple[int] | None,
        conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_events'").fetchone(),
    )
    if runtime_exists is not None and alert_columns and "daemon_event_id" not in alert_columns:
        return _apply_migration(conn, current, 48, [])
    return _apply_migration(
        conn,
        current,
        48,
        [
            _DAEMON_EVENTS_DDL,
            _MESSAGE_VERSIONS_DDL,
            _SYNC_ALERT_EVENTS_DDL,
            _SYNC_ALERT_DELETED_INDEX_DDL,
            _SYNC_ALERT_EDIT_INDEX_DDL,
            _SYNC_ALERT_ACCESS_INDEX_DDL,
            """INSERT INTO daemon_events(kind, dialog_id, occurred_at, payload_json)
               SELECT 'access_lost', sd.dialog_id, sd.access_lost_at, '{}'
                 FROM synced_dialogs sd
                WHERE sd.status = 'access_lost'
                  AND sd.access_lost_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM daemon_events de
                       WHERE de.kind = 'access_lost'
                         AND de.dialog_id = sd.dialog_id
                         AND de.occurred_at = sd.access_lost_at
                  )""",
            """INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, message_id, version, daemon_event_id)
               SELECT c.kind, c.occurred_at, c.dialog_id, c.message_id, c.version, c.daemon_event_id
                 FROM (
                       SELECT 'deleted_message' AS kind, m.deleted_at AS occurred_at,
                              m.dialog_id, m.message_id, NULL AS version, NULL AS daemon_event_id,
                              1 AS kind_rank
                         FROM messages m
                        WHERE m.is_deleted = 1 AND m.deleted_at IS NOT NULL
                       UNION ALL
                       SELECT 'edit', mv.edit_date, mv.dialog_id, mv.message_id, mv.version, NULL, 2
                         FROM message_versions mv
                        WHERE mv.edit_date IS NOT NULL
                       UNION ALL
                       SELECT 'access_lost', de.occurred_at, de.dialog_id, NULL, NULL, de.id, 3
                         FROM daemon_events de
                        WHERE de.kind = 'access_lost' AND de.dialog_id IS NOT NULL
                 ) c
                WHERE NOT EXISTS (
                      SELECT 1 FROM sync_alert_events sae
                       WHERE (c.kind = 'deleted_message' AND sae.kind = c.kind
                              AND sae.dialog_id = c.dialog_id AND sae.message_id = c.message_id)
                          OR (c.kind = 'edit' AND sae.kind = c.kind
                              AND sae.dialog_id = c.dialog_id AND sae.message_id = c.message_id
                              AND sae.version = c.version)
                          OR (c.kind = 'access_lost' AND sae.kind = c.kind
                              AND sae.daemon_event_id = c.daemon_event_id)
                  )
                ORDER BY c.occurred_at, c.kind_rank, c.dialog_id,
                         COALESCE(c.message_id, 0), COALESCE(c.version, 0), COALESCE(c.daemon_event_id, 0)""",
            _SYNC_ALERT_DELETED_INSERT_TRIGGER,
            _SYNC_ALERT_DELETED_UPDATE_TRIGGER,
            _SYNC_ALERT_EDIT_TRIGGER,
            _SYNC_ALERT_ACCESS_TRIGGER,
        ],
    )


def _apply_migration_49(conn: sqlite3.Connection, current: int) -> int:
    """Index the bounded authored-message activity feed."""
    return _apply_migration(conn, current, 49, [_MESSAGES_OWN_ACTIVITY_SENT_INDEX_DDL])


def _apply_migration_50(conn: sqlite3.Connection, current: int) -> int:
    """Keep list-dialog aggregate reads inside a compact covering index."""
    return _apply_migration(conn, current, 50, [_MESSAGES_DIALOG_SUMMARY_INDEX_DDL])


def _sequence_high_water(conn: sqlite3.Connection, table: str, id_column: str) -> int:
    row = cast(
        tuple[int | None] | None,
        conn.execute(
            f"SELECT MAX(value) FROM (SELECT COALESCE(MAX({id_column}), 0) AS value FROM {table} "
            "UNION ALL SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = ?), 0))",
            (table,),
        ).fetchone(),
    )
    return int(row[0]) if row and row[0] is not None else 0


def _rebuild_message_versions(conn: sqlite3.Connection, origin_expression: str) -> None:
    """Rebuild the version table when its origin CHECK/default is unsafe."""
    conn.execute("ALTER TABLE message_versions RENAME TO message_versions_origin_migration")
    conn.execute(_MESSAGE_VERSIONS_DDL)
    conn.execute(
        f"""INSERT INTO message_versions
           (dialog_id, message_id, version, old_text, edit_date, origin)
           SELECT dialog_id, message_id, version, old_text, edit_date, {origin_expression}
             FROM message_versions_origin_migration"""
    )
    conn.execute("DROP TABLE message_versions_origin_migration")


def _message_versions_has_safe_origin_contract(conn: sqlite3.Connection) -> bool:
    row = cast(
        tuple[str | None] | None,
        conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'message_versions'").fetchone(),
    )
    sql = (row[0] or "").lower() if row is not None else ""
    return "legacy_unknown" in sql


def _legacy_alert_projection_v51(conn: sqlite3.Connection) -> None:
    """Copy only defensible v50 alerts into the replacement projection."""
    tables = {
        str(row[0])
        for row in cast(
            list[tuple[object]], conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        )
    }
    if {"entities", "dialogs", "messages", "message_versions"} <= tables:
        transcription_filter = ""
        if "message_transcriptions" in tables:
            transcription_filter = """AND NOT EXISTS (
                       SELECT 1 FROM message_transcriptions mt
                        WHERE mt.dialog_id = a.dialog_id
                          AND mt.message_id = a.message_id
                          AND mt.received_at = a.occurred_at
                   )"""
        conn.execute(
            f"""WITH candidates(source_seq, kind, occurred_at, dialog_id, message_id, version) AS (
                    SELECT MIN(a.seq), 'deleted_message', MIN(a.occurred_at), a.dialog_id, a.message_id, NULL
                      FROM sync_alert_events a
                      JOIN messages m ON m.dialog_id = a.dialog_id AND m.message_id = a.message_id
                     WHERE a.kind = 'deleted_message'
                       AND {_LEGACY_HUMAN_DM_ALERT_PREDICATE}
                     GROUP BY a.dialog_id, a.message_id
                    UNION ALL
                    SELECT MIN(a.seq), 'edit', MIN(a.occurred_at), a.dialog_id, a.message_id, a.version
                      FROM sync_alert_events a
                      JOIN messages m ON m.dialog_id = a.dialog_id AND m.message_id = a.message_id
                      JOIN message_versions mv
                        ON mv.dialog_id = a.dialog_id AND mv.message_id = a.message_id AND mv.version = a.version
                     WHERE a.kind = 'edit'
                       AND {_LEGACY_HUMAN_DM_ALERT_PREDICATE}
                       {transcription_filter}
                     GROUP BY a.dialog_id, a.message_id, a.version
                    UNION ALL
                    SELECT a.seq, 'access_lost', a.occurred_at, a.dialog_id, NULL, NULL
                      FROM sync_alert_events a
                     WHERE a.kind = 'access_lost' AND a.dialog_id IS NOT NULL
                )
                INSERT INTO sync_alert_events_v51(kind, occurred_at, dialog_id, message_id, version)
                SELECT kind, occurred_at, dialog_id, message_id, version
                  FROM candidates
                 ORDER BY source_seq"""
        )
    else:
        conn.execute(
            """INSERT INTO sync_alert_events_v51(kind, occurred_at, dialog_id, message_id, version)
               SELECT 'access_lost', occurred_at, dialog_id, NULL, NULL
                 FROM sync_alert_events
                WHERE kind = 'access_lost' AND dialog_id IS NOT NULL
                ORDER BY seq"""
        )
    # Carry daemon lifecycle rows that never received a durable projection.
    conn.execute(
        """INSERT INTO sync_alert_events_v51(kind, occurred_at, dialog_id, message_id, version)
           SELECT 'access_lost', d.occurred_at, d.dialog_id, NULL, NULL
             FROM daemon_events d
            WHERE d.kind = 'access_lost' AND d.dialog_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM sync_alert_events a
                   WHERE a.kind = 'access_lost'
                     AND a.daemon_event_id = d.id
              )"""
    )


def _complete_replayed_migration_51(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS telemetry_events")
    conn.execute("DROP TABLE IF EXISTS daemon_events")
    conn.execute("INSERT INTO schema_version VALUES (51, strftime('%s', 'now'))")


def _prepare_migration_51_schema(conn: sqlite3.Connection) -> int:
    source_high_water = max(
        _sequence_high_water(conn, "sync_alert_events", "seq"),
        _sequence_high_water(conn, "daemon_events", "id"),
    )
    version_column_rows = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(message_versions)").fetchall())
    version_columns = {str(row[1]) for row in version_column_rows}
    if "origin" not in version_columns:
        _rebuild_message_versions(conn, "'legacy_unknown'")
    elif not _message_versions_has_safe_origin_contract(conn):
        _rebuild_message_versions(
            conn,
            "CASE WHEN origin IN ('telegram_edit', 'transcription') THEN origin ELSE 'legacy_unknown' END",
        )
    for trigger in (
        "sync_alert_events_message_insert_deleted",
        "sync_alert_events_message_delete_transition",
        "sync_alert_events_message_edit",
        "sync_alert_events_access_lost",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute(_RUNTIME_EVENTS_DDL)
    for stmt in _RUNTIME_EVENTS_INDEXES_DDL:
        conn.execute(stmt)
    return source_high_water


def _replace_alert_projection_v51(conn: sqlite3.Connection, source_high_water: int) -> None:
    conn.execute(_SYNC_ALERT_EVENTS_V51_DDL)
    # Seed the replacement AUTOINCREMENT before copying rows so every
    # migration-created alert is strictly above both old sequence domains.
    conn.execute(
        "INSERT INTO sqlite_sequence(name, seq) VALUES ('sync_alert_events_v51', ?)",
        (source_high_water,),
    )
    _legacy_alert_projection_v51(conn)
    conn.execute("DROP TABLE sync_alert_events")
    conn.execute("ALTER TABLE sync_alert_events_v51 RENAME TO sync_alert_events")
    max_seq_row = cast(tuple[int | None] | None, conn.execute("SELECT MAX(seq) FROM sync_alert_events").fetchone())
    max_seq = int(max_seq_row[0]) if max_seq_row and max_seq_row[0] is not None else 0
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'sync_alert_events'")
    conn.execute(
        "INSERT INTO sqlite_sequence(name, seq) VALUES ('sync_alert_events', ?)",
        (max(source_high_water, max_seq),),
    )
    conn.execute(_SYNC_ALERT_DELETED_INDEX_DDL)
    conn.execute(_SYNC_ALERT_EDIT_INDEX_DDL)
    for stmt in _SYNC_ALERT_V51_TRIGGERS:
        conn.execute(stmt)


def _finish_migration_51(conn: sqlite3.Connection) -> None:
    conn.execute(_DAEMON_STATE_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO daemon_state(key, value) VALUES "
        "('runtime_events_history_started_at_ms', CAST(strftime('%s', 'now') AS INTEGER) * 1000)"
    )
    conn.execute("DROP TABLE telemetry_events")
    conn.execute("DROP TABLE daemon_events")
    conn.execute("INSERT INTO schema_version VALUES (51, strftime('%s', 'now'))")


def _apply_migration_51(conn: sqlite3.Connection, current: int) -> int:
    """Atomically cut over runtime observations and the focused alert policy."""
    if current >= _EVENT_STORE_MIGRATION_51:
        return current
    table_rows = cast(
        list[tuple[object]], conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    )
    alert_column_rows = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(sync_alert_events)").fetchall())
    tables = {str(row[0]) for row in table_rows}
    alert_columns = {str(row[1]) for row in alert_column_rows}
    if "runtime_events" in tables and alert_columns and "daemon_event_id" not in alert_columns:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _complete_replayed_migration_51(conn)
            conn.commit()
            return 51
        except BaseException:
            conn.rollback()
            raise
    conn.execute("BEGIN IMMEDIATE")
    try:
        source_high_water = _prepare_migration_51_schema(conn)
        _replace_alert_projection_v51(conn, source_high_water)
        _finish_migration_51(conn)
        conn.commit()
        return 51
    except BaseException:
        conn.rollback()
        raise


def _apply_migration_52(conn: sqlite3.Connection, current: int) -> int:
    """Allow honest legacy origins without guessing existing row provenance."""
    if current >= _MESSAGE_ORIGIN_MIGRATION_52:
        return current
    conn.execute("BEGIN IMMEDIATE")
    try:
        version_columns = {
            str(row[1])
            for row in cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(message_versions)").fetchall())
        }
        if "origin" not in version_columns:
            _rebuild_message_versions(conn, "'legacy_unknown'")
        elif not _message_versions_has_safe_origin_contract(conn):
            # The deployed v51 did not persist insertion time.  edit_date is
            # Telegram source time and can predate cutover for a later
            # backfill, so it cannot safely identify legacy rows.
            _rebuild_message_versions(conn, "origin")
        conn.execute("INSERT INTO schema_version VALUES (52, strftime('%s', 'now'))")
        conn.commit()
        return 52
    except BaseException:
        conn.rollback()
        raise


def _apply_migration_53(conn: sqlite3.Connection, current: int) -> int:
    """Retain old message text only when it backs a durable human-DM edit alert."""
    if current >= _MESSAGE_HISTORY_MIGRATION_53:
        return current
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """DELETE FROM message_versions AS mv
                WHERE NOT EXISTS (
                    SELECT 1
                      FROM sync_alert_events AS alert
                     WHERE alert.kind = 'edit'
                       AND alert.dialog_id = mv.dialog_id
                       AND alert.message_id = mv.message_id
                       AND alert.version = mv.version
                )"""
        )
        conn.execute("INSERT INTO schema_version VALUES (53, strftime('%s', 'now'))")
        conn.commit()
        return 53
    except BaseException:
        conn.rollback()
        raise


def _migrate_runtime_lifecycle_events_v54(conn: sqlite3.Connection) -> int:
    conn.execute(
        """UPDATE conversation_history_events_v54 AS h
              SET reason_code = (
                      SELECT r.reason_code FROM runtime_events r
                       WHERE r.kind = 'sync.' || h.kind AND r.dialog_id = h.dialog_id
                         AND CAST(r.observed_at_ms / 1000 AS INTEGER) = h.occurred_at
                       ORDER BY r.id DESC LIMIT 1
                  ),
                  previous_status = (
                      SELECT json_extract(r.payload_json, '$.previous_status') FROM runtime_events r
                       WHERE r.kind = 'sync.' || h.kind AND r.dialog_id = h.dialog_id
                         AND CAST(r.observed_at_ms / 1000 AS INTEGER) = h.occurred_at
                       ORDER BY r.id DESC LIMIT 1
                  )
            WHERE h.kind IN ('access_lost', 'access_restored')
              AND EXISTS (
                  SELECT 1 FROM runtime_events r
                   WHERE r.kind = 'sync.' || h.kind AND r.dialog_id = h.dialog_id
                     AND CAST(r.observed_at_ms / 1000 AS INTEGER) = h.occurred_at
              )"""
    )
    predicate = """r.kind IN ('sync.access_lost', 'sync.access_restored')
                  AND r.dialog_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM sync_alert_events h
                       WHERE h.kind = substr(r.kind, 6)
                         AND h.dialog_id = r.dialog_id
                         AND h.occurred_at = CAST(r.observed_at_ms / 1000 AS INTEGER)
                  )"""
    conn.execute(
        f"""INSERT INTO conversation_history_events_v54(
               kind, occurred_at, time_basis, dialog_id, reason_code, previous_status
           )
           SELECT substr(r.kind, 6), CAST(r.observed_at_ms / 1000 AS INTEGER), 'observed',
                  r.dialog_id, r.reason_code, json_extract(r.payload_json, '$.previous_status')
             FROM runtime_events r
            WHERE {predicate}
            ORDER BY r.observed_at_ms, r.id"""
    )
    return cast(int, conn.execute(f"SELECT COUNT(*) FROM runtime_events r WHERE {predicate}").fetchone()[0])


def _apply_migration_54(conn: sqlite3.Connection, current: int) -> int:
    """Name event stores by durability and make conversation history append-only."""
    if current >= _EVENT_NAMES_MIGRATION_54:
        return current
    conn.execute("BEGIN IMMEDIATE")
    try:
        runtime_high_water = _sequence_high_water(conn, "runtime_events", "id")
        history_high_water = _sequence_high_water(conn, "sync_alert_events", "seq")
        for trigger in (
            "sync_alert_events_message_insert_deleted",
            "sync_alert_events_message_delete_transition",
            "sync_alert_events_message_edit",
            "sync_alert_events_access_lost",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(_RUNTIME_OBSERVATIONS_V54_DDL)
        conn.execute(
            """INSERT INTO runtime_observations_v54
               SELECT id, observed_at_ms, kind, runtime_instance_id, operation_id, outcome,
                      reason_code, dialog_id, duration_ms, tool_name, result_count,
                      has_cursor, page_depth, has_filter, error_type, NULL, NULL, payload_json
                 FROM runtime_events
                WHERE kind NOT IN ('sync.access_lost', 'sync.access_restored')"""
        )
        conn.execute(_CONVERSATION_HISTORY_EVENTS_V54_DDL)
        conn.execute(
            """INSERT INTO conversation_history_events_v54(
                   seq, kind, occurred_at, time_basis, dialog_id, message_id, version
               )
               SELECT seq, kind, occurred_at,
                      CASE WHEN kind = 'edit' THEN 'telegram' ELSE 'observed' END,
                      dialog_id, message_id, version
                 FROM sync_alert_events"""
        )
        runtime_lifecycle_count = _migrate_runtime_lifecycle_events_v54(conn)
        runtime_count = cast(
            int,
            conn.execute(
                "SELECT COUNT(*) FROM runtime_events WHERE kind NOT IN ('sync.access_lost', 'sync.access_restored')"
            ).fetchone()[0],
        )
        copied_runtime_count = cast(int, conn.execute("SELECT COUNT(*) FROM runtime_observations_v54").fetchone()[0])
        history_count = cast(int, conn.execute("SELECT COUNT(*) FROM sync_alert_events").fetchone()[0])
        copied_history_count = cast(
            int, conn.execute("SELECT COUNT(*) FROM conversation_history_events_v54").fetchone()[0]
        )
        if runtime_count != copied_runtime_count or history_count + runtime_lifecycle_count != copied_history_count:
            raise RuntimeError("event store migration count mismatch")
        conn.execute("DROP TABLE runtime_events")
        conn.execute("ALTER TABLE runtime_observations_v54 RENAME TO runtime_observations")
        conn.execute("DROP TABLE sync_alert_events")
        conn.execute("ALTER TABLE conversation_history_events_v54 RENAME TO conversation_history_events")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('runtime_observations', 'conversation_history_events')"
        )
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('runtime_observations', ?)",
            (runtime_high_water,),
        )
        migrated_history_max = cast(
            int, conn.execute("SELECT COALESCE(MAX(seq), 0) FROM conversation_history_events").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('conversation_history_events', ?)",
            (max(history_high_water, migrated_history_max),),
        )
        conn.execute("CREATE INDEX idx_runtime_observations_time ON runtime_observations(observed_at_ms DESC, id DESC)")
        conn.execute(
            "CREATE INDEX idx_runtime_observations_kind_time ON runtime_observations(kind, observed_at_ms DESC, id DESC)"
        )
        conn.execute(
            "CREATE INDEX idx_runtime_observations_dialog_time ON runtime_observations(dialog_id, observed_at_ms DESC, id DESC) WHERE dialog_id IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_runtime_observations_source ON runtime_observations(source_namespace, source_event_id) WHERE source_namespace IS NOT NULL AND source_event_id IS NOT NULL"
        )
        for statement in _CONVERSATION_HISTORY_INDEXES_V54_DDL:
            conn.execute(statement)
        conn.execute(_EVENT_RECOVERY_LEDGER_DDL)
        conn.execute(
            """INSERT OR REPLACE INTO daemon_state(key, value)
               SELECT 'runtime_observations_history_started_at_ms', value
                 FROM daemon_state WHERE key = 'runtime_events_history_started_at_ms'"""
        )
        conn.execute(
            """INSERT OR REPLACE INTO daemon_state(key, value)
               SELECT 'runtime_observations_last_cap_truncation_ms', value
                 FROM daemon_state WHERE key = 'runtime_events_last_cap_truncation_ms'"""
        )
        conn.execute(
            "DELETE FROM daemon_state WHERE key IN ('runtime_events_history_started_at_ms', 'runtime_events_last_cap_truncation_ms')"
        )
        conn.execute(
            """CREATE TABLE schema_version_v54 (
                   version INTEGER PRIMARY KEY,
                   applied_at INTEGER NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO schema_version_v54(version, applied_at)
               SELECT version, MIN(applied_at) FROM schema_version GROUP BY version"""
        )
        conn.execute("INSERT INTO schema_version_v54 VALUES (54, strftime('%s', 'now'))")
        conn.execute("DROP TABLE schema_version")
        conn.execute("ALTER TABLE schema_version_v54 RENAME TO schema_version")
        for statement in _CONVERSATION_HISTORY_TRIGGERS_V54:
            conn.execute(statement)
        conn.commit()
        return 54
    except BaseException:
        conn.rollback()
        raise


def _apply_migration_55(conn: sqlite3.Connection, current: int) -> int:
    """Persist Telegram's explanation for future access-loss transitions."""
    if current >= _ACCESS_CAUSE_MIGRATION_55:
        return current
    conn.execute("BEGIN IMMEDIATE")
    try:
        column_rows = cast(
            list[tuple[object, ...]],
            conn.execute("PRAGMA table_info(conversation_history_events)").fetchall(),
        )
        columns = {str(row[1]) for row in column_rows}
        if "access_change_cause" not in columns:
            conn.execute(
                "ALTER TABLE conversation_history_events ADD COLUMN access_change_cause TEXT "
                "CHECK(access_change_cause IN ('self_left','removed_by_admin','banned_by_admin','unknown'))"
            )
        if "actor_id" not in columns:
            conn.execute("ALTER TABLE conversation_history_events ADD COLUMN actor_id INTEGER")
        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (55, strftime('%s', 'now'))")
        conn.commit()
        return 55
    except BaseException:
        conn.rollback()
        raise


def _apply_migration_56(conn: sqlite3.Connection, current: int) -> int:
    """Separate exact tool names from stable capability and contract identity."""
    if current >= _TOOL_CAPABILITY_MIGRATION_56:
        return current
    conn.execute("BEGIN IMMEDIATE")
    try:
        column_rows = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(runtime_observations)").fetchall())
        columns = {str(row[1]) for row in column_rows}
        if "tool_capability" not in columns:
            conn.execute("ALTER TABLE runtime_observations ADD COLUMN tool_capability TEXT")
        if "contract_version" not in columns:
            conn.execute("ALTER TABLE runtime_observations ADD COLUMN contract_version INTEGER")
        conn.execute(
            """UPDATE runtime_observations
                  SET tool_capability=CASE
                        WHEN tool_name IN ('get_sync_alerts','list_important_events','list_conversation_changes')
                          THEN 'conversation_changes'
                        ELSE tool_name
                      END,
                      contract_version=CASE
                        WHEN tool_name IN ('get_sync_alerts','list_important_events') THEN 0
                        ELSE 1
                      END
                WHERE kind='mcp.call'
                  AND (tool_capability IS NULL OR contract_version IS NULL)"""
        )
        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (56, strftime('%s', 'now'))")
        conn.commit()
        return 56
    except BaseException:
        conn.rollback()
        raise


def _apply_migration_57(conn: sqlite3.Connection, current: int) -> int:
    """Persist independent progressive entity-profile section outcomes."""
    return _apply_migration(
        conn,
        current,
        _ENTITY_PROFILE_SECTIONS_MIGRATION_57,
        [
            """CREATE TABLE IF NOT EXISTS entity_detail_sections (
                entity_id INTEGER NOT NULL,
                section TEXT NOT NULL CHECK(section IN (
                    'full_profile', 'common_chats', 'contact_overlap',
                    'avatar_history', 'personal_channel'
                )),
                status TEXT NOT NULL CHECK(status IN (
                    'fresh', 'stale', 'pending', 'unavailable', 'not_applicable'
                )),
                observed_at INTEGER,
                reason TEXT,
                payload_json TEXT,
                retry_at INTEGER,
                PRIMARY KEY(entity_id, section),
                FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
            ) WITHOUT ROWID""",
            (
                "CREATE INDEX IF NOT EXISTS idx_entity_detail_sections_status "
                "ON entity_detail_sections(status, retry_at, observed_at)"
            ),
            """CREATE TABLE IF NOT EXISTS entity_profile_refresh_state (
                entity_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('failed', 'pending')),
                retry_at INTEGER,
                reason TEXT,
                updated_at INTEGER NOT NULL
            ) WITHOUT ROWID""",
        ],
    )


def _apply_migration_58(conn: sqlite3.Connection, current: int) -> int:
    """Add ordered author lookup paths used by Account Trace."""
    return _apply_migration(
        conn,
        current,
        _ACCOUNT_TRACE_INDEXES_MIGRATION_58,
        [
            _MESSAGES_ACCOUNT_TRACE_SENDER_INDEX_DDL,
            _MESSAGES_ACCOUNT_TRACE_POST_AUTHOR_INDEX_DDL,
        ],
    )


def _apply_migration_59(conn: sqlite3.Connection, current: int) -> int:
    """Add durable per-dialog scheduling for scheduled-message reconciliation."""
    if current >= _SCHEDULED_RECONCILIATION_MIGRATION_59:
        return current
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_SCHEDULED_RECONCILIATION_STATE_DDL)
        conn.execute(_SCHEDULED_RECONCILIATION_REPAIR_INDEX_DDL)
        conn.execute(_SCHEDULED_RECONCILIATION_DISCOVERY_INDEX_DDL)
        conn.execute(_OWN_ONLY_DIALOGS_DDL)
        now = _row_first_int(cast(tuple[object] | None, conn.execute("SELECT unixepoch()").fetchone()))
        conn.execute(
            """
            INSERT OR IGNORE INTO scheduled_reconciliation_state (
                dialog_id, repair_due_at, discovery_due_at, updated_at
            )
            SELECT candidate.dialog_id,
                   CASE WHEN active.dialog_id IS NULL THEN NULL ELSE :now END,
                   :now + (((candidate.dialog_id % :quiet_seconds) + :quiet_seconds) % :quiet_seconds),
                   :now
            FROM (
                SELECT dialog_id FROM dialogs
                 WHERE hidden = 0 AND type IN ('user', 'bot', 'channel')
                UNION
                SELECT dialog_id FROM own_only_dialogs
                UNION
                SELECT dialog_id FROM scheduled_messages WHERE message_state = 'scheduled'
            ) AS candidate
            LEFT JOIN (
                SELECT DISTINCT dialog_id FROM scheduled_messages WHERE message_state = 'scheduled'
            ) AS active ON active.dialog_id = candidate.dialog_id
            """,
            {"now": now, "quiet_seconds": SCHEDULED_QUIET_DISCOVERY_SECONDS},
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_version VALUES (?, strftime('%s', 'now'))",
            (_SCHEDULED_RECONCILIATION_MIGRATION_59,),
        )
        conn.commit()
        return _SCHEDULED_RECONCILIATION_MIGRATION_59
    except BaseException:
        conn.rollback()
        raise


def _apply_migration_60(conn: sqlite3.Connection, current: int) -> int:
    """Add restart-safe cursors for domain-owned durable demand slices."""
    if current >= _DOMAIN_RESUME_STATE_MIGRATION_60:
        return current
    conn.execute("BEGIN IMMEDIATE")
    try:
        activity_columns = {
            str(row[1])
            for row in cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(activity_dialog_state)"))
        }
        if "hot_page_offset_id" not in activity_columns:
            conn.execute("ALTER TABLE activity_dialog_state ADD COLUMN hot_page_offset_id INTEGER")
        if "hot_window_max_id" not in activity_columns:
            conn.execute("ALTER TABLE activity_dialog_state ADD COLUMN hot_window_max_id INTEGER")
        if "hot_window_had_new" not in activity_columns:
            conn.execute(
                "ALTER TABLE activity_dialog_state ADD COLUMN hot_window_had_new "
                "INTEGER NOT NULL DEFAULT 0 CHECK(hot_window_had_new IN (0, 1))"
            )

        dialog_columns = {
            str(row[1]) for row in cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(dialogs)"))
        }
        if "revision" not in dialog_columns:
            conn.execute("ALTER TABLE dialogs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0)")
        conn.execute(_DIALOG_FULL_RECONCILIATION_STATE_DDL)
        conn.execute(_DIALOG_FULL_RECONCILIATION_BASELINE_DDL)
        conn.execute(_DIALOG_FULL_RECONCILIATION_UNSEEN_INDEX_DDL)
        conn.execute(_DIALOGS_REVISION_TRIGGER_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO dialog_full_reconciliation_state(singleton, generation, status) "
            "VALUES (1, 0, 'idle')"
        )

        conn.execute(_DELTA_ACCESS_RECOVERY_STATE_DDL)
        conn.execute(_DELTA_ACCESS_RECOVERY_DUE_INDEX_DDL)
        conn.execute(_DELTA_ACCESS_RECOVERY_CLEAR_TRIGGER_DDL)
        refresh_columns = {
            str(row[1])
            for row in cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(entity_profile_refresh_state)"))
        }
        refresh_schema_row = cast(
            tuple[str] | None,
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='entity_profile_refresh_state'"
            ).fetchone(),
        )
        refresh_schema = refresh_schema_row[0] if refresh_schema_row is not None else ""
        if {"next_section", "acquisition_cursor"} - refresh_columns or "'rejected'" not in refresh_schema:
            next_section_expr = "next_section" if "next_section" in refresh_columns else "'full_profile'"
            cursor_expr = "acquisition_cursor" if "acquisition_cursor" in refresh_columns else "0"
            conn.execute("ALTER TABLE entity_profile_refresh_state RENAME TO entity_profile_refresh_state_v59")
            conn.execute(_ENTITY_PROFILE_REFRESH_STATE_V60_DDL)
            conn.execute(
                "INSERT INTO entity_profile_refresh_state("
                "entity_id, status, retry_at, reason, updated_at, next_section, acquisition_cursor) "
                "SELECT entity_id, status, retry_at, reason, updated_at, "
                f"{next_section_expr}, {cursor_expr} FROM entity_profile_refresh_state_v59"
            )
            conn.execute("DROP TABLE entity_profile_refresh_state_v59")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_profile_refresh_due "
            "ON entity_profile_refresh_state(status, retry_at, updated_at, entity_id)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_version VALUES (?, strftime('%s', 'now'))",
            (_DOMAIN_RESUME_STATE_MIGRATION_60,),
        )
        conn.commit()
        return _DOMAIN_RESUME_STATE_MIGRATION_60
    except BaseException:
        conn.rollback()
        raise


def _apply_migrations(conn: sqlite3.Connection) -> None:  # noqa: PLR0915
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
    current = _apply_migrations_21_to_28(conn, current)
    current = _apply_migration_29(conn, current)
    current = _apply_migration_30(conn, current)
    current = _apply_migration_31(conn, current)
    current = _apply_migration_32(conn, current)
    current = _apply_migration_33(conn, current)
    current = _apply_migration_34(conn, current)
    current = _apply_migration_35(conn, current)
    current = _apply_migration_36(conn, current)
    current = _apply_migration_37(conn, current)
    current = _apply_migration_38(conn, current)
    current = _apply_migration_39(conn, current)
    current = _apply_migration_40(conn, current)
    current = _apply_migration_41(conn, current)
    current = _apply_migration_42(conn, current)
    current = _apply_migration_43(conn, current)
    current = _apply_migration_44(conn, current)
    current = _apply_migration_45(conn, current)
    current = _apply_migration_46(conn, current)
    current = _apply_migration_47(conn, current)
    current = _apply_migration_48(conn, current)
    current = _apply_migration_49(conn, current)
    current = _apply_migration_50(conn, current)
    # A schema-version ledger can be manually damaged while the v54 tables
    # remain intact. Never replay the destructive event-store migrations over
    # that already-current physical schema; repair only the ledger.
    v54_tables = {
        str(item[0])
        for item in cast(
            list[tuple[object]], conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        )
    }
    if {"runtime_observations", "conversation_history_events", "event_recovery_ledger"} <= v54_tables:
        for version in range(51, 55):
            conn.execute(
                "INSERT OR IGNORE INTO schema_version VALUES (?, strftime('%s', 'now'))",
                (version,),
            )
        conn.commit()
        current = 54
    current = _apply_migration_51(conn, current)
    current = _apply_migration_52(conn, current)
    current = _apply_migration_53(conn, current)
    current = _apply_migration_54(conn, current)
    current = _apply_migration_55(conn, current)
    current = _apply_migration_56(conn, current)
    current = _apply_migration_57(conn, current)
    current = _apply_migration_58(conn, current)
    current = _apply_migration_59(conn, current)
    current = _apply_migration_60(conn, current)

    logger.info("sync_db migrations applied through version %d", _CURRENT_SCHEMA_VERSION)


def _ensure_scheduled_messages_fts(conn: sqlite3.Connection) -> None:
    """Repair the scheduled FTS companion if an earlier v27 rollout omitted it."""
    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_messages'").fetchone()
        is None
    ):
        return

    conn.execute(_SCHEDULED_MESSAGES_FTS_DDL)
    from .fts import stem_text

    rows = cast(
        list[tuple[int, int, str | None]],
        conn.execute(
            "SELECT sm.dialog_id, sm.message_id, sm.text "
            "FROM scheduled_messages sm "
            "LEFT JOIN scheduled_messages_fts sf "
            "  ON sf.dialog_id = sm.dialog_id AND sf.message_id = sm.message_id "
            "WHERE sf.message_id IS NULL"
        ).fetchall(),
    )
    if rows:
        conn.executemany(
            "INSERT INTO scheduled_messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
            ((dialog_id, message_id, stem_text(text)) for dialog_id, message_id, text in rows),
        )
    conn.commit()


def _ensure_hydration_jobs(conn: sqlite3.Connection) -> None:
    """Ensure the current hydration queue and its due-time index exist."""
    conn.execute(_HYDRATION_JOBS_DDL)
    conn.execute(_HYDRATION_JOBS_SCHEDULE_INDEX_DDL)
    conn.execute(_TRANSCRIBABLE_TRANSCRIPTION_REPAIR_INDEX_DDL)
    conn.execute("DROP INDEX IF EXISTS idx_messages_voice_undeleted_sent")
    conn.execute(_MEDIA_METADATA_UNRESOLVED_CONTACT_OTHER_INDEX_DDL)
    conn.execute(_MEDIA_METADATA_UNRESOLVED_VIDEO_INDEX_DDL)
    conn.commit()


def ensure_own_only_schema(conn: sqlite3.Connection) -> None:
    """Create the ownership cache table used by scheduled reconciliation and reads."""
    conn.execute(_OWN_ONLY_DIALOGS_DDL)
    conn.commit()


def _sync_schema_table_names(conn: sqlite3.Connection) -> set[str]:
    rows = cast(
        list[tuple[object]],
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('entities', 'dialogs')"
        ).fetchall(),
    )
    return {cast(str, row[0]) for row in rows}


def _reserved_entity_rows(conn: sqlite3.Connection) -> list[tuple[object, object, object]]:
    return cast(
        list[tuple[object, object, object]],
        conn.execute("SELECT id, username, type FROM entities WHERE username IS NOT NULL").fetchall(),
    )


def _reserved_reply_ids(rows: list[tuple[object, object, object]]) -> list[int]:
    return [int(cast(int | str, row[0])) for row in rows if is_reserved_replies_username(row[1])]


def _repair_reserved_entities(conn: sqlite3.Connection, rows: list[tuple[object, object, object]]) -> None:
    conn.executemany(
        "UPDATE entities SET type = ? WHERE id = ?",
        (
            (SERVICE_DIALOG_TYPE, int(cast(int | str, row[0])))
            for row in rows
            if is_reserved_replies_username(row[1]) and is_bot_dialog_type(row[2])
        ),
    )


def _repair_reserved_dialogs(conn: sqlite3.Connection, reply_ids: list[int]) -> None:
    placeholders = ",".join("?" * len(reply_ids))
    dialog_rows = cast(
        list[tuple[object, object]],
        conn.execute(
            f"SELECT dialog_id, type FROM dialogs WHERE dialog_id IN ({placeholders})",
            reply_ids,
        ).fetchall(),
    )
    conn.executemany(
        "UPDATE dialogs SET type = ? WHERE dialog_id = ?",
        ((SERVICE_DIALOG_TYPE, int(cast(int | str, row[0]))) for row in dialog_rows if is_bot_dialog_type(row[1])),
    )


def repair_reserved_dialog_types(conn: sqlite3.Connection) -> None:
    """Repair persisted @replies rows through the normal startup path.

    Older snapshots classified Telegram's reserved Replies peer as ``bot``.
    Username matching is delegated to the canonical classifier so this repair
    cannot drift into numeric-ID or display-name heuristics.
    """
    tables = _sync_schema_table_names(conn)
    if "entities" not in tables:
        return
    rows = _reserved_entity_rows(conn)
    reply_ids = _reserved_reply_ids(rows)
    if not reply_ids:
        return
    with conn:
        _repair_reserved_entities(conn, rows)
        if "dialogs" in tables:
            _repair_reserved_dialogs(conn, reply_ids)


def ensure_sync_schema(db_path: Path) -> None:
    """Ensure sync.db exists and has the current schema.

    Probes the DB first, then acquires fcntl lock before applying migrations
    to prevent parallel-process races.
    """
    probe_conn = _open_sync_db(db_path)
    try:
        if _schema_ready(probe_conn):
            ensure_own_only_schema(probe_conn)
            _ensure_scheduled_messages_fts(probe_conn)
            _ensure_hydration_jobs(probe_conn)
            repair_reserved_dialog_types(probe_conn)
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

            ensure_own_only_schema(bootstrap_conn)
            _ensure_scheduled_messages_fts(bootstrap_conn)
            _ensure_hydration_jobs(bootstrap_conn)
            repair_reserved_dialog_types(bootstrap_conn)
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


def migrate_legacy_databases(
    conn: sqlite3.Connection,
    state_dir: Path,
    *,
    telemetry_retention_ttl_seconds: int,
) -> None:
    """One-shot migration of durable entities and removal of obsolete local databases.

    Called once at daemon startup after ensure_sync_schema(). Idempotent —
    INSERT OR IGNORE skips existing rows. Deletes legacy files after success.
    """
    if telemetry_retention_ttl_seconds < 1:
        raise ValueError("telemetry_retention_ttl_seconds must be positive")
    entity_cache_path = state_dir / "entity_cache.db"
    entity_lock_path = state_dir / "entity_cache.db.bootstrap.lock"
    analytics_path = state_dir / "analytics.db"

    # Migrate entities (only entities table — reaction_metadata, topic_metadata,
    # message_cache are cache-layer data with TTL, not worth migrating)
    entity_stmts = [
        (
            "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) "
            "SELECT id, type, name, username, updated_at FROM legacy.entities"
        ),
    ]
    copied_entities = _migrate_from_legacy_db(conn, entity_cache_path, entity_stmts)
    if copied_entities:
        logger.info("migrated %d entities from entity_cache.db", copied_entities)

    # Runtime observations deliberately start at the v51 cutover boundary;
    # analytics.db used a superseded event contract and is discarded below.
    for path in [entity_cache_path, entity_lock_path, analytics_path]:
        if path.exists():
            path.unlink()
            logger.info("deleted legacy file: %s", path)
