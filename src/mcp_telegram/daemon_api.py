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
  - get_entity_info: type-tagged entity profile (user/bot/channel/supergroup/group), DB-first with 5-min TTL
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
import dataclasses
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.tl.functions.messages import (  # type: ignore[import-untyped]
    GetCommonChatsRequest,
    GetDialogFiltersRequest,
    GetForumTopicsRequest,
)
from telethon.tl.functions.photos import GetUserPhotosRequest  # type: ignore[import-untyped]
from telethon.tl.functions.users import GetFullUserRequest  # type: ignore[import-untyped]
from telethon.tl.types import Channel, Chat  # type: ignore[import-untyped]
from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

from telethon.errors import ChatAdminRequiredError  # type: ignore[import-untyped]
from telethon.tl.functions.channels import GetFullChannelRequest  # type: ignore[import-untyped]
from telethon.tl.functions.channels import GetParticipantsRequest  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetFullChatRequest  # type: ignore[import-untyped]
from telethon.tl.functions.messages import SearchRequest as MessagesSearchRequest  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    ChannelParticipantsContacts,
    ChatReactionsAll,
    ChatReactionsNone,
    ChatReactionsSome,
    InputMessagesFilterChatPhotos,
    MessageActionChatEditPhoto,
)

# Per CONTEXT D-01 / SPEC Req 8: GetEntityInfo cache TTL is uniform 5 minutes.
# Single value — per-field TTL tiers are explicitly out of scope (see CONTEXT
# Deferred Ideas §"Per-field TTL tiers"). The orchestrator gates on
# int(time.time()) - entity_details.fetched_at < _ENTITY_DETAIL_TTL_SECONDS.
_ENTITY_DETAIL_TTL_SECONDS = 300

# Per CONTEXT D-02: detail_json blobs carry an embedded schema discriminator
# at the top level so future Telethon-driven shape changes are detectable
# without an ALTER TABLE. Bump on any breaking shape change.
_ENTITY_DETAIL_SCHEMA_VERSION = 1


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
    row = conn.execute("SELECT type FROM entities WHERE id = ?", (dialog_id,)).fetchone()
    if row is None:
        return "Unknown"
    return str(row[0])


def _read_state_for_dialog(conn: sqlite3.Connection, dialog_id: int, dialog_type: str) -> ReadState | None:
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

    # WR-04 + WR-05: fold cursor lookup and count aggregation into a single
    # statement (CTE) for atomic snapshot consistency. Without this, a
    # concurrent on_message_read / on_outbox_read writer committing between
    # the two reads could produce a mathematically inconsistent response
    # (cursor at T0, count at T1). WR-05: exclude tombstoned messages
    # (is_deleted = 0) so counts match _BATCHED_UNREAD_COUNTS_SQL used by
    # list_dialogs.
    row = conn.execute(
        """
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
        WHERE m.dialog_id = :dialog_id AND m.is_deleted = 0
        """,
        {"dialog_id": dialog_id},
    ).fetchone()
    # ``row`` is always a single aggregate row (SUM/MIN with no FROM rows yield NULL).
    # Cursor subqueries resolve to NULL when synced_dialogs has no matching row —
    # identical to the previous two-query behaviour.
    read_inbox_max_id = row[0] if row is not None else None
    read_outbox_max_id = row[1] if row is not None else None
    agg_row = (row[2], row[3], row[4], row[5]) if row is not None else (None, None, None, None)
    in_cnt = int(agg_row[0] or 0)
    out_cnt = int(agg_row[1] or 0)
    in_min = agg_row[2]
    out_min = agg_row[3]

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


USER_TTL: int = 2_592_000  # 30 days
GROUP_TTL: int = 604_800  # 7 days

from rapidfuzz import fuzz as _fuzz
from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

from .budget import allocate_message_budget_proportional, unread_chat_tier
from .formatter import format_reaction_counts
from .fts import stem_query
from .models import ReadMessage, ReadState
from .pagination import (
    HistoryDirection,
    decode_navigation_token,
    encode_history_navigation,
    encode_search_navigation,
)
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
    Resolved,
    _parse_tme_link,
    latinize,
)
from .resolver import (
    resolve as resolve_entity_sync,
)

logger = logging.getLogger(__name__)

_current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_request_id",
    default=None,
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
            count_row = conn.execute(_COUNT_SYNCED_MESSAGES_SQL, (dialog_id,)).fetchone()
            local_count = count_row[0] if count_row else 0

            meta["access_lost_at"] = access_lost_at
            meta["last_synced_at"] = last_synced_at
            meta["last_event_at"] = last_event_at
            meta["sync_coverage_pct"] = _compute_sync_coverage(total_messages, local_count)
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
_SENDER_FIRST_NAME_SQL = "COALESCE(e_raw.name, e_eff.name, m.sender_first_name) AS sender_first_name"
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
    "SELECT dialog_id, status, total_messages, access_lost_at, "
    "read_inbox_max_id, read_outbox_max_id FROM synced_dialogs"
)

# Plan 39.3-03 Task 4 (AC-11/AC-12): batched unread-count query.
# One GROUP BY pass over `messages`; for WITHOUT ROWID messages the PK
# (dialog_id, message_id) IS the table — scan traverses the PK B-tree.
# NULL-cursor semantics: COALESCE(cursor, -1) treats a NULL cursor as
# "everything is unread" on that side. This is a deliberate trade-off for
# triage display during the bootstrap window (documented in 39.3-03 <interfaces>
# MEDIUM-2 + <threat_model> T-39.3-15c). Header rendering separately maps
# NULL cursor → "[inbox: unknown (sync pending)]" (D-03) — the two semantics
# diverge intentionally.
#
# Contract note (WR-06): results of this query are emitted on `list_dialogs`
# rows as `unread_in` / `unread_out` ONLY for DMs (type == "User"). Non-DM
# rows OMIT both keys entirely. See the inline comment in `_list_dialogs`
# where the keys are conditionally attached for the full contract text.
_BATCHED_UNREAD_COUNTS_SQL = (
    "SELECT m.dialog_id, "
    'SUM(CASE WHEN m."out" = 0 AND m.message_id > COALESCE(sd.read_inbox_max_id, -1) '
    "THEN 1 ELSE 0 END) AS unread_in, "
    'SUM(CASE WHEN m."out" = 1 AND m.message_id > COALESCE(sd.read_outbox_max_id, -1) '
    "THEN 1 ELSE 0 END) AS unread_out "
    "FROM messages m JOIN synced_dialogs sd USING(dialog_id) "
    "WHERE sd.status = 'synced' AND m.is_deleted = 0 "
    "GROUP BY m.dialog_id"
)

_MARK_FOR_SYNC_SQL = "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'not_synced')"
_UNMARK_SYNC_SQL = "UPDATE synced_dialogs SET status = 'not_synced', sync_progress = NULL WHERE dialog_id = ?"

_GET_SYNC_STATUS_SQL = (
    "SELECT status, last_synced_at, last_event_at, sync_progress, total_messages, access_lost_at "
    "FROM synced_dialogs WHERE dialog_id = ?"
)
_COUNT_SYNCED_MESSAGES_SQL = "SELECT COUNT(*) FROM messages WHERE dialog_id = ? AND is_deleted = 0"

# TODO: _COUNT_MESSAGES_BY_DIALOG_SQL scans the full messages table via GROUP BY.
# For large datasets (millions of messages), consider adding a covering index
# on messages(dialog_id, is_deleted) or caching counts in synced_dialogs.
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
_GET_READ_POSITION_SQL = "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = ?"
_COUNT_BOOTSTRAP_PENDING_SQL = (
    "SELECT COUNT(*) FROM synced_dialogs WHERE status = 'synced' AND read_inbox_max_id IS NULL"
)

