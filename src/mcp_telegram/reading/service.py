"""Application service for all local and Telegram-backed reading operations."""

import asyncio
import dataclasses
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from rapidfuzz import fuzz as _fuzz

from ..budget import allocate_message_budget_proportional, unread_chat_tier
from ..daemon_message import (
    cached_reaction_freshness,
    project_cached_message_facts,
    project_cached_message_facts_by_dialog,
)
from ..dialog_directory_coverage import DialogDirectoryCoverage, read_dialog_directory_coverage
from ..dialog_identity import read_dialog_identities
from ..dialog_identity_contracts import DialogIdentity
from ..dialog_selector import DialogSelector, DialogSelectorError, optional_dialog_selector, required_dialog_selector
from ..fts import stem_query
from ..models import DialogType, DraftReadRecord, ReadMessage, ReadState
from ..own_only import own_only_basis_by_dialog
from ..pagination import (
    HistoryDirection,
    NavigationToken,
    decode_navigation_token,
    encode_history_navigation,
    encode_search_navigation,
)
from ..reactions.contracts import ReactionFreshness
from ..request_timing import current_timing, timing_phase
from ..resolver import latinize
from ..sync_db import open_sync_db_reader
from ..sync_read_model import SyncReadModelContractError, build_sync_read_model
from ..telegram_fragments import FragmentContextService
from ..telegram_reading import (
    GatewayFailure,
    TelegramHistoryGateway,
)
from ..temporal import parse_utc_boundary
from .draft_projection import draft_message_key, read_drafts
from .query_records import read_message_from_row
from .scheduled_projection import (
    build_scheduled_list_query,
    build_scheduled_search_query,
    scheduled_message_time,
    scheduled_messages_available,
    scheduled_row_to_wire,
    scheduled_summary_by_dialog,
)
from .sqlite_projection import (
    _COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL,
    _COUNT_READ_POSITION_PENDING_SQL,
    _FETCH_UNREAD_MESSAGES_SQL,
    _GET_READ_POSITION_SQL,
    _LIST_DIALOG_MESSAGE_AGGREGATES_SQL,
    _LIST_DIALOGS_SQL,
    _LIST_MESSAGES_BASE_SQL,
    _READ_POSITION_PENDING_IDENTITIES_SQL,
    _SELECT_FTS_ALL_SQL,
    _SELECT_FTS_SQL,
    _SELECT_SYNC_STATUS_SQL,
    _UNREAD_SUMMARY_SQL,
    _build_access_metadata,
    _build_list_messages_query,
    _compute_snapshot_age_h,
    _read_state_for_dialog,
    count_dialog_rows,
    message_sent_at,
    read_daemon_state_int,
    read_daemon_state_value,
)

# These thresholds are part of reading's local fuzzy dialog projection.  They
# intentionally do not depend on the account-trace orchestration module.
_TRACE_ACRONYM_MIN_LEN = 2
_TRACE_ACRONYM_MAX_LEN = 4
_TRACE_FUZZY_MIN_LEN = 4
_TRACE_FUZZY_SCORE_MIN = 75


