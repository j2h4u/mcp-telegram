"""SQLite read queries for dialogs, sync status, and read-only alerts."""

from __future__ import annotations

import sqlite3
import time
from typing import cast

_SNAPSHOT_STALE_THRESHOLD_S = 12 * 3600


def _compute_snapshot_age_h(max_snapshot_at: int | None) -> int | None:
    """Return integer hours since the freshest snapshot, or None when fresh/unknown."""
    if max_snapshot_at is None:
        return None
    age_s = int(time.time()) - int(max_snapshot_at)
    if age_s > _SNAPSHOT_STALE_THRESHOLD_S:
        return age_s // 3600
    return None


def _compute_sync_coverage(
    total_messages: int | None,
    local_count: int,
) -> int | None:
    """Compute sync coverage when the local and remote counts are comparable."""
    if _sync_coverage_unknown(total_messages, local_count):
        return None
    if total_messages == 0:
        return 100
    return round(local_count / cast(int, total_messages) * 100)


def _sync_coverage_unknown(total_messages: int | None, local_count: int) -> bool:
    return total_messages is None or total_messages < 0 or local_count > total_messages


def _build_access_metadata(
    conn: sqlite3.Connection,
    dialog_id: int,
    status: str,
) -> dict:
    """Build consistent access metadata for list_messages and search_messages."""
    meta: dict = {"dialog_access": "archived" if status == "access_lost" else "live"}

    if status == "access_lost":
        row = cast(
            tuple[object, object, object, object, object] | None,
            conn.execute(_SELECT_DIALOG_ACCESS_META_SQL, (dialog_id,)).fetchone(),
        )
        if row:
            _, total_messages, access_lost_at, last_synced_at, last_event_at = row
            total_messages_i = cast(int | None, total_messages)
            count_row = cast(tuple[object] | None, conn.execute(_COUNT_SYNCED_MESSAGES_SQL, (dialog_id,)).fetchone())
            local_count = int(cast(int | str, count_row[0])) if count_row else 0

            meta["access_lost_at"] = access_lost_at
            meta["last_synced_at"] = last_synced_at
            meta["last_event_at"] = last_event_at
            meta["sync_coverage_pct"] = _compute_sync_coverage(total_messages_i, local_count)
            if total_messages_i is None:
                meta["archived_message_count"] = local_count

    return meta


_SELECT_SYNCED_STATUSES_SQL = (
    "SELECT dialog_id, status, total_messages, access_lost_at, "
    "read_inbox_max_id, read_outbox_max_id FROM synced_dialogs"
)

# Phase 44 (LISTDIALOGS-01/02/04, DIFF-04): pure-SQL dialog list.
# LEFT JOIN synced_dialogs to preserve sync_status/total_messages/access_lost_at.
# `:name_pat` is a Python-lowered LIKE pattern (e.g. "%женск%") OR None for
# no pre-filter. Cyrillic case-folding is delegated to the Python fuzzy pass
# because SQLite LOWER() is ASCII-only.
# `:archived_filter` and `:pinned_filter` are 0 (filter rows where col=0)
# or None (no filter).
_LIST_DIALOGS_SQL = """
WITH agent_visible_dialogs AS (
    SELECT
        d.dialog_id,
        d.name,
        d.type,
        d.archived,
        d.pinned,
        d.members,
        d.created,
        COALESCE(d.last_message_at, sd.last_event_at, sd.last_synced_at, sd.access_lost_at) AS last_message_at,
        d.snapshot_at,
        d.unread_mentions_count,
        d.unread_reactions_count,
        d.draft_text,
        sd.status AS sync_status,
        sd.total_messages,
        sd.access_lost_at
    FROM dialogs d
    LEFT JOIN synced_dialogs sd USING(dialog_id)
    WHERE d.hidden = 0 OR sd.status = 'access_lost'

    UNION ALL

    SELECT
        sd.dialog_id,
        NULL AS name,
        NULL AS type,
        0 AS archived,
        0 AS pinned,
        NULL AS members,
        NULL AS created,
        COALESCE(sd.last_event_at, sd.last_synced_at, sd.access_lost_at) AS last_message_at,
        NULL AS snapshot_at,
        0 AS unread_mentions_count,
        0 AS unread_reactions_count,
        NULL AS draft_text,
        sd.status AS sync_status,
        sd.total_messages,
        sd.access_lost_at
    FROM synced_dialogs sd
    LEFT JOIN dialogs d USING(dialog_id)
    WHERE sd.status = 'access_lost' AND d.dialog_id IS NULL
)
SELECT
    dialog_id, name, type, archived, pinned,
    members, created, last_message_at, snapshot_at,
    unread_mentions_count, unread_reactions_count, draft_text,
    sync_status, total_messages, access_lost_at
FROM agent_visible_dialogs
WHERE (:archived_filter IS NULL OR archived = :archived_filter)
AND (:pinned_filter IS NULL OR pinned = :pinned_filter)
AND (:name_pat IS NULL OR LOWER(name) LIKE :name_pat ESCAPE '\\')
ORDER BY pinned DESC, last_message_at DESC
"""