# Entity / telemetry SQL
_UPSERT_ENTITY_SQL = (
    "INSERT OR REPLACE INTO entities (id, type, name, username, name_normalized, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
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
    f"{EFFECTIVE_SENDER_ID_SQL}, m.is_service, m.out, m.dialog_id, "
    f"mf.fwd_from_name, m.post_author "
    f"FROM messages m "
    f"LEFT JOIN topic_metadata tm "
    f"  ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
    f"{_SENDER_ENTITY_JOINS_SQL}"
    f"LEFT JOIN message_forwards mf ON mf.dialog_id = m.dialog_id AND mf.message_id = m.message_id "
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
    ReadMessage field names; rows are fetched via conn.row_factory = sqlite3.Row
    and converted to ReadMessage objects by _list_messages_from_db.

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
        latency_p95_ms = latency_values_ms[p95_idx] if p95_idx < len(latency_values_ms) else latency_values_ms[-1]

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
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._client = client
        self._shutdown_event = shutdown_event
        # Phase 39.1: cached authenticated user id, populated once by
        # sync_main() after client.connect() completes (see daemon.py).
        # Query-build paths (Plan 39.1-02) read this as a bound SQL parameter
        # to collapse DM direction (`out`) into an effective sender id without
        # calling Telethon on every read.
        self.self_id: int | None = None
        # Set to True once Telegram is connected and all startup steps complete.
        # While False, handle_client returns daemon_not_ready with startup_detail.
        self._ready: bool = False
        self.startup_detail: str = "connecting to Telegram"

    def _dm_peer_ids(self) -> set[int]:
        """Return ids of all DM peers the operator has ever exchanged messages with.

        Per CONTEXT D-12 / D-13 (PRODUCT-LOCKED): "people I know" is defined as
        anyone with whom the operator has ever exchanged DMs (a synced 1:1
        dialog). Phonebook contacts are a subset signal, not a separate axis.
        Group/channel-only message senders are explicitly excluded.

        Source: SELECT dialog_id FROM synced_dialogs WHERE dialog_id > 0 AND
        status != 'access_lost' (DM peers have positive dialog_id; channels
        and groups have negative ids). The access_lost filter excludes peers
        the operator was blocked by, deleted, or otherwise can no longer
        reach — those aren't "known" relationships any more (LOW-1 from
        47-REVIEWS.md, opencode 2026-04-25).

        Bounded to hundreds of rows in practice — no precomputed table, no
        new column on entities. Computed per call in Python from one indexed
        SELECT — O(n) in DM-peer count. Re-runs on every contacts_subscribed
        invocation; not cached.

        Used by _fetch_channel_detail / _fetch_supergroup_detail /
        _fetch_group_detail in Plan 03 to compute contacts_subscribed.
        """
        rows = self._conn.execute(
            "SELECT dialog_id FROM synced_dialogs "
            "WHERE dialog_id > 0 AND status != 'access_lost'"
        ).fetchall()
        return {row[0] for row in rows}

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
                if not self._ready:
                    response = {
                        "ok": False,
                        "error": "daemon_not_ready",
                        "detail": self.startup_detail,
                    }
                else:
                    if request_id:
                        logger.debug("daemon_api_request method=%s request_id=%s", method, request_id)
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
        except (ConnectionResetError, BrokenPipeError):
            # MCP client (or healthcheck) disconnected before we finished
            # writing the response — expected on tool-call timeouts and
            # short-lived health probes. Don't log a stack trace.
            logger.debug(
                "daemon_api client_disconnected method=%s request_id=%s",
                method,
                request_id,
            )
        except Exception:
            logger.exception(
                "daemon_api handle_client_write_error method=%s request_id=%s",
                method,
                request_id,
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
        if method == "get_entity_info":
            return await self._get_entity_info(req)
        if method == "get_inbox":
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
        if method == "get_my_recent_activity":
            return await self._get_my_recent_activity(req)
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

        raise ValueError(f"Dialog {dialog!r} not found. Check the dialog name or use dialog_id from ListDialogs.")

    async def _resolve_dialog_id(
        self,
        dialog_id: int,
        dialog: str | None,
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
                logger.debug(
                    "msg_to_dict timestamp conversion failed msg_id=%s", getattr(msg, "id", "?"), exc_info=True
                )
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
        messages: list[ReadMessage],
        limit: int,
        dialog_id: int,
        direction: str,
        direction_enum: HistoryDirection,
    ) -> str | None:
        """Encode a next-page navigation token if the result set is full."""
        if messages and len(messages) == limit:
            last = messages[-1]
            last_msg_id = last["message_id"] if isinstance(last, dict) else last.message_id
            logger.debug(
                "list_messages_pagination anchor_msg_id=%d dialog_id=%d direction=%s%s",
                last_msg_id,
                dialog_id,
                direction,
                _rid(),
            )
            return encode_history_navigation(last_msg_id, dialog_id, direction=direction_enum)
        return None

    async def _freshen_reactions_if_stale(self, dialog_id: int, entity: Any, message_ids: list[int]) -> None:
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
        row = self._conn.execute("SELECT 1 FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone()
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
            for msg_id, msg in zip(stale_ids, messages, strict=False):
                if msg is None:
                    continue
                rows = extract_reactions_rows(dialog_id, msg_id, getattr(msg, "reactions", None))
                apply_reactions_delta(self._conn, dialog_id, msg_id, rows)
                self._conn.execute(
                    "INSERT OR REPLACE INTO message_reactions_freshness "
                    "(dialog_id, message_id, checked_at) VALUES (?, ?, ?)",
                    (dialog_id, msg_id, now),
                )

    async def _resolve_unread_position(
        self,
        dialog_id: int,
        unread_after_id: int | None,
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
        msg_ids = [r["message_id"] for r in rows]
        # Phase 39.2 Plan 02: TTL-gated JIT freshen for stale subset only.
        if msg_ids:
            await self._freshen_reactions_if_stale(dialog_id, dialog_id, msg_ids)
        reaction_map = _fetch_reaction_counts(self._conn, dialog_id, msg_ids)
        messages = [
            ReadMessage(
                **dict(r),
                reactions_display=format_reaction_counts(reaction_map[r["message_id"]]) if r["message_id"] in reaction_map else "",
            )
            for r in rows
        ]
        # Phase 39: observability counter. Emit ONCE on the sync.db success path.
        # Non-sync.db branches (Telegram fallback/error) intentionally do not emit this line.
        null_sender_rows = sum(1 for m in messages if m.sender_id is None)
        unresolved_entity_rows = sum(
            1 for m in messages if m.sender_id is not None and m.sender_first_name is None
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
            messages,
            limit,
            dialog_id,
            direction,
            direction_enum,
        )
        return {
            "ok": True,
            "data": {"messages": [dataclasses.asdict(m) for m in messages], "source": "sync_db", "next_navigation": next_nav},
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
            _LIST_MESSAGES_BASE_SQL + " AND m.message_id <= :anchor ORDER BY m.message_id DESC LIMIT :limit",
            {
                "dialog_id": dialog_id,
                "self_id": self.self_id,
                "anchor": anchor_message_id,
                "limit": half + 1,
            },
        ).fetchall()

        after_rows = self._conn.execute(
            _LIST_MESSAGES_BASE_SQL + " AND m.message_id > :anchor ORDER BY m.message_id ASC LIMIT :limit",
            {
                "dialog_id": dialog_id,
                "self_id": self.self_id,
                "anchor": anchor_message_id,
                "limit": half,
            },
        ).fetchall()

        # before_rows are DESC — reverse to get chronological order, then append after
        rows = list(reversed(before_rows)) + list(after_rows)
        msg_ids = [r["message_id"] for r in rows]
        # Phase 39.2 Plan 02: JIT freshen for the context-window slice.
        if msg_ids:
            await self._freshen_reactions_if_stale(dialog_id, dialog_id, msg_ids)
        reaction_map = _fetch_reaction_counts(self._conn, dialog_id, msg_ids)
        messages = [
            ReadMessage(
                **dict(r),
                reactions_display=format_reaction_counts(reaction_map[r["message_id"]]) if r["message_id"] in reaction_map else "",
            )
            for r in rows
        ]
        # Phase 39: observability counter — mirror main path so anchor branch is not a blind spot.
        null_sender_rows = sum(1 for m in messages if m.sender_id is None)
        unresolved_entity_rows = sum(
            1 for m in messages if m.sender_id is not None and m.sender_first_name is None
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
                "messages": [dataclasses.asdict(m) for m in messages],
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
                dialog_id,
                exc,
                _rid(),
                exc_info=True,
            )
            return {"ok": False, "error": "telegram_error", "message": "failed to fetch messages"}

        next_nav = self._maybe_encode_next_nav(
            messages,
            limit,
            dialog_id,
            direction,
            direction_enum,
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
                    "message": (f"Navigation token belongs to dialog {nav.dialog_id}, not {dialog_id}"),
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
            current_status = row[0] if row else None

            # Phase 999.1 (D-07): Fragment-dialog branch. If the dialog is not fully
            # synced, perform a targeted getMessages fetch, cache into messages,
            # then serve the context window from sync.db with coverage='fragment'.
            if current_status in (None, "not_synced", "fragment"):
                await self._fetch_fragment_context(dialog_id, context_message_id)
                result = await self._list_messages_context_window(
                    dialog_id=dialog_id,
                    anchor_message_id=context_message_id,
                    context_size=context_size,
                )
                # Annotate coverage on the response payload.
                data = result.get("data") if isinstance(result.get("data"), dict) else None
                if data is not None:
                    data["coverage"] = "fragment"
                else:
                    result["coverage"] = "fragment"
                return result

            if current_status not in ("synced", "syncing"):
                return {
                    "ok": False,
                    "error": "not_synced",
                    "message": ("Context window requires the dialog to be synced. Use MarkDialogForSync first."),
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

        direction_enum = HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST

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
            # Global search: skip reaction injection (cross-dialog, intentional — see comment below)
            messages = [ReadMessage(**dict(r)) for r in rows]
            # Global search: results span multiple dialogs. Skip reaction injection --
            # fetching reactions per-dialog for a cross-dialog result set adds complexity
            # with little value (search results are for finding messages, not analyzing
            # reactions). This is an intentional design decision, not a bug.
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
            # Scoped search: single dialog_id — inject reactions before building ReadMessage
            if dialog_id:
                msg_ids = [r["message_id"] for r in rows]
                # Phase 39.2 Plan 02: scoped search → JIT freshen for the slice.
                # Global search (dialog_id is None / 0) is OUT of JIT scope per
                # CONTEXT.md §Out of scope — no single dialog for per-message gate.
                if msg_ids:
                    await self._freshen_reactions_if_stale(dialog_id, dialog_id, msg_ids)
                reaction_map = _fetch_reaction_counts(self._conn, dialog_id, msg_ids)
                messages = [
                    ReadMessage(**dict(r), reactions_display=format_reaction_counts(reaction_map[r["message_id"]]) if r["message_id"] in reaction_map else "")
                    for r in rows
                ]
            else:
                messages = [ReadMessage(**dict(r)) for r in rows]

        next_nav: str | None = None
        if messages and len(messages) == limit:
            next_offset = offset + limit
            nav_dialog_id = 0 if global_mode else dialog_id
            next_nav = encode_search_navigation(next_offset, nav_dialog_id, query)

        # Phase 39.3-03 Task 2: build read_state_per_dialog for every distinct
        # dialog_id appearing in results. Only DMs (dialog_type == "User") are
        # included — non-DM hits are absent from the map (documented HIGH-1
        # resolution: per-dialog header block only covers DMs).
        distinct_dialog_ids = {m.dialog_id for m in messages if m.dialog_id}
        read_state_per_dialog: dict[int, ReadState] = {}
        for did in distinct_dialog_ids:
            dt = _dialog_type_from_db(self._conn, did)
            rs = _read_state_for_dialog(self._conn, did, dt)
            if rs is not None:
                read_state_per_dialog[did] = rs

        # Enrich with access metadata for scoped searches
        if not global_mode and dialog_id:
            row = self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)).fetchone()
            scoped_status = row[0] if row else None
            access_meta = _build_access_metadata(self._conn, dialog_id, scoped_status or "not_synced")
            return {
                "ok": True,
                "data": {
                    "messages": [dataclasses.asdict(m) for m in messages],
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
                "messages": [dataclasses.asdict(m) for m in messages],
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
        # Load current sync statuses for O(1) lookup. Plan 39.3-03 Task 4:
        # tuple now includes read cursors for use by the DM unread enrichment.
        synced_rows = self._conn.execute(_SELECT_SYNCED_STATUSES_SQL).fetchall()
        synced_meta: dict[int, tuple[str, int | None, int | None, int | None, int | None]] = {
            row[0]: (row[1], row[2], row[3], row[4], row[5]) for row in synced_rows
        }
        local_counts: dict[int, int] = dict(self._conn.execute(_COUNT_MESSAGES_BY_DIALOG_SQL).fetchall())
        # Plan 39.3-03 Task 4 (AC-11, AC-12): batched per-dialog (unread_in, unread_out).
        # Single GROUP BY pass — hits the messages PRIMARY KEY B-tree. Only populated
        # for DM dialogs in the response loop below (non-DM rows omit both keys).
        unread_counts: dict[int, tuple[int, int]] = {
            row[0]: (int(row[1] or 0), int(row[2] or 0))
            for row in self._conn.execute(_BATCHED_UNREAD_COUNTS_SQL).fetchall()
        }

        exclude_archived: bool = req.get("exclude_archived", False)
        ignore_pinned: bool = req.get("ignore_pinned", False)
        name_filter_raw: str | None = req.get("filter")

        # Prepare fuzzy filter state. Empty / whitespace-only filter is treated
        # as no filter. We normalize via `latinize` (same as resolver) so the
        # comparison is case- and script-insensitive. Matching strategy:
        #   1. substring in normalized space — captures the typical "give me
        #      anything with 'женск'" intent without surprising fuzzy expansion
        #   2. Word-initials match — short queries (2-4 chars) treated as
        #      acronyms against word-initial sequences of the dialog name
        #      (e.g. "ЖС" → "zs" hits "KS x Женские Сезоны" which initials to "kxzs").
        #   3. rapidfuzz.partial_ratio >= 80 fallback for queries AND targets ≥ 4 chars.
        filter_norm: str | None = None
        if name_filter_raw is not None:
            stripped = name_filter_raw.strip()
            if stripped:
                filter_norm = latinize(stripped)

        archived_filter = False if exclude_archived else None

        dialogs = []
        try:
            async for d in self._client.iter_dialogs(
                archived=archived_filter,
                ignore_pinned=ignore_pinned,
            ):
                entity = getattr(d, "entity", None)
                entity_type = _classify_dialog_type(entity)

                # Name filter (tool-level convenience: Telethon iter_dialogs has
                # no server-side filter — we scan everything and screen here).
                # Match order (first-hit wins):
                #   1. Substring in latinized space — primary. "женск" hits
                #      "Женские сезоны" cleanly.
                #   2. Word-initials match — short upper/mixed queries treated as
                #      acronyms. "ЖС" → initials "zs" → matches any dialog whose
                #      word-initial sequence contains "zs" (e.g. "KS x Женские
                #      Сезоны" → initials "kxzs").
                #   3. rapidfuzz.partial_ratio ≥ 80 — typo-tolerant fallback for
                #      queries AND targets ≥ 4 chars (avoids short-name noise).
                if filter_norm is not None:
                    raw_name = getattr(d, "name", None) or ""
                    if not raw_name:
                        continue
                    name_norm = latinize(raw_name)
                    # Acronym initials use raw case-folded chars — latinizing
                    # would expand "Ж" to "zh" and break single-char-per-word matching.
                    name_initials_raw = "".join(w[0] for w in raw_name.split() if w).lower()
                    filter_raw_lc = (name_filter_raw or "").strip().lower()
                    if filter_norm in name_norm:
                        pass
                    elif 2 <= len(filter_raw_lc) <= 4 and filter_raw_lc in name_initials_raw:
                        pass  # acronym hit ("ЖС" → "жс" ⊆ "kxжс")
                    elif (
                        len(filter_norm) >= 4
                        and len(name_norm) >= 4
                        and _fuzz.partial_ratio(filter_norm, name_norm) >= 80
                    ):
                        pass  # typo-tolerant fuzzy hit
                    else:
                        continue
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

                sync_meta_row = synced_meta.get(d.id, ("not_synced", None, None, None, None))
                sync_status = sync_meta_row[0]
                total_messages = sync_meta_row[1]
                access_lost_at = sync_meta_row[2]
                local_count = local_counts.get(d.id, 0)
                coverage_pct = _compute_sync_coverage(total_messages, local_count)
                row: dict = {
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
                # Plan 39.3-03 Task 4: include unread_in / unread_out ONLY for DMs
                # (dialog_type == "User"). Non-DM rows OMIT both keys (AC-11).
                #
                # DAEMON API CONTRACT (WR-06): `unread_in` and `unread_out` are
                # PRESENT only on rows where `type == "User"`. For non-DM rows
                # (Bot / Group / Channel / Forum / Chat / Unknown) the keys are
                # ABSENT — not set to None, not set to 0. Consumers must
                # distinguish "missing key" (non-DM, counts undefined) from
                # "present with value 0" (DM with nothing unread) using
                # membership tests (e.g. `"unread_in" in d`) or `.get()` with
                # an explicit sentinel. This is the canonical contract; the
                # `ListDialogs` tool docstring mirrors it for the public surface.
                if entity_type == "User":
                    in_cnt, out_cnt = unread_counts.get(d.id, (0, 0))
                    row["unread_in"] = in_cnt
                    row["unread_out"] = out_cnt
                dialogs.append(row)
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
                    "date": int(t.date.timestamp())
                    if hasattr(getattr(t, "date", None), "timestamp")
                    else getattr(t, "date", None),
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
    # get_entity_info
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

    async def _get_entity_info(self, req: dict) -> dict:
        """Type-tagged entity inspector covering 5 Telegram entity kinds.

        DB-first read from sync.db.entity_details; live MTProto fetch only on
        cache miss or staleness. Returns one of five 'type' discriminators:
        'user' | 'bot' | 'channel' | 'supergroup' | 'group'. Per CONTEXT D-05
        + D-06: this orchestrator owns DB cache, TTL gate, common-envelope
        assembly, write-back, and error envelope shaping. Per-type helpers
        (_fetch_user_detail / _fetch_channel_detail / _fetch_supergroup_detail
        / _fetch_group_detail) own only the type-specific RPC chain.

        Request schema: {method: "get_entity_info", entity_id: int}
        Response schema: see CONTEXT §"Daemon dispatch layout" D-06 + D-10.
        """
        entity_id = req.get("entity_id")
        if not isinstance(entity_id, int):
            return {
                "ok": False,
                "error": "telegram_api_error",
                "message": "entity_id missing or not an integer",
                "data": None,
            }

        now = int(time.time())

        # ----- DB-first read (SPEC Req 8) -----
        try:
            row = self._conn.execute(
                "SELECT detail_json, fetched_at FROM entity_details WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            logger.warning(
                "entity_info db_read_failed entity_id=%r error=%s%s",
                entity_id, exc, _rid(),
            )
            return {
                "ok": False,
                "error": "db_unavailable",
                "message": str(exc),
                "data": None,
            }

        if row is not None:
            detail_json, fetched_at = row
            if now - fetched_at < _ENTITY_DETAIL_TTL_SECONDS:
                # Cache HIT, fresh
                try:
                    detail = json.loads(detail_json)
                except json.JSONDecodeError:
                    logger.warning(
                        "entity_info detail_json_corrupt entity_id=%r%s — treating as cache miss",
                        entity_id, _rid(),
                    )
                    detail = None
                if detail is not None and detail.get("schema") == _ENTITY_DETAIL_SCHEMA_VERSION:
                    return {"ok": True, "data": self._strip_envelope_schema(detail)}
                # Schema mismatch or corrupt JSON → fall through to live fetch.

        # ----- Cache miss / stale: live fetch -----
        try:
            entity = await self._client.get_entity(entity_id)
        except (ValueError, KeyError) as exc:
            logger.warning(
                "entity_info entity_not_found entity_id=%r error=%s%s",
                entity_id, exc, _rid(),
            )
            return {
                "ok": False,
                "error": "entity_not_found",
                "message": str(exc),
                "data": None,
            }
        except Exception as exc:
            logger.warning(
                "entity_info get_entity_failed entity_id=%r error=%s%s",
                entity_id, exc, _rid(), exc_info=True,
            )
            return {
                "ok": False,
                "error": "telegram_api_error",
                "message": str(exc),
                "data": None,
            }

        # ----- Dispatch by type (D-09 — reuses _classify_dialog_type) -----
        dispatch_kind = _classify_dialog_type(entity)

        if dispatch_kind in ("User", "Bot"):
            detail = await self._fetch_user_detail(entity)
        elif dispatch_kind == "Channel":
            detail = await self._fetch_channel_detail(entity)
        elif dispatch_kind in ("Group", "Forum"):
            detail = await self._fetch_supergroup_detail(entity)
        elif dispatch_kind == "Chat":
            detail = await self._fetch_group_detail(entity)
        else:
            return {
                "ok": False,
                "error": "unsupported_entity_type",
                "message": f"unknown entity kind: {dispatch_kind}",
                "data": None,
            }

        if detail is None:
            return {
                "ok": False,
                "error": "telegram_api_error",
                "message": "per-type helper returned no detail",
                "data": None,
            }

        # ----- Write back: entities (auto-resolve, SPEC Req 11) + entity_details -----
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entity_id,
                    detail.get("type", "unknown"),
                    detail.get("name"),
                    detail.get("username"),
                    now,
                ),
            )
            payload_with_schema = {"schema": _ENTITY_DETAIL_SCHEMA_VERSION, **detail}
            self._conn.execute(
                "INSERT OR REPLACE INTO entity_details (entity_id, detail_json, fetched_at) "
                "VALUES (?, ?, ?)",
                (entity_id, json.dumps(payload_with_schema), now),
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning(
                "entity_info db_writeback_failed entity_id=%r error=%s%s",
                entity_id, exc, _rid(), exc_info=True,
            )
            # Continue: still return the live data even if cache write failed.

        return {"ok": True, "data": detail}

    @staticmethod
    def _strip_envelope_schema(detail: dict) -> dict:
        """Remove the internal 'schema' discriminator before returning to the wire.

        The 'schema' field is a write-back implementation detail; clients see
        only the typed payload. Pure-Python dict copy — bounded size.
        """
        return {k: v for k, v in detail.items() if k != "schema"}

    async def _fetch_user_detail(self, user) -> dict:
        """Per-type helper: User/Bot detail. Per CONTEXT D-07, body refactored
        verbatim from the prior _get_user_info except for the resolution
        prelude (orchestrator passes an already-resolved entity).

        Per CONTEXT D-08: User vs Bot discriminated via getattr(user, "bot", False)
        — same RPC chain (GetFullUserRequest + GetUserPhotosRequest +
        GetCommonChatsRequest) for both kinds, only the response 'type' differs.

        SPEC Req 4: User/Bot field surface preserved verbatim from prior
        GetUserInfo. The diff between the prior tool's data dict and this
        helper's return is exactly: 'type' added, 'my_membership' added,
        'photos' renamed to 'avatar_history', 'avatar_count' added; everything
        else unchanged.
        """
        user_id = int(user.id)

        # --- common_chats (existing _get_user_info body) ---
        common_chats: list[dict] = []
        try:
            common_result = await self._client(GetCommonChatsRequest(user_id=user_id, max_id=0, limit=100))
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
            logger.warning(
                "entity_info user common_chats_failed user_id=%r error=%s%s",
                user_id, exc, _rid(), exc_info=True,
            )

        # --- GetFullUserRequest body — verbatim from old _get_user_info ---
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
                business_work_hours = {"timezone": getattr(raw_hours, "timezone_id", None)}
            raw_note = getattr(user_full, "note", None)
            if raw_note is not None:
                note = getattr(raw_note, "text", None) or None
        except Exception as exc:
            logger.warning(
                "entity_info user full_user_failed user_id=%r error=%s%s",
                user_id, exc, _rid(), exc_info=True,
            )

        # --- folder name resolution (verbatim from old _get_user_info) ---
        if folder_id is not None:
            try:
                filters = await self._client(GetDialogFiltersRequest())
                for f in filters or []:
                    if getattr(f, "id", None) == folder_id:
                        raw_title = getattr(f, "title", None)
                        folder_name = getattr(raw_title, "text", raw_title) if raw_title else None
                        break
            except Exception as exc:
                logger.warning(
                    "entity_info user folder_resolve_failed folder_id=%r error=%s%s",
                    folder_id, exc, _rid(), exc_info=True,
                )

        # --- extra usernames + emoji status + restriction reason (verbatim) ---
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

        # --- avatar_history + avatar_count (rename from `photos`; SPEC Req 10 —
        #     no file_id / file_reference / download_*) ---
        avatar_history: list[dict] = []
        avatar_count: int = 0
        try:
            photos_result = await self._client(
                GetUserPhotosRequest(user_id=user, offset=0, max_id=0, limit=100)
            )
            avatar_count = int(getattr(photos_result, "count", len(getattr(photos_result, "photos", []))))
            for photo in getattr(photos_result, "photos", []):
                photo_id = getattr(photo, "id", None)
                photo_date = getattr(photo, "date", None)
                if photo_id is None or photo_date is None:
                    continue
                avatar_history.append({
                    "photo_id": int(photo_id),
                    "date": photo_date.isoformat(),
                })
        except Exception as exc:
            logger.warning(
                "entity_info user photos_failed user_id=%r error=%s%s",
                user_id, exc, _rid(), exc_info=True,
            )

        # --- common-envelope fields (D-06) ---
        first_name = getattr(user, "first_name", None)
        last_name = getattr(user, "last_name", None)
        name = " ".join(part for part in (first_name, last_name) if part)
        username = getattr(user, "username", None)

        # --- my_membership for User/Bot: relationship sub-block per SPEC Req 3 ---
        contact_flag = bool(getattr(user, "contact", False))
        mutual_contact = bool(getattr(user, "mutual_contact", False))
        close_friend = bool(getattr(user, "close_friend", False))
        my_membership = {
            "is_member": contact_flag or mutual_contact,
            "is_admin": False,
            "admin_rights": None,
            "relationship": {
                "contact": contact_flag,
                "mutual_contact": mutual_contact,
                "close_friend": close_friend,
                "blocked": blocked,
            },
        }

        # --- type discriminator (D-08) ---
        entity_type = "bot" if bool(getattr(user, "bot", False)) else "user"

        return {
            # Common envelope (D-06)
            "id": user_id,
            "type": entity_type,
            "name": name or None,
            "username": username,
            "about": about,
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            # User/Bot-specific (verbatim shape from old _get_user_info data dict)
            "first_name": first_name,
            "last_name": last_name,
            "extra_usernames": extra_usernames,
            "emoji_status_id": emoji_status_id,
            "status": self._format_user_status(getattr(user, "status", None)),
            "phone": getattr(user, "phone", None),
            "lang_code": getattr(user, "lang_code", None),
            "contact": contact_flag,
            "mutual_contact": mutual_contact,
            "close_friend": close_friend,
            "send_paid_messages_stars": getattr(user, "send_paid_messages_stars", None),
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
        }

    async def _search_chat_photo_history(self, peer, full_chat) -> tuple[list[dict], int]:
        """Avatar history via messages.Search(filter=ChatPhotos), with the
        D-19 broadcast-channel reconciliation (prepend full.chat_photo if
        missing) and D-20 fallback (degrade to chat_photo-only on RPC error).

        Returns (avatar_history, avatar_count). Reused by _fetch_channel_detail
        (this task), _fetch_supergroup_detail (Plan 03), and _fetch_group_detail
        (Plan 03).
        """
        peer_id = int(telethon_utils.get_peer_id(peer))
        avatar_history: list[dict] = []
        avatar_count = 0
        # HIGH-3 from 47-REVIEWS.md cycle 3 (codex 2026-04-25): explicit
        # search_failed flag. Without it, after the photo search raises and
        # D-19 reconciliation inserts `full_chat.chat_photo`, `avatar_history`
        # is no longer empty, so the old D-20 fallback block (`if not
        # avatar_history and current_photo_id is not None`) is skipped — and
        # `avatar_count` stays at 0 even though one current photo is present.
        # That contradicts D-20's locked contract: "avatar_count = 1 in this
        # fallback case". The flag lets D-20 run alongside D-19 instead of
        # being short-circuited by it.
        search_failed = False
        try:
            search_result = await self._client(MessagesSearchRequest(
                peer=peer,
                q="",
                filter=InputMessagesFilterChatPhotos(),
                min_date=None, max_date=None,
                offset_id=0, add_offset=0, limit=100,
                max_id=0, min_id=0, hash=0,
                from_id=None,
            ))
            avatar_count = int(getattr(search_result, "count", len(getattr(search_result, "messages", []))))
            for msg in getattr(search_result, "messages", []):
                action = getattr(msg, "action", None)
                if isinstance(action, MessageActionChatEditPhoto):
                    photo = getattr(action, "photo", None)
                    photo_date = getattr(msg, "date", None)
                    if photo is not None and photo_date is not None and getattr(photo, "id", None) is not None:
                        avatar_history.append({
                            "photo_id": int(photo.id),
                            "date": photo_date.isoformat(),
                        })
        except Exception as exc:
            search_failed = True
            logger.warning(
                "entity_info avatar_search_failed peer_id=%r error=%s%s",
                peer_id, exc, _rid(),
            )

        # D-19 reconciliation: prepend full.chat_photo if it's missing from search.
        chat_photo = getattr(full_chat, "chat_photo", None) if full_chat is not None else None
        current_photo_id = getattr(chat_photo, "id", None) if chat_photo is not None else None
        if current_photo_id is not None and not any(
            p["photo_id"] == int(current_photo_id) for p in avatar_history
        ):
            chat_photo_date = getattr(chat_photo, "date", None)
            avatar_history.insert(0, {
                "photo_id": int(current_photo_id),
                "date": chat_photo_date.isoformat() if chat_photo_date is not None else None,
            })

        # D-20 fallback (HIGH-3 from 47-REVIEWS.md cycle 3 — re-ordered): runs
        # whenever the search raised, regardless of whether D-19 just inserted
        # the current chat_photo. Two cases:
        #   (a) Search failed AND chat_photo unknown → empty history, count=0
        #       (already the natural state — nothing to do).
        #   (b) Search failed AND chat_photo known → D-19 already prepended
        #       the current photo, but `avatar_count` is still the broken
        #       initial 0 (the failed Search never gave us a count). D-20
        #       mandates `avatar_count = 1` here. Use max(...) so a partial
        #       (rare) success — non-zero count + later raise — is preserved.
        if search_failed and current_photo_id is not None:
            avatar_count = max(avatar_count, 1)
        # Belt-and-suspenders: legacy D-20 path for the (theoretical) case
        # where avatar_history is still empty AND chat_photo exists AND search
        # did NOT raise (e.g. Search returned a degenerate empty payload).
        # Same surface as before the fix.
        if not avatar_history and current_photo_id is not None:
            chat_photo_date = getattr(chat_photo, "date", None)
            avatar_history = [{
                "photo_id": int(current_photo_id),
                "date": chat_photo_date.isoformat() if chat_photo_date is not None else None,
            }]
            avatar_count = max(avatar_count, 1)

        return avatar_history, avatar_count

    async def _fetch_channel_detail(self, channel) -> dict:
        """Per-type helper: Broadcast Channel detail (megagroup=False).

        Per CONTEXT D-09 / D-21: max 6 MTProto requests on the non-User path
        — 1 GetFullChannelRequest + ≤1 messages.Search(ChatPhotos) + ≤1 photo
        item (current chat_photo reconciliation per D-19). Plan 03 adds the
        ≤2 GetParticipants pages for the contacts_subscribed enumeration.

        Returns SPEC Req 5 surface + common envelope. contacts_subscribed
        enumeration is Plan 03's territory; Plan 02 returns either the
        privacy-gated null + reason='not_an_admin' (SPEC Req 9) or a stub
        flagged for Plan 03.
        """
        channel_id = int(telethon_utils.get_peer_id(channel))

        # ----- GetFullChannelRequest -----
        full_chat = None
        subscribers_count: int | None = None
        linked_chat_id: int | None = None
        pinned_msg_id: int | None = None
        slow_mode_seconds: int | None = None
        available_reactions: dict = {"kind": "none", "emojis": []}
        about: str | None = None
        try:
            full_result = await self._client(GetFullChannelRequest(channel=channel))
            full_chat = full_result.full_chat
            subscribers_count = getattr(full_chat, "participants_count", None)
            linked_chat_id_raw = getattr(full_chat, "linked_chat_id", None)
            if linked_chat_id_raw is not None:
                # Channel ids are returned in bare form by Telethon; normalize to peer-id form
                # so downstream GetEntityInfo calls work with the same id scheme.
                # MEDIUM from 47-REVIEWS.md cycle 2 (codex): use the canonical Telethon
                # helper instead of `int(f"-100{raw}")` string concatenation. The string
                # form is brittle — it assumes raw > 0 and channel-shape, and breaks if
                # Telethon ever returns a peer-form id directly. The util handles both.
                if linked_chat_id_raw > 0:
                    from telethon.tl.types import PeerChannel  # type: ignore[import-untyped]
                    linked_chat_id = int(telethon_utils.get_peer_id(PeerChannel(linked_chat_id_raw)))
                else:
                    # Already in peer-id form — pass through.
                    linked_chat_id = int(linked_chat_id_raw)
            pinned_msg_id = getattr(full_chat, "pinned_msg_id", None)
            slow_mode_seconds = getattr(full_chat, "slowmode_seconds", None)
            about = getattr(full_chat, "about", None) or None

            raw_reactions = getattr(full_chat, "available_reactions", None)
            if isinstance(raw_reactions, ChatReactionsAll):
                available_reactions = {"kind": "all", "emojis": []}
            elif isinstance(raw_reactions, ChatReactionsSome):
                emojis = []
                for r in getattr(raw_reactions, "reactions", []) or []:
                    em = getattr(r, "emoticon", None)
                    if em:
                        emojis.append(em)
                available_reactions = {"kind": "some", "emojis": emojis}
            elif isinstance(raw_reactions, ChatReactionsNone) or raw_reactions is None:
                available_reactions = {"kind": "none", "emojis": []}
        except Exception as exc:
            logger.warning(
                "entity_info channel full_channel_failed channel_id=%r error=%s%s",
                channel_id, exc, _rid(), exc_info=True,
            )

        # ----- restrictions (from the channel entity itself, mirrors User path) -----
        restrictions: list[dict] = []
        for rr in getattr(channel, "restriction_reason", None) or []:
            restrictions.append({
                "platform": getattr(rr, "platform", None),
                "reason": getattr(rr, "reason", None),
                "text": getattr(rr, "text", None),
            })

        # ----- my_membership: is_admin derived from channel flags -----
        is_creator = bool(getattr(channel, "creator", False))
        admin_rights_obj = getattr(channel, "admin_rights", None)
        is_admin = is_creator or (admin_rights_obj is not None)
        my_membership = {
            "is_member": not bool(getattr(channel, "left", False)),
            "is_admin": is_admin,
            "admin_rights": (
                {
                    field: bool(getattr(admin_rights_obj, field, False))
                    for field in (
                        "change_info", "post_messages", "edit_messages",
                        "delete_messages", "ban_users", "invite_users",
                        "pin_messages", "add_admins", "anonymous", "manage_call",
                        "other", "manage_topics", "post_stories", "edit_stories",
                        "delete_stories",
                    )
                }
                if admin_rights_obj is not None else None
            ),
        }

        # ----- contacts_subscribed (Plan 02 partial: privacy gate only; Plan 03 enumerates) -----
        # HIGH-A from 47-REVIEWS.md cycle 2 (2026-04-25, opencode + codex
        # consensus): Plan 03 Task 3 REPLACES the `enumeration_owned_by_plan_03`
        # admin-path stub below with real enumeration on this same helper.
        # Plan 02's responsibility ends at the privacy-gate (`not_an_admin`)
        # branch; Plan 03 owns the admin branch. This is a TEMPORARY stub
        # designed to be overwritten in Wave 3 — leaving it in production
        # would violate SPEC Req 9 (acceptance: GetEntityInfo on a known
        # broadcast channel returns contacts_subscribed for an admin caller).
        contacts_subscribed = None
        contacts_subscribed_partial = False
        contacts_reason = None
        if not is_admin:
            # Broadcast channels hide subscriber lists from non-admins. SPEC Req 9.
            contacts_subscribed = None
            contacts_reason = "not_an_admin"
        else:
            # Plan 03 Task 3 replaces this admin branch with real enumeration:
            #   subscribers_count <= 1000  →  iter_participants ∩ _dm_peer_ids()
            #   subscribers_count > 1000   →  ChannelParticipantsContacts ∩ _dm_peer_ids()
            #                                  + contacts_subscribed_partial=True
            # Plan 02 commits with this stub so the broadcast envelope shape is
            # complete; Plan 03 OVERWRITES this branch in the same file.
            # If Plan 03 ships and any production response still carries
            # contacts_reason="enumeration_owned_by_plan_03", that is a HIGH
            # regression — the Plan 04 acceptance grep enforces this.
            contacts_subscribed = None
            contacts_reason = "enumeration_owned_by_plan_03"

        # ----- avatar_history via shared helper (D-17..D-20) -----
        # MEDIUM-1 from 47-REVIEWS.md (opencode 2026-04-25): avatar-search +
        # D-19 reconciliation + D-20 fallback live in the shared
        # `_search_chat_photo_history` helper defined above.
        avatar_history, avatar_count = await self._search_chat_photo_history(channel, full_chat)

        # ----- common envelope assembly -----
        title = getattr(channel, "title", None)
        username = getattr(channel, "username", None)

        return {
            # Common envelope (D-06)
            "id": channel_id,
            "type": "channel",
            "name": title,
            "username": username,
            "about": about,
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            # Channel-specific (SPEC Req 5)
            "subscribers_count": subscribers_count,
            "linked_chat_id": linked_chat_id,
            "pinned_msg_id": pinned_msg_id,
            "slow_mode_seconds": slow_mode_seconds,
            "available_reactions": available_reactions,
            "restrictions": restrictions,
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": contacts_subscribed_partial,
            "contacts_reason": contacts_reason,
        }

    def _enrich_contact_ids_with_names(self, ids: set[int]) -> list[dict]:
        """Resolve a set of entity ids to {id, name, username} dicts via JOIN
        on the entities table. Per CONTEXT D-14: ids alone aren't useful for
        the LLM; names are.

        Returns a list (not set) sorted by name for stable ordering across
        repeated calls. Empty input → empty list.
        """
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT id, name, username FROM entities WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
        # ids that have no entities row are returned with name/username=None so
        # the count still matches; LLM sees the gap and can re-resolve later.
        seen = {row[0] for row in rows}
        out = [{"id": row[0], "name": row[1], "username": row[2]} for row in rows]
        for missing_id in ids - seen:
            out.append({"id": missing_id, "name": None, "username": None})
        return sorted(out, key=lambda d: ((d["name"] or ""), d["id"]))

    async def _fetch_supergroup_detail(self, channel) -> dict:
        """Per-type helper: Supergroup (Channel.megagroup=True). Forum
        supergroups (forum=True) also route here per CONTEXT D-09 / SPEC Req 6.

        SPEC Req 6 surface: members_count, linked_broadcast_id,
        slow_mode_seconds, has_topics, restrictions, contacts_subscribed +
        common envelope + avatar_history.

        contacts_subscribed enumeration policy (CONTEXT D-14 / D-15, SPEC Req 9):
          - non-admin + hidden members:        null + reason='hidden_by_admin'
          - admin or open + members_count<=1000: iter_participants ∩ _dm_peer_ids()
          - members_count>1000:                ChannelParticipantsContacts ∩ _dm_peer_ids()
                                                with contacts_subscribed_partial=True,
                                                reason='too_large'
        """
        channel_id = int(telethon_utils.get_peer_id(channel))

        # ----- GetFullChannelRequest -----
        full_chat = None
        members_count: int | None = None
        linked_broadcast_id: int | None = None
        slow_mode_seconds: int | None = None
        about: str | None = None
        try:
            full_result = await self._client(GetFullChannelRequest(channel=channel))
            full_chat = full_result.full_chat
            members_count = getattr(full_chat, "participants_count", None)
            linked_chat_raw = getattr(full_chat, "linked_chat_id", None)
            if linked_chat_raw is not None:
                # MEDIUM from 47-REVIEWS.md cycle 2 (codex): use the canonical
                # Telethon helper instead of `int(f"-100{raw}")` string
                # concatenation — see Plan 02 Task 3 for the same fix on
                # `_fetch_channel_detail`. The string form is brittle.
                if linked_chat_raw > 0:
                    from telethon.tl.types import PeerChannel  # type: ignore[import-untyped]
                    linked_broadcast_id = int(telethon_utils.get_peer_id(PeerChannel(linked_chat_raw)))
                else:
                    # Already in peer-id form — pass through.
                    linked_broadcast_id = int(linked_chat_raw)
            slow_mode_seconds = getattr(full_chat, "slowmode_seconds", None)
            about = getattr(full_chat, "about", None) or None
        except Exception as exc:
            logger.warning(
                "entity_info supergroup full_channel_failed channel_id=%r error=%s%s",
                channel_id, exc, _rid(), exc_info=True,
            )

        # ----- restrictions -----
        restrictions: list[dict] = []
        for rr in getattr(channel, "restriction_reason", None) or []:
            restrictions.append({
                "platform": getattr(rr, "platform", None),
                "reason": getattr(rr, "reason", None),
                "text": getattr(rr, "text", None),
            })

        # ----- my_membership -----
        is_creator = bool(getattr(channel, "creator", False))
        admin_rights_obj = getattr(channel, "admin_rights", None)
        is_admin = is_creator or (admin_rights_obj is not None)
        my_membership = {
            "is_member": not bool(getattr(channel, "left", False)),
            "is_admin": is_admin,
            "admin_rights": (
                {
                    field: bool(getattr(admin_rights_obj, field, False))
                    for field in (
                        "change_info", "post_messages", "edit_messages",
                        "delete_messages", "ban_users", "invite_users",
                        "pin_messages", "add_admins", "anonymous", "manage_call",
                        "other", "manage_topics", "post_stories", "edit_stories",
                        "delete_stories",
                    )
                }
                if admin_rights_obj is not None else None
            ),
        }

        # ----- contacts_subscribed (D-14 / D-15 / SPEC Req 9) -----
        contacts_subscribed = None
        contacts_subscribed_partial = False
        contacts_reason: str | None = None

        # Privacy gate (HIGH-3 from 47-REVIEWS.md, opencode 2026-04-25):
        # Telethon's Channel.hidden_members attribute (layer 160+) is the explicit
        # signal that the supergroup admin has hidden the member list. If the
        # attribute is present and True AND we are not admin, gate immediately.
        # Otherwise, attempt enumeration and let ChatAdminRequiredError be the
        # ground truth (Telegram raises it when a non-admin tries to enumerate
        # a hidden-members supergroup). Do NOT use channel.noforwards as a proxy
        # — that flag controls "Restrict Saving Content" and is independent of
        # member-list visibility (a supergroup can have noforwards=True with an
        # open member list, or noforwards=False with hidden members).
        hidden_members = bool(getattr(channel, "hidden_members", False)) and not is_admin

        if hidden_members:
            contacts_subscribed = None
            contacts_reason = "hidden_by_admin"
        elif members_count is not None and members_count > 1000:
            # D-15 above-threshold: phone-contacts intersection only.
            try:
                gp_result = await self._client(GetParticipantsRequest(
                    channel=channel,
                    filter=ChannelParticipantsContacts(q=""),
                    offset=0, limit=200, hash=0,
                ))
                contact_ids = {int(u.id) for u in getattr(gp_result, "users", []) if hasattr(u, "id")}
                dm_peers = self._dm_peer_ids()
                intersect_ids = contact_ids & dm_peers
                contacts_subscribed = self._enrich_contact_ids_with_names(intersect_ids)
                contacts_subscribed_partial = True
                contacts_reason = "too_large"
            except ChatAdminRequiredError:
                # Telegram refused the enumeration → ground-truth hidden members.
                contacts_subscribed = None
                contacts_reason = "hidden_by_admin"
            except Exception as exc:
                logger.warning(
                    "entity_info supergroup contacts_filter_failed channel_id=%r error=%s%s",
                    channel_id, exc, _rid(), exc_info=True,
                )
                contacts_subscribed = None
                contacts_reason = "enumeration_failed"
        else:
            # D-14 ≤1000 path: iter_participants → intersect → enrich with names.
            try:
                participant_ids: set[int] = set()
                async for participant in self._client.iter_participants(channel, limit=1000):
                    pid = getattr(participant, "id", None)
                    if pid is not None:
                        participant_ids.add(int(pid))
                dm_peers = self._dm_peer_ids()
                intersect_ids = participant_ids & dm_peers
                contacts_subscribed = self._enrich_contact_ids_with_names(intersect_ids)
                contacts_subscribed_partial = False
            except ChatAdminRequiredError:
                # Telegram refused the enumeration → ground-truth hidden members.
                contacts_subscribed = None
                contacts_reason = "hidden_by_admin"
            except Exception as exc:
                logger.warning(
                    "entity_info supergroup iter_participants_failed channel_id=%r error=%s%s",
                    channel_id, exc, _rid(), exc_info=True,
                )
                contacts_subscribed = None
                contacts_reason = "enumeration_failed"

        # ----- avatar_history per D-17 / D-19 / D-20 (same pattern as Channel) -----
        avatar_history, avatar_count = await self._search_chat_photo_history(channel, full_chat)

        title = getattr(channel, "title", None)
        username = getattr(channel, "username", None)

        return {
            # Common envelope (D-06)
            "id": channel_id,
            "type": "supergroup",
            "name": title,
            "username": username,
            "about": about,
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            # Supergroup-specific (SPEC Req 6)
            "members_count": members_count,
            "linked_broadcast_id": linked_broadcast_id,
            "slow_mode_seconds": slow_mode_seconds,
            "has_topics": bool(getattr(channel, "forum", False)),
            "restrictions": restrictions,
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": contacts_subscribed_partial,
            "contacts_reason": contacts_reason,
        }

    async def _fetch_group_detail(self, chat) -> dict:
        """Per-type helper: legacy basic Chat (Telethon Chat, not Channel).

        Per CONTEXT D-16: legacy basic groups always expose their participant
        list — no admin gate, no hide-members setting. Full enumerate +
        intersect with DM-peer set per SPEC Req 9 / Req 7.

        SPEC Req 12: migrated_to returned verbatim in peer-id form
        (int(telethon_utils.get_peer_id(migrated_to_obj))) when Telegram
        reports it. NO auto-follow code path exists in this method or in
        _get_entity_info — LLM is responsible for re-querying with the new id.
        """
        chat_id = int(telethon_utils.get_peer_id(chat))

        # ----- migrated_to (SPEC Req 12) -----
        migrated_to_obj = getattr(chat, "migrated_to", None)
        migrated_to: int | None = None
        if migrated_to_obj is not None:
            try:
                migrated_to = int(telethon_utils.get_peer_id(migrated_to_obj))
            except Exception as exc:
                logger.warning(
                    "entity_info group migrated_to_normalize_failed chat_id=%r error=%s%s",
                    chat_id, exc, _rid(),
                )

        # ----- GetFullChatRequest -----
        full_chat = None
        members_count: int | None = None
        about: str | None = None
        invite_link: str | None = None
        participants_objs: list = []
        try:
            full_result = await self._client(GetFullChatRequest(chat_id=int(chat.id)))
            full_chat = full_result.full_chat
            about = getattr(full_chat, "about", None) or None
            exported_invite = getattr(full_chat, "exported_invite", None)
            if exported_invite is not None:
                invite_link = getattr(exported_invite, "link", None)
            raw_participants = getattr(full_chat, "participants", None)
            if raw_participants is not None:
                participants_objs = list(getattr(raw_participants, "participants", []) or [])
                members_count = len(participants_objs)
            # Telethon Chat itself also exposes participants_count at times:
            if members_count is None:
                members_count = getattr(chat, "participants_count", None)
        except Exception as exc:
            logger.warning(
                "entity_info group full_chat_failed chat_id=%r error=%s%s",
                chat_id, exc, _rid(), exc_info=True,
            )

        # ----- restrictions -----
        restrictions: list[dict] = []
        for rr in getattr(chat, "restriction_reason", None) or []:
            restrictions.append({
                "platform": getattr(rr, "platform", None),
                "reason": getattr(rr, "reason", None),
                "text": getattr(rr, "text", None),
            })

        # ----- my_membership: legacy chats have a creator flag and admin_rights -----
        is_creator = bool(getattr(chat, "creator", False))
        admin_rights_obj = getattr(chat, "admin_rights", None)
        is_admin = is_creator or (admin_rights_obj is not None)
        my_membership = {
            "is_member": not bool(getattr(chat, "left", False)),
            "is_admin": is_admin,
            "admin_rights": (
                {
                    field: bool(getattr(admin_rights_obj, field, False))
                    for field in (
                        "change_info", "post_messages", "edit_messages",
                        "delete_messages", "ban_users", "invite_users",
                        "pin_messages", "add_admins", "anonymous", "manage_call",
                        "other", "manage_topics", "post_stories", "edit_stories",
                        "delete_stories",
                    )
                }
                if admin_rights_obj is not None else None
            ),
        }

        # ----- contacts_subscribed: full participant list always available (D-16) -----
        contacts_subscribed = None
        contacts_subscribed_partial = False
        contacts_reason: str | None = None
        try:
            participant_ids = {
                int(getattr(p, "user_id", 0))
                for p in participants_objs
                if getattr(p, "user_id", None) is not None
            }
            dm_peers = self._dm_peer_ids()
            intersect_ids = participant_ids & dm_peers
            contacts_subscribed = self._enrich_contact_ids_with_names(intersect_ids)
        except Exception as exc:
            logger.warning(
                "entity_info group contacts_intersect_failed chat_id=%r error=%s%s",
                chat_id, exc, _rid(),
            )
            contacts_subscribed = None
            contacts_reason = "enumeration_failed"

        # ----- avatar_history (shared helper) -----
        avatar_history, avatar_count = await self._search_chat_photo_history(chat, full_chat)

        title = getattr(chat, "title", None)

        return {
            # Common envelope (D-06)
            "id": chat_id,
            "type": "group",
            "name": title,
            "username": None,  # legacy chats have no username
            "about": about,
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            # Group-specific (SPEC Req 7)
            "members_count": members_count,
            "migrated_to": migrated_to,
            "invite_link": invite_link,
            "restrictions": restrictions,
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": contacts_subscribed_partial,
            "contacts_reason": contacts_reason,
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
        if category == "group" and participants_count is not None and participants_count > group_size_threshold:  # noqa: SIM103
            return False
        return True

    async def _collect_unread_dialogs(self, scope: str, group_size_threshold: int) -> tuple[list[dict], dict[int, int]]:
        """Return unread dialog entries from sync.db. Zero Telegram API calls.

        Uses a single grouped query (_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL)
        — scalar subquery computes unread_count per dialog in one round trip.
        Excludes dialogs with read_inbox_max_id IS NULL (not yet bootstrapped).
        See _list_unread_messages for bootstrap_pending visibility.
        """
        rows = self._conn.execute(_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL).fetchall()

        _ENTITY_TYPE_TO_CATEGORY: dict[str, str] = {
            "User": "user",
            "Bot": "bot",
            "Channel": "channel",
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
                category,
                scope,
                None,
                group_size_threshold,
            ):
                continue

            unread_dialogs.append(
                {
                    "chat_id": dialog_id,
                    "display_name": display_name,
                    "unread_count": int(unread_count),
                    "unread_mentions_count": 0,  # not stored — see RESEARCH open question #1
                    "category": category,
                    "date": last_event_at,  # int unix ts (NOT datetime — see _rank_unread_entries)
                    "read_inbox_max_id": read_max,
                }
            )
            unread_counts[dialog_id] = int(unread_count)

        return unread_dialogs, unread_counts

    @staticmethod
    def _rank_unread_entries(entries: list[dict]) -> None:
        """Assign priority tiers and sort in place (lower tier = higher priority)."""
        for entry in entries:
            entry["tier"] = unread_chat_tier(
                {
                    "unread_mentions_count": entry["unread_mentions_count"],
                    "category": entry["category"],
                }
            )
        # date is last_event_at (int unix timestamp) after Plan 38-02 rewrite — not datetime
        entries.sort(key=lambda e: (e["tier"], -(e["date"] if e["date"] else 0)))

    async def _fetch_unread_groups(self, entries: list[dict], allocation: dict[int, int]) -> list[dict]:
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
            msg_ids = [r["message_id"] for r in rows]
            # Phase 39.2 Plan 02: per-dialog JIT freshen + reactions injection.
            if msg_ids:
                await self._freshen_reactions_if_stale(chat_id, chat_id, msg_ids)
                reaction_map = _fetch_reaction_counts(self._conn, chat_id, msg_ids)
                group_messages = [
                    ReadMessage(**dict(r), reactions_display=format_reaction_counts(reaction_map[r["message_id"]]) if r["message_id"] in reaction_map else "")
                    for r in rows
                ]
            else:
                group_messages = [ReadMessage(**dict(r)) for r in rows]
            group["messages"] = [dataclasses.asdict(m) for m in group_messages]
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
            self._conn.execute("DELETE FROM telemetry_events WHERE timestamp < ?", (cutoff,))
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
            for r in self._conn.execute(_GET_DIALOG_TOP_REACTIONS_SQL, (dialog_id, limit)).fetchall()
        ]
        mentions = [
            {"value": r[0], "count": int(r[1])}
            for r in self._conn.execute(_GET_DIALOG_TOP_MENTIONS_SQL, (dialog_id, limit)).fetchall()
        ]
        hashtags = [
            {"value": r[0], "count": int(r[1])}
            for r in self._conn.execute(_GET_DIALOG_TOP_HASHTAGS_SQL, (dialog_id, limit)).fetchall()
        ]
        forwards = [
            {"peer_id": r[0], "name": r[1], "count": int(r[2])}
            for r in self._conn.execute(_GET_DIALOG_TOP_FORWARDS_SQL, (dialog_id, limit)).fetchall()
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
    # _fetch_fragment_context (helper for _list_messages fragment branch)
    # ------------------------------------------------------------------

    async def _fetch_fragment_context(
        self, dialog_id: int, anchor_message_id: int
    ) -> None:
        """Targeted getMessages around an anchor; caches into messages table.

        Per D-08: default context window is 5 messages AFTER the anchor.
        Fragment dialog row is INSERT OR IGNORE (never overwrites 'synced').
        """
        # Ensure synced_dialogs row exists with status='fragment' (idempotent).
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'fragment')",
                (dialog_id,),
            )

        try:
            ids = list(range(anchor_message_id, anchor_message_id + 6))  # anchor + 5 after
            entity = await self._client.get_input_entity(dialog_id)
            fetched = await self._client.get_messages(entity, ids=ids)
        except Exception:
            logger.warning(
                "fragment_fetch_failed dialog_id=%s anchor=%s",
                dialog_id, anchor_message_id, exc_info=True,
            )
            return

        # Upsert into messages using existing sync_worker helpers.
        # ExtractedMessage.message is a StoredMessage dataclass — bind via asdict().
        from dataclasses import asdict

        from .sync_worker import (
            INSERT_MESSAGE_SQL,
            extract_message_row,
            extract_reactions_rows,
        )

        stored_msgs = []
        reaction_rows_all = []
        for msg in fetched:
            if msg is None:
                continue
            extracted = extract_message_row(dialog_id, msg)
            if extracted is None:
                continue
            # Use .message field (StoredMessage dataclass), not the deprecated .row attribute.
            stored_msgs.append(extracted.message)
            reactions = extract_reactions_rows(dialog_id, msg.id, getattr(msg, "reactions", None))
            reaction_rows_all.extend(reactions)

        if not stored_msgs:
            return

        with self._conn:
            # INSERT_MESSAGE_SQL uses named params bound to StoredMessage field names.
            self._conn.executemany(
                INSERT_MESSAGE_SQL,
                [asdict(m) for m in stored_msgs],
            )
            # message_reactions upsert: mirror sync_worker pattern exactly.
            if reaction_rows_all:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO message_reactions "
                    "(dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
                    [(r.dialog_id, r.message_id, r.emoji, r.count) for r in reaction_rows_all],
                )

    # ------------------------------------------------------------------
    # get_my_recent_activity
    # ------------------------------------------------------------------

    async def _get_my_recent_activity(self, req: dict) -> dict:
        """Read own outgoing messages (out=1, non-service, non-deleted) with scan_status context.

        D-05: per-comment blocks, scan_status from activity_sync_state.

        Request: since_hours (int, 1–8760, default 168), limit (int, 1–2000, default 500).
        Response: {"ok": True, "data": {"comments": [...], "scan_status": str, "scanned_at": int|None}}
        Each comment: {"dialog_id", "message_id", "sent_at", "text", "dialog_name"}
        dialog_name falls back to str(dialog_id) when no entities row exists.
        scan_status: "never_run" if backfill_started_at IS NULL (daemon never ran the loop),
                     "in_progress" if backfill_started_at IS NOT NULL but backfill_complete != '1',
                     "complete" if backfill_complete == '1'.
        """
        since_hours_raw = req.get("since_hours", 168)
        try:
            since_hours = int(since_hours_raw)
        except (TypeError, ValueError):
            since_hours = 168
        since_hours = max(1, min(8760, since_hours))

        limit_raw = req.get("limit", 500)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 500
        limit = max(1, min(2000, limit))

        since_ts = int(time.time()) - since_hours * 3600

        rows = self._conn.execute(
            "SELECT m.dialog_id, m.message_id, m.sent_at, m.text, "
            "       e.name AS dialog_name "
            "FROM messages m "
            "LEFT JOIN entities e ON e.id = m.dialog_id "
            # out=1: authored by the account owner.
            # is_service=0: exclude join/leave/group-created system events
            #   — activity_comments never held these rows, so the read
            #   path must not start surfacing them after unification.
            # is_deleted=0: exclude tombstones for messages the user
            #   deleted — same pre-v15 behavior preservation rationale.
            "WHERE m.out = 1 AND m.is_service = 0 AND m.is_deleted = 0 "
            "  AND m.sent_at >= ? "
            "ORDER BY m.sent_at DESC "
            "LIMIT ?",
            (since_ts, limit),
        ).fetchall()

        state_rows = dict(
            self._conn.execute("SELECT key, value FROM activity_sync_state").fetchall()
        )
        backfill_complete = state_rows.get("backfill_complete") == "1"
        backfill_started = state_rows.get("backfill_started_at") is not None
        last_sync_at_str = state_rows.get("last_sync_at")
        last_sync_at: int | None = int(last_sync_at_str) if last_sync_at_str else None

        if backfill_complete:
            scan_status = "complete"
        elif backfill_started:
            scan_status = "in_progress"
        else:
            scan_status = "never_run"

        reactions_by_msg: dict[tuple[int, int], list[dict]] = {}
        if rows:
            rx_params: list[int] = []
            for r in rows:
                rx_params.extend([r[0], r[1]])
            rx_placeholders = ",".join("(?,?)" for _ in rows)
            for rx in self._conn.execute(
                f"SELECT dialog_id, message_id, emoji, count FROM message_reactions "
                f"WHERE (dialog_id, message_id) IN (VALUES {rx_placeholders}) "
                f"ORDER BY count DESC",
                rx_params,
            ).fetchall():
                reactions_by_msg.setdefault((rx[0], rx[1]), []).append(
                    {"emoji": rx[2], "count": rx[3]}
                )

        comments = [
            {
                "dialog_id": r[0],
                "message_id": r[1],
                "sent_at": r[2],
                "text": r[3],
                "dialog_name": r[4] if r[4] else str(r[0]),
                "reactions": reactions_by_msg.get((r[0], r[1]), []),
            }
            for r in rows
        ]

        return {
            "ok": True,
            "data": {
                "comments": comments,
                "scan_status": scan_status,
                "scanned_at": last_sync_at,
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
            row = self._conn.execute(_ENTITY_BY_USERNAME_SQL, (username_query,)).fetchone()
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
        display_name_map = dict(self._conn.execute(_ALL_ENTITY_NAMES_SQL, (now - USER_TTL, now - GROUP_TTL)).fetchall())
        normalized = dict(
            self._conn.execute(_ALL_ENTITY_NAMES_NORMALIZED_SQL, (now - USER_TTL, now - GROUP_TTL)).fetchall()
        )

        result = resolve_entity_sync(query, display_name_map, None, normalized_name_map=normalized)

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
