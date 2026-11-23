"""Shared SQLite eligibility for message fact hydration."""

import sqlite3

from .own_only_contracts import CHANNEL_PEER_ID_OFFSET

# Automatic recovery is limited to messages sent or edited since May 2026.
_RICH_AUTOMATIC_DATE_SQL = "(m.sent_at >= 1777593600 OR COALESCE(m.edit_date, 0) >= 1777593600)"
_RICH_CONTENT_UNRESOLVED_SQL = "(m.rich_message IS NULL OR json_extract(m.rich_message,'$.part')=1)"
_RICH_AUTOMATIC_CONTENT_SQL = "(m.rich_message IS NOT NULL OR " + _RICH_AUTOMATIC_DATE_SQL + ")"
_RICH_CURRENT_SQL = (
    "m.is_deleted=0 AND NOT EXISTS (SELECT 1 FROM message_observations o "
    "WHERE o.dialog_id=m.dialog_id AND o.message_id=m.message_id AND o.is_deleted=1) "
    f"AND (m.dialog_id < -{CHANNEL_PEER_ID_OFFSET} OR NOT EXISTS "
    "(SELECT 1 FROM nonchannel_message_deletions n WHERE n.message_id=m.message_id))"
)
_RICH_AUTOMATIC_ELIGIBILITY_SQL = (
    _RICH_CURRENT_SQL + " AND " + _RICH_CONTENT_UNRESOLVED_SQL + " AND " + _RICH_AUTOMATIC_CONTENT_SQL
)
_FACT_HYDRATION_ELIGIBILITY_SQL = (
    "SELECT 1 FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.dialog_id = ? AND sd.status IN ('syncing', 'synced')"
)


def fact_hydration_eligible(conn: sqlite3.Connection, dialog_id: int) -> bool:
    """Return whether the dialog currently permits full-history hydration."""
    return conn.execute(_FACT_HYDRATION_ELIGIBILITY_SQL, (dialog_id,)).fetchone() is not None


def rich_composition_hydration_eligible(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Only retained, undeleted unknown compositions in enrolled dialogs need recovery."""
    return (
        conn.execute(
            "SELECT 1 FROM messages m WHERE m.dialog_id=? AND m.message_id=? "
            "AND "
            + _RICH_CURRENT_SQL
            + " AND "
            + _RICH_CONTENT_UNRESOLVED_SQL
            + " AND EXISTS ("
            + _FACT_HYDRATION_ELIGIBILITY_SQL
            + ")",
            (dialog_id, message_id, dialog_id),
        ).fetchone()
        is not None
    )


def rich_composition_automatic_eligible(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Retain partial compositions of any age, and recent unknown compositions."""
    return (
        conn.execute(
            "SELECT 1 FROM messages m WHERE m.dialog_id=? AND m.message_id=? AND " + _RICH_AUTOMATIC_CONTENT_SQL,
            (dialog_id, message_id),
        ).fetchone()
        is not None
    )
