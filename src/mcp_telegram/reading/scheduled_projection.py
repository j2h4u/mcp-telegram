"""SQLite queries and wire mapping for the local scheduled-message mirror."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from ..daemon_message import project_read_message_content
from ..scheduled_contracts import scheduled_message_version
from .query_records import read_message_from_row

SCHEDULED_MESSAGES_TABLE_SQL = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_messages'"
SCHEDULED_MESSAGES_FTS_TABLE_SQL = (
    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_messages_fts'"
)

_SCHEDULED_MESSAGE_COLUMNS_SQL = """
    sm.message_id AS message_id,
    sm.scheduled_at AS sent_at,
    sm.message_state,
    sm.text,
    sm.sender_id,
    sm.sender_first_name,
    sm.media_kind,
    sm.media_payload,
    sm.reply_to_msg_id,
    sm.forum_topic_id,
    0 AS is_deleted,
    NULL AS deleted_at,
    sm.edit_date AS edit_date,
    NULL AS topic_title,
    sm.sender_id AS effective_sender_id,
    sm.is_service,
    sm.out,
    sm.dialog_id,
    NULL AS fwd_from_name,
    sm.post_author,
    NULL AS dialog_name,
    sm.scheduled_at AS scheduled_at,
    sm.published_at AS published_at,
    sm.service_action,
    sm.formatting_entities,
    sm.schedule_repeat_period,
    sm.observation_order,
    sm.visibility,
    sm.unpublished,
    sm.unseen
"""
_SCHEDULED_MESSAGE_SELECT_SQL = "SELECT" + _SCHEDULED_MESSAGE_COLUMNS_SQL
_SCHEDULED_MESSAGE_SEARCH_SELECT_SQL = "SELECT DISTINCT" + _SCHEDULED_MESSAGE_COLUMNS_SQL
_SCHEDULED_MESSAGE_LIST_FROM_SQL = """
FROM scheduled_messages sm
"""
_SCHEDULED_MESSAGE_SEARCH_FROM_SQL = """
FROM scheduled_messages sm
JOIN scheduled_messages_fts sf
  ON sf.dialog_id = sm.dialog_id AND sf.message_id = sm.message_id
"""


class _ScheduledListRequest(Protocol):
    @property
    def dialog_id(self) -> int: ...

    @property
    def limit(self) -> int: ...

    @property
    def direction(self) -> str: ...

    @property
    def anchor_msg_id(self) -> int | None: ...

    @property
    def anchor_sent_at(self) -> int | None: ...

    @property
    def sender_id(self) -> int | None: ...

    @property
    def sender_name(self) -> str | None: ...

    @property
    def topic_id(self) -> int | None: ...

    @property
    def since_utc(self) -> int | None: ...

    @property
    def until_utc(self) -> int | None: ...


def scheduled_messages_available(conn: sqlite3.Connection) -> bool:
    """Return whether both scheduled mirror tables are present."""
    return (
        conn.execute(SCHEDULED_MESSAGES_TABLE_SQL).fetchone() is not None
        and conn.execute(SCHEDULED_MESSAGES_FTS_TABLE_SQL).fetchone() is not None
    )


def scheduled_message_time(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> int | None:
    """Return a scheduled row's timestamp for cursor fallback."""
    row = cast(
        tuple[object] | None,
        conn.execute(
            "SELECT scheduled_at FROM scheduled_messages WHERE dialog_id = ? AND message_id = ?",
            (dialog_id, message_id),
        ).fetchone(),
    )
    if row is None or row[0] is None:
        return None
    return int(cast(int | str, row[0]))


def _escape_like(value: str) -> str:
    table: dict[str, str | int | None] = {"\\": "\\\\", "%": "\\%", "_": "\\_"}
    return value.translate(str.maketrans(table))


