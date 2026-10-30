"""Application service for durable local reading operations."""

import asyncio
import dataclasses
import hashlib
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from rapidfuzz import fuzz as _fuzz

from ..budget import allocate_message_budget_round_robin, unread_chat_tier
from ..daemon_message import (
    cached_reaction_freshness,
    project_cached_message_facts,
    project_cached_message_facts_by_dialog,
)
from ..dialog_directory_coverage import DialogDirectoryCoverage, read_dialog_directory_coverage
from ..dialog_identity import read_dialog_identities
from ..dialog_identity_contracts import DialogIdentity
from ..dialog_selector import DialogSelector, DialogSelectorError, optional_dialog_selector, required_dialog_selector
from ..folders.read_repository import folder_snapshot, folders_by_dialog
from ..fts import stem_query
from ..models import DialogType, DraftReadRecord, ReadMessage, ReadState
from ..own_only import own_only_basis_by_dialog
from ..pagination import (
    DialogDirectoryNavigationToken,
    HistoryDirection,
    NavigationToken,
    ScheduledQueueNavigationToken,
    decode_dialog_directory_navigation,
    decode_navigation_token,
    decode_scheduled_queue_navigation,
    encode_dialog_directory_navigation,
    encode_history_navigation,
    encode_scheduled_queue_navigation,
    encode_search_navigation,
)
from ..reactions.contracts import ReactionFreshness
from ..request_timing import current_timing, timing_phase
from ..resolver import latinize
from ..sync_db import open_sync_db_reader
from ..sync_read_model import (
    LocalHistoryCoverage,
    SyncReadModelContractError,
    build_sync_read_model,
    local_history_coverage,
)
from ..sync_transactions import read_snapshot
from ..temporal import parse_utc_boundary
from .draft_projection import draft_message_key, read_drafts
from .query_records import read_message_from_row
from .scheduled_projection import (
    build_scheduled_global_list_query,
    build_scheduled_list_query,
    build_scheduled_search_query,
    scheduled_message_time,
    scheduled_messages_available,
    scheduled_row_to_wire,
    scheduled_summary_by_dialog,
)
from .sqlite_projection import (
    _COLLECT_UNREAD_DIALOG_CANDIDATES_SQL,
    _COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL,
    _COUNT_READ_POSITION_PENDING_SQL,
    _DIALOG_DIRECTORY_FIRST_PAGE_SQL,
    _DIALOG_DIRECTORY_PAGE_SQL,
    _DIALOG_DIRECTORY_UPPER_SQL,
    _FETCH_UNREAD_MESSAGES_SQL,
    _GET_READ_POSITION_SQL,
    _LIST_DIALOG_MESSAGE_AGGREGATES_SQL,
    _LIST_DIALOGS_SQL,
    _LIST_MESSAGES_BASE_SQL,
    _READ_POSITION_PENDING_IDENTITIES_SQL,
    _SELECT_FTS_ALL_CHRONOLOGICAL_SQL,
    _SELECT_FTS_ALL_SQL,
    _SELECT_FTS_CHRONOLOGICAL_SQL,
    _SELECT_FTS_SQL,
    _SELECT_SYNC_STATUS_SQL,
    _UNREAD_SUMMARY_SQL,
    _build_access_metadata,
    _build_list_messages_query,
    _compute_snapshot_age_h,
    _read_state_for_dialog,
    count_dialog_rows,
    message_sent_at,
    read_context_anchor,
    read_daemon_state_int,
    read_daemon_state_value,
)

# These thresholds are part of reading's local fuzzy dialog projection.  They
# intentionally do not depend on the account-trace orchestration module.
_TRACE_ACRONYM_MIN_LEN = 2
_TRACE_ACRONYM_MAX_LEN = 4
_TRACE_FUZZY_MIN_LEN = 4
_TRACE_FUZZY_SCORE_MIN = 75


class _LocalSearchIndexUnavailableError(Exception):
    """A local sent-message FTS query could not be executed."""


