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
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

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
    GROUP_TTL,
    USER_TTL,
    DaemonAccountTraceDeps,
    DaemonAccountTraceService,
)
from .daemon_dialog_queries import (
    _COLLECT_UNREAD_DIALOGS_WITH_COUNTS_SQL,
    _COUNT_BOOTSTRAP_PENDING_SQL,
    _COUNT_SYNCED_MESSAGES_SQL,
    _GET_ACCESS_LOST_ALERTS_SQL,
    _GET_DELETED_ALERTS_SQL,
    _GET_EDIT_ALERTS_SQL,
    _GET_SYNC_STATUS_SQL,
    _LIST_TOPICS_SQL,
    _MARK_FOR_SYNC_SQL,
    _UNMARK_SYNC_SQL,
    _compute_sync_coverage,
)
from .daemon_entity_info import DaemonEntityInfoService, EntityInfoDeps
from .daemon_ipc import get_daemon_socket_path as _get_daemon_socket_path
from .daemon_message_queries import (
    _FETCH_UNREAD_MESSAGES_SQL,
    _read_message_from_row,
)
from .daemon_read_state_queries import _dialog_type_from_db, _read_state_for_dialog
from .models import DialogType, ReadMessage

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


def _attr(obj: object, name: str, default: object | None = None) -> object | None:
    try:
        return cast(object | None, object.__getattribute__(obj, name))
    except AttributeError:
        return default


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(cast(int | str, value))
    except TypeError, ValueError:
        return default


def get_daemon_socket_path() -> Path:
    """Return the canonical path for the daemon Unix socket."""
    return _get_daemon_socket_path()


from .budget import allocate_message_budget_proportional, unread_chat_tier
from .daemon_message import fetch_reaction_counts
from .daemon_source_export import (
    _describe_source,
    _export_source_changes,
    _read_source_unit_window,
)
from .feedback_db import VALID_SEVERITIES, VALID_STATUSES
from .formatter import format_reaction_counts
from .telegram_fragments import FragmentContextService, TelethonTelegramFragmentGateway
from .telegram_history import TelethonTelegramHistoryGateway
from .telegram_reactions import ReactionFreshener, TelethonTelegramReactionGateway
from .telegram_reading import ReactionFreshness


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


if TYPE_CHECKING:
    from .daemon_account_trace import _AccountTraceClientLike
    from .daemon_account_trace import _LoggerLike as AccountTraceLoggerLike
    from .daemon_reading import DaemonReadingService
    from .daemon_reading import _ListMessagesDbRequest as ReadingListMessagesDbRequest
    from .daemon_reading import _ListMessagesTelegramRequest as ReadingListMessagesTelegramRequest
    from .daemon_reading import _LoggerLike as ReadingLoggerLike
    from .pagination import HistoryDirection
else:
    _AccountTraceClientLike = object
    AccountTraceLoggerLike = object
    ReadingListMessagesDbRequest = object
    ReadingListMessagesTelegramRequest = object
    ReadingLoggerLike = object

# Phase 39.2 §Key technical decisions: per-message TTL for JIT reactions freshen-on-read.
# Amortizes rapid paginated reads on the same ids; live events catch most mutations.
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
        self._reaction_freshener = ReactionFreshener(
            self._conn,
            TelethonTelegramReactionGateway(self._client),
            log=logger,
        )
        self._activity_stats_service: _activity_stats.DaemonActivityStatsService | None = None

    def _get_reading_service(self) -> DaemonReadingService:
        """Get memoized reading-service instance with explicit daemon dependencies."""
        if self._reading_service is None:
            from .daemon_reading import DaemonReadingDeps, DaemonReadingService

            self._reading_service = DaemonReadingService(
                DaemonReadingDeps(
                    conn=self._conn,
                    sync_db_path=self._sync_db_path,
                    self_id=self.self_id,
                    resolve_dialog_id=self._resolve_dialog_id,
                    fragment_context=FragmentContextService(
                        self._conn,
                        TelethonTelegramFragmentGateway(self._client),
                    ),
                    reaction_freshener=self._reaction_freshener,
                    history_gateway=TelethonTelegramHistoryGateway(self._client),
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
        message_state: str,
        topic_id: int | None,
    ) -> tuple[int | None, str] | dict:
        """Delegate history-navigation decoding to the reading service."""
        from .daemon_reading import DaemonReadingService

        return DaemonReadingService._decode_history_navigation(
            navigation,
            dialog_id,
            direction,
            message_state,
            topic_id,
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
            group_messages, freshness = await self._enrich_unread_rows(chat_id, rows)
            group["messages"] = [dataclasses.asdict(m) for m in group_messages]
            if freshness is not None:
                group["reaction_freshness"] = freshness.as_dict()
            groups.append(group)

        return groups

    async def _enrich_unread_rows(
        self,
        dialog_id: int,
        rows: list[Mapping[str, object]],
    ) -> tuple[list[ReadMessage], ReactionFreshness | None]:
        """Freshen and render reactions for one unread group."""
        message_ids = [int(cast(int | str, row["message_id"])) for row in rows]
        if not message_ids:
            return [_read_message_from_row(row) for row in rows], None

        freshness = await self._reaction_freshener.refresh(dialog_id, dialog_id, message_ids)
        reaction_map = fetch_reaction_counts(self._conn, dialog_id, message_ids)
        messages = [
            _read_message_from_row(
                row,
                reactions_display=format_reaction_counts(reaction_map.get(int(cast(int | str, row["message_id"])), [])),
            )
            for row in rows
        ]
        return messages, freshness

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
