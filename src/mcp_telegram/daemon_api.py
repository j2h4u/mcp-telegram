"""Daemon API server — Unix socket request dispatcher.

DaemonAPIServer listens on a Unix domain socket and handles seventeen methods:
  - list_messages: read from sync.db (synced dialogs) or Telegram (on-demand)
  - search_messages: FTS5 stemmed full-text search against messages_fts
  - trace_account_messages: observable authored-message evidence for one account
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
  - submit_feedback: write a feedback row to feedback.db

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
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.tl.functions.channels import (
    GetFullChannelRequest,  # type: ignore[import-untyped]
    GetParticipantsRequest,  # type: ignore[import-untyped]
)
from telethon.tl.functions.messages import (  # type: ignore[import-untyped]
    GetCommonChatsRequest,
    GetDialogFiltersRequest,
    GetFullChatRequest,  # type: ignore[import-untyped]
)
from telethon.tl.functions.messages import SearchRequest as MessagesSearchRequest  # type: ignore[import-untyped]
from telethon.tl.functions.photos import GetUserPhotosRequest  # type: ignore[import-untyped]
from telethon.tl.functions.users import GetFullUserRequest  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    Channel,
    ChannelParticipantsContacts,
    Chat,
    ChatReactionsAll,
    ChatReactionsNone,
    ChatReactionsSome,
    InputMessagesFilterChatPhotos,
    MessageActionChatEditPhoto,
)

from .daemon_account_trace import (
    DaemonAccountTraceDeps,
    DaemonAccountTraceService,
)
from .daemon_entity_info import DaemonEntityInfoService, EntityInfoDeps
from .daemon_ipc import get_daemon_socket_path as _get_daemon_socket_path


def get_daemon_socket_path() -> Path:
    """Return the canonical path for the daemon Unix socket."""
    return _get_daemon_socket_path()


def _dialog_type_from_db(conn: sqlite3.Connection, dialog_id: int) -> str:
    """Return dialog type string from sync.db entities table.

    Zero Telegram API calls — pure sqlite lookup. Returns one of the values
    produced by ``DialogType.from_entity()`` (same vocabulary). Returns "Unknown"
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
    if DialogType.parse(dialog_type) != DialogType.USER:
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
        WHERE m.dialog_id = :dialog_id AND m.is_deleted = 0 AND m.is_service = 0
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

from .budget import allocate_message_budget_proportional, unread_chat_tier
from .daemon_message import fetch_reaction_counts, message_to_dict
from .daemon_source_export import (
    _describe_source,
    _export_source_changes,
    _read_source_unit_window,
)
from .feedback_db import VALID_SEVERITIES, VALID_STATUSES
from .formatter import format_reaction_counts
from .fts import stem_query
from .models import DialogType, ReadMessage, ReadState
from .pagination import (
    HistoryDirection,
    decode_navigation_token,
    encode_search_navigation,
)

# Phase 39.2 §Key technical decisions: per-message TTL for JIT reactions freshen-on-read.
# Amortizes rapid paginated reads on the same ids; live events catch most mutations.
REACTIONS_TTL_SECONDS = 600
_TRACE_ACRONYM_MIN_LEN = 2
_TRACE_ACRONYM_MAX_LEN = 4
_TRACE_FUZZY_MIN_LEN = 4
_TRACE_FUZZY_SCORE_MIN = 75
_TELEMETRY_TOOL_NAME_MAX_LEN = 200
_FEEDBACK_MESSAGE_MAX_LEN = 10000
_FEEDBACK_CONTEXT_MAX_LEN = 2000
_FEEDBACK_MODEL_MAX_LEN = 200
_FEEDBACK_HARNESS_MAX_LEN = 200
_UPSERT_ENTITIES_MAX_LEN = 10000
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

DEFAULT_ACTIVITY_DIALOG_KINDS = ("group", "forum")
_ALLOWED_ACTIVITY_DIALOG_KINDS = {"all", "user", "bot", "group", "forum", "channel", "unknown"}
_ACTIVITY_DIALOG_KIND_ALIASES = {
    "dm": ("user", "bot"),
    "dms": ("user", "bot"),
    "private": ("user", "bot"),
    "personal": ("user", "bot"),
    "direct": ("user", "bot"),
    "groups": ("group", "forum"),
    "supergroup": ("group",),
    "supergroups": ("group",),
    "chat": ("group",),
    "chats": ("group",),
    "forums": ("forum",),
}


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
# Phase 44 (LISTDIALOGS-04): stale-snapshot threshold. Computed against
# MAX(dialogs.snapshot_at WHERE hidden=0). Returned in response.data as
# snapshot_age_h: int (hours) when stale, None when fresh or unknown.
#
# NOTE (RESEARCH.md Assumption A2): MAX(snapshot_at) is OPTIMISTIC.
# Even one recently-refreshed dialog will make the whole snapshot appear
# fresh, hiding the case where the majority of rows are stale. This is
# accepted in v1.6: the reconciliation watermark and per-row staleness
# would be more accurate, but require additional schema and reconciliation
# changes that are out of scope. The agent-facing UX is "if stale, surface
# one number; otherwise stay quiet" and MAX is the simplest signal that
# satisfies that contract. Revisit if user feedback shows the optimistic
# bias misleads agents (track in beads, not here).
_SNAPSHOT_STALE_THRESHOLD_S = 12 * 3600


