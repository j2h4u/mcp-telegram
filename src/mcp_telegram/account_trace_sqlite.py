"""Read-only SQLite access for Account Trace.

This module deliberately contains no Telegram calls, local writes, response
projection, or coverage interpretation.  Its callers retain those policies.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict, cast

_ENTITY_BY_USERNAME_SQL = "SELECT id, name, username, name_normalized FROM entities WHERE username = ? COLLATE NOCASE"
_TRACE_ACCOUNT_BY_ID_SQL = "SELECT id, name, username, name_normalized FROM entities WHERE id = ?"
_TRACE_ACCOUNT_NAMES_SQL = (
    "SELECT id, name FROM entities "
    "WHERE id > 0 AND name IS NOT NULL "
    "AND ((type IN ('User', 'Bot', 'user', 'bot') AND updated_at > ?) "
    "OR (type NOT IN ('User', 'Bot', 'user', 'bot') AND updated_at > ?))"
)
_TRACE_ACCOUNT_NAMES_NORMALIZED_SQL = (
    "SELECT id, name_normalized FROM entities "
    "WHERE id > 0 AND name_normalized IS NOT NULL "
    "AND ((type IN ('User', 'Bot', 'user', 'bot') AND updated_at > ?) "
    "OR (type NOT IN ('User', 'Bot', 'user', 'bot') AND updated_at > ?))"
)

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
TRACE_MESSAGE_COMPARE_FIELDS = (
    "dialog_id",
    "message_id",
    "sent_at",
    "text",
    "sender_id",
    "sender_first_name",
    "media_kind",
    "reply_to_msg_id",
    "reply_count",
    "forum_topic_id",
    "edit_date",
    "grouped_id",
    "reply_to_peer_id",
    "out",
    "is_service",
    "post_author",
    "is_deleted",
)


@dataclass(frozen=True, slots=True)
class TraceMessageQueryRequest:
    target_user_id: int
    self_id: int | None
    limit: int
    post_author_aliases: list[str] | None = None
    exact_dialog_id: int | None = None
    exact_topic_id: int | None = None
    sent_after_ts: int | None = None
    sent_before_ts: int | None = None
    navigation: dict[str, int] | None = None
    scope_dialog_ids: list[int] | None = None


class TraceDialogMetadata(TypedDict):
    dialog_type: str
    status: str
    hidden: bool


def _rows(cursor: sqlite3.Cursor) -> list[object]:
    return [cast(object, row) for row in cast(Sequence[object], cursor.fetchall())]


def _one(cursor: sqlite3.Cursor) -> object | None:
    return cast(object | None, cursor.fetchone())


def _sequence(row: object) -> Sequence[object]:
    return cast(Sequence[object], row)


def _mapping(row: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], row)


def _dict(row: object) -> dict[str, object]:
    if isinstance(row, sqlite3.Row):
        return dict(zip(row.keys(), row, strict=True))
    return dict(_mapping(row))


def _int(value: object) -> int:
    return int(cast(int | str, value))


def _pair(row: object) -> tuple[object, object]:
    values = _sequence(row)
    return values[0], values[1]


def evidence_page(conn: sqlite3.Connection, request: TraceMessageQueryRequest) -> list[Mapping[str, object]]:
    """Execute the ordered Account Trace evidence query, including LIMIT + 1."""
    sql, params = build_evidence_query(request)
    return [cast(Mapping[str, object], row) for row in _rows(conn.execute(sql, params))]


def build_evidence_query(request: TraceMessageQueryRequest) -> tuple[str, dict[str, object]]:
    """Build the canonical ordered Account Trace evidence query."""
    params: dict[str, object] = {
        "target_user_id": request.target_user_id,
        "self_id": request.self_id,
        "limit": request.limit,
    }
    sql = (
        "SELECT m.dialog_id, m.message_id, m.sent_at, m.text, m.sender_id, "
        "m.media_kind, m.media_payload, m.forum_topic_id AS topic_id, "
        "COALESCE(d.name, e_dialog.name, CAST(m.dialog_id AS TEXT)) AS dialog_title, "
        "COALESCE(d.type, e_dialog.type) AS dialog_type, "
        "COALESCE(tm.title, CASE WHEN m.forum_topic_id = 1 THEN 'General' END) AS topic_title, "
        "m.post_author AS author_signature, "
        f"{EFFECTIVE_SENDER_ID_SQL}, "
        "CASE "
        f"WHEN {_EFFECTIVE_SENDER_ID_EXPR} = :target_user_id THEN 'effective_sender_id' "
        "ELSE 'post_author_signature' END AS authorship_basis "
        "FROM messages m "
        "LEFT JOIN dialogs d ON d.dialog_id = m.dialog_id "
        "LEFT JOIN entities e_dialog ON e_dialog.id = m.dialog_id "
        "LEFT JOIN topic_metadata tm ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
        "WHERE m.is_deleted = 0 AND m.is_service = 0"
    )
    predicates = [f"{_EFFECTIVE_SENDER_ID_EXPR} = :target_user_id"]
    aliases = request.post_author_aliases or []
    if aliases:
        predicates.append(
            f"m.post_author IN ({', '.join(f':post_author_alias_{index}' for index in range(len(aliases)))})"
        )
        for index, alias in enumerate(aliases):
            params[f"post_author_alias_{index}"] = alias
    sql += f" AND ({' OR '.join(predicates)})"
    if request.scope_dialog_ids:
        placeholders = [f":scope_{index}" for index in range(len(request.scope_dialog_ids))]
        sql += f" AND m.dialog_id IN ({', '.join(placeholders)})"
        params.update({f"scope_{index}": dialog_id for index, dialog_id in enumerate(request.scope_dialog_ids)})
    elif request.exact_dialog_id is not None:
        sql += " AND m.dialog_id = :exact_dialog_id"
        params["exact_dialog_id"] = request.exact_dialog_id
    if request.exact_topic_id is not None:
        sql += " AND m.forum_topic_id = :exact_topic_id"
        params["exact_topic_id"] = request.exact_topic_id
    if request.sent_after_ts is not None:
        sql += " AND m.sent_at >= :sent_after"
        params["sent_after"] = request.sent_after_ts
    if request.sent_before_ts is not None:
        sql += " AND m.sent_at <= :sent_before"
        params["sent_before"] = request.sent_before_ts
    if request.navigation is not None:
        sql += " AND (m.sent_at < :nav_sent_at OR (m.sent_at = :nav_sent_at AND m.dialog_id < :nav_dialog_id) OR (m.sent_at = :nav_sent_at AND m.dialog_id = :nav_dialog_id AND m.message_id < :nav_message_id))"
        params.update(
            {
                "nav_sent_at": request.navigation["sent_at"],
                "nav_dialog_id": request.navigation["dialog_id"],
                "nav_message_id": request.navigation["message_id"],
            }
        )
    return sql + " ORDER BY m.sent_at DESC, m.dialog_id DESC, m.message_id DESC LIMIT :limit", params


def account_by_id(conn: sqlite3.Connection, account_id: int) -> Mapping[str, object] | None:
    row = _one(conn.execute(_TRACE_ACCOUNT_BY_ID_SQL, (account_id,)))
    return None if row is None else _mapping(row)


def account_by_username(conn: sqlite3.Connection, username: str) -> Mapping[str, object] | None:
    row = _one(conn.execute(_ENTITY_BY_USERNAME_SQL, (username,)))
    return None if row is None else _mapping(row)


def account_directory_names(
    conn: sqlite3.Connection, *, user_after: int, group_after: int
) -> tuple[list[tuple[object, object]], list[tuple[object, object]]]:
    names = [_pair(row) for row in _rows(conn.execute(_TRACE_ACCOUNT_NAMES_SQL, (user_after, group_after)))]
    normalized = [
        _pair(row) for row in _rows(conn.execute(_TRACE_ACCOUNT_NAMES_NORMALIZED_SQL, (user_after, group_after)))
    ]
    return names, normalized


def coverage_fragments(
    conn: sqlite3.Connection,
    *,
    target_user_id: int,
    exact_dialog_id: int | None = None,
    exact_topic_id: int | None = None,
    coverage_kind: str = "authored_message",
) -> list[dict[str, object]]:
    sql = "SELECT target_user_id, dialog_id, topic_id, coverage_kind, status, fetched_at, checkpoint, last_error, next_retry_at, created_at, updated_at FROM trace_coverage_fragments WHERE target_user_id = :target_user_id AND coverage_kind = :coverage_kind"
    params: dict[str, object] = {"target_user_id": target_user_id, "coverage_kind": coverage_kind}
    if exact_dialog_id is not None:
        sql += " AND dialog_id = :exact_dialog_id"
        params["exact_dialog_id"] = exact_dialog_id
    if exact_topic_id is not None:
        sql += " AND topic_id = :exact_topic_id"
        params["exact_topic_id"] = exact_topic_id
    return [_dict(row) for row in _rows(conn.execute(sql, params))]


def fragment_next_retry_at(
    conn: sqlite3.Connection,
    *,
    target_user_id: int,
    dialog_id: int,
    topic_id: int | None,
) -> int | None:
    row = _one(
        conn.execute(
            "SELECT next_retry_at FROM trace_coverage_fragments WHERE target_user_id = ? AND dialog_id = ? AND topic_id = ? AND coverage_kind = 'authored_message'",
            (target_user_id, dialog_id, 0 if topic_id is None else topic_id),
        )
    )
    if row is None or _sequence(row)[0] is None:
        return None
    return _int(_sequence(row)[0])


def dialog_statuses(conn: sqlite3.Connection, dialog_ids: set[int]) -> dict[int, str | None]:
    if not dialog_ids:
        return {}
    rows = _rows(
        conn.execute(
            f"SELECT dialog_id, status FROM synced_dialogs WHERE dialog_id IN ({','.join('?' * len(dialog_ids))})",
            tuple(dialog_ids),
        )
    )
    result = {_int(_sequence(row)[0]): str(_sequence(row)[1]) for row in rows}
    return {dialog_id: result.get(dialog_id) for dialog_id in dialog_ids}


def access_lost_dialog_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        _int(_sequence(row)[0])
        for row in _rows(conn.execute("SELECT dialog_id FROM synced_dialogs WHERE status = 'access_lost'"))
    }


def hidden_dialog_ids(conn: sqlite3.Connection) -> set[int]:
    return {_int(_sequence(row)[0]) for row in _rows(conn.execute("SELECT dialog_id FROM dialogs WHERE hidden = 1"))}


def dialog_metadata(conn: sqlite3.Connection, dialog_id: int) -> TraceDialogMetadata:
    row = _one(
        conn.execute(
            "SELECT COALESCE(d.type, e.type, 'Unknown'), COALESCE(sd.status, 'not_synced'), COALESCE(d.hidden, 0) FROM (SELECT ? AS dialog_id) x LEFT JOIN dialogs d ON d.dialog_id = x.dialog_id LEFT JOIN entities e ON e.id = x.dialog_id LEFT JOIN synced_dialogs sd ON sd.dialog_id = x.dialog_id",
            (dialog_id,),
        )
    )
    values = _sequence(row) if row is not None else ("Unknown", "not_synced", 0)
    return {"dialog_type": str(values[0]), "status": str(values[1]), "hidden": bool(values[2])}


def common_chat_ids(conn: sqlite3.Connection, target_user_id: int) -> list[int]:
    row = _one(conn.execute("SELECT detail_json FROM entity_details WHERE entity_id = ?", (target_user_id,)))
    if row is None:
        return []
    try:
        items = cast(dict[str, object], json.loads(str(_sequence(row)[0]))).get("common_chats", [])
    except TypeError, json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    result: list[int] = []
    for item in items:
        if isinstance(item, dict) and item.get("id") is not None:
            try:
                result.append(_int(item["id"]))
            except TypeError, ValueError:
                pass
    return result


def retry_fragment_dialog_ids(conn: sqlite3.Connection, *, target_user_id: int, now: int) -> list[int]:
    return [
        _int(_sequence(row)[0])
        for row in _rows(
            conn.execute(
                "SELECT dialog_id FROM trace_coverage_fragments WHERE target_user_id = ? AND status != 'complete' AND (next_retry_at IS NULL OR next_retry_at <= ?) ORDER BY updated_at ASC, dialog_id ASC",
                (target_user_id, now),
            )
        )
    ]


def visible_synced_dialog_ids(conn: sqlite3.Connection) -> list[int]:
    return [
        _int(_sequence(row)[0])
        for row in _rows(
            conn.execute(
                "SELECT sd.dialog_id FROM synced_dialogs sd LEFT JOIN dialogs d ON d.dialog_id = sd.dialog_id WHERE sd.status != 'access_lost' AND COALESCE(d.hidden, 0) = 0 ORDER BY sd.dialog_id ASC"
            )
        )
    ]


def existing_message_bundle(
    conn: sqlite3.Connection, *, dialog_id: int, message_id: int, fields: Sequence[str]
) -> dict[str, object] | None:
    row = _one(
        conn.execute(
            f"SELECT {', '.join(fields)} FROM messages WHERE dialog_id = ? AND message_id = ?", (dialog_id, message_id)
        )
    )
    if row is None:
        return None
    return {
        "message": {field: _sequence(row)[index] for index, field in enumerate(fields)},
        "reactions": sorted(
            tuple(_sequence(item))
            for item in _rows(
                conn.execute(
                    "SELECT emoji, count FROM message_reactions WHERE dialog_id = ? AND message_id = ? ORDER BY emoji, count",
                    (dialog_id, message_id),
                )
            )
        ),
        "entities": sorted(
            tuple(_sequence(item))
            for item in _rows(
                conn.execute(
                    "SELECT offset, length, type, value FROM message_entities WHERE dialog_id = ? AND message_id = ? ORDER BY offset, length, type, value",
                    (dialog_id, message_id),
                )
            )
        ),
        "forward": (
            tuple(_sequence(forward))
            if (
                forward := _one(
                    conn.execute(
                        "SELECT fwd_from_peer_id, fwd_from_name, fwd_date, fwd_channel_post FROM message_forwards WHERE dialog_id = ? AND message_id = ?",
                        (dialog_id, message_id),
                    )
                )
            )
            else None
        ),
    }
