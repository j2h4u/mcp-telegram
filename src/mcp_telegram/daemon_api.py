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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

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

from . import daemon_activity_stats as _activity_stats
from .daemon_account_trace import (
    DaemonAccountTraceDeps,
    DaemonAccountTraceService,
)
from .daemon_entity_info import DaemonEntityInfoService, EntityInfoDeps
from .daemon_ipc import get_daemon_socket_path as _get_daemon_socket_path

DEFAULT_ACTIVITY_DIALOG_KINDS = _activity_stats.DEFAULT_ACTIVITY_DIALOG_KINDS
_ACTIVITY_DIALOG_KIND_ALIASES = _activity_stats._ACTIVITY_DIALOG_KIND_ALIASES
_ALLOWED_ACTIVITY_DIALOG_KINDS = _activity_stats._ALLOWED_ACTIVITY_DIALOG_KINDS
_GET_DIALOG_TOP_FORWARDS_SQL = _activity_stats._GET_DIALOG_TOP_FORWARDS_SQL
_GET_DIALOG_TOP_HASHTAGS_SQL = _activity_stats._GET_DIALOG_TOP_HASHTAGS_SQL
_GET_DIALOG_TOP_MENTIONS_SQL = _activity_stats._GET_DIALOG_TOP_MENTIONS_SQL
_GET_DIALOG_TOP_REACTIONS_SQL = _activity_stats._GET_DIALOG_TOP_REACTIONS_SQL
_SELECT_SYNC_STATUS_SQL = _activity_stats._SELECT_SYNC_STATUS_SQL


def _attr(obj: object, name: str, default: object | None = None) -> object | None:
    try:
        return cast(object | None, object.__getattribute__(obj, name))
    except AttributeError:
        return default


def _row_sequence(row: object) -> Sequence[object]:
    return cast(Sequence[object], row)


def _row_mapping(row: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], row)


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
        reactions_display=reactions_display,
        dialog_name=cast(str | None, _row_value(row, "dialog_name")),
    )


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
    row = cast(tuple[object] | None, conn.execute("SELECT type FROM entities WHERE id = ?", (dialog_id,)).fetchone())
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
    row = cast(
        tuple[object, object, object, object, object, object] | None,
        conn.execute(
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
        ).fetchone(),
    )
    # ``row`` is always a single aggregate row (SUM/MIN with no FROM rows yield NULL).
    # Cursor subqueries resolve to NULL when synced_dialogs has no matching row —
    # identical to the previous two-query behaviour.
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


USER_TTL: int = 2_592_000  # 30 days
GROUP_TTL: int = 604_800  # 7 days


from .budget import allocate_message_budget_proportional, unread_chat_tier
from .daemon_message import fetch_reaction_counts
from .daemon_source_export import (
    _describe_source,
    _export_source_changes,
    _read_source_unit_window,
)
from .feedback_db import VALID_SEVERITIES, VALID_STATUSES
from .formatter import format_reaction_counts
from .models import DialogType, ReadMessage, ReadState
from .sync_worker import extract_reactions_rows