class LoggerLike(Protocol):
    def debug(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def info(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def warning(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def error(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def exception(self, msg: str, *args: object, **kwargs: object) -> None: ...


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


@dataclass(frozen=True)
class ReadingDeps:
    """External collaborators required by the reading application service."""

    conn: sqlite3.Connection
    sync_db_path: Path | None
    self_id: int | None
    resolve_dialog_id: Callable[[DialogSelector], Awaitable[int | dict]]
    logger: LoggerLike
    rid: Callable[[], str]
    deleted_message_visibility_seconds: int
    draft_response_budget_bytes: int


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
    read_scope: str = "dialog"
    since_utc: int | None = None
    until_utc: int | None = None

    def context_filter_error(self) -> dict[str, object] | None:
        """Reject filters that cannot describe an anchored context window."""
        if self.context_message_id is None:
            return None
        if self.unread or any(
            value is not None for value in (self.sender_id, self.sender_name, self.topic_id, self.unread_after_id)
        ):
            return {
                "ok": False,
                "error": "invalid_context_filters",
                "message": "Context anchors cannot be combined with sender, topic, or unread filters.",
                "required_action": "Remove the filters or the context anchor.",
            }
        return None


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
    scheduled_coverage: dict[str, object] | None = None


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
        elif state in {"scheduled", "unknown_missing"}:
            self.scheduled_message_id = _message_id_from_item(row)
            self.scheduled_sent_at = _object_to_int_or_none(row.get("sent_at"))
        elif state == "draft":
            self.draft_key = str(row["message_key"])


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


_DIRECTORY_PAGE_LIMIT = 500


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
            read_scope=req.get("read_scope", "dialog"),
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

        All reads project persisted facts. Grouping cross-dialog results keeps
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
        _set_timing_route("local_context")
        return await self._list_messages_context_window(
            dialog_id=dialog_id,
            anchor_message_id=request.context_message_id or 0,
            context_size=request.context_size,
            since_utc=request.since_utc,
            until_utc=request.until_utc,
        )

    async def _list_messages_history_result(
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
        result["data"]["interval_coverage"] = local_history_coverage(
            status, access_metadata["last_synced_at"]
        ).interval_coverage(request.until_utc)
        result["data"]["selection_state"] = local_history_coverage(
            status, access_metadata["last_synced_at"]
        ).selection_state(
            present=bool(result["data"]["messages"]),
            attribution_complete=(
                request.topic_id is None and request.sender_name is None and request.sender_id is None
            ),
            since_utc=request.since_utc,
            until_utc=request.until_utc,
            paginated=anchor_msg_id is not None,
        )
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

    def _fetch_search_rows(self, sql: str, parameters: dict[str, object]) -> list[object]:
        try:
            return _fetchall_rows(self._conn.execute(sql, parameters))
        except sqlite3.Error as exc:
            raise _LocalSearchIndexUnavailableError from exc

    async def _search_messages_global_result(
        self,
        request: _SearchMessagesRequest,
        stemmed: str,
    ) -> dict:
        with timing_phase("local_projection"):
            rows = self._fetch_search_rows(
                _SELECT_FTS_ALL_CHRONOLOGICAL_SQL if request.message_state == "all" else _SELECT_FTS_ALL_SQL,
                {
                    "query": stemmed,
                    "limit": request.limit,
                    "offset": request.offset,
                    "self_id": self._deps.self_id,
                    "since_utc": request.since_utc,
                    "until_utc": request.until_utc,
                },
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
                "offset": request.offset,
                "read_state_per_dialog": self._read_state_per_dialog(messages),
            },
        }

    async def _search_messages_scoped_result(
        self,
        request: _SearchMessagesRequest,
        stemmed: str,
    ) -> dict:
        with timing_phase("local_projection"):
            rows = self._fetch_search_rows(
                _SELECT_FTS_CHRONOLOGICAL_SQL if request.message_state == "all" else _SELECT_FTS_SQL,
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
                "offset": request.offset,
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
        account_coverage: dict[str, object] | None = None
        coverage_result = self._scheduled_account_coverage()
        if isinstance(coverage_result, dict):
            return coverage_result
        _, account_coverage = coverage_result
        if account_coverage.get("state") == "unknown" or not scheduled_messages_available(self._conn):
            return {
                "ok": True,
                "data": {
                    "messages": [],
                    "total": 0,
                    "next_navigation": None,
                    "offset": request.offset,
                    "source": "scheduled_messages",
                    "scheduled_coverage": account_coverage,
                },
            }
        with timing_phase("local_projection"):
            own_basis = self._own_only_basis_by_dialog()
            sql, params = build_scheduled_search_query(
                dialog_id=request.dialog_id,
                query=stem_query(request.query),
                limit=request.limit,
                offset=request.offset,
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
                        inclusion_basis=own_basis.get(_object_to_int(_row_value(raw_row, "dialog_id"))),
                        account_id=self._deps.self_id,
                    ),
                    "dialog_name": identities[_object_to_int(_row_value(raw_row, "dialog_id"))].display_name,
                    "dialog_name_source": identities[
                        _object_to_int(_row_value(raw_row, "dialog_id"))
                    ].display_name_source,
                    "dialog_type": identities[_object_to_int(_row_value(raw_row, "dialog_id"))].dialog_type.value,
                    "scheduled_coverage": self._scheduled_dialog_coverage(
                        _object_to_int(_row_value(raw_row, "dialog_id"))
                    ),
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
                "offset": request.offset,
                "source": "scheduled_messages",
                "scope": "account",
                "scheduled_coverage": account_coverage,
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
        rows = list(
            {(row["dialog_id"], row.get("message_state", "sent"), row["message_id"]): row for row in rows}.values()
        )
        rows.sort(
            key=lambda row: (
                int(row.get("sent_at") or 0),
                int(row["message_id"]),
                int(row["dialog_id"]),
                row.get("message_state", "sent"),
            )
        )
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
                "offset": request.offset,
                "source": "sync_db+scheduled_messages",
                "read_state_per_dialog": sent_data.get("read_state_per_dialog", {}),
                "scope": "all",
                "scheduled_coverage": scheduled_data.get("scheduled_coverage"),
                "dialog_name": sent_data.get("dialog_name", scheduled_data.get("dialog_name")),
                "dialog_name_source": sent_data.get("dialog_name_source", scheduled_data.get("dialog_name_source")),
            },
        }

    def _scheduled_search_for_mixed(self, request: _SearchMessagesRequest) -> dict:
        result = self._search_scheduled_messages(request)
        if result.get("ok"):
            return result
        return {
            "ok": True,
            "data": {
                "messages": [],
                "total": 0,
                "next_navigation": None,
                "offset": request.offset,
                "source": "scheduled_messages",
                "scheduled_coverage": {"state": "unknown", "reason": result.get("error")},
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
        scheduled_summary: Mapping[int, tuple[int, int | None, int]],
        own_basis: Mapping[int, tuple[str, ...]],
        identities: Mapping[int, DialogIdentity],
        conn: sqlite3.Connection,
        scheduled_coverages: Mapping[int, dict[str, object]] | None = None,
    ) -> list[tuple[Mapping[str, object], tuple[int, int | None, int], tuple[str, ...] | None]]:
        selected_rows: list[tuple[Mapping[str, object], tuple[int, int | None, int], tuple[str, ...] | None]] = []
        for row in sql_rows:
            dialog_id = _object_to_int(row["dialog_id"])
            identity = identities[dialog_id]
            if request.message_state != "scheduled" and request.scope == "own_only" and dialog_id not in own_basis:
                continue
            summary = scheduled_summary.get(dialog_id, (0, None, 0))
            if request.message_state == "scheduled" and summary[0] == 0 and summary[2] == 0:
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
                        "scheduled_coverage": (
                            self._scheduled_dialog_coverage(dialog_id, conn)
                            if scheduled_coverages is None
                            else scheduled_coverages.get(dialog_id)
                        ),
                    },
                    summary,
                    own_basis.get(dialog_id),
                )
            )
        return selected_rows

    def _project_list_dialog_rows(
        self,
        conn: sqlite3.Connection,
        selected_rows: Sequence[tuple[Mapping[str, object], tuple[int, int | None, int], tuple[str, ...] | None]],
        request: _ListDialogsRequest,
        directory_coverage: dict,
        scheduled_coverage: dict[str, object],
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
                "scheduled_coverage": scheduled_coverage,
            },
        }

    @staticmethod
    def _empty_list_dialogs_response(
        scope: str,
        directory_coverage: dict,
        scheduled_coverage: dict[str, object],
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
                "scheduled_coverage": scheduled_coverage,
            },
        }

    def _shape_dialog_row(  # noqa: PLR0913, PLR0917
        self,
        row: Mapping[str, object],
        aggregate: _DialogMessageAggregate,
        enrollment_enabled: bool | None,
        read_model_now: int,
        scheduled_summary: tuple[int, int | None, int] = (0, None, 0),
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
            "topic_capability": row["topic_capability"],
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
        scheduled_summary: tuple[int, int | None, int],
        inclusion_basis: tuple[str, ...] | None,
    ) -> dict[str, object]:
        return {
            "scheduled_count": scheduled_summary[0],
            "next_scheduled_at": scheduled_summary[1],
            "unresolved_scheduled_count": scheduled_summary[2],
            "inclusion_basis": list(inclusion_basis) if inclusion_basis is not None else None,
            "scheduled_coverage": row.get("scheduled_coverage"),
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

    def _local_context_anchor(
        self,
        dialog_id: int,
        anchor_message_id: int,
        since_utc: int | None,
        until_utc: int | None,
    ) -> int | dict[str, object]:
        """Require a retained visible anchor and return its chronological position."""
        with timing_phase("local_projection"):
            anchor = read_context_anchor(self._conn, dialog_id, anchor_message_id)
        unavailable = None
        if anchor is None:
            unavailable = "anchor_missing"
        elif anchor[1]:
            unavailable = "anchor_deleted"
        elif (since_utc is not None and anchor[0] < since_utc) or (until_utc is not None and anchor[0] >= until_utc):
            unavailable = "anchor_outside_time_range"
        if unavailable is not None:
            return {
                "ok": False,
                "error": "local_context_unavailable",
                "message": "The requested anchor is unavailable in the retained local selection.",
                "context_availability": unavailable,
                "required_action": "Choose a retained, non-deleted anchor within the UTC bounds, or mark the dialog for sync to acquire history.",
            }
        assert anchor is not None
        return anchor[0]

    async def _list_messages_context_window(
        self,
        dialog_id: int,
        anchor_message_id: int,
        context_size: int,
        since_utc: int | None = None,
        until_utc: int | None = None,
    ) -> dict:
        """Return messages centred on anchor_message_id from sync.db."""
        anchor_sent_at = self._local_context_anchor(dialog_id, anchor_message_id, since_utc, until_utc)
        if isinstance(anchor_sent_at, dict):
            return anchor_sent_at
        before_count = context_size // 2
        after_count = context_size - before_count - 1
        with timing_phase("local_projection"):
            before_rows = _fetchall_rows(
                self._conn.execute(
                    _LIST_MESSAGES_BASE_SQL
                    + " AND (m.sent_at < :anchor_sent_at OR (m.sent_at = :anchor_sent_at AND m.message_id <= :anchor)) AND (:since_utc IS NULL OR m.sent_at >= :since_utc) AND (:until_utc IS NULL OR m.sent_at < :until_utc) ORDER BY m.sent_at DESC, m.message_id DESC LIMIT :limit",
                    {
                        "dialog_id": dialog_id,
                        "self_id": self._deps.self_id,
                        "anchor": anchor_message_id,
                        "anchor_sent_at": anchor_sent_at,
                        "limit": context_size,
                        "since_utc": since_utc,
                        "until_utc": until_utc,
                    },
                )
            )

            after_rows = _fetchall_rows(
                self._conn.execute(
                    _LIST_MESSAGES_BASE_SQL
                    + " AND (m.sent_at > :anchor_sent_at OR (m.sent_at = :anchor_sent_at AND m.message_id > :anchor)) AND (:since_utc IS NULL OR m.sent_at >= :since_utc) AND (:until_utc IS NULL OR m.sent_at < :until_utc) ORDER BY m.sent_at ASC, m.message_id ASC LIMIT :limit",
                    {
                        "dialog_id": dialog_id,
                        "self_id": self._deps.self_id,
                        "anchor": anchor_message_id,
                        "anchor_sent_at": anchor_sent_at,
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
                    "selection_state": "present",
                    **_build_access_metadata(
                        self._conn,
                        dialog_id,
                        _status_from_row(_fetchone_row(self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,))))
                        or "not_synced",
                    ),
                    "next_navigation": None,
                    "dialog_type": dialog_type,
                    "dialog_name": identity.display_name,
                    "dialog_name_source": identity.display_name_source,
                    "read_state": read_state,
                    "reaction_freshness": freshness.as_dict(),
                },
            }

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

    def _scheduled_account_coverage(
        self, conn: sqlite3.Connection | None = None, *, account_id: int | None = None
    ) -> tuple[int, dict[str, object]] | dict[str, object]:
        """Read scheduled account and baseline coverage without acquiring or mutating facts."""
        source = self._conn if conn is None else conn
        account_id = self._deps.self_id if account_id is None else account_id
        if type(account_id) is not int or account_id <= 0:
            return {
                "ok": False,
                "error": "scheduled_account_unverified",
                "message": "The local scheduled queue is not bound to a verified Telegram account.",
            }
        try:
            account = _fetchone_row(
                source.execute(
                    "SELECT account_id,directory_generation,next_retry_at,last_snapshot_at,last_error "
                    "FROM scheduled_sync_state WHERE key='account'"
                )
            )
            if account is None or _row_value(account, "account_id") is None:
                return account_id, {"state": "unknown", "reason": "scheduled_account_unbound"}
            if _row_value(account, "account_id") != account_id:
                return {
                    "ok": False,
                    "error": "scheduled_account_mismatch",
                    "message": "The local scheduled queue account does not match the verified Telegram account.",
                }
            directory = read_dialog_directory_coverage(source).to_wire()
            state_count_rows = cast(
                list[tuple[object, object]],
                source.execute(
                    "SELECT coverage_state,COUNT(*) FROM scheduled_reconciliation_state GROUP BY coverage_state"
                ).fetchall(),
            )
            state_counts = {cast(str, row[0]): _object_to_int(row[1]) for row in state_count_rows}
            baseline_missing_count = _object_to_int(
                cast(
                    tuple[object],
                    source.execute(
                        "SELECT COUNT(*) FROM ("
                        "SELECT dialog_id FROM dialogs UNION "
                        "SELECT dialog_id FROM scheduled_messages WHERE message_state IN ('scheduled','unknown_missing') UNION "
                        "SELECT ?"
                        ") AS candidates LEFT JOIN scheduled_reconciliation_state s USING(dialog_id) "
                        "WHERE s.dialog_id IS NULL",
                        (account_id,),
                    ).fetchone(),
                )[0]
            )
            acquisition_due = _object_to_int(
                cast(
                    tuple[object],
                    source.execute(
                        "SELECT COUNT(*) FROM scheduled_reconciliation_state "
                        "WHERE repair_due_at IS NOT NULL OR discovery_due_at IS NOT NULL"
                    ).fetchone(),
                )[0]
            )
        except sqlite3.Error, TypeError, IndexError, KeyError:
            return {
                "ok": False,
                "error": "scheduled_coverage_unavailable",
                "message": "Scheduled queue account coverage could not be read safely.",
            }
        return account_id, self._scheduled_account_coverage_data(
            account_id=account_id,
            account=account,
            directory=directory,
            state_counts=state_counts,
            baseline_missing_count=baseline_missing_count,
            acquisition_due=acquisition_due,
        )

    @staticmethod
    def _scheduled_account_coverage_data(  # noqa: PLR0913
        *,
        account_id: int,
        account: object,
        directory: dict[str, object],
        state_counts: dict[str, int],
        baseline_missing_count: int,
        acquisition_due: int,
    ) -> dict[str, object]:
        directory_generation = directory.get("publication_generation")
        enumerated_generation = _object_to_int_or_none(_row_value(account, "directory_generation"))
        generation_matches = enumerated_generation is not None and enumerated_generation == directory_generation
        incomplete_count = sum(state_counts.get(state, 0) for state in ("unknown", "inaccessible"))
        coverage_state = (
            "complete" if generation_matches and incomplete_count == 0 and baseline_missing_count == 0 else "partial"
        )
        return {
            "state": coverage_state,
            "account_id": account_id,
            "directory_generation": directory_generation,
            "enumerated_generation": enumerated_generation,
            "generation_matches": generation_matches,
            "directory_coverage": directory,
            "baseline_counts": {
                "complete": state_counts.get("complete", 0),
                "unknown": state_counts.get("unknown", 0),
                "inaccessible": state_counts.get("inaccessible", 0),
                "unsupported": state_counts.get("unsupported", 0),
                "missing": baseline_missing_count,
            },
            "acquisition_due_dialog_count": acquisition_due,
            "next_retry_at": _object_to_int_or_none(_row_value(account, "next_retry_at")),
            "last_snapshot_at": _object_to_int_or_none(_row_value(account, "last_snapshot_at")),
            "last_error": _object_to_str_or_none(_row_value(account, "last_error")),
        }

    def _scheduled_dialog_coverage(self, dialog_id: int, conn: sqlite3.Connection | None = None) -> dict[str, object]:
        source = self._conn if conn is None else conn
        try:
            account = _fetchone_row(source.execute("SELECT account_id FROM scheduled_sync_state WHERE key='account'"))
            if (
                type(self._deps.self_id) is not int
                or account is None
                or _row_value(account, "account_id") != self._deps.self_id
            ):
                return {"state": "unknown", "reason": "scheduled_account_not_bound"}
            row = _fetchone_row(
                source.execute(
                    "SELECT coverage_state,coverage_reason,baseline_observed_at,directory_generation,"
                    "repair_due_at,discovery_due_at FROM scheduled_reconciliation_state WHERE dialog_id=?",
                    (dialog_id,),
                )
            )
        except sqlite3.Error:
            row = None
        if row is None:
            return {
                "state": "unknown",
                "reason": "baseline_missing",
                "baseline_observed_at": None,
                "directory_generation": None,
                "acquisition_pending": False,
            }
        return {
            "state": _row_value(row, "coverage_state"),
            "reason": _row_value(row, "coverage_reason"),
            "baseline_observed_at": _object_to_int_or_none(_row_value(row, "baseline_observed_at")),
            "directory_generation": _object_to_int_or_none(_row_value(row, "directory_generation")),
            "acquisition_pending": (
                _row_value(row, "repair_due_at") is not None or _row_value(row, "discovery_due_at") is not None
            ),
        }

    @staticmethod
    def _scheduled_filter_fingerprint(
        *,
        sender_id: int | None,
        sender_name: str | None,
        topic_id: int | None,
        since_utc: int | None,
        until_utc: int | None,
    ) -> str:
        payload = json.dumps(
            [sender_id, sender_name, topic_id, since_utc, until_utc],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()[:24]

    def _list_scheduled_queue(  # noqa: PLR0914
        self,
        request: _ListMessagesRequest,
        *,
        dialog_id: int | None,
        read_scope: str,
        direction: str,
    ) -> dict:
        """Read pending scheduled facts with a local, account-bound keyset."""
        _set_timing_route("local_non_sent_state")
        with timing_phase("local_projection"):
            account_result = self._scheduled_account_coverage()
        if isinstance(account_result, dict):
            return account_result
        account_id, coverage = account_result
        with timing_phase("resolution"):
            fingerprint = self._scheduled_filter_fingerprint(
                sender_id=request.sender_id,
                sender_name=request.sender_name,
                topic_id=request.topic_id,
                since_utc=request.since_utc,
                until_utc=request.until_utc,
            )
            navigation = self._bind_scheduled_queue_navigation(
                request,
                account_id=account_id,
                fingerprint=fingerprint,
                dialog_id=dialog_id,
                read_scope=read_scope,
                direction=direction,
            )
        if isinstance(navigation, dict):
            return navigation
        anchor, direction = navigation
        raw_rows = self._read_scheduled_queue_rows(request, dialog_id, direction, anchor, coverage)
        with timing_phase("response_shape"):
            has_more = len(raw_rows) > request.limit
            selected = raw_rows[: request.limit]
            selected_count = len(selected)
            rows, identities = self._project_scheduled_queue_rows(selected, dialog_id, account_id)
            bounded_rows, budget_truncated = self._bound_scheduled_queue_rows(rows)
            if budget_truncated:
                selected = selected[: len(bounded_rows)]
                has_more = True
            rows = bounded_rows if direction == "oldest" else list(reversed(bounded_rows))
            next_navigation = None
            if has_more and selected:
                next_navigation = self._scheduled_queue_next_navigation(
                    selected[-1],
                    account_id=account_id,
                    read_scope=read_scope,
                    dialog_id=dialog_id,
                    direction=direction,
                    fingerprint=fingerprint,
                )
            data: dict[str, object] = {
                "messages": rows,
                "source": "scheduled_messages",
                "next_navigation": next_navigation,
                "message_state": "scheduled",
                "read_scope": read_scope,
                "navigation_direction": direction,
                "scope": "all",
                "scheduled_coverage": coverage,
                "has_more": has_more,
                "truncation": {
                    "is_truncated": budget_truncated,
                    "shown_count": len(rows),
                    "hidden_count": selected_count - len(rows) if budget_truncated else 0,
                    "reason": "response_budget" if budget_truncated else None,
                },
            }
            if dialog_id is not None:
                identity = identities[dialog_id]
                data.update(
                    dialog_id=dialog_id,
                    dialog_name=identity.display_name,
                    dialog_name_source=identity.display_name_source,
                    dialog_type=identity.dialog_type.value,
                    dialog_scheduled_coverage=self._scheduled_dialog_coverage(dialog_id),
                )
            return {"ok": True, "data": data}

    @staticmethod
    def _bind_scheduled_queue_navigation(  # noqa: PLR0913
        request: _ListMessagesRequest,
        *,
        account_id: int,
        fingerprint: str,
        dialog_id: int | None,
        read_scope: str,
        direction: str,
    ) -> tuple[ScheduledQueueNavigationToken | None, str] | dict:
        anchor: ScheduledQueueNavigationToken | None = None
        if request.navigation not in (None, "latest", "start", "newest", "oldest"):
            try:
                anchor = decode_scheduled_queue_navigation(request.navigation)
            except ValueError as exc:
                return {"ok": False, "error": "invalid_navigation", "message": str(exc)}
            # The opaque cursor owns its traversal direction. MCP's direction
            # defaults to newest on continuation, including after start/oldest.
            direction = anchor.direction.value
            if (
                anchor.account_id != account_id
                or anchor.read_scope != read_scope
                or anchor.dialog_id != dialog_id
                or anchor.filter_fingerprint != fingerprint
            ):
                return {
                    "ok": False,
                    "error": "invalid_navigation",
                    "message": "Navigation token belongs to a different account, scope, direction, or filter.",
                }
        elif request.navigation in {"oldest", "start"}:
            direction = "oldest"
        return anchor, direction

    def _read_scheduled_queue_rows(
        self,
        request: _ListMessagesRequest,
        dialog_id: int | None,
        direction: str,
        anchor: ScheduledQueueNavigationToken | None,
        coverage: dict[str, object],
    ) -> list[object]:
        if coverage.get("state") == "unknown" or not scheduled_messages_available(self._conn):
            raw_rows: list[object] = []
        else:
            with timing_phase("local_projection"):
                sql, params = build_scheduled_global_list_query(
                    dialog_id=dialog_id,
                    limit=request.limit + 1,
                    direction=direction,
                    anchor_bucket=anchor.time_bucket if anchor is not None else None,
                    anchor_scheduled_at=anchor.scheduled_at if anchor is not None else None,
                    anchor_dialog_id=anchor.anchor_dialog_id if anchor is not None else None,
                    anchor_message_id=anchor.message_id if anchor is not None else None,
                    sender_id=request.sender_id,
                    sender_name=request.sender_name,
                    topic_id=request.topic_id,
                    since_utc=request.since_utc,
                    until_utc=request.until_utc,
                )
                raw_rows = _fetchall_rows(self._conn.execute(sql, params))
        return raw_rows

    def _project_scheduled_queue_rows(
        self,
        selected: list[object],
        dialog_id: int | None,
        account_id: int,
    ) -> tuple[list[dict], dict[int, DialogIdentity]]:
        identities = read_dialog_identities(
            self._conn,
            {_object_to_int(_row_value(row, "dialog_id")) for row in selected}
            | ({dialog_id} if dialog_id is not None else set()),
        )
        own_basis = self._own_only_basis_by_dialog()
        rows: list[dict] = []
        for raw_row in selected:
            row_dialog_id = _object_to_int(_row_value(raw_row, "dialog_id"))
            row = {
                **scheduled_row_to_wire(
                    cast(Mapping[str, object], raw_row),
                    inclusion_basis=own_basis.get(row_dialog_id),
                    account_id=account_id,
                ),
                "dialog_name": identities[row_dialog_id].display_name,
                "dialog_name_source": identities[row_dialog_id].display_name_source,
                "dialog_type": identities[row_dialog_id].dialog_type.value,
                "scheduled_coverage": self._scheduled_dialog_coverage(row_dialog_id),
            }
            rows.append(row)
        return rows, identities

    def _bound_scheduled_queue_rows(self, rows: list[dict]) -> tuple[list[dict], bool]:
        bounded_rows: list[dict] = []
        payload_bytes = 0
        budget_truncated = False
        for row in rows:
            row_bytes = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
            if bounded_rows and payload_bytes + row_bytes > self._deps.draft_response_budget_bytes:
                budget_truncated = True
                break
            bounded_rows.append(row)
            payload_bytes += row_bytes
        return bounded_rows, budget_truncated

    @staticmethod
    def _scheduled_queue_next_navigation(  # noqa: PLR0913
        last: object,
        *,
        account_id: int,
        read_scope: str,
        dialog_id: int | None,
        direction: str,
        fingerprint: str,
    ) -> str:
        last_dialog_id = _object_to_int(_row_value(last, "dialog_id"))
        scheduled_at = _object_to_int_or_none(_row_value(last, "scheduled_at"))
        return encode_scheduled_queue_navigation(
            ScheduledQueueNavigationToken(
                account_id=account_id,
                read_scope=cast("Literal['dialog', 'all_dialogs']", read_scope),
                dialog_id=dialog_id,
                direction=HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST,
                filter_fingerprint=fingerprint,
                time_bucket=0 if scheduled_at is None else 1,
                scheduled_at=scheduled_at,
                anchor_dialog_id=last_dialog_id,
                message_id=_object_to_int(_row_value(last, "message_id")),
            )
        )

    def _list_scheduled_messages_from_db(self, req: _ListMessagesDbRequest) -> dict:
        """Read pending scheduled messages from the separate local mirror.

        Scheduled messages deliberately do not use ``messages`` or any of its
        derived tables.  This path is local-only: it never falls back to a
        Telegram request when the mirror is empty or unavailable.
        When ``own_only_dialogs`` exists, its ownership cache is authoritative.
        """
        coverage_result = self._scheduled_account_coverage()
        if isinstance(coverage_result, dict):
            return coverage_result
        account_id, account_coverage = coverage_result
        with timing_phase("local_projection"):
            own_basis: dict[int, tuple[str, ...]] = {}
            if account_coverage.get("state") != "unknown" and scheduled_messages_available(self._conn):
                own_basis = self._own_only_basis_by_dialog()
                anchor_sent_at = req.anchor_sent_at
                if req.anchor_msg_id is not None and anchor_sent_at is None:
                    anchor_sent_at = scheduled_message_time(self._conn, req.dialog_id, req.anchor_msg_id)
                sql, params = build_scheduled_list_query(
                    req,
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
                        account_id=account_id,
                    ),
                    "dialog_name": identities[_object_to_int(_row_value(raw_row, "dialog_id"))].display_name,
                    "dialog_name_source": identities[
                        _object_to_int(_row_value(raw_row, "dialog_id"))
                    ].display_name_source,
                    "dialog_type": identities[_object_to_int(_row_value(raw_row, "dialog_id"))].dialog_type.value,
                    "scheduled_coverage": self._scheduled_dialog_coverage(
                        _object_to_int(_row_value(raw_row, "dialog_id"))
                    ),
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
                    "scope": "all",
                    "account_coverage": account_coverage,
                    "scheduled_coverage": self._scheduled_dialog_coverage(req.dialog_id),
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
        response = {
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
        return self._attach_dialog_identity(response, req.dialog_id)

    def _attach_dialog_identity(self, response: dict, dialog_id: int) -> dict:
        """Attach the canonical owner bundle to a successful scoped response."""
        identity = read_dialog_identities(self._conn, [dialog_id])[dialog_id]
        data = response.get("data")
        if isinstance(data, dict):
            data.update(
                {
                    "dialog_type": identity.dialog_type.value,
                    "dialog_name": identity.display_name,
                    "dialog_name_source": identity.display_name_source,
                }
            )
        return response

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
            result = self._attach_dialog_identity(scheduled_result, dialog_id)
        with timing_phase("response_shape"):
            result["data"]["read_state"] = None
        return result

    async def _all_local_state_result(self, all_request: _AllLocalStateRequest) -> dict:
        navigation = all_request.navigation
        sent_request = dataclasses.replace(
            all_request.db_request,
            anchor_msg_id=navigation.value if navigation is not None else None,
            anchor_sent_at=navigation.sent_at if navigation is not None else None,
        )
        sent_rows = await self._all_sent_rows(sent_request)
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
        scheduled_result = self._list_scheduled_messages_from_db(scheduled_request)
        scheduled_rows = scheduled_result.get("data", {}).get("messages", []) if scheduled_result.get("ok") else []
        scheduled_coverage = (
            scheduled_result.get("data", {}).get("account_coverage")
            if scheduled_result.get("ok")
            else {"state": "unknown", "reason": scheduled_result.get("error")}
        )
        draft_rows = draft_result["data"]["messages"]
        state = _AllLocalState(
            all_request,
            sent_rows,
            scheduled_rows,
            draft_rows,
            draft_result,
            self._all_local_metadata(all_request.dialog_id, all_request.status),
            scheduled_coverage,
        )
        return self._all_local_response(state)

    async def _all_sent_rows(self, db_request: _ListMessagesDbRequest) -> list[dict]:
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
        return dialog_type, read_state, access_metadata

    def _all_projection_absence_known(self, state: _AllLocalState) -> bool:
        scheduled = self._scheduled_dialog_coverage(state.request.dialog_id)
        draft = state.draft_result["data"]["draft_coverage"] if state.draft_result is not None else {}
        return (
            isinstance(state.scheduled_coverage, dict)
            and state.scheduled_coverage.get("state") in {"complete", "partial"}
            and scheduled.get("state") == "complete"
            and scheduled.get("baseline_observed_at") is not None
            and draft.get("presence") == "absent"
            and draft.get("freshness") == "current"
        )

    def _all_selection_state(self, state: _AllLocalState, page: list[dict], coverage: LocalHistoryCoverage) -> str:
        request = state.request.request
        selection = coverage.selection_state(
            present=bool(page),
            attribution_complete=request.topic_id is None and request.sender_name is None and request.sender_id is None,
            since_utc=request.since_utc,
            until_utc=request.until_utc,
            paginated=state.request.navigation is not None,
        )
        if selection == "known_empty" and not request.unread and not self._all_projection_absence_known(state):
            return "unknown"
        return selection

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
            history_coverage = local_history_coverage(
                state.request.status, cast(int | None, access_metadata.get("last_synced_at"))
            )
            selection_state = self._all_selection_state(state, page, history_coverage)
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
                    "selection_state": selection_state,
                    "interval_coverage": history_coverage.interval_coverage(request.until_utc),
                    "dialog_type": dialog_type,
                    "read_state": read_state,
                    **projection_metadata,
                    "scheduled_coverage": state.scheduled_coverage,
                    "dialog_scheduled_coverage": self._scheduled_dialog_coverage(state.request.dialog_id),
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
    def _all_row_sort_key(row: dict) -> tuple[int, int, int, int | str, int]:
        state = str(row.get("message_state"))
        state_order = {"sent": 0, "scheduled": 1, "unknown_missing": 1, "draft": 2}
        timestamp_value = row.get("sent_at")
        if state == "draft":
            timestamp_value = row.get("draft_updated_at")
        has_time = 0 if timestamp_value is None else 1
        timestamp = int(timestamp_value or 0)
        if state == "draft":
            return (has_time, timestamp, 1, str(row.get("message_key") or ""), state_order["draft"])
        return (
            has_time,
            timestamp,
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
        if request.message_state == "scheduled":
            return self._scheduled_local_state_result(
                dialog_id,
                self._list_scheduled_messages_from_db(
                    self._local_state_db_request(dialog_id, request, direction, None, None)
                ),
            )
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
        all_navigation = self._decode_all_history_navigation(request)
        if isinstance(all_navigation, dict):
            return all_navigation
        anchor_sent_at = None if all_navigation is None else all_navigation.sent_at
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

    @staticmethod
    def _decode_all_history_navigation(request: _ListMessagesRequest) -> NavigationToken | dict | None:
        all_navigation: NavigationToken | None = None
        if request.message_state == "all" and request.navigation not in (None, "newest", "oldest"):
            navigation = request.navigation
            assert navigation is not None
            try:
                all_navigation = decode_navigation_token(navigation)
            except ValueError as exc:
                return {"ok": False, "error": "invalid_navigation", "message": str(exc)}
        return all_navigation

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
        """Return retained messages from sync.db without acquiring Telegram facts."""
        try:
            request = self._parse_list_messages_request(req)
            if request.read_scope == "all_dialogs":
                selector = None
            else:
                selector = required_dialog_selector(
                    exact_id=req.get("dialog_id"),
                    dialog=req.get("dialog"),
                )
        except DialogSelectorError as exc:
            return _selector_error_response(exc)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_time_range", "message": str(exc)}
        if request.read_scope not in {"dialog", "all_dialogs"}:
            return {"ok": False, "error": "invalid_read_scope", "message": "read_scope must be dialog or all_dialogs"}
        if request.read_scope == "all_dialogs":
            return self._list_all_dialogs_scheduled(request, req)
        state_error = self._scheduled_list_filter_error(request)
        if state_error is not None:
            return state_error
        assert selector is not None
        return await self._list_messages_resolved(request, selector)

    def _list_all_dialogs_scheduled(self, request: _ListMessagesRequest, req: dict) -> dict:
        if request.message_state != "scheduled":
            return {
                "ok": False,
                "error": "invalid_read_scope",
                "message": 'read_scope="all_dialogs" is available only with message_state="scheduled".',
            }
        if (
            any(
                req.get(field) is not None
                for field in (
                    "dialog_id",
                    "dialog",
                    "topic_id",
                    "topic",
                    "exact_topic_id",
                    "anchor_message_id",
                    "context_message_id",
                )
            )
            or request.unread
        ):
            return {
                "ok": False,
                "error": "invalid_read_scope",
                "message": "All-dialog scheduled reads cannot include a dialog selector, topic, anchor, or unread filter.",
            }
        direction = request.direction if request.direction in {"newest", "oldest"} else "newest"
        return self._list_scheduled_queue(
            request,
            dialog_id=None,
            read_scope="all_dialogs",
            direction=direction,
        )

    @staticmethod
    def _scheduled_list_filter_error(request: _ListMessagesRequest) -> dict | None:
        if request.message_state == "scheduled" and request.unread:
            return {
                "ok": False,
                "error": "unread_state_unsupported",
                "message": 'message_state="scheduled" has no unread semantics. Action: retry with message_state="sent" or message_state="all".',
            }
        if request.message_state == "scheduled" and request.context_message_id is not None:
            return {
                "ok": False,
                "error": "scheduled_context_unsupported",
                "message": 'message_state="scheduled" does not support sent-history context windows. Action: retry with message_state="sent".',
            }
        return None

    async def _list_messages_resolved(self, request: _ListMessagesRequest, selector: DialogSelector) -> dict:
        direction = request.direction
        if direction not in ("newest", "oldest"):
            direction = "newest"

        context_error = request.context_filter_error()
        if context_error is not None:
            return context_error
        resolver = self._deps.resolve_dialog_id
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
        if request.message_state == "scheduled":
            result = self._list_scheduled_queue(
                request,
                dialog_id=dialog_id,
                read_scope="dialog",
                direction=direction,
            )
            if result.get("ok"):
                result["data"]["dialog_id"] = dialog_id
            with timing_phase("response_shape"):
                return self._attach_directory_coverage(result, getattr(resolved, "coverage", None))
        result = await self._list_messages_for_state(dialog_id, request, direction)
        if result.get("ok"):
            data = result.get("data")
            if isinstance(data, dict):
                data["dialog_id"] = dialog_id
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
                dataclasses.replace(request, offset=0, limit=request.offset + request.limit),
                stemmed,
            )
            return self._attach_directory_coverage(
                self._merge_search_results(
                    sent_result,
                    self._scheduled_search_for_mixed(
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
                    dataclasses.replace(request, offset=0, limit=request.offset + request.limit),
                    stemmed,
                ),
                self._scheduled_search_for_mixed(
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
        try:
            result = await self._search_messages_for_state(request, stemmed, selector)
        except _LocalSearchIndexUnavailableError:
            return {
                "ok": False,
                "error": "local_search_index_unavailable",
                "message": "The local message search index could not be queried.",
                "required_action": "Use list_messages to read retained local messages.",
            }
        if selector is None:
            result = self._attach_directory_coverage(result, read_dialog_directory_coverage(self._conn))
        return result

    async def list_unread_messages(self, req: dict[str, object]) -> dict:
        """Return prioritized unread messages across dialogs from sync.db."""
        timing = current_timing()
        if timing is not None:
            timing.set_route("local_inbox")
        with timing_phase("local_projection"):
            db_path = self._deps.sync_db_path
            if db_path is not None:
                return await asyncio.to_thread(self._list_unread_messages_from_reader, db_path, req, time.monotonic())
            return self._list_unread_messages_sync(self._conn, req)

    def _list_unread_messages_from_reader(self, db_path: Path, req: dict[str, object], submitted_at: float) -> dict:
        timing = current_timing()
        if timing is not None:
            timing.add_nested_phase("local_projection", "executor_queue", (time.monotonic() - submitted_at) * 1000)
        with timing.nested_phase("local_projection", "reader_open") if timing is not None else nullcontext():
            conn = open_sync_db_reader(db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            return self._list_unread_messages_sync(conn, req)
        finally:
            conn.close()

    def _list_unread_messages_sync(self, conn: sqlite3.Connection, req: dict[str, object]) -> dict:
        if "scope" in req:
            return {
                "ok": False,
                "error": "invalid_input",
                "message": "scope is not supported by get_inbox; use get_unread_summary for an overview",
            }
        limit = _clamp(_coerce_int(req.get("limit", 40), 40), 1, 100)
        page = _clamp(_coerce_int(req.get("page", 1), 1), 1, 2_147_483_647)
        group_size_threshold = _coerce_int(req.get("group_size_threshold", 100), 100)
        try:
            include_dialog_types = self._parse_dialog_type_allowlist(req.get("include_dialog_types"))
        except ValueError as exc:
            return {"ok": False, "error": "invalid_input", "message": str(exc)}
        try:
            since_utc = _parse_request_boundary(req, "since_utc")
        except ValueError as exc:
            return {"ok": False, "error": "invalid_input", "message": str(exc)}
        with timing_phase("dialog_selection", parent="local_projection"):
            entries, counts = self._collect_unread_dialogs(
                group_size_threshold, since_utc, include_dialog_types, conn=conn
            )
            self._rank_unread_entries(entries)
            page_data = self._paginate_unread_entries(
                entries, counts, page, _clamp(_coerce_int(req.get("dialogs_per_page", 20), 20), 1, 20)
            )
        with timing_phase("message_projection", parent="local_projection"):
            groups = self._fetch_unread_groups(
                conn,
                cast(list[dict], page_data["entries"]),
                allocate_message_budget_round_robin(
                    cast(dict[int, int], page_data["counts"]),
                    limit,
                    tiers={entry["chat_id"]: entry["tier"] for entry in cast(list[dict], page_data["entries"])},
                    max_per_chat=_clamp(_coerce_int(req.get("messages_per_dialog", 5), 5), 1, 20),
                ),
                since_utc,
            )
        with timing_phase("pending_read_positions", parent="local_projection"):
            pending_row = cast(tuple[object] | None, conn.execute(_COUNT_READ_POSITION_PENDING_SQL).fetchone())
            pending_count = int(cast(int | str, pending_row[0])) if pending_row else 0
            pending_ids = [
                int(cast(int | str, row[0]))
                for row in cast(list[tuple[object]], conn.execute(_READ_POSITION_PENDING_IDENTITIES_SQL).fetchall())
            ]
            pending_identities = read_dialog_identities(conn, pending_ids)
            pending_entities = [
                {
                    "dialog_id": dialog_id,
                    "display_name": pending_identities[dialog_id].display_name,
                    "username": pending_identities[dialog_id].username,
                    "display_name_source": pending_identities[dialog_id].display_name_source,
                    "name_unavailable_reason": pending_identities[dialog_id].name_unavailable_reason,
                }
                for dialog_id in pending_ids
            ]
        return {
            "ok": True,
            "data": {
                "groups": groups,
                "page": page,
                "page_size": page_data["page_size"],
                "total_dialog_count": page_data["total_dialog_count"],
                "remaining_dialog_count": page_data["remaining_dialog_count"],
                "next_page": page_data["next_page"],
                "total_message_count": sum(counts.values()),
                "page_message_count": sum(cast(dict[int, int], page_data["counts"]).values()),
                "read_position_pending_count": pending_count,
                "read_position_pending_entities": pending_entities,
            },
        }

    @staticmethod
    def _paginate_unread_entries(
        entries: list[dict], counts: dict[int, int], page: int, page_size: int
    ) -> dict[str, object]:
        total_dialog_count = len(entries)
        page_start = (page - 1) * page_size
        page_entries = entries[page_start : page_start + page_size]
        page_counts = {int(entry["chat_id"]): counts[int(entry["chat_id"])] for entry in page_entries}
        shown_dialog_count = len(page_entries)
        return {
            "entries": page_entries,
            "counts": page_counts,
            "page_size": page_size,
            "total_dialog_count": total_dialog_count,
            "remaining_dialog_count": max(0, total_dialog_count - page_start - shown_dialog_count),
            "next_page": page + 1 if page_start + shown_dialog_count < total_dialog_count else None,
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

    @staticmethod
    def _eligible_unread_dialogs(
        conn: sqlite3.Connection,
        group_size_threshold: int,
        include_dialog_types: tuple[DialogType, ...] | None,
    ) -> dict[int, DialogIdentity]:
        candidates = cast(
            list[tuple[int, int | None]],
            conn.execute(_COLLECT_UNREAD_DIALOG_CANDIDATES_SQL).fetchall(),
        )
        identities = read_dialog_identities(conn, [row[0] for row in candidates])
        return {
            dialog_id: identities[dialog_id]
            for dialog_id, participants_count in candidates
            if (include_dialog_types is None or identities[dialog_id].dialog_type in include_dialog_types)
            and ReadingService._should_include_unread_dialog(
                identities[dialog_id].dialog_type,
                participants_count,
                group_size_threshold,
                allow_channel=include_dialog_types is not None and DialogType.CHANNEL in include_dialog_types,
            )
        }

    def _collect_unread_dialogs(
        self,
        group_size_threshold: int,
        since_utc: int | None = None,
        include_dialog_types: tuple[DialogType, ...] | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[list[dict], dict[int, int]]:
        if conn is None:
            conn = self._conn
        identities = ReadingService._eligible_unread_dialogs(conn, group_size_threshold, include_dialog_types)
        if not identities:
            return [], {}
        rows = cast(
            list[tuple[object, object, object, object, object]],
            conn.execute(
                _COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL,
                {
                    "dialog_ids": json.dumps(list(identities)),
                    "since_utc": since_utc,
                    "deleted_since_utc": int(time.time()) - self._deps.deleted_message_visibility_seconds,
                },
            ).fetchall(),
        )
        mention_counts = ReadingService._unread_mention_counts(conn, rows)
        entries: list[dict] = []
        counts: dict[int, int] = {}
        for row in rows:
            dialog_id_i = int(cast(int | str, row[0]))
            identity = identities[dialog_id_i]
            unread_count_i = int(cast(int | str, row[4]))
            if unread_count_i == 0:
                continue
            category = identity.dialog_type
            entries.append(
                {
                    "chat_id": dialog_id_i,
                    "display_name": identity.display_name,
                    "username": identity.username,
                    "display_name_source": identity.display_name_source,
                    "name_unavailable_reason": identity.name_unavailable_reason,
                    "dialog_type": category.value,
                    "unread_count": unread_count_i,
                    "unread_mentions_count": mention_counts.get(dialog_id_i, 0),
                    "category": category,
                    "date": row[2],
                    "read_inbox_max_id": row[1],
                }
            )
            counts[dialog_id_i] = unread_count_i
        return entries, counts

    @staticmethod
    def _unread_mention_counts(conn: sqlite3.Connection, rows: Sequence[tuple[object, ...]]) -> dict[int, int]:
        table_info = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(dialogs)").fetchall())
        dialog_columns = {str(row[1]) for row in table_info}
        if not rows or "unread_mentions_count" not in dialog_columns:
            return {}
        dialog_ids = [int(cast(int | str, row[0])) for row in rows]
        placeholders = ",".join("?" for _ in dialog_ids)
        mention_rows = cast(
            list[tuple[object, object]],
            conn.execute(
                f"SELECT dialog_id, unread_mentions_count FROM dialogs WHERE dialog_id IN ({placeholders})",
                dialog_ids,
            ).fetchall(),
        )
        return {int(cast(int | str, dialog_id)): int(cast(int | str, count or 0)) for dialog_id, count in mention_rows}

    @staticmethod
    def _rank_unread_entries(entries: list[dict]) -> None:
        for entry in entries:
            entry["tier"] = unread_chat_tier(
                {"unread_mentions_count": entry["unread_mentions_count"], "category": entry["category"]}
            )
        entries.sort(key=lambda entry: (entry["tier"], -(entry["date"] or 0), entry["chat_id"]))

    def _fetch_unread_groups(
        self,
        conn: sqlite3.Connection,
        entries: list[dict],
        allocation: dict[int, int],
        since_utc: int | None = None,
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
                "name_unavailable_reason": entry["name_unavailable_reason"],
                "tier": entry["tier"],
                "category": entry["category"],
                "unread_count": entry["unread_count"],
                "unread_mentions_count": entry["unread_mentions_count"],
                "dialog_type": dialog_type,
                "read_state": _read_state_for_dialog(conn, chat_id, dialog_type),
                "messages": [],
            }
            if budget:
                rows = cast(
                    list[Mapping[str, object]],
                    conn.execute(
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
                messages, freshness = self._enrich_unread_rows(conn, chat_id, rows)
                group["messages"] = [dataclasses.asdict(message) for message in reversed(messages)]
                if freshness is not None:
                    group["reaction_freshness"] = freshness.as_dict()
            groups.append(group)
        return groups

    @staticmethod
    def _enrich_unread_rows(
        conn: sqlite3.Connection, dialog_id: int, rows: list[Mapping[str, object]]
    ) -> tuple[list[ReadMessage], ReactionFreshness | None]:
        messages = [read_message_from_row(row) for row in rows]
        if not messages:
            return messages, None
        enriched = project_cached_message_facts(conn, dialog_id, messages)
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
                        "name_unavailable_reason": identity.name_unavailable_reason,
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

    def _list_dialogs_page_transaction(self, conn: sqlite3.Connection, req: dict) -> dict:
        with read_snapshot(conn):
            return self._list_dialogs_page(conn, req)

    def _list_dialogs_page(self, conn: sqlite3.Connection, req: dict) -> dict:
        account = req.get("verified_account_id")
        if type(account) is not int or account <= 0:
            return {
                "ok": False,
                "error": "account_unverified",
                "message": "Verified account unavailable.",
                "required_action": "Retry after account verification.",
            }
        owner = cast(
            tuple[object] | None,
            conn.execute("SELECT account_id FROM dialog_directory_state WHERE singleton=1").fetchone(),
        )
        if owner is None or owner[0] != account:
            return {
                "ok": False,
                "error": "account_unverified",
                "message": "Local account ownership is unavailable or changed.",
                "required_action": "Verify the account.",
            }
        input_error = self._directory_request_error(req)
        if input_error is not None:
            return input_error
        request = self._parse_list_dialogs_request(req)
        error = self._list_dialogs_request_error(request)
        if error is not None:
            return error
        try:
            return self._build_dialogs_page(conn, req, request, account)
        except ValueError as exc:
            return {
                "ok": False,
                "error": "invalid_navigation",
                "message": str(exc),
                "required_action": "Restart navigation.",
            }

    @staticmethod
    def _directory_request_error(req: dict) -> dict | None:
        return (
            ReadingService._directory_selector_error(req)
            or ReadingService._directory_bounds_error(req)
            or ReadingService._directory_navigation_error(req)
        )

    @staticmethod
    def _directory_selector_error(req: dict) -> dict | None:
        valid = {item.value for item in DialogType}
        if req.get("dialog_type") is not None and (
            not isinstance(req["dialog_type"], str) or req["dialog_type"] not in valid
        ):
            return {"ok": False, "error": "invalid_input", "message": "Unknown dialog type."}
        for key in ("exclude_self", "exclude_archived", "ignore_pinned"):
            if key in req and type(req[key]) is not bool:
                return {"ok": False, "error": "invalid_input", "message": f"{key} must be a boolean."}
        if req.get("filter") is not None and not isinstance(req["filter"], str):
            return {"ok": False, "error": "invalid_input", "message": "filter must be a string."}
        return None

    @staticmethod
    def _directory_bounds_error(req: dict) -> dict | None:
        error = ReadingService._directory_integer_error(req)
        if error is not None:
            return error
        if req.get("limit") is not None and not 1 <= req["limit"] <= _DIRECTORY_PAGE_LIMIT:
            return {"ok": False, "error": "invalid_input", "message": "limit must be between 1 and 500."}
        lower, upper = req.get("last_message_since_utc"), req.get("last_message_until_utc")
        if lower is not None and upper is not None and lower > upper:
            return {"ok": False, "error": "invalid_input", "message": "Time bounds must not be reversed."}
        if req.get("folder_id") is not None and req["folder_id"] < 0:
            return {"ok": False, "error": "invalid_input", "message": "folder_id must be nonnegative."}
        return None

    @staticmethod
    def _directory_integer_error(req: dict) -> dict | None:
        for key in ("folder_id", "last_message_since_utc", "last_message_until_utc", "limit"):
            if req.get(key) is not None and type(req[key]) is not int:
                return {"ok": False, "error": "invalid_input", "message": f"{key} must be an integer."}
        return None

    @staticmethod
    def _directory_navigation_error(req: dict) -> dict | None:
        navigation = req.get("navigation")
        if navigation is not None and (not isinstance(navigation, str) or not navigation):
            return {
                "ok": False,
                "error": "invalid_navigation",
                "message": "Invalid navigation.",
                "required_action": "Restart navigation.",
            }
        return None

    def _build_dialogs_page(
        self, conn: sqlite3.Connection, req: dict, request: _ListDialogsRequest, account: int
    ) -> dict:
        dialog_filter = self._prepare_list_dialogs_filter(request.filter_raw)
        fingerprint = self._directory_fingerprint(req, request, dialog_filter)
        rows, upper = self._directory_candidate_rows(conn, req, request, account, fingerprint)
        candidates = [cast(Mapping[str, object], row) for row in rows[:500]]
        directory_coverage = read_dialog_directory_coverage(conn).to_wire()
        scheduled_coverage = self._directory_account_coverage(conn, account, directory_coverage)
        selected, examined = self._directory_select_candidates(
            conn, req, request, account, dialog_filter, candidates, scheduled_coverage
        )
        has_more = examined is not None and (
            len(rows) > len(candidates) or examined != _object_to_int(candidates[-1]["dialog_id"])
        )
        return self._finish_dialogs_page(
            conn,
            req,
            request,
            account,
            fingerprint,
            upper,
            examined,
            has_more,
            selected,
            scheduled_coverage,
            directory_coverage,
        )

    @staticmethod
    def _directory_fingerprint(req: dict, request: _ListDialogsRequest, dialog_filter: _ListDialogsFilter) -> str:
        payload = [
            request.exclude_archived,
            request.ignore_pinned,
            dialog_filter.normalized,
            dialog_filter.raw_lower,
            request.message_state,
            request.scope,
            req.get("folder_id"),
            req.get("dialog_type"),
            req.get("exclude_self", False),
            req.get("last_message_since_utc"),
            req.get("last_message_until_utc"),
        ]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _directory_candidate_rows(
        conn: sqlite3.Connection, req: dict, request: _ListDialogsRequest, account: int, fingerprint: str
    ) -> tuple[list[object], int | None]:
        params = {
            "archived_filter": 0 if request.exclude_archived else None,
            "pinned_filter": 0 if request.ignore_pinned else None,
        }
        navigation = req.get("navigation")
        upper: int | None
        after: int | None
        if navigation not in (None, "start"):
            cursor = decode_dialog_directory_navigation(navigation)
            if cursor.account_id != account or cursor.fingerprint != fingerprint:
                raise ValueError("Navigation token belongs to a different account or selection")
            upper, after = cursor.upper_dialog_id, cursor.last_dialog_id
        else:
            result = cast(tuple[int | None], conn.execute(_DIALOG_DIRECTORY_UPPER_SQL, params).fetchone())
            upper, after = result[0], None
        if upper is None:
            return [], None
        return _fetchall_rows(
            conn.execute(
                _DIALOG_DIRECTORY_FIRST_PAGE_SQL if after is None else _DIALOG_DIRECTORY_PAGE_SQL,
                {**params, "after_id": after, "upper_id": upper},
            )
        ), upper

    def _directory_account_coverage(self, conn: sqlite3.Connection, account: int, directory: dict) -> dict[str, object]:
        result = self._scheduled_account_coverage(conn, account_id=account)
        if not isinstance(result, dict):
            return result[1]
        return {
            "state": "unknown",
            "account_id": account,
            "directory_generation": directory.get("publication_generation"),
            "reason": result.get("error"),
            "directory_coverage": directory,
        }

    def _directory_select_candidates(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        req: dict,
        request: _ListDialogsRequest,
        account: int,
        dialog_filter: _ListDialogsFilter,
        candidates: list[Mapping[str, object]],
        scheduled_coverage: dict[str, object],
    ) -> tuple[list, int | None]:
        ids = [_object_to_int(row["dialog_id"]) for row in candidates]
        identities = read_dialog_identities(conn, ids)
        summaries = (
            scheduled_summary_by_dialog(conn, scheduled_now=int(time.time()), dialog_ids=ids)
            if request.message_state == "scheduled" and scheduled_coverage.get("state") != "unknown"
            else {}
        )
        own = own_only_basis_by_dialog(conn, ids) if request.scope == "own_only" else {}
        folders = folders_by_dialog(conn, ids) if req.get("folder_id") not in (None, 1) else {}
        archived = self._directory_archived_facts(conn, ids) if req.get("folder_id") == 1 else {}
        selectable = self._select_list_dialog_rows(
            candidates, request, dialog_filter, summaries, own, identities, conn, {}
        )
        selected_by_id = {_object_to_int(item[0]["dialog_id"]): item for item in selectable}
        return self._directory_take_candidates(req, candidates, identities, folders, archived, account, selected_by_id)

    @staticmethod
    def _directory_take_candidates(  # noqa: PLR0913, PLR0917
        req: dict,
        candidates: list[Mapping[str, object]],
        identities: Mapping[int, DialogIdentity],
        folders: Mapping[int, list[dict[str, object]]],
        archived: Mapping[int, bool | None],
        account: int,
        selected_by_id: dict,
    ) -> tuple[list, int | None]:
        limit = req.get("limit") or 100
        if type(limit) is not int or not 1 <= limit <= _DIRECTORY_PAGE_LIMIT:
            raise ValueError("Invalid page limit")
        selected = []
        examined = None
        for row in candidates:
            dialog_id = _object_to_int(row["dialog_id"])
            examined = dialog_id
            item = selected_by_id.get(dialog_id)
            if item is None or not ReadingService._directory_page_matches(
                req, row, identities[dialog_id], folders, archived, account
            ):
                continue
            selected.append(item)
            if len(selected) == limit:
                break
        return selected, examined

    @staticmethod
    def _directory_archived_facts(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, bool | None]:
        if not ids:
            return {}
        rows = cast(
            list[tuple[int, object]],
            conn.execute(
                "SELECT d.dialog_id, COALESCE(f.archived,d.archived) FROM dialogs d LEFT JOIN dialog_directory_facts f USING(dialog_id) WHERE d.hidden=0 AND d.dialog_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))",
                (json.dumps(ids),),
            ).fetchall(),
        )
        return {int(row[0]): None if row[1] is None else bool(row[1]) for row in rows}

    @staticmethod
    def _directory_page_matches(  # noqa: PLR0913, PLR0917
        req: dict,
        row: Mapping[str, object],
        identity: DialogIdentity,
        folders: Mapping[int, list[dict[str, object]]],
        archived: Mapping[int, bool | None],
        account: int,
    ) -> bool:
        dialog_id = _object_to_int(row["dialog_id"])
        if req.get("dialog_type") is not None and identity.dialog_type.value != req["dialog_type"]:
            return False
        if req.get("exclude_self") and dialog_id == account:
            return False
        if not ReadingService._directory_time_matches(req, row):
            return False
        return ReadingService._directory_folder_matches(req, dialog_id, folders, archived)

    @staticmethod
    def _directory_time_matches(req: dict, row: Mapping[str, object]) -> bool:
        value = _object_to_int_or_none(row["last_message_at"])
        lower, upper = req.get("last_message_since_utc"), req.get("last_message_until_utc")
        return not (
            (lower is not None and (value is None or value < lower))
            or (upper is not None and (value is None or value >= upper))
        )

    @staticmethod
    def _directory_folder_matches(
        req: dict, dialog_id: int, folders: Mapping[int, list[dict[str, object]]], archived: Mapping[int, bool | None]
    ) -> bool:
        folder_id = req.get("folder_id")
        if folder_id == 1:
            return archived.get(dialog_id) is True
        if folder_id is not None:
            return any(item["id"] == folder_id for item in folders.get(dialog_id, []))
        return True

    def _finish_dialogs_page(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        req: dict,
        request: _ListDialogsRequest,
        account: int,
        fingerprint: str,
        upper: int | None,
        examined: int | None,
        has_more: bool,
        selected: list,
        scheduled_coverage: dict[str, object],
        directory_coverage: dict,
    ) -> dict:
        ids = [_object_to_int(item[0]["dialog_id"]) for item in selected]
        summaries = (
            scheduled_summary_by_dialog(conn, scheduled_now=int(time.time()), dialog_ids=ids)
            if scheduled_coverage.get("state") != "unknown"
            else {}
        )
        own = own_only_basis_by_dialog(conn, ids)
        coverage = self._directory_scheduled_coverages(conn, ids, account)
        selected = [
            (
                {**row, "scheduled_coverage": coverage[dialog_id]},
                summaries.get(dialog_id, (0, None, 0)),
                own.get(dialog_id),
            )
            for row, _, _ in selected
            for dialog_id in [_object_to_int(row["dialog_id"])]
        ]
        result = self._project_list_dialog_rows(conn, selected, request, directory_coverage, scheduled_coverage)
        data = result["data"]
        self._directory_add_page_metadata(conn, req, account, fingerprint, upper, examined, has_more, ids, data)
        return result

    def _directory_add_page_metadata(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        req: dict,
        account: int,
        fingerprint: str,
        upper: int | None,
        examined: int | None,
        has_more: bool,
        ids: list[int],
        data: dict,
    ) -> None:
        folder_id = req.get("folder_id")
        folders = folders_by_dialog(conn, ids) if folder_id != 1 else {}
        for dialog in data["dialogs"]:
            dialog_id = dialog["id"]
            placements = [{"id": 1, "title": "Archived"}] if folder_id == 1 else folders.get(dialog_id, [])
            dialog.update(
                folder_ids=[item["id"] for item in placements], folders=placements, is_self=dialog_id == account
            )
        data["folder_snapshot"] = folder_snapshot(
            conn, stale_after_seconds=req.get("folder_snapshot_stale_after_seconds", 900)
        )
        data["folder_membership_unknown_count"] = self._directory_folder_unknown_count(conn, folder_id)
        data["navigation"] = {
            "has_more": has_more,
            "order": "dialog_id_asc",
            "consistency": "live",
            "next_navigation": encode_dialog_directory_navigation(
                DialogDirectoryNavigationToken(account, fingerprint, examined, upper)
            )
            if has_more and examined is not None and upper is not None
            else None,
        }
        if not ids and upper is None:
            data["bootstrap_pending"] = count_dialog_rows(conn) == 0

    @staticmethod
    def _directory_folder_unknown_count(conn: sqlite3.Connection, folder_id: int | None) -> int:
        if folder_id is None:
            return 0
        if folder_id == 1:
            return cast(
                tuple[int],
                conn.execute(
                    "SELECT COUNT(*) FROM dialogs d LEFT JOIN dialog_directory_facts f USING(dialog_id) WHERE d.hidden=0 AND COALESCE(f.archived,d.archived) IS NULL"
                ).fetchone(),
            )[0]
        namespace = "default" if folder_id == 0 else "filter"
        return cast(
            tuple[int],
            conn.execute(
                "SELECT COUNT(*) FROM telegram_folder_local_members WHERE namespace=? AND folder_id=? AND state='unknown'",
                (namespace, folder_id),
            ).fetchone(),
        )[0]

    @staticmethod
    def _directory_scheduled_coverages(
        conn: sqlite3.Connection, ids: Sequence[int], account: int
    ) -> dict[int, dict[str, object]]:
        if not ids:
            return {}
        try:
            bound = cast(
                tuple[object] | None,
                conn.execute("SELECT account_id FROM scheduled_sync_state WHERE key='account'").fetchone(),
            )
            if bound is None or bound[0] != account:
                return {dialog_id: {"state": "unknown", "reason": "scheduled_account_not_bound"} for dialog_id in ids}
            rows = cast(
                list[tuple[int, object, object, object, object, object, object]],
                conn.execute(
                    "SELECT dialog_id,coverage_state,coverage_reason,baseline_observed_at,directory_generation,repair_due_at,discovery_due_at FROM scheduled_reconciliation_state WHERE dialog_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))",
                    (json.dumps(ids),),
                ).fetchall(),
            )
        except sqlite3.Error:
            rows = []
        result: dict[int, dict[str, object]] = {
            dialog_id: {
                "state": "unknown",
                "reason": "baseline_missing",
                "baseline_observed_at": None,
                "directory_generation": None,
                "acquisition_pending": False,
            }
            for dialog_id in ids
        }
        for row in rows:
            result[int(row[0])] = {
                "state": row[1],
                "reason": row[2],
                "baseline_observed_at": row[3],
                "directory_generation": row[4],
                "acquisition_pending": row[5] is not None or row[6] is not None,
            }
        return result

    @staticmethod
    def _is_dialogs_page(req: dict) -> bool:
        return (
            any(
                req.get(key) is not None
                for key in ("navigation", "dialog_type", "last_message_since_utc", "last_message_until_utc")
            )
            or req.get("exclude_self", False) is not False
        )

    def _list_dialogs_sync(self, conn: sqlite3.Connection, req: dict) -> dict:
        """Return dialog list from the local dialogs snapshot (pure SQL)."""
        if self._is_dialogs_page(req):
            return self._list_dialogs_page_transaction(conn, req)
        directory_coverage = read_dialog_directory_coverage(conn).to_wire()
        request = self._parse_list_dialogs_request(req)
        request_error = self._list_dialogs_request_error(request)
        if request_error is not None:
            return request_error
        dialog_filter = self._prepare_list_dialogs_filter(request.filter_raw)
        coverage_result = self._scheduled_account_coverage(conn)
        if isinstance(coverage_result, dict):
            scheduled_summary: dict[int, tuple[int, int | None, int]] = {}
            scheduled_coverage = {
                "state": "unknown",
                "account_id": self._deps.self_id,
                "directory_generation": directory_coverage.get("publication_generation"),
                "reason": coverage_result.get("error"),
                "directory_coverage": directory_coverage,
            }
        else:
            _, scheduled_coverage = coverage_result
            scheduled_summary = (
                {}
                if scheduled_coverage.get("state") == "unknown"
                else scheduled_summary_by_dialog(conn, scheduled_now=int(time.time()))
            )
        own_basis: dict[int, tuple[str, ...]] = self._own_only_basis_by_dialog(conn)
        sql_rows = self._fetch_list_dialog_rows(conn, request, dialog_filter)
        if not sql_rows:
            count_total = count_dialog_rows(conn)
            return self._empty_list_dialogs_response(
                request.scope,
                directory_coverage,
                scheduled_coverage,
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
            conn,
        )
        if not selected_rows:
            return self._empty_list_dialogs_response(
                request.scope,
                directory_coverage,
                scheduled_coverage,
                bootstrap_pending=False,
            )
        return self._project_list_dialog_rows(conn, selected_rows, request, directory_coverage, scheduled_coverage)
