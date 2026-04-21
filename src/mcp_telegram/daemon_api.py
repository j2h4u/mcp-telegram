"""Daemon API server — Unix socket request dispatcher.

DaemonAPIServer listens on a Unix domain socket and handles fifteen methods:
  - list_messages: read from sync.db (synced dialogs) or Telegram (on-demand)
  - search_messages: FTS5 stemmed full-text search against messages_fts
  - list_dialogs: live dialog list from Telegram enriched with sync_status
  - list_topics: forum topic list via Telegram API
  - get_me: current user info via Telegram API
  - mark_dialog_for_sync: add/remove dialog from sync scope
  - get_sync_status: sync status and message statistics for a dialog
  - get_sync_alerts: deleted messages, edit history, access-lost dialogs
  - get_user_info: user profile and common chats
  - list_unread_messages: prioritized unread messages across dialogs
  - record_telemetry: write telemetry event to sync.db
  - get_usage_stats: read usage statistics from sync.db
  - upsert_entities: batch upsert entities into sync.db
  - resolve_entity: fuzzy entity resolution from sync.db
  - get_dialog_stats: aggregate analytics (reactions, mentions, hashtags, forwards) for a synced dialog

Protocol: newline-delimited JSON (one request line → one response line).

Dialog name resolution: when dialog_id is absent or 0 and a "dialog" string
is present, _resolve_dialog_name() resolves it to a numeric id via
client.get_entity() with fallback to iter_dialogs() fuzzy match.

Architecture:
- One DaemonAPIServer instance is created per daemon run; it holds a
  reference to the long-lived sqlite3.Connection and TelegramClient.
- handle_client() is passed directly to asyncio.start_unix_server().
- Formatting (format_messages) stays on the MCP server side — the daemon
  returns raw row dicts that the MCP tools format.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

from telethon.tl.functions.messages import (  # type: ignore[import-untyped]
    GetCommonChatsRequest,
    GetDialogFiltersRequest,
    GetForumTopicsRequest,
)
from telethon.tl.functions.users import GetFullUserRequest  # type: ignore[import-untyped]
from telethon.tl.types import Channel, Chat  # type: ignore[import-untyped]
from telethon import utils as telethon_utils  # type: ignore[import-untyped]


def _classify_dialog_type(entity: object | None) -> str:
    """Classify a Telethon dialog entity into a human-readable type string.

    Returns one of: "User", "Bot", "Channel", "Group", "Forum", "Chat", "Unknown".
    Branch order matters: Forum must be checked before Group because forum=True
    implies megagroup=True — checking megagroup first would misclassify forums.
    User detection uses duck-typing (hasattr first_name) to avoid an extra import.
    """
    if entity is None:
        return "Unknown"
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False) and getattr(entity, "forum", False):
            return "Forum"
        if getattr(entity, "megagroup", False):
            return "Group"
        return "Channel"
    if isinstance(entity, Chat):
        return "Chat"
    if hasattr(entity, "first_name"):
        return "Bot" if getattr(entity, "bot", False) else "User"
    return "Unknown"


def _dialog_type_from_db(conn: sqlite3.Connection, dialog_id: int) -> str:
    """Return dialog type string from sync.db entities table.

    Zero Telegram API calls — pure sqlite lookup. Returns one of the values
    produced by _classify_dialog_type() (same vocabulary). Returns "Unknown"
    when no entity row exists yet (the row is populated by sync bootstrap and
    by live event handlers).

    This is the cheap daemon-side path for _list_messages / _search_messages /
    _list_unread_messages where only the numeric dialog_id is available.
    """
    row = conn.execute(
        "SELECT type FROM entities WHERE id = ?", (dialog_id,)
    ).fetchone()
    if row is None:
        return "Unknown"
    return str(row[0])


def _read_state_for_dialog(
    conn: sqlite3.Connection, dialog_id: int, dialog_type: str
) -> dict | None:
    """Compute the bidirectional ReadState for a DM.

    Returns None for non-DM dialog types (Channel/Group/Forum/Chat/Bot/Unknown).
    For DMs (dialog_type == "User"):
      * Reads read_inbox_max_id + read_outbox_max_id from synced_dialogs.
      * Counts unread per side: incoming (out=0) above read_inbox_max_id,
        outgoing (out=1) above read_outbox_max_id.
      * Resolves cursor_state in {populated, null, all_read}.
      * Fetches MIN(sent_at) of the unread tail per side when count > 0.

    Zero Telegram API calls. Single pair of SQL queries (cursor row + one
    GROUP BY count-and-min over messages). See Plan 39.3-03 / models.ReadState.
    """
    if dialog_type != "User":
        return None

    cursor_row = conn.execute(
        "SELECT read_inbox_max_id, read_outbox_max_id "
        "FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    if cursor_row is None:
        read_inbox_max_id, read_outbox_max_id = None, None
    else:
        read_inbox_max_id, read_outbox_max_id = cursor_row[0], cursor_row[1]

    # Aggregate incoming/outgoing unread counts + oldest-unread-sent_at in one pass.
    # NOTE: NULL cursor → COALESCE(cursor, -1) treats ALL messages on that side as unread
    # in row semantics (matches the bootstrap-pending trade-off documented in
    # <interfaces> §MEDIUM-2). Header rendering separately renders cursor_state="null"
    # as "unknown (sync pending)" — see formatter._render_read_state_header.
    agg_row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN out = 0 AND message_id > COALESCE(:in_c, -1) THEN 1 ELSE 0 END) AS in_cnt,
          SUM(CASE WHEN out = 1 AND message_id > COALESCE(:out_c, -1) THEN 1 ELSE 0 END) AS out_cnt,
          MIN(CASE WHEN out = 0 AND message_id > COALESCE(:in_c, -1) THEN sent_at END) AS in_min,
          MIN(CASE WHEN out = 1 AND message_id > COALESCE(:out_c, -1) THEN sent_at END) AS out_min
        FROM messages
        WHERE dialog_id = :dialog_id
        """,
        {"dialog_id": dialog_id, "in_c": read_inbox_max_id, "out_c": read_outbox_max_id},
    ).fetchone()
    in_cnt = int(agg_row[0] or 0)
    out_cnt = int(agg_row[1] or 0)
    in_min = agg_row[2]
    out_min = agg_row[3]

    def _state(cursor: int | None, unread_count: int) -> str:
        if cursor is None:
            return "null"
        if unread_count == 0:
            return "all_read"
        return "populated"

    rs: dict = {
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


USER_TTL: int = 2_592_000   # 30 days
GROUP_TTL: int = 604_800    # 7 days

from .budget import allocate_message_budget_proportional, unread_chat_tier
from .fts import stem_query
from .pagination import (
    HistoryDirection,
    decode_navigation_token,
    encode_history_navigation,
    encode_search_navigation,
)
from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

from .formatter import format_reaction_counts
from .sync_worker import (
    apply_reactions_delta,
    extract_reactions_rows,
    extract_reply_and_topic,
)

# Phase 39.2 §Key technical decisions: per-message TTL for JIT reactions freshen-on-read.
# Amortizes rapid paginated reads on the same ids; live events catch most mutations.
REACTIONS_TTL_SECONDS = 600
from .resolver import (
    Candidates,
    NotFound,
    Resolved,
    _parse_tme_link,
    latinize,
    resolve as resolve_entity_sync,
)

logger = logging.getLogger(__name__)

_current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_request_id", default=None,
)