# Contract note (WR-06): results are emitted as unread_in / unread_out only for DMs.
_BATCHED_UNREAD_COUNTS_SQL = (
    "SELECT m.dialog_id, "
    'SUM(CASE WHEN m."out" = 0 AND m.message_id > COALESCE(sd.read_inbox_max_id, -1) '
    "THEN 1 ELSE 0 END) AS unread_in, "
    'SUM(CASE WHEN m."out" = 1 AND m.message_id > COALESCE(sd.read_outbox_max_id, -1) '
    "THEN 1 ELSE 0 END) AS unread_out "
    "FROM messages m JOIN synced_dialogs sd USING(dialog_id) "
    "WHERE sd.status = 'synced' AND m.is_deleted = 0 AND m.is_service = 0 "
    "GROUP BY m.dialog_id"
)

_MARK_FOR_SYNC_SQL = "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'not_synced')"
_UNMARK_SYNC_SQL = "UPDATE synced_dialogs SET status = 'not_synced', sync_progress = NULL WHERE dialog_id = ?"

_GET_SYNC_STATUS_SQL = (
    "SELECT status, last_synced_at, last_event_at, sync_progress, total_messages, access_lost_at "
    "FROM synced_dialogs WHERE dialog_id = ?"
)
_COUNT_SYNCED_MESSAGES_SQL = "SELECT COUNT(*) FROM messages WHERE dialog_id = ? AND is_deleted = 0"
_COUNT_MESSAGES_BY_DIALOG_SQL = "SELECT dialog_id, COUNT(*) FROM messages WHERE is_deleted = 0 GROUP BY dialog_id"

_SELECT_DIALOG_ACCESS_META_SQL = (
    "SELECT status, total_messages, access_lost_at, last_synced_at, last_event_at "
    "FROM synced_dialogs WHERE dialog_id = ?"
)

_GET_DELETED_ALERTS_SQL = (
    "SELECT dialog_id, message_id, text, deleted_at "
    "FROM messages WHERE is_deleted = 1 AND deleted_at > ? "
    "ORDER BY deleted_at DESC LIMIT ?"
)
_GET_EDIT_ALERTS_SQL = (
    "SELECT dialog_id, message_id, version, old_text, edit_date "
    "FROM message_versions WHERE edit_date > ? "
    "ORDER BY edit_date DESC LIMIT ?"
)
_GET_ACCESS_LOST_ALERTS_SQL = (
    "SELECT dialog_id, access_lost_at FROM synced_dialogs WHERE status = 'access_lost' AND access_lost_at > ?"
)

# Unread SQL - zero Telegram API calls.
_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL = (
    "SELECT sd.dialog_id, sd.read_inbox_max_id, sd.last_event_at, "
    "COALESCE(e.name, CAST(sd.dialog_id AS TEXT)) AS display_name, "
    "COALESCE(e.type, d.type, 'Unknown') AS entity_type, "
    "d.members AS participants_count, "
    "(SELECT COUNT(*) FROM messages m "
    " WHERE m.dialog_id = sd.dialog_id "
    "   AND m.message_id > sd.read_inbox_max_id "
    "   AND m.is_deleted = 0"
    '   AND m."out" = 0'
    "   AND m.is_service = 0) AS unread_count "
    "FROM synced_dialogs sd "
    "LEFT JOIN entities e ON e.id = sd.dialog_id "
    "LEFT JOIN dialogs d ON d.dialog_id = sd.dialog_id "
    "WHERE sd.status = 'synced' "
    "AND sd.read_inbox_max_id IS NOT NULL"
)

_GET_READ_POSITION_SQL = "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = ?"
_COUNT_BOOTSTRAP_PENDING_SQL = (
    "SELECT COUNT(*) FROM synced_dialogs WHERE status = 'synced' AND read_inbox_max_id IS NULL"
)

# list_topics - read from topic_metadata snapshot.
_LIST_TOPICS_SQL = (
    "SELECT topic_id, title, icon_emoji_id, date "
    "FROM topic_metadata "
    "WHERE dialog_id = ? AND is_deleted = 0 AND hidden = 0 "
    "ORDER BY topic_id ASC"
)
