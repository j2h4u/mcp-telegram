"""SQLite read-state queries and row mapping for direct-message dialogs."""

from __future__ import annotations

import sqlite3
from typing import Literal, cast

from .models import DialogType, ReadState

_DIALOG_TYPE_SQL = "SELECT type FROM entities WHERE id = ?"
_READ_STATE_SQL = """
WITH sd AS (
  SELECT read_inbox_max_id AS in_c, read_outbox_max_id AS out_c
  FROM synced_dialogs WHERE dialog_id = :dialog_id
)
SELECT
  (SELECT in_c FROM sd)  AS in_cursor,
  (SELECT out_c FROM sd) AS out_cursor,
  SUM(CASE WHEN m.out = 0 AND m.message_id > COALESCE((SELECT in_c FROM sd), -1)  THEN 1 ELSE 0 END) AS in_cnt,
  SUM(CASE WHEN m.out = 1 AND m.message_id > COALESCE((SELECT out_c FROM sd), -1) THEN 1 ELSE 0 END) AS out_cnt,
  MIN(CASE WHEN m.out = 0 AND m.message_id > COALESCE((SELECT in_c FROM sd), -1)  THEN m.sent_at END) AS in_min,
  MIN(CASE WHEN m.out = 1 AND m.message_id > COALESCE((SELECT out_c FROM sd), -1) THEN m.sent_at END) AS out_min
FROM messages m
WHERE m.dialog_id = :dialog_id AND m.is_deleted = 0 AND m.is_service = 0
"""


def _dialog_type_from_db(conn: sqlite3.Connection, dialog_id: int) -> str:
    """Return the cached entity type for a dialog, or ``Unknown``."""
    row = cast(tuple[object] | None, conn.execute(_DIALOG_TYPE_SQL, (dialog_id,)).fetchone())
    if row is None:
        return "Unknown"
    return str(row[0])


def _read_state_for_dialog(conn: sqlite3.Connection, dialog_id: int, dialog_type: str) -> ReadState | None:
    """Compute bidirectional read state for a direct-message dialog."""
    if DialogType.parse(dialog_type) != DialogType.USER:
        return None

    row = cast(
        tuple[object, object, object, object, object, object] | None,
        conn.execute(_READ_STATE_SQL, {"dialog_id": dialog_id}).fetchone(),
    )
    # Aggregate functions return one row even when no messages match.
    read_inbox_max_id = cast(int | None, row[0]) if row is not None else None
    read_outbox_max_id = cast(int | None, row[1]) if row is not None else None
    agg_row = (
        cast(tuple[int | None, int | None, int | None, int | None], (row[2], row[3], row[4], row[5]))
        if row is not None
        else (None, None, None, None)
    )
    in_cnt = int(agg_row[0] or 0)
    out_cnt = int(agg_row[1] or 0)
    in_min = cast(int | None, agg_row[2])
    out_min = cast(int | None, agg_row[3])

    def _state(cursor: int | None, unread_count: int) -> Literal["populated", "null", "all_read"]:
        if cursor is None:
            return "null"
        if unread_count == 0:
            return "all_read"
        return "populated"

    rs: ReadState = {
        "inbox_unread_count": in_cnt,
        "inbox_cursor_state": _state(read_inbox_max_id, in_cnt),
        "outbox_unread_count": out_cnt,
        "outbox_cursor_state": _state(read_outbox_max_id, out_cnt),
    }
    if read_inbox_max_id is not None:
        rs["inbox_max_id_anchor"] = int(read_inbox_max_id)
    if read_outbox_max_id is not None:
        rs["outbox_max_id_anchor"] = int(read_outbox_max_id)
    if in_cnt > 0 and in_min is not None:
        rs["inbox_oldest_unread_date"] = int(in_min)
    if out_cnt > 0 and out_min is not None:
        rs["outbox_oldest_unread_date"] = int(out_min)
    return rs