def build_scheduled_list_query(
    request: _ScheduledListRequest,
    *,
    scheduled_now: int | None = None,
    anchor_sent_at: int | None = None,
) -> tuple[str, dict[str, object]]:
    """Build the complete, parameterized scheduled-message list query."""
    clauses = [
        "sm.dialog_id = :dialog_id",
        "sm.message_state IN ('scheduled', 'unknown_missing')",
    ]
    params: dict[str, object] = {
        "dialog_id": request.dialog_id,
        "limit": request.limit,
    }
    if request.sender_id is not None:
        clauses.append("sm.sender_id = :filter_sender_id")
        params["filter_sender_id"] = request.sender_id
    elif request.sender_name is not None:
        clauses.append("sm.sender_first_name LIKE :sender_name_pattern ESCAPE '\\' COLLATE NOCASE")
        params["sender_name_pattern"] = "%" + _escape_like(request.sender_name) + "%"
    if request.topic_id is not None:
        clauses.append("sm.forum_topic_id = :topic_id")
        params["topic_id"] = request.topic_id
    since_utc = getattr(request, "since_utc", None)
    until_utc = getattr(request, "until_utc", None)
    if since_utc is not None:
        clauses.append("sm.scheduled_at >= :since_utc")
        params["since_utc"] = since_utc
    if until_utc is not None:
        clauses.append("sm.scheduled_at < :until_utc")
        params["until_utc"] = until_utc
    anchor_clause = _scheduled_list_anchor_clause(request, anchor_sent_at, params)
    if anchor_clause is not None:
        clauses.append(anchor_clause)
    order = "ASC" if request.direction == "oldest" else "DESC"
    sql = (
        _SCHEDULED_MESSAGE_SELECT_SQL
        + _SCHEDULED_MESSAGE_LIST_FROM_SQL
        + "WHERE "
        + " AND ".join(clauses)
        + " ORDER BY sm.scheduled_at "
        + order
        + ", sm.message_id "
        + order
        + " LIMIT :limit"
    )
    return sql, params


def _scheduled_list_anchor_clause(
    request: _ScheduledListRequest,
    anchor_sent_at: int | None,
    params: dict[str, object],
) -> str | None:
    if request.anchor_msg_id is None:
        return None
    params["anchor_msg_id"] = request.anchor_msg_id
    if anchor_sent_at is None:
        if request.direction == "oldest":
            return "(sm.scheduled_at IS NOT NULL OR (sm.scheduled_at IS NULL AND sm.message_id > :anchor_msg_id))"
        return "(sm.scheduled_at IS NULL AND sm.message_id < :anchor_msg_id)"
    params["anchor_at"] = anchor_sent_at
    if request.direction == "oldest":
        return "(sm.scheduled_at > :anchor_at OR (sm.scheduled_at = :anchor_at AND sm.message_id > :anchor_msg_id))"
    return (
        "(sm.scheduled_at < :anchor_at OR sm.scheduled_at IS NULL OR "
        "(sm.scheduled_at = :anchor_at AND sm.message_id < :anchor_msg_id))"
    )


