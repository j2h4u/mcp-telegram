"""Reading-domain service for daemon read/search/list handlers."""

import dataclasses
import inspect
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from rapidfuzz import fuzz as _fuzz
from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

from . import daemon_api as api
from .daemon_message import _MessageLike as _DaemonMessageLike
from .daemon_message import fetch_reaction_counts, message_to_dict
from .formatter import format_reaction_counts
from .fts import stem_query
from .pagination import (
    HistoryDirection,
    decode_navigation_token,
    encode_history_navigation,
    encode_search_navigation,
)


class _LoggerLike(Protocol):
    def debug(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def info(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def warning(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def exception(self, msg: str, *args: object, **kwargs: object) -> None: ...


class _TelegramClientLike(Protocol):
    async def get_messages(self, entity: object, ids: list[int]) -> object: ...

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]: ...


@dataclass(frozen=True)
class DaemonReadingDeps:
    """Dependencies for ``DaemonReadingService``."""

    conn: sqlite3.Connection
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


@dataclass
class _ListMessagesDbRequest:
    dialog_id: int
    limit: int
    self_id: int | None
    direction: str
    direction_enum: HistoryDirection
    anchor_msg_id: int | None
    sender_id: int | None
    sender_name: str | None
    topic_id: int | None
    unread_after_id: int | None


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


@dataclass(frozen=True)
class _ListDialogsRequest:
    exclude_archived: bool
    ignore_pinned: bool
    filter_raw: str | None


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
    if isinstance(item, api.ReadMessage):
        return item.message_id
    if isinstance(item, Mapping):
        row = _row_mapping(item)
        return _object_to_int(row["message_id"])
    row_message_id = _row_value(item, "message_id")
    if row_message_id is not None:
        return _object_to_int(row_message_id)
    return _object_to_int(getattr(item, "message_id", None))


def _read_message_from_row(row: object) -> api.ReadMessage:
    return api.ReadMessage(
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
            limit=api._clamp(req.get("limit", 50), 1, 500),
            navigation=req.get("navigation"),
            direction=req.get("direction", "newest"),
            sender_id=req.get("sender_id"),
            sender_name=req.get("sender_name"),
            topic_id=req.get("topic_id"),
            unread_after_id=req.get("unread_after_id"),
            unread=bool(req.get("unread")),
            context_message_id=req.get("context_message_id"),
            context_size=api._clamp(req.get("context_size", 10), 2, 50),
        )

    @staticmethod
    def _parse_search_messages_request(req: dict) -> _SearchMessagesRequest:
        return _SearchMessagesRequest(
            dialog_id=req.get("dialog_id", 0) or 0,
            dialog=req.get("dialog"),
            query=req.get("query", ""),
            limit=api._clamp(req.get("limit", 20), 1, 200),
            offset=max(0, req.get("offset", 0)),
        )

    @staticmethod
    def _parse_list_dialogs_request(req: dict) -> _ListDialogsRequest:
        return _ListDialogsRequest(
            exclude_archived=bool(req.get("exclude_archived", False)),
            ignore_pinned=bool(req.get("ignore_pinned", False)),
            filter_raw=req.get("filter"),
        )

    @staticmethod
    def _prepare_list_dialogs_filter(filter_raw: str | None) -> _ListDialogsFilter:
        raw_lower: str | None = None
        name_pat: str | None = None
        normalized: str | None = None
        if filter_raw is not None:
            stripped = filter_raw.strip()
            if stripped:
                normalized = api.latinize(stripped)
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
                direction=context.direction_enum,
            )
        return None

    @staticmethod
    def _decode_history_navigation(
        navigation: str | None,
        dialog_id: int,
        direction: str,
    ) -> tuple[int | None, str] | dict:
        """Decode a history navigation token into (anchor_msg_id, direction)."""
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
                    "message": f"Navigation token belongs to dialog {nav.dialog_id}, not {dialog_id}",
                }
            anchor_msg_id = nav.value
            if nav.direction is not None:
                direction = str(nav.direction)
        elif navigation == "oldest":
            direction = "oldest"
        return anchor_msg_id, direction

    async def _build_read_messages_from_rows(
        self,
        dialog_id: int,
        rows: Sequence[object],
        *,
        log_rendered: bool,
    ) -> list[api.ReadMessage]:
        msg_ids = [_message_id_from_item(r) for r in rows]
        if msg_ids:
            await self._freshen_reactions_if_stale(dialog_id, dialog_id, msg_ids)
        reaction_map = fetch_reaction_counts(self._conn, dialog_id, msg_ids)
        messages: list[api.ReadMessage] = []
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

    def _read_state_per_dialog(self, messages: list[api.ReadMessage]) -> dict[int, api.ReadState]:
        read_state_per_dialog: dict[int, api.ReadState] = {}
        for dialog_id in {m.dialog_id for m in messages if m.dialog_id}:
            dialog_type = api._dialog_type_from_db(self._conn, dialog_id)
            read_state = api._read_state_for_dialog(self._conn, dialog_id, dialog_type)
            if read_state is not None:
                read_state_per_dialog[dialog_id] = read_state
        return read_state_per_dialog

    async def _list_messages_context_result(
        self,
        dialog_id: int,
        request: _ListMessagesRequest,
    ) -> dict:
        row = _fetchone_row(self._conn.execute(api._SELECT_SYNC_STATUS_SQL, (dialog_id,)))
        current_status = _status_from_row(row)
        if current_status in (None, "not_synced", "fragment"):
            if not await self._deps.fetch_fragment_context(dialog_id, request.context_message_id or 0):
                return {
                    "ok": False,
                    "error": "fragment_fetch_failed",
                    "message": "Could not fetch messages from Telegram.",
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
                "message": ("Context window requires the dialog to be synced. Use MarkDialogForSync first."),
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
        nav_result = self._decode_history_navigation(request.navigation, dialog_id, direction)
        if isinstance(nav_result, dict):
            return nav_result
        anchor_msg_id, direction = nav_result

        direction_enum = HistoryDirection.OLDEST if direction == "oldest" else HistoryDirection.NEWEST
        unread_after_id = request.unread_after_id
        if request.unread:
            unread_after_id = await self._resolve_unread_position(dialog_id, request.unread_after_id)

        row = _fetchone_row(self._conn.execute(api._SELECT_SYNC_STATUS_SQL, (dialog_id,)))
        status = _status_from_row(row)

        dialog_type = api._dialog_type_from_db(self._conn, dialog_id)
        read_state = api._read_state_for_dialog(self._conn, dialog_id, dialog_type)

        if status in ("synced", "syncing", "access_lost"):
            result = await self._list_messages_from_db(
                _ListMessagesDbRequest(
                    dialog_id=dialog_id,
                    limit=request.limit,
                    self_id=self._deps.self_id,
                    direction=direction,
                    direction_enum=direction_enum,
                    anchor_msg_id=anchor_msg_id,
                    sender_id=request.sender_id,
                    sender_name=request.sender_name,
                    topic_id=request.topic_id,
                    unread_after_id=unread_after_id,
                )
            )
            result["data"].update(api._build_access_metadata(self._conn, dialog_id, status))
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
                api._SELECT_FTS_ALL_SQL,
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
                api._SELECT_FTS_SQL,
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
        row = _fetchone_row(self._conn.execute(api._SELECT_SYNC_STATUS_SQL, (request.dialog_id,)))
        scoped_status = _status_from_row(row)
        access_meta = api._build_access_metadata(self._conn, request.dialog_id, scoped_status or "not_synced")
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
        messages: list[api.ReadMessage],
        *,
        global_mode: bool,
    ) -> str | None:
        if messages and len(messages) == request.limit:
            next_offset = request.offset + request.limit
            nav_dialog_id = 0 if global_mode else request.dialog_id
            return encode_search_navigation(next_offset, nav_dialog_id, request.query)
        return None

    def _fetch_list_dialog_rows(
        self,
        request: _ListDialogsRequest,
        dialog_filter: _ListDialogsFilter,
    ) -> list[Mapping[str, object]]:
        params = {
            "archived_filter": 0 if request.exclude_archived else None,
            "pinned_filter": 0 if request.ignore_pinned else None,
            "name_pat": dialog_filter.name_pat,
        }
        rows = _fetchall_rows(self._conn.execute(api._LIST_DIALOGS_SQL, params))
        if not rows and dialog_filter.name_pat is not None and dialog_filter.normalized:
            rows = _fetchall_rows(self._conn.execute(api._LIST_DIALOGS_SQL, {**params, "name_pat": None}))
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
        name_norm = api.latinize(raw_name)
        if name_norm in (None, ""):
            return False
        filter_raw_lc = dialog_filter.raw_lower or ""
        name_initials_raw = "".join(w[0] for w in raw_name.split() if w).lower()
        matches_acronym = (
            api._TRACE_ACRONYM_MIN_LEN <= len(filter_raw_lc) <= api._TRACE_ACRONYM_MAX_LEN
            and filter_raw_lc in name_initials_raw
        )
        matches_fuzzy = (
            len(dialog_filter.normalized) >= api._TRACE_FUZZY_MIN_LEN
            and len(name_norm) >= api._TRACE_FUZZY_MIN_LEN
            and _fuzz.partial_ratio(dialog_filter.normalized, name_norm) >= api._TRACE_FUZZY_SCORE_MIN
        )
        return dialog_filter.normalized in name_norm or matches_acronym or matches_fuzzy

    def _shape_dialog_row(
        self,
        row: Mapping[str, object],
        local_counts: dict[int, int],
        unread_counts: dict[int, tuple[int, int]],
        dialog_filter: _ListDialogsFilter,
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
            "sync_coverage_pct": api._compute_sync_coverage(
                _object_to_int_or_none(row["total_messages"]),
                local_counts.get(d_id, 0),
            ),
            "access_lost_at": row["access_lost_at"],
            "unread_mentions_count": _object_to_int(row["unread_mentions_count"], 0),
            "unread_reactions_count": _object_to_int(row["unread_reactions_count"], 0),
            "draft_text": row["draft_text"],
        }
        if api.DialogType.parse(_object_to_str_or_none(row["type"])) == api.DialogType.USER:
            in_cnt, out_cnt = unread_counts.get(d_id, (0, 0))
            row_data["unread_in"] = in_cnt
            row_data["unread_out"] = out_cnt
        return row_data, _object_to_int_or_none(row["snapshot_at"])

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
        threshold = now - api.REACTIONS_TTL_SECONDS
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
        row = _fetchone_row(self._conn.execute(api._GET_READ_POSITION_SQL, (dialog_id,)))
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
                api._LIST_MESSAGES_BASE_SQL + " AND m.message_id <= :anchor ORDER BY m.message_id DESC LIMIT :limit",
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
                api._LIST_MESSAGES_BASE_SQL + " AND m.message_id > :anchor ORDER BY m.message_id ASC LIMIT :limit",
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

        dialog_type = api._dialog_type_from_db(self._conn, dialog_id)
        read_state = api._read_state_for_dialog(self._conn, dialog_id, dialog_type)
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
        except Exception as exc:
            self._logger.warning(
                "list_messages_telegram_error dialog_id=%d error=%s%s",
                req.dialog_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )
            return {"ok": False, "error": "telegram_error", "message": "failed to fetch messages"}

        next_nav = self._maybe_encode_next_nav(
            _NextNavContext(
                messages=messages,
                limit=req.limit,
                dialog_id=req.dialog_id,
                direction=req.direction,
                direction_enum=req.direction_enum,
                logger=self._logger,
                request_id=self._deps.rid,
            ),
        )
        return {
            "ok": True,
            "data": {"messages": messages, "source": "telegram", "next_navigation": next_nav},
        }

    async def _list_messages_from_db(self, req: _ListMessagesDbRequest) -> dict:
        """Read messages from sync.db using the dynamic query builder."""
        sql, params = api._build_list_messages_query(req)
        rows = _fetchall_rows(self._conn.execute(sql, params))
        messages = await self._build_read_messages_from_rows(req.dialog_id, rows, log_rendered=True)
        next_nav = self._maybe_encode_next_nav(
            _NextNavContext(
                messages=messages,
                limit=req.limit,
                dialog_id=req.dialog_id,
                direction=req.direction,
                direction_enum=req.direction_enum,
                logger=self._logger,
                request_id=self._deps.rid,
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

        if request.context_message_id is not None:
            return await self._list_messages_context_result(dialog_id, request)
        return await self._list_messages_history_result(dialog_id, request, direction)

    async def _search_messages(self, req: dict) -> dict:
        """FTS5 stemmed full-text search against messages_fts."""
        request = self._parse_search_messages_request(req)
        stemmed = stem_query(request.query)
        if not stemmed:
            return {"ok": True, "data": {"messages": [], "total": 0}}
        global_mode = not request.dialog_id and request.dialog is None
        if not global_mode:
            resolved = await self._deps.resolve_dialog_id(request.dialog_id, request.dialog)
            if isinstance(resolved, dict):
                return resolved
            request = _SearchMessagesRequest(
                dialog_id=resolved,
                dialog=request.dialog,
                query=request.query,
                limit=request.limit,
                offset=request.offset,
            )
            return await self._search_messages_scoped_result(request, stemmed)
        return await self._search_messages_global_result(request, stemmed)

    async def _list_dialogs(self, req: dict) -> dict:
        """Return dialog list from the local dialogs snapshot (pure SQL)."""
        request = self._parse_list_dialogs_request(req)
        dialog_filter = self._prepare_list_dialogs_filter(request.filter_raw)
        local_counts = {
            _object_to_int(_row_sequence(row)[0]): _object_to_int(_row_sequence(row)[1], 0)
            for row in _fetchall_rows(self._conn.execute(api._COUNT_MESSAGES_BY_DIALOG_SQL))
        }
        unread_counts = {
            _object_to_int(_row_sequence(row)[0]): (
                _object_to_int(_row_sequence(row)[1], 0),
                _object_to_int(_row_sequence(row)[2], 0),
            )
            for row in _fetchall_rows(self._conn.execute(api._BATCHED_UNREAD_COUNTS_SQL))
        }
        sql_rows = self._fetch_list_dialog_rows(request, dialog_filter)
        if not sql_rows:
            count_row = _fetchone_row(self._conn.execute("SELECT COUNT(*) FROM dialogs"))
            count_total = _object_to_int(_row_sequence(count_row)[0]) if count_row is not None else 0
            return {
                "ok": True,
                "data": {
                    "dialogs": [],
                    "snapshot_age_h": None,
                    "bootstrap_pending": count_total == 0,
                },
            }

        dialogs: list[dict] = []
        max_snapshot: int | None = None
        for row in sql_rows:
            row_data, snapshot_at = self._shape_dialog_row(row, local_counts, unread_counts, dialog_filter)
            if row_data is None:
                continue
            if snapshot_at is not None and (max_snapshot is None or snapshot_at > max_snapshot):
                max_snapshot = snapshot_at
            dialogs.append(row_data)

        snapshot_age_h = api._compute_snapshot_age_h(max_snapshot)
        return {
            "ok": True,
            "data": {
                "dialogs": dialogs,
                "snapshot_age_h": snapshot_age_h,
                "bootstrap_pending": False,
            },
        }