class LoggerLike(Protocol):
    def debug(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def info(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def warning(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def error(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def exception(self, msg: str, *args: object, **kwargs: object) -> None: ...


def _safe_exception_message(exc: BaseException) -> str:
    message = str(exc).replace("\n", "\\n")
    if not message:
        return type(exc).__name__
    return message


def _set_timing_route(route: str, *, fallback: bool = False) -> None:
    timing = current_timing()
    if timing is None:
        return
    if fallback:
        timing.mark_fallback(route)
    else:
        timing.set_route(route)


def _log_rendered_message_stats(logger: LoggerLike, dialog_id: int, messages: Sequence[ReadMessage]) -> None:
    """Record sender-resolution counters for a rendered message page."""
    null_sender_rows = sum(1 for message in messages if message.sender_id is None)
    unresolved_entity_rows = sum(
        1 for message in messages if message.sender_id is not None and message.sender_first_name is None
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


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp *value* to the inclusive range [low, high]."""
    return max(low, min(value, high))


def _selector_error_response(exc: DialogSelectorError) -> dict[str, object]:
    return {"ok": False, "error": exc.code, "message": str(exc)}


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(cast(int | str, value))
    except TypeError, ValueError:
        return default


def _parse_request_boundary(req: Mapping[str, object], field: str) -> int | None:
    raw = req.get(field)
    if raw is not None and not isinstance(raw, str):
        raise ValueError(f"{field} must be an RFC3339 UTC timestamp")
    return parse_utc_boundary(cast(str | None, raw), field=field)


def _validate_time_bounds(since_utc: int | None, until_utc: int | None) -> None:
    if since_utc is not None and until_utc is not None and since_utc >= until_utc:
        raise ValueError("since_utc must be earlier than until_utc")


def _log_recoverable_telegram_error(
    logger: LoggerLike,
    *,
    event: str,
    dialog_id: int,
    exc: BaseException,
    request_id: str,
) -> None:
    logger.warning(
        "%s dialog_id=%d error_type=%s error_message=%s%s",
        event,
        dialog_id,
        type(exc).__name__,
        _safe_exception_message(exc),
        request_id,
    )


@dataclass(frozen=True)
class ReadingDeps:
    """External collaborators required by the reading application service."""

    conn: sqlite3.Connection
    sync_db_path: Path | None
    self_id: int | None
    resolve_dialog_id: Callable[[DialogSelector], Awaitable[int | dict]]
    fragment_context: FragmentContextService
    history_gateway: TelegramHistoryGateway
    logger: LoggerLike
    rid: Callable[[], str]
    deleted_message_visibility_seconds: int
    draft_response_budget_bytes: int
    resolve_dialog_id_local: Callable[[DialogSelector], Awaitable[int | dict]] | None = None


@dataclass(frozen=True)
class _ListMessagesRequest:
    dialog_id: int
    dialog: str | None
    limit: int
    navigation: str | None
    direction: str
    sender_id: int | None
    sender_name: str | None
    topic_id: int | None
    unread_after_id: int | None
    unread: bool
    context_message_id: int | None
    context_size: int
    message_state: str
    since_utc: int | None = None
    until_utc: int | None = None


@dataclass(frozen=True, slots=True)
class _UnreadPosition:
    """A known inbox read cursor for an unread filter."""

    value: int


@dataclass(frozen=True, slots=True)
class _ReadPositionPending:
    """The daemon has not yet reconciled an inbox read cursor."""

    def response(self) -> dict[str, str | bool]:
        return {
            "ok": False,
            "error": "read_position_pending",
            "message": "The inbox read position is not available for this dialog, so unread results are unknown.",
            "required_action": "Retry shortly while the sync daemon reconciles read positions.",
        }


_UnreadPositionResult = _UnreadPosition | _ReadPositionPending


@dataclass
class _ListMessagesDbRequest:
    dialog_id: int
    limit: int
    self_id: int | None
    direction: str
    direction_enum: HistoryDirection
    anchor_msg_id: int | None
    anchor_sent_at: int | None
    sender_id: int | None
    sender_name: str | None
    topic_id: int | None
    unread_after_id: int | None
    unread: bool = False
    since_utc: int | None = None
    until_utc: int | None = None


@dataclass(frozen=True)
class _AllLocalStateRequest:
    dialog_id: int
    request: _ListMessagesRequest
    direction: str
    status: str | None
    db_request: _ListMessagesDbRequest
    navigation: NavigationToken | None


@dataclass(frozen=True)
class _AllLocalState:
    request: _AllLocalStateRequest
    sent_rows: list[dict]
    scheduled_rows: list[dict]
    draft_rows: list[dict]
    draft_result: dict | None
    metadata: tuple[str, ReadState | None, dict[str, object]]


@dataclass
class _AllNavigationPosition:
    """Independent positions for the three streams in an ``all`` cursor."""

    sent_message_id: int | None = None
    sent_at: int | None = None
    scheduled_message_id: int | None = None
    scheduled_sent_at: int | None = None
    draft_key: str | None = None

    @classmethod
    def from_navigation(cls, navigation: NavigationToken | None) -> _AllNavigationPosition:
        if navigation is None:
            return cls()
        return cls(
            sent_message_id=navigation.value,
            sent_at=navigation.sent_at,
            scheduled_message_id=navigation.scheduled_message_id,
            scheduled_sent_at=navigation.scheduled_sent_at,
            draft_key=navigation.draft_key,
        )

    def advance(self, row: dict) -> None:
        state = row.get("message_state")
        if state == "sent":
            self.sent_message_id = _message_id_from_item(row)
            self.sent_at = _object_to_int(row.get("sent_at"))
        elif state == "scheduled":
            self.scheduled_message_id = _message_id_from_item(row)
            self.scheduled_sent_at = _object_to_int(row.get("sent_at"))
        elif state == "draft":
            self.draft_key = str(row["message_key"])


_MAX_TELEGRAM_BOUNDARY_BATCHES = 16


@dataclass(frozen=True)
class _ListMessagesTelegramRequest:
    dialog_id: int
    limit: int
    direction: str
    direction_enum: HistoryDirection
    anchor_msg_id: int | None
    sender_id: int | None
    topic_id: int | None
    unread_after_id: int | None
    unread: bool = False
    since_utc: int | None = None
    until_utc: int | None = None


@dataclass
class _TelegramBatchRun:
    messages: list[object]
    last_raw_message: object | None
    last_batch_index: int
    last_batch_size: int
    last_batch_message_id: int
    last_batch_previous_offset: int | None
    failure: GatewayFailure | None = None


@dataclass(frozen=True, slots=True)
class _HistoryNavigationContext:
    """Immutable request context bound into a history continuation token."""

    dialog_id: int
    direction: str
    message_state: str
    topic_id: int | None
    unread: bool = False
    since_utc: int | None = None
    until_utc: int | None = None


def _coerce_history_navigation_context(
    context_or_dialog_id: _HistoryNavigationContext | int,
    legacy: tuple[object, ...],
    context_kwargs: dict[str, object],
) -> _HistoryNavigationContext:
    if isinstance(context_or_dialog_id, _HistoryNavigationContext):
        if legacy or context_kwargs:
            raise TypeError("history navigation context cannot be combined with legacy fields")
        return context_or_dialog_id

    field_names = ("direction", "message_state", "topic_id")
    if len(legacy) > len(field_names):
        raise TypeError("too many legacy history navigation fields")
    fields = list(legacy)
    for field_name in field_names[len(fields) :]:
        if field_name not in context_kwargs:
            raise TypeError(f"missing history navigation field: {field_name}")
        fields.append(context_kwargs.pop(field_name))
    unknown = set(context_kwargs) - {"unread", "since_utc", "until_utc"}
    if unknown:
        raise TypeError(f"unexpected history navigation fields: {', '.join(sorted(unknown))}")
    return _HistoryNavigationContext(
        dialog_id=context_or_dialog_id,
        direction=cast(str, fields[0]),
        message_state=cast(str, fields[1]),
        topic_id=cast(int | None, fields[2]),
        unread=cast(bool, context_kwargs.get("unread", False)),
        since_utc=cast(int | None, context_kwargs.get("since_utc")),
        until_utc=cast(int | None, context_kwargs.get("until_utc")),
    )


@dataclass(frozen=True)
class _SearchMessagesRequest:
    dialog_id: int
    dialog: str | None
    query: str
    limit: int
    offset: int
    navigation: str | None
    message_state: str
    since_utc: int | None = None
    until_utc: int | None = None


@dataclass(frozen=True)
class _ListDialogsRequest:
    exclude_archived: bool
    ignore_pinned: bool
    filter_raw: str | None
    message_state: str
    scope: str


@dataclass(frozen=True)
class _ListDialogsFilter:
    raw: str | None
    normalized: str | None
    raw_lower: str | None


@dataclass(frozen=True, slots=True)
class _DialogMessageAggregate:
    local_message_count: int = 0
    unread_in: int = 0
    unread_out: int = 0


@dataclass(frozen=True)
class _NextNavContext:
    messages: Sequence[object]
    limit: int
    dialog_id: int
    direction: str
    direction_enum: HistoryDirection
    logger: LoggerLike
    request_id: Callable[[], str]
    topic_id: int | None = None
    message_state: str = "sent"
    unread: bool = False
    since_utc: int | None = None
    until_utc: int | None = None


def _row_mapping(row: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], row)


def _row_sequence(row: object) -> Sequence[object]:
    return cast(Sequence[object], row)


def _fetchone_row(cursor: sqlite3.Cursor) -> object | None:
    return cast(object | None, cursor.fetchone())


def _fetchall_rows(cursor: sqlite3.Cursor) -> list[object]:
    rows = cast(Sequence[object], cursor.fetchall())
    return [cast(object, row) for row in rows]


def _row_value(row: object, key: str, default: object | None = None) -> object | None:
    try:
        return cast(object | None, row[key])  # type: ignore[index]
    except AttributeError, IndexError, KeyError, TypeError:
        return default


def _object_to_int(value: object | None, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if value is None:
        return default
    return int(cast(int | str, value))


def _object_to_int_or_none(value: object | None) -> int | None:
    if isinstance(value, int):
        return value
    if value is None:
        return None

    return int(cast(int | str, value))


def _object_to_str_or_none(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _message_id_from_item(item: object) -> int:
    if isinstance(item, ReadMessage):
        return item.message_id
    if isinstance(item, Mapping):
        row = _row_mapping(item)
        return _object_to_int(row["message_id"])
    row_message_id = _row_value(item, "message_id")
    if row_message_id is not None:
        return _object_to_int(row_message_id)
    return _object_to_int(getattr(item, "message_id", None))


def _message_sent_at(item: object) -> int | None:
    if isinstance(item, ReadMessage):
        return item.sent_at
    return _object_to_int_or_none(_row_value(item, "sent_at"))


@dataclass(frozen=True, slots=True)
class _TelegramBatchSelection:
    messages: tuple[object, ...]
    seen_ids: frozenset[int]
    last_message_id: int
    last_raw_message: object | None


@dataclass(frozen=True, slots=True)
class _TelegramBatchRequest:
    batch: Sequence[object]
    seen_ids: frozenset[int]
    current_count: int
    limit: int
    since_utc: int | None
    until_utc: int | None


def _select_telegram_batch(request: _TelegramBatchRequest) -> _TelegramBatchSelection:
    """Select one bounded history batch without mutating request state."""
    selected: list[object] = []
    updated_seen = set(request.seen_ids)
    for message in request.batch:
        message_id = _message_id_from_item(message)
        if message_id in updated_seen:
            continue
        updated_seen.add(message_id)
        sent_at = _message_sent_at(message)
        if not _telegram_message_in_time_range(sent_at, request):
            continue
        selected.append(message)
        if request.current_count + len(selected) >= request.limit:
            break
    return _TelegramBatchSelection(
        messages=tuple(selected),
        seen_ids=frozenset(updated_seen),
        last_message_id=_message_id_from_item(request.batch[-1]) if request.batch else 0,
        last_raw_message=request.batch[-1] if request.batch else None,
    )


def _telegram_message_in_time_range(sent_at: int | None, request: _TelegramBatchRequest) -> bool:
    if sent_at is None:
        return False
    if request.since_utc is not None and sent_at < request.since_utc:
        return False
    return request.until_utc is None or sent_at < request.until_utc


def _next_telegram_offset(last_message_id: int, current_offset: int | None) -> int | None:
    if last_message_id <= 0 or last_message_id == current_offset:
        return None
    return last_message_id


@dataclass(frozen=True, slots=True)
class _TelegramBatchCapContext:
    has_time_bounds: bool
    message_count: int
    limit: int
    batch_size: int
    batch_index: int
    max_batches: int
    last_message_id: int
    previous_offset: int | None


def _telegram_batch_cap_reached(context: _TelegramBatchCapContext) -> bool:
    return (
        context.has_time_bounds
        and context.message_count < context.limit
        and context.batch_size >= context.limit
        and context.batch_index == context.max_batches - 1
        and context.last_message_id > 0
        and context.last_message_id != context.previous_offset
    )


def _telegram_history_kwargs(req: _ListMessagesTelegramRequest) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "limit": req.limit,
            "offset_id": req.anchor_msg_id,
            "from_user": req.sender_id,
            "reply_to": req.topic_id,
            "min_id": req.unread_after_id,
            "reverse": True if req.direction == "oldest" else None,
            "offset_date": datetime.fromtimestamp(req.until_utc, tz=UTC) if req.until_utc is not None else None,
        }.items()
        if value is not None
    }


def _context_uses_fragment_fallback(status: str | None) -> bool:
    return status in (None, "not_synced", "fragment", "own_only")


def _mark_fragment_coverage(result: dict) -> None:
    data = result.get("data")
    if isinstance(data, dict):
        data["coverage"] = "fragment"
    else:
        result["coverage"] = "fragment"


def _status_from_row(row: object | None) -> str | None:
    if row is None:
        return None
    values = _row_sequence(row)
    if not values:
        return None
    value = values[0]
    return None if value is None else str(value)


def _topic_attribution_receipt(conn: sqlite3.Connection, dialog_id: int) -> dict[str, object]:
    """Read the local projection receipt without inferring history completeness."""
    row = _fetchone_row(
        conn.execute(
            "SELECT topic_attribution_version, topic_attribution_state, "
            "topic_attribution_observed_at, topic_attribution_completed_at, topic_attribution_no_topic_count "
            "FROM synced_dialogs WHERE dialog_id = ?",
            (dialog_id,),
        )
    )
    if row is None:
        return {"version": 0, "state": "unknown", "observed_at": None, "completed_at": None, "no_topic_count": 0}
    values = _row_sequence(row)
    return {
        "version": int(cast(int | str, values[0] or 0)),
        "state": str(values[1] or "unknown"),
        "observed_at": values[2],
        "completed_at": values[3],
        "no_topic_count": int(cast(int | str, values[4] or 0)),
    }


def _topic_selection_state(
    *, topic_id: object, messages: Sequence[object], status: str | None, receipt: Mapping[str, object]
) -> str | None:
    """Classify a topic-filtered page without treating NULL as Telegram absence."""
    if topic_id is None:
        return None
    if messages:
        return "present"
    # Topic roots and legal General-topic members can retain NULL attribution
    # even after a complete current extraction pass. A local empty filter
    # therefore cannot prove remote topic absence without a separate
    # topic-member receipt, which this release deliberately does not invent.
    del status, receipt
    return "unknown"


def _dialog_filter_matches_acronym(filter_raw: str, name: str) -> bool:
    if not _TRACE_ACRONYM_MIN_LEN <= len(filter_raw) <= _TRACE_ACRONYM_MAX_LEN:
        return False
    initials = "".join(word[0] for word in name.split() if word).lower()
    return filter_raw in initials


def _dialog_filter_matches_fuzzy(filter_normalized: str, name_normalized: str) -> bool:
    if len(filter_normalized) < _TRACE_FUZZY_MIN_LEN or len(name_normalized) < _TRACE_FUZZY_MIN_LEN:
        return False
    return _fuzz.partial_ratio(filter_normalized, name_normalized) >= _TRACE_FUZZY_SCORE_MIN


class ReadingService:
    """Domain service for list/search/list_dialogs and helper operations."""

    def __init__(self, deps: ReadingDeps) -> None:
        self._deps = deps

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._deps.conn

    @property
    def _logger(self) -> LoggerLike:
        return self._deps.logger

    @staticmethod
    def _parse_list_messages_request(req: dict) -> _ListMessagesRequest:
        since_utc = _parse_request_boundary(req, "since_utc")
        until_utc = _parse_request_boundary(req, "until_utc")
        _validate_time_bounds(since_utc, until_utc)
        return _ListMessagesRequest(
            dialog_id=req.get("dialog_id", 0) or 0,
            dialog=req.get("dialog"),
            limit=_clamp(req.get("limit", 50), 1, 500),
            navigation=req.get("navigation"),
            direction=req.get("direction", "newest"),
            sender_id=req.get("sender_id"),
            sender_name=req.get("sender_name"),
            topic_id=req.get("topic_id"),
            unread_after_id=req.get("unread_after_id"),
            unread=bool(req.get("unread")),
            context_message_id=req.get("context_message_id"),
            context_size=_clamp(req.get("context_size", 10), 2, 50),
            message_state=req.get("message_state", "all"),
            since_utc=since_utc,
            until_utc=until_utc,
        )

    @staticmethod
    def _parse_search_messages_request(req: dict) -> _SearchMessagesRequest:
        since_utc = _parse_request_boundary(req, "since_utc")
        until_utc = _parse_request_boundary(req, "until_utc")
        _validate_time_bounds(since_utc, until_utc)
        return _SearchMessagesRequest(
            dialog_id=req.get("dialog_id", 0) or 0,
            dialog=req.get("dialog"),
            query=req.get("query", ""),
            limit=_clamp(req.get("limit", 20), 1, 200),
            offset=max(0, req.get("offset", 0)),
            navigation=req.get("navigation"),
            message_state=req.get("message_state", "sent"),
            since_utc=since_utc,
            until_utc=until_utc,
        )

    @staticmethod
    def _parse_list_dialogs_request(req: dict) -> _ListDialogsRequest:
        return _ListDialogsRequest(
            exclude_archived=bool(req.get("exclude_archived", False)),
            ignore_pinned=bool(req.get("ignore_pinned", False)),
            filter_raw=req.get("filter"),
            message_state=req.get("message_state", "all"),
            scope=req.get("scope", "all"),
        )

    @staticmethod
    def _prepare_list_dialogs_filter(filter_raw: str | None) -> _ListDialogsFilter:
        raw_lower: str | None = None
        normalized: str | None = None
        if filter_raw is not None:
            stripped = filter_raw.strip()
            if stripped:
                normalized = latinize(stripped)
                raw_lower = stripped.lower()
        return _ListDialogsFilter(
            raw=filter_raw,
            normalized=normalized,
            raw_lower=raw_lower,
        )

    @staticmethod
    def _maybe_encode_next_nav(
        context: _NextNavContext,
    ) -> str | None:
        """Encode a next-page navigation token if the result set is full."""
        if context.messages and len(context.messages) == context.limit:
            last = context.messages[-1]
            last_msg_id = _message_id_from_item(last)
            context.logger.debug(
                "list_messages_pagination anchor_msg_id=%d dialog_id=%d direction=%s%s",
                last_msg_id,
                context.dialog_id,
                context.direction,
                context.request_id(),
            )
            return encode_history_navigation(
                last_msg_id,
                context.dialog_id,
                topic_id=context.topic_id,
                direction=context.direction_enum,
                sent_at=ReadingService._navigation_sent_at(last),
                message_state=context.message_state,
                unread=context.unread,
                since_utc=context.since_utc,
                until_utc=context.until_utc,
            )
        return None

    @staticmethod
    def encode_next_navigation(  # noqa: PLR0913
        *,
        messages: Sequence[object],
        limit: int,
        dialog_id: int,
        direction: str,
        direction_enum: HistoryDirection,
        logger: LoggerLike,
        request_id: Callable[[], str],
    ) -> str | None:
        """Encode a history continuation for daemon IPC wiring."""
        return ReadingService._maybe_encode_next_nav(
            _NextNavContext(
                messages=messages,
                limit=limit,
                dialog_id=dialog_id,
                direction=direction,
                direction_enum=direction_enum,
                logger=logger,
                request_id=request_id,
            )
        )

    @staticmethod
    def _navigation_sent_at(message: object) -> int | None:
        return _message_sent_at(message)

    @staticmethod
    def _telegram_boundary_continuation(
        req: _ListMessagesTelegramRequest,
        last_raw_message: object | None,
    ) -> str | None:
        """Continue after the last raw batch when bounded paging hits its cap."""
        if last_raw_message is None:
            return None
        message_id = _message_id_from_item(last_raw_message)
        if message_id <= 0:
            return None
        return encode_history_navigation(
            message_id,
            req.dialog_id,
            topic_id=req.topic_id,
            direction=req.direction_enum,
            sent_at=ReadingService._navigation_sent_at(last_raw_message),
            message_state="sent",
            unread=req.unread,
            since_utc=req.since_utc,
            until_utc=req.until_utc,
        )

    def _telegram_next_navigation(
        self,
        req: _ListMessagesTelegramRequest,
        messages: list[object],
        last_raw_message: object | None,
        batch_cap_reached: bool,
    ) -> str | None:
        if batch_cap_reached and len(messages) < req.limit:
            return self._telegram_boundary_continuation(req, last_raw_message)
        return self._maybe_encode_next_nav(
            _NextNavContext(
                messages=messages,
                limit=req.limit,
                dialog_id=req.dialog_id,
                direction=req.direction,
                direction_enum=req.direction_enum,
                topic_id=req.topic_id,
                logger=self._logger,
                request_id=self._deps.rid,
                message_state="sent",
                unread=req.unread,
                since_utc=req.since_utc,
                until_utc=req.until_utc,
            ),
        )

    @staticmethod
    def _decode_history_navigation(
        navigation: str | None,
        context_or_dialog_id: _HistoryNavigationContext | int,
        *legacy: object,
        **context_kwargs: object,
    ) -> tuple[int | None, str] | dict:
        """Decode a history navigation token into (anchor_msg_id, direction)."""
        context = _coerce_history_navigation_context(context_or_dialog_id, legacy, context_kwargs)
        direction = context.direction
        anchor_msg_id: int | None = None
        if navigation and navigation not in ("newest", "oldest"):
            try:
                nav = decode_navigation_token(navigation)
            except ValueError as exc:
                return {"ok": False, "error": "invalid_navigation", "message": str(exc)}
            error_message = ReadingService._history_navigation_error(
                nav,
                context,
            )
            if error_message is not None:
                return {"ok": False, "error": "invalid_navigation", "message": error_message}
            anchor_msg_id = nav.value
            if nav.direction is not None:
                direction = str(nav.direction)
        elif navigation == "oldest":
            direction = "oldest"
        return anchor_msg_id, direction

    @staticmethod
    def _history_navigation_error(  # noqa: PLR0911
        navigation: NavigationToken,
        context: _HistoryNavigationContext,
    ) -> str | None:
        if navigation.kind != "history":
            return f"Navigation token is for {navigation.kind}, not history"
        if navigation.dialog_id != context.dialog_id:
            return f"Navigation token belongs to dialog {navigation.dialog_id}, not {context.dialog_id}"
        if navigation.message_state != context.message_state:
            return (
                f"Navigation token belongs to message_state {navigation.message_state!r}, not {context.message_state!r}"
            )
        if navigation.unread != context.unread:
            return f"Navigation token belongs to unread={navigation.unread!r}, not {context.unread!r}"
        if navigation.topic_id != context.topic_id:
            return f"Navigation token belongs to topic {navigation.topic_id!r}, not {context.topic_id!r}"
        if navigation.since_utc != context.since_utc or navigation.until_utc != context.until_utc:
            return "Navigation token belongs to a different time range"
        return None

    @staticmethod
    def decode_history_navigation(
        navigation: str | None,
        dialog_id: int,
        direction: str,
        message_state: str,
        topic_id: int | None,
    ) -> tuple[int | None, str] | dict:
        """Decode a history continuation for daemon IPC wiring."""
        return ReadingService._decode_history_navigation(
            navigation,
            _HistoryNavigationContext(
                dialog_id=dialog_id,
                direction=direction,
                message_state=message_state,
                topic_id=topic_id,
            ),
        )

    async def _build_read_messages_from_rows(
        self,
        dialog_id: int,
        rows: Sequence[object],
        *,
        log_rendered: bool,
    ) -> tuple[list[ReadMessage], ReactionFreshness]:
        with timing_phase("local_projection"):
            messages = project_cached_message_facts(
                self._conn,
                dialog_id,
                [read_message_from_row(r) for r in rows],
            )
            freshness = cached_reaction_freshness(len(messages))
        if log_rendered:
            with timing_phase("response_shape"):
                _log_rendered_message_stats(self._logger, dialog_id, messages)
        return messages, freshness

    def _enrich_cached_facts(self, messages: Sequence[ReadMessage]) -> list[ReadMessage]:
        """Project cached facts onto a cross-dialog result without Telegram RPCs.

        Global search spans multiple dialogs, so it cannot use the scoped
        fresheners/read-receipt gateway.  Grouping by the row's dialog keeps
        side-table lookups correctly keyed while preserving the result order.
        Missing fact tables/rows are intentionally represented by the helpers'
        nullable/unavailable defaults.
        """
        with timing_phase("local_projection"):
            return project_cached_message_facts_by_dialog(self._conn, messages)

    def _read_state_per_dialog(self, messages: list[ReadMessage]) -> dict[int, ReadState]:
        read_state_per_dialog: dict[int, ReadState] = {}
        with timing_phase("local_projection"):
            dialog_ids = {m.dialog_id for m in messages if m.dialog_id}
            identities = read_dialog_identities(self._conn, dialog_ids)
            for dialog_id in dialog_ids:
                dialog_type = identities[dialog_id].dialog_type.value
                read_state = _read_state_for_dialog(self._conn, dialog_id, dialog_type)
                if read_state is not None:
                    read_state_per_dialog[dialog_id] = read_state
        return read_state_per_dialog

    async def _list_messages_context_result(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
    ) -> dict:
        with timing_phase("local_projection"):
            row = _fetchone_row(self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)))
            current_status = _status_from_row(row)
        if _context_uses_fragment_fallback(current_status):
            return await self._list_fragment_context_result(dialog_id, request, current_status)
        if current_status not in ("synced", "syncing"):
            return {
                "ok": False,
                "error": "not_synced",
                "message": "Context window is unavailable for this dialog state.",
                "required_action": "Mark the dialog for sync to read broader history, or retry with an anchor_message_id for bounded fragment context.",
                "context_availability": "context_window_unavailable",
                "dialog_status": current_status or "not_synced",
            }
        _set_timing_route("local_context")
        return await self._list_messages_context_window(
            dialog_id=dialog_id,
            anchor_message_id=request.context_message_id or 0,
            context_size=request.context_size,
            since_utc=request.since_utc,
            until_utc=request.until_utc,
        )

    async def _list_fragment_context_result(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        current_status: str | None,
    ) -> dict:
        _set_timing_route("telegram_context_fallback", fallback=True)
        with timing_phase("telegram_fallback"):
            fragment_result = await self._deps.fragment_context.fetch(
                dialog_id,
                request.context_message_id or 0,
                request.context_size,
            )
        if not fragment_result.ok:
            failure = fragment_result.failure
            detail = failure.as_dict() if failure is not None else None
            return {
                "ok": False,
                "error": "fragment_fetch_failed",
                "message": "Could not fetch bounded context from Telegram.",
                "required_action": "Retry with a valid anchor_message_id, or mark the dialog for sync if broader history is needed.",
                "context_availability": "fragment_unavailable",
                "dialog_status": current_status or "not_synced",
                "fragment_failure": detail,
            }
        result = await self._list_messages_context_window(
            dialog_id=dialog_id,
            anchor_message_id=request.context_message_id or 0,
            context_size=request.context_size,
            since_utc=request.since_utc,
            until_utc=request.until_utc,
        )
        _mark_fragment_coverage(result)
        return result

    async def _list_messages_history_result(  # noqa: PLR0914
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        direction: str,
    ) -> dict:
        nav_result = self._decode_history_navigation(
            request.navigation,
            _HistoryNavigationContext(
                dialog_id=dialog_id,
                direction=direction,
                message_state=request.message_state,
                topic_id=request.topic_id,
                unread=request.unread,
                since_utc=request.since_utc,
                until_utc=request.until_utc,
            ),
        )
        if isinstance(nav_result, dict):
            return nav_result
        anchor_msg_id, direction = nav_result

        if request.unread:
            unread_position = await self._resolve_unread_position(dialog_id, request.unread_after_id)
            if isinstance(unread_position, _ReadPositionPending):
                return unread_position.response()
            request = dataclasses.replace(request, unread_after_id=unread_position.value)

        with timing_phase("local_projection"):
            row = _fetchone_row(self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)))
            status = _status_from_row(row)
            identity = read_dialog_identities(self._conn, [dialog_id])[dialog_id]
            dialog_type = identity.dialog_type.value
            read_state = _read_state_for_dialog(self._conn, dialog_id, dialog_type)

        if status in ("synced", "syncing", "access_lost"):
            _set_timing_route("local_history")
            db_request = _ListMessagesDbRequest(
                dialog_id=dialog_id,
                limit=request.limit,
                self_id=self._deps.self_id,
                direction=direction,
                direction_enum=HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST,
                anchor_msg_id=anchor_msg_id,
                anchor_sent_at=self._local_history_anchor_sent_at(dialog_id, anchor_msg_id, request.navigation),
                sender_id=request.sender_id,
                sender_name=request.sender_name,
                topic_id=request.topic_id,
                unread_after_id=request.unread_after_id,
                unread=request.unread,
                since_utc=request.since_utc,
                until_utc=request.until_utc,
            )
            result = await self._list_messages_from_db(db_request)
            with timing_phase("local_projection"):
                access_metadata = _build_access_metadata(self._conn, dialog_id, status)
            result["data"].update(access_metadata)
            result["data"]["dialog_type"] = dialog_type
            result["data"]["dialog_name"] = identity.display_name
            result["data"]["dialog_name_source"] = identity.display_name_source
            result["data"]["read_state"] = read_state
            with timing_phase("local_projection"):
                receipt = _topic_attribution_receipt(self._conn, dialog_id)
                result["data"]["topic_attribution"] = receipt
                selection_state = _topic_selection_state(
                    topic_id=request.topic_id,
                    messages=result["data"]["messages"],
                    status=status,
                    receipt=receipt,
                )
                if selection_state is not None:
                    result["data"]["selection_state"] = selection_state
            return result

        _set_timing_route("telegram_fallback", fallback=True)
        telegram_result = await self._list_messages_from_telegram(
            _ListMessagesTelegramRequest(
                dialog_id=dialog_id,
                limit=request.limit,
                direction=direction,
                direction_enum=HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST,
                anchor_msg_id=anchor_msg_id,
                sender_id=request.sender_id,
                topic_id=request.topic_id,
                unread_after_id=request.unread_after_id,
                unread=request.unread,
                since_utc=request.since_utc,
                until_utc=request.until_utc,
            )
        )
        if telegram_result.get("ok"):
            telegram_result["data"]["dialog_access"] = "live"
            telegram_result["data"]["dialog_type"] = dialog_type
            telegram_result["data"]["dialog_name"] = identity.display_name
            telegram_result["data"]["dialog_name_source"] = identity.display_name_source
            telegram_result["data"]["read_state"] = read_state
        return telegram_result

    async def _search_messages_global_result(
        self,
        request: _SearchMessagesRequest,
        stemmed: str,
    ) -> dict:
        with timing_phase("local_projection"):
            rows = _fetchall_rows(
                self._conn.execute(
                    _SELECT_FTS_ALL_SQL,
                    {
                        "query": stemmed,
                        "limit": request.limit,
                        "offset": request.offset,
                        "self_id": self._deps.self_id,
                        "since_utc": request.since_utc,
                        "until_utc": request.until_utc,
                    },
                )
            )
            identities = read_dialog_identities(
                self._conn, {_object_to_int(_row_value(row, "dialog_id")) for row in rows}
            )
        messages = [
            dataclasses.replace(
                message,
                dialog_name=identities[message.dialog_id].display_name,
                dialog_name_source=identities[message.dialog_id].display_name_source,
            )
            for message in self._enrich_search_messages(rows)
        ]
        next_nav = self._search_next_navigation(request, messages, global_mode=True)
        return {
            "ok": True,
            "data": {
                "messages": [dataclasses.asdict(m) for m in messages],
                "dialog_name_source": None,
                "total": len(messages),
                "next_navigation": next_nav,
                "read_state_per_dialog": self._read_state_per_dialog(messages),
            },
        }

    async def _search_messages_scoped_result(
        self,
        request: _SearchMessagesRequest,
        stemmed: str,
    ) -> dict:
        with timing_phase("local_projection"):
            rows = _fetchall_rows(
                self._conn.execute(
                    _SELECT_FTS_SQL,
                    {
                        "query": stemmed,
                        "dialog_id": request.dialog_id,
                        "limit": request.limit,
                        "offset": request.offset,
                        "self_id": self._deps.self_id,
                        "since_utc": request.since_utc,
                        "until_utc": request.until_utc,
                    },
                )
            )
        messages, freshness = await self._build_read_messages_from_rows(request.dialog_id, rows, log_rendered=False)
        messages = self._restore_search_plain_text(rows, messages)
        identity = read_dialog_identities(self._conn, [request.dialog_id])[request.dialog_id]
        messages = [
            dataclasses.replace(
                message,
                dialog_name=identity.display_name,
                dialog_name_source=identity.display_name_source,
            )
            for message in messages
        ]
        next_nav = self._search_next_navigation(request, messages, global_mode=False)
        with timing_phase("local_projection"):
            row = _fetchone_row(self._conn.execute(_SELECT_SYNC_STATUS_SQL, (request.dialog_id,)))
            scoped_status = _status_from_row(row)
            access_meta = _build_access_metadata(self._conn, request.dialog_id, scoped_status or "not_synced")
        return {
            "ok": True,
            "data": {
                "messages": [dataclasses.asdict(m) for m in messages],
                "dialog_name": identity.display_name,
                "dialog_name_source": identity.display_name_source,
                "total": len(messages),
                "next_navigation": next_nav,
                "read_state_per_dialog": self._read_state_per_dialog(messages),
                "reaction_freshness": freshness.as_dict(),
                **access_meta,
            },
        }

    def _enrich_search_messages(self, rows: Sequence[object]) -> list[ReadMessage]:
        """Enrich search hits while retaining raw text for plain snippets.

        ``project_cached_message_facts`` remains the sole content projector.  A
        search hit needs the source text that existed before that projection so
        its bounded snippet cannot expose a hidden-link destination.  Keep that
        source value only for this response; list/full-body reads continue to
        return the canonical projected message.
        """
        raw_messages = [read_message_from_row(row) for row in rows]
        projected = self._enrich_cached_facts(raw_messages)
        return self._restore_search_plain_text(rows, projected, raw_messages=raw_messages)

    @staticmethod
    def _restore_search_plain_text(
        rows: Sequence[object],
        messages: Sequence[ReadMessage],
        *,
        raw_messages: Sequence[ReadMessage] | None = None,
    ) -> list[ReadMessage]:
        """Put raw persisted text back on enriched search rows only.

        The row order is the SQL result order, so pairing by position preserves
        all canonical reaction/read/lifecycle facts without another enrichment
        or projection pass.
        """
        raw = list(raw_messages) if raw_messages is not None else [read_message_from_row(row) for row in rows]
        return [dataclasses.replace(message, text=source.text) for message, source in zip(messages, raw, strict=True)]

    def _search_next_navigation(
        self,
        request: _SearchMessagesRequest,
        messages: Sequence[object],
        *,
        global_mode: bool,
    ) -> str | None:
        if messages and len(messages) == request.limit:
            next_offset = request.offset + request.limit
            nav_dialog_id = 0 if global_mode else request.dialog_id
            return encode_search_navigation(
                next_offset,
                nav_dialog_id,
                request.query,
                request.message_state,
                since_utc=request.since_utc,
                until_utc=request.until_utc,
            )
        return None

    def _search_scheduled_messages(self, request: _SearchMessagesRequest) -> dict:
        """Search pending scheduled text in its local mirror.

        This is intentionally a separate source from ``messages_fts`` because
        scheduled rows are mutable and must never enter sent-history FTS.
        The returned rows still use the same ReadMessage envelope plus lifecycle
        metadata as ordinary search results.
        """
        if not scheduled_messages_available(self._conn):
            return {
                "ok": True,
                "data": {"messages": [], "total": 0, "next_navigation": None, "source": "scheduled_messages"},
            }
        with timing_phase("local_projection"):
            own_basis = self._own_only_basis_by_dialog()
            if (request.dialog_id and request.dialog_id not in own_basis) or (not request.dialog_id and not own_basis):
                raw_rows: list[object] = []
            else:
                sql, params = build_scheduled_search_query(
                    dialog_id=request.dialog_id,
                    own_dialog_ids=sorted(own_basis),
                    query=stem_query(request.query),
                    limit=request.limit,
                    offset=request.offset,
                    scheduled_now=int(time.time()),
                    since_utc=request.since_utc,
                    until_utc=request.until_utc,
                )
                raw_rows = _fetchall_rows(self._conn.execute(sql, params))
        with timing_phase("response_shape"):
            dialog_ids = {_object_to_int(_row_value(raw_row, "dialog_id")) for raw_row in raw_rows}
            if request.dialog_id:
                dialog_ids.add(request.dialog_id)
            identities = read_dialog_identities(self._conn, dialog_ids)
            rows = [
                {
                    **scheduled_row_to_wire(
                        cast(Mapping[str, object], raw_row),
                        inclusion_basis=own_basis.get(_object_to_int(_row_value(raw_row, "dialog_id")), ()),
                    ),
                    "dialog_name": identities[_object_to_int(_row_value(raw_row, "dialog_id"))].display_name,
                    "dialog_name_source": identities[
                        _object_to_int(_row_value(raw_row, "dialog_id"))
                    ].display_name_source,
                }
                for raw_row in raw_rows
            ]
            next_nav = self._search_next_navigation(
                request,
                rows,
                global_mode=not request.dialog_id,
            )
        return {
            "ok": True,
            "data": {
                "messages": rows,
                "total": len(rows),
                "next_navigation": next_nav,
                "source": "scheduled_messages",
                "scope": "own_only",
                **(
                    {
                        "dialog_name": identities[request.dialog_id].display_name,
                        "dialog_name_source": identities[request.dialog_id].display_name_source,
                    }
                    if request.dialog_id
                    else {}
                ),
            },
        }

    @staticmethod
    def _merge_search_results(
        sent_result: dict,
        scheduled_result: dict,
        request: _SearchMessagesRequest,
    ) -> dict:
        sent_data = sent_result.get("data", {})
        scheduled_data = scheduled_result.get("data", {})
        rows = [*sent_data.get("messages", []), *scheduled_data.get("messages", [])]
        rows.sort(key=lambda row: (int(row.get("sent_at") or 0), int(row.get("message_id") or 0)))
        page = rows[request.offset : request.offset + request.limit]
        next_navigation = (
            encode_search_navigation(
                request.offset + request.limit,
                request.dialog_id,
                request.query,
                request.message_state,
                since_utc=request.since_utc,
                until_utc=request.until_utc,
            )
            if len(page) == request.limit
            else None
        )
        return {
            "ok": True,
            "data": {
                "messages": page,
                "total": len(page),
                "next_navigation": next_navigation,
                "source": "sync_db+scheduled_messages",
                "read_state_per_dialog": sent_data.get("read_state_per_dialog", {}),
                "scope": "all",
                "dialog_name": sent_data.get("dialog_name", scheduled_data.get("dialog_name")),
                "dialog_name_source": sent_data.get("dialog_name_source", scheduled_data.get("dialog_name_source")),
            },
        }

    def _fetch_list_dialog_rows(
        self,
        conn: sqlite3.Connection,
        request: _ListDialogsRequest,
        dialog_filter: _ListDialogsFilter,
    ) -> list[Mapping[str, object]]:
        params = {
            "archived_filter": 0 if request.exclude_archived else None,
            "pinned_filter": 0 if request.ignore_pinned else None,
        }
        rows = _fetchall_rows(conn.execute(_LIST_DIALOGS_SQL, params))
        return [cast(Mapping[str, object], row) for row in rows]

    @staticmethod
    def _fetch_list_dialog_aggregates(
        conn: sqlite3.Connection,
        dialog_ids: Sequence[int],
    ) -> dict[int, _DialogMessageAggregate]:
        if not dialog_ids:
            return {}
        unique_ids = list(dict.fromkeys(dialog_ids))
        rows = _fetchall_rows(
            conn.execute(
                _LIST_DIALOG_MESSAGE_AGGREGATES_SQL,
                {"dialog_ids_json": json.dumps(unique_ids, separators=(",", ":"))},
            )
        )
        return {
            _object_to_int(_row_value(row, "dialog_id")): _DialogMessageAggregate(
                local_message_count=_object_to_int(_row_value(row, "local_message_count")),
                unread_in=_object_to_int(_row_value(row, "unread_in")),
                unread_out=_object_to_int(_row_value(row, "unread_out")),
            )
            for row in rows
        }

    @staticmethod
    def _fetch_history_enrollment(
        conn: sqlite3.Connection,
        dialog_ids: Sequence[int],
    ) -> dict[int, bool]:
        if not dialog_ids:
            return {}
        unique_ids = list(dict.fromkeys(dialog_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        rows = _fetchall_rows(
            conn.execute(
                f"SELECT dialog_id, enabled FROM full_history_enrollment WHERE dialog_id IN ({placeholders})",
                unique_ids,
            )
        )
        enrollment: dict[int, bool] = {}
        for row in rows:
            raw_enabled = _row_value(row, "enabled")
            if isinstance(raw_enabled, bool) or raw_enabled not in (0, 1):
                raise SyncReadModelContractError(
                    f"invalid persisted history enrollment for dialog {_row_value(row, 'dialog_id')!r}"
                )
            enrollment[_object_to_int(_row_value(row, "dialog_id"))] = raw_enabled == 1
        return enrollment

    def _dialog_row_matches_filter(
        self,
        dialog_filter: _ListDialogsFilter,
        name: str | None,
        username: str | None = None,
    ) -> bool:
        if dialog_filter.normalized is None:
            return True
        filter_raw_lc = dialog_filter.raw_lower or ""
        for raw_name in (name or "", username or ""):
            name_norm = latinize(raw_name)
            if name_norm and (
                dialog_filter.normalized in name_norm
                or _dialog_filter_matches_acronym(filter_raw_lc, raw_name)
                or _dialog_filter_matches_fuzzy(dialog_filter.normalized, name_norm)
            ):
                return True
        return False

    def _list_dialogs_request_error(self, request: _ListDialogsRequest) -> dict | None:
        if request.message_state not in {"sent", "scheduled", "all"}:
            return {
                "ok": False,
                "error": "invalid_message_state",
                "message": "message_state must be sent, scheduled, or all",
            }
        if request.scope not in {"all", "own_only"}:
            return {
                "ok": False,
                "error": "invalid_scope",
                "message": "scope must be all or own_only",
            }
        return None

    def _select_list_dialog_rows(  # noqa: PLR0913, PLR0917
        self,
        sql_rows: Sequence[Mapping[str, object]],
        request: _ListDialogsRequest,
        dialog_filter: _ListDialogsFilter,
        scheduled_summary: Mapping[int, tuple[int, int | None]],
        own_basis: Mapping[int, tuple[str, ...]],
        identities: Mapping[int, DialogIdentity],
    ) -> list[tuple[Mapping[str, object], tuple[int, int | None], tuple[str, ...] | None]]:
        selected_rows: list[tuple[Mapping[str, object], tuple[int, int | None], tuple[str, ...] | None]] = []
        for row in sql_rows:
            dialog_id = _object_to_int(row["dialog_id"])
            identity = identities[dialog_id]
            if request.scope == "own_only" and dialog_id not in own_basis:
                continue
            summary = scheduled_summary.get(dialog_id, (0, None))
            if dialog_id not in own_basis:
                summary = (0, None)
            if request.message_state == "scheduled" and summary[0] == 0:
                continue
            if not self._dialog_row_matches_filter(dialog_filter, identity.display_name, identity.username):
                continue
            selected_rows.append(
                (
                    {
                        **row,
                        "name": identity.display_name,
                        "type": identity.dialog_type.value,
                        "display_name_source": identity.display_name_source,
                    },
                    summary,
                    own_basis.get(dialog_id),
                )
            )
        return selected_rows

    def _project_list_dialog_rows(
        self,
        conn: sqlite3.Connection,
        selected_rows: Sequence[tuple[Mapping[str, object], tuple[int, int | None], tuple[str, ...] | None]],
        request: _ListDialogsRequest,
        directory_coverage: dict,
    ) -> dict:
        dialog_ids = [_object_to_int(row["dialog_id"]) for row, _, _ in selected_rows]
        aggregates = self._fetch_list_dialog_aggregates(conn, dialog_ids)
        enrollment_by_dialog = self._fetch_history_enrollment(conn, dialog_ids)
        read_model_now = int(time.time())
        dialogs: list[dict] = []
        max_snapshot: int | None = None
        for row, summary, inclusion_basis in selected_rows:
            dialog_id = _object_to_int(row["dialog_id"])
            row_data, snapshot_at = self._shape_dialog_row(
                row,
                aggregates.get(dialog_id, _DialogMessageAggregate()),
                enrollment_by_dialog.get(dialog_id),
                read_model_now,
                summary,
                inclusion_basis,
            )
            if snapshot_at is not None and (max_snapshot is None or snapshot_at > max_snapshot):
                max_snapshot = snapshot_at
            dialogs.append(row_data)
        return {
            "ok": True,
            "data": {
                "dialogs": dialogs,
                "snapshot_age_h": _compute_snapshot_age_h(max_snapshot),
                "bootstrap_pending": False,
                "scope": request.scope,
                "directory_coverage": directory_coverage,
            },
        }

    @staticmethod
    def _empty_list_dialogs_response(
        scope: str,
        directory_coverage: dict,
        *,
        bootstrap_pending: bool,
    ) -> dict:
        return {
            "ok": True,
            "data": {
                "dialogs": [],
                "snapshot_age_h": None,
                "bootstrap_pending": bootstrap_pending,
                "scope": scope,
                "directory_coverage": directory_coverage,
            },
        }

    def _shape_dialog_row(  # noqa: PLR0913, PLR0917
        self,
        row: Mapping[str, object],
        aggregate: _DialogMessageAggregate,
        enrollment_enabled: bool | None,
        read_model_now: int,
        scheduled_summary: tuple[int, int | None] = (0, None),
        inclusion_basis: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], int | None]:
        d_id = _object_to_int(row["dialog_id"])
        local_count = aggregate.local_message_count
        sync_read_model = build_sync_read_model(
            persisted_status=cast(str | None, row["sync_status"]),
            enrollment_enabled=enrollment_enabled,
            last_synced_at=_object_to_int_or_none(row["last_synced_at"]),
            last_event_at=_object_to_int_or_none(row["last_event_at"]),
            last_delta_checked_at=_object_to_int_or_none(row["last_delta_checked_at"]),
            saved_message_count=local_count,
            total_messages=_object_to_int_or_none(row["total_messages"]),
            now=read_model_now,
        )

        row_data: dict[str, object] = {
            "id": d_id,
            "name": row["name"],
            "display_name_source": row["display_name_source"],
            "type": row["type"],
            "last_message_at": row["last_message_at"],
            "archived": bool(row["archived"]),
            # Telegram-authoritative dialog fact. NULL means this snapshot has
            # not observed a count; never infer it from local message rows.
            "unread_count": _object_to_int_or_none(row["unread_count"]),
            "members": row["members"],
            "created": row["created"],
            **sync_read_model.to_wire(),
            "access_lost_at": row["access_lost_at"],
            "unread_in": None,
            "unread_out": None,
            "unread_mentions_count": _object_to_int(row["unread_mentions_count"], 0),
            "unread_reactions_count": _object_to_int(row["unread_reactions_count"], 0),
            **ReadingService._dialog_lifecycle_fields(row, scheduled_summary, inclusion_basis),
        }
        if DialogType.parse(_object_to_str_or_none(row["type"])) == DialogType.USER:
            row_data["unread_in"] = aggregate.unread_in
            row_data["unread_out"] = aggregate.unread_out
        return row_data, _object_to_int_or_none(row["snapshot_at"])

    @staticmethod
    def _dialog_lifecycle_fields(
        row: Mapping[str, object],
        scheduled_summary: tuple[int, int | None],
        inclusion_basis: tuple[str, ...] | None,
    ) -> dict[str, object]:
        return {
            "scheduled_count": scheduled_summary[0],
            "next_scheduled_at": scheduled_summary[1],
            "inclusion_basis": list(inclusion_basis) if inclusion_basis is not None else None,
        }

    async def _resolve_unread_position(
        self,
        dialog_id: int,
        unread_after_id: int | None,
    ) -> _UnreadPositionResult:
        """Resolve unread cutoff from synced_dialogs."""
        if unread_after_id is not None:
            return _UnreadPosition(unread_after_id)
        with timing_phase("local_projection"):
            row = _fetchone_row(self._conn.execute(_GET_READ_POSITION_SQL, (dialog_id,)))
        if row is not None:
            values = _row_sequence(row)
            if values and values[0] is not None:
                return _UnreadPosition(_object_to_int(values[0]))
        return _ReadPositionPending()

    async def _list_messages_context_window(
        self,
        dialog_id: int,
        anchor_message_id: int,
        context_size: int,
        since_utc: int | None = None,
        until_utc: int | None = None,
    ) -> dict:
        """Return messages centred on anchor_message_id from sync.db."""
        before_count = context_size // 2
        after_count = context_size - before_count - 1
        with timing_phase("local_projection"):
            before_rows = _fetchall_rows(
                self._conn.execute(
                    _LIST_MESSAGES_BASE_SQL
                    + " AND m.message_id <= :anchor AND (:since_utc IS NULL OR m.sent_at >= :since_utc) AND (:until_utc IS NULL OR m.sent_at < :until_utc) ORDER BY m.message_id DESC LIMIT :limit",
                    {
                        "dialog_id": dialog_id,
                        "self_id": self._deps.self_id,
                        "anchor": anchor_message_id,
                        "limit": context_size,
                        "since_utc": since_utc,
                        "until_utc": until_utc,
                    },
                )
            )

            after_rows = _fetchall_rows(
                self._conn.execute(
                    _LIST_MESSAGES_BASE_SQL
                    + " AND m.message_id > :anchor AND (:since_utc IS NULL OR m.sent_at >= :since_utc) AND (:until_utc IS NULL OR m.sent_at < :until_utc) ORDER BY m.message_id ASC LIMIT :limit",
                    {
                        "dialog_id": dialog_id,
                        "self_id": self._deps.self_id,
                        "anchor": anchor_message_id,
                        "limit": context_size,
                        "since_utc": since_utc,
                        "until_utc": until_utc,
                    },
                )
            )

        with timing_phase("response_shape"):
            selected_before = before_rows[: before_count + 1]
            selected_after = after_rows[:after_count]
            remaining = context_size - len(selected_before) - len(selected_after)
            if remaining > 0:
                selected_after.extend(after_rows[after_count : after_count + remaining])
                remaining = context_size - len(selected_before) - len(selected_after)
            if remaining > 0:
                selected_before.extend(before_rows[before_count + 1 : before_count + 1 + remaining])
            rows = list(reversed(selected_before)) + list(selected_after)
        messages, freshness = await self._build_read_messages_from_rows(dialog_id, rows, log_rendered=True)
        with timing_phase("local_projection"):
            identity = read_dialog_identities(self._conn, [dialog_id])[dialog_id]
            dialog_type = identity.dialog_type.value
            read_state = _read_state_for_dialog(self._conn, dialog_id, dialog_type)
        with timing_phase("response_shape"):
            return {
                "ok": True,
                "data": {
                    "messages": [dataclasses.asdict(m) for m in messages],
                    "source": "sync_db",
                    "anchor_message_id": anchor_message_id,
                    "next_navigation": None,
                    "dialog_type": dialog_type,
                    "dialog_name": identity.display_name,
                    "dialog_name_source": identity.display_name_source,
                    "read_state": read_state,
                    "reaction_freshness": freshness.as_dict(),
                },
            }

    async def _list_messages_from_telegram(
        self,
        req: _ListMessagesTelegramRequest,
    ) -> dict:
        """Fetch messages on-demand from Telegram API."""
        self._logger.debug("list_messages_fallback_telegram dialog_id=%d%s", req.dialog_id, self._deps.rid())
        base_kwargs = _telegram_history_kwargs(req)
        has_time_bounds = req.since_utc is not None or req.until_utc is not None
        max_batches = _MAX_TELEGRAM_BOUNDARY_BATCHES if has_time_bounds else 1
        with timing_phase("telegram_fallback"):
            batch_run = await self._fetch_telegram_batches(req, base_kwargs, max_batches)
        if batch_run.failure is not None:
            return self._list_messages_telegram_error(req, batch_run.failure)
        with timing_phase("response_shape"):
            messages = batch_run.messages
            batch_cap_reached = _telegram_batch_cap_reached(
                _TelegramBatchCapContext(
                    has_time_bounds=has_time_bounds,
                    message_count=len(messages),
                    limit=req.limit,
                    batch_size=batch_run.last_batch_size,
                    batch_index=batch_run.last_batch_index,
                    max_batches=max_batches,
                    last_message_id=batch_run.last_batch_message_id,
                    previous_offset=batch_run.last_batch_previous_offset,
                ),
            )
            messages = messages[: req.limit]

            next_nav = self._telegram_next_navigation(req, messages, batch_run.last_raw_message, batch_cap_reached)
            return {
                "ok": True,
                "data": {"messages": messages, "source": "telegram", "next_navigation": next_nav},
            }

    async def _fetch_telegram_batches(
        self,
        req: _ListMessagesTelegramRequest,
        base_kwargs: dict[str, object],
        max_batches: int,
    ) -> _TelegramBatchRun:
        messages: list[object] = []
        seen_message_ids: set[int] = set()
        next_offset_id = req.anchor_msg_id
        last_raw_message: object | None = None
        last_batch_index = -1
        last_batch_size = 0
        last_batch_message_id = 0
        last_batch_previous_offset: int | None = None
        for batch_index in range(max_batches):
            last_batch_index = batch_index
            last_batch_previous_offset = next_offset_id
            iter_kwargs = {**base_kwargs, "offset_id": next_offset_id} if next_offset_id is not None else base_kwargs
            history_result = await self._deps.history_gateway.fetch_history(
                req.dialog_id,
                iter_kwargs,
                self._deps.self_id,
            )
            if not history_result.ok:
                failure = history_result.failure
                assert failure is not None
                return _TelegramBatchRun(
                    messages=messages,
                    last_raw_message=last_raw_message,
                    last_batch_index=last_batch_index,
                    last_batch_size=last_batch_size,
                    last_batch_message_id=last_batch_message_id,
                    last_batch_previous_offset=last_batch_previous_offset,
                    failure=failure,
                )
            batch = list(history_result.messages)
            last_batch_size = len(batch)
            selection = _select_telegram_batch(
                _TelegramBatchRequest(
                    batch=batch,
                    seen_ids=frozenset(seen_message_ids),
                    current_count=len(messages),
                    limit=req.limit,
                    since_utc=req.since_utc,
                    until_utc=req.until_utc,
                ),
            )
            seen_message_ids = set(selection.seen_ids)
            messages.extend(selection.messages)
            last_batch_message_id = selection.last_message_id
            if selection.last_raw_message is not None:
                last_raw_message = selection.last_raw_message
            if len(messages) >= req.limit or len(batch) < req.limit:
                break
            next_offset = _next_telegram_offset(last_batch_message_id, next_offset_id)
            if next_offset is None:
                break
            next_offset_id = next_offset
        return _TelegramBatchRun(
            messages=messages,
            last_raw_message=last_raw_message,
            last_batch_index=last_batch_index,
            last_batch_size=last_batch_size,
            last_batch_message_id=last_batch_message_id,
            last_batch_previous_offset=last_batch_previous_offset,
        )

    def _list_messages_telegram_error(self, req: _ListMessagesTelegramRequest, failure: GatewayFailure) -> dict:
        if not failure.retryable or failure.kind.value in {"flood_wait", "access_lost", "transient"}:
            _log_recoverable_telegram_error(
                self._logger,
                event="list_messages_telegram_error",
                dialog_id=req.dialog_id,
                exc=RuntimeError(failure.error_message),
                request_id=self._deps.rid(),
            )
            detail: dict[str, object] = {
                "error_type": failure.error_type,
                "error_message": failure.error_message,
                "retryable": failure.retryable,
            }
            if failure.retry_after is not None:
                detail["retry_after"] = failure.retry_after
            return {
                "ok": False,
                "error": "telegram_error",
                "message": "failed to fetch messages",
                "detail": detail,
            }

        self._logger.error(
            "list_messages_telegram_unexpected dialog_id=%d%s",
            req.dialog_id,
            self._deps.rid(),
        )
        return {"ok": False, "error": "telegram_error", "message": "failed to fetch messages"}

    async def _list_messages_from_db(self, req: _ListMessagesDbRequest) -> dict:
        """Read messages from sync.db using the dynamic query builder."""
        with timing_phase("local_projection"):
            sql, params = _build_list_messages_query(req, query_logger=self._logger)
            rows = _fetchall_rows(self._conn.execute(sql, params))

        messages, freshness = await self._build_read_messages_from_rows(req.dialog_id, rows, log_rendered=True)
        with timing_phase("response_shape"):
            next_nav = self._maybe_encode_next_nav(
                _NextNavContext(
                    messages=messages,
                    limit=req.limit,
                    dialog_id=req.dialog_id,
                    direction=req.direction,
                    direction_enum=req.direction_enum,
                    topic_id=req.topic_id,
                    logger=self._logger,
                    request_id=self._deps.rid,
                    message_state="sent",
                    unread=req.unread,
                    since_utc=req.since_utc,
                    until_utc=req.until_utc,
                ),
            )
            return {
                "ok": True,
                "data": {
                    "messages": [dataclasses.asdict(m) for m in messages],
                    "source": "sync_db",
                    "next_navigation": next_nav,
                    "reaction_freshness": freshness.as_dict(),
                },
            }

    def _own_only_basis_by_dialog(self, conn: sqlite3.Connection | None = None) -> dict[int, tuple[str, ...]]:
        """Return the required ownership cache; schema errors fail closed."""
        source = self._conn if conn is None else conn
        return own_only_basis_by_dialog(source)

    def _list_scheduled_messages_from_db(self, req: _ListMessagesDbRequest) -> dict:
        """Read pending scheduled messages from the separate local mirror.

        Scheduled messages deliberately do not use ``messages`` or any of its
        derived tables.  This path is local-only: it never falls back to a
        Telegram request when the mirror is empty or unavailable.
        When ``own_only_dialogs`` exists, its ownership cache is authoritative.
        """
        with timing_phase("local_projection"):
            own_basis: dict[int, tuple[str, ...]] = {}
            if scheduled_messages_available(self._conn):
                existing_basis = self._own_only_basis_by_dialog()
                if req.dialog_id not in existing_basis:
                    raw_rows: list[object] = []
                else:
                    own_basis = existing_basis
                    anchor_sent_at = req.anchor_sent_at
                    if req.anchor_msg_id is not None and anchor_sent_at is None:
                        anchor_sent_at = scheduled_message_time(self._conn, req.dialog_id, req.anchor_msg_id)
                    sql, params = build_scheduled_list_query(
                        req,
                        scheduled_now=int(time.time()),
                        anchor_sent_at=anchor_sent_at,
                    )
                    raw_rows = _fetchall_rows(self._conn.execute(sql, params))
            else:
                raw_rows = []

        with timing_phase("response_shape"):
            identity_ids = {req.dialog_id}
            identity_ids.update(_object_to_int(_row_value(raw_row, "dialog_id")) for raw_row in raw_rows)
            identities = read_dialog_identities(self._conn, identity_ids)
            rows = [
                {
                    **scheduled_row_to_wire(
                        cast(Mapping[str, object], raw_row),
                        inclusion_basis=own_basis.get(_object_to_int(_row_value(raw_row, "dialog_id")), ()),
                    ),
                    "dialog_name": identities[_object_to_int(_row_value(raw_row, "dialog_id"))].display_name,
                    "dialog_name_source": identities[
                        _object_to_int(_row_value(raw_row, "dialog_id"))
                    ].display_name_source,
                }
                for raw_row in raw_rows
            ]
            next_nav = self._maybe_encode_next_nav(
                _NextNavContext(
                    messages=rows,
                    limit=req.limit,
                    dialog_id=req.dialog_id,
                    direction=req.direction,
                    direction_enum=req.direction_enum,
                    topic_id=req.topic_id,
                    logger=self._logger,
                    request_id=self._deps.rid,
                    message_state="scheduled",
                    since_utc=req.since_utc,
                    until_utc=req.until_utc,
                )
            )
            return {
                "ok": True,
                "data": {
                    "messages": rows,
                    "source": "scheduled_messages",
                    "next_navigation": next_nav,
                    "message_state": "scheduled",
                    "scope": "own_only",
                    "dialog_name": identities[req.dialog_id].display_name,
                    "dialog_name_source": identities[req.dialog_id].display_name_source,
                },
            }

    def _list_draft_messages_from_db(self, req: _ListMessagesDbRequest, *, navigation: str | None = None) -> dict:
        """Read mutable author-only composition from the dedicated local projection.

        This path has no gateway dependency.  Empty local rows and unavailable
        receipts remain visible through ``draft_coverage`` instead of being
        misrepresented as an empty sent-history page.
        """
        with timing_phase("local_projection"):
            records, coverage = read_drafts(
                self._conn,
                account_id=self._deps.self_id,
                dialog_id=req.dialog_id,
                topic_id=req.topic_id,
                sender_id=req.sender_id,
                sender_name=req.sender_name,
                since_utc=req.since_utc,
                until_utc=req.until_utc,
            )
        visible = self._visible_drafts(records, req.direction)
        start = self._draft_cursor_start(navigation, req, coverage.projection_fingerprint, visible)
        if isinstance(start, dict):
            return start
        candidates = visible[start : start + req.limit]
        with timing_phase("response_shape"):
            page, budget_truncated = self._bounded_draft_page(candidates)
            rows = [self._draft_wire_row(record) for record in page]
        next_navigation = self._draft_next_navigation(req, coverage.projection_fingerprint, visible, page, start)
        return {
            "ok": True,
            "data": {
                "messages": rows,
                "source": "draft_current",
                "next_navigation": next_navigation,
                "message_state": "draft",
                "scope": "own_only",
                "draft_coverage": coverage.to_wire(),
                "draft_fingerprint": coverage.projection_fingerprint,
                "truncation": {
                    "is_truncated": budget_truncated,
                    "shown_count": len(page),
                    "hidden_count": len(visible) - start - len(page) if budget_truncated else 0,
                    "reason": "response_budget" if budget_truncated else None,
                },
            },
        }

    @staticmethod
    def _visible_drafts(records: list[DraftReadRecord], direction: str) -> list[DraftReadRecord]:
        visible = [record for record in records if record.state == "present"]
        visible.sort(
            key=lambda record: (record.observation_completed_at, draft_message_key(record)),
            reverse=direction != "oldest",
        )
        return visible

    @staticmethod
    def _draft_cursor_start(
        navigation: str | None,
        req: _ListMessagesDbRequest,
        fingerprint: str | None,
        visible: list[DraftReadRecord],
    ) -> int | dict:
        if navigation in (None, "newest", "oldest"):
            return 0
        try:
            cursor = decode_navigation_token(navigation)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_navigation", "message": str(exc)}
        error = ReadingService._draft_cursor_error(cursor, req, fingerprint)
        if error is not None:
            return error
        if cursor.message_state == "all" and cursor.draft_key is None:
            return 0
        assert cursor.draft_key is not None
        positions = {draft_message_key(record): index for index, record in enumerate(visible)}
        if cursor.draft_key not in positions:
            return ReadingService._draft_projection_changed("Draft scope changed since this page.")
        return positions[cursor.draft_key] + 1

    @staticmethod
    def _draft_cursor_error(
        cursor: NavigationToken, req: _ListMessagesDbRequest, fingerprint: str | None
    ) -> dict | None:
        if cursor.kind != "history" or cursor.message_state not in {"draft", "all"}:
            return {"ok": False, "error": "invalid_navigation", "message": "Navigation token is not a draft cursor."}
        if cursor.dialog_id != req.dialog_id or cursor.topic_id != req.topic_id:
            return {
                "ok": False,
                "error": "invalid_navigation",
                "message": "Navigation token belongs to a different draft scope.",
            }
        if cursor.since_utc != req.since_utc or cursor.until_utc != req.until_utc:
            return {
                "ok": False,
                "error": "invalid_navigation",
                "message": "Navigation token belongs to a different time range.",
            }
        # all+unread never reaches draft validation: the history token binds
        # unread before this path, and that mode selects published rows only.
        if cursor.draft_fingerprint != fingerprint:
            return ReadingService._draft_projection_changed("Draft composition changed since this page.")
        if cursor.message_state == "draft" and cursor.draft_key is None:
            return {"ok": False, "error": "invalid_navigation", "message": "Draft cursor is missing its scope key."}
        return None

    @staticmethod
    def _draft_projection_changed(detail: str) -> dict:
        return {
            "ok": False,
            "error": "draft_projection_changed",
            "message": f"{detail} Action: restart the draft read from the beginning.",
        }

    @staticmethod
    def _draft_next_navigation(
        req: _ListMessagesDbRequest,
        fingerprint: str | None,
        visible: list[DraftReadRecord],
        page: list[DraftReadRecord],
        start: int,
    ) -> str | None:
        if len(visible) <= start + len(page) or not page:
            return None
        return encode_history_navigation(
            None,
            req.dialog_id,
            topic_id=req.topic_id,
            direction=req.direction_enum,
            message_state="draft",
            since_utc=req.since_utc,
            until_utc=req.until_utc,
            draft_key=draft_message_key(page[-1]),
            draft_fingerprint=fingerprint,
        )

    def _bounded_draft_page(self, candidates: list[DraftReadRecord]) -> tuple[list[DraftReadRecord], bool]:
        """Keep complete draft rows within the structured response byte budget."""
        page: list[DraftReadRecord] = []
        payload_bytes = 0
        for record in candidates:
            row_bytes = len(
                json.dumps(ReadingService._draft_wire_row(record), ensure_ascii=False, separators=(",", ":")).encode()
            )
            if page and payload_bytes + row_bytes > self._deps.draft_response_budget_bytes:
                return page, True
            page.append(record)
            payload_bytes += row_bytes
        return page, False

    @staticmethod
    def _draft_wire_row(record: DraftReadRecord) -> dict[str, object]:
        return {
            "message_state": "draft",
            "message_key": draft_message_key(record),
            "dialog_id": record.dialog_id,
            "draft_scope": {
                "dialog_id": record.dialog_id,
                "topic_id": record.topic_id,
                "subdialog_peer_id": record.subdialog_peer_id,
            },
            "draft_status": record.state,
            "text": record.text,
            "entities": list(record.entities),
            "reply_to": record.reply_to,
            "media": record.media,
            "suggested_post": record.suggested_post,
            "rich_message": record.rich_message,
            "effect_id": record.effect_id,
            "no_webpage": record.no_webpage,
            "invert_media": record.invert_media,
            "composition_complete": record.composition_complete,
            "observation_source": record.source_kind,
            "observed_at": record.source_observed_at,
            "draft_updated_at": record.observation_completed_at,
            "projection_revision": record.projection_revision,
            "normalization_version": record.normalization_version,
            "visibility": "author_only",
            "unpublished": True,
            "published": False,
            "unseen": True,
        }

    async def _list_messages_local_state_result(  # noqa: PLR0913, PLR0917
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        direction: str,
        status: str | None,
        anchor_msg_id: int | None = None,
        anchor_sent_at: int | None = None,
        all_navigation: NavigationToken | None = None,
    ) -> dict:
        db_request = self._local_state_db_request(dialog_id, request, direction, anchor_msg_id, anchor_sent_at)
        if request.message_state == "draft":
            return self._list_draft_messages_from_db(db_request, navigation=request.navigation)
        if request.message_state == "scheduled":
            return self._scheduled_local_state_result(dialog_id, self._list_scheduled_messages_from_db(db_request))
        all_request = _AllLocalStateRequest(
            dialog_id,
            request,
            direction,
            status,
            db_request,
            all_navigation,
        )
        return await self._all_local_state_result(all_request)

    def _local_state_db_request(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        direction: str,
        anchor_msg_id: int | None,
        anchor_sent_at: int | None,
    ) -> _ListMessagesDbRequest:
        return _ListMessagesDbRequest(
            dialog_id=dialog_id,
            limit=request.limit + 1 if request.message_state == "all" else request.limit,
            self_id=self._deps.self_id,
            direction=direction,
            direction_enum=HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST,
            anchor_msg_id=anchor_msg_id,
            anchor_sent_at=anchor_sent_at,
            sender_id=request.sender_id,
            sender_name=request.sender_name,
            topic_id=request.topic_id,
            unread_after_id=request.unread_after_id if request.message_state == "all" else None,
            since_utc=request.since_utc,
            until_utc=request.until_utc,
        )

    def _scheduled_local_state_result(self, dialog_id: int, scheduled_result: dict) -> dict:
        with timing_phase("local_projection"):
            identity = read_dialog_identities(self._conn, [dialog_id])[dialog_id]
            dialog_type = identity.dialog_type.value
        with timing_phase("response_shape"):
            scheduled_result["data"]["dialog_type"] = dialog_type
            scheduled_result["data"]["dialog_name"] = identity.display_name
            scheduled_result["data"]["dialog_name_source"] = identity.display_name_source
            scheduled_result["data"]["read_state"] = None
        return scheduled_result

    async def _all_local_state_result(self, all_request: _AllLocalStateRequest) -> dict:
        navigation = all_request.navigation
        sent_request = dataclasses.replace(
            all_request.db_request,
            anchor_msg_id=navigation.value if navigation is not None else None,
            anchor_sent_at=navigation.sent_at if navigation is not None else None,
        )
        sent_rows = await self._all_sent_rows(all_request.status, sent_request)
        # Telegram read cursors only describe published incoming history.
        # Scheduled and draft projections are author-only state and cannot be
        # truthfully classified as unread, so do not query or validate either
        # mutable projection for this sent-only page.
        if all_request.request.unread:
            state = _AllLocalState(
                all_request,
                sent_rows,
                [],
                [],
                None,
                self._all_local_metadata(all_request.dialog_id, all_request.status),
            )
            return self._all_local_response(state)

        scheduled_request = dataclasses.replace(
            all_request.db_request,
            anchor_msg_id=navigation.scheduled_message_id if navigation is not None else None,
            anchor_sent_at=navigation.scheduled_sent_at if navigation is not None else None,
            unread_after_id=None,
        )
        draft_result = self._list_draft_messages_from_db(
            all_request.db_request, navigation=all_request.request.navigation
        )
        if not draft_result.get("ok"):
            return draft_result
        scheduled_rows = self._list_scheduled_messages_from_db(scheduled_request)["data"]["messages"]
        draft_rows = draft_result["data"]["messages"]
        state = _AllLocalState(
            all_request,
            sent_rows,
            scheduled_rows,
            draft_rows,
            draft_result,
            self._all_local_metadata(all_request.dialog_id, all_request.status),
        )
        return self._all_local_response(state)

    async def _all_sent_rows(self, status: str | None, db_request: _ListMessagesDbRequest) -> list[dict]:
        if status not in {"synced", "syncing", "access_lost"}:
            return []
        sent_result = await self._list_messages_from_db(db_request)
        return [{**row, "message_state": "sent"} for row in sent_result["data"]["messages"]]

    def _all_local_metadata(
        self, dialog_id: int, status: str | None
    ) -> tuple[str, ReadState | None, dict[str, object]]:
        with timing_phase("local_projection"):
            identity = read_dialog_identities(self._conn, [dialog_id])[dialog_id]
            dialog_type = identity.dialog_type.value
            read_state = _read_state_for_dialog(self._conn, dialog_id, dialog_type)
            access_metadata = _build_access_metadata(self._conn, dialog_id, status or "not_synced")
            access_metadata["dialog_name"] = identity.display_name
            access_metadata["dialog_name_source"] = identity.display_name_source
            if status not in {"synced", "syncing", "access_lost"}:
                access_metadata.update(
                    {
                        "dialog_access": "local_only",
                        "coverage": "local_only",
                        "required_action": (
                            "Mark the dialog for sync to make a complete local history available, or retry with "
                            'message_state="sent" for an on-demand live page.'
                        ),
                    }
                )
        return dialog_type, read_state, access_metadata

    def _all_local_response(self, state: _AllLocalState) -> dict:
        dialog_type, read_state, access_metadata = state.metadata
        request = state.request.request
        with timing_phase("response_shape"):
            combined = self._combined_local_rows(
                state.sent_rows, state.scheduled_rows, state.draft_rows, state.request.direction
            )
            draft_result = state.draft_result
            has_more = len(combined) > request.limit or (
                draft_result is not None and draft_result["data"]["next_navigation"] is not None
            )
            page = combined[: request.limit]
            next_nav = self._all_next_navigation(
                page,
                has_more,
                state.request,
                draft_result["data"]["draft_fingerprint"] if draft_result is not None else None,
            )
            topic_metadata: dict[str, object] = {}
            if request.topic_id is not None:
                with timing_phase("local_projection"):
                    receipt = _topic_attribution_receipt(self._conn, state.request.dialog_id)
                topic_metadata = {
                    "topic_attribution": receipt,
                    "selection_state": _topic_selection_state(
                        topic_id=request.topic_id,
                        messages=page,
                        status=state.request.status,
                        receipt=receipt,
                    ),
                }
            projection_metadata: dict[str, object] = {}
            if draft_result is not None:
                projection_metadata = {
                    "draft_coverage": draft_result["data"]["draft_coverage"],
                    "truncation": draft_result["data"]["truncation"],
                }
            return {
                "ok": True,
                "data": {
                    "messages": page,
                    "source": "sync_db" if request.unread else "sync_db+scheduled_messages+draft_current",
                    "next_navigation": next_nav,
                    "message_state": "all",
                    "dialog_type": dialog_type,
                    "read_state": read_state,
                    **projection_metadata,
                    **topic_metadata,
                    **access_metadata,
                },
            }

    @staticmethod
    def _combined_local_rows(
        sent_rows: list[dict], scheduled_rows: list[dict], draft_rows: list[dict], direction: str
    ) -> list[dict]:
        combined = [*sent_rows, *scheduled_rows, *draft_rows]
        combined.sort(
            key=ReadingService._all_row_sort_key,
            reverse=direction != "oldest",
        )
        return combined

    @staticmethod
    def _all_row_sort_key(row: dict) -> tuple[int, int, int | str, int]:
        state = str(row.get("message_state"))
        state_order = {"sent": 0, "scheduled": 1, "draft": 2}
        if state == "draft":
            return (
                int(row.get("sent_at") or row.get("draft_updated_at") or 0),
                1,
                str(row.get("message_key") or ""),
                state_order["draft"],
            )
        return (
            int(row.get("sent_at") or row.get("draft_updated_at") or 0),
            0,
            _object_to_int(row.get("message_id")),
            state_order.get(state, 3),
        )

    @staticmethod
    def _all_next_navigation(
        page: list[dict],
        has_more: bool,
        all_request: _AllLocalStateRequest,
        draft_fingerprint: str | None,
    ) -> str | None:
        if not has_more or not page:
            return None
        position = _AllNavigationPosition.from_navigation(all_request.navigation)
        for row in page:
            position.advance(row)
        return encode_history_navigation(
            position.sent_message_id,
            all_request.dialog_id,
            topic_id=all_request.request.topic_id,
            direction=(HistoryDirection.OLDEST if all_request.direction == "oldest" else HistoryDirection.NEWEST),
            sent_at=position.sent_at,
            message_state="all",
            unread=all_request.request.unread,
            since_utc=all_request.request.since_utc,
            until_utc=all_request.request.until_utc,
            draft_key=position.draft_key,
            draft_fingerprint=draft_fingerprint,
            scheduled_message_id=position.scheduled_message_id,
            scheduled_sent_at=position.scheduled_sent_at,
        )

    @staticmethod
    def _history_navigation_sent_at(navigation: str | None) -> int | None:
        if navigation in (None, "newest", "oldest"):
            return None
        return decode_navigation_token(navigation).sent_at

    def _local_history_anchor_sent_at(
        self,
        dialog_id: int,
        anchor_msg_id: int | None,
        navigation: str | None,
    ) -> int | None:
        sent_at = self._history_navigation_sent_at(navigation)
        if sent_at is not None or anchor_msg_id is None:
            return sent_at
        return message_sent_at(self._conn, dialog_id, anchor_msg_id)

    async def _list_messages_non_sent(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        direction: str,
    ) -> dict:
        _set_timing_route("local_non_sent_state")
        with timing_phase("local_projection"):
            row = _fetchone_row(self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)))
        status = _status_from_row(row)
        if request.context_message_id is not None:
            return {
                "ok": False,
                "error": f"{request.message_state}_context_unsupported",
                "message": (
                    f'message_state="{request.message_state}" does not support sent-history context windows. '
                    'Action: retry with message_state="sent".'
                ),
            }
        nav_result = self._decode_history_navigation(
            request.navigation,
            _HistoryNavigationContext(
                dialog_id=dialog_id,
                direction=direction,
                message_state=request.message_state,
                topic_id=request.topic_id,
                unread=request.unread,
                since_utc=request.since_utc,
                until_utc=request.until_utc,
            ),
        )
        if isinstance(nav_result, dict):
            return nav_result
        anchor_msg_id, direction = nav_result
        anchor_sent_at = None
        all_navigation: NavigationToken | None = None
        if request.message_state == "all" and request.navigation not in (None, "newest", "oldest"):
            navigation = request.navigation
            assert navigation is not None
            try:
                all_navigation = decode_navigation_token(navigation)
                anchor_sent_at = all_navigation.sent_at
            except ValueError as exc:
                return {"ok": False, "error": "invalid_navigation", "message": str(exc)}
        if request.message_state == "all" and request.unread:
            unread_position = await self._resolve_unread_position(dialog_id, request.unread_after_id)
            if isinstance(unread_position, _ReadPositionPending):
                return unread_position.response()
            request = dataclasses.replace(request, unread_after_id=unread_position.value)
        return await self._list_messages_local_state_result(
            dialog_id,
            request,
            direction,
            status,
            anchor_msg_id,
            anchor_sent_at,
            all_navigation,
        )

    async def _list_messages_for_state(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        direction: str,
    ) -> dict:
        if request.message_state not in {"sent", "scheduled", "draft", "all"}:
            return {
                "ok": False,
                "error": "invalid_message_state",
                "message": "message_state must be sent, scheduled, draft, or all",
            }
        if request.unread and request.message_state in {"scheduled", "draft"}:
            return {
                "ok": False,
                "error": "unread_state_unsupported",
                "message": (
                    f'message_state="{request.message_state}" has no unread semantics. '
                    'Action: retry with message_state="sent" or message_state="all".'
                ),
            }
        if request.message_state != "sent":
            return await self._list_messages_non_sent(dialog_id, request, direction)

        if request.context_message_id is not None:
            return await self._list_messages_context_result(dialog_id, request)
        return await self._list_messages_history_result(dialog_id, request, direction)

    async def _list_messages(self, req: dict) -> dict:
        """Return messages from sync.db (if synced) or Telegram (on-demand)."""
        try:
            selector = required_dialog_selector(
                exact_id=req.get("dialog_id"),
                dialog=req.get("dialog"),
            )
            request = self._parse_list_messages_request(req)
        except DialogSelectorError as exc:
            return _selector_error_response(exc)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_time_range", "message": str(exc)}
        direction = request.direction
        if direction not in ("newest", "oldest"):
            direction = "newest"

        resolver = self._deps.resolve_dialog_id
        if request.message_state in {"draft", "all"} and self._deps.resolve_dialog_id_local is not None:
            resolver = self._deps.resolve_dialog_id_local
        with timing_phase("resolution"):
            resolved = await resolver(selector)
        if isinstance(resolved, dict):
            return resolved
        dialog_id = resolved
        if not dialog_id:
            return {
                "ok": False,
                "error": "missing_dialog",
                "message": "Either dialog_id or dialog name is required",
            }
        result = await self._list_messages_for_state(dialog_id, request, direction)
        with timing_phase("response_shape"):
            return self._attach_directory_coverage(result, getattr(resolved, "coverage", None))

    async def _search_messages_scoped_for_state(
        self,
        request: _SearchMessagesRequest,
        stemmed: str,
        selector: DialogSelector,
    ) -> dict:
        resolved = await self._deps.resolve_dialog_id(selector)
        if isinstance(resolved, dict):
            return resolved
        directory_coverage = getattr(resolved, "coverage", None)
        request = dataclasses.replace(request, dialog_id=resolved)
        navigation_result = self._bind_search_navigation(request, resolved)
        if isinstance(navigation_result, dict):
            return navigation_result
        request = navigation_result
        if request.message_state == "scheduled":
            return self._attach_directory_coverage(self._search_scheduled_messages(request), directory_coverage)
        if request.message_state == "all":
            sent_result = await self._search_messages_scoped_result(
                dataclasses.replace(request, message_state="sent", offset=0, limit=request.offset + request.limit),
                stemmed,
            )
            return self._attach_directory_coverage(
                self._merge_search_results(
                    sent_result,
                    self._search_scheduled_messages(
                        dataclasses.replace(request, offset=0, limit=request.offset + request.limit)
                    ),
                    request,
                ),
                directory_coverage,
            )
        return self._attach_directory_coverage(
            await self._search_messages_scoped_result(request, stemmed), directory_coverage
        )

    @staticmethod
    def _attach_directory_coverage(
        result: dict,
        coverage: DialogDirectoryCoverage | None,
    ) -> dict:
        """Carry selector coverage alongside successful local read results."""
        if coverage is None or not result.get("ok"):
            return result
        data = result.get("data")
        if isinstance(data, dict):
            data["directory_coverage"] = coverage.to_wire()
        else:
            result["directory_coverage"] = coverage.to_wire()
        return result

    async def _search_messages_for_state(
        self,
        request: _SearchMessagesRequest,
        stemmed: str,
        selector: DialogSelector | None,
    ) -> dict:
        if request.message_state not in {"sent", "scheduled", "all"}:
            return {
                "ok": False,
                "error": "invalid_message_state",
                "message": "message_state must be sent, scheduled, or all",
            }
        global_mode = selector is None
        if global_mode:
            navigation_result = self._bind_search_navigation(request, 0)
            if isinstance(navigation_result, dict):
                return navigation_result
            request = navigation_result
        if global_mode and request.message_state == "scheduled":
            return self._search_scheduled_messages(request)
        if global_mode and request.message_state == "all":
            return self._merge_search_results(
                await self._search_messages_global_result(
                    dataclasses.replace(request, message_state="sent", offset=0, limit=request.offset + request.limit),
                    stemmed,
                ),
                self._search_scheduled_messages(
                    dataclasses.replace(request, offset=0, limit=request.offset + request.limit)
                ),
                request,
            )
        if not global_mode:
            assert selector is not None
            return await self._search_messages_scoped_for_state(request, stemmed, selector)
        return await self._search_messages_global_result(request, stemmed)

    @staticmethod
    def _bind_search_navigation(
        request: _SearchMessagesRequest,
        dialog_id: int,
    ) -> _SearchMessagesRequest | dict:
        """Validate a search cursor after its dialog scope has been resolved."""
        if request.navigation is None:
            return request
        try:
            navigation = decode_navigation_token(request.navigation)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_navigation", "message": str(exc)}

        error_message = ReadingService._search_navigation_context_error(navigation, request, dialog_id)
        if error_message is not None:
            return {"ok": False, "error": "invalid_navigation", "message": error_message}
        offset = navigation.value
        if offset is None:
            return {
                "ok": False,
                "error": "invalid_navigation",
                "message": "Search navigation token is missing its offset.",
            }
        return dataclasses.replace(request, offset=offset)

    @staticmethod
    def _search_navigation_context_error(
        navigation: NavigationToken,
        request: _SearchMessagesRequest,
        dialog_id: int,
    ) -> str | None:
        if navigation.kind != "search":
            return f"Navigation token is for {navigation.kind}, not search"
        if navigation.query != request.query:
            return "Navigation token belongs to a different search query"
        if navigation.message_state != request.message_state:
            return (
                f"Navigation token belongs to message_state {navigation.message_state!r}, not {request.message_state!r}"
            )
        if navigation.dialog_id != dialog_id:
            return f"Navigation token belongs to dialog {navigation.dialog_id}, not {dialog_id}"
        if navigation.since_utc != request.since_utc or navigation.until_utc != request.until_utc:
            return "Navigation token belongs to a different time range"
        return None

    async def _search_messages(self, req: dict) -> dict:
        """FTS5 stemmed full-text search against messages_fts."""
        try:
            selector = optional_dialog_selector(
                exact_id=req.get("dialog_id"),
                dialog=req.get("dialog"),
            )
            request = self._parse_search_messages_request(req)
        except DialogSelectorError as exc:
            return _selector_error_response(exc)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_time_range", "message": str(exc)}
        stemmed = stem_query(request.query)
        if not stemmed:
            return {
                "ok": False,
                "error": "invalid_query",
                "message": (
                    "query must contain at least one Cyrillic or Latin letter or ASCII digit. "
                    "Action: pass a searchable query, or use list_messages."
                ),
            }
        result = await self._search_messages_for_state(request, stemmed, selector)
        if selector is None:
            result = self._attach_directory_coverage(result, read_dialog_directory_coverage(self._conn))
        return result

    async def list_unread_messages(self, req: dict[str, object]) -> dict:
        """Return prioritized unread messages across dialogs from sync.db."""
        if "scope" in req:
            return {
                "ok": False,
                "error": "invalid_input",
                "message": "scope is not supported by get_inbox; use get_unread_summary for an overview",
            }
        limit = _clamp(_coerce_int(req.get("limit", 100), 100), 1, 500)
        group_size_threshold = _coerce_int(req.get("group_size_threshold", 100), 100)
        try:
            include_dialog_types = self._parse_dialog_type_allowlist(req.get("include_dialog_types"))
        except ValueError as exc:
            return {"ok": False, "error": "invalid_input", "message": str(exc)}
        try:
            since_utc = _parse_request_boundary(req, "since_utc")
        except ValueError as exc:
            return {"ok": False, "error": "invalid_input", "message": str(exc)}
        entries, counts = self._collect_unread_dialogs(group_size_threshold, since_utc, include_dialog_types)
        self._rank_unread_entries(entries)
        groups = await self._fetch_unread_groups(
            entries,
            allocate_message_budget_proportional(counts, limit),
            since_utc,
        )
        pending_row = cast(tuple[object] | None, self._conn.execute(_COUNT_READ_POSITION_PENDING_SQL).fetchone())
        pending_count = int(cast(int | str, pending_row[0])) if pending_row else 0
        pending_rows = cast(
            list[tuple[object]],
            self._conn.execute(_READ_POSITION_PENDING_IDENTITIES_SQL).fetchall(),
        )
        pending_ids = [int(cast(int | str, row[0])) for row in pending_rows]
        pending_identities = read_dialog_identities(self._conn, pending_ids)
        pending_entities = [
            {
                "dialog_id": dialog_id,
                "display_name": pending_identities[dialog_id].display_name,
                "username": pending_identities[dialog_id].username,
                "display_name_source": pending_identities[dialog_id].display_name_source,
            }
            for dialog_id in pending_ids
        ]
        return {
            "ok": True,
            "data": {
                "groups": groups,
                "read_position_pending_count": pending_count,
                "read_position_pending_entities": pending_entities,
            },
        }

    @staticmethod
    def _parse_dialog_type_allowlist(raw: object) -> tuple[DialogType, ...] | None:
        if raw is None:
            return None
        if not isinstance(raw, list) or not raw:
            raise ValueError("include_dialog_types must be a non-empty list of canonical dialog types")
        parsed: list[DialogType] = []
        for value in raw:
            if isinstance(value, DialogType):
                dialog_type = value
            elif isinstance(value, str):
                try:
                    dialog_type = DialogType(value)
                except ValueError as exc:
                    raise ValueError(
                        f"include_dialog_types contains unsupported dialog type {value!r}; "
                        f"expected one of {[item.value for item in DialogType]}"
                    ) from exc
            else:
                raise ValueError("include_dialog_types must contain only canonical dialog type strings")
            if dialog_type not in parsed:
                parsed.append(dialog_type)
        return tuple(parsed)

    @staticmethod
    def _should_include_unread_dialog(
        category: str,
        participants_count: int | None,
        group_size_threshold: int,
        *,
        allow_channel: bool = False,
    ) -> bool:
        dialog_type = DialogType.parse(category)
        if dialog_type == DialogType.CHANNEL and not allow_channel:
            return False
        return not (
            dialog_type in (DialogType.SUPERGROUP, DialogType.GROUP, DialogType.FORUM)
            and participants_count is not None
            and participants_count > group_size_threshold
        )

    def _collect_unread_dialogs(
        self,
        group_size_threshold: int,
        since_utc: int | None = None,
        include_dialog_types: tuple[DialogType, ...] | None = None,
    ) -> tuple[list[dict], dict[int, int]]:
        rows = cast(
            list[tuple[object, object, object, object, object]],
            self._conn.execute(
                _COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL,
                {
                    "since_utc": since_utc,
                    "deleted_since_utc": int(time.time()) - self._deps.deleted_message_visibility_seconds,
                },
            ).fetchall(),
        )
        identities = read_dialog_identities(self._conn, [int(cast(int | str, row[0])) for row in rows])
        entries: list[dict] = []
        counts: dict[int, int] = {}
        for row in rows:
            (
                dialog_id,
                read_max,
                last_event_at,
                participants_count,
                unread_count,
            ) = row
            dialog_id_i = int(cast(int | str, dialog_id))
            identity = identities[dialog_id_i]
            unread_count_i = int(cast(int | str, unread_count))
            if unread_count_i == 0:
                continue
            category = identity.dialog_type
            if include_dialog_types is not None and category not in include_dialog_types:
                continue
            if not self._should_include_unread_dialog(
                category,
                cast(int | None, participants_count),
                group_size_threshold,
                allow_channel=include_dialog_types is not None and DialogType.CHANNEL in include_dialog_types,
            ):
                continue
            entries.append(
                {
                    "chat_id": dialog_id_i,
                    "display_name": identity.display_name,
                    "username": identity.username,
                    "display_name_source": identity.display_name_source,
                    "dialog_type": category.value,
                    "unread_count": unread_count_i,
                    "unread_mentions_count": 0,
                    "category": category,
                    "date": last_event_at,
                    "read_inbox_max_id": read_max,
                }
            )
            counts[dialog_id_i] = unread_count_i
        return entries, counts

    @staticmethod
    def _rank_unread_entries(entries: list[dict]) -> None:
        for entry in entries:
            entry["tier"] = unread_chat_tier(
                {"unread_mentions_count": entry["unread_mentions_count"], "category": entry["category"]}
            )
        entries.sort(key=lambda entry: (entry["tier"], -(entry["date"] or 0)))

    async def _fetch_unread_groups(
        self, entries: list[dict], allocation: dict[int, int], since_utc: int | None = None
    ) -> list[dict]:
        groups: list[dict] = []
        for entry in entries:
            chat_id = int(cast(int | str, entry["chat_id"]))
            budget = allocation.get(chat_id, 0)
            dialog_type = cast(str, entry["dialog_type"])
            group: dict = {
                "dialog_id": chat_id,
                "display_name": entry["display_name"],
                "username": entry["username"],
                "display_name_source": entry["display_name_source"],
                "tier": entry["tier"],
                "category": entry["category"],
                "unread_count": entry["unread_count"],
                "unread_mentions_count": entry["unread_mentions_count"],
                "dialog_type": dialog_type,
                "read_state": _read_state_for_dialog(self._conn, chat_id, dialog_type),
                "messages": [],
            }
            if budget:
                rows = cast(
                    list[Mapping[str, object]],
                    self._conn.execute(
                        _FETCH_UNREAD_MESSAGES_SQL,
                        {
                            "dialog_id": chat_id,
                            "after_msg_id": entry["read_inbox_max_id"],
                            "limit": budget,
                            "self_id": self._deps.self_id,
                            "since_utc": since_utc,
                            "deleted_since_utc": int(time.time()) - self._deps.deleted_message_visibility_seconds,
                        },
                    ).fetchall(),
                )
                messages, freshness = await self._enrich_unread_rows(chat_id, rows)
                group["messages"] = [dataclasses.asdict(message) for message in messages]
                if freshness is not None:
                    group["reaction_freshness"] = freshness.as_dict()
            groups.append(group)
        return groups

    async def _enrich_unread_rows(
        self, dialog_id: int, rows: list[Mapping[str, object]]
    ) -> tuple[list[ReadMessage], ReactionFreshness | None]:
        messages = [read_message_from_row(row) for row in rows]
        if not messages:
            return messages, None
        enriched = project_cached_message_facts(self._conn, dialog_id, messages)
        return enriched, cached_reaction_freshness(len(enriched))

    # Public facade methods keep daemon_api free of reading request types and internals.
    async def list_messages(self, req: dict[str, object]) -> dict:
        return await self._list_messages(req)

    async def search_messages(self, req: dict[str, object]) -> dict:
        return await self._search_messages(req)

    async def list_dialogs(self, req: dict[str, object]) -> dict:
        return await self._list_dialogs(req)

    async def get_unread_summary(self, req: dict[str, object]) -> dict:
        return await self._get_unread_summary(req)

    async def list_messages_context_window(self, *, dialog_id: int, anchor_message_id: int, context_size: int) -> dict:
        return await self._list_messages_context_window(
            dialog_id=dialog_id, anchor_message_id=anchor_message_id, context_size=context_size
        )

    async def list_messages_from_telegram(self, req: object) -> dict:
        return await self._list_messages_from_telegram(cast(_ListMessagesTelegramRequest, req))

    async def list_messages_from_db(self, req: object) -> dict:
        return await self._list_messages_from_db(cast(_ListMessagesDbRequest, req))

    async def resolve_unread_position(self, dialog_id: int, unread_after_id: int | None) -> int | dict[str, str | bool]:
        """Expose the legacy daemon response shape for read-position lookup."""
        result = await self._resolve_unread_position(dialog_id, unread_after_id)
        if isinstance(result, _ReadPositionPending):
            return result.response()
        return result.value

    async def _list_dialogs(self, req: dict) -> dict:
        """Return dialog list from the local dialogs snapshot.

        Production file-backed databases use a dedicated read-only connection in
        a worker thread. This keeps the combined ``mcp-telegram serve`` event
        loop responsive while the query performs SQLite aggregation. In-memory
        tests keep the direct connection path because there is no file to reopen.
        """
        db_path = self._deps.sync_db_path
        if db_path is not None:
            started = time.monotonic()
            try:
                return await asyncio.to_thread(self._list_dialogs_from_reader, db_path, req)
            finally:
                elapsed_ms = (time.monotonic() - started) * 1000
                self._logger.debug("list_dialogs_sql_reader completed in %.3fms%s", elapsed_ms, self._deps.rid())
        return self._list_dialogs_sync(self._conn, req)

    async def _get_unread_summary(self, req: dict) -> dict:
        """Return the bounded unread overview from the Dialog projection.

        This path deliberately does not inspect messages, read cursors, or
        Telegram. The persisted ``dialogs`` row is the source of truth for
        this overview, including unsynced and own-only rows; the lifecycle
        join only excludes access-lost rows.
        """
        db_path = self._deps.sync_db_path
        if db_path is not None:
            started = time.monotonic()
            try:
                return await asyncio.to_thread(self._get_unread_summary_from_reader, db_path, req)
            finally:
                elapsed_ms = (time.monotonic() - started) * 1000
                self._logger.info("get_unread_summary_sql_reader completed in %.3fms%s", elapsed_ms, self._deps.rid())
        return self._get_unread_summary_sync(self._conn, req)

    def _get_unread_summary_from_reader(self, db_path: Path, req: dict) -> dict:
        conn = open_sync_db_reader(db_path)
        try:
            conn.row_factory = sqlite3.Row
            return self._get_unread_summary_sync(conn, req)
        finally:
            conn.close()

    @staticmethod
    def _get_unread_summary_sync(conn: sqlite3.Connection, req: dict) -> dict:
        raw_limit = req.get("limit", 50)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            return {"ok": False, "error": "invalid_input", "message": "limit must be an integer"}
        limit = _clamp(raw_limit, 1, 200)
        began_transaction = not conn.in_transaction
        if began_transaction:
            conn.execute("BEGIN")
        try:
            rows = _fetchall_rows(conn.execute(_UNREAD_SUMMARY_SQL, {"limit": limit}))
            total_matching = _object_to_int(_row_value(rows[0], "total_matching")) if rows else 0
            identities = read_dialog_identities(conn, [_object_to_int(_row_value(row, "dialog_id")) for row in rows])

            dialogs: list[dict[str, object]] = []
            for row in rows:
                dialog_id = _object_to_int(_row_value(row, "dialog_id"))
                identity = identities[dialog_id]
                unread_mark_raw = _row_value(row, "unread_mark")
                dialogs.append(
                    {
                        "dialog_id": dialog_id,
                        "name": identity.display_name,
                        "username": identity.username,
                        "dialog_type": identity.dialog_type.value,
                        "display_name_source": identity.display_name_source,
                        "unread_count": _object_to_int_or_none(_row_value(row, "unread_count")),
                        "unread_mark": (None if unread_mark_raw is None else bool(_object_to_int(unread_mark_raw, 0))),
                        "unread_mentions_count": _object_to_int(_row_value(row, "unread_mentions_count"), 0),
                        "unread_reactions_count": _object_to_int(_row_value(row, "unread_reactions_count"), 0),
                        "archived": bool(_object_to_int(_row_value(row, "archived"), 0)),
                        "last_message_at": _object_to_int_or_none(_row_value(row, "last_message_at")),
                    }
                )

            def state_value(key: str) -> str | None:
                return read_daemon_state_value(conn, key)

            def state_int(key: str) -> int | None:
                return read_daemon_state_int(conn, key)

            observation = {
                "status": state_value("dialog_unread_sweep_status"),
                "completed_at": state_int("dialog_unread_sweep_completed_at"),
                "observed_count": state_int("dialog_unread_sweep_observed_count"),
                "visible_count": state_int("dialog_unread_sweep_last_visible_count"),
            }
            return {
                "ok": True,
                "data": {
                    "dialogs": dialogs,
                    "count": len(dialogs),
                    "total_matching": total_matching,
                    "truncated": total_matching > len(dialogs),
                    "source_observation": observation,
                },
            }
        finally:
            if began_transaction:
                conn.rollback()

    def _list_dialogs_from_reader(self, db_path: Path, req: dict) -> dict:
        conn = open_sync_db_reader(db_path)
        try:
            conn.row_factory = sqlite3.Row
            return self._list_dialogs_sync(conn, req)
        finally:
            conn.close()

    def _list_dialogs_sync(self, conn: sqlite3.Connection, req: dict) -> dict:
        """Return dialog list from the local dialogs snapshot (pure SQL)."""
        directory_coverage = read_dialog_directory_coverage(conn).to_wire()
        request = self._parse_list_dialogs_request(req)
        request_error = self._list_dialogs_request_error(request)
        if request_error is not None:
            return request_error
        dialog_filter = self._prepare_list_dialogs_filter(request.filter_raw)
        scheduled_summary = scheduled_summary_by_dialog(conn, scheduled_now=int(time.time()))
        own_basis: dict[int, tuple[str, ...]] = self._own_only_basis_by_dialog(conn)
        sql_rows = self._fetch_list_dialog_rows(conn, request, dialog_filter)
        if not sql_rows:
            count_total = count_dialog_rows(conn)
            return self._empty_list_dialogs_response(
                request.scope,
                directory_coverage,
                bootstrap_pending=count_total == 0,
            )
        identities = read_dialog_identities(conn, [_object_to_int(_row_value(row, "dialog_id")) for row in sql_rows])
        selected_rows = self._select_list_dialog_rows(
            sql_rows,
            request,
            dialog_filter,
            scheduled_summary,
            own_basis,
            identities,
        )
        if not selected_rows:
            return self._empty_list_dialogs_response(
                request.scope,
                directory_coverage,
                bootstrap_pending=False,
            )
        return self._project_list_dialog_rows(conn, selected_rows, request, directory_coverage)
