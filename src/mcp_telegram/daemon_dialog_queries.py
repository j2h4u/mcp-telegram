"""SQLite read queries for dialogs, sync status, and read-only alerts."""

from __future__ import annotations

_GET_SYNC_STATUS_SQL = (
    "SELECT sd.status, sd.last_synced_at, sd.last_event_at, sd.sync_progress, sd.total_messages, sd.access_lost_at, "
    "last_delta_checked_at, delta_refresh_requested_at, access_last_revalidated_at, access_next_revalidate_at, "
    "fhe.enabled, fhe.source "
    "FROM synced_dialogs sd LEFT JOIN full_history_enrollment fhe USING(dialog_id) WHERE sd.dialog_id = ?"
)
_COUNT_SYNCED_MESSAGES_SQL = "SELECT COUNT(*) FROM messages WHERE dialog_id = ? AND is_deleted = 0"

# list_topics - read from topic_metadata snapshot.
_LIST_TOPICS_SQL = (
    "SELECT topic_id, title, icon_emoji_id, date, icon_emoji, icon_color "
    "FROM topic_metadata "
    "WHERE dialog_id = ? AND is_deleted = 0 AND hidden = 0 "
    "ORDER BY topic_id ASC"
)
