"""SQLite read queries for dialogs, sync status, and read-only alerts."""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from ..alert_policy import incoming_human_dm_sql
from ..models import DialogType, ReadMessage, ReadState
from ..sync_read_model import compute_sync_coverage

_SELECT_SYNC_STATUS_SQL = "SELECT status FROM synced_dialogs WHERE dialog_id = ?"
_MESSAGE_SENT_AT_SQL = "SELECT sent_at FROM messages WHERE dialog_id = ? AND message_id = ?"


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


class _QueryLogger(Protocol):
    def debug(self, msg: str, *args: object, **kwargs: object) -> None: ...


_SNAPSHOT_STALE_THRESHOLD_S = 12 * 3600


def _compute_snapshot_age_h(max_snapshot_at: int | None) -> int | None:
    """Return integer hours since the freshest snapshot, or None when fresh/unknown."""
    if max_snapshot_at is None:
        return None
    age_s = int(time.time()) - int(max_snapshot_at)
    if age_s > _SNAPSHOT_STALE_THRESHOLD_S:
        return age_s // 3600
    return None


def count_dialog_rows(conn: sqlite3.Connection) -> int:
    row = cast(tuple[object] | None, conn.execute("SELECT COUNT(*) FROM dialogs").fetchone())
    return 0 if row is None or row[0] is None else int(cast(int | str, row[0]))


def read_daemon_state_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = cast(tuple[object] | None, conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone())
    return None if row is None or row[0] is None else str(row[0])


