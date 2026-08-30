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
  - get_entity_info: type-tagged entity profile, DB-first with configured entity-detail TTL
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
from telethon.errors import FloodWaitError, RPCError  # type: ignore[import-untyped]
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
from .daemon_dialog_queries import (
    _COUNT_SYNCED_MESSAGES_SQL,
    _GET_ACCESS_LOST_ALERTS_SQL,
    _GET_DELETED_ALERTS_SQL,
    _GET_EDIT_ALERTS_SQL,
    _GET_SYNC_STATUS_SQL,
    _LIST_TOPICS_SQL,
)
from .daemon_entity_info import DaemonEntityInfoService, EntityInfoDeps
from .daemon_message import (
    project_cached_message_facts_by_dialog,
)
from .dialog_selector import DialogSelector, DialogSelectorError, required_dialog_selector
from .entity_store import EntitySnapshot, upsert_entity_snapshots
from .flood import flood_seconds
from .folders.read_model import dialog_placement, folder_snapshot, folders_by_dialog, list_folder_messages, list_folders
from .history_enrollment import disable_history, enable_history, read_intent
from .important_events.read_model import list_important_events as read_important_events
from .models import ReadMessage
from .reading import ReadingDeps, ReadingService
from .reading.query_records import read_message_from_row
from .sync_read_model import build_sync_read_model, compute_sync_coverage
from .telegram_rpc import FloodWaitErrors
from .topics.contracts import TopicSourceUnavailableError
from .topics.refresh import TopicRefresher

# Entity / telemetry SQL
_ALL_ENTITY_NAMES_SQL = (
    "SELECT id, name FROM entities "
    "WHERE name IS NOT NULL "
    "AND ((type IN ('User', 'Bot') AND updated_at > ?) "  # PascalCase per ListDialogs type vocabulary
    "OR (type NOT IN ('User', 'Bot') AND updated_at > ?))"
)
_ALL_ENTITY_NAMES_NORMALIZED_SQL = (
    "SELECT id, name_normalized FROM entities "
    "WHERE name_normalized IS NOT NULL "
    "AND ((type IN ('User', 'Bot') AND updated_at > ?) "  # PascalCase per ListDialogs type vocabulary
    "OR (type NOT IN ('User', 'Bot') AND updated_at > ?))"
)
_ENTITY_BY_USERNAME_SQL = "SELECT id, name, username, type FROM entities WHERE username = ? COLLATE NOCASE"


@dataclasses.dataclass(frozen=True, slots=True)
class DaemonApiPolicy:
    """Operator-controlled cache and retention policy supplied by the daemon root."""

    read_at_ttl_seconds: int
    entity_detail_ttl_seconds: int
    user_directory_ttl_seconds: int
    group_directory_ttl_seconds: int
    resolver_enrichment_ttl_seconds: int
    folder_snapshot_stale_after_seconds: int
    telemetry_retention_ttl_seconds: int
    slow_request_seconds: float


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


def _topic_icons_need_refresh(rows: list[tuple[object, object, object, object, object, object]]) -> bool:
    """Whether a cached custom topic icon still lacks its Unicode fallback."""
    return any(row[2] is not None and row[4] is None for row in rows)


from .feedback_service import FeedbackService
from .reactions.refresh import ReactionFreshener
from .telegram_fragments import FragmentContextService, TelethonTelegramFragmentGateway
from .telegram_history import TelethonTelegramHistoryGateway