def _compute_snapshot_age_h(max_snapshot_at: int | None) -> int | None:
    """Return integer hours since the freshest snapshot, or None when fresh/unknown.

    Per Assumption A2 (RESEARCH.md): MAX(snapshot_at) is the freshest row's
    timestamp, not a watermark. One refreshed row makes the whole snapshot
    appear fresh — accepted trade-off for v1.6.
    """
    if max_snapshot_at is None:
        return None
    age_s = int(time.time()) - int(max_snapshot_at)
    if age_s > _SNAPSHOT_STALE_THRESHOLD_S:
        return age_s // 3600
    return None


# Phase 44 (LISTDIALOGS-01/02/04, DIFF-04): pure-SQL dialog list.
# LEFT JOIN synced_dialogs to preserve sync_status/total_messages/access_lost_at.
# `:name_pat` is a Python-lowered LIKE pattern (e.g. "%женск%") OR None for
# no pre-filter. Cyrillic case-folding is delegated to the Python fuzzy pass
# because SQLite LOWER() is ASCII-only — see RESEARCH.md Pitfall 1.
# `:archived_filter` and `:pinned_filter` are 0 (filter rows where col=0)
# or None (no filter). See filter_design_contract in 44-01-PLAN.md.
_LIST_DIALOGS_SQL = (
    "SELECT d.dialog_id, d.name, d.type, d.archived, d.pinned, "
    "d.members, d.created, d.last_message_at, d.snapshot_at, "
    "d.unread_mentions_count, d.unread_reactions_count, d.draft_text, "
    "sd.status AS sync_status, sd.total_messages, sd.access_lost_at "
    "FROM dialogs d "
    "LEFT JOIN synced_dialogs sd USING(dialog_id) "
    "WHERE d.hidden = 0 "
    "AND (:archived_filter IS NULL OR d.archived = :archived_filter) "
    "AND (:pinned_filter IS NULL OR d.pinned = :pinned_filter) "
    "AND (:name_pat IS NULL OR LOWER(d.name) LIKE :name_pat ESCAPE '\\') "
    "ORDER BY d.pinned DESC, d.last_message_at DESC"
)

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
    "WHERE sd.status = 'synced' AND m.is_deleted = 0 AND m.is_service = 0 "
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
    "   AND m.is_deleted = 0"
    '   AND m."out" = 0'
    "   AND m.is_service = 0) AS unread_count "
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
    f'AND m."out" = 0 AND m.is_service = 0 '
    f"ORDER BY m.message_id ASC LIMIT :limit"
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
_ENTITY_BY_USERNAME_SQL = "SELECT id, name, username, name_normalized FROM entities WHERE username = ? COLLATE NOCASE"


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


# list_topics — read from topic_metadata snapshot (Phase 45)
_LIST_TOPICS_SQL = (
    "SELECT topic_id, title, icon_emoji_id, date "
    "FROM topic_metadata "
    "WHERE dialog_id = ? AND is_deleted = 0 AND hidden = 0 "
    "ORDER BY topic_id ASC"
)


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


def _assert_select_columns_match_read_message() -> None:
    """Verify SELECT aliases in _LIST_MESSAGES_BASE_SQL cover all
    ReadMessage fields except the two injected post-query fields."""
    from dataclasses import fields as dc_fields

    expected = frozenset(f.name for f in dc_fields(ReadMessage) if f.name not in {"reactions_display", "dialog_name"})
    # Match both `... AS alias` forms and bare table-qualified refs (`m.col`, `mf.col`)
    aliases = frozenset(re.findall(r"\bAS\s+(\w+)", _LIST_MESSAGES_BASE_SQL))
    bare = frozenset(re.findall(r"\b(?:m|mf)\.(\w+)\b", _LIST_MESSAGES_BASE_SQL))
    found = aliases | bare
    missing = expected - found
    extra = found - expected
    assert not missing and not extra, f"SELECT/ReadMessage field mismatch — missing: {missing}, extra: {extra}"