def read_daemon_state_int(conn: sqlite3.Connection, key: str) -> int | None:
    value = read_daemon_state_value(conn, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def message_sent_at(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> int | None:
    """Return a local history cursor's timestamp when the message remains projected."""
    row = cast(tuple[object] | None, conn.execute(_MESSAGE_SENT_AT_SQL, (dialog_id, message_id)).fetchone())
    return None if row is None or row[0] is None else int(cast(int | str, row[0]))


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
            meta["sync_coverage_pct"] = compute_sync_coverage(total_messages_i, local_count)
            if total_messages_i is None:
                meta["archived_message_count"] = local_count

    return meta


# Phase 44 (LISTDIALOGS-01/02/04, DIFF-04): pure-SQL dialog list.
# LEFT JOIN synced_dialogs to preserve sync_status/total_messages/access_lost_at.
# Dialog name filtering is applied after the owner selects canonical identity.
# Cyrillic case-folding is delegated to the Python fuzzy pass because SQLite
# LOWER() is ASCII-only.
# `:archived_filter` and `:pinned_filter` are 0 (filter rows where col=0)
# or None (no filter).
_LIST_DIALOGS_SQL = """
WITH agent_visible_dialogs AS (
    SELECT
        d.dialog_id,
        d.archived,
        d.pinned,
        d.members,
        d.created,
        COALESCE(d.last_message_at, sd.last_event_at, sd.last_synced_at, sd.access_lost_at) AS last_message_at,
        d.snapshot_at,
        d.unread_mentions_count,
        d.unread_reactions_count,
        d.unread_count,
        sd.status AS sync_status,
        sd.total_messages,
        sd.last_synced_at,
        sd.last_event_at,
        sd.last_delta_checked_at,
        sd.access_lost_at
    FROM dialogs d
    LEFT JOIN synced_dialogs sd USING(dialog_id)
    WHERE d.hidden = 0 OR sd.status = 'access_lost'

    UNION ALL

    SELECT
        sd.dialog_id,
        0 AS archived,
        0 AS pinned,
        NULL AS members,
        NULL AS created,
        COALESCE(sd.last_event_at, sd.last_synced_at, sd.access_lost_at) AS last_message_at,
        NULL AS snapshot_at,
        0 AS unread_mentions_count,
        0 AS unread_reactions_count,
        NULL AS unread_count,
        sd.status AS sync_status,
        sd.total_messages,
        sd.last_synced_at,
        sd.last_event_at,
        sd.last_delta_checked_at,
        sd.access_lost_at
    FROM synced_dialogs sd
    LEFT JOIN dialogs d USING(dialog_id)
    WHERE sd.status = 'access_lost' AND d.dialog_id IS NULL
)
SELECT
    dialog_id, archived, pinned,
    members, created, last_message_at, snapshot_at,
    unread_mentions_count, unread_reactions_count, unread_count,
    sync_status, total_messages, last_synced_at, last_event_at, last_delta_checked_at, access_lost_at
FROM agent_visible_dialogs
WHERE (:archived_filter IS NULL OR archived = :archived_filter)
AND (:pinned_filter IS NULL OR pinned = :pinned_filter)
ORDER BY pinned DESC, last_message_at DESC, dialog_id ASC
"""

_LIST_DIALOG_MESSAGE_AGGREGATES_SQL = """
WITH selected_dialogs AS MATERIALIZED (
    SELECT CAST(value AS INTEGER) AS dialog_id
    FROM json_each(:dialog_ids_json)
), message_aggregates AS (
    SELECT
        ids.dialog_id,
        SUM(CASE WHEN m.is_deleted = 0 THEN 1 ELSE 0 END) AS local_message_count,
        SUM(CASE
            WHEN sd.status = 'synced'
             AND m.is_deleted = 0
             AND m.is_service = 0
             AND m."out" = 0
             AND m.message_id > COALESCE(sd.read_inbox_max_id, -1)
            THEN 1 ELSE 0 END
        ) AS unread_in,
        SUM(CASE
            WHEN sd.status = 'synced'
             AND m.is_deleted = 0
             AND m.is_service = 0
             AND m."out" = 1
             AND m.message_id > COALESCE(sd.read_outbox_max_id, -1)
            THEN 1 ELSE 0 END
        ) AS unread_out
    FROM selected_dialogs ids
    LEFT JOIN synced_dialogs sd USING(dialog_id)
    CROSS JOIN messages m
    WHERE m.dialog_id = ids.dialog_id
    GROUP BY ids.dialog_id
)
SELECT
    ids.dialog_id,
    COALESCE(a.local_message_count, 0) AS local_message_count,
    COALESCE(a.unread_in, 0) AS unread_in,
    COALESCE(a.unread_out, 0) AS unread_out
FROM selected_dialogs ids
LEFT JOIN message_aggregates a USING(dialog_id)
"""

# Unread summary is intentionally sourced only from the persisted Telegram
# Dialog projection plus the dialog lifecycle visibility join.  The
# ``synced_dialogs`` join exists solely to exclude access-lost dialogs; it does
# not enroll dialogs, provide unread facts, or supply message bodies.
_UNREAD_SUMMARY_SQL = """
WITH matching AS (
    SELECT
        d.dialog_id,
        d.archived,
        d.last_message_at,
        d.unread_count,
        d.unread_mark,
        d.unread_mentions_count,
        d.unread_reactions_count
    FROM dialogs d
    LEFT JOIN synced_dialogs sd USING(dialog_id)
    WHERE d.hidden = 0
      AND (sd.status IS NULL OR sd.status <> 'access_lost')
      AND (
          COALESCE(d.unread_count, 0) > 0
          OR COALESCE(d.unread_mark, 0) <> 0
          OR COALESCE(d.unread_mentions_count, 0) > 0
          OR COALESCE(d.unread_reactions_count, 0) > 0
      )
)
SELECT
    matching.*,
    COUNT(*) OVER () AS total_matching
FROM matching
ORDER BY
    CASE WHEN COALESCE(unread_mark, 0) <> 0 THEN 1 ELSE 0 END DESC,
    COALESCE(unread_mentions_count, 0) DESC,
    COALESCE(unread_reactions_count, 0) DESC,
    COALESCE(unread_count, 0) DESC,
    last_message_at DESC,
    dialog_id ASC
LIMIT :limit
"""

# Contract note (WR-06): results are emitted as unread_in / unread_out only for DMs.
_COUNT_SYNCED_MESSAGES_SQL = "SELECT COUNT(*) FROM messages WHERE dialog_id = ? AND is_deleted = 0"

_SELECT_DIALOG_ACCESS_META_SQL = (
    "SELECT status, total_messages, access_lost_at, last_synced_at, last_event_at "
    "FROM synced_dialogs WHERE dialog_id = ?"
)

# Unread SQL - zero Telegram API calls.
_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL = (
    "SELECT sd.dialog_id, sd.read_inbox_max_id, sd.last_event_at, "
    "d.members AS participants_count, "
    "(SELECT COUNT(*) FROM messages m "
    " WHERE m.dialog_id = sd.dialog_id "
    "   AND m.message_id > sd.read_inbox_max_id "
    '   AND m."out" = 0'
    "   AND m.is_service = 0"
    "   AND ((m.is_deleted = 0 AND (:since_utc IS NULL OR m.sent_at >= :since_utc))"
    "     OR (m.is_deleted = 1 AND m.deleted_at >= :deleted_since_utc"
    "       AND (:since_utc IS NULL OR m.deleted_at >= :since_utc)"
    f"       AND {incoming_human_dm_sql('m')}))) AS unread_count "
    "FROM synced_dialogs sd "
    "LEFT JOIN dialogs d ON d.dialog_id = sd.dialog_id "
    "WHERE sd.status = 'synced' "
    "AND sd.read_inbox_max_id IS NOT NULL"
)

_GET_READ_POSITION_SQL = "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = ?"
_COUNT_READ_POSITION_PENDING_SQL = (
    "SELECT COUNT(*) FROM synced_dialogs WHERE status = 'synced' AND read_inbox_max_id IS NULL"
)
_READ_POSITION_PENDING_IDENTITIES_SQL = (
    "SELECT sd.dialog_id "
    "FROM synced_dialogs sd "
    "WHERE sd.status = 'synced' AND sd.read_inbox_max_id IS NULL "
    "ORDER BY sd.dialog_id LIMIT 20"
)


# Phase 39.1-02: effective_sender_id collapses DM direction into a concrete user id.
# For DM outgoing rows (sender_id IS NULL, out=1) -> self_id (from :self_id parameter).
# For DM incoming rows (sender_id IS NULL, out=0) -> dialog_id (the peer).
# For service messages (is_service=1) or group unknown senders -> NULL.
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
_SENDER_USERNAME_SQL = "COALESCE(e_raw.username, e_eff.username) AS sender_username"
_SENDER_NAME_FILTER_SQL = "COALESCE(m.sender_first_name, e_raw.name, e_eff.name)"
_SENDER_ENTITY_JOINS_SQL = (
    "LEFT JOIN entities e_raw ON e_raw.id = m.sender_id "
    f"LEFT JOIN entities e_eff ON e_eff.id = {_EFFECTIVE_SENDER_ID_EXPR} "
)

_SELECT_MESSAGES_SQL = (
    f"SELECT m.message_id, m.sent_at, m.text, m.sender_id, "
    f"{_SENDER_FIRST_NAME_SQL}, "
    f"m.media_kind, m.media_payload, NULL AS content_kind, m.reply_to_msg_id, m.forum_topic_id, "
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
    f"m.sent_at, m.media_kind, m.media_payload, NULL AS content_kind, m.reply_to_msg_id, m.sender_id, m.forum_topic_id, "
    f"COALESCE(tm.title, CASE WHEN m.forum_topic_id = 1 THEN 'General' END) AS topic_title, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out, m.dialog_id "
    f"FROM messages_fts f "
    f"JOIN messages m ON m.dialog_id = f.dialog_id AND m.message_id = f.message_id "
    f"LEFT JOIN topic_metadata tm "
    f"  ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"WHERE messages_fts MATCH :query AND f.dialog_id = :dialog_id "
    f"AND (:since_utc IS NULL OR m.sent_at >= :since_utc) "
    f"AND (:until_utc IS NULL OR m.sent_at < :until_utc) "
    f"ORDER BY rank LIMIT :limit OFFSET :offset"
)

# _SELECT_FTS_ALL_SQL keeps entity joins only for sender identity.
_SELECT_FTS_ALL_SQL = (
    f"SELECT f.message_id, m.text, "
    f"{_SENDER_FIRST_NAME_SQL}, "
    f"m.sent_at, m.media_kind, m.media_payload, NULL AS content_kind, m.reply_to_msg_id, m.sender_id, m.forum_topic_id, "
    f"COALESCE(tm.title, CASE WHEN m.forum_topic_id = 1 THEN 'General' END) AS topic_title, "
    f"f.dialog_id, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out "
    f"FROM messages_fts f "
    f"JOIN messages m ON m.dialog_id = f.dialog_id AND m.message_id = f.message_id "
    f"LEFT JOIN topic_metadata tm "
    f"  ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"WHERE messages_fts MATCH :query "
    f"AND (:since_utc IS NULL OR m.sent_at >= :since_utc) "
    f"AND (:until_utc IS NULL OR m.sent_at < :until_utc) "
    f"ORDER BY rank LIMIT :limit OFFSET :offset"
)

_FETCH_UNREAD_MESSAGES_SQL = (
    f"SELECT m.message_id, m.sent_at, m.text, m.sender_id, "
    f"{_SENDER_FIRST_NAME_SQL}, {_SENDER_USERNAME_SQL}, m.media_kind, m.media_payload, NULL AS content_kind, "
    f"m.forum_topic_id, COALESCE(tm.title, CASE WHEN m.forum_topic_id = 1 THEN 'General' END) AS topic_title, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out, m.dialog_id, m.is_deleted, m.deleted_at "
    f"FROM messages m "
    f"LEFT JOIN topic_metadata tm "
    f"  ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"WHERE m.dialog_id = :dialog_id AND m.message_id > :after_msg_id "
    f'AND m."out" = 0 AND m.is_service = 0 '
    f"AND ((m.is_deleted = 0 AND (:since_utc IS NULL OR m.sent_at >= :since_utc)) "
    f"OR (m.is_deleted = 1 AND m.deleted_at >= :deleted_since_utc "
    f"AND (:since_utc IS NULL OR m.deleted_at >= :since_utc) AND {incoming_human_dm_sql('m')})) "
    f"ORDER BY m.message_id ASC LIMIT :limit"
)

# Base SELECT shared by _build_list_messages_query and _list_messages_context_window.
# Appends dialog_id=:dialog_id and is_deleted=0 guards; callers add further conditions
# (appended as " AND ..." with named params or positional - see _build_list_messages_query).
# Callers MUST bind :self_id (used by EFFECTIVE_SENDER_ID_SQL CASE expression).
_LIST_MESSAGES_BASE_SQL = (
    f"SELECT m.message_id, m.sent_at, m.text, m.sender_id, "
    f"{_SENDER_FIRST_NAME_SQL}, "
    f"m.media_kind, m.media_payload, NULL AS content_kind, m.reply_to_msg_id, m.forum_topic_id, "
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
        if f.name
        not in {
            "reactions_display",
            "dialog_name",
            "dialog_name_source",
            "read_at",
            "reaction_events",
            "reaction_events_status",
            "media_description",
            "media_payload",
            # Username is an inbox-only enrichment; other read surfaces keep
            # their existing SQL contract during this vertical slice.
            "sender_username",
        }
    )
    aliases = frozenset(re.findall(r"\bAS\s+(\w+)", _LIST_MESSAGES_BASE_SQL))
    bare = frozenset(re.findall(r"\b(?:m|mf)\.(\w+)\b", _LIST_MESSAGES_BASE_SQL))
    found = aliases | bare
    missing = expected - found
    extra = (found - expected) - {"media_payload"}
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


def _build_list_messages_query(
    req: _ListMessagesDbRequest,
    *,
    query_logger: _QueryLogger | None = None,
) -> tuple[str, dict[str, object]]:
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
    sql, params = _apply_list_messages_time_filters(sql, params, since_utc, until_utc)
    sql, params = _apply_list_messages_sender_filter(sql, params, sender_id, sender_name)
    sql, params = _apply_list_messages_topic_and_unread_filters(sql, params, topic_id, unread_after_id)
    sql, params = _apply_list_messages_anchor_filter(sql, params, req)

    if direction == "oldest":
        sql += " ORDER BY m.sent_at ASC, m.message_id ASC"
    else:
        sql += " ORDER BY m.sent_at DESC, m.message_id DESC"

    sql += " LIMIT :limit"

    if query_logger is not None:
        _log_list_messages_query(
            query_logger,
            params,
            direction,
            sender_id=sender_id,
            sender_name=sender_name,
            topic_id=topic_id,
            unread_after_id=unread_after_id,
            anchor=anchor_msg_id,
            since_utc=since_utc,
            until_utc=until_utc,
        )
    return sql, params


def _apply_list_messages_time_filters(
    sql: str,
    params: dict[str, object],
    since_utc: int | None,
    until_utc: int | None,
) -> tuple[str, dict[str, object]]:
    if since_utc is not None:
        sql += " AND m.sent_at >= :since_utc"
        params["since_utc"] = since_utc
    if until_utc is not None:
        sql += " AND m.sent_at < :until_utc"
        params["until_utc"] = until_utc
    return sql, params


def _apply_list_messages_sender_filter(
    sql: str,
    params: dict[str, object],
    sender_id: int | None,
    sender_name: str | None,
) -> tuple[str, dict[str, object]]:
    if sender_id is not None:
        sql += f" AND {_EFFECTIVE_SENDER_ID_EXPR} = :filter_sender_id"
        params["filter_sender_id"] = sender_id
    elif sender_name is not None:
        # Prefer the stored historical name, but fall back to resolved sender entities for DM rows
        # whose raw sender fields are intentionally NULL.
        sql += f" AND {_SENDER_NAME_FILTER_SQL} LIKE :sender_name_pattern ESCAPE '\\' COLLATE NOCASE"
        escaped = sender_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["sender_name_pattern"] = f"%{escaped}%"
    return sql, params


def _apply_list_messages_topic_and_unread_filters(
    sql: str,
    params: dict[str, object],
    topic_id: int | None,
    unread_after_id: int | None,
) -> tuple[str, dict[str, object]]:
    if topic_id is not None:
        sql += " AND m.forum_topic_id = :topic_id"
        params["topic_id"] = topic_id
    if unread_after_id is not None:
        sql += " AND m.message_id > :unread_after_id"
        params["unread_after_id"] = unread_after_id
    return sql, params


def _log_list_messages_query(
    query_logger: _QueryLogger,
    params: dict[str, object],
    direction: str,
    **filters: int | str | None,
) -> None:
    active_filters = "+".join(name for name, value in filters.items() if value is not None) or "none"
    query_logger.debug(
        "list_messages_query filters=%s param_count=%d direction=%s",
        active_filters,
        len(params),
        direction,
    )


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


@dataclass(frozen=True, slots=True)
class _ReadStateValues:
    inbox_cursor: int | None
    outbox_cursor: int | None
    inbox_count: int
    outbox_count: int
    inbox_oldest: int | None
    outbox_oldest: int | None


def _read_state_for_dialog(conn: sqlite3.Connection, dialog_id: int, dialog_type: str) -> ReadState | None:
    """Compute bidirectional read state for a direct-message dialog."""
    if DialogType.parse(dialog_type) != DialogType.USER:
        return None

    row = cast(
        tuple[object, object, object, object, object, object] | None,
        conn.execute(_READ_STATE_SQL, {"dialog_id": dialog_id}).fetchone(),
    )
    values = _read_state_values(row)
    rs: ReadState = {
        "inbox_unread_count": values.inbox_count,
        "inbox_cursor_state": _read_cursor_state(values.inbox_cursor, values.inbox_count),
        "outbox_unread_count": values.outbox_count,
        "outbox_cursor_state": _read_cursor_state(values.outbox_cursor, values.outbox_count),
    }
    _add_read_state_positions(rs, values)
    return rs


def _read_state_values(
    row: tuple[object, object, object, object, object, object] | None,
) -> _ReadStateValues:
    # Aggregate functions return one row even when no messages match.
    if row is None:
        return _ReadStateValues(None, None, 0, 0, None, None)
    return _ReadStateValues(
        inbox_cursor=cast(int | None, row[0]),
        outbox_cursor=cast(int | None, row[1]),
        inbox_count=int(cast(int | None, row[2]) or 0),
        outbox_count=int(cast(int | None, row[3]) or 0),
        inbox_oldest=cast(int | None, row[4]),
        outbox_oldest=cast(int | None, row[5]),
    )


def _read_cursor_state(cursor: int | None, unread_count: int) -> Literal["populated", "null", "all_read"]:
    if cursor is None:
        return "null"
    if unread_count == 0:
        return "all_read"
    return "populated"


def _add_read_state_positions(state: ReadState, values: _ReadStateValues) -> None:
    if values.inbox_cursor is not None:
        state["inbox_max_id_anchor"] = int(values.inbox_cursor)
    if values.outbox_cursor is not None:
        state["outbox_max_id_anchor"] = int(values.outbox_cursor)
    if values.inbox_count > 0 and values.inbox_oldest is not None:
        state["inbox_oldest_unread_date"] = int(values.inbox_oldest)
    if values.outbox_count > 0 and values.outbox_oldest is not None:
        state["outbox_oldest_unread_date"] = int(values.outbox_oldest)
