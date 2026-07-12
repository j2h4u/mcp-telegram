"""Reading-domain service for daemon read/search/list handlers."""

import asyncio
import dataclasses
import inspect
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from rapidfuzz import fuzz as _fuzz
from telethon.errors import (
    FloodWaitError,  # type: ignore[import-untyped]
    RPCError,  # type: ignore[import-untyped]
)

from .daemon_account_trace import (
    _TRACE_ACRONYM_MAX_LEN,
    _TRACE_ACRONYM_MIN_LEN,
    _TRACE_FUZZY_MIN_LEN,
    _TRACE_FUZZY_SCORE_MIN,
)
from .daemon_activity_stats import _SELECT_SYNC_STATUS_SQL
from .daemon_dialog_queries import (
    _BATCHED_UNREAD_COUNTS_SQL,
    _COUNT_MESSAGES_BY_DIALOG_SQL,
    _GET_READ_POSITION_SQL,
    _LIST_DIALOGS_SQL,
    _build_access_metadata,
    _compute_snapshot_age_h,
    _compute_sync_coverage,
)
from .daemon_message import REACTIONS_TTL_SECONDS, fetch_reaction_counts, message_to_dict
from .daemon_message import _MessageLike as _DaemonMessageLike
from .daemon_message_queries import (
    _LIST_MESSAGES_BASE_SQL,
    _SELECT_FTS_ALL_SQL,
    _SELECT_FTS_SQL,
    _build_list_messages_query,
)
from .daemon_read_state_queries import _dialog_type_from_db, _read_state_for_dialog
from .formatter import format_reaction_counts
from .fts import stem_query
from .models import DialogType, ReadMessage, ReadState
from .own_only import own_only_basis_by_dialog
from .pagination import (
    HistoryDirection,
    NavigationToken,
    decode_navigation_token,
    encode_history_navigation,
    encode_search_navigation,
)
from .resolver import latinize
from .sync_db import open_sync_db_reader