_assert_select_columns_match_read_message()


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
        feedback_conn: sqlite3.Connection | None = None,
    ) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._feedback_conn = feedback_conn  # feedback.db — daemon is sole writer
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
            "SELECT dialog_id FROM synced_dialogs WHERE dialog_id > 0 AND status != 'access_lost'"
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
        """Handle one client connection: read JSON-line requests until EOF.

        DaemonConnection supports multiple sequential request() calls inside one
        async-with block, so the server keeps the stream open and returns one
        response line per request line.
        """
        method = ""
        request_id: str | None = None
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line.decode())
                except json.JSONDecodeError as exc:
                    logger.warning("daemon_api invalid JSON: %s", exc)
                    response = {
                        "ok": False,
                        "error": "invalid_json",
                        "message": "invalid JSON",
                    }
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
                            logger.debug(
                                "daemon_api_request method=%s request_id=%s",
                                method,
                                request_id,
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
                            response = {
                                "ok": False,
                                "error": "internal",
                                "message": "internal error",
                            }
                        finally:
                            _current_request_id.reset(token)
                        if request_id:
                            response = {**response, "request_id": request_id}

                encoded = json.dumps(response).encode() + b"\n"
                writer.write(encoded)
                await writer.drain()
        except ConnectionResetError, BrokenPipeError:
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
        if method == "describe_source":
            return _describe_source(req)
        if method == "export_source_changes":
            return _export_source_changes(self._conn, req)
        if method == "read_source_unit_window":
            return _read_source_unit_window(self._conn, req)
        if method == "search_messages":
            return await self._search_messages(req)
        if method == "trace_account_messages":
            return await self._trace_account_messages(req)
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
        if method == "submit_feedback":
            return await self._submit_feedback(req)
        if method == "update_feedback_status":
            return await self._update_feedback_status(req)
        return {"ok": False, "error": "unknown_method"}

    # (dotMD source-export helpers are defined in daemon_source_export.py)

    # ------------------------------------------------------------------
    # Dialog name resolution
    # ------------------------------------------------------------------

    async def _resolve_dialog_name(self, dialog: str) -> int:
        """Resolve a dialog name string to a numeric dialog_id.

        Resolution order (fastest-first):
        1. client.get_entity() — handles @username, phone, invite link.
        2. entities table — exact/normalized/substring match against cached DB.
        2.5. dialogs snapshot table — Phase 41 mirror of iter_dialogs() data.
             No name_normalized column → no transliteration; Cyrillic queries
             that need anyascii fall through to step 3.
        3. iter_dialogs() — last resort for dialogs not in entities or dialogs snapshot.

        Returns telethon peer id (negative for channels/groups).
        Raises ValueError with descriptive message on failure.
        """
        try:
            entity = await self._client.get_entity(dialog)
            return int(telethon_utils.get_peer_id(entity))
        except ValueError, KeyError:
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

        # Step 2.5: dialogs snapshot table — name lookup with hidden=0 guard.
        # Mirrors entities step 2 structure; uses hidden=0 (same as _LIST_DIALOGS_SQL).
        # Phase 46 D-04: avoids live iter_dialogs() RPC for dialogs already in snapshot.
        #
        # Known limitation: the `dialogs` table has NO `name_normalized` column,
        # so this branch does not perform anyascii / Cyrillic transliteration.
        # A query like "zhenskie sezony" against a dialog named "Женские сезоны"
        # will MISS step 2.5 and fall through to step 3 (iter_dialogs). This is
        # acceptable: the entities table step 2 already covers the transliteration
        # path via `name_normalized`, and step 3 is a correct (if slower) fallback.
        # The cache hit rate of step 2.5 is therefore lower for non-Latin names
        # than the entities path, by design.
        #
        # LIKE-wildcard parity: the `'%' || LOWER(?) || '%'` substring pattern is
        # the same shape used by the entities-table step 2 query above. Literal
        # `%` or `_` in the user's query string are interpreted as LIKE wildcards
        # in BOTH branches — pre-existing behaviour, not a regression. No
        # external-attacker model applies (daemon socket is local-only).
        #
        # Performance note: no index covers `LOWER(name)`; the query does a
        # linear scan over non-hidden rows (filtered by `idx_dialogs_hidden_pinned`).
        # `dialogs` is bounded by user's dialog count (typically <500); sub-ms.
        row = self._conn.execute(
            """
            SELECT dialog_id FROM dialogs
            WHERE hidden = 0
              AND (LOWER(name) = LOWER(?)
                   OR (? != '' AND LOWER(name) LIKE '%' || LOWER(?) || '%'))
            ORDER BY
              CASE WHEN LOWER(name) = LOWER(?) THEN 0
                   ELSE 1
              END
            LIMIT 1
            """,
            (dialog, dialog, dialog, dialog),
        ).fetchone()
        if row:
            logger.debug("resolve_dialog_dialogs_cache hit query=%r id=%d", dialog, row[0])
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

    def _trace_service(self) -> DaemonAccountTraceService:
        return DaemonAccountTraceService(
            DaemonAccountTraceDeps(
                conn=self._conn,
                client=self._client,
                resolve_dialog_id=self._resolve_dialog_id,
                self_id=self.self_id,
                logger=logger,
                rid=_rid,
            )
        )

    async def _trace_account_messages(self, req: dict) -> dict:
        """Return observable authored-message evidence for one account reference."""
        return await self._trace_service()._trace_account_messages(req)

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
        reaction_map = fetch_reaction_counts(self._conn, dialog_id, msg_ids)
        messages = [
            ReadMessage(
                **dict(r),
                reactions_display=format_reaction_counts(reaction_map[r["message_id"]])
                if r["message_id"] in reaction_map
                else "",
            )
            for r in rows
        ]
        # Phase 39: observability counter — mirror main path so anchor branch is not a blind spot.
        null_sender_rows = sum(1 for m in messages if m.sender_id is None)
        unresolved_entity_rows = sum(1 for m in messages if m.sender_id is not None and m.sender_first_name is None)
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
            messages.extend(
                [
                    message_to_dict(msg, dialog_id=dialog_id, self_id=self.self_id)
                    async for msg in self._client.iter_messages(dialog_id, **iter_kwargs)
                ]
            )
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
                if not await self._fetch_fragment_context(dialog_id, context_message_id):
                    return {
                        "ok": False,
                        "error": "fragment_fetch_failed",
                        "message": "Could not fetch messages from Telegram.",
                    }
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
                reaction_map = fetch_reaction_counts(self._conn, dialog_id, msg_ids)
                messages = [
                    ReadMessage(
                        **dict(r),
                        reactions_display=format_reaction_counts(reaction_map[r["message_id"]])
                        if r["message_id"] in reaction_map
                        else "",
                    )
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
        """Return dialog list from the local dialogs snapshot (pure SQL, zero Telegram calls).

        Implements LISTDIALOGS-01 (no iter_dialogs), LISTDIALOGS-02/03 (SQL LIKE pre-filter
        + Python fuzzy pass with ASCII acronym safety-net retry), LISTDIALOGS-04 (snapshot
        staleness annotation), and DIFF-04 (per-row unread_mentions_count, unread_reactions_count,
        draft_text fields added to daemon response for Plan 02 renderer).

        Request keys:
          exclude_archived (bool, default False) — WHERE archived=0 clause
          ignore_pinned    (bool, default False) — WHERE pinned=0 clause (row filter)
          filter           (str|None)            — name filter (SQL LIKE + Python fuzzy)

        Response data:
          dialogs          list of dialog dicts
          snapshot_age_h   int (hours) when MAX(snapshot_at) > 12h old, else None
          bootstrap_pending bool — True only when dialogs table has zero rows (sync not started)

        WR-06 contract preserved: User rows carry unread_in/unread_out; non-User rows omit both.
        """
        # -- param extraction -------------------------------------------------
        exclude_archived: bool = bool(req.get("exclude_archived", False))
        ignore_pinned: bool = bool(req.get("ignore_pinned", False))
        name_filter_raw: str | None = req.get("filter")

        # -- filter_norm + SQL LIKE pattern -----------------------------------
        # Empty / whitespace-only filter treated as no filter.
        # ASCII filter: SQL LIKE pre-filter + Python fuzzy pass.
        # Cyrillic filter: LIKE skipped (SQLite LOWER() is ASCII-only per RESEARCH.md
        # Pitfall 1, Assumption A1); Python fuzzy pass runs on the unfiltered SQL set.
        filter_norm: str | None = None
        name_pat: str | None = None
        if name_filter_raw is not None:
            stripped = name_filter_raw.strip()
            if stripped:
                filter_norm = latinize(stripped)
                if stripped.isascii():
                    # Escape LIKE-special chars before wrapping in %...%
                    esc = stripped.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    name_pat = f"%{esc}%"
                # else: Cyrillic — leave name_pat=None, Python pass handles it

        # -- SQL bind params --------------------------------------------------
        archived_filter: int | None = 0 if exclude_archived else None
        pinned_filter: int | None = 0 if ignore_pinned else None

        # -- pre-load enrichment maps (unchanged from pre-rewrite) ------------
        local_counts: dict[int, int] = dict(self._conn.execute(_COUNT_MESSAGES_BY_DIALOG_SQL).fetchall())
        # Plan 39.3-03 Task 4 (AC-11, WR-06): batched per-dialog (unread_in, unread_out).
        # Single GROUP BY pass — hits the messages PRIMARY KEY B-tree.
        unread_counts: dict[int, tuple[int, int]] = {
            row[0]: (int(row[1] or 0), int(row[2] or 0))
            for row in self._conn.execute(_BATCHED_UNREAD_COUNTS_SQL).fetchall()
        }

        # -- main SQL query ---------------------------------------------------
        params: dict = {
            "archived_filter": archived_filter,
            "pinned_filter": pinned_filter,
            "name_pat": name_pat,
        }
        sql_rows = self._conn.execute(_LIST_DIALOGS_SQL, params).fetchall()

        # ASCII acronym safety net (per filter_design_contract, REVIEWS Concern 1):
        # If the LIKE pre-filter was active and returned nothing, but the caller
        # supplied a non-empty filter, retry without LIKE so the Python fuzzy
        # pass can still match acronyms like "KJ" -> "Kitchen Journal".
        if not sql_rows and name_pat is not None and filter_norm:
            params_retry = {**params, "name_pat": None}
            sql_rows = self._conn.execute(_LIST_DIALOGS_SQL, params_retry).fetchall()

        # -- bootstrap-empty path (REVIEWS Concern 2) -------------------------
        # bootstrap_pending=True means the table is truly empty (sync hasn't run).
        # COUNT(*) includes hidden rows — a table with only hidden rows is NOT
        # bootstrap-pending (sync has run, the rows are simply hidden/excluded).
        if not sql_rows:
            count_total = self._conn.execute("SELECT COUNT(*) FROM dialogs").fetchone()[0]
            if count_total == 0:
                return {
                    "ok": True,
                    "data": {
                        "dialogs": [],
                        "snapshot_age_h": None,
                        "bootstrap_pending": True,
                    },
                }
            return {
                "ok": True,
                "data": {
                    "dialogs": [],
                    "snapshot_age_h": None,
                    "bootstrap_pending": False,
                },
            }

        # -- build result list ------------------------------------------------
        dialogs: list[dict] = []
        max_snapshot: int | None = None

        for sql_row in sql_rows:
            (
                d_id,
                d_name,
                d_type,
                _d_archived,
                _d_pinned,
                d_members,
                d_created,
                d_last_at,
                d_snapshot_at,
                d_mentions,
                d_reactions,
                d_draft,
                sd_status,
                sd_total,
                sd_access_lost,
            ) = sql_row

            # -- Python fuzzy filter (Pass 2) ---------------------------------
            # Runs when filter_norm is set (Cyrillic input: always;
            # ASCII input: only when name_pat is already narrow or retry path ran).
            # Match order: substring -> acronym -> partial_ratio.
            if filter_norm is not None:
                raw_name = d_name or ""
                if not raw_name:
                    continue
                name_norm = latinize(raw_name)
                # Acronym initials use raw case-folded chars — latinizing would
                # expand "Ж" to "zh" and break single-char-per-word matching.
                name_initials_raw = "".join(w[0] for w in raw_name.split() if w).lower()
                filter_raw_lc = (name_filter_raw or "").strip().lower()
                if filter_norm in name_norm:
                    pass  # substring hit
                elif (
                    _TRACE_ACRONYM_MIN_LEN <= len(filter_raw_lc) <= _TRACE_ACRONYM_MAX_LEN
                    and filter_raw_lc in name_initials_raw
                ):
                    pass  # acronym hit ("ЖС" -> "жс" ⊆ "kxжс")
                elif (
                    len(filter_norm) >= _TRACE_FUZZY_MIN_LEN
                    and len(name_norm) >= _TRACE_FUZZY_MIN_LEN
                    and _fuzz.partial_ratio(filter_norm, name_norm) >= _TRACE_FUZZY_SCORE_MIN
                ):
                    pass  # typo-tolerant fuzzy hit
                else:
                    continue

            # -- accumulate max_snapshot_at (over visible+filtered rows) ------
            if d_snapshot_at is not None and (max_snapshot is None or d_snapshot_at > max_snapshot):
                max_snapshot = d_snapshot_at

            # -- sync coverage ------------------------------------------------
            coverage_pct = _compute_sync_coverage(sd_total, local_counts.get(d_id, 0))

            # -- row dict (DIFF-04: three new fields added) --------------------
            row: dict = {
                "id": d_id,
                "name": d_name,
                "type": d_type,
                "last_message_at": d_last_at,
                "unread_count": 0,  # legacy key — iter_dialogs value gone; kept for compat
                "members": d_members,
                "created": d_created,
                "sync_status": sd_status if sd_status is not None else "not_synced",
                "sync_coverage_pct": coverage_pct,
                "access_lost_at": sd_access_lost,
                # DIFF-04: per-row snapshot fields (Plan 02 renderer decides display)
                "unread_mentions_count": int(d_mentions or 0),
                "unread_reactions_count": int(d_reactions or 0),
                "draft_text": d_draft,
            }

            # WR-06 contract: unread_in/unread_out ONLY on User (DM) rows.
            # Non-DM rows OMIT both keys entirely — not None, not 0.
            if DialogType.parse(d_type) == DialogType.USER:
                in_cnt, out_cnt = unread_counts.get(d_id, (0, 0))
                row["unread_in"] = in_cnt
                row["unread_out"] = out_cnt

            dialogs.append(row)

        snapshot_age_h = _compute_snapshot_age_h(max_snapshot)
        return {
            "ok": True,
            "data": {
                "dialogs": dialogs,
                "snapshot_age_h": snapshot_age_h,
                "bootstrap_pending": False,
            },
        }

    # ------------------------------------------------------------------
    # list_topics
    # ------------------------------------------------------------------

    async def _list_topics(self, req: dict) -> dict:
        """Return forum topics for a dialog from the topic_metadata snapshot table.

        Zero Telegram API calls. Returns the same response shape as the previous
        live-API implementation. The topic_metadata table is kept current by
        Phase 42 event handlers and Phase 43 reconciliation.

        Request: dialog_id (int) or dialog (str).
        Response data: {"topics": [{"id", "title", "icon_emoji_id", "date"}],
        "dialog_id": int}.
        Errors: missing_dialog, dialog_not_found (from _resolve_dialog_id).
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

        rows = self._conn.execute(_LIST_TOPICS_SQL, (dialog_id,)).fetchall()
        topics = [
            {
                "id": row[0],
                "title": row[1],
                "icon_emoji_id": row[2],
                "date": row[3],
            }
            for row in rows
        ]
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

    async def _get_entity_info(self, req: dict) -> dict:
        """Type-tagged entity inspector covering 5 Telegram entity kinds."""
        service = DaemonEntityInfoService(
            EntityInfoDeps(
                conn=self._conn,
                client=self._client,
                dm_peer_ids=self._dm_peer_ids,
                get_peer_id=telethon_utils.get_peer_id,
                rid=_rid,
                logger=logger,
                now_provider=time.time,
                get_common_chats_request=GetCommonChatsRequest,
                get_dialog_filters_request=GetDialogFiltersRequest,
                get_full_user_request=GetFullUserRequest,
                get_user_photos_request=GetUserPhotosRequest,
                get_messages_search_request=MessagesSearchRequest,
                get_full_channel_request=GetFullChannelRequest,
                get_participants_request=GetParticipantsRequest,
                channel_participants_contacts_request=ChannelParticipantsContacts,
                get_full_chat_request=GetFullChatRequest,
                input_messages_filter_chat_photos=InputMessagesFilterChatPhotos,
                message_action_chat_edit_photo=MessageActionChatEditPhoto,
                chat_reactions_all=ChatReactionsAll,
                chat_reactions_some=ChatReactionsSome,
                chat_reactions_none=ChatReactionsNone,
                channel_type=Channel,
                chat_type=Chat,
            )
        )
        return await service.get_entity_info(req)

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
        dt = DialogType.parse(category)
        if dt == DialogType.CHANNEL:
            return False
        return not (
            dt in (DialogType.SUPERGROUP, DialogType.GROUP, DialogType.FORUM)
            and participants_count is not None
            and participants_count > group_size_threshold
        )

    async def _collect_unread_dialogs(self, scope: str, group_size_threshold: int) -> tuple[list[dict], dict[int, int]]:
        """Return unread dialog entries from sync.db. Zero Telegram API calls.

        Uses a single grouped query (_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL)
        — scalar subquery computes unread_count per dialog in one round trip.
        Excludes dialogs with read_inbox_max_id IS NULL (not yet bootstrapped).
        See _list_unread_messages for bootstrap_pending visibility.
        """
        rows = self._conn.execute(_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL).fetchall()

        unread_dialogs: list[dict] = []
        unread_counts: dict[int, int] = {}

        for row in rows:
            dialog_id, read_max, last_event_at, display_name, entity_type, unread_count = row
            if unread_count == 0:
                continue

            # Single source of truth — parse the stored type (tolerates legacy
            # mixed-case rows) instead of a bespoke capitalized→category map.
            category = DialogType.parse(entity_type)

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
        entries.sort(key=lambda e: (e["tier"], -(e["date"] or 0)))

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
                reaction_map = fetch_reaction_counts(self._conn, chat_id, msg_ids)
                group_messages = [
                    ReadMessage(
                        **dict(r),
                        reactions_display=format_reaction_counts(reaction_map[r["message_id"]])
                        if r["message_id"] in reaction_map
                        else "",
                    )
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
        event = req.get("event")
        if not isinstance(event, dict):
            return {"ok": False, "error": "invalid_input", "message": "event must be a JSON object"}
        tool_name = event.get("tool_name", "")
        if not isinstance(tool_name, str) or len(tool_name) > _TELEMETRY_TOOL_NAME_MAX_LEN:
            return {"ok": False, "error": "invalid_input", "message": "tool_name must be a string (max 200 chars)"}
        try:
            self._conn.execute(
                "INSERT INTO telemetry_events "
                "(tool_name, timestamp, duration_ms, result_count, "
                "has_cursor, page_depth, has_filter, error_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tool_name,
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
    # submit_feedback
    # ------------------------------------------------------------------

    async def _submit_feedback(self, req: dict) -> dict:
        """Persist a feedback row in feedback.db.

        Validates message (required, non-empty after strip, ≤10000 chars) and
        optional severity (must be in VALID_SEVERITIES).  Optional context,
        model, harness fields are length-capped at the daemon layer as
        defense-in-depth — direct socket callers bypass the Pydantic tool layer.

        NOTE: the user-supplied message text is intentionally NOT logged at any
        level to avoid accidental disclosure of sensitive context.
        """
        if self._feedback_conn is None:
            return {
                "ok": False,
                "error": "internal",
                "message": "feedback database not initialised",
            }
        message = req.get("message", "")
        if not isinstance(message, str):
            return {"ok": False, "error": "invalid_input", "message": "message must be a string"}
        stripped = message.strip()
        if not stripped:
            return {"ok": False, "error": "invalid_input", "message": "message is required"}
        if len(message) > _FEEDBACK_MESSAGE_MAX_LEN:
            return {"ok": False, "error": "invalid_input", "message": "message too long (max 10000 chars)"}

        severity = req.get("severity")
        if severity is not None and severity not in VALID_SEVERITIES:
            valid_list = ", ".join(sorted(VALID_SEVERITIES))
            return {
                "ok": False,
                "error": "invalid_input",
                "message": f"severity must be one of: {valid_list}",
            }

        # Defense-in-depth: cap optional fields at the daemon layer too.
        # Pydantic max_length on SubmitFeedback (48-03) blocks oversize payloads
        # from MCP clients, but a direct socket caller could bypass the tool —
        # daemon is the canonical trust boundary, so it enforces the same caps.
        context = req.get("context")
        model = req.get("model")
        harness = req.get("harness")
        if context is not None and len(str(context)) > _FEEDBACK_CONTEXT_MAX_LEN:
            return {"ok": False, "error": "invalid_input", "message": "context too long (max 2000 chars)"}
        if model is not None and len(str(model)) > _FEEDBACK_MODEL_MAX_LEN:
            return {"ok": False, "error": "invalid_input", "message": "model too long (max 200 chars)"}
        if harness is not None and len(str(harness)) > _FEEDBACK_HARNESS_MAX_LEN:
            return {"ok": False, "error": "invalid_input", "message": "harness too long (max 200 chars)"}

        try:
            cur = self._feedback_conn.execute(
                "INSERT INTO feedback (submitted_at, message, severity, context, model, harness) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    stripped,
                    severity,
                    context,
                    model,
                    harness,
                ),
            )
            self._feedback_conn.commit()
            return {"ok": True, "data": {"message": "Feedback recorded. Thank you!", "id": cur.lastrowid}}
        except Exception as exc:
            logger.error("submit_feedback failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "internal", "message": "internal error"}

    # ------------------------------------------------------------------
    # update_feedback_status
    # ------------------------------------------------------------------

    async def _update_feedback_status(self, req: dict) -> dict:
        """Update the status of a feedback row in feedback.db.

        Sole-writer contract: every status change for feedback rows arrives
        through this handler. The CLI never writes feedback.db directly.

        Validates id (positive int), status (must be in VALID_STATUSES),
        and reason (must be None or a str — direct socket callers could
        send arbitrary JSON, so we type-check before binding into SQL).

        Optional `reason` is stored verbatim in the status_comment column;
        omitted reason writes NULL.

        Commit ordering: the UPDATE runs first, then we inspect rowcount.
        If rowcount == 0 (row not found) we return without committing — no
        observable DB change. Only successful updates reach `commit()`.

        NOTE: row contents (message text, reason text) are never logged.
        """
        feedback_id = req.get("id")
        if not isinstance(feedback_id, int) or feedback_id <= 0:
            return {
                "ok": False,
                "error": "invalid_input",
                "message": "id must be a positive integer",
            }

        status = req.get("status")
        if status not in VALID_STATUSES:
            valid_list = ", ".join(sorted(VALID_STATUSES))
            return {
                "ok": False,
                "error": "invalid_input",
                "message": f"status must be one of: {valid_list}",
            }

        reason = req.get("reason")  # may be None or a string
        # Type-check reason BEFORE binding into SQL. A direct socket caller
        # could send a list/dict and trigger a sqlite3 binding error which
        # would surface as 'internal' instead of 'invalid_input'.
        if reason is not None and not isinstance(reason, str):
            return {
                "ok": False,
                "error": "invalid_input",
                "message": "reason must be a string or null",
            }

        # T-49-11: no length cap on status_comment by design (single-operator
        # low-volume queue; SQLite handles multi-MB TEXT comfortably).

        if self._feedback_conn is None:
            return {
                "ok": False,
                "error": "internal",
                "message": "feedback database not initialised",
            }

        try:
            cur = self._feedback_conn.execute(
                "UPDATE feedback SET status = ?, status_changed_at = ?, status_comment = ? WHERE id = ?",
                (status, int(time.time()), reason, feedback_id),
            )
            if cur.rowcount == 0:
                # No row matched — do NOT commit (nothing to persist anyway,
                # but explicit ordering keeps the success/no-op paths clean).
                return {
                    "ok": False,
                    "error": "not_found",
                    "message": f"Feedback id {feedback_id} not found.",
                }
            self._feedback_conn.commit()
            # NOTE: response intentionally returns only a confirmation message,
            # not the full updated row. CLI prints the message string. Both
            # reviewers (opencode, codex) flagged this as LOW; accepted for
            # this phase to keep the surface minimal — revisit if `feedback
            # status` UX needs to echo the canonical row state.
            return {
                "ok": True,
                "data": {"message": f"Feedback {feedback_id} status set to '{status}'."},
            }
        except Exception as exc:
            logger.error("update_feedback_status failed: %s", exc, exc_info=True)
            return {
                "ok": False,
                "error": "internal",
                "message": "internal error",
            }

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

    async def _fetch_fragment_context(self, dialog_id: int, anchor_message_id: int) -> bool:
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
                dialog_id,
                anchor_message_id,
                exc_info=True,
            )
            return False

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
            return True

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
        return True

    # ------------------------------------------------------------------
    # get_my_recent_activity
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_activity_dialog_kinds(value: object) -> tuple[list[str] | None, str | None]:
        if value is None:
            return list(DEFAULT_ACTIVITY_DIALOG_KINDS), None
        if isinstance(value, str):
            raw_values: list[object] = [value]
        elif isinstance(value, list | tuple | set):
            raw_values = list(value)
        else:
            return None, "dialog_kinds must be a list of strings"

        normalized_values: list[str] = []
        for raw in raw_values:
            if not isinstance(raw, str):
                return None, "dialog_kinds entries must be strings"
            normalized = raw.strip().lower()
            if not normalized:
                continue
            expanded = _ACTIVITY_DIALOG_KIND_ALIASES.get(normalized, (normalized,))
            for kind in expanded:
                if kind not in _ALLOWED_ACTIVITY_DIALOG_KINDS:
                    allowed = ", ".join(sorted(_ALLOWED_ACTIVITY_DIALOG_KINDS))
                    return None, f"dialog_kinds entries must be one of: {allowed}"
                if kind not in normalized_values:
                    normalized_values.append(kind)

        if "all" in normalized_values:
            return ["all"], None
        if not normalized_values:
            return None, "dialog_kinds must include at least one kind"
        return normalized_values, None

    async def _get_my_recent_activity(self, req: dict) -> dict:
        """Read own outgoing messages (out=1, non-service, non-deleted) with scan_status context.

        D-05: per-comment blocks, scan_status from activity_sync_state.

        Request: since_hours (int, 1–8760, default 168), limit (int, 1–2000, default 500).
        Optional dialog_kinds defaults to ["group", "forum"] to exclude DMs.
        Response: {"ok": True, "data": {"comments": [...], "scan_status": str, "scanned_at": int|None}}
        Each comment includes dialog identity/type, message identity/time/text,
        sync status, reply_count, and reaction counters.
        dialog_name falls back to str(dialog_id) when no entities row exists.
        scan_status: "never_run" if backfill_started_at IS NULL (daemon never ran the loop),
                     "in_progress" if backfill_started_at IS NOT NULL but backfill_complete != '1',
                     "complete" if backfill_complete == '1'.
        """
        since_hours_raw = req.get("since_hours", 168)
        try:
            since_hours = int(since_hours_raw)
        except TypeError, ValueError:
            since_hours = 168
        since_hours = max(1, min(8760, since_hours))

        limit_raw = req.get("limit", 500)
        try:
            limit = int(limit_raw)
        except TypeError, ValueError:
            limit = 500
        limit = max(1, min(2000, limit))

        dialog_kinds, dialog_kind_error = self._normalize_activity_dialog_kinds(
            req.get("dialog_kinds", list(DEFAULT_ACTIVITY_DIALOG_KINDS))
        )
        if dialog_kind_error is not None or dialog_kinds is None:
            return {
                "ok": False,
                "error": "invalid_dialog_kinds",
                "message": dialog_kind_error or "invalid dialog_kinds",
            }

        since_ts = int(time.time()) - since_hours * 3600
        dialog_kind_filter_sql = ""
        query_params: list[object] = [since_ts]
        if dialog_kinds != ["all"]:
            dialog_kind_placeholders = ",".join("?" for _ in dialog_kinds)
            dialog_kind_filter_sql = f"WHERE dialog_kind IN ({dialog_kind_placeholders}) "
            query_params.extend(dialog_kinds)
        query_params.append(limit)

        rows = self._conn.execute(
            "WITH typed_activity AS ("
            "SELECT m.dialog_id AS dialog_id, m.message_id AS message_id, "
            "       m.sent_at AS sent_at, m.text AS text, "
            "       e.name AS dialog_name, "
            "       CASE "
            "         WHEN lower(COALESCE(e.type, '')) = 'bot' THEN 'bot' "
            "         WHEN d.type IS NOT NULL AND d.type != '' THEN d.type "
            "         WHEN e.type IS NOT NULL AND e.type != '' THEN e.type "
            "         ELSE 'unknown' "
            "       END AS dialog_type, "
            "       CASE "
            "         WHEN COALESCE(m.reply_count, 0) >= COALESCE(dr.direct_reply_count, 0) "
            "         THEN COALESCE(m.reply_count, 0) "
            "         ELSE COALESCE(dr.direct_reply_count, 0) "
            "       END AS reply_count, "
            "       sd.status AS sync_status "
            "FROM messages m "
            "LEFT JOIN ("
            "  SELECT dialog_id, reply_to_msg_id AS message_id, COUNT(*) AS direct_reply_count "
            "  FROM messages "
            "  WHERE reply_to_msg_id IS NOT NULL AND is_service = 0 AND is_deleted = 0 "
            "  GROUP BY dialog_id, reply_to_msg_id"
            ") dr ON dr.dialog_id = m.dialog_id AND dr.message_id = m.message_id "
            "LEFT JOIN entities e ON e.id = m.dialog_id "
            "LEFT JOIN dialogs d ON d.dialog_id = m.dialog_id "
            "LEFT JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id "
            # out=1: authored by the account owner.
            # is_service=0: exclude join/leave/group-created system events
            #   — activity_comments never held these rows, so the read
            #   path must not start surfacing them after unification.
            # is_deleted=0: exclude tombstones for messages the user
            #   deleted — same pre-v15 behavior preservation rationale.
            "WHERE m.out = 1 AND m.is_service = 0 AND m.is_deleted = 0 "
            "  AND m.sent_at >= ? "
            "), kinded_activity AS ("
            "SELECT ta.*, "
            "       CASE "
            "         WHEN lower(ta.dialog_type) = 'bot' THEN 'bot' "
            "         WHEN lower(ta.dialog_type) = 'user' THEN 'user' "
            "         WHEN lower(ta.dialog_type) = 'forum' THEN 'forum' "
            "         WHEN EXISTS (SELECT 1 FROM topic_metadata tm WHERE tm.dialog_id = ta.dialog_id) THEN 'forum' "
            "         WHEN lower(ta.dialog_type) IN ('group', 'supergroup', 'chat') THEN 'group' "
            "         WHEN lower(ta.dialog_type) = 'channel' THEN 'channel' "
            "         WHEN lower(ta.dialog_type) = 'unknown' AND ta.dialog_id > 0 THEN 'user' "
            "         WHEN lower(ta.dialog_type) = 'unknown' AND ta.dialog_id < 0 THEN 'group' "
            "         ELSE 'unknown' "
            "       END AS dialog_kind "
            "FROM typed_activity ta"
            ") "
            "SELECT * FROM ("
            "SELECT * FROM kinded_activity "
            f"{dialog_kind_filter_sql}"
            "ORDER BY sent_at DESC, dialog_id DESC, message_id DESC "
            "LIMIT ?"
            ") ORDER BY sent_at ASC, dialog_id ASC, message_id ASC",
            query_params,
        ).fetchall()

        state_rows = dict(self._conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
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
                reactions_by_msg.setdefault((rx[0], rx[1]), []).append({"emoji": rx[2], "count": rx[3]})

        comments = [
            {
                "dialog_id": r[0],
                "message_id": r[1],
                "sent_at": r[2],
                "text": r[3],
                "dialog_name": r[4] or str(r[0]),
                "dialog_type": r[5],
                "dialog_category": r[8],
                "reply_count": r[6],
                "sync_status": r[7],
                "reactions": reactions_by_msg.get((r[0], r[1]), []),
            }
            for r in rows
        ]

        return {
            "ok": True,
            "data": {
                "comments": comments,
                "dialog_kinds": dialog_kinds,
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
        if not isinstance(entities, list) or len(entities) > _UPSERT_ENTITIES_MAX_LEN:
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
                        e.get("name") or None,
                        e.get("username"),
                        latinize(e["name"]) if e.get("name") else None,
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