class _LoggerLike(Protocol):
    def debug(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def info(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def warning(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def error(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def exception(self, msg: str, *_args: object, **_kwargs: object) -> None: ...


class DaemonClientLike(Protocol):
    async def get_entity(self, entity_id: str | int) -> object: ...

    def iter_dialogs(self) -> AsyncIterator[object]: ...

    async def get_me(self) -> object | None: ...

    async def get_input_entity(self, dialog_id: int) -> object: ...

    async def get_messages(self, entity: object, ids: list[int]) -> object: ...

    def iter_participants(self, peer: object, limit: int = 0) -> AsyncIterator[object]: ...

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]: ...

    async def __call__(self, request: object) -> object: ...


class DaemonHealthStatus(Protocol):
    """Daemon-wide operational health gate exposed to the API boundary."""

    @property
    def open(self) -> bool: ...

    def detail(self) -> str: ...


class _HealthyDaemonStatus:
    @property
    def open(self) -> bool:
        return False

    def detail(self) -> str:
        return "daemon health gate is closed"


def _healthy_daemon_status() -> DaemonHealthStatus:
    return _HealthyDaemonStatus()


type _DispatchHandler = Callable[
    [dict[str, object]],
    Awaitable[dict[str, object]] | dict[str, object],
]


if TYPE_CHECKING:
    from .daemon_account_trace import _AccountTraceClientLike
    from .daemon_account_trace import _LoggerLike as AccountTraceLoggerLike
    from .pagination import HistoryDirection
else:
    _AccountTraceClientLike = object
    AccountTraceLoggerLike = object

# Phase 39.2 §Key technical decisions: per-message TTL for JIT reactions freshen-on-read.
# Amortizes rapid paginated reads on the same ids; live events catch most mutations.
_TELEMETRY_TOOL_NAME_MAX_LEN = 200
_UPSERT_ENTITIES_MAX_LEN = 10000


from .resolver import (
    Candidates,
    MatchInfo,
    NotFound,
    Resolved,
    ResolverEnrichmentPolicy,
    _fuzzy_resolve,
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


@dataclasses.dataclass(frozen=True, slots=True)
class _ResolverEntityCache:
    """SQLite-backed resolver cache used only with an explicit TTL policy."""

    conn: sqlite3.Connection

    def get(self, entity_id: int, ttl_seconds: int) -> Mapping[str, object] | None:
        row = cast(
            tuple[str | None, str | None] | None,
            self.conn.execute(
                "SELECT username, type FROM entities WHERE id = ? AND updated_at > ?",
                (entity_id, int(time.time()) - ttl_seconds),
            ).fetchone(),
        )
        if row is None:
            return None
        return {"username": row[0], "type": row[1]}

    def get_by_username(self, username: str) -> tuple[int, str] | None:
        row = cast(
            tuple[int, str | None] | None,
            self.conn.execute(_ENTITY_BY_USERNAME_SQL, (username,)).fetchone(),
        )
        if row is None:
            return None
        return (int(row[0]), str(row[1] or f"@{username}"))


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

    def __init__(  # noqa: PLR0913
        self,
        conn: sqlite3.Connection,
        client: DaemonClientLike,
        shutdown_event: asyncio.Event,
        feedback_service: FeedbackService | None = None,
        sync_db_path: Path | None = None,
        *,
        reaction_freshener: ReactionFreshener,
        hydration_requester: Callable[[sqlite3.Connection, int, int], None] | None = None,
        topic_refresher: TopicRefresher | None = None,
        policy: DaemonApiPolicy,
        health_status: Callable[[], DaemonHealthStatus] = _healthy_daemon_status,
    ) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._sync_db_path = _resolve_sync_db_path(conn, sync_db_path)
        self._feedback_service = feedback_service
        self._client = client
        self._shutdown_event = shutdown_event
        # Phase 39.1: cached authenticated user id, populated once by
        # sync_main() after client.connect() completes (see daemon.py).
        # Query-build paths (Plan 39.1-02) read this as a bound SQL parameter
        # to collapse DM direction (`out`) into an effective sender id without
        # calling Telethon on every read.
        self.self_id: int | None = None
        self.self_profile: dict[str, object] | None = None
        # Set to True once Telegram is connected and all startup steps complete.
        # While False, handle_client returns daemon_not_ready with startup_detail.
        self._ready: bool = False
        self.startup_detail: str = "connecting to Telegram"
        self._reading_service: ReadingService | None = None
        self._topic_refresher = topic_refresher
        self._hydration_requester = hydration_requester
        self._policy = policy
        self._health_status = health_status
        self._activity_stats_service: _activity_stats.DaemonActivityStatsService | None = None

    def _get_reading_service(self) -> ReadingService:
        """Get memoized reading-service instance with explicit daemon dependencies."""
        if self._reading_service is None:
            self._reading_service = ReadingService(
                ReadingDeps(
                    conn=self._conn,
                    sync_db_path=self._sync_db_path,
                    self_id=self.self_id,
                    resolve_dialog_id=self._resolve_dialog_id,
                    fragment_context=FragmentContextService(
                        self._conn,
                        TelethonTelegramFragmentGateway(self._client),
                    ),
                    history_gateway=TelethonTelegramHistoryGateway(self._client),
                    logger=cast(_LoggerLike, logger),
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

        health_status = self._health_status()
        if health_status.open:
            return (
                {
                    "ok": False,
                    "error": "flood_wait_kill_switch_open",
                    "detail": health_status.detail(),
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
        started_at = time.perf_counter()
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

        self._log_request_completion(method, request_id, response, time.perf_counter() - started_at)

        if request_id:
            response = {**response, "request_id": request_id}
        return response, method, request_id

    def _log_request_completion(
        self,
        method: str,
        request_id: str | None,
        response: Mapping[str, object],
        duration_s: float,
    ) -> None:
        ok = bool(response.get("ok"))
        if ok and duration_s < self._policy.slow_request_seconds:
            return
        logger.info(
            "daemon_api_request_complete method=%s ok=%s duration_s=%.3f request_id=%s error=%s",
            method,
            ok,
            duration_s,
            request_id,
            response.get("error"),
        )

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
            "search_messages": self._search_messages,
            "trace_account_messages": self._trace_account_messages,
            "list_dialogs": self._list_dialogs,
            "get_unread_summary": self._get_unread_summary,
            "list_folders": self._list_folders,
            "list_folder_messages": self._list_folder_messages,
            "list_topics": self._list_topics,
            "get_me": self._get_me,
            "mark_dialog_for_sync": self._mark_dialog_for_sync,
            "get_sync_status": self._get_sync_status,
            "get_sync_alerts": self._get_sync_alerts,
            "list_important_events": self._list_important_events,
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

    # ------------------------------------------------------------------
    # Dialog name resolution
    # ------------------------------------------------------------------

    def _local_dialog_metadata(self) -> dict[int, tuple[str | None, str | None, bool]]:
        """Read dialog eligibility from the canonical snapshot without column-order assumptions."""
        cursor = self._conn.execute(
            "SELECT d.*, sd.status AS selector_sync_status "
            "FROM dialogs d LEFT JOIN synced_dialogs sd ON sd.dialog_id = d.dialog_id "
            "ORDER BY d.dialog_id"
        )
        columns = [str(description[0]) for description in cursor.description or ()]
        metadata: dict[int, tuple[str | None, str | None, bool]] = {}
        rows = cast(list[Sequence[object]], cursor.fetchall())
        for raw_row in rows:
            row = dict(zip(columns, raw_row, strict=True))
            dialog_id = _coerce_int(row.get("dialog_id"), 0)
            if dialog_id == 0:
                continue
            raw_name = row.get("name")
            raw_type = row.get("type")
            name = raw_name if isinstance(raw_name, str) else None
            entity_type = raw_type if isinstance(raw_type, str) else None
            eligible = not bool(row.get("hidden", 0)) or row.get("selector_sync_status") == "access_lost"
            metadata[dialog_id] = (name, entity_type, eligible)
        return metadata

    def _local_dialog_directory(
        self,
    ) -> tuple[
        dict[int, str],
        dict[int, str],
        dict[int, str],
        dict[int, str],
        dict[int, str],
        set[int],
    ]:
        """Return exact-name cache entries and fuzzy-eligible dialog entries."""
        dialog_metadata = self._local_dialog_metadata()
        names: dict[int, str] = {}
        normalized: dict[int, str] = {}
        fuzzy_names: dict[int, str] = {}
        fuzzy_normalized: dict[int, str] = {}
        entity_types: dict[int, str] = {}
        ineligible_ids = {entity_id for entity_id, (_name, _type, eligible) in dialog_metadata.items() if not eligible}
        entity_rows = cast(
            list[tuple[object, ...]],
            self._conn.execute("SELECT id, name, type FROM entities WHERE name IS NOT NULL ORDER BY id").fetchall(),
        )
        entity_names = {_coerce_int(row[0], 0): row[1] for row in entity_rows}
        stored_entity_types = {
            _coerce_int(row[0], 0): row[2] for row in entity_rows if isinstance(row[2], str) and row[2]
        }
        for entity_id in sorted(set(entity_names) | set(dialog_metadata)):
            dialog_name, dialog_type, eligible = dialog_metadata.get(entity_id, (None, None, True))
            if not eligible:
                continue
            entity_name = entity_names.get(entity_id)
            name = dialog_name or (entity_name if isinstance(entity_name, str) else None)
            if entity_id == 0 or not isinstance(name, str) or not name.strip():
                continue
            names[entity_id] = name
            normalized[entity_id] = latinize(name)
            if dialog_type is not None:
                entity_types[entity_id] = dialog_type
            elif entity_id in stored_entity_types:
                entity_types[entity_id] = stored_entity_types[entity_id]
            if entity_id in dialog_metadata:
                fuzzy_names[entity_id] = name
                fuzzy_normalized[entity_id] = latinize(name)
        return names, normalized, fuzzy_names, fuzzy_normalized, entity_types, ineligible_ids

    @staticmethod
    def _resolve_exact_natural_name(
        query: str,
        names: dict[int, str],
        normalized: dict[int, str],
        entity_types: Mapping[int, str],
    ) -> Resolved | Candidates | None:
        norm_query = latinize(query)
        exact_names = {entity_id: name for entity_id, name in names.items() if normalized.get(entity_id) == norm_query}
        if not exact_names:
            return None
        exact_normalized = dict.fromkeys(exact_names, norm_query)
        result = _fuzzy_resolve(query, exact_names, normalized_name_map=exact_normalized)
        DaemonAPIServer._apply_dialog_candidate_types(result, entity_types)
        return result if isinstance(result, Resolved | Candidates) else None

    @staticmethod
    def _apply_dialog_candidate_types(
        result: Resolved | Candidates | NotFound,
        entity_types: Mapping[int, str],
    ) -> None:
        """Replace resolver id-sign guesses with observed local types or unknown."""
        if isinstance(result, Candidates):
            for match in result.matches:
                match["entity_type"] = entity_types.get(match["entity_id"])

    def _resolve_local_dialog_username(self, username: str, query: str) -> Resolved | Candidates | NotFound:
        dialog_metadata = self._local_dialog_metadata()
        rows = cast(
            list[tuple[object, ...]],
            self._conn.execute(_ENTITY_BY_USERNAME_SQL, (username,)).fetchall(),
        )
        matches: list[MatchInfo] = []
        for row in rows:
            entity_id = _coerce_int(row[0], 0)
            dialog_name, dialog_type, eligible = dialog_metadata.get(entity_id, (None, None, True))
            if entity_id == 0 or not eligible:
                continue
            name = str(dialog_name or row[1] or f"@{username}")
            matches.append(
                {
                    "entity_id": entity_id,
                    "display_name": name,
                    "score": 100,
                    "username": str(row[2]) if row[2] is not None else username,
                    "entity_type": dialog_type or (str(row[3]) if row[3] is not None else None),
                    "disambiguation_hint": None,
                }
            )
        matches.sort(key=lambda item: (item["display_name"].casefold(), item["entity_id"]))
        if len(matches) == 1:
            match = matches[0]
            return Resolved(entity_id=match["entity_id"], display_name=match["display_name"])
        if matches:
            return Candidates(query=query, matches=matches)
        return NotFound(query=query)

    async def _resolve_dialog_entity(self, dialog: str) -> int | None:
        """Resolve a dialog selector through the live Telegram entity lookup."""
        try:
            entity = await self._client.get_entity(dialog)
            return int(cast(int, telethon_utils.get_peer_id(entity)))
        except ValueError, KeyError:
            return None
        except FloodWaitErrors:
            raise
        except RPCError, TimeoutError:
            raise
        except Exception:
            logger.exception("unexpected get_entity failure for %r", dialog)
            raise

    async def _remote_dialog_directory(
        self,
        *,
        excluded_ids: set[int],
    ) -> tuple[dict[int, str], dict[int, str]]:
        """Enumerate the full visible remote directory before fuzzy resolution."""
        names: dict[int, str] = {}
        normalized: dict[int, str] = {}
        async for remote_dialog in self._client.iter_dialogs():
            name = _attr(remote_dialog, "name", "")
            entity = _attr(remote_dialog, "entity", None)
            if not isinstance(name, str) or not name.strip() or entity is None:
                continue
            entity_id = int(cast(int, telethon_utils.get_peer_id(entity)))
            if entity_id in excluded_ids:
                continue
            names[entity_id] = name
            normalized[entity_id] = latinize(name)
        return names, normalized

    async def _resolve_dialog_username(
        self,
        query: str,
        username: str,
        *,
        allow_remote_lookup: bool,
        ineligible_ids: set[int],
    ) -> Resolved | Candidates | NotFound:
        local = self._resolve_local_dialog_username(username, query)
        if not isinstance(local, NotFound) or not allow_remote_lookup:
            return local
        entity_id = await self._resolve_dialog_entity(query)
        if entity_id is not None and entity_id not in ineligible_ids:
            return Resolved(entity_id=entity_id, display_name=query)
        return local

    @staticmethod
    def _sorted_dialog_matches(matches: list[MatchInfo]) -> list[MatchInfo]:
        return sorted(
            matches,
            key=lambda item: (-item["score"], item["display_name"].casefold(), item["entity_id"]),
        )

    async def _resolve_dialog_name(
        self,
        dialog: str,
        *,
        allow_remote_lookup: bool = True,
    ) -> Resolved | Candidates | NotFound:
        """Resolve one normalized natural selector without silent precedence."""
        tme = _parse_tme_link(dialog)
        username = tme[0] if tme is not None else dialog[1:] if dialog.startswith("@") else None
        if username is not None:
            ineligible_ids = {
                entity_id
                for entity_id, (_name, _type, eligible) in self._local_dialog_metadata().items()
                if not eligible
            }
            return await self._resolve_dialog_username(
                dialog,
                username,
                allow_remote_lookup=allow_remote_lookup,
                ineligible_ids=ineligible_ids,
            )

        (
            local_names,
            local_normalized,
            fuzzy_names,
            fuzzy_normalized,
            entity_types,
            ineligible_ids,
        ) = self._local_dialog_directory()
        exact_result = self._resolve_exact_natural_name(dialog, local_names, local_normalized, entity_types)
        if exact_result is not None:
            return exact_result
        local_result = _fuzzy_resolve(dialog, fuzzy_names, normalized_name_map=fuzzy_normalized)
        self._apply_dialog_candidate_types(local_result, entity_types)
        if not allow_remote_lookup:
            return local_result

        logger.debug("resolve_dialog_fallback_iter_dialogs query=%r", dialog)
        remote_names, remote_normalized = await self._remote_dialog_directory(excluded_ids=ineligible_ids)
        all_names = {**fuzzy_names, **remote_names}
        all_normalized = {**fuzzy_normalized, **remote_normalized}
        exact_result = self._resolve_exact_natural_name(dialog, all_names, all_normalized, entity_types)
        if exact_result is not None:
            return exact_result
        result = _fuzzy_resolve(dialog, all_names, normalized_name_map=all_normalized)
        self._apply_dialog_candidate_types(result, entity_types)
        return result

    @staticmethod
    def _dialog_resolution_retryable_response(*, retry_after: int | None = None) -> dict[str, object]:
        response: dict[str, object] = {
            "ok": False,
            "error": "dialog_resolution_retryable",
            "message": "Telegram dialog enumeration did not complete; no dialog was selected.",
            "retryable": True,
            "required_action": "Retry the request; use an exact dialog id when already known.",
        }
        if retry_after is not None:
            response["retry_after"] = retry_after
        return response

    async def _resolve_dialog_id(
        self,
        selector: DialogSelector,
        *,
        allow_remote_lookup: bool = True,
    ) -> int | dict:
        """Resolve a validated selector to one id or a stable failure response."""
        if selector.exact_id is not None:
            return selector.exact_id
        assert selector.query is not None
        try:
            result = await self._resolve_dialog_name(selector.query, allow_remote_lookup=allow_remote_lookup)
        except (RPCError, TimeoutError) as exc:
            retry_after = flood_seconds(exc) if isinstance(exc, FloodWaitErrors) else None
            return self._dialog_resolution_retryable_response(retry_after=retry_after)
        if isinstance(result, Resolved):
            return result.entity_id
        if isinstance(result, Candidates):
            candidates = self._sorted_dialog_matches(result.matches)
            if len(candidates) == 1:
                return {
                    "ok": False,
                    "error": "dialog_not_found",
                    "message": f"Dialog {selector.label!r} was not found; one approximate match is available.",
                    "suggestion": candidates[0],
                    "required_action": "Retry with the suggestion's exact dialog id, or refine the dialog name.",
                }
            return {
                "ok": False,
                "error": "ambiguous_dialog",
                "message": f"Dialog {selector.label!r} matched multiple dialogs.",
                "candidates": candidates,
                "required_action": "Retry with an exact dialog id from candidates.",
            }
        return {
            "ok": False,
            "error": "dialog_not_found",
            "message": f"Dialog {selector.label!r} was not found.",
            "required_action": "Call list_dialogs, then retry with an exact dialog id or full dialog name.",
        }

    def _trace_service(self) -> DaemonAccountTraceService:
        return DaemonAccountTraceService(
            DaemonAccountTraceDeps(
                conn=self._conn,
                client=cast(_AccountTraceClientLike, self._client),
                resolve_dialog_id=self._resolve_dialog_id,
                self_id=self.self_id,
                logger=cast(AccountTraceLoggerLike, logger),
                rid=_rid,
                user_directory_ttl_seconds=self._policy.user_directory_ttl_seconds,
                group_directory_ttl_seconds=self._policy.group_directory_ttl_seconds,
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
        return await self._get_reading_service().list_messages_context_window(
            dialog_id=dialog_id,
            anchor_message_id=anchor_message_id,
            context_size=context_size,
        )

    async def _list_messages_from_telegram(self, req: object) -> dict:
        """Delegate Telegram fallback reads to the reading service."""
        return await self._get_reading_service().list_messages_from_telegram(req)

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
        return ReadingService.encode_next_navigation(
            messages=messages,
            limit=limit,
            dialog_id=dialog_id,
            direction=direction,
            direction_enum=direction_enum,
            logger=cast(_LoggerLike, logger),
            request_id=_rid,
        )

    async def _resolve_unread_position(
        self,
        dialog_id: int,
        unread_after_id: int | None,
    ) -> int | None:
        """Delegate unread-position resolution to the reading service."""
        return await self._get_reading_service().resolve_unread_position(dialog_id, unread_after_id)

    async def _list_messages_from_db(self, req: dict[str, object]) -> dict:
        """Delegate sync.db reads to the reading service."""
        # "list_messages rendered"
        return await self._get_reading_service().list_messages_from_db(req)

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
        return ReadingService.decode_history_navigation(
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
        return await self._get_reading_service().list_messages(cast(dict[str, object], req))

    # ------------------------------------------------------------------
    # search_messages
    # ------------------------------------------------------------------

    async def _search_messages(self, req: dict[str, object]) -> dict:
        """Delegate full-text search to the reading service."""
        return await self._get_reading_service().search_messages(cast(dict[str, object], req))

    # ------------------------------------------------------------------
    # list_dialogs
    # ------------------------------------------------------------------

    async def _list_dialogs(self, req: dict[str, object]) -> dict:
        """Delegate list_dialogs reads to the reading service."""
        result = await self._get_reading_service().list_dialogs(cast(dict[str, object], req))
        if not result.get("ok"):
            return result
        memberships = folders_by_dialog(self._conn)
        requested_folder = req.get("folder_id")
        raw_limit = req.get("limit")
        limit = None if raw_limit is None else _clamp(_coerce_int(raw_limit, 100), 1, 500)
        data = cast(dict[str, object], result.get("data", {}))
        dialogs = cast(list[dict[str, object]], data.get("dialogs", []))
        enriched = []
        for dialog in dialogs:
            folders = memberships.get(int(cast(int | str, dialog["id"])), [])
            ids = [int(cast(int | str, folder["id"])) for folder in folders]
            dialog["folder_ids"] = ids
            dialog["folders"] = folders
            if requested_folder is None or int(cast(int | str, requested_folder)) in ids:
                enriched.append(dialog)
                if limit is not None and len(enriched) >= limit:
                    break
        data["dialogs"] = enriched
        data["folder_snapshot"] = folder_snapshot(
            self._conn,
            stale_after_seconds=self._policy.folder_snapshot_stale_after_seconds,
        )
        return result

    async def _get_unread_summary(self, req: dict[str, object]) -> dict:
        """Delegate the Dialog-projection unread overview to the read service."""
        return await self._get_reading_service().get_unread_summary(cast(dict[str, object], req))

    async def _list_folders(self, _req: dict[str, object]) -> dict:
        return {
            "ok": True,
            "data": {
                "folders": list_folders(self._conn),
                "folder_snapshot": folder_snapshot(
                    self._conn,
                    stale_after_seconds=self._policy.folder_snapshot_stale_after_seconds,
                ),
            },
        }

    async def _list_folder_messages(self, req: dict[str, object]) -> dict:
        folder_id = int(cast(int | str, req.get("folder_id", 0)))
        limit = max(1, min(int(cast(int | str, req.get("limit", 20))), 100))
        data = list_folder_messages(self._conn, folder_id, limit)
        raw_messages = cast(list[dict[str, object]], data["messages"])
        messages = [read_message_from_row(row) for row in raw_messages]
        projected = project_cached_message_facts_by_dialog(self._conn, messages)
        data["messages"] = [
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {"text", "media_description", "media_kind", "media_payload"}
                },
                "text": message.text,
                "media_description": message.media_description,
                "media_kind": message.media_kind,
                "content_kind": message.content_kind,
            }
            for row, message in zip(raw_messages, projected, strict=True)
        ]
        data["folder_snapshot"] = folder_snapshot(
            self._conn,
            stale_after_seconds=self._policy.folder_snapshot_stale_after_seconds,
        )
        return {"ok": True, "data": data}

    # list_topics
    # ------------------------------------------------------------------

    async def _list_topics(self, req: dict[str, object]) -> dict:
        """Return topics for a dialog from the canonical topic_metadata snapshot table.

        Normally this is a local read. If the catalog is empty and the daemon has
        a topic refresher, the daemon may perform a one-shot refresh because the
        daemon owns TelegramClient and persistence. MCP callers still go through
        this daemon API boundary and never call Telegram directly.

        Request: dialog_id (int) or dialog (str).
        Response data: {"topics": [{"id", "title", "icon_emoji_id", "icon_emoji", "icon_color", "date"}],
        "dialog_id": int}.
        Errors: missing_dialog, dialog_not_found (from _resolve_dialog_id).
        """
        try:
            selector = required_dialog_selector(exact_id=req.get("dialog_id"), dialog=req.get("dialog"))
        except DialogSelectorError as exc:
            return {"ok": False, "error": exc.code, "message": str(exc)}

        resolved = await self._resolve_dialog_id(selector, allow_remote_lookup=False)
        if isinstance(resolved, dict):
            return resolved
        dialog_id = resolved

        rows = self._topic_rows(dialog_id)
        empty_reason = None
        if (not rows or _topic_icons_need_refresh(rows)) and self._topic_refresher is not None:
            empty_reason = await self._refresh_topic_catalog_for_list_topics(dialog_id)
            rows = self._topic_rows(dialog_id)
        topics = [
            {
                "id": int(cast(int | str, row[0])),
                "title": row[1],
                "icon_emoji_id": row[2],
                "date": row[3],
                "icon_emoji": row[4],
                "icon_color": row[5],
            }
            for row in rows
        ]
        data = {"topics": topics, "dialog_id": dialog_id}
        if not topics and empty_reason is not None:
            data["empty_reason"] = empty_reason
        return {"ok": True, "data": data}

    def _topic_rows(self, dialog_id: int) -> list[tuple[object, object, object, object, object, object]]:
        return cast(
            list[tuple[object, object, object, object, object, object]],
            self._conn.execute(_LIST_TOPICS_SQL, (dialog_id,)).fetchall(),
        )

    async def _refresh_topic_catalog_for_list_topics(self, dialog_id: int) -> str:
        if self._topic_refresher is None:
            return "topic_catalog_not_refreshed"
        try:
            entity = await self._client.get_entity(dialog_id)
            refreshed = await self._topic_refresher.refresh(dialog_id, entity)
        except FloodWaitError as exc:
            logger.info(
                "list_topics_refresh_deferred_flood_wait dialog_id=%d seconds=%s",
                dialog_id,
                getattr(exc, "seconds", None),
            )
            return "topic_catalog_deferred_flood_wait"
        except TopicSourceUnavailableError as exc:
            logger.info("list_topics_refresh_unavailable dialog_id=%d error=%s", dialog_id, exc)
            return "topic_catalog_unavailable"
        return "no_active_topics" if refreshed == 0 else "topic_catalog_refreshed"

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
        """Persist explicit full-history intent and report factual coverage."""
        dialog_id = _coerce_int(req.get("dialog_id", 0), 0)
        enable = bool(req.get("enable", True))
        now = int(time.time())
        outcome = enable_history(self._conn, dialog_id, now=now) if enable else disable_history(self._conn, dialog_id)
        if enable and self._hydration_requester is not None:
            self._hydration_requester(self._conn, dialog_id, now)
        self._conn.commit()
        logger.info("mark_dialog_for_sync dialog_id=%d enable=%s", dialog_id, enable)
        return {
            "ok": True,
            "data": {
                "dialog_id": dialog_id,
                "enabled": outcome.enabled,
                "enrollment_source": outcome.source.value,
                "coverage_status": outcome.coverage_status,
                "action": outcome.action,
                "blocked_reason": outcome.blocked_reason,
                "full_history_will_be_fetched": outcome.full_history_will_be_fetched,
            },
        }

    # ------------------------------------------------------------------
    # get_sync_status
    # ------------------------------------------------------------------

    async def _get_sync_status(self, req: dict[str, object]) -> dict:  # noqa: PLR0914
        """Return sync status and message statistics for a dialog.

        delete_detection is derived from dialog_id sign:
        - Negative → channel/supergroup → "reliable (channel)"
        - Positive → DM/small group → "best-effort weekly (DM)"
        """
        dialog_id = _coerce_int(req.get("dialog_id", 0), 0)
        row = cast(tuple[object, ...] | None, self._conn.execute(_GET_SYNC_STATUS_SQL, (dialog_id,)).fetchone())

        if row is not None:
            status = str(row[0])
            last_synced_at = cast(int | None, row[1])
            last_event_at = cast(int | None, row[2])
            sync_progress = cast(int | None, row[3])
            total_messages = cast(int | None, row[4])
            access_lost_at = cast(int | None, row[5])
            last_delta_checked_at = cast(int | None, row[6])
            delta_refresh_requested_at = cast(int | None, row[7])
            access_revalidation = (cast(int | None, row[8]), cast(int | None, row[9]))
            enrollment_enabled = bool(row[10]) if row[10] is not None else None
            enrollment_source = cast(str | None, row[11])
        else:
            status = "not_synced"
            last_synced_at = None
            last_event_at = None
            sync_progress = None
            total_messages = None
            access_lost_at = None
            last_delta_checked_at = None
            delta_refresh_requested_at = None
            access_revalidation = (None, None)
            intent = read_intent(self._conn, dialog_id)
            enrollment_enabled = intent.enabled
            enrollment_source = intent.source.value if intent.source else None

        count_row = cast(tuple[object] | None, self._conn.execute(_COUNT_SYNCED_MESSAGES_SQL, (dialog_id,)).fetchone())
        message_count = int(cast(int | str, count_row[0])) if count_row is not None else 0

        data: dict = {
            "dialog_id": dialog_id,
            "message_count": message_count,
            "last_synced_at": last_synced_at,
            "last_event_at": last_event_at,
            "last_delta_checked_at": last_delta_checked_at,
            "delta_refresh_requested_at": delta_refresh_requested_at,
            "sync_progress": sync_progress,
            "sync_progress_message_id": sync_progress,
            "total_messages": total_messages,
            "delete_detection": "reliable (channel)" if dialog_id < 0 else "best-effort weekly (DM)",
            "sync_coverage_pct": compute_sync_coverage(total_messages, message_count),
            "access_lost_at": access_lost_at,
            "access_last_revalidated_at": access_revalidation[0],
            "access_next_revalidate_at": access_revalidation[1],
            "enrollment_enabled": enrollment_enabled,
            "enrollment_source": enrollment_source,
            "coverage_status": status,
            "realtime_history": (
                "full"
                if enrollment_enabled and status in {"syncing", "synced"}
                else "own_only"
                if status == "own_only"
                else "none"
            ),
            **build_sync_read_model(
                status=status,
                timestamps=(last_synced_at, last_event_at, last_delta_checked_at),
                local_count=message_count,
                total_messages=total_messages,
            ),
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

    def _list_important_events(self, req: dict[str, object]) -> dict:
        """Return recent daemon-observed important events."""
        last_hours = _clamp(_coerce_int(req.get("last_hours", 24), 24), 1, 24 * 30)
        timezone = req.get("timezone", "UTC")
        if not isinstance(timezone, str):
            return {"ok": False, "error": "invalid_input", "message": "timezone must be a string"}
        try:
            events = read_important_events(self._conn, last_hours=last_hours, timezone=timezone)
        except ValueError, TypeError:
            return {"ok": False, "error": "invalid_input", "message": "timezone must be a valid IANA timezone"}
        return {"ok": True, "data": {"timezone": timezone, "last_hours": last_hours, "events": events}}

    async def _get_entity_info(self, req: dict[str, object]) -> dict:
        """Type-tagged entity inspector covering 5 Telegram entity kinds."""
        service = DaemonEntityInfoService(
            EntityInfoDeps(
                conn=self._conn,
                client=cast(DaemonClientLike, self._client),
                dm_peer_ids=self._dm_peer_ids,
                self_id=self.self_id,
                self_profile=self.self_profile,
                get_peer_id=telethon_utils.get_peer_id,
                rid=_rid,
                logger=cast(logging.Logger, logger),
                now_provider=time.time,
                detail_ttl_seconds=self._policy.entity_detail_ttl_seconds,
                slow_stage_seconds=self._policy.slow_request_seconds,
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
        result = await service.get_entity_info(req)
        if result.get("ok"):
            data = cast(dict[str, object], result.get("data", {}))
            entity_id = data.get("id")
            if isinstance(entity_id, int):
                data["dialog_placement"] = dialog_placement(self._conn, entity_id)
        return result

    # ------------------------------------------------------------------
    # list_unread_messages
    # ------------------------------------------------------------------

    async def _list_unread_messages(self, req: dict[str, object]) -> dict:
        """Delegate get_inbox orchestration to the reading application service."""
        return await self._get_reading_service().list_unread_messages(req)

    # ------------------------------------------------------------------
    # record_telemetry
    # ------------------------------------------------------------------

    async def _record_telemetry(self, req: dict[str, object]) -> dict:
        """Write a telemetry event row to sync.db telemetry_events table.

        Evicts rows older than the configured retention window on every write.
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
            cutoff = time.time() - self._policy.telemetry_retention_ttl_seconds
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
        """Delegate feedback submission to the daemon-wired application service."""
        if self._feedback_service is None:
            return {"ok": False, "error": "internal", "message": "feedback database not initialised"}
        return self._feedback_service.submit_feedback(req)

    # ------------------------------------------------------------------
    # update_feedback_status
    # ------------------------------------------------------------------

    async def _update_feedback_status(self, req: dict) -> dict:
        """Delegate feedback status changes to the daemon-wired application service."""
        if self._feedback_service is None:
            return {"ok": False, "error": "internal", "message": "feedback database not initialised"}
        return self._feedback_service.update_feedback_status(req)

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
            upsert_entity_snapshots(
                self._conn,
                [
                    EntitySnapshot(
                        entity_id=cast(int, e["id"]),
                        entity_type=cast(str, e["type"]),
                        name=cast(str | None, e.get("name") or None),
                        username=cast(str | None, e.get("username")),
                        name_normalized=latinize(str(e["name"])) if e.get("name") else None,
                        updated_at=now,
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
                self._conn.execute(
                    _ALL_ENTITY_NAMES_SQL,
                    (now - self._policy.user_directory_ttl_seconds, now - self._policy.group_directory_ttl_seconds),
                ).fetchall(),
            )
        )
        normalized = dict(
            cast(
                list[tuple[int, str]],
                self._conn.execute(
                    _ALL_ENTITY_NAMES_NORMALIZED_SQL,
                    (now - self._policy.user_directory_ttl_seconds, now - self._policy.group_directory_ttl_seconds),
                ).fetchall(),
            )
        )

        result = resolve_entity_sync(
            query,
            display_name_map,
            ResolverEnrichmentPolicy(
                entity_cache=_ResolverEntityCache(self._conn),
                ttl_seconds=self._policy.resolver_enrichment_ttl_seconds,
            ),
            normalized_name_map=normalized,
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