class _LoggerLike(Protocol):
    def debug(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def info(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def warning(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def exception(self, msg: str, *args: object, **kwargs: object) -> None: ...


def _safe_exception_message(exc: BaseException) -> str:
    message = str(exc).replace("\n", "\\n")
    if not message:
        return type(exc).__name__
    return message


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp *value* to the inclusive range [low, high]."""
    return max(low, min(value, high))


def _log_recoverable_telegram_error(
    logger: _LoggerLike,
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


class _TelegramClientLike(Protocol):
    async def get_messages(self, entity: object, ids: list[int]) -> object: ...

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]: ...


@dataclass(frozen=True)
class DaemonReadingDeps:
    """Dependencies for ``DaemonReadingService``."""

    conn: sqlite3.Connection
    sync_db_path: Path | None
    client: _TelegramClientLike
    self_id: int | None
    resolve_dialog_id: Callable[[int, str | None], Awaitable[int | dict]]
    fetch_fragment_context: Callable[[int, int], Awaitable[bool]]
    logger: _LoggerLike
    rid: Callable[[], str]


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


_SCHEDULED_MESSAGES_TABLE_SQL = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_messages'"
_SCHEDULED_MESSAGES_FTS_TABLE_SQL = (
    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_messages_fts'"
)
_OWN_ONLY_DIALOGS_TABLE_SQL = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'own_only_dialogs'"

_SCHEDULED_MESSAGES_SQL = """
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
FROM scheduled_messages sm
LEFT JOIN dialogs d ON d.dialog_id = sm.dialog_id
WHERE sm.dialog_id = :dialog_id
  AND sm.message_state = 'scheduled'
"""


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


@dataclass(frozen=True)
class _SearchMessagesRequest:
    dialog_id: int
    dialog: str | None
    query: str
    limit: int
    offset: int
    navigation: str | None
    message_state: str


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
    name_pat: str | None


@dataclass(frozen=True)
class _NextNavContext:
    messages: Sequence[object]
    limit: int
    dialog_id: int
    direction: str
    direction_enum: HistoryDirection
    logger: _LoggerLike
    request_id: Callable[[], str]
    topic_id: int | None = None
    message_state: str = "sent"


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


def _read_message_from_row(row: object) -> ReadMessage:
    return ReadMessage(
        message_id=_object_to_int(cast(object | None, _row_value(row, "message_id"))),
        sent_at=_object_to_int(cast(object | None, _row_value(row, "sent_at"))),
        dialog_id=_object_to_int(cast(object | None, _row_value(row, "dialog_id"))),
        text=_object_to_str_or_none(cast(object | None, _row_value(row, "text"))),
        sender_id=_object_to_int_or_none(cast(object | None, _row_value(row, "sender_id"))),
        sender_first_name=_object_to_str_or_none(cast(object | None, _row_value(row, "sender_first_name"))),
        media_description=_object_to_str_or_none(cast(object | None, _row_value(row, "media_description"))),
        reply_to_msg_id=_object_to_int_or_none(cast(object | None, _row_value(row, "reply_to_msg_id"))),
        forum_topic_id=_object_to_int_or_none(cast(object | None, _row_value(row, "forum_topic_id"))),
        is_deleted=_object_to_int(cast(object | None, _row_value(row, "is_deleted")), 0),
        deleted_at=_object_to_int_or_none(cast(object | None, _row_value(row, "deleted_at"))),
        edit_date=_object_to_int_or_none(cast(object | None, _row_value(row, "edit_date"))),
        topic_title=_object_to_str_or_none(cast(object | None, _row_value(row, "topic_title"))),
        effective_sender_id=_object_to_int_or_none(cast(object | None, _row_value(row, "effective_sender_id"))),
        is_service=_object_to_int(cast(object | None, _row_value(row, "is_service")), 0),
        out=_object_to_int(cast(object | None, _row_value(row, "out")), 0),
        fwd_from_name=_object_to_str_or_none(cast(object | None, _row_value(row, "fwd_from_name"))),
        post_author=_object_to_str_or_none(cast(object | None, _row_value(row, "post_author"))),
        dialog_name=_object_to_str_or_none(cast(object | None, _row_value(row, "dialog_name"))),
    )


def _status_from_row(row: object | None) -> str | None:
    if row is None:
        return None
    values = _row_sequence(row)
    if not values:
        return None
    value = values[0]
    return None if value is None else str(value)


class DaemonReadingService:
    """Domain service for list/search/list_dialogs and helper operations."""

    def __init__(self, deps: DaemonReadingDeps) -> None:
        self._deps = deps

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._deps.conn

    @property
    def _logger(self) -> _LoggerLike:
        return self._deps.logger

    @staticmethod
    def _parse_list_messages_request(req: dict) -> _ListMessagesRequest:
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
            message_state=req.get("message_state", "sent"),
        )

    @staticmethod
    def _parse_search_messages_request(req: dict) -> _SearchMessagesRequest:
        return _SearchMessagesRequest(
            dialog_id=req.get("dialog_id", 0) or 0,
            dialog=req.get("dialog"),
            query=req.get("query", ""),
            limit=_clamp(req.get("limit", 20), 1, 200),
            offset=max(0, req.get("offset", 0)),
            navigation=req.get("navigation"),
            message_state=req.get("message_state", "sent"),
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
        name_pat: str | None = None
        normalized: str | None = None
        if filter_raw is not None:
            stripped = filter_raw.strip()
            if stripped:
                normalized = latinize(stripped)
                raw_lower = stripped.lower()
                if stripped.isascii():
                    escaped = stripped.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    name_pat = f"%{escaped}%"
        return _ListDialogsFilter(
            raw=filter_raw,
            normalized=normalized,
            raw_lower=raw_lower,
            name_pat=name_pat,
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
                sent_at=DaemonReadingService._navigation_sent_at(last),
                message_state=context.message_state,
            )
        return None

    @staticmethod
    def _navigation_sent_at(message: object) -> int | None:
        if isinstance(message, ReadMessage):
            return message.sent_at
        return _object_to_int_or_none(_row_value(message, "sent_at"))

    @staticmethod
    def _decode_history_navigation(
        navigation: str | None,
        dialog_id: int,
        direction: str,
        message_state: str,
        topic_id: int | None,
    ) -> tuple[int | None, str] | dict:
        """Decode a history navigation token into (anchor_msg_id, direction)."""
        anchor_msg_id: int | None = None
        if navigation and navigation not in ("newest", "oldest"):
            try:
                nav = decode_navigation_token(navigation)
            except ValueError as exc:
                return {"ok": False, "error": "invalid_navigation", "message": str(exc)}
            error_message = DaemonReadingService._history_navigation_error(
                nav,
                dialog_id,
                message_state,
                topic_id,
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
    def _history_navigation_error(
        navigation: NavigationToken,
        dialog_id: int,
        message_state: str,
        topic_id: int | None,
    ) -> str | None:
        if navigation.kind != "history":
            return f"Navigation token is for {navigation.kind}, not history"
        if navigation.dialog_id != dialog_id:
            return f"Navigation token belongs to dialog {navigation.dialog_id}, not {dialog_id}"
        if navigation.message_state != message_state:
            return f"Navigation token belongs to message_state {navigation.message_state!r}, not {message_state!r}"
        if navigation.topic_id != topic_id:
            return f"Navigation token belongs to topic {navigation.topic_id!r}, not {topic_id!r}"
        return None

    async def _build_read_messages_from_rows(
        self,
        dialog_id: int,
        rows: Sequence[object],
        *,
        log_rendered: bool,
    ) -> list[ReadMessage]:
        msg_ids = [_message_id_from_item(r) for r in rows]
        if msg_ids:
            await self._freshen_reactions_if_stale(dialog_id, dialog_id, msg_ids)
        reaction_map = fetch_reaction_counts(self._conn, dialog_id, msg_ids)
        messages: list[ReadMessage] = []
        for r in rows:
            message = _read_message_from_row(r)
            reaction_key = message.message_id
            messages.append(
                dataclasses.replace(
                    message,
                    reactions_display=format_reaction_counts(reaction_map[reaction_key])
                    if reaction_key in reaction_map
                    else "",
                )
            )
        if log_rendered:
            null_sender_rows = sum(1 for m in messages if m.sender_id is None)
            unresolved_entity_rows = sum(1 for m in messages if m.sender_id is not None and m.sender_first_name is None)
            self._logger.info(
                "list_messages rendered",
                extra={
                    "dialog_id": dialog_id,
                    "rows": len(messages),
                    "null_sender_rows": null_sender_rows,
                    "unresolved_entity_rows": unresolved_entity_rows,
                },
            )
        return messages

    def _read_state_per_dialog(self, messages: list[ReadMessage]) -> dict[int, ReadState]:
        read_state_per_dialog: dict[int, ReadState] = {}
        for dialog_id in {m.dialog_id for m in messages if m.dialog_id}:
            dialog_type = _dialog_type_from_db(self._conn, dialog_id)
            read_state = _read_state_for_dialog(self._conn, dialog_id, dialog_type)
            if read_state is not None:
                read_state_per_dialog[dialog_id] = read_state
        return read_state_per_dialog

    async def _list_messages_context_result(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
    ) -> dict:
        row = _fetchone_row(self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)))
        current_status = _status_from_row(row)
        if current_status in (None, "not_synced", "fragment", "own_only"):
            if not await self._deps.fetch_fragment_context(dialog_id, request.context_message_id or 0):
                return {
                    "ok": False,
                    "error": "fragment_fetch_failed",
                    "message": "Could not fetch bounded context from Telegram.",
                    "required_action": "Retry with a valid anchor_message_id, or mark the dialog for sync if broader history is needed.",
                    "context_availability": "fragment_unavailable",
                    "dialog_status": current_status or "not_synced",
                }
            result = await self._list_messages_context_window(
                dialog_id=dialog_id,
                anchor_message_id=request.context_message_id or 0,
                context_size=request.context_size,
            )
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
                "message": "Context window is unavailable for this dialog state.",
                "required_action": "Mark the dialog for sync to read broader history, or retry with an anchor_message_id for bounded fragment context.",
                "context_availability": "context_window_unavailable",
                "dialog_status": current_status or "not_synced",
            }
        return await self._list_messages_context_window(
            dialog_id=dialog_id,
            anchor_message_id=request.context_message_id or 0,
            context_size=request.context_size,
        )

    async def _list_messages_history_result(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        direction: str,
    ) -> dict:
        nav_result = self._decode_history_navigation(
            request.navigation,
            dialog_id,
            direction,
            request.message_state,
            request.topic_id,
        )
        if isinstance(nav_result, dict):
            return nav_result
        anchor_msg_id, direction = nav_result

        direction_enum = HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST
        unread_after_id = request.unread_after_id
        if request.unread:
            unread_after_id = await self._resolve_unread_position(dialog_id, request.unread_after_id)

        row = _fetchone_row(self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)))
        status = _status_from_row(row)

        dialog_type = _dialog_type_from_db(self._conn, dialog_id)
        read_state = _read_state_for_dialog(self._conn, dialog_id, dialog_type)

        if status in ("synced", "syncing", "access_lost"):
            result = await self._list_messages_from_db(
                _ListMessagesDbRequest(
                    dialog_id=dialog_id,
                    limit=request.limit,
                    self_id=self._deps.self_id,
                    direction=direction,
                    direction_enum=direction_enum,
                    anchor_msg_id=anchor_msg_id,
                    anchor_sent_at=None,
                    sender_id=request.sender_id,
                    sender_name=request.sender_name,
                    topic_id=request.topic_id,
                    unread_after_id=unread_after_id,
                )
            )
            result["data"].update(_build_access_metadata(self._conn, dialog_id, status))
            result["data"]["dialog_type"] = dialog_type
            result["data"]["read_state"] = read_state
            return result

        telegram_result = await self._list_messages_from_telegram(
            _ListMessagesTelegramRequest(
                dialog_id=dialog_id,
                limit=request.limit,
                direction=direction,
                direction_enum=direction_enum,
                anchor_msg_id=anchor_msg_id,
                sender_id=request.sender_id,
                topic_id=request.topic_id,
                unread_after_id=unread_after_id,
            )
        )
        if telegram_result.get("ok"):
            telegram_result["data"]["dialog_access"] = "live"
            telegram_result["data"]["dialog_type"] = dialog_type
            telegram_result["data"]["read_state"] = read_state
        return telegram_result

    async def _search_messages_global_result(
        self,
        request: _SearchMessagesRequest,
        stemmed: str,
    ) -> dict:
        rows = _fetchall_rows(
            self._conn.execute(
                _SELECT_FTS_ALL_SQL,
                {
                    "query": stemmed,
                    "limit": request.limit,
                    "offset": request.offset,
                    "self_id": self._deps.self_id,
                },
            )
        )
        messages = [_read_message_from_row(r) for r in rows]
        next_nav = self._search_next_navigation(request, messages, global_mode=True)
        return {
            "ok": True,
            "data": {
                "messages": [dataclasses.asdict(m) for m in messages],
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
        rows = _fetchall_rows(
            self._conn.execute(
                _SELECT_FTS_SQL,
                {
                    "query": stemmed,
                    "dialog_id": request.dialog_id,
                    "limit": request.limit,
                    "offset": request.offset,
                    "self_id": self._deps.self_id,
                },
            )
        )
        messages = await self._build_read_messages_from_rows(request.dialog_id, rows, log_rendered=False)
        next_nav = self._search_next_navigation(request, messages, global_mode=False)
        row = _fetchone_row(self._conn.execute(_SELECT_SYNC_STATUS_SQL, (request.dialog_id,)))
        scoped_status = _status_from_row(row)
        access_meta = _build_access_metadata(self._conn, request.dialog_id, scoped_status or "not_synced")
        return {
            "ok": True,
            "data": {
                "messages": [dataclasses.asdict(m) for m in messages],
                "total": len(messages),
                "next_navigation": next_nav,
                "read_state_per_dialog": self._read_state_per_dialog(messages),
                **access_meta,
            },
        }

    def _search_next_navigation(
        self,
        request: _SearchMessagesRequest,
        messages: list[ReadMessage],
        *,
        global_mode: bool,
    ) -> str | None:
        if messages and len(messages) == request.limit:
            next_offset = request.offset + request.limit
            nav_dialog_id = 0 if global_mode else request.dialog_id
            return encode_search_navigation(next_offset, nav_dialog_id, request.query, request.message_state)
        return None

    def _search_scheduled_messages(self, request: _SearchMessagesRequest) -> dict:
        """Search pending scheduled text in its local mirror.

        This is intentionally a separate source from ``messages_fts`` because
        scheduled rows are mutable and must never enter sent-history FTS.
        The returned rows still use the same ReadMessage envelope plus lifecycle
        metadata as ordinary search results.
        """
        if not self._scheduled_messages_available():
            return {
                "ok": True,
                "data": {"messages": [], "total": 0, "next_navigation": None, "source": "scheduled_messages"},
            }
        own_basis = self._own_only_basis_by_dialog()
        if own_basis is not None and (
            (request.dialog_id and request.dialog_id not in own_basis) or (not request.dialog_id and not own_basis)
        ):
            return {
                "ok": True,
                "data": {
                    "messages": [],
                    "total": 0,
                    "next_navigation": None,
                    "source": "scheduled_messages",
                    "scope": "own_only",
                },
            }
        sql = _SCHEDULED_MESSAGES_SQL.replace(
            "WHERE sm.dialog_id = :dialog_id\n  AND sm.message_state = 'scheduled'",
            "WHERE sm.message_state = 'scheduled'",
        )
        params: dict[str, object] = {
            "limit": request.limit,
            "offset": request.offset,
            "scheduled_now": int(time.time()),
        }
        if request.dialog_id:
            sql += " AND sm.dialog_id = :dialog_id"
            params["dialog_id"] = request.dialog_id
        elif own_basis is not None:
            own_ids = sorted(own_basis)
            placeholders = ", ".join(f":own_scope_{index}" for index in range(len(own_ids)))
            sql += f" AND sm.dialog_id IN ({placeholders})"
            params.update({f"own_scope_{index}": dialog_id for index, dialog_id in enumerate(own_ids)})
        sql = sql.replace(
            "FROM scheduled_messages sm",
            "FROM scheduled_messages sm JOIN scheduled_messages_fts sf "
            "ON sf.dialog_id = sm.dialog_id AND sf.message_id = sm.message_id",
        )
        sql += " AND scheduled_messages_fts MATCH :query AND sm.scheduled_at > :scheduled_now"
        params["query"] = stem_query(request.query)
        sql += " ORDER BY sm.scheduled_at ASC, sm.message_id ASC LIMIT :limit OFFSET :offset"
        rows: list[dict[str, object]] = []
        for raw_row in _fetchall_rows(self._conn.execute(sql, params)):
            message = _read_message_from_row(raw_row)
            item = dataclasses.asdict(message)
            item.update(
                {
                    "message_state": "scheduled",
                    "unpublished": True,
                    "unseen": True,
                    "scheduled_at": message.sent_at,
                    "published_at": _object_to_int_or_none(_row_value(raw_row, "published_at")),
                    "inclusion_basis": list(own_basis.get(message.dialog_id, ())) if own_basis is not None else [],
                }
            )
            rows.append(item)
        next_nav = self._search_next_navigation(
            request,
            [_read_message_from_row(row) for row in rows],
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
            "name_pat": dialog_filter.name_pat,
        }
        rows = _fetchall_rows(conn.execute(_LIST_DIALOGS_SQL, params))
        if not rows and dialog_filter.name_pat is not None and dialog_filter.normalized:
            rows = _fetchall_rows(conn.execute(_LIST_DIALOGS_SQL, {**params, "name_pat": None}))
        return [cast(Mapping[str, object], row) for row in rows]

    def _dialog_row_matches_filter(
        self,
        dialog_filter: _ListDialogsFilter,
        name: str | None,
    ) -> bool:
        if dialog_filter.normalized is None:
            return True
        raw_name = name or ""
        if not raw_name:
            return False
        name_norm = latinize(raw_name)
        if name_norm in (None, ""):
            return False
        filter_raw_lc = dialog_filter.raw_lower or ""
        name_initials_raw = "".join(w[0] for w in raw_name.split() if w).lower()
        matches_acronym = (
            _TRACE_ACRONYM_MIN_LEN <= len(filter_raw_lc) <= _TRACE_ACRONYM_MAX_LEN
            and filter_raw_lc in name_initials_raw
        )
        matches_fuzzy = (
            len(dialog_filter.normalized) >= _TRACE_FUZZY_MIN_LEN
            and len(name_norm) >= _TRACE_FUZZY_MIN_LEN
            and _fuzz.partial_ratio(dialog_filter.normalized, name_norm) >= _TRACE_FUZZY_SCORE_MIN
        )
        return dialog_filter.normalized in name_norm or matches_acronym or matches_fuzzy

    def _shape_dialog_row(  # noqa: PLR0913, PLR0917
        self,
        row: Mapping[str, object],
        local_counts: dict[int, int],
        unread_counts: dict[int, tuple[int, int]],
        dialog_filter: _ListDialogsFilter,
        scheduled_summary: tuple[int, int | None] = (0, None),
        inclusion_basis: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object] | None, int | None]:
        d_id = _object_to_int(row["dialog_id"])
        if not self._dialog_row_matches_filter(dialog_filter, _object_to_str_or_none(row["name"])):
            return None, None

        row_data: dict[str, object] = {
            "id": d_id,
            "name": row["name"],
            "type": row["type"],
            "last_message_at": row["last_message_at"],
            "unread_count": 0,
            "members": row["members"],
            "created": row["created"],
            "sync_status": row["sync_status"] if row["sync_status"] is not None else "not_synced",
            "sync_coverage_pct": _compute_sync_coverage(
                _object_to_int_or_none(row["total_messages"]),
                local_counts.get(d_id, 0),
            ),
            "access_lost_at": row["access_lost_at"],
            "unread_mentions_count": _object_to_int(row["unread_mentions_count"], 0),
            "unread_reactions_count": _object_to_int(row["unread_reactions_count"], 0),
            **DaemonReadingService._dialog_lifecycle_fields(row, scheduled_summary, inclusion_basis),
        }
        if DialogType.parse(_object_to_str_or_none(row["type"])) == DialogType.USER:
            in_cnt, out_cnt = unread_counts.get(d_id, (0, 0))
            row_data["unread_in"] = in_cnt
            row_data["unread_out"] = out_cnt
        return row_data, _object_to_int_or_none(row["snapshot_at"])

    @staticmethod
    def _dialog_lifecycle_fields(
        row: Mapping[str, object],
        scheduled_summary: tuple[int, int | None],
        inclusion_basis: tuple[str, ...] | None,
    ) -> dict[str, object]:
        return {
            "draft_text": row["draft_text"],
            "scheduled_count": scheduled_summary[0],
            "next_scheduled_at": scheduled_summary[1],
            "inclusion_basis": list(inclusion_basis) if inclusion_basis is not None else None,
        }

    async def _freshen_reactions_if_stale(
        self,
        dialog_id: int,
        entity: object,
        message_ids: list[int],
    ) -> None:
        """Per-message TTL-gated JIT reaction freshen from Telegram."""
        if not message_ids:
            return
        row = _fetchone_row(self._conn.execute("SELECT 1 FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)))
        if row is None:
            return

        now = int(time.time())
        threshold = now - REACTIONS_TTL_SECONDS
        placeholders = ",".join("?" * len(message_ids))
        fresh_rows = _fetchall_rows(
            self._conn.execute(
                f"SELECT message_id FROM message_reactions_freshness "
                f"WHERE dialog_id = ? AND message_id IN ({placeholders}) "
                f"AND checked_at > ?",
                [dialog_id, *message_ids, threshold],
            )
        )
        fresh_ids = {_object_to_int(_row_sequence(r)[0]) for r in fresh_rows}
        stale_ids = [mid for mid in message_ids if mid not in fresh_ids]
        if not stale_ids:
            return

        try:
            messages_result = self._deps.client.get_messages(entity, ids=stale_ids)
            if not inspect.isawaitable(messages_result):
                return
            messages = cast(Sequence[object], await cast(Awaitable[object], messages_result))
        except FloodWaitError as exc:
            self._logger.warning(
                "jit_reactions_floodwait dialog_id=%d stale_count=%d seconds=%d",
                dialog_id,
                len(stale_ids),
                getattr(exc, "seconds", 0),
            )
            return
        except Exception:
            self._logger.exception("jit_reactions_failed dialog_id=%d", dialog_id)
            return

        from .sync_worker import apply_reactions_delta, extract_reactions_rows

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
        """Resolve unread cutoff from synced_dialogs."""
        if unread_after_id is not None:
            return unread_after_id
        row = _fetchone_row(self._conn.execute(_GET_READ_POSITION_SQL, (dialog_id,)))
        if row is not None:
            values = _row_sequence(row)
            if values and values[0] is not None:
                return _object_to_int(values[0])
        return None

    async def _list_messages_context_window(
        self,
        dialog_id: int,
        anchor_message_id: int,
        context_size: int,
    ) -> dict:
        """Return messages centred on anchor_message_id from sync.db."""
        half = max(1, context_size // 2)
        before_rows = _fetchall_rows(
            self._conn.execute(
                _LIST_MESSAGES_BASE_SQL + " AND m.message_id <= :anchor ORDER BY m.message_id DESC LIMIT :limit",
                {
                    "dialog_id": dialog_id,
                    "self_id": self._deps.self_id,
                    "anchor": anchor_message_id,
                    "limit": half + 1,
                },
            )
        )

        after_rows = _fetchall_rows(
            self._conn.execute(
                _LIST_MESSAGES_BASE_SQL + " AND m.message_id > :anchor ORDER BY m.message_id ASC LIMIT :limit",
                {
                    "dialog_id": dialog_id,
                    "self_id": self._deps.self_id,
                    "anchor": anchor_message_id,
                    "limit": half,
                },
            )
        )

        rows = list(reversed(before_rows)) + list(after_rows)
        messages = await self._build_read_messages_from_rows(dialog_id, rows, log_rendered=True)

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
        req: _ListMessagesTelegramRequest,
    ) -> dict:
        """Fetch messages on-demand from Telegram API."""
        self._logger.debug("list_messages_fallback_telegram dialog_id=%d%s", req.dialog_id, self._deps.rid())
        iter_kwargs: dict[str, object] = {
            k: v
            for k, v in {
                "limit": req.limit,
                "offset_id": req.anchor_msg_id,
                "from_user": req.sender_id,
                "reply_to": req.topic_id,
                "min_id": req.unread_after_id,
                "reverse": True if req.direction == "oldest" else None,
            }.items()
            if v is not None
        }
        messages: list[dict[str, object]] = []
        try:
            messages.extend(
                [
                    message_to_dict(
                        cast(_DaemonMessageLike, msg),
                        dialog_id=req.dialog_id,
                        self_id=self._deps.self_id,
                    )
                    async for msg in self._deps.client.iter_messages(req.dialog_id, **iter_kwargs)
                ]
            )
        except Exception as exc:  # noqa: BLE001 - boundary helper logs expected vs unexpected Telegram failures.
            return self._list_messages_telegram_error(req, exc)

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
            ),
        )
        return {
            "ok": True,
            "data": {"messages": messages, "source": "telegram", "next_navigation": next_nav},
        }

    def _list_messages_telegram_error(self, req: _ListMessagesTelegramRequest, exc: Exception) -> dict:
        if isinstance(exc, (RPCError, ValueError)):
            _log_recoverable_telegram_error(
                self._logger,
                event="list_messages_telegram_error",
                dialog_id=req.dialog_id,
                exc=exc,
                request_id=self._deps.rid(),
            )
            return {
                "ok": False,
                "error": "telegram_error",
                "message": "failed to fetch messages",
                "detail": {
                    "error_type": type(exc).__name__,
                    "error_message": _safe_exception_message(exc),
                    "retryable": False,
                },
            }

        self._logger.exception(
            "list_messages_telegram_unexpected dialog_id=%d%s",
            req.dialog_id,
            self._deps.rid(),
        )
        return {"ok": False, "error": "telegram_error", "message": "failed to fetch messages"}

    async def _list_messages_from_db(self, req: _ListMessagesDbRequest) -> dict:
        """Read messages from sync.db using the dynamic query builder."""
        sql, params = _build_list_messages_query(req)
        rows = _fetchall_rows(self._conn.execute(sql, params))
        messages = await self._build_read_messages_from_rows(req.dialog_id, rows, log_rendered=True)
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
            ),
        )
        return {
            "ok": True,
            "data": {
                "messages": [dataclasses.asdict(m) for m in messages],
                "source": "sync_db",
                "next_navigation": next_nav,
            },
        }

    def _scheduled_messages_available(self) -> bool:
        return (
            _fetchone_row(self._conn.execute(_SCHEDULED_MESSAGES_TABLE_SQL)) is not None
            and _fetchone_row(self._conn.execute(_SCHEDULED_MESSAGES_FTS_TABLE_SQL)) is not None
        )

    def _own_only_basis_by_dialog(self, conn: sqlite3.Connection | None = None) -> dict[int, tuple[str, ...]] | None:
        """Return the ownership cache, or None for pre-cache test databases."""
        source = conn or self._conn
        if _fetchone_row(source.execute(_OWN_ONLY_DIALOGS_TABLE_SQL)) is None:
            return None
        return own_only_basis_by_dialog(source)

    def _list_scheduled_messages_from_db(  # noqa: C901, PLR0912, PLR0915
        self, req: _ListMessagesDbRequest
    ) -> dict:
        """Read pending scheduled messages from the separate local mirror.

        Scheduled messages deliberately do not use ``messages`` or any of its
        derived tables.  This path is local-only: it never falls back to a
        Telegram request when the mirror is empty or unavailable.
        """
        if not self._scheduled_messages_available():
            rows: list[dict[str, object]] = []
        else:
            sql = _SCHEDULED_MESSAGES_SQL
            own_basis = self._own_only_basis_by_dialog()
            if own_basis is not None and req.dialog_id not in own_basis:
                rows = []
                own_basis = {}
            else:
                own_basis = own_basis or {}
            params: dict[str, object] = {
                "dialog_id": req.dialog_id,
                "limit": req.limit,
                "self_id": req.self_id,
                "scheduled_now": int(time.time()),
            }
            if req.sender_id is not None:
                sql += " AND sm.sender_id = :filter_sender_id"
                params["filter_sender_id"] = req.sender_id
            if req.sender_name is not None:
                sql += " AND sm.sender_first_name LIKE :sender_name_pattern ESCAPE '\\' COLLATE NOCASE"
                escaped = req.sender_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params["sender_name_pattern"] = f"%{escaped}%"
            if req.topic_id is not None:
                sql += " AND sm.forum_topic_id = :topic_id"
                params["topic_id"] = req.topic_id
            sql += " AND sm.scheduled_at > :scheduled_now"
            if req.anchor_msg_id is not None:
                anchor_row = _fetchone_row(
                    self._conn.execute(
                        "SELECT scheduled_at FROM scheduled_messages WHERE dialog_id = ? AND message_id = ?",
                        (req.dialog_id, req.anchor_msg_id),
                    )
                )
                anchor_at = req.anchor_sent_at
                if anchor_at is None:
                    anchor_at = _object_to_int_or_none(_row_value(anchor_row, "scheduled_at"))
                if anchor_at is None:
                    anchor_at = 0
                operator = ">" if req.direction == "oldest" else "<"
                sql += (
                    f" AND (sm.scheduled_at {operator} :anchor_at OR "
                    f"(sm.scheduled_at = :anchor_at AND sm.message_id {operator} :anchor_msg_id))"
                )
                params["anchor_at"] = anchor_at
                params["anchor_msg_id"] = req.anchor_msg_id
            if req.direction == "oldest":
                sql += " ORDER BY sm.scheduled_at ASC, sm.message_id ASC"
            else:
                sql += " ORDER BY sm.scheduled_at DESC, sm.message_id DESC"
            sql += " LIMIT :limit"
            raw_rows = _fetchall_rows(self._conn.execute(sql, params))
            rows = []
            for raw_row in raw_rows:
                message = _read_message_from_row(raw_row)
                item = dataclasses.asdict(message)
                item.update(
                    {
                        "message_state": "scheduled",
                        "unpublished": True,
                        "unseen": True,
                        "scheduled_at": message.sent_at,
                        "published_at": _object_to_int_or_none(_row_value(raw_row, "published_at")),
                        "inclusion_basis": list(own_basis.get(message.dialog_id, ())),
                    }
                )
                rows.append(item)
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
            },
        }

    async def _list_messages_local_state_result(  # noqa: PLR0913, PLR0917
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        direction: str,
        status: str | None,
        anchor_msg_id: int | None = None,
        anchor_sent_at: int | None = None,
    ) -> dict:
        direction_enum = HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST
        db_request = _ListMessagesDbRequest(
            dialog_id=dialog_id,
            limit=request.limit + 1 if request.message_state == "all" else request.limit,
            self_id=self._deps.self_id,
            direction=direction,
            direction_enum=direction_enum,
            anchor_msg_id=anchor_msg_id,
            anchor_sent_at=anchor_sent_at,
            sender_id=request.sender_id,
            sender_name=request.sender_name,
            topic_id=request.topic_id,
            unread_after_id=None,
        )
        scheduled_result = self._list_scheduled_messages_from_db(db_request)
        if request.message_state == "scheduled":
            scheduled_result["data"]["dialog_type"] = _dialog_type_from_db(self._conn, dialog_id)
            scheduled_result["data"]["read_state"] = None
            return scheduled_result

        if status in ("synced", "syncing", "access_lost"):
            sent_result = await self._list_messages_from_db(db_request)
            sent_rows = sent_result["data"]["messages"]
        else:
            sent_rows = []
        scheduled_rows = scheduled_result["data"]["messages"]
        combined = [*sent_rows, *scheduled_rows]
        combined.sort(
            key=lambda row: (int(row.get("sent_at") or 0), int(row.get("message_id") or 0)),
            reverse=direction != "oldest",
        )
        has_more = len(combined) > request.limit
        combined = combined[: request.limit]
        next_nav = None
        if has_more and combined:
            last = combined[-1]
            next_nav = encode_history_navigation(
                _message_id_from_item(last),
                dialog_id,
                direction=HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST,
                sent_at=_object_to_int(last.get("sent_at")),
                message_state="all",
            )
        return {
            "ok": True,
            "data": {
                "messages": combined,
                "source": "sync_db+scheduled_messages",
                "next_navigation": next_nav,
                "message_state": "all",
                "dialog_type": _dialog_type_from_db(self._conn, dialog_id),
                "read_state": _read_state_for_dialog(
                    self._conn,
                    dialog_id,
                    _dialog_type_from_db(self._conn, dialog_id),
                ),
                **_build_access_metadata(self._conn, dialog_id, status or "not_synced"),
            },
        }

    async def _list_messages_non_sent(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        direction: str,
    ) -> dict:
        row = _fetchone_row(self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)))
        status = _status_from_row(row)
        if request.context_message_id is not None:
            return {
                "ok": False,
                "error": "scheduled_context_unsupported",
                "message": "Scheduled messages do not support sent-history context windows.",
            }
        nav_result = self._decode_history_navigation(
            request.navigation,
            dialog_id,
            direction,
            request.message_state,
            request.topic_id,
        )
        if isinstance(nav_result, dict):
            return nav_result
        anchor_msg_id, direction = nav_result
        anchor_sent_at = None
        if request.message_state == "all" and request.navigation not in (None, "newest", "oldest"):
            navigation = request.navigation
            assert navigation is not None
            try:
                anchor_sent_at = decode_navigation_token(navigation).sent_at
            except ValueError as exc:
                return {"ok": False, "error": "invalid_navigation", "message": str(exc)}
        return await self._list_messages_local_state_result(
            dialog_id, request, direction, status, anchor_msg_id, anchor_sent_at
        )

    async def _list_messages_for_state(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
        direction: str,
    ) -> dict:
        if request.message_state not in {"sent", "scheduled", "all"}:
            return {
                "ok": False,
                "error": "invalid_message_state",
                "message": "message_state must be sent, scheduled, or all",
            }
        if request.message_state != "sent":
            return await self._list_messages_non_sent(dialog_id, request, direction)

        if request.context_message_id is not None:
            return await self._list_messages_context_result(dialog_id, request)
        return await self._list_messages_history_result(dialog_id, request, direction)

    async def _list_messages(self, req: dict) -> dict:
        """Return messages from sync.db (if synced) or Telegram (on-demand)."""
        request = self._parse_list_messages_request(req)
        direction = request.direction
        if direction not in ("newest", "oldest"):
            direction = "newest"

        resolved = await self._deps.resolve_dialog_id(request.dialog_id, request.dialog)
        if isinstance(resolved, dict):
            return resolved
        dialog_id = resolved
        if not dialog_id:
            return {
                "ok": False,
                "error": "missing_dialog",
                "message": "Either dialog_id or dialog name is required",
            }
        return await self._list_messages_for_state(dialog_id, request, direction)

    async def _search_messages_scoped_for_state(
        self,
        request: _SearchMessagesRequest,
        stemmed: str,
    ) -> dict:
        resolved = await self._deps.resolve_dialog_id(request.dialog_id, request.dialog)
        if isinstance(resolved, dict):
            return resolved
        request = dataclasses.replace(request, dialog_id=resolved)
        navigation_result = self._bind_search_navigation(request, resolved)
        if isinstance(navigation_result, dict):
            return navigation_result
        request = navigation_result
        if request.message_state == "scheduled":
            return self._search_scheduled_messages(request)
        if request.message_state == "all":
            sent_result = await self._search_messages_scoped_result(
                dataclasses.replace(request, message_state="sent", offset=0, limit=request.offset + request.limit),
                stemmed,
            )
            return self._merge_search_results(
                sent_result,
                self._search_scheduled_messages(
                    dataclasses.replace(request, offset=0, limit=request.offset + request.limit)
                ),
                request,
            )
        return await self._search_messages_scoped_result(request, stemmed)

    async def _search_messages_for_state(self, request: _SearchMessagesRequest, stemmed: str) -> dict:
        if request.message_state not in {"sent", "scheduled", "all"}:
            return {
                "ok": False,
                "error": "invalid_message_state",
                "message": "message_state must be sent, scheduled, or all",
            }
        global_mode = not request.dialog_id and request.dialog is None
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
            return await self._search_messages_scoped_for_state(request, stemmed)
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

        error_message: str | None = None
        if navigation.kind != "search":
            error_message = f"Navigation token is for {navigation.kind}, not search"
        elif navigation.query != request.query:
            error_message = "Navigation token belongs to a different search query"
        elif navigation.message_state != request.message_state:
            error_message = (
                f"Navigation token belongs to message_state {navigation.message_state!r}, not {request.message_state!r}"
            )
        elif navigation.dialog_id != dialog_id:
            error_message = f"Navigation token belongs to dialog {navigation.dialog_id}, not {dialog_id}"
        if error_message is not None:
            return {"ok": False, "error": "invalid_navigation", "message": error_message}
        return dataclasses.replace(request, offset=navigation.value)

    async def _search_messages(self, req: dict) -> dict:
        """FTS5 stemmed full-text search against messages_fts."""
        request = self._parse_search_messages_request(req)
        stemmed = stem_query(request.query)
        if not stemmed:
            return {"ok": True, "data": {"messages": [], "total": 0}}
        return await self._search_messages_for_state(request, stemmed)

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
                self._logger.info("list_dialogs_sql_reader completed in %.3fms%s", elapsed_ms, self._deps.rid())
        return self._list_dialogs_sync(self._conn, req)

    def _list_dialogs_from_reader(self, db_path: Path, req: dict) -> dict:
        conn = open_sync_db_reader(db_path)
        try:
            conn.row_factory = sqlite3.Row
            return self._list_dialogs_sync(conn, req)
        finally:
            conn.close()

    def _scheduled_summary_by_dialog(self, conn: sqlite3.Connection) -> dict[int, tuple[int, int | None]]:
        """Summarize pending scheduled rows without reading sent history."""
        if _fetchone_row(conn.execute(_SCHEDULED_MESSAGES_TABLE_SQL)) is None:
            return {}
        rows = _fetchall_rows(
            conn.execute(
                """
                SELECT dialog_id, COUNT(*) AS scheduled_count, MIN(scheduled_at) AS next_scheduled_at
                FROM scheduled_messages
                WHERE message_state = 'scheduled' AND scheduled_at > :scheduled_now
                GROUP BY dialog_id
                """,
                {"scheduled_now": int(time.time())},
            )
        )
        return {
            _object_to_int(_row_value(row, "dialog_id")): (
                _object_to_int(_row_value(row, "scheduled_count")),
                _object_to_int_or_none(_row_value(row, "next_scheduled_at")),
            )
            for row in rows
        }

    def _list_dialogs_sync(self, conn: sqlite3.Connection, req: dict) -> dict:  # noqa: PLR0914
        """Return dialog list from the local dialogs snapshot (pure SQL)."""
        request = self._parse_list_dialogs_request(req)
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
        dialog_filter = self._prepare_list_dialogs_filter(request.filter_raw)
        local_counts = {
            _object_to_int(_row_sequence(row)[0]): _object_to_int(_row_sequence(row)[1], 0)
            for row in _fetchall_rows(conn.execute(_COUNT_MESSAGES_BY_DIALOG_SQL))
        }
        unread_counts = {
            _object_to_int(_row_sequence(row)[0]): (
                _object_to_int(_row_sequence(row)[1], 0),
                _object_to_int(_row_sequence(row)[2], 0),
            )
            for row in _fetchall_rows(conn.execute(_BATCHED_UNREAD_COUNTS_SQL))
        }
        scheduled_summary = self._scheduled_summary_by_dialog(conn)
        own_basis = self._own_only_basis_by_dialog(conn)
        sql_rows = self._fetch_list_dialog_rows(conn, request, dialog_filter)
        if not sql_rows:
            count_row = _fetchone_row(conn.execute("SELECT COUNT(*) FROM dialogs"))
            count_total = _object_to_int(_row_sequence(count_row)[0]) if count_row is not None else 0
            return {
                "ok": True,
                "data": {
                    "dialogs": [],
                    "snapshot_age_h": None,
                    "bootstrap_pending": count_total == 0,
                    "scope": request.scope,
                },
            }

        dialogs: list[dict] = []
        max_snapshot: int | None = None
        for row in sql_rows:
            dialog_id = _object_to_int(row["dialog_id"])
            if request.scope == "own_only" and (
                (own_basis is not None and dialog_id not in own_basis)
                or (own_basis is None and str(row.get("sync_status") or "") != "own_only")
            ):
                continue
            summary = scheduled_summary.get(dialog_id, (0, None))
            if own_basis is not None and dialog_id not in own_basis:
                summary = (0, None)
            if request.message_state == "scheduled" and summary[0] == 0:
                continue
            row_data, snapshot_at = self._shape_dialog_row(
                row,
                local_counts,
                unread_counts,
                dialog_filter,
                summary,
                own_basis.get(dialog_id) if own_basis is not None else None,
            )
            if row_data is None:
                continue
            if snapshot_at is not None and (max_snapshot is None or snapshot_at > max_snapshot):
                max_snapshot = snapshot_at
            dialogs.append(row_data)

        snapshot_age_h = _compute_snapshot_age_h(max_snapshot)
        return {
            "ok": True,
            "data": {
                "dialogs": dialogs,
                "snapshot_age_h": snapshot_age_h,
                "bootstrap_pending": False,
                "scope": request.scope,
            },
        }