def build_scheduled_global_list_query(  # noqa: PLR0913
    *,
    limit: int,
    direction: str,
    dialog_id: int | None = None,
    anchor_bucket: int | None = None,
    anchor_scheduled_at: int | None = None,
    anchor_dialog_id: int | None = None,
    anchor_message_id: int | None = None,
    sender_id: int | None = None,
    sender_name: str | None = None,
    topic_id: int | None = None,
    since_utc: int | None = None,
    until_utc: int | None = None,
) -> tuple[str, dict[str, object]]:
    """Build the global pending queue keyset query with a stable null-time bucket."""
    clauses = ["sm.message_state IN ('scheduled', 'unknown_missing')"]
    params: dict[str, object] = {"limit": limit}
    if dialog_id is not None:
        clauses.append("sm.dialog_id = :dialog_id")
        params["dialog_id"] = dialog_id
    if sender_id is not None:
        clauses.append("sm.sender_id = :filter_sender_id")
        params["filter_sender_id"] = sender_id
    elif sender_name is not None:
        clauses.append("sm.sender_first_name LIKE :sender_name_pattern ESCAPE '\\' COLLATE NOCASE")
        params["sender_name_pattern"] = "%" + _escape_like(sender_name) + "%"
    if topic_id is not None:
        clauses.append("sm.forum_topic_id = :topic_id")
        params["topic_id"] = topic_id
    if since_utc is not None:
        clauses.append("sm.scheduled_at >= :since_utc")
        params["since_utc"] = since_utc
    if until_utc is not None:
        clauses.append("sm.scheduled_at < :until_utc")
        params["until_utc"] = until_utc
    if anchor_bucket is not None:
        clauses.append(
            _scheduled_global_anchor_clause(
                direction=direction,
                anchor_bucket=anchor_bucket,
                anchor_scheduled_at=anchor_scheduled_at,
                anchor_dialog_id=anchor_dialog_id,
                anchor_message_id=anchor_message_id,
                params=params,
            )
        )
    order = "ASC" if direction == "oldest" else "DESC"
    sql = (
        _SCHEDULED_MESSAGE_SELECT_SQL
        + _SCHEDULED_MESSAGE_LIST_FROM_SQL
        + "WHERE "
        + " AND ".join(clauses)
        + " ORDER BY (sm.scheduled_at IS NOT NULL) "
        + order
        + ", sm.scheduled_at "
        + order
        + ", sm.dialog_id "
        + order
        + ", sm.message_id "
        + order
        + " LIMIT :limit"
    )
    return sql, params


def _scheduled_global_anchor_clause(  # noqa: PLR0913
    *,
    direction: str,
    anchor_bucket: int,
    anchor_scheduled_at: int | None,
    anchor_dialog_id: int | None,
    anchor_message_id: int | None,
    params: dict[str, object],
) -> str:
    if anchor_dialog_id is None or anchor_message_id is None:
        raise ValueError("global scheduled cursor requires dialog and message ids")
    params.update(
        anchor_bucket=anchor_bucket,
        anchor_scheduled_at=anchor_scheduled_at,
        anchor_dialog_id=anchor_dialog_id,
        anchor_message_id=anchor_message_id,
    )
    comparison = ">" if direction == "oldest" else "<"
    dialog_after = "sm.dialog_id > :anchor_dialog_id" if direction == "oldest" else "sm.dialog_id < :anchor_dialog_id"
    message_after = (
        "sm.message_id > :anchor_message_id" if direction == "oldest" else "sm.message_id < :anchor_message_id"
    )
    time_after = (
        "sm.scheduled_at > :anchor_scheduled_at" if direction == "oldest" else "sm.scheduled_at < :anchor_scheduled_at"
    )
    same_bucket_after = "( " + dialog_after + " OR (sm.dialog_id = :anchor_dialog_id AND " + message_after + "))"
    if anchor_bucket == 0:
        same_bucket = same_bucket_after
    else:
        same_bucket = (
            "(" + time_after + " OR (sm.scheduled_at = :anchor_scheduled_at AND " + same_bucket_after[1:-1] + "))"
        )
    return (
        "((sm.scheduled_at IS NOT NULL) "
        + comparison
        + " :anchor_bucket OR ((sm.scheduled_at IS NOT NULL) = :anchor_bucket AND "
        + same_bucket
        + "))"
    )


def build_scheduled_search_query(  # noqa: PLR0913
    *,
    dialog_id: int,
    query: str,
    limit: int,
    offset: int,
    scheduled_now: int | None = None,
    since_utc: int | None = None,
    until_utc: int | None = None,
) -> tuple[str, dict[str, object]]:
    """Build the complete, parameterized scheduled-message search query."""
    clauses = [
        "sm.message_state IN ('scheduled', 'unknown_missing')",
        "scheduled_messages_fts MATCH :query",
    ]
    params: dict[str, object] = {
        "query": query,
        "limit": limit,
        "offset": offset,
    }
    if since_utc is not None:
        clauses.append("sm.scheduled_at >= :since_utc")
        params["since_utc"] = since_utc
    if until_utc is not None:
        clauses.append("sm.scheduled_at < :until_utc")
        params["until_utc"] = until_utc
    if dialog_id:
        clauses.append("sm.dialog_id = :dialog_id")
        params["dialog_id"] = dialog_id
    sql = (
        _SCHEDULED_MESSAGE_SEARCH_SELECT_SQL
        + _SCHEDULED_MESSAGE_SEARCH_FROM_SQL
        + "WHERE "
        + " AND ".join(clauses)
        + " ORDER BY sm.scheduled_at ASC, sm.message_id ASC, sm.dialog_id ASC LIMIT :limit OFFSET :offset"
    )
    return sql, params


