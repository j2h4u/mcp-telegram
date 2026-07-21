"""SQLite queries and wire mapping for the local scheduled-message mirror."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from .daemon_message_queries import _read_message_from_row

SCHEDULED_MESSAGES_TABLE_SQL = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_messages'"
SCHEDULED_MESSAGES_FTS_TABLE_SQL = (
    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_messages_fts'"
)

_SCHEDULED_MESSAGE_SELECT_SQL = """
SELECT
    sm.message_id AS message_id,
    sm.scheduled_at AS sent_at,
    sm.text,
    sm.sender_id,
    sm.sender_first_name,
    sm.media_description,
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
    d.name AS dialog_name,
    sm.scheduled_at AS scheduled_at,
    sm.published_at AS published_at
"""
_SCHEDULED_MESSAGE_LIST_FROM_SQL = """
FROM scheduled_messages sm
LEFT JOIN dialogs d ON d.dialog_id = sm.dialog_id
"""
_SCHEDULED_MESSAGE_SEARCH_FROM_SQL = """
FROM scheduled_messages sm
JOIN scheduled_messages_fts sf
  ON sf.dialog_id = sm.dialog_id AND sf.message_id = sm.message_id
LEFT JOIN dialogs d ON d.dialog_id = sm.dialog_id
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
    scheduled_now: int,
    anchor_sent_at: int | None = None,
) -> tuple[str, dict[str, object]]:
    """Build the complete, parameterized scheduled-message list query."""
    clauses = [
        "sm.dialog_id = :dialog_id",
        "sm.message_state = 'scheduled'",
        "sm.scheduled_at > :scheduled_now",
    ]
    params: dict[str, object] = {
        "dialog_id": request.dialog_id,
        "scheduled_now": scheduled_now,
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
    if request.anchor_msg_id is not None:
        anchor_at = 0 if anchor_sent_at is None else anchor_sent_at
        params["anchor_at"] = anchor_at
        params["anchor_msg_id"] = request.anchor_msg_id
        if request.direction == "oldest":
            clauses.append(
                "(sm.scheduled_at > :anchor_at OR (sm.scheduled_at = :anchor_at AND sm.message_id > :anchor_msg_id))"
            )
        else:
            clauses.append(
                "(sm.scheduled_at < :anchor_at OR (sm.scheduled_at = :anchor_at AND sm.message_id < :anchor_msg_id))"
            )
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


def build_scheduled_search_query(  # noqa: PLR0913
    *,
    dialog_id: int,
    own_dialog_ids: Sequence[int] | None,
    query: str,
    limit: int,
    offset: int,
    scheduled_now: int,
    since_utc: int | None = None,
    until_utc: int | None = None,
) -> tuple[str, dict[str, object]]:
    """Build the complete, parameterized scheduled-message search query."""
    clauses = [
        "sm.message_state = 'scheduled'",
        "scheduled_messages_fts MATCH :query",
        "sm.scheduled_at > :scheduled_now",
    ]
    params: dict[str, object] = {
        "query": query,
        "scheduled_now": scheduled_now,
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
    elif own_dialog_ids is not None:
        if not own_dialog_ids:
            clauses.append("0")
        else:
            names = [":own_scope_" + str(index) for index in range(len(own_dialog_ids))]
            clauses.append("sm.dialog_id IN (" + ", ".join(names) + ")")
            params.update({"own_scope_" + str(index): value for index, value in enumerate(own_dialog_ids)})
    sql = (
        _SCHEDULED_MESSAGE_SELECT_SQL
        + _SCHEDULED_MESSAGE_SEARCH_FROM_SQL
        + "WHERE "
        + " AND ".join(clauses)
        + " ORDER BY sm.scheduled_at ASC, sm.message_id ASC LIMIT :limit OFFSET :offset"
    )
    return sql, params


def scheduled_row_to_wire(
    row: object,
    *,
    inclusion_basis: Sequence[str],
) -> dict[str, object]:
    """Normalize a scheduled DB row to the common message/lifecycle wire shape."""
    message = _read_message_from_row(cast(Mapping[str, object], row))
    item = dataclasses.asdict(message)
    item.update(
        {
            "message_state": "scheduled",
            "unpublished": True,
            "unseen": True,
            "scheduled_at": message.sent_at,
            "published_at": _optional_int(_row_value(row, "published_at")),
            "inclusion_basis": list(inclusion_basis),
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


def scheduled_summary_by_dialog(conn: sqlite3.Connection, *, scheduled_now: int) -> dict[int, tuple[int, int | None]]:
    """Summarize future scheduled rows without reading sent history."""
    if conn.execute(SCHEDULED_MESSAGES_TABLE_SQL).fetchone() is None:
        return {}
    rows = cast(
        list[tuple[object, object, object]],
        conn.execute(
            """
            SELECT dialog_id, COUNT(*) AS scheduled_count, MIN(scheduled_at) AS next_scheduled_at
            FROM scheduled_messages
            WHERE message_state = 'scheduled' AND scheduled_at > :scheduled_now
            GROUP BY dialog_id
            """,
            {"scheduled_now": scheduled_now},
        ).fetchall(),
    )
    return {int(cast(int | str, row[0])): (int(cast(int | str, row[1])), _optional_int(row[2])) for row in rows}