def _rid() -> str:
    """Return ' request_id=X' suffix for log lines, or empty string."""
    rid = _current_request_id.get()
    return f" request_id={rid}" if rid else ""


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp *value* to the inclusive range [low, high]."""
    return max(low, min(value, high))


def _compute_sync_coverage(
    total_messages: int | None,
    local_count: int,
) -> int | None:
    """Compute sync_coverage_pct. Returns int 0-100 or None if unknown."""
    if total_messages is not None and total_messages > 0:
        return min(100, round(local_count / total_messages * 100))
    if total_messages == 0:
        return 100  # trivially complete
    return None


def _build_access_metadata(
    conn: sqlite3.Connection,
    dialog_id: int,
    status: str,
) -> dict:
    """Build consistent access metadata for list_messages / search_messages responses.

    Returns dict with: dialog_access, and for access_lost dialogs: access_lost_at,
    last_synced_at, last_event_at, sync_coverage_pct, and optionally
    archived_message_count (when total_messages is None).
    """
    meta: dict = {"dialog_access": "archived" if status == "access_lost" else "live"}

    if status == "access_lost":
        row = conn.execute(_SELECT_DIALOG_ACCESS_META_SQL, (dialog_id,)).fetchone()
        if row:
            _, total_messages, access_lost_at, last_synced_at, last_event_at = row
            count_row = conn.execute(
                _COUNT_SYNCED_MESSAGES_SQL, (dialog_id,)
            ).fetchone()
            local_count = count_row[0] if count_row else 0

            meta["access_lost_at"] = access_lost_at
            meta["last_synced_at"] = last_synced_at
            meta["last_event_at"] = last_event_at
            meta["sync_coverage_pct"] = _compute_sync_coverage(
                total_messages, local_count
            )
            if total_messages is None:
                meta["archived_message_count"] = local_count

    return meta


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def get_daemon_socket_path() -> Path:
    """Return the canonical path for the daemon Unix socket."""
    return xdg_state_home() / "mcp-telegram" / "daemon.sock"


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_SELECT_SYNC_STATUS_SQL = "SELECT status FROM synced_dialogs WHERE dialog_id = ?"

# Phase 39.1-02: effective_sender_id collapses DM direction into a concrete user id.
# For DM outgoing rows (sender_id IS NULL, out=1) → self_id (from :self_id parameter).
# For DM incoming rows (sender_id IS NULL, out=0) → dialog_id (the peer).
# For service messages (is_service=1) or group unknown senders → NULL (render as System/unknown).
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
# first_name for DM incoming; self name for DM outgoing — though "Я" wins at render).
_SENDER_FIRST_NAME_SQL = (
    "COALESCE(e_raw.name, e_eff.name, m.sender_first_name) AS sender_first_name"
)
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
    f"ORDER BY rank LIMIT :limit OFFSET :offset"
)

_SELECT_SYNCED_STATUSES_SQL = (
    "SELECT dialog_id, status, total_messages, access_lost_at FROM synced_dialogs"
)

_MARK_FOR_SYNC_SQL = "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'not_synced')"
_UNMARK_SYNC_SQL = "UPDATE synced_dialogs SET status = 'not_synced' WHERE dialog_id = ?"

_GET_SYNC_STATUS_SQL = (
    "SELECT status, last_synced_at, last_event_at, sync_progress, total_messages, access_lost_at "
    "FROM synced_dialogs WHERE dialog_id = ?"
)
_COUNT_SYNCED_MESSAGES_SQL = "SELECT COUNT(*) FROM messages WHERE dialog_id = ? AND is_deleted = 0"

# TODO: _COUNT_MESSAGES_BY_DIALOG_SQL scans the full messages table via GROUP BY.
# For large datasets (millions of messages), consider adding a covering index
# on messages(dialog_id, is_deleted) or caching counts in synced_dialogs.
_COUNT_MESSAGES_BY_DIALOG_SQL = (
    "SELECT dialog_id, COUNT(*) FROM messages WHERE is_deleted = 0 GROUP BY dialog_id"
)

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
    "SELECT dialog_id, access_lost_at "
    "FROM synced_dialogs WHERE status = 'access_lost' AND access_lost_at > ?"
)

# Dialog stats SQL (get_dialog_stats)
_GET_DIALOG_TOP_REACTIONS_SQL = (
    "SELECT emoji, SUM(count) AS total "
    "FROM message_reactions WHERE dialog_id = ? "
    "GROUP BY emoji ORDER BY total DESC LIMIT ?"
)
_GET_DIALOG_TOP_MENTIONS_SQL = (
    "SELECT value, COUNT(*) AS cnt FROM message_entities "
    "WHERE dialog_id = ? AND type = 'mention' AND value IS NOT NULL "
    "GROUP BY value ORDER BY cnt DESC LIMIT ?"
)
_GET_DIALOG_TOP_HASHTAGS_SQL = (
    "SELECT value, COUNT(*) AS cnt FROM message_entities "
    "WHERE dialog_id = ? AND type = 'hashtag' AND value IS NOT NULL "
    "GROUP BY value ORDER BY cnt DESC LIMIT ?"
)
_GET_DIALOG_TOP_FORWARDS_SQL = (
    "SELECT fwd_from_peer_id, fwd_from_name, COUNT(*) AS cnt "
    "FROM message_forwards "
    "WHERE dialog_id = ? AND (fwd_from_peer_id IS NOT NULL OR fwd_from_name IS NOT NULL) "
    "GROUP BY fwd_from_peer_id, fwd_from_name ORDER BY cnt DESC LIMIT ?"
)

# Unread SQL — zero Telegram API calls (Plan 38-02)
# Single grouped query: scalar subquery provides per-dialog unread_count in ONE round trip,
# replacing the N+1 COUNT(*)-per-dialog pattern. Uses idx_synced_dialogs_status_read_position
# (schema v8) for the outer filter and messages PK (dialog_id, message_id) for range scans.
_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL = (
    "SELECT sd.dialog_id, sd.read_inbox_max_id, sd.last_event_at, "
    "COALESCE(e.name, CAST(sd.dialog_id AS TEXT)) AS display_name, "
    "COALESCE(e.type, 'Unknown') AS entity_type, "
    "(SELECT COUNT(*) FROM messages m "
    " WHERE m.dialog_id = sd.dialog_id "
    "   AND m.message_id > sd.read_inbox_max_id "
    "   AND m.is_deleted = 0) AS unread_count "
    "FROM synced_dialogs sd "
    "LEFT JOIN entities e ON e.id = sd.dialog_id "
    "WHERE sd.status = 'synced' "
    "AND sd.read_inbox_max_id IS NOT NULL"
)
_FETCH_UNREAD_MESSAGES_SQL = (
    f"SELECT m.message_id, m.sent_at, m.text, m.sender_id, "
    f"{_SENDER_FIRST_NAME_SQL}, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out, m.dialog_id "
    f"FROM messages m "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"WHERE m.dialog_id = :dialog_id AND m.message_id > :after_msg_id AND m.is_deleted = 0 "
    f"ORDER BY m.message_id DESC LIMIT :limit"
)
_GET_READ_POSITION_SQL = (
    "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = ?"
)
_COUNT_BOOTSTRAP_PENDING_SQL = (
    "SELECT COUNT(*) FROM synced_dialogs "
    "WHERE status = 'synced' AND read_inbox_max_id IS NULL"
)

# Entity / telemetry SQL
_UPSERT_ENTITY_SQL = (
    "INSERT OR REPLACE INTO entities "
    "(id, type, name, username, name_normalized, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_ALL_ENTITY_NAMES_SQL = (
    "SELECT id, name FROM entities "
    "WHERE name IS NOT NULL "
    "AND ((type IN ('User', 'Bot') AND updated_at >= ?) "  # PascalCase per ListDialogs type vocabulary
    "OR (type NOT IN ('User', 'Bot') AND updated_at >= ?))"
)
_ALL_ENTITY_NAMES_NORMALIZED_SQL = (
    "SELECT id, name_normalized FROM entities "
    "WHERE name_normalized IS NOT NULL "
    "AND ((type IN ('User', 'Bot') AND updated_at >= ?) "  # PascalCase per ListDialogs type vocabulary
    "OR (type NOT IN ('User', 'Bot') AND updated_at >= ?))"
)
_ENTITY_BY_USERNAME_SQL = "SELECT id, name FROM entities WHERE username = ?"

# Column names returned by _build_list_messages_query, in SELECT order.
_DB_MESSAGE_COLUMNS = (
    "message_id", "sent_at", "text", "sender_id", "sender_first_name",
    "media_description", "reply_to_msg_id", "forum_topic_id",
    "is_deleted", "deleted_at", "edit_date", "topic_title",
    "effective_sender_id", "is_service", "out", "dialog_id",
)


# ---------------------------------------------------------------------------
# Reaction helper
# ---------------------------------------------------------------------------


def _fetch_reaction_counts(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: list[int],
) -> dict[int, list[tuple[str, int]]]:
    """Return {message_id: [(emoji, count), ...]} for the given page.

    Single IN query against message_reactions table. Returns empty dict
    if no message_ids or no reactions found. Results are ordered by
    count DESC, then emoji ASC (Unicode code point) for deterministic
    display. This matches the sort contract in format_reaction_counts.
    Addresses review Priority Action #5.
    """
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"SELECT message_id, emoji, count FROM message_reactions "
        f"WHERE dialog_id = ? AND message_id IN ({placeholders}) "
        f"ORDER BY count DESC, emoji",
        [dialog_id, *message_ids],
    ).fetchall()
    result: dict[int, list[tuple[str, int]]] = {}
    for msg_id, emoji, count in rows:
        result.setdefault(int(msg_id), []).append((emoji, int(count)))
    return result


# ---------------------------------------------------------------------------
# Dynamic SQL builder for list_messages
# ---------------------------------------------------------------------------

# Base SELECT shared by _build_list_messages_query and _list_messages_context_window.
# Appends dialog_id=:dialog_id and is_deleted=0 guards; callers add further conditions
# (appended as " AND ..." with named params or positional — see _build_list_messages_query).
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
    f"tm.title AS topic_title, "
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out, m.dialog_id "
    f"FROM messages m "
    f"LEFT JOIN topic_metadata tm "
    f"  ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"WHERE m.dialog_id = :dialog_id AND m.is_deleted = 0"
)


def _build_list_messages_query(
    *,
    dialog_id: int,
    limit: int,
    self_id: int | None = None,
    direction: str = "newest",
    anchor_msg_id: int | None = None,
    sender_id: int | None = None,
    sender_name: str | None = None,
    topic_id: int | None = None,
    unread_after_id: int | None = None,
) -> tuple[str, dict]:
    """Build a parameterized SELECT for list_messages against sync.db.

    Returns (sql_string, params_dict).  Column names in the SELECT match
    the keys used by _list_messages_from_db (use conn.row_factory = sqlite3.Row
    or unpack via _DB_MESSAGE_COLUMNS).

    self_id is bound to :self_id (used by the EFFECTIVE_SENDER_ID_SQL CASE
    expression to collapse DM direction). If not set, DM outgoing rows will
    project effective_sender_id=NULL instead of the authenticated user id.
    """
    params: dict = {"dialog_id": dialog_id, "limit": limit, "self_id": self_id}
    sql = _LIST_MESSAGES_BASE_SQL

    if sender_id is not None:
        sql += " AND m.sender_id = :filter_sender_id"
        params["filter_sender_id"] = sender_id
    elif sender_name is not None:
        # Filter uses denormalized column intentionally — searches match historical names (name-at-send-time), while display COALESCEs against entities for current name.
        sql += " AND m.sender_first_name LIKE :sender_name_pattern ESCAPE '\\' COLLATE NOCASE"
        escaped = sender_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["sender_name_pattern"] = f"%{escaped}%"

    if topic_id is not None:
        sql += " AND m.forum_topic_id = :topic_id"
        params["topic_id"] = topic_id

    if unread_after_id is not None:
        sql += " AND m.message_id > :unread_after_id"
        params["unread_after_id"] = unread_after_id

    if anchor_msg_id is not None:
        if direction == "oldest":
            sql += " AND m.message_id > :anchor_msg_id"
        else:
            sql += " AND m.message_id < :anchor_msg_id"
        params["anchor_msg_id"] = anchor_msg_id

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
            ]
            if v is not None
        )
        or "none",
        len(params),
        direction,
    )
    return sql, params


# ---------------------------------------------------------------------------
# Usage stats query
# ---------------------------------------------------------------------------


def _query_usage_stats(cursor: sqlite3.Cursor, since: int) -> dict:
    """Run all analytics queries and return the raw stats dict."""
    tool_dist = dict(
        cursor.execute(
            "SELECT tool_name, COUNT(*) FROM telemetry_events "
            "WHERE timestamp >= ? GROUP BY tool_name ORDER BY COUNT(*) DESC",
            (since,),
        ).fetchall()
    )

    error_dist = dict(
        cursor.execute(
            "SELECT error_type, COUNT(*) FROM telemetry_events "
            "WHERE timestamp >= ? AND error_type IS NOT NULL "
            "GROUP BY error_type ORDER BY COUNT(*) DESC",
            (since,),
        ).fetchall()
    )

    def _scalar(sql: str, params: tuple = (since,), default: int = 0) -> int:
        row = cursor.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else default

    max_depth = _scalar(
        "SELECT MAX(page_depth) FROM telemetry_events WHERE timestamp >= ?",
    )
    filter_count = _scalar(
        "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ? AND has_filter = 1",
    )
    total_calls = _scalar(
        "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ?",
    )

    latencies = cursor.execute(
        "SELECT duration_ms FROM telemetry_events WHERE timestamp >= ? ORDER BY duration_ms",
        (since,),
    ).fetchall()

    latency_median_ms = 0
    latency_p95_ms = 0
    if latencies:
        latency_values_ms = [lat[0] for lat in latencies]
        latency_median_ms = latency_values_ms[len(latency_values_ms) // 2]
        p95_idx = int(len(latency_values_ms) * 0.95)
        latency_p95_ms = (
            latency_values_ms[p95_idx]
            if p95_idx < len(latency_values_ms)
            else latency_values_ms[-1]
        )

    return {
        "tool_distribution": tool_dist,
        "error_distribution": error_dist,
        "max_page_depth": max_depth,
        "total_calls": total_calls,
        "filter_count": filter_count,
        "latency_median_ms": latency_median_ms,
        "latency_p95_ms": latency_p95_ms,
    }


# ---------------------------------------------------------------------------
# DaemonAPIServer
# ---------------------------------------------------------------------------


class DaemonAPIServer:
    """Unix socket server that dispatches JSON requests to Telegram/sync.db.

    Instantiated once per daemon run by sync_main().  handle_client() is
    passed to asyncio.start_unix_server() as the client connected callback.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: Any,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._conn = conn
        self._client = client
        self._shutdown_event = shutdown_event
        # Phase 39.1: cached authenticated user id, populated once by
        # sync_main() after client.connect() completes (see daemon.py).
        # Query-build paths (Plan 39.1-02) read this as a bound SQL parameter
        # to collapse DM direction (`out`) into an effective sender id without
        # calling Telethon on every read.
        self.self_id: int | None = None

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one client connection: read one request, write one response.

        One request per connection — client opens a new Unix socket connection
        for each call. The request_id field (if present) is echoed back in the
        response for cross-process log correlation.
        """
        method = ""
        request_id: str | None = None
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line.decode())
            except json.JSONDecodeError as exc:
                logger.warning("daemon_api invalid JSON: %s", exc)
                response = {"ok": False, "error": "invalid_json", "message": "invalid JSON"}
            else:
                request_id = req.get("request_id")
                method = req.get("method", "")
                if request_id:
                    logger.debug(
                        "daemon_api_request method=%s request_id=%s", method, request_id
                    )
                token = _current_request_id.set(request_id)
                try:
                    response = await self._dispatch(req)
                except Exception:
                    logger.exception(
                        "daemon_api_dispatch_error method=%s request_id=%s",
                        method,
                        request_id,
                    )
                    response = {"ok": False, "error": "internal", "message": "internal error"}
                finally:
                    _current_request_id.reset(token)
                if request_id:
                    response = {**response, "request_id": request_id}

            encoded = json.dumps(response).encode() + b"\n"
            writer.write(encoded)
            await writer.drain()
        except Exception:
            logger.exception(
                "daemon_api handle_client_write_error method=%s request_id=%s",
                method, request_id,
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                logger.debug("wait_closed error method=%s", method, exc_info=True)

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    async def _dispatch(self, req: dict) -> dict:
        """Route request to the appropriate handler by method name."""
        method = req.get("method", "")
        if method == "list_messages":
            return await self._list_messages(req)
        if method == "search_messages":
            return await self._search_messages(req)
        if method == "list_dialogs":
            return await self._list_dialogs(req)
        if method == "list_topics":
            return await self._list_topics(req)
        if method == "get_me":
            return await self._get_me(req)
        if method == "mark_dialog_for_sync":
            return await self._mark_dialog_for_sync(req)
        if method == "get_sync_status":
            return await self._get_sync_status(req)
        if method == "get_sync_alerts":
            return await self._get_sync_alerts(req)
        if method == "get_user_info":
            return await self._get_user_info(req)
        if method == "list_unread_messages":
            return await self._list_unread_messages(req)
        if method == "record_telemetry":
            return await self._record_telemetry(req)
        if method == "get_usage_stats":
            return await self._get_usage_stats(req)
        if method == "upsert_entities":
            return await self._upsert_entities(req)
        if method == "resolve_entity":
            return await self._resolve_entity(req)
        if method == "get_dialog_stats":
            return await self._get_dialog_stats(req)
        return {"ok": False, "error": "unknown_method"}

    # ------------------------------------------------------------------
    # Dialog name resolution
    # ------------------------------------------------------------------

    async def _resolve_dialog_name(self, dialog: str) -> int:
        """Resolve a dialog name string to a numeric dialog_id.

        Resolution order (fastest-first):
        1. client.get_entity() — handles @username, phone, invite link.
        2. entities table — exact/normalized/substring match against cached DB.
        3. iter_dialogs() — last resort for dialogs not yet in entities table.

        Returns telethon peer id (negative for channels/groups).
        Raises ValueError with descriptive message on failure.
        """
        try:
            entity = await self._client.get_entity(dialog)
            return int(telethon_utils.get_peer_id(entity))
        except (ValueError, KeyError):
            pass
        except Exception:
            logger.debug("get_entity failed for %r, falling back to entities DB", dialog, exc_info=True)

        # Fast path: look up in local entities table (O(1), no network).
        # Priority: exact name match > normalized exact > normalized substring.
        norm = latinize(dialog)
        row = self._conn.execute(
            """
            SELECT id FROM entities
            WHERE LOWER(name) = LOWER(?)
               OR name_normalized = ?
               OR (? != '' AND name_normalized LIKE '%' || ? || '%')
            ORDER BY
              CASE WHEN LOWER(name) = LOWER(?) THEN 0
                   WHEN name_normalized = ?     THEN 1
                   ELSE 2
              END
            LIMIT 1
            """,
            (dialog, norm, norm, norm, dialog, norm),
        ).fetchone()
        if row:
            logger.debug("resolve_dialog_entities_cache hit query=%r id=%d", dialog, row[0])
            return row[0]

        # Slow path: iterate dialogs via Telegram API (catches dialogs not yet in entities).
        logger.debug("resolve_dialog_fallback_iter_dialogs query=%r", dialog)
        matched_dialog: Any | None = None
        async for d in self._client.iter_dialogs():
            name = getattr(d, "name", "") or ""
            if name.lower() == dialog.lower():
                matched_dialog = d
                break
            if dialog.lower() in name.lower() and matched_dialog is None:
                matched_dialog = d

        if matched_dialog is not None:
            return int(telethon_utils.get_peer_id(matched_dialog.entity))

        raise ValueError(
            f"Dialog {dialog!r} not found. "
            "Check the dialog name or use dialog_id from ListDialogs."
        )

    async def _resolve_dialog_id(
        self, dialog_id: int, dialog: str | None,
    ) -> int | dict:
        """Resolve dialog_id from name if needed.

        Returns the resolved int dialog_id on success,
        or an error response dict on failure.
        """
        if not dialog_id and dialog:
            try:
                return await self._resolve_dialog_name(dialog)
            except ValueError as exc:
                return {"ok": False, "error": "dialog_not_found", "message": str(exc)}
        return dialog_id

    # ------------------------------------------------------------------
    # Shared message helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _msg_to_dict(
        msg: Any,
        dialog_id: int | None = None,
        self_id: int | None = None,
    ) -> dict:
        """Convert a live Telethon message object to the standard message dict.

        Used by the on-demand path in _list_messages and by _list_unread_messages
        to avoid duplicating the same field-extraction logic.

        Phase 39.1-02: when dialog_id and self_id are supplied, computes
        `effective_sender_id` and `is_service` in Python using the same
        decision tree as the EFFECTIVE_SENDER_ID_SQL CASE expression so the
        Telethon fallback path emits the same row shape as the sync.db path.
        """
        sender_first_name: str | None = None
        if getattr(msg, "sender", None) is not None:
            sender_first_name = getattr(msg.sender, "first_name", None)
        sent_at = 0
        if getattr(msg, "date", None) is not None:
            try:
                sent_at = int(msg.date.timestamp())
            except Exception:
                logger.debug("msg_to_dict timestamp conversion failed msg_id=%s", getattr(msg, "id", "?"), exc_info=True)
                sent_at = 0

        media = getattr(msg, "media", None)
        media_description: str | None = None
        if media is not None:
            from .formatter import _describe_media
            media_description = _describe_media(media)

        reactions_obj = getattr(msg, "reactions", None)
        reactions_display = ""
        if reactions_obj is not None:
            results_list = getattr(reactions_obj, "results", None) or []
            counts: list[tuple[str, int]] = []
            for item in results_list:
                reaction = getattr(item, "reaction", None)
                emoticon = getattr(reaction, "emoticon", None) if reaction else None
                count = getattr(item, "count", 0)
                if emoticon is not None:
                    counts.append((emoticon, int(count)))
            reactions_display = format_reaction_counts(counts)

        reply_to_msg_id, forum_topic_id = extract_reply_and_topic(msg)

        edit_date_raw = getattr(msg, "edit_date", None)
        edit_date: int | None = None
        if edit_date_raw is not None:
            try:
                edit_date = int(edit_date_raw.timestamp())
            except Exception:
                edit_date = None

        # Phase 39.1-02: mirror the SQL EFFECTIVE_SENDER_ID_SQL CASE tree in
        # Python so Telethon-fallback row dicts carry the same discriminators
        # as sync.db rows. Fields default to conservative values when inputs
        # are absent (e.g. pre-Plan-01 test fixtures).
        is_service_flag = 0
        try:
            from telethon.tl import types as _tl_types  # type: ignore[import-untyped]
            if isinstance(msg, _tl_types.MessageService):
                is_service_flag = 1
        except Exception:
            # Tests may pass bare MagicMock objects with no Telethon type.
            logger.debug("msg_to_dict: telethon MessageService isinstance check failed", exc_info=True)
        out_flag = 1 if getattr(msg, "out", False) else 0

        raw_sender_id = getattr(msg, "sender_id", None)
        effective_sender_id: int | None
        if raw_sender_id is not None:
            effective_sender_id = raw_sender_id
        elif is_service_flag == 1:
            effective_sender_id = None
        elif dialog_id is not None and dialog_id > 0 and out_flag == 1 and self_id is not None:
            effective_sender_id = self_id
        elif dialog_id is not None and dialog_id > 0 and out_flag == 0:
            effective_sender_id = dialog_id
        else:
            effective_sender_id = None

        return {
            "message_id": msg.id,
            "sent_at": sent_at,
            "text": getattr(msg, "message", None),
            "sender_id": raw_sender_id,
            "sender_first_name": sender_first_name,
            "media_description": media_description,
            "reply_to_msg_id": reply_to_msg_id,
            "forum_topic_id": forum_topic_id,
            "reactions_display": reactions_display,
            "is_deleted": 0,
            "edit_date": edit_date,
            "effective_sender_id": effective_sender_id,
            "is_service": is_service_flag,
            "out": out_flag,
            "dialog_id": dialog_id,
        }

    # ------------------------------------------------------------------
    # list_messages — helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _maybe_encode_next_nav(
        messages: list[dict],
        limit: int,
        dialog_id: int,
        direction: str,
        direction_enum: HistoryDirection,
    ) -> str | None:
        """Encode a next-page navigation token if the result set is full."""
        if messages and len(messages) == limit:
            last_msg_id = messages[-1]["message_id"]
            logger.debug(
                "list_messages_pagination anchor_msg_id=%d dialog_id=%d direction=%s%s",
                last_msg_id, dialog_id, direction, _rid(),
            )
            return encode_history_navigation(
                last_msg_id, dialog_id, direction=direction_enum
            )
        return None

    async def _freshen_reactions_if_stale(
        self, dialog_id: int, entity: Any, message_ids: list[int]
    ) -> None:
        """Per-message TTL-gated JIT reaction freshen (Phase 39.2 Plan 02).

        Looks up freshness rows for ``message_ids``; for any id whose
        ``checked_at > now - REACTIONS_TTL_SECONDS`` it skips. Fetches ONLY
        the stale subset from Telegram via ``client.get_messages(entity, ids=...)``
        in a single bounded round-trip. For each non-None Message returned,
        applies the reaction delta and upserts the freshness row. Partial
        ``None`` results retain their prior freshness state (AC-6-PARTIAL).

        On ``FloodWaitError``: warning logged; no DB mutation; stale cache
        remains served. Other exceptions: logged + swallowed; stale cache
        served. The fetch window is bounded to ``len(stale_ids)`` and is
        never expanded — never escalates to ``iter_messages`` or full-history.
        """
        if not message_ids:
            return
        # synced_dialogs gate: missing row → never freshen (e.g. unsynced dialog).
        row = self._conn.execute(
            "SELECT 1 FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)
        ).fetchone()
        if row is None:
            return

        now = int(time.time())
        threshold = now - REACTIONS_TTL_SECONDS
        placeholders = ",".join("?" * len(message_ids))
        fresh_rows = self._conn.execute(
            f"SELECT message_id FROM message_reactions_freshness "
            f"WHERE dialog_id = ? AND message_id IN ({placeholders}) "
            f"AND checked_at > ?",
            [dialog_id, *message_ids, threshold],
        ).fetchall()
        fresh_ids = {int(r[0]) for r in fresh_rows}
        stale_ids = [mid for mid in message_ids if mid not in fresh_ids]
        if not stale_ids:
            return  # AC-4: zero API cost

        try:
            messages = await self._client.get_messages(entity, ids=stale_ids)
        except FloodWaitError as exc:
            logger.warning(
                "jit_reactions_floodwait dialog_id=%d stale_count=%d seconds=%d",
                dialog_id,
                len(stale_ids),
                getattr(exc, "seconds", 0),
            )
            return  # AC-6: no freshness upsert, no reaction mutation
        except Exception:
            logger.exception("jit_reactions_failed dialog_id=%d", dialog_id)
            return

        # Telethon `get_messages(ids=list)` returns a list aligned to input order;
        # `None` for missing entries (AC-6-PARTIAL).
        with self._conn:
            for msg_id, msg in zip(stale_ids, messages):
                if msg is None:
                    continue
                rows = extract_reactions_rows(
                    dialog_id, msg_id, getattr(msg, "reactions", None)
                )
                apply_reactions_delta(self._conn, dialog_id, msg_id, rows)
                self._conn.execute(
                    "INSERT OR REPLACE INTO message_reactions_freshness "
                    "(dialog_id, message_id, checked_at) VALUES (?, ?, ?)",
                    (dialog_id, msg_id, now),
                )

    async def _resolve_unread_position(
        self, dialog_id: int, unread_after_id: int | None,
    ) -> int | None:
        """Resolve unread cutoff from synced_dialogs. Zero Telegram API calls.

        If unread_after_id is explicitly supplied, it wins.
        Otherwise reads synced_dialogs.read_inbox_max_id — if NULL or row
        missing, returns None (dialog not bootstrapped; caller skips unread filter).
        """
        if unread_after_id is not None:
            return unread_after_id
        row = self._conn.execute(_GET_READ_POSITION_SQL, (dialog_id,)).fetchone()
        if row and row[0] is not None:
            return int(row[0])
        return None

    async def _list_messages_from_db(
        self,
        *,
        dialog_id: int,
        limit: int,
        direction: str,
        direction_enum: HistoryDirection,
        anchor_msg_id: int | None,
        sender_id: int | None,
        sender_name: str | None,
        topic_id: int | None,
        unread_after_id: int | None,
    ) -> dict:
        """Read messages from sync.db using the dynamic query builder."""
        sql, params = _build_list_messages_query(
            dialog_id=dialog_id,
            limit=limit,
            self_id=self.self_id,
            direction=direction,
            anchor_msg_id=anchor_msg_id,
            sender_id=sender_id,
            sender_name=sender_name,
            topic_id=topic_id,
            unread_after_id=unread_after_id,
        )
        rows = self._conn.execute(sql, params).fetchall()
        messages = [
            dict(zip(_DB_MESSAGE_COLUMNS, r))
            for r in rows
        ]
        # Inject reaction counts from message_reactions table
        msg_ids = [m["message_id"] for m in messages]
        # Phase 39.2 Plan 02: TTL-gated JIT freshen for stale subset only.
        if msg_ids:
            await self._freshen_reactions_if_stale(dialog_id, dialog_id, msg_ids)
        reaction_map = _fetch_reaction_counts(self._conn, dialog_id, msg_ids)
        for m in messages:
            counts = reaction_map.get(m["message_id"])
            m["reactions_display"] = format_reaction_counts(counts) if counts else ""
        # Phase 39: observability counter. Emit ONCE on the sync.db success path.
        # Non-sync.db branches (Telegram fallback/error) intentionally do not emit this line.
        null_sender_rows = sum(1 for m in messages if m.get("sender_id") is None)
        unresolved_entity_rows = sum(
            1 for m in messages
            if m.get("sender_id") is not None and m.get("sender_first_name") is None
        )
        logger.info(
            "list_messages rendered",
            extra={
                "dialog_id": dialog_id,
                "rows": len(messages),
                "null_sender_rows": null_sender_rows,
                "unresolved_entity_rows": unresolved_entity_rows,
            },
        )
        next_nav = self._maybe_encode_next_nav(
            messages, limit, dialog_id, direction, direction_enum,
        )
        return {
            "ok": True,
            "data": {"messages": messages, "source": "sync_db", "next_navigation": next_nav},
        }

    async def _list_messages_context_window(
        self,
        *,
        dialog_id: int,
        anchor_message_id: int,
        context_size: int,
    ) -> dict:
        """Return messages centred on anchor_message_id from sync.db.

        Fetches up to context_size//2 messages before the anchor and up to
        context_size//2 after it (the anchor itself is included in the before
        half).  Results are returned in chronological order (oldest first).

        Only works for synced dialogs — callers must check sync status first.
        """
        half = max(1, context_size // 2)

        before_rows = self._conn.execute(
            _LIST_MESSAGES_BASE_SQL
            + " AND m.message_id <= :anchor ORDER BY m.message_id DESC LIMIT :limit",
            {
                "dialog_id": dialog_id,
                "self_id": self.self_id,
                "anchor": anchor_message_id,
                "limit": half + 1,
            },
        ).fetchall()

        after_rows = self._conn.execute(
            _LIST_MESSAGES_BASE_SQL
            + " AND m.message_id > :anchor ORDER BY m.message_id ASC LIMIT :limit",
            {
                "dialog_id": dialog_id,
                "self_id": self.self_id,
                "anchor": anchor_message_id,
                "limit": half,
            },
        ).fetchall()

        # before_rows are DESC — reverse to get chronological order, then append after
        rows = list(reversed(before_rows)) + list(after_rows)
        messages = [dict(zip(_DB_MESSAGE_COLUMNS, r)) for r in rows]
        # Inject reaction counts from message_reactions table
        msg_ids = [m["message_id"] for m in messages]
        # Phase 39.2 Plan 02: JIT freshen for the context-window slice.
        if msg_ids:
            await self._freshen_reactions_if_stale(dialog_id, dialog_id, msg_ids)
        reaction_map = _fetch_reaction_counts(self._conn, dialog_id, msg_ids)
        for m in messages:
            counts = reaction_map.get(m["message_id"])
            m["reactions_display"] = format_reaction_counts(counts) if counts else ""
        # Phase 39: observability counter — mirror main path so anchor branch is not a blind spot.
        null_sender_rows = sum(1 for m in messages if m.get("sender_id") is None)
        unresolved_entity_rows = sum(
            1 for m in messages
            if m.get("sender_id") is not None and m.get("sender_first_name") is None
        )
        logger.info(
            "list_messages rendered",
            extra={
                "dialog_id": dialog_id,
                "rows": len(messages),
                "null_sender_rows": null_sender_rows,
                "unresolved_entity_rows": unresolved_entity_rows,
            },
        )
        dialog_type = _dialog_type_from_db(self._conn, dialog_id)
        read_state = _read_state_for_dialog(self._conn, dialog_id, dialog_type)
        return {
            "ok": True,
            "data": {
                "messages": messages,
                "source": "sync_db",
                "anchor_message_id": anchor_message_id,
                "next_navigation": None,
                "dialog_type": dialog_type,
                "read_state": read_state,
            },
        }

    async def _list_messages_from_telegram(
        self,
        *,
        dialog_id: int,
        limit: int,
        direction: str,
        direction_enum: HistoryDirection,
        anchor_msg_id: int | None,
        sender_id: int | None,
        topic_id: int | None,
        unread_after_id: int | None,
    ) -> dict:
        """Fetch messages on-demand from Telegram API.

        Note: sender_name filtering is not supported on this path (Telegram
        iter_messages only accepts sender_id via from_user=).  The caller
        (_list_messages) intentionally omits sender_name from iter_kwargs.
        """
        logger.debug("list_messages_fallback_telegram dialog_id=%d%s", dialog_id, _rid())
        iter_kwargs: dict = {
            k: v
            for k, v in {
                "limit": limit,
                "offset_id": anchor_msg_id,
                "from_user": sender_id,
                "reply_to": topic_id,
                "min_id": unread_after_id,
                "reverse": True if direction == "oldest" else None,
            }.items()
            if v is not None
        }

        messages: list[dict] = []
        try:
            async for msg in self._client.iter_messages(dialog_id, **iter_kwargs):
                messages.append(self._msg_to_dict(msg, dialog_id=dialog_id, self_id=self.self_id))
        except Exception as exc:
            logger.warning(
                "list_messages_telegram_error dialog_id=%d error=%s%s",
                dialog_id, exc, _rid(), exc_info=True,
            )
            return {"ok": False, "error": "telegram_error", "message": "failed to fetch messages"}

        next_nav = self._maybe_encode_next_nav(
            messages, limit, dialog_id, direction, direction_enum,
        )
        return {
            "ok": True,
            "data": {"messages": messages, "source": "telegram", "next_navigation": next_nav},
        }

    # ------------------------------------------------------------------
    # list_messages — navigation decoding
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_history_navigation(
        navigation: str | None,
        dialog_id: int,
        direction: str,
    ) -> tuple[int | None, str] | dict:
        """Decode a history navigation token into (anchor_msg_id, direction).

        Returns a (anchor_msg_id, direction) tuple on success, or an error
        response dict on validation failure.
        """
        anchor_msg_id: int | None = None
        if navigation and navigation not in ("newest", "oldest"):
            try:
                nav = decode_navigation_token(navigation)
            except ValueError as exc:
                return {"ok": False, "error": "invalid_navigation", "message": str(exc)}
            if nav.kind != "history":
                return {
                    "ok": False,
                    "error": "invalid_navigation",
                    "message": f"Navigation token is for {nav.kind}, not history",
                }
            if nav.dialog_id != dialog_id:
                return {
                    "ok": False,
                    "error": "invalid_navigation",
                    "message": (
                        f"Navigation token belongs to dialog {nav.dialog_id}, "
                        f"not {dialog_id}"
                    ),
                }
            anchor_msg_id = nav.value
            if nav.direction is not None:
                direction = str(nav.direction)
        elif navigation == "oldest":
            direction = "oldest"
        return anchor_msg_id, direction

    # ------------------------------------------------------------------
    # list_messages — main handler
    # ------------------------------------------------------------------

    async def _list_messages(self, req: dict) -> dict:
        """Return messages from sync.db (if synced) or Telegram (on-demand).

        Request params:
          dialog_id       int — numeric dialog id (preferred)
          dialog          str — fuzzy dialog name (resolved via _resolve_dialog_name)
          limit           int — max messages (clamped 1..500, default 50)
          direction       "newest" (default) | "oldest"
          sender_id       int — filter by sender_id; takes precedence over sender_name
          sender_name     str — filter by sender name LIKE (sync.db only)
          topic_id        int — filter by forum_topic_id
          unread_after_id int — filter message_id > X
          unread          bool — auto-resolve read position via GetPeerDialogsRequest
          navigation      str — opaque base64 cursor or "newest"/"oldest" sentinel

        Response on success:
          {"ok": True, "data": {"messages": [...], "source": "sync_db"|"telegram",
                                "next_navigation": str|None}}

        Each message dict contains: message_id, sent_at, text, sender_id,
        sender_first_name, media_description, reply_to_msg_id, forum_topic_id,
        reactions_display (str, sync.db only), is_deleted, edit_date (int|None), topic_title (str|None, sync.db only).

        Errors: dialog_not_found, missing_dialog, invalid_navigation.
        """
        dialog_id: int = req.get("dialog_id", 0) or 0
        dialog: str | None = req.get("dialog")
        limit: int = _clamp(req.get("limit", 50), 1, 500)
        navigation: str | None = req.get("navigation")
        direction: str = req.get("direction", "newest")
        sender_id: int | None = req.get("sender_id")
        sender_name: str | None = req.get("sender_name")
        topic_id: int | None = req.get("topic_id")
        unread_after_id: int | None = req.get("unread_after_id")
        unread: bool = bool(req.get("unread"))
        context_message_id: int | None = req.get("context_message_id")
        context_size: int = _clamp(req.get("context_size", 10), 2, 50)

        if direction not in ("newest", "oldest"):
            direction = "newest"

        resolved = await self._resolve_dialog_id(dialog_id, dialog)
        if isinstance(resolved, dict):
            return resolved
        dialog_id = resolved

        if not dialog_id:
            return {
                "ok": False,
                "error": "missing_dialog",
                "message": "Either dialog_id or dialog name is required",
            }

        if context_message_id is not None:
            row = self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)).fetchone()
            if row is None or row[0] not in ("synced", "syncing"):
                return {
                    "ok": False,
                    "error": "not_synced",
                    "message": (
                        "Context window requires the dialog to be synced. "
                        "Use MarkDialogForSync first."
                    ),
                }
            return await self._list_messages_context_window(
                dialog_id=dialog_id,
                anchor_message_id=context_message_id,
                context_size=context_size,
            )

        nav_result = self._decode_history_navigation(navigation, dialog_id, direction)
        if isinstance(nav_result, dict):
            return nav_result
        anchor_msg_id, direction = nav_result

        direction_enum = (
            HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST
        )

        if unread:
            unread_after_id = await self._resolve_unread_position(dialog_id, unread_after_id)

        row = self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)).fetchone()
        status = row[0] if row is not None else None

        dialog_type = _dialog_type_from_db(self._conn, dialog_id)
        read_state = _read_state_for_dialog(self._conn, dialog_id, dialog_type)

        if status in ("synced", "syncing", "access_lost"):
            result = await self._list_messages_from_db(
                dialog_id=dialog_id,
                limit=limit,
                direction=direction,
                direction_enum=direction_enum,
                anchor_msg_id=anchor_msg_id,
                sender_id=sender_id,
                sender_name=sender_name,
                topic_id=topic_id,
                unread_after_id=unread_after_id,
            )
            result["data"].update(_build_access_metadata(self._conn, dialog_id, status))
            result["data"]["dialog_type"] = dialog_type
            result["data"]["read_state"] = read_state
            return result

        telegram_result = await self._list_messages_from_telegram(
            dialog_id=dialog_id,
            limit=limit,
            direction=direction,
            direction_enum=direction_enum,
            anchor_msg_id=anchor_msg_id,
            sender_id=sender_id,
            topic_id=topic_id,
            unread_after_id=unread_after_id,
        )
        if telegram_result.get("ok"):
            telegram_result["data"]["dialog_access"] = "live"
            telegram_result["data"]["dialog_type"] = dialog_type
            telegram_result["data"]["read_state"] = read_state
        return telegram_result

    # ------------------------------------------------------------------
    # search_messages
    # ------------------------------------------------------------------

    async def _search_messages(self, req: dict) -> dict:
        """FTS5 stemmed full-text search against messages_fts.

        Global mode (dialog_id=0, dialog=None): searches all synced dialogs.
        Each result includes dialog_id and dialog_name for identification.

        Scoped mode (dialog provided): searches within one dialog only.
        """
        dialog_id: int = req.get("dialog_id", 0) or 0
        dialog: str | None = req.get("dialog")
        query: str = req.get("query", "")
        limit: int = _clamp(req.get("limit", 20), 1, 200)
        offset: int = max(0, req.get("offset", 0))

        global_mode = not dialog_id and dialog is None

        if not global_mode:
            resolved = await self._resolve_dialog_id(dialog_id, dialog)
            if isinstance(resolved, dict):
                return resolved
            dialog_id = resolved

        # Stem the query
        stemmed = stem_query(query)
        if not stemmed:
            return {"ok": True, "data": {"messages": [], "total": 0}}

        # Phase 39.2 Plan 02: global search (dialog_id=None) is OUT of JIT scope.
        # No single dialog → per-message freshness gate is not well-defined across
        # dialogs. Global mode serves best-effort cached reactions only.
        if global_mode:
            rows = self._conn.execute(
                _SELECT_FTS_ALL_SQL,
                {
                    "query": stemmed,
                    "limit": limit,
                    "offset": offset,
                    "self_id": self.self_id,
                },
            ).fetchall()
            messages = [
                {
                    "message_id": r[0],
                    "text": r[1],
                    "sender_first_name": r[2],
                    "sent_at": r[3],
                    "media_description": r[4],
                    "reply_to_msg_id": r[5],
                    "sender_id": r[6],
                    "forum_topic_id": r[7],
                    "dialog_id": r[8],
                    "dialog_name": r[9],
                    "effective_sender_id": r[10],
                    "is_service": r[11],
                    "out": r[12],
                    # Global search: results span multiple dialogs. Skip reaction injection --
                    # fetching reactions per-dialog for a cross-dialog result set adds complexity
                    # with little value (search results are for finding messages, not analyzing
                    # reactions). This is an intentional design decision, not a bug.
                    "reactions_display": "",
                }
                for r in rows
            ]
        else:
            rows = self._conn.execute(
                _SELECT_FTS_SQL,
                {
                    "query": stemmed,
                    "dialog_id": dialog_id,
                    "limit": limit,
                    "offset": offset,
                    "self_id": self.self_id,
                },
            ).fetchall()
            messages = [
                {
                    "message_id": r[0],
                    "text": r[1],
                    "sender_first_name": r[2],
                    "sent_at": r[3],
                    "media_description": r[4],
                    "reply_to_msg_id": r[5],
                    "sender_id": r[6],
                    "forum_topic_id": r[7],
                    "effective_sender_id": r[8],
                    "is_service": r[9],
                    "out": r[10],
                    "dialog_id": r[11],
                }
                for r in rows
            ]
            # Scoped search: single dialog_id -- inject reactions
            if dialog_id:
                msg_ids = [r["message_id"] for r in messages]
                # Phase 39.2 Plan 02: scoped search → JIT freshen for the slice.
                # Global search (dialog_id is None / 0) is OUT of JIT scope per
                # CONTEXT.md §Out of scope — no single dialog for per-message gate.
                if msg_ids:
                    await self._freshen_reactions_if_stale(
                        dialog_id, dialog_id, msg_ids
                    )
                reaction_map = _fetch_reaction_counts(self._conn, dialog_id, msg_ids)
                for r in messages:
                    counts = reaction_map.get(r["message_id"])
                    r["reactions_display"] = format_reaction_counts(counts) if counts else ""
            else:
                for r in messages:
                    r["reactions_display"] = ""

        next_nav: str | None = None
        if messages and len(messages) == limit:
            next_offset = offset + limit
            nav_dialog_id = 0 if global_mode else dialog_id
            next_nav = encode_search_navigation(next_offset, nav_dialog_id, query)

        # Phase 39.3-03 Task 2: build read_state_per_dialog for every distinct
        # dialog_id appearing in results. Only DMs (dialog_type == "User") are
        # included — non-DM hits are absent from the map (documented HIGH-1
        # resolution: per-dialog header block only covers DMs).
        distinct_dialog_ids = {m["dialog_id"] for m in messages if m.get("dialog_id")}
        read_state_per_dialog: dict[int, dict] = {}
        for did in distinct_dialog_ids:
            dt = _dialog_type_from_db(self._conn, did)
            rs = _read_state_for_dialog(self._conn, did, dt)
            if rs is not None:
                read_state_per_dialog[did] = rs

        # Enrich with access metadata for scoped searches
        if not global_mode and dialog_id:
            row = self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)).fetchone()
            scoped_status = row[0] if row else None
            access_meta = _build_access_metadata(
                self._conn, dialog_id, scoped_status or "not_synced"
            )
            return {
                "ok": True,
                "data": {
                    "messages": messages,
                    "total": len(messages),
                    "next_navigation": next_nav,
                    "read_state_per_dialog": read_state_per_dialog,
                    **access_meta,
                },
            }

        # Global mode: no single dialog_access (results span multiple dialogs)
        return {
            "ok": True,
            "data": {
                "messages": messages,
                "total": len(messages),
                "next_navigation": next_nav,
                "read_state_per_dialog": read_state_per_dialog,
            },
        }

    # ------------------------------------------------------------------
    # list_dialogs
    # ------------------------------------------------------------------

    async def _list_dialogs(self, req: dict) -> dict:
        """Return live dialog list from Telegram enriched with sync_status.

        Request: exclude_archived (bool), ignore_pinned (bool).
        Response data: {"dialogs": [{"id", "name", "type", "last_message_at",
        "unread_count", "members", "created", "sync_status"}, ...]}.
        """
        # Load current sync statuses for O(1) lookup
        synced_rows = self._conn.execute(_SELECT_SYNCED_STATUSES_SQL).fetchall()
        synced_meta: dict[int, tuple[str, int | None, int | None]] = {
            row[0]: (row[1], row[2], row[3]) for row in synced_rows
        }
        local_counts: dict[int, int] = dict(
            self._conn.execute(_COUNT_MESSAGES_BY_DIALOG_SQL).fetchall()
        )

        exclude_archived: bool = req.get("exclude_archived", False)
        ignore_pinned: bool = req.get("ignore_pinned", False)

        archived_filter = False if exclude_archived else None

        dialogs = []
        try:
            async for d in self._client.iter_dialogs(
                archived=archived_filter,
                ignore_pinned=ignore_pinned,
            ):
                entity = getattr(d, "entity", None)
                entity_type = _classify_dialog_type(entity)
                last_msg_at: int | None = None
                if getattr(d, "date", None) is not None:
                    try:
                        last_msg_at = int(d.date.timestamp())
                    except Exception:
                        last_msg_at = None
                members = getattr(entity, "participants_count", None) if entity is not None else None
                created_ts: int | None = None
                entity_date = getattr(entity, "date", None)
                if entity_date is not None:
                    try:
                        created_ts = int(entity_date.timestamp())
                    except Exception:
                        pass

                sync_meta_row = synced_meta.get(d.id, ("not_synced", None, None))
                sync_status = sync_meta_row[0]
                total_messages = sync_meta_row[1]
                access_lost_at = sync_meta_row[2]
                local_count = local_counts.get(d.id, 0)
                coverage_pct = _compute_sync_coverage(total_messages, local_count)
                dialogs.append(
                    {
                        "id": d.id,
                        "name": getattr(d, "name", None),
                        "type": entity_type,
                        "last_message_at": last_msg_at,
                        "unread_count": getattr(d, "unread_count", 0),
                        "members": members,
                        "created": created_ts,
                        "sync_status": sync_status,
                        "sync_coverage_pct": coverage_pct,
                        "access_lost_at": access_lost_at,
                    }
                )
        except Exception as exc:
            logger.warning("list_dialogs_telegram_error error=%s", exc, exc_info=True)
            return {"ok": False, "error": "telegram_error", "message": "failed to list dialogs"}

        return {"ok": True, "data": {"dialogs": dialogs}}

    # ------------------------------------------------------------------
    # list_topics
    # ------------------------------------------------------------------

    async def _list_topics(self, req: dict) -> dict:
        """Return forum topics for a dialog via Telegram API.

        Request: dialog_id (int) or dialog (str).
        Response data: {"topics": [{"id", "title", "icon_emoji_id", "date"}],
        "dialog_id": int}.
        Errors: entity_not_found, topics_fetch_failed, missing_dialog.
        """
        dialog_id: int = req.get("dialog_id", 0) or 0
        dialog: str | None = req.get("dialog")

        resolved = await self._resolve_dialog_id(dialog_id, dialog)
        if isinstance(resolved, dict):
            return resolved
        dialog_id = resolved

        if not dialog_id:
            return {
                "ok": False,
                "error": "missing_dialog",
                "message": "Either dialog_id or dialog name is required for list_topics",
            }

        try:
            entity = await self._client.get_entity(dialog_id)
        except Exception as exc:
            logger.warning("get_entity failed for dialog_id=%s: %s%s", dialog_id, exc, _rid())
            return {
                "ok": False,
                "error": "entity_not_found",
                "message": "telegram API error",
            }

        try:
            result = await self._client(
                GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                )
            )
            topics = [
                {
                    "id": t.id,
                    "title": getattr(t, "title", None),
                    "icon_emoji_id": getattr(t, "icon_emoji_id", None),
                    "date": int(t.date.timestamp()) if hasattr(getattr(t, "date", None), "timestamp") else getattr(t, "date", None),
                }
                for t in getattr(result, "topics", [])
            ]
        except Exception as exc:
            logger.warning("topics fetch failed for dialog_id=%s: %s%s", dialog_id, exc, _rid())
            return {
                "ok": False,
                "error": "topics_fetch_failed",
                "message": "telegram API error",
            }

        return {"ok": True, "data": {"topics": topics, "dialog_id": dialog_id}}

    # ------------------------------------------------------------------
    # get_me
    # ------------------------------------------------------------------

    async def _get_me(self, req: dict) -> dict:
        """Return current user info from Telegram.

        Request: no parameters.
        Response data: {"id", "first_name", "last_name", "username"}.
        Errors: telegram_error, not_found.
        """
        # Note: this path returns the full User object (name, username). The
        # lightweight `self.self_id` cached at startup is used by query-build
        # paths (Plan 39.1-02); this handler still fetches full profile on
        # demand because callers want display fields, not just the id.
        try:
            me = await self._client.get_me()
        except Exception as exc:
            logger.warning("get_me_failed error=%s", exc, exc_info=True)
            return {"ok": False, "error": "telegram_error", "message": "failed to retrieve account info"}
        if me is None:
            return {"ok": False, "error": "not_found", "message": "account info unavailable"}
        return {
            "ok": True,
            "data": {
                "id": me.id,
                "first_name": me.first_name,
                "last_name": me.last_name,
                "username": me.username,
            },
        }

    # ------------------------------------------------------------------
    # mark_dialog_for_sync
    # ------------------------------------------------------------------

    async def _mark_dialog_for_sync(self, req: dict) -> dict:
        """Add or remove a dialog from sync scope in synced_dialogs.

        enable=True: INSERT OR IGNORE with status='not_synced' (daemon picks
        up the new dialog within one heartbeat interval).
        enable=False: UPDATE status back to 'not_synced' (re-queues dialog
        for a full re-sync on the next daemon cycle; local messages are kept).
        """
        dialog_id: int = req.get("dialog_id", 0)
        enable: bool = req.get("enable", True)
        if enable:
            self._conn.execute(_MARK_FOR_SYNC_SQL, (dialog_id,))
        else:
            self._conn.execute(_UNMARK_SYNC_SQL, (dialog_id,))
        self._conn.commit()
        logger.info("mark_dialog_for_sync dialog_id=%d enable=%s", dialog_id, enable)
        return {"ok": True}

    # ------------------------------------------------------------------
    # get_sync_status
    # ------------------------------------------------------------------

    async def _get_sync_status(self, req: dict) -> dict:
        """Return sync status and message statistics for a dialog.

        delete_detection is derived from dialog_id sign:
        - Negative → channel/supergroup → "reliable (channel)"
        - Positive → DM/small group → "best-effort weekly (DM)"
        """
        dialog_id: int = req.get("dialog_id", 0)
        row = self._conn.execute(_GET_SYNC_STATUS_SQL, (dialog_id,)).fetchone()

        if row is not None:
            status: str = row[0]
            last_synced_at: int | None = row[1]
            last_event_at: int | None = row[2]
            sync_progress: int | None = row[3]
            total_messages: int | None = row[4]
            access_lost_at: int | None = row[5]
        else:
            status = "not_synced"
            last_synced_at = None
            last_event_at = None
            sync_progress = None
            total_messages = None
            access_lost_at = None

        count_row = self._conn.execute(_COUNT_SYNCED_MESSAGES_SQL, (dialog_id,)).fetchone()
        message_count: int = count_row[0] if count_row is not None else 0

        sync_coverage_pct = _compute_sync_coverage(total_messages, message_count)
        delete_detection = "reliable (channel)" if dialog_id < 0 else "best-effort weekly (DM)"

        data: dict = {
            "dialog_id": dialog_id,
            "status": status,
            "message_count": message_count,
            "last_synced_at": last_synced_at,
            "last_event_at": last_event_at,
            "sync_progress": sync_progress,
            "total_messages": total_messages,
            "delete_detection": delete_detection,
            "sync_coverage_pct": sync_coverage_pct,
            "access_lost_at": access_lost_at,
        }
        if status == "access_lost" and total_messages is None:
            data["archived_message_count"] = message_count
        return {"ok": True, "data": data}

    # ------------------------------------------------------------------
    # get_sync_alerts
    # ------------------------------------------------------------------

    async def _get_sync_alerts(self, req: dict) -> dict:
        """Return sync alerts: deleted messages, edit history, access-lost dialogs.

        since: unix timestamp — only return alerts newer than this value (default 0).
        limit: max items per category (default 50).
        """
        since: int = req.get("since", 0)
        limit: int = _clamp(req.get("limit", 50), 1, 500)

        deleted_rows = self._conn.execute(_GET_DELETED_ALERTS_SQL, (since, limit)).fetchall()
        deleted_messages = [
            {
                "dialog_id": r[0],
                "message_id": r[1],
                "text": r[2],
                "deleted_at": r[3],
            }
            for r in deleted_rows
        ]

        edit_rows = self._conn.execute(_GET_EDIT_ALERTS_SQL, (since, limit)).fetchall()
        edits = [
            {
                "dialog_id": r[0],
                "message_id": r[1],
                "version": r[2],
                "old_text": r[3],
                "edit_date": r[4],
            }
            for r in edit_rows
        ]

        access_lost_rows = self._conn.execute(_GET_ACCESS_LOST_ALERTS_SQL, (since,)).fetchall()
        access_lost = [
            {
                "dialog_id": r[0],
                "access_lost_at": r[1],
            }
            for r in access_lost_rows
        ]

        return {
            "ok": True,
            "data": {
                "deleted_messages": deleted_messages,
                "edits": edits,
                "access_lost": access_lost,
            },
        }

    # ------------------------------------------------------------------
    # get_user_info
    # ------------------------------------------------------------------

    @staticmethod
    def _format_user_status(status: object) -> dict | None:
        """Serialize a Telethon UserStatus object to a plain dict.

        Returns None for UserStatusEmpty or missing status.
        """
        if status is None:
            return None
        type_name = type(status).__name__
        if type_name == "UserStatusOnline":
            expires = getattr(status, "expires", None)
            return {"type": "online", "expires": expires.isoformat() if expires else None}
        if type_name == "UserStatusOffline":
            was_online = getattr(status, "was_online", None)
            return {"type": "offline", "was_online": was_online.isoformat() if was_online else None}
        if type_name == "UserStatusRecently":
            return {"type": "recently"}
        if type_name == "UserStatusLastWeek":
            return {"type": "last_week"}
        if type_name == "UserStatusLastMonth":
            return {"type": "last_month"}
        return None  # UserStatusEmpty or unknown

    async def _get_user_info(self, req: dict) -> dict:
        """Return user profile and list of common chats.

        Calls client.get_entity(user_id) and GetCommonChatsRequest to build
        a complete user profile dict with typed common_chats entries.
        """
        user_id: int = req.get("user_id", 0)
        try:
            user = await self._client.get_entity(user_id)
        except Exception as exc:
            logger.warning("get_entity failed for user_id=%s: %s%s", user_id, exc, _rid())
            return {"ok": False, "error": "user_not_found", "message": "telegram API error"}

        # Fetch common chats (only available for user entities)
        common_chats: list[dict] = []
        try:
            common_result = await self._client(
                GetCommonChatsRequest(user_id=user_id, max_id=0, limit=100)
            )
            for chat in getattr(common_result, "chats", []):
                if isinstance(chat, Channel):
                    chat_type = "supergroup" if getattr(chat, "megagroup", False) else "channel"
                elif isinstance(chat, Chat):
                    chat_type = "group"
                else:
                    chat_type = "user"

                common_chats.append({
                    "id": int(telethon_utils.get_peer_id(chat)),
                    "name": getattr(chat, "title", None) or str(chat.id),
                    "type": chat_type,
                })
        except Exception as exc:
            logger.warning("get_user_info common_chats_failed user_id=%r error=%s%s", user_id, exc, _rid(), exc_info=True)

        # Fetch full user profile
        about: str | None = None
        personal_channel_id: int | None = None
        birthday: dict | None = None
        blocked: bool = False
        ttl_period: int | None = None
        private_forward_name: str | None = None
        bot_info: dict | None = None
        business_location: dict | None = None
        business_intro: dict | None = None
        business_work_hours: dict | None = None
        note: str | None = None
        folder_id: int | None = None
        folder_name: str | None = None
        try:
            full_result = await self._client(GetFullUserRequest(id=user_id))
            user_full = full_result.full_user
            about = getattr(user_full, "about", None) or None
            personal_channel_id = getattr(user_full, "personal_channel_id", None)
            blocked = bool(getattr(user_full, "blocked", False))
            ttl_period = getattr(user_full, "ttl_period", None)
            private_forward_name = getattr(user_full, "private_forward_name", None) or None
            folder_id = getattr(user_full, "folder_id", None)

            bday = getattr(user_full, "birthday", None)
            if bday is not None:
                birthday = {
                    "day": getattr(bday, "day", None),
                    "month": getattr(bday, "month", None),
                    "year": getattr(bday, "year", None),
                }

            raw_bot_info = getattr(user_full, "bot_info", None)
            if raw_bot_info is not None:
                commands = []
                for cmd in getattr(raw_bot_info, "commands", None) or []:
                    commands.append({
                        "command": getattr(cmd, "command", ""),
                        "description": getattr(cmd, "description", ""),
                    })
                bot_info = {
                    "description": getattr(raw_bot_info, "description", None) or None,
                    "commands": commands,
                }

            raw_loc = getattr(user_full, "business_location", None)
            if raw_loc is not None:
                geo = getattr(raw_loc, "geo_point", None)
                business_location = {
                    "address": getattr(raw_loc, "address", None),
                    "lat": getattr(geo, "lat", None) if geo else None,
                    "long": getattr(geo, "long", None) if geo else None,
                }

            raw_intro = getattr(user_full, "business_intro", None)
            if raw_intro is not None:
                business_intro = {
                    "title": getattr(raw_intro, "title", None),
                    "description": getattr(raw_intro, "description", None),
                }

            raw_hours = getattr(user_full, "business_work_hours", None)
            if raw_hours is not None:
                business_work_hours = {
                    "timezone": getattr(raw_hours, "timezone_id", None),
                }

            raw_note = getattr(user_full, "note", None)
            if raw_note is not None:
                note = getattr(raw_note, "text", None) or None

        except Exception as exc:
            logger.warning("get_user_info full_user_failed user_id=%r error=%s%s", user_id, exc, _rid(), exc_info=True)

        # Resolve folder_id → folder name
        if folder_id is not None:
            try:
                filters = await self._client(GetDialogFiltersRequest())
                for f in filters or []:
                    if getattr(f, "id", None) == folder_id:
                        raw_title = getattr(f, "title", None)
                        # title may be str or TextWithEntities
                        folder_name = getattr(raw_title, "text", raw_title) if raw_title else None
                        break
            except Exception as exc:
                logger.warning("get_user_info folder_resolve_failed folder_id=%r error=%s%s", folder_id, exc, _rid(), exc_info=True)

        # Additional usernames (Telegram allows multiple active usernames)
        extra_usernames: list[str] = []
        for uname in getattr(user, "usernames", None) or []:
            name_str = getattr(uname, "username", None)
            if name_str and name_str != getattr(user, "username", None):
                extra_usernames.append(name_str)

        emoji_status = getattr(user, "emoji_status", None)
        emoji_status_id: int | None = None
        if emoji_status is not None:
            emoji_status_id = getattr(emoji_status, "document_id", None)

        restriction_reason: list[dict] = []
        for rr in getattr(user, "restriction_reason", None) or []:
            restriction_reason.append({
                "platform": getattr(rr, "platform", None),
                "reason": getattr(rr, "reason", None),
                "text": getattr(rr, "text", None),
            })

        return {
            "ok": True,
            "data": {
                "id": user.id,
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "username": getattr(user, "username", None),
                "extra_usernames": extra_usernames,
                "emoji_status_id": emoji_status_id,
                "status": self._format_user_status(getattr(user, "status", None)),
                "phone": getattr(user, "phone", None),
                "lang_code": getattr(user, "lang_code", None),
                "contact": bool(getattr(user, "contact", False)),
                "mutual_contact": bool(getattr(user, "mutual_contact", False)),
                "close_friend": bool(getattr(user, "close_friend", False)),
                "send_paid_messages_stars": getattr(user, "send_paid_messages_stars", None),
                "about": about,
                "personal_channel_id": personal_channel_id,
                "birthday": birthday,
                "verified": bool(getattr(user, "verified", False)),
                "premium": bool(getattr(user, "premium", False)),
                "bot": bool(getattr(user, "bot", False)),
                "scam": bool(getattr(user, "scam", False)),
                "fake": bool(getattr(user, "fake", False)),
                "restricted": bool(getattr(user, "restricted", False)),
                "restriction_reason": restriction_reason,
                "blocked": blocked,
                "ttl_period": ttl_period,
                "private_forward_name": private_forward_name,
                "bot_info": bot_info,
                "business_location": business_location,
                "business_intro": business_intro,
                "business_work_hours": business_work_hours,
                "note": note,
                "folder_id": folder_id,
                "folder_name": folder_name,
                "common_chats": common_chats,
            },
        }

    # ------------------------------------------------------------------
    # list_unread_messages
    # ------------------------------------------------------------------

    async def _list_unread_messages(self, req: dict) -> dict:
        """Return prioritized unread messages across dialogs.

        Request: scope ("personal"|"all"), limit (int, 1-500),
        group_size_threshold (int).
        Response data: {"groups": [{"dialog_id", "display_name", "tier",
        "category", "unread_count", "unread_mentions_count",
        "messages": [{"message_id", "sent_at", "text", ...}]}]}.
        """
        scope: str = req.get("scope", "personal")
        limit: int = _clamp(req.get("limit", 100), 1, 500)
        group_size_threshold: int = req.get("group_size_threshold", 100)

        unread_dialogs, unread_counts = await self._collect_unread_dialogs(scope, group_size_threshold)
        self._rank_unread_entries(unread_dialogs)
        allocation = allocate_message_budget_proportional(unread_counts, limit)
        groups = await self._fetch_unread_groups(unread_dialogs, allocation)

        pending_row = self._conn.execute(_COUNT_BOOTSTRAP_PENDING_SQL).fetchone()
        bootstrap_pending = int(pending_row[0]) if pending_row else 0
        return {"ok": True, "data": {"groups": groups, "bootstrap_pending": bootstrap_pending}}

    @staticmethod
    def _should_include_unread_dialog(
        category: str,
        scope: str,
        participants_count: int | None,
        group_size_threshold: int,
    ) -> bool:
        """Decide whether a dialog should be included in unread results."""
        if scope != "personal":
            return True
        if category == "channel":
            return False
        if (
            category == "group"
            and participants_count is not None
            and participants_count > group_size_threshold
        ):
            return False
        return True

    async def _collect_unread_dialogs(
        self, scope: str, group_size_threshold: int
    ) -> tuple[list[dict], dict[int, int]]:
        """Return unread dialog entries from sync.db. Zero Telegram API calls.

        Uses a single grouped query (_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL)
        — scalar subquery computes unread_count per dialog in one round trip.
        Excludes dialogs with read_inbox_max_id IS NULL (not yet bootstrapped).
        See _list_unread_messages for bootstrap_pending visibility.
        """
        rows = self._conn.execute(_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL).fetchall()

        _ENTITY_TYPE_TO_CATEGORY: dict[str, str] = {
            "User": "user", "Bot": "bot", "Channel": "channel",
        }

        unread_dialogs: list[dict] = []
        unread_counts: dict[int, int] = {}

        for row in rows:
            dialog_id, read_max, last_event_at, display_name, entity_type, unread_count = row
            if unread_count == 0:
                continue

            category = _ENTITY_TYPE_TO_CATEGORY.get(entity_type, "group")

            # participants_count=None — not stored in sync.db, so group_size_threshold
            # has no effect here. _should_include_unread_dialog treats None as permissive
            # for groups (all groups pass regardless of size).
            # TODO: persist participants_count in entities to enable threshold filtering.
            if not self._should_include_unread_dialog(
                category, scope, None, group_size_threshold,
            ):
                continue

            unread_dialogs.append({
                "chat_id": dialog_id,
                "display_name": display_name,
                "unread_count": int(unread_count),
                "unread_mentions_count": 0,  # not stored — see RESEARCH open question #1
                "category": category,
                "date": last_event_at,  # int unix ts (NOT datetime — see _rank_unread_entries)
                "read_inbox_max_id": read_max,
            })
            unread_counts[dialog_id] = int(unread_count)

        return unread_dialogs, unread_counts

    @staticmethod
    def _rank_unread_entries(entries: list[dict]) -> None:
        """Assign priority tiers and sort in place (lower tier = higher priority)."""
        for entry in entries:
            entry["tier"] = unread_chat_tier({
                "unread_mentions_count": entry["unread_mentions_count"],
                "category": entry["category"],
            })
        # date is last_event_at (int unix timestamp) after Plan 38-02 rewrite — not datetime
        entries.sort(key=lambda e: (e["tier"], -(e["date"] if e["date"] else 0)))

    async def _fetch_unread_groups(
        self, entries: list[dict], allocation: dict[int, int]
    ) -> list[dict]:
        """Fetch unread message bodies from sync.db. Zero Telegram API calls."""
        groups: list[dict] = []
        for entry in entries:
            chat_id = entry["chat_id"]
            budget = allocation.get(chat_id, 0)
            # Phase 39.3-03 Task 2: include dialog_type + read_state per group
            # so the tool layer can render a per-chat header block (HIGH-3).
            dialog_type = _dialog_type_from_db(self._conn, chat_id)
            read_state = _read_state_for_dialog(self._conn, chat_id, dialog_type)
            group: dict = {
                "dialog_id": chat_id,
                "display_name": entry["display_name"],
                "tier": entry["tier"],
                "category": entry["category"],
                "unread_count": entry["unread_count"],
                "unread_mentions_count": entry["unread_mentions_count"],
                "dialog_type": dialog_type,
                "read_state": read_state,
                "messages": [],
            }
            if budget == 0:
                groups.append(group)  # always include summary even with no messages
                continue

            rows = self._conn.execute(
                _FETCH_UNREAD_MESSAGES_SQL,
                {
                    "dialog_id": chat_id,
                    "after_msg_id": entry["read_inbox_max_id"],
                    "limit": budget,
                    "self_id": self.self_id,
                },
            ).fetchall()
            group_messages = [
                {
                    "message_id": r[0],
                    "sent_at": r[1],
                    "text": r[2],
                    "sender_id": r[3],
                    "sender_first_name": r[4],
                    "effective_sender_id": r[5],
                    "is_service": r[6],
                    "out": r[7],
                    "dialog_id": r[8],
                }
                for r in rows
            ]
            # Phase 39.2 Plan 02: per-dialog JIT freshen + reactions injection.
            # Multi-dialog call → group by dialog (already grouped by entry/chat_id).
            msg_ids = [m["message_id"] for m in group_messages]
            if msg_ids:
                await self._freshen_reactions_if_stale(chat_id, chat_id, msg_ids)
                reaction_map = _fetch_reaction_counts(self._conn, chat_id, msg_ids)
                for m in group_messages:
                    counts = reaction_map.get(m["message_id"])
                    m["reactions_display"] = (
                        format_reaction_counts(counts) if counts else ""
                    )
            group["messages"] = group_messages
            groups.append(group)

        return groups

    # ------------------------------------------------------------------
    # record_telemetry
    # ------------------------------------------------------------------

    _TELEMETRY_TTL_SECONDS = 30 * 86400  # 30 days

    async def _record_telemetry(self, req: dict) -> dict:
        """Write a telemetry event row to sync.db telemetry_events table.

        Evicts rows older than 30 days on every write to prevent unbounded growth.
        """
        event = req.get("event", {})
        tool_name = event.get("tool_name", "")
        if not isinstance(tool_name, str) or len(tool_name) > 200:
            return {"ok": False, "error": "invalid_input", "message": "tool_name must be a string (max 200 chars)"}
        try:
            self._conn.execute(
                "INSERT INTO telemetry_events "
                "(tool_name, timestamp, duration_ms, result_count, "
                "has_cursor, page_depth, has_filter, error_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.get("tool_name"),
                    event.get("timestamp"),
                    event.get("duration_ms"),
                    event.get("result_count"),
                    event.get("has_cursor"),
                    event.get("page_depth"),
                    event.get("has_filter"),
                    event.get("error_type"),
                ),
            )
            cutoff = time.time() - self._TELEMETRY_TTL_SECONDS
            self._conn.execute(
                "DELETE FROM telemetry_events WHERE timestamp < ?", (cutoff,)
            )
            self._conn.commit()
            return {"ok": True}
        except Exception as exc:
            logger.error("record_telemetry failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "internal", "message": "internal error"}

    # ------------------------------------------------------------------
    # get_usage_stats
    # ------------------------------------------------------------------

    async def _get_usage_stats(self, req: dict) -> dict:
        """Return usage statistics from sync.db telemetry_events.

        Request: since (int, unix timestamp; default 30 days ago).
        Response data: {"tool_distribution", "error_distribution",
        "max_page_depth", "total_calls", "filter_count",
        "latency_median_ms", "latency_p95_ms"}.
        """
        since: int = req.get("since", int(time.time()) - 30 * 86400)
        try:
            stats = _query_usage_stats(self._conn.cursor(), since)
            return {"ok": True, "data": stats}
        except Exception as exc:
            logger.error("get_usage_stats failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "internal", "message": "internal error"}

    # ------------------------------------------------------------------
    # get_dialog_stats
    # ------------------------------------------------------------------

    async def _get_dialog_stats(self, req: dict) -> dict:
        """Return aggregate analytics for one synced dialog.

        Request: dialog_id (int) OR dialog (str fuzzy name), limit (int 1-20, default 5).
        Response data: {"dialog_id", "top_reactions", "top_mentions", "top_hashtags",
        "top_forwards"} — each a list of dicts sorted by count DESC.
        Errors: not_synced (dialog not in scope), missing_dialog, dialog_not_found.
        access_lost dialogs are allowed — archived analytics remain useful.
        """
        dialog_id: int = req.get("dialog_id", 0) or 0
        dialog: str | None = req.get("dialog")
        limit: int = _clamp(req.get("limit", 5), 1, 20)

        resolved = await self._resolve_dialog_id(dialog_id, dialog)
        if isinstance(resolved, dict):
            return resolved
        dialog_id = resolved
        if not dialog_id:
            return {
                "ok": False,
                "error": "missing_dialog",
                "message": "Either dialog_id or dialog name is required for get_dialog_stats",
            }

        row = self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)).fetchone()
        if row is None or row[0] not in ("synced", "syncing", "access_lost"):
            return {
                "ok": False,
                "error": "not_synced",
                "message": "GetDialogStats requires a synced dialog. Use MarkDialogForSync first.",
            }

        reactions = [
            {"emoji": r[0], "count": int(r[1])}
            for r in self._conn.execute(
                _GET_DIALOG_TOP_REACTIONS_SQL, (dialog_id, limit)
            ).fetchall()
        ]
        mentions = [
            {"value": r[0], "count": int(r[1])}
            for r in self._conn.execute(
                _GET_DIALOG_TOP_MENTIONS_SQL, (dialog_id, limit)
            ).fetchall()
        ]
        hashtags = [
            {"value": r[0], "count": int(r[1])}
            for r in self._conn.execute(
                _GET_DIALOG_TOP_HASHTAGS_SQL, (dialog_id, limit)
            ).fetchall()
        ]
        forwards = [
            {"peer_id": r[0], "name": r[1], "count": int(r[2])}
            for r in self._conn.execute(
                _GET_DIALOG_TOP_FORWARDS_SQL, (dialog_id, limit)
            ).fetchall()
        ]
        return {
            "ok": True,
            "data": {
                "dialog_id": dialog_id,
                "top_reactions": reactions,
                "top_mentions": mentions,
                "top_hashtags": hashtags,
                "top_forwards": forwards,
            },
        }

    # ------------------------------------------------------------------
    # upsert_entities
    # ------------------------------------------------------------------

    async def _upsert_entities(self, req: dict) -> dict:
        """Batch upsert entity rows into sync.db entities table.

        Request: entities (list of {"id": int, "type": str, "name": str,
        "username": str|None}, max 10000).
        Response: {"ok": true, "upserted": int} on success.
        Errors: invalid_input (not a list or >10000), internal.
        """
        entities = req.get("entities", [])
        if not isinstance(entities, list) or len(entities) > 10000:
            return {"ok": False, "error": "invalid_input", "message": "entities must be a list (max 10000)"}
        if not entities:
            return {"ok": True, "upserted": 0}
        now = int(time.time())
        try:
            self._conn.executemany(
                _UPSERT_ENTITY_SQL,
                [
                    (
                        e["id"],
                        e["type"],
                        e["name"],
                        e.get("username"),
                        latinize(e["name"]),
                        now,
                    )
                    for e in entities
                ],
            )
            self._conn.commit()
            return {"ok": True, "upserted": len(entities)}
        except Exception as exc:
            logger.error("upsert_entities failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "internal", "message": "internal error"}

    # ------------------------------------------------------------------
    # resolve_entity
    # ------------------------------------------------------------------

    async def _resolve_entity(self, req: dict) -> dict:
        """Fuzzy entity resolution from sync.db entities table.

        Request: query (str — @username or fuzzy name).
        Response data: {"result": "resolved", "entity_id", "display_name"}
        or {"result": "candidates", "matches": [...]}
        or {"result": "not_found", "query"}.
        Errors: missing_query.
        """
        query: str = req.get("query", "")
        if not query:
            return {"ok": False, "error": "missing_query"}

        # t.me URL: extract @username (and optionally message_id) then fall through
        tme = _parse_tme_link(query)
        if tme is not None:
            query = f"@{tme[0]}"

        # @username lookup
        if query.startswith("@"):
            username_query = query[1:]
            row = self._conn.execute(
                _ENTITY_BY_USERNAME_SQL, (username_query,)
            ).fetchone()
            if row:
                return {
                    "ok": True,
                    "data": {
                        "result": "resolved",
                        "entity_id": row[0],
                        "display_name": row[1] or f"@{username_query}",
                    },
                }
            return {"ok": True, "data": {"result": "not_found", "query": query}}

        now = int(time.time())
        display_name_map = dict(
            self._conn.execute(
                _ALL_ENTITY_NAMES_SQL, (now - USER_TTL, now - GROUP_TTL)
            ).fetchall()
        )
        normalized = dict(
            self._conn.execute(
                _ALL_ENTITY_NAMES_NORMALIZED_SQL, (now - USER_TTL, now - GROUP_TTL)
            ).fetchall()
        )

        result = resolve_entity_sync(
            query, display_name_map, None, normalized_name_map=normalized
        )

        if isinstance(result, Resolved):
            return {
                "ok": True,
                "data": {
                    "result": "resolved",
                    "entity_id": result.entity_id,
                    "display_name": result.display_name,
                },
            }
        if isinstance(result, Candidates):
            return {
                "ok": True,
                "data": {"result": "candidates", "matches": result.matches},
            }
        return {"ok": True, "data": {"result": "not_found", "query": query}}