def scheduled_row_to_wire(
    row: object,
    *,
    inclusion_basis: tuple[str, ...] | list[str] | None = None,
    account_id: int | None = None,
) -> dict[str, object]:
    """Normalize a scheduled DB row to the common message/lifecycle wire shape."""
    message = read_message_from_row(cast(Mapping[str, object], row))
    message = project_read_message_content(message)
    item = dataclasses.asdict(message)
    scheduled_at = _optional_int(_row_value(row, "scheduled_at"))
    item["sent_at"] = scheduled_at
    observation_order = _optional_int(_row_value(row, "observation_order"))
    expected_version = None
    if account_id is not None and observation_order is not None and observation_order > 0:
        expected_version = scheduled_message_version(account_id, message.dialog_id, message.id, observation_order)
    item.update(
        {
            "message_state": _row_value(row, "message_state") or "scheduled",
            "visibility": _row_value(row, "visibility") or "author_only",
            "unpublished": bool(_row_value(row, "unpublished") if _row_value(row, "unpublished") is not None else True),
            "published": False,
            "unseen": bool(_row_value(row, "unseen") if _row_value(row, "unseen") is not None else True),
            "scheduled_at": scheduled_at,
            "published_at": _optional_int(_row_value(row, "published_at")),
            "repeat_period": _optional_int(_row_value(row, "schedule_repeat_period")),
            "expected_version": expected_version,
            "inclusion_basis": list(inclusion_basis) if inclusion_basis is not None else None,
        }
    )
    return item


def _optional_int(value: object | None) -> int | None:
    if value is None:
        return None
    return int(cast(int | str, value))


def _row_value(row: object, key: str) -> object | None:
    try:
        return cast(object | None, row[key])  # type: ignore[index]
    except AttributeError, IndexError, KeyError, TypeError:
        return None


def scheduled_summary_by_dialog(
    conn: sqlite3.Connection, *, scheduled_now: int, dialog_ids: Sequence[int] | None = None
) -> dict[int, tuple[int, int | None, int]]:
    """Summarize future rows separately from due or unresolved pending rows."""
    if conn.execute(SCHEDULED_MESSAGES_TABLE_SQL).fetchone() is None:
        return {}
    rows = cast(
        list[tuple[object, object, object, object]],
        conn.execute(
            """
            SELECT dialog_id,
                SUM(CASE WHEN message_state='scheduled' AND scheduled_at > :scheduled_now THEN 1 ELSE 0 END),
                MIN(CASE WHEN message_state='scheduled' AND scheduled_at > :scheduled_now THEN scheduled_at END),
                SUM(CASE WHEN message_state='unknown_missing' OR
                    (message_state='scheduled' AND (scheduled_at IS NULL OR scheduled_at <= :scheduled_now))
                    THEN 1 ELSE 0 END)
            FROM scheduled_messages
            WHERE message_state IN ('scheduled','unknown_missing')
              AND (:dialog_ids IS NULL OR dialog_id IN (SELECT CAST(value AS INTEGER) FROM json_each(:dialog_ids)))
            GROUP BY dialog_id
            """,
            {"scheduled_now": scheduled_now, "dialog_ids": json.dumps(dialog_ids) if dialog_ids is not None else None},
        ).fetchall(),
    )
    return {
        int(cast(int | str, row[0])): (
            int(cast(int | str, row[1] or 0)),
            _optional_int(row[2]),
            int(cast(int | str, row[3] or 0)),
        )
        for row in rows
    }
