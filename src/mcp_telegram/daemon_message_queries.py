"""SQLite read queries and row mappers for stored Telegram messages."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Protocol, cast

from .models import ReadMessage

logger = logging.getLogger("mcp_telegram.daemon_api")


class _ListMessagesDbRequest(Protocol):
    dialog_id: int
    limit: int
    self_id: int | None
    direction: str
    anchor_msg_id: int | None
    anchor_sent_at: int | None
    sender_id: int | None
    sender_name: str | None
    topic_id: int | None
    unread_after_id: int | None
    since_utc: int | None
    until_utc: int | None


def _row_value(row: object, key: str, default: object | None = None) -> object | None:
    try:
        return cast(object | None, row[key])  # type: ignore[index]
    except AttributeError, IndexError, KeyError, TypeError:
        return default


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(cast(int | str, value))
    except TypeError, ValueError:
        return default


def _read_message_from_row(row: Mapping[str, object], *, reactions_display: str = "") -> ReadMessage:
    return ReadMessage(
        message_id=_coerce_int(row["message_id"], 0),
        sent_at=_coerce_int(row["sent_at"], 0),
        dialog_id=_coerce_int(row["dialog_id"], 0),
        text=cast(str | None, _row_value(row, "text")),
        sender_id=cast(int | None, _row_value(row, "sender_id")),
        sender_first_name=cast(str | None, _row_value(row, "sender_first_name")),
        media_description=cast(str | None, _row_value(row, "media_description")),
        reply_to_msg_id=cast(int | None, _row_value(row, "reply_to_msg_id")),
        forum_topic_id=cast(int | None, _row_value(row, "forum_topic_id")),
        is_deleted=_coerce_int(cast(object, _row_value(row, "is_deleted", 0)), 0),
        deleted_at=cast(int | None, _row_value(row, "deleted_at")),
        edit_date=cast(int | None, _row_value(row, "edit_date")),
        topic_title=cast(str | None, _row_value(row, "topic_title")),
        effective_sender_id=cast(int | None, _row_value(row, "effective_sender_id")),
        is_service=_coerce_int(cast(object, _row_value(row, "is_service", 0)), 0),
        out=_coerce_int(cast(object, _row_value(row, "out", 0)), 0),
        fwd_from_name=cast(str | None, _row_value(row, "fwd_from_name")),
        post_author=cast(str | None, _row_value(row, "post_author")),
        read_at=cast(int | None, _row_value(row, "read_at")),
        reactions_display=reactions_display,
        dialog_name=cast(str | None, _row_value(row, "dialog_name")),
    )


# Phase 39.1-02: effective_sender_id collapses DM direction into a concrete user id.
# For DM outgoing rows (sender_id IS NULL, out=1) -> self_id (from :self_id parameter).
# For DM incoming rows (sender_id IS NULL, out=0) -> dialog_id (the peer).
# For service messages (is_service=1) or group unknown senders -> NULL (render as System/unknown).
# Interpolated into every read-path SELECT; every caller MUST bind :self_id.
_EFFECTIVE_SENDER_ID_EXPR = (
    "COALESCE("
    "m.sender_id, "
    "CASE "
    "WHEN m.is_service = 1 THEN NULL "
    "WHEN m.dialog_id > 0 AND m.out = 1 THEN :self_id "
    "WHEN m.dialog_id > 0 AND m.out = 0 THEN m.dialog_id "
    "ELSE NULL "
    "END"
    ")"
)
EFFECTIVE_SENDER_ID_SQL = _EFFECTIVE_SENDER_ID_EXPR + " AS effective_sender_id"

# Shared sender_first_name projection with dual JOINs: resolve name either from
# the raw sender_id OR, when sender_id IS NULL, from the effective_sender_id (peer
# first_name for DM incoming; self name for DM outgoing - though "Я" wins at render).
_SENDER_FIRST_NAME_SQL = "COALESCE(e_raw.name, e_eff.name, m.sender_first_name) AS sender_first_name"
_SENDER_NAME_FILTER_SQL = "COALESCE(m.sender_first_name, e_raw.name, e_eff.name)"
_SENDER_ENTITY_JOINS_SQL = (
    "LEFT JOIN entities e_raw ON e_raw.id = m.sender_id "
    f"LEFT JOIN entities e_eff ON e_eff.id = {_EFFECTIVE_SENDER_ID_EXPR} "
)

_SELECT_MESSAGES_SQL = (
    f"SELECT m.message_id, m.sent_at, m.text, m.sender_id, "
    f"{_SENDER_FIRST_NAME_SQL}, "
    f"m.media_description, m.reply_to_msg_id, m.forum_topic_id, "
    f"m.is_deleted, m.deleted_at, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out, m.dialog_id "
    f"FROM messages m "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"WHERE m.dialog_id = :dialog_id AND m.is_deleted = 0 "
    f"ORDER BY m.sent_at DESC LIMIT :limit"
)

_SELECT_FTS_SQL = (
    f"SELECT f.message_id, m.text, "
    f"{_SENDER_FIRST_NAME_SQL}, "
    f"m.sent_at, m.media_description, m.reply_to_msg_id, m.sender_id, m.forum_topic_id, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out, m.dialog_id "
    f"FROM messages_fts f "
    f"JOIN messages m ON m.dialog_id = f.dialog_id AND m.message_id = f.message_id "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"WHERE messages_fts MATCH :query AND f.dialog_id = :dialog_id "
    f"AND (:since_utc IS NULL OR m.sent_at >= :since_utc) "
    f"AND (:until_utc IS NULL OR m.sent_at < :until_utc) "
    f"ORDER BY rank LIMIT :limit OFFSET :offset"
)

# _SELECT_FTS_ALL_SQL uses aliases e_raw/e_eff for sender entity JOINs (matching the
# shared helpers) and de for dialog name entity JOIN.
_SELECT_FTS_ALL_SQL = (
    f"SELECT f.message_id, m.text, "
    f"{_SENDER_FIRST_NAME_SQL}, "
    f"m.sent_at, m.media_description, m.reply_to_msg_id, m.sender_id, m.forum_topic_id, "
    f"f.dialog_id, COALESCE(de.name, CAST(f.dialog_id AS TEXT)) AS dialog_name, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out "
    f"FROM messages_fts f "
    f"JOIN messages m ON m.dialog_id = f.dialog_id AND m.message_id = f.message_id "
    f"LEFT JOIN entities de ON de.id = f.dialog_id "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"WHERE messages_fts MATCH :query "
    f"AND (:since_utc IS NULL OR m.sent_at >= :since_utc) "
    f"AND (:until_utc IS NULL OR m.sent_at < :until_utc) "
    f"ORDER BY rank LIMIT :limit OFFSET :offset"
)

_FETCH_UNREAD_MESSAGES_SQL = (
    f"SELECT m.message_id, m.sent_at, m.text, m.sender_id, "
    f"{_SENDER_FIRST_NAME_SQL}, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out, m.dialog_id "
    f"FROM messages m "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"WHERE m.dialog_id = :dialog_id AND m.message_id > :after_msg_id AND m.is_deleted = 0 "
    f'AND m."out" = 0 AND m.is_service = 0 '
    f"ORDER BY m.message_id ASC LIMIT :limit"
)

# Base SELECT shared by _build_list_messages_query and _list_messages_context_window.
# Appends dialog_id=:dialog_id and is_deleted=0 guards; callers add further conditions
# (appended as " AND ..." with named params or positional - see _build_list_messages_query).
# Callers MUST bind :self_id (used by EFFECTIVE_SENDER_ID_SQL CASE expression).
_LIST_MESSAGES_BASE_SQL = (
    f"SELECT m.message_id, m.sent_at, m.text, m.sender_id, "
    f"{_SENDER_FIRST_NAME_SQL}, "
    f"m.media_description, m.reply_to_msg_id, m.forum_topic_id, "
    f"m.is_deleted, m.deleted_at, "
    f"COALESCE("
    f"  (SELECT MAX(mv.edit_date) FROM message_versions mv "
    f"   WHERE mv.dialog_id = m.dialog_id AND mv.message_id = m.message_id), "
    f"  m.edit_date"
    f") AS edit_date, "
    f"COALESCE(tm.title, CASE WHEN m.forum_topic_id = 1 THEN 'General' END) AS topic_title, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out, m.dialog_id, "
    f"mf.fwd_from_name, m.post_author "
    f"FROM messages m "
    f"LEFT JOIN topic_metadata tm "
    f"  ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"LEFT JOIN message_forwards mf ON mf.dialog_id = m.dialog_id AND mf.message_id = m.message_id "
    f"WHERE m.dialog_id = :dialog_id AND m.is_deleted = 0"
)


def _assert_select_columns_match_read_message() -> None:
    """Verify SELECT aliases in _LIST_MESSAGES_BASE_SQL cover ReadMessage fields."""
    from dataclasses import fields as dc_fields

    expected = frozenset(
        f.name
        for f in dc_fields(ReadMessage)
        if f.name not in {"reactions_display", "dialog_name", "read_at", "reaction_events", "reaction_events_status"}
    )
    aliases = frozenset(re.findall(r"\bAS\s+(\w+)", _LIST_MESSAGES_BASE_SQL))
    bare = frozenset(re.findall(r"\b(?:m|mf)\.(\w+)\b", _LIST_MESSAGES_BASE_SQL))
    found = aliases | bare
    missing = expected - found
    extra = found - expected
    assert not missing and not extra, f"SELECT/ReadMessage field mismatch - missing: {missing}, extra: {extra}"


_assert_select_columns_match_read_message()


def _apply_list_messages_anchor_filter(
    sql: str,
    params: dict[str, object],
    req: _ListMessagesDbRequest,
) -> tuple[str, dict[str, object]]:
    anchor_msg_id = req.anchor_msg_id
    if anchor_msg_id is None:
        return sql, params

    anchor_sent_at = getattr(req, "anchor_sent_at", None)
    if anchor_sent_at is not None:
        if req.direction == "oldest":
            sql += (
                " AND (m.sent_at > :anchor_sent_at OR (m.sent_at = :anchor_sent_at AND m.message_id > :anchor_msg_id))"
            )
        else:
            sql += (
                " AND (m.sent_at < :anchor_sent_at OR (m.sent_at = :anchor_sent_at AND m.message_id < :anchor_msg_id))"
            )
        params["anchor_sent_at"] = anchor_sent_at
    elif req.direction == "oldest":
        sql += " AND m.message_id > :anchor_msg_id"
    else:
        sql += " AND m.message_id < :anchor_msg_id"
    params["anchor_msg_id"] = anchor_msg_id
    return sql, params


def _build_list_messages_query(req: _ListMessagesDbRequest) -> tuple[str, dict[str, object]]:
    """Build a parameterized SELECT for list_messages against sync.db."""
    dialog_id = req.dialog_id
    limit = req.limit
    self_id = getattr(req, "self_id", None)
    direction = req.direction
    anchor_msg_id = req.anchor_msg_id
    sender_id = req.sender_id
    sender_name = req.sender_name
    topic_id = req.topic_id
    unread_after_id = req.unread_after_id
    since_utc = getattr(req, "since_utc", None)
    until_utc = getattr(req, "until_utc", None)

    params: dict[str, object] = {
        "dialog_id": dialog_id,
        "limit": limit,
        "self_id": self_id,
    }
    sql = _LIST_MESSAGES_BASE_SQL

    if since_utc is not None:
        sql += " AND m.sent_at >= :since_utc"
        params["since_utc"] = since_utc
    if until_utc is not None:
        sql += " AND m.sent_at < :until_utc"
        params["until_utc"] = until_utc

    if sender_id is not None:
        sql += f" AND {_EFFECTIVE_SENDER_ID_EXPR} = :filter_sender_id"
        params["filter_sender_id"] = sender_id
    elif sender_name is not None:
        # Prefer the stored historical name, but fall back to resolved sender entities for DM rows
        # whose raw sender fields are intentionally NULL.
        sql += f" AND {_SENDER_NAME_FILTER_SQL} LIKE :sender_name_pattern ESCAPE '\\' COLLATE NOCASE"
        escaped = sender_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["sender_name_pattern"] = f"%{escaped}%"

    if topic_id is not None:
        sql += " AND m.forum_topic_id = :topic_id"
        params["topic_id"] = topic_id

    if unread_after_id is not None:
        sql += " AND m.message_id > :unread_after_id"
        params["unread_after_id"] = unread_after_id

    sql, params = _apply_list_messages_anchor_filter(sql, params, req)

    if direction == "oldest":
        sql += " ORDER BY m.message_id ASC"
    else:
        sql += " ORDER BY m.message_id DESC"

    sql += " LIMIT :limit"

    logger.debug(
        "list_messages_query filters=%s param_count=%d direction=%s",
        "+".join(
            f
            for f, v in [
                ("sender_id", sender_id),
                ("sender_name", sender_name),
                ("topic_id", topic_id),
                ("unread_after_id", unread_after_id),
                ("anchor", anchor_msg_id),
                ("since_utc", since_utc),
                ("until_utc", until_utc),
            ]
            if v is not None
        )
        or "none",
        len(params),
        direction,
    )
    return sql, params