class _LoggerLike(Protocol):
    def debug(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def info(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def warning(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def error(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def exception(self, msg: str, *_args: object, **_kwargs: object) -> None: ...


class _DaemonClientLike(Protocol):
    async def get_entity(self, entity_id: str | int) -> object: ...

    def iter_dialogs(self) -> AsyncIterator[object]: ...

    async def get_me(self) -> object | None: ...

    async def get_input_entity(self, dialog_id: int) -> object: ...

    async def get_messages(self, entity: object, ids: list[int]) -> object: ...

    def iter_participants(self, peer: object, limit: int = 0) -> AsyncIterator[object]: ...

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]: ...

    async def __call__(self, request: object) -> object: ...


type _DispatchHandler = Callable[
    [dict[str, object]],
    Awaitable[dict[str, object]] | dict[str, object],
]


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


if TYPE_CHECKING:
    from .daemon_account_trace import _AccountTraceClientLike
    from .daemon_account_trace import _LoggerLike as AccountTraceLoggerLike
    from .daemon_reading import DaemonReadingService
    from .daemon_reading import _ListMessagesDbRequest as ReadingListMessagesDbRequest
    from .daemon_reading import _ListMessagesTelegramRequest as ReadingListMessagesTelegramRequest
    from .daemon_reading import _LoggerLike as ReadingLoggerLike
    from .daemon_reading import _TelegramClientLike as ReadingTelegramClientLike
    from .pagination import HistoryDirection
else:
    _AccountTraceClientLike = object
    AccountTraceLoggerLike = object
    ReadingListMessagesDbRequest = object
    ReadingListMessagesTelegramRequest = object
    ReadingLoggerLike = object
    ReadingTelegramClientLike = object

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


@dataclasses.dataclass(frozen=True)
class _SubmitFeedbackRequest:
    message: str
    severity: str | None
    context: str | None
    model: str | None
    harness: str | None

    @classmethod
    def parse(cls, req: dict) -> _SubmitFeedbackRequest:
        message = req.get("message", "")
        if not isinstance(message, str):
            raise ValueError("message must be a string")

        stripped = message.strip()
        if not stripped:
            raise ValueError("message is required")
        if len(message) > _FEEDBACK_MESSAGE_MAX_LEN:
            raise ValueError("message too long (max 10000 chars)")

        severity = req.get("severity")
        if severity is not None and severity not in VALID_SEVERITIES:
            valid_list = ", ".join(sorted(VALID_SEVERITIES))
            raise ValueError(f"severity must be one of: {valid_list}")

        context = req.get("context")
        model = req.get("model")
        harness = req.get("harness")
        if context is not None and len(str(context)) > _FEEDBACK_CONTEXT_MAX_LEN:
            raise ValueError("context too long (max 2000 chars)")
        if model is not None and len(str(model)) > _FEEDBACK_MODEL_MAX_LEN:
            raise ValueError("model too long (max 200 chars)")
        if harness is not None and len(str(harness)) > _FEEDBACK_HARNESS_MAX_LEN:
            raise ValueError("harness too long (max 200 chars)")

        return cls(
            message=stripped,
            severity=severity,
            context=context,
            model=model,
            harness=harness,
        )


@dataclasses.dataclass(frozen=True)
class _UpdateFeedbackStatusRequest:
    feedback_id: int
    status: str
    reason: str | None

    @classmethod
    def parse(cls, req: dict) -> _UpdateFeedbackStatusRequest:
        feedback_id = req.get("id")
        if not isinstance(feedback_id, int) or feedback_id <= 0:
            raise ValueError("id must be a positive integer")

        status = req.get("status")
        if status not in VALID_STATUSES:
            valid_list = ", ".join(sorted(VALID_STATUSES))
            raise ValueError(f"status must be one of: {valid_list}")

        reason = req.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string or null")

        return cls(feedback_id=feedback_id, status=status, reason=reason)


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
_DATABASE_LIST_NAME_INDEX = 1
_DATABASE_LIST_PATH_INDEX = 2


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
    """Compute sync_coverage_pct.

    Returns int 0-100 when local_count is comparable to the Telegram total.
    Returns None when the total is unknown or the local row count exceeds the
    Telegram-side total, because that is not a meaningful coverage ratio.
    """
    if _sync_coverage_unknown(total_messages, local_count):
        return None
    if total_messages == 0:
        return 100
    return round(local_count / cast(int, total_messages) * 100)


def _sync_coverage_unknown(total_messages: int | None, local_count: int) -> bool:
    return total_messages is None or total_messages < 0 or local_count > total_messages


def _sync_db_path_from_connection(conn: sqlite3.Connection) -> Path | None:
    rows = cast(Sequence[Sequence[object]], conn.execute("PRAGMA database_list").fetchall())
    for values in rows:
        if len(values) > _DATABASE_LIST_PATH_INDEX and values[_DATABASE_LIST_NAME_INDEX] == "main":
            db_path = values[_DATABASE_LIST_PATH_INDEX]
            if db_path:
                return Path(str(db_path))
    return None


def _resolve_sync_db_path(conn: sqlite3.Connection, explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        return explicit_path
    return _sync_db_path_from_connection(conn)


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
            meta["sync_coverage_pct"] = _compute_sync_coverage(total_messages_i, local_count)
            if total_messages_i is None:
                meta["archived_message_count"] = local_count

    return meta


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

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
_LIST_DIALOGS_SQL = """
WITH agent_visible_dialogs AS (
    SELECT
        d.dialog_id,
        d.name,
        d.type,
        d.archived,
        d.pinned,
        d.members,
        d.created,
        COALESCE(d.last_message_at, sd.last_event_at, sd.last_synced_at, sd.access_lost_at) AS last_message_at,
        d.snapshot_at,
        d.unread_mentions_count,
        d.unread_reactions_count,
        d.draft_text,
        sd.status AS sync_status,
        sd.total_messages,
        sd.access_lost_at
    FROM dialogs d
    LEFT JOIN synced_dialogs sd USING(dialog_id)
    WHERE d.hidden = 0 OR sd.status = 'access_lost'

    UNION ALL

    SELECT
        sd.dialog_id,
        NULL AS name,
        NULL AS type,
        0 AS archived,
        0 AS pinned,
        NULL AS members,
        NULL AS created,
        COALESCE(sd.last_event_at, sd.last_synced_at, sd.access_lost_at) AS last_message_at,
        NULL AS snapshot_at,
        0 AS unread_mentions_count,
        0 AS unread_reactions_count,
        NULL AS draft_text,
        sd.status AS sync_status,
        sd.total_messages,
        sd.access_lost_at
    FROM synced_dialogs sd
    LEFT JOIN dialogs d USING(dialog_id)
    WHERE sd.status = 'access_lost' AND d.dialog_id IS NULL
)
SELECT
    dialog_id, name, type, archived, pinned,
    members, created, last_message_at, snapshot_at,
    unread_mentions_count, unread_reactions_count, draft_text,
    sync_status, total_messages, access_lost_at
FROM agent_visible_dialogs
WHERE (:archived_filter IS NULL OR archived = :archived_filter)
AND (:pinned_filter IS NULL OR pinned = :pinned_filter)
AND (:name_pat IS NULL OR LOWER(name) LIKE :name_pat ESCAPE '\\')
ORDER BY pinned DESC, last_message_at DESC
"""

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

# NOTE: This scans all non-deleted rows once to compute per-dialog message
# totals for list_dialogs. `messages` is WITHOUT ROWID with primary key
# `(dialog_id, message_id)`, so add-on dialog/is_deleted indexes should be
# treated as a performance experiment before any migration.
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

# Unread SQL — zero Telegram API calls (Plan 38-02)
# Single grouped query: scalar subquery provides per-dialog unread_count in ONE round trip,
# replacing the N+1 COUNT(*)-per-dialog pattern. Uses idx_synced_dialogs_status_read_position
# (schema v8) for the outer filter and messages PK (dialog_id, message_id) for range scans.
_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL = (
    "SELECT sd.dialog_id, sd.read_inbox_max_id, sd.last_event_at, "
    "COALESCE(e.name, CAST(sd.dialog_id AS TEXT)) AS display_name, "
    "COALESCE(e.type, d.type, 'Unknown') AS entity_type, "
    "d.members AS participants_count, "
    "(SELECT COUNT(*) FROM messages m "
    " WHERE m.dialog_id = sd.dialog_id "
    "   AND m.message_id > sd.read_inbox_max_id "
    "   AND m.is_deleted = 0"
    '   AND m."out" = 0'
    "   AND m.is_service = 0) AS unread_count "
    "FROM synced_dialogs sd "
    "LEFT JOIN entities e ON e.id = sd.dialog_id "
    "LEFT JOIN dialogs d ON d.dialog_id = sd.dialog_id "
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


def _apply_list_messages_anchor_filter(
    sql: str,
    params: dict[str, object],
    req: _ListMessagesDbRequest,
) -> tuple[str, dict[str, object]]:
    anchor_msg_id = req.anchor_msg_id
    if anchor_msg_id is None:
        return sql, params

    anchor_sent_at = _attr(req, "anchor_sent_at", None)
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
    """Build a parameterized SELECT for list_messages against sync.db.

    Returns (sql_string, params_dict).  Column names in the SELECT match
    ReadMessage field names; rows are fetched via conn.row_factory = sqlite3.Row
    and converted to ReadMessage objects by _list_messages_from_db.

    self_id is bound to `req.self_id` (used by the EFFECTIVE_SENDER_ID_SQL CASE
    expression to collapse DM direction). If not set, DM outgoing rows will
    project effective_sender_id=NULL instead of the authenticated user id.
    """
    dialog_id = req.dialog_id
    limit = req.limit
    self_id = _attr(req, "self_id", None)
    direction = req.direction
    anchor_msg_id = req.anchor_msg_id
    sender_id = req.sender_id
    sender_name = req.sender_name
    topic_id = req.topic_id
    unread_after_id = req.unread_after_id

    params: dict[str, object] = {"dialog_id": dialog_id, "limit": limit, "self_id": self_id}
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
        client: _DaemonClientLike,
        shutdown_event: asyncio.Event,
        feedback_conn: sqlite3.Connection | None = None,
        sync_db_path: Path | None = None,
    ) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._sync_db_path = _resolve_sync_db_path(conn, sync_db_path)
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
        self._reading_service: DaemonReadingService | None = None
        self._activity_stats_service: _activity_stats.DaemonActivityStatsService | None = None

    def _get_reading_service(self) -> DaemonReadingService:
        """Get memoized reading-service instance with explicit daemon dependencies."""
        if self._reading_service is None:
            from .daemon_reading import DaemonReadingDeps, DaemonReadingService

            self._reading_service = DaemonReadingService(
                DaemonReadingDeps(
                    conn=self._conn,
                    sync_db_path=self._sync_db_path,
                    client=cast(ReadingTelegramClientLike, self._client),
                    self_id=self.self_id,
                    resolve_dialog_id=self._resolve_dialog_id,
                    fetch_fragment_context=self._fetch_fragment_context,
                    logger=cast(ReadingLoggerLike, logger),
                    rid=_rid,
                )
            )
        return self._reading_service

    def _get_activity_stats_service(self) -> _activity_stats.DaemonActivityStatsService:
        """Get memoized activity/stats service with explicit daemon dependencies."""
        if self._activity_stats_service is None:
            self._activity_stats_service = _activity_stats.DaemonActivityStatsService(
                _activity_stats.DaemonActivityStatsDeps(
                    conn=self._conn,
                    resolve_dialog_id=self._resolve_dialog_id,
                    logger=cast(_activity_stats._LoggerLike, logger),
                )
            )
        return self._activity_stats_service

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
        rows = cast(
            list[tuple[int]],
            self._conn.execute(
                "SELECT dialog_id FROM synced_dialogs WHERE dialog_id > 0 AND status != 'access_lost'"
            ).fetchall(),
        )
        return {row[0] for row in rows}

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_client_line(
        self, line: bytes, method: str, request_id: str | None
    ) -> tuple[dict, str, str | None]:
        try:
            req = cast(dict[str, object], json.loads(line.decode()))
        except json.JSONDecodeError as exc:
            logger.warning("daemon_api invalid JSON: %s", exc)
            return (
                {
                    "ok": False,
                    "error": "invalid_json",
                    "message": "invalid JSON",
                },
                method,
                request_id,
            )

        request_id_obj = req.get("request_id")
        request_id = request_id_obj if isinstance(request_id_obj, str) else None
        method_obj = req.get("method", "")
        method = method_obj if isinstance(method_obj, str) else ""
        if not self._ready:
            return (
                {
                    "ok": False,
                    "error": "daemon_not_ready",
                    "detail": self.startup_detail,
                },
                method,
                request_id,
            )

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
        return response, method, request_id

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
                response, method, request_id = await self._handle_client_line(line, method, request_id)
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

    def _dispatch_handlers(self) -> dict[str, _DispatchHandler]:
        return {
            "list_messages": self._list_messages,
            "describe_source": _describe_source,
            "export_source_changes": lambda req: _export_source_changes(self._conn, req),
            "read_source_unit_window": lambda req: _read_source_unit_window(self._conn, req),
            "search_messages": self._search_messages,
            "trace_account_messages": self._trace_account_messages,
            "list_dialogs": self._list_dialogs,
            "list_topics": self._list_topics,
            "get_me": self._get_me,
            "mark_dialog_for_sync": self._mark_dialog_for_sync,
            "get_sync_status": self._get_sync_status,
            "get_sync_alerts": self._get_sync_alerts,
            "get_entity_info": self._get_entity_info,
            "get_inbox": self._list_unread_messages,
            "record_telemetry": self._record_telemetry,
            "get_usage_stats": self._get_usage_stats,
            "upsert_entities": self._upsert_entities,
            "resolve_entity": self._resolve_entity,
            "get_dialog_stats": self._get_dialog_stats,
            "get_my_recent_activity": self._get_my_recent_activity,
            "submit_feedback": self._submit_feedback,
            "update_feedback_status": self._update_feedback_status,
        }

    async def _dispatch(self, req: dict[str, object]) -> dict[str, object]:
        """Route request to the appropriate handler by method name."""
        method_raw = req.get("method", "")
        method = method_raw if isinstance(method_raw, str) else ""
        handler = cast(_DispatchHandler | None, self._dispatch_handlers().get(method))
        if handler is None:
            return {"ok": False, "error": "unknown_method"}

        result = handler(req)
        if isinstance(result, dict):
            return result
        return cast(dict[str, object], await result)

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
            return int(cast(int, telethon_utils.get_peer_id(entity)))
        except ValueError, KeyError:
            pass
        except Exception:
            logger.debug("get_entity failed for %r, falling back to entities DB", dialog, exc_info=True)

        # Fast path: look up in local entities table (O(1), no network).
        # Priority: exact name match > normalized exact > normalized substring.
        norm = latinize(dialog)
        row = cast(
            tuple[object] | None,
            self._conn.execute(
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
            ).fetchone(),
        )
        if row:
            logger.debug("resolve_dialog_entities_cache hit query=%r id=%d", dialog, row[0])
            return _coerce_int(row[0], 0)

        # Step 2.5: dialogs snapshot table — name lookup with agent-visible guard.
        # Hidden dialogs are skipped unless they are access_lost archives; those
        # remain queryable by name even when Telegram no longer exposes them.
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
        row = cast(
            tuple[object] | None,
            self._conn.execute(
                """
                SELECT d.dialog_id
                FROM dialogs d
                LEFT JOIN synced_dialogs sd USING(dialog_id)
                WHERE (d.hidden = 0 OR sd.status = 'access_lost')
                  AND (LOWER(d.name) = LOWER(?)
                       OR (? != '' AND LOWER(d.name) LIKE '%' || LOWER(?) || '%'))
                ORDER BY
                  CASE WHEN LOWER(d.name) = LOWER(?) THEN 0
                       ELSE 1
                  END
                LIMIT 1
                """,
                (dialog, dialog, dialog, dialog),
            ).fetchone(),
        )
        if row:
            logger.debug("resolve_dialog_dialogs_cache hit query=%r id=%d", dialog, row[0])
            return _coerce_int(row[0], 0)

        # Slow path: iterate dialogs via Telegram API (catches dialogs not yet in entities).
        logger.debug("resolve_dialog_fallback_iter_dialogs query=%r", dialog)
        matched_dialog: object | None = None
        async for d in self._client.iter_dialogs():
            name = _attr(d, "name", "") or ""
            if isinstance(name, str) and name.lower() == dialog.lower():
                matched_dialog = d
                break
            if isinstance(name, str) and dialog.lower() in name.lower() and matched_dialog is None:
                matched_dialog = d

        if matched_dialog is not None:
            entity = _attr(matched_dialog, "entity", None)
            if entity is None:
                raise ValueError(
                    f"Dialog {dialog!r} not found. Check the dialog name or use dialog_id from ListDialogs."
                )
            return int(cast(int, telethon_utils.get_peer_id(entity)))

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
                client=cast(_AccountTraceClientLike, self._client),
                resolve_dialog_id=self._resolve_dialog_id,
                self_id=self.self_id,
                logger=cast(AccountTraceLoggerLike, logger),
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
        """Delegate context-window reads to the reading service."""
        # "list_messages rendered"
        return await self._get_reading_service()._list_messages_context_window(
            dialog_id=dialog_id,
            anchor_message_id=anchor_message_id,
            context_size=context_size,
        )

    async def _list_messages_from_telegram(self, req: object) -> dict:
        """Delegate Telegram fallback reads to the reading service."""
        return await self._get_reading_service()._list_messages_from_telegram(
            cast(ReadingListMessagesTelegramRequest, req)
        )

    # ------------------------------------------------------------------
    # list_messages — helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _maybe_encode_next_nav(
        messages: list[ReadMessage] | list[dict],
        limit: int,
        dialog_id: int,
        direction: str,
        direction_enum: HistoryDirection,
    ) -> str | None:
        """Delegate pagination encoding to the reading service."""
        from .daemon_reading import DaemonReadingService, _NextNavContext

        return DaemonReadingService._maybe_encode_next_nav(
            _NextNavContext(
                messages=messages,
                limit=limit,
                dialog_id=dialog_id,
                direction=direction,
                direction_enum=direction_enum,
                logger=cast(ReadingLoggerLike, logger),
                request_id=_rid,
            ),
        )

    async def _freshen_reactions_if_stale(
        self,
        dialog_id: int,
        entity: object,
        message_ids: list[int],
    ) -> None:
        """Delegate JIT reaction refresh to the reading service."""
        await self._get_reading_service()._freshen_reactions_if_stale(dialog_id, entity, message_ids)

    async def _resolve_unread_position(
        self,
        dialog_id: int,
        unread_after_id: int | None,
    ) -> int | None:
        """Delegate unread-position resolution to the reading service."""
        return await self._get_reading_service()._resolve_unread_position(dialog_id, unread_after_id)

    async def _list_messages_from_db(self, req: dict[str, object]) -> dict:
        """Delegate sync.db reads to the reading service."""
        # "list_messages rendered"
        return await self._get_reading_service()._list_messages_from_db(cast(ReadingListMessagesDbRequest, req))

    # ------------------------------------------------------------------
    # list_messages — navigation decoding
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_history_navigation(
        navigation: str | None,
        dialog_id: int,
        direction: str,
    ) -> tuple[int | None, str] | dict:
        """Delegate history-navigation decoding to the reading service."""
        from .daemon_reading import DaemonReadingService

        return DaemonReadingService._decode_history_navigation(
            navigation,
            dialog_id,
            direction,
        )

    # ------------------------------------------------------------------
    # list_messages — main handler
    # ------------------------------------------------------------------

    async def _list_messages(self, req: dict[str, object]) -> dict:
        """Delegate list_messages orchestration to the reading service."""
        return await self._get_reading_service()._list_messages(cast(dict[str, object], req))

    # ------------------------------------------------------------------
    # search_messages
    # ------------------------------------------------------------------

    async def _search_messages(self, req: dict[str, object]) -> dict:
        """Delegate full-text search to the reading service."""
        return await self._get_reading_service()._search_messages(cast(dict[str, object], req))

    # ------------------------------------------------------------------
    # list_dialogs
    # ------------------------------------------------------------------

    async def _list_dialogs(self, req: dict[str, object]) -> dict:
        """Delegate list_dialogs reads to the reading service."""
        return await self._get_reading_service()._list_dialogs(cast(dict[str, object], req))

    # list_topics
    # ------------------------------------------------------------------

    async def _list_topics(self, req: dict[str, object]) -> dict:
        """Return forum topics for a dialog from the topic_metadata snapshot table.

        Zero Telegram API calls. Returns the same response shape as the previous
        live-API implementation. The topic_metadata table is kept current by
        Phase 42 event handlers and Phase 43 reconciliation.

        Request: dialog_id (int) or dialog (str).
        Response data: {"topics": [{"id", "title", "icon_emoji_id", "date"}],
        "dialog_id": int}.
        Errors: missing_dialog, dialog_not_found (from _resolve_dialog_id).
        """
        dialog_id = _coerce_int(req.get("dialog_id", 0), 0)
        dialog_obj = req.get("dialog")
        dialog = dialog_obj if isinstance(dialog_obj, str) else None

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

        rows = cast(
            list[tuple[object, object, object, object]], self._conn.execute(_LIST_TOPICS_SQL, (dialog_id,)).fetchall()
        )
        topics = [
            {
                "id": int(cast(int | str, row[0])),
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

    async def _get_me(self, req: dict[str, object]) -> dict:
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
                "id": int(cast(int | str, _attr(me, "id", 0))),
                "first_name": _attr(me, "first_name", None),
                "last_name": _attr(me, "last_name", None),
                "username": _attr(me, "username", None),
            },
        }

    # ------------------------------------------------------------------
    # mark_dialog_for_sync
    # ------------------------------------------------------------------

    async def _mark_dialog_for_sync(self, req: dict[str, object]) -> dict:
        """Add or remove a dialog from sync scope in synced_dialogs.

        enable=True: INSERT OR IGNORE with status='not_synced' (daemon picks
        up the new dialog within one heartbeat interval).
        enable=False: UPDATE status back to 'not_synced' (re-queues dialog
        for a full re-sync on the next daemon cycle; local messages are kept).
        """
        dialog_id = _coerce_int(req.get("dialog_id", 0), 0)
        enable = bool(req.get("enable", True))
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

    async def _get_sync_status(self, req: dict[str, object]) -> dict:
        """Return sync status and message statistics for a dialog.

        delete_detection is derived from dialog_id sign:
        - Negative → channel/supergroup → "reliable (channel)"
        - Positive → DM/small group → "best-effort weekly (DM)"
        """
        dialog_id = _coerce_int(req.get("dialog_id", 0), 0)
        row = cast(
            tuple[object, object, object, object, object, object] | None,
            self._conn.execute(_GET_SYNC_STATUS_SQL, (dialog_id,)).fetchone(),
        )

        if row is not None:
            status = str(row[0])
            last_synced_at = cast(int | None, row[1])
            last_event_at = cast(int | None, row[2])
            sync_progress = cast(int | None, row[3])
            total_messages = cast(int | None, row[4])
            access_lost_at = cast(int | None, row[5])
        else:
            status = "not_synced"
            last_synced_at = None
            last_event_at = None
            sync_progress = None
            total_messages = None
            access_lost_at = None

        count_row = cast(tuple[object] | None, self._conn.execute(_COUNT_SYNCED_MESSAGES_SQL, (dialog_id,)).fetchone())
        message_count = int(cast(int | str, count_row[0])) if count_row is not None else 0

        sync_coverage_pct = _compute_sync_coverage(total_messages, message_count)
        delete_detection = "reliable (channel)" if dialog_id < 0 else "best-effort weekly (DM)"

        data: dict = {
            "dialog_id": dialog_id,
            "status": status,
            "message_count": message_count,
            "last_synced_at": last_synced_at,
            "last_event_at": last_event_at,
            "sync_progress": sync_progress,
            "sync_progress_message_id": sync_progress,
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

    async def _get_sync_alerts(self, req: dict[str, object]) -> dict:
        """Return sync alerts: deleted messages, edit history, access-lost dialogs.

        since: unix timestamp — only return alerts newer than this value (default 0).
        limit: max items per category (default 50).
        """
        since = _coerce_int(req.get("since", 0), 0)
        limit = _clamp(_coerce_int(req.get("limit", 50), 50), 1, 500)

        deleted_rows = cast(
            list[tuple[object, object, object, object]],
            self._conn.execute(_GET_DELETED_ALERTS_SQL, (since, limit)).fetchall(),
        )
        deleted_messages = [
            {
                "dialog_id": r[0],
                "message_id": r[1],
                "text": r[2],
                "deleted_at": r[3],
            }
            for r in deleted_rows
        ]

        edit_rows = cast(
            list[tuple[object, object, object, object, object]],
            self._conn.execute(_GET_EDIT_ALERTS_SQL, (since, limit)).fetchall(),
        )
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

        access_lost_rows = cast(
            list[tuple[object, object]], self._conn.execute(_GET_ACCESS_LOST_ALERTS_SQL, (since,)).fetchall()
        )
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

    async def _get_entity_info(self, req: dict[str, object]) -> dict:
        """Type-tagged entity inspector covering 5 Telegram entity kinds."""
        service = DaemonEntityInfoService(
            EntityInfoDeps(
                conn=self._conn,
                client=cast(_DaemonClientLike, self._client),
                dm_peer_ids=self._dm_peer_ids,
                get_peer_id=telethon_utils.get_peer_id,
                rid=_rid,
                logger=cast(logging.Logger, logger),
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

    async def _list_unread_messages(self, req: dict[str, object]) -> dict:
        """Return prioritized unread messages across dialogs.

        Request: scope ("personal"|"all"), limit (int, 1-500),
        group_size_threshold (int).
        Response data: {"groups": [{"dialog_id", "display_name", "tier",
        "category", "unread_count", "unread_mentions_count",
        "messages": [{"message_id", "sent_at", "text", ...}]}]}.
        """
        scope_obj = req.get("scope", "personal")
        scope = scope_obj if isinstance(scope_obj, str) else "personal"
        limit = _clamp(_coerce_int(req.get("limit", 100), 100), 1, 500)
        group_size_threshold = _coerce_int(req.get("group_size_threshold", 100), 100)

        unread_dialogs, unread_counts = await self._collect_unread_dialogs(scope, group_size_threshold)
        self._rank_unread_entries(unread_dialogs)
        allocation = allocate_message_budget_proportional(unread_counts, limit)
        groups = await self._fetch_unread_groups(unread_dialogs, allocation)

        pending_row = cast(tuple[object] | None, self._conn.execute(_COUNT_BOOTSTRAP_PENDING_SQL).fetchone())
        bootstrap_pending = int(cast(int | str, pending_row[0])) if pending_row else 0
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
        rows = cast(
            list[tuple[object, object, object, object, object, object, object]],
            self._conn.execute(_COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL).fetchall(),
        )

        unread_dialogs: list[dict] = []
        unread_counts: dict[int, int] = {}

        for row in rows:
            dialog_id, read_max, last_event_at, display_name, entity_type, participants_count, unread_count = row
            dialog_id_i = int(cast(int | str, dialog_id))
            unread_count_i = int(cast(int | str, unread_count))
            if unread_count_i == 0:
                continue

            # Single source of truth — parse the stored type (tolerates legacy
            # mixed-case rows) instead of a bespoke capitalized→category map.
            category = DialogType.parse(str(entity_type))

            if not self._should_include_unread_dialog(
                category,
                scope,
                cast(int | None, participants_count),
                group_size_threshold,
            ):
                continue

            unread_dialogs.append(
                {
                    "chat_id": dialog_id_i,
                    "display_name": display_name,
                    "unread_count": unread_count_i,
                    "unread_mentions_count": 0,  # not stored — see RESEARCH open question #1
                    "category": category,
                    "date": last_event_at,  # int unix ts (NOT datetime — see _rank_unread_entries)
                    "read_inbox_max_id": read_max,
                }
            )
            unread_counts[dialog_id_i] = unread_count_i

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
            chat_id = int(cast(int | str, entry["chat_id"]))
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

            rows = cast(
                list[Mapping[str, object]],
                self._conn.execute(
                    _FETCH_UNREAD_MESSAGES_SQL,
                    {
                        "dialog_id": chat_id,
                        "after_msg_id": entry["read_inbox_max_id"],
                        "limit": budget,
                        "self_id": self.self_id,
                    },
                ).fetchall(),
            )
            msg_ids = [int(cast(int | str, r["message_id"])) for r in rows]
            # Phase 39.2 Plan 02: per-dialog JIT freshen + reactions injection.
            if msg_ids:
                await self._freshen_reactions_if_stale(chat_id, chat_id, msg_ids)
                reaction_map = fetch_reaction_counts(self._conn, chat_id, msg_ids)
                group_messages = [
                    _read_message_from_row(
                        r,
                        reactions_display=format_reaction_counts(reaction_map[int(cast(int | str, r["message_id"]))])
                        if int(cast(int | str, r["message_id"])) in reaction_map
                        else "",
                    )
                    for r in rows
                ]
            else:
                group_messages = [_read_message_from_row(r) for r in rows]
            group["messages"] = [dataclasses.asdict(m) for m in group_messages]
            groups.append(group)

        return groups

    # ------------------------------------------------------------------
    # record_telemetry
    # ------------------------------------------------------------------

    _TELEMETRY_TTL_SECONDS = 30 * 86400  # 30 days

    async def _record_telemetry(self, req: dict[str, object]) -> dict:
        """Write a telemetry event row to sync.db telemetry_events table.

        Evicts rows older than 30 days on every write to prevent unbounded growth.
        """
        event_obj = req.get("event")
        if not isinstance(event_obj, dict):
            return {"ok": False, "error": "invalid_input", "message": "event must be a JSON object"}
        event = cast(Mapping[str, object], event_obj)
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
            logger.exception("record_telemetry failed: %s", exc)
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
        try:
            request = _SubmitFeedbackRequest.parse(req)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_input", "message": str(exc)}

        if self._feedback_conn is None:
            return {"ok": False, "error": "internal", "message": "feedback database not initialised"}

        try:
            cur = self._feedback_conn.execute(
                "INSERT INTO feedback (submitted_at, message, severity, context, model, harness) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    request.message,
                    request.severity,
                    request.context,
                    request.model,
                    request.harness,
                ),
            )
            self._feedback_conn.commit()
            return {"ok": True, "data": {"message": "Feedback recorded. Thank you!", "id": cur.lastrowid}}
        except Exception as exc:
            logger.exception("submit_feedback failed: %s", exc)
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
        try:
            request = _UpdateFeedbackStatusRequest.parse(req)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_input", "message": str(exc)}

        # T-49-11: no length cap on status_comment by design (single-operator
        # low-volume queue; SQLite handles multi-MB TEXT comfortably).

        if self._feedback_conn is None:
            return {"ok": False, "error": "internal", "message": "feedback database not initialised"}

        try:
            cur = self._feedback_conn.execute(
                "UPDATE feedback SET status = ?, status_changed_at = ?, status_comment = ? WHERE id = ?",
                (request.status, int(time.time()), request.reason, request.feedback_id),
            )
            if cur.rowcount == 0:
                # No row matched — do NOT commit (nothing to persist anyway,
                # but explicit ordering keeps the success/no-op paths clean).
                return {"ok": False, "error": "not_found", "message": f"Feedback id {request.feedback_id} not found."}
            self._feedback_conn.commit()
            # NOTE: response intentionally returns only a confirmation message,
            # not the full updated row. CLI prints the message string. Both
            # reviewers (opencode, codex) flagged this as LOW; accepted for
            # this phase to keep the surface minimal — revisit if `feedback
            # status` UX needs to echo the canonical row state.
            return {
                "ok": True,
                "data": {"message": f"Feedback {request.feedback_id} status set to '{request.status}'."},
            }
        except Exception as exc:
            logger.exception("update_feedback_status failed: %s", exc)
            return {"ok": False, "error": "internal", "message": "internal error"}

    # ------------------------------------------------------------------
    # get_usage_stats
    # ------------------------------------------------------------------

    async def _get_usage_stats(self, req: dict[str, object]) -> dict:
        return await self._get_activity_stats_service().get_usage_stats(req)

    # ------------------------------------------------------------------
    # get_dialog_stats
    # ------------------------------------------------------------------

    async def _get_dialog_stats(self, req: dict[str, object]) -> dict:
        return await self._get_activity_stats_service().get_dialog_stats(req)

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
        )

        fetched_rows = cast(Sequence[object | None], fetched)
        extracted_messages = []
        reaction_rows_all = []
        for msg in fetched_rows:
            if msg is None:
                continue
            extracted = extract_message_row(dialog_id, msg)
            if extracted is None:
                continue
            # Use .message field (StoredMessage dataclass), not the deprecated .row attribute.
            extracted_messages.append(extracted)
            msg_id = getattr(msg, "id", None)
            if not isinstance(msg_id, int):
                continue
            reactions = extract_reactions_rows(dialog_id, msg_id, _attr(msg, "reactions", None))
            reaction_rows_all.extend(reactions)

        if not extracted_messages:
            return True

        with self._conn:
            # INSERT_MESSAGE_SQL uses named params bound to StoredMessage field names.
            self._conn.executemany(
                INSERT_MESSAGE_SQL,
                [{**asdict(item.message), "reply_count": item.reply_count} for item in extracted_messages],
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

    async def _get_my_recent_activity(self, req: dict[str, object]) -> dict:
        return await self._get_activity_stats_service().get_my_recent_activity(req)

    # ------------------------------------------------------------------
    # upsert_entities
    # ------------------------------------------------------------------

    async def _upsert_entities(self, req: dict[str, object]) -> dict:
        """Batch upsert entity rows into sync.db entities table.

        Request: entities (list of {"id": int, "type": str, "name": str,
        "username": str|None}, max 10000).
        Response: {"ok": true, "upserted": int} on success.
        Errors: invalid_input (not a list or >10000), internal.
        """
        entities_obj = req.get("entities", [])
        entities = entities_obj if isinstance(entities_obj, list) else []
        if not isinstance(entities, list) or len(entities) > _UPSERT_ENTITIES_MAX_LEN:
            return {"ok": False, "error": "invalid_input", "message": "entities must be a list (max 10000)"}
        if not entities:
            return {"ok": True, "upserted": 0}
        now = int(time.time())
        try:
            mapped_entities = [cast(Mapping[str, object], e) for e in entities]
            self._conn.executemany(
                _UPSERT_ENTITY_SQL,
                [
                    (
                        e["id"],
                        e["type"],
                        e.get("name") or None,
                        e.get("username"),
                        latinize(str(e["name"])) if e.get("name") else None,
                        now,
                    )
                    for e in mapped_entities
                ],
            )
            self._conn.commit()
            return {"ok": True, "upserted": len(entities)}
        except Exception as exc:
            logger.exception("upsert_entities failed: %s", exc)
            return {"ok": False, "error": "internal", "message": "internal error"}

    # ------------------------------------------------------------------
    # resolve_entity
    # ------------------------------------------------------------------

    async def _resolve_entity(self, req: dict[str, object]) -> dict:
        """Fuzzy entity resolution from sync.db entities table.

        Request: query (str — @username or fuzzy name).
        Response data: {"result": "resolved", "entity_id", "display_name"}
        or {"result": "candidates", "matches": [...]}
        or {"result": "not_found", "query"}.
        Errors: missing_query.
        """
        query_obj = req.get("query", "")
        query = query_obj if isinstance(query_obj, str) else ""
        if not query:
            return {"ok": False, "error": "missing_query"}

        # t.me URL: extract @username (and optionally message_id) then fall through
        tme = _parse_tme_link(query)
        if tme is not None:
            query = f"@{tme[0]}"

        # @username lookup
        if query.startswith("@"):
            username_query = query[1:]
            row = cast(
                tuple[object, object, object, object] | None,
                self._conn.execute(_ENTITY_BY_USERNAME_SQL, (username_query,)).fetchone(),
            )
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
            cast(
                list[tuple[int, str]],
                self._conn.execute(_ALL_ENTITY_NAMES_SQL, (now - USER_TTL, now - GROUP_TTL)).fetchall(),
            )
        )
        normalized = dict(
            cast(
                list[tuple[int, str]],
                self._conn.execute(_ALL_ENTITY_NAMES_NORMALIZED_SQL, (now - USER_TTL, now - GROUP_TTL)).fetchall(),
            )
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
