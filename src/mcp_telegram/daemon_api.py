"""Daemon API server — Unix socket request dispatcher.

DaemonAPIServer listens on a Unix domain socket and handles seventeen methods:
  - list_messages: read from sync.db (synced dialogs) or Telegram (on-demand)
  - search_messages: FTS5 stemmed full-text search against messages_fts
  - trace_account_messages: observable authored-message evidence for one account
  - list_dialogs: canonical local dialog list enriched with sync_status
  - list_topics: forum topic list via Telegram API
  - get_me: current user info via Telegram API
  - mark_dialog_for_sync: add/remove dialog from sync scope
  - get_sync_status: sync status and message statistics for a dialog
  - list_conversation_changes: durable edits, deletions, and access changes
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
is present, _resolve_dialog_name() resolves it from the canonical local dialog
directory. Explicit usernames may use one targeted exact-peer lookup.

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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, SupportsIndex, cast

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.channels import (
    GetFullChannelRequest,  # type: ignore[import-untyped]
    GetParticipantsRequest,  # type: ignore[import-untyped]
)
from telethon.tl.functions.messages import (  # type: ignore[import-untyped]
    GetCommonChatsRequest,
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
from .auth_scope import TelegramAuthScope
from .conversation_changes import ConversationChangesTokenCodec, query_conversation_changes
from .daemon_account_trace import (
    DaemonAccountTraceDeps,
    DaemonAccountTraceService,
)
from .daemon_dialog_queries import (
    _COUNT_SYNCED_MESSAGES_SQL,
    _GET_SYNC_STATUS_SQL,
    _LIST_TOPICS_SQL,
)
from .daemon_entity_info import DaemonEntityInfoService, EntityInfoDeps
from .demand_wiring import DemandOfferSink, offer_durable_demand
from .dialog_directory_coverage import DialogDirectoryCoverage, read_dialog_directory_coverage
from .dialog_selector import DialogSelector, DialogSelectorError, required_dialog_selector
from .entity_profile.ports import ProfilePairObservationHook
from .entity_profile.refresh import RefreshLimits
from .entity_store import EntitySnapshot, upsert_entity_snapshots
from .flood import TelegramRpcThrottled
from .folders.read_model import dialog_placement, folder_snapshot, folder_summaries, folders_by_dialog
from .history_enrollment import disable_history, enable_history, read_intent
from .models import ReadMessage
from .reading import ReadingDeps, ReadingService
from .runtime_observations import (
    RuntimeObservationPolicy,
    prune_runtime_observations,
    record_runtime_observation,
    tool_telemetry_identity,
)
from .sync_read_model import SyncStatus, build_sync_read_model
from .telegram_demand import AcquisitionKind
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    RpcAdmissionError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    UnclassifiedTelegramRpcError,
    rpc_scope,
)
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
_TELEMETRY_OUTCOMES = frozenset({"success", "tool_error", "validation_error", "exception", "cancelled"})
_TELEMETRY_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_runtime_event_write_count = 0


@contextmanager
def _preserve_or_rpc_scope(
    source: TelegramRpcSource,
    *,
    acquisition_kind: AcquisitionKind | None = None,
) -> Iterator[None]:
    """Classify direct calls and refine, but never replace, an active root."""
    with rpc_scope(source, acquisition_kind=acquisition_kind):
        yield


def _rpc_busy_response() -> dict[str, object]:
    """Project recoverable admission pressure without exposing scheduler internals."""
    return {
        "ok": False,
        "error": "telegram_rpc_busy",
        "message": "Telegram request capacity is currently busy; retry shortly or narrow the request.",
        "retryable": True,
        "required_action": "Retry shortly, or narrow the request scope.",
    }


def _rpc_admission_response(error: RpcAdmissionError) -> dict[str, object] | None:
    """Project typed pre-transport admission rejection."""
    if not isinstance(error, (RpcAdmissionSaturatedError, RpcAdmissionExpiredError)):
        return None
    return _rpc_busy_response()


def _telemetry_input_error(message: str) -> dict[str, object]:
    return {"ok": False, "error": "invalid_input", "message": message}


def _normalize_telemetry_outcome(event: dict[str, object]) -> tuple[str, str | None] | dict[str, object]:
    raw_outcome = event.get("outcome")
    if raw_outcome is None:
        raw_outcome = "exception" if event.get("error_type") is not None else "success"
    if not isinstance(raw_outcome, str) or raw_outcome not in _TELEMETRY_OUTCOMES:
        return _telemetry_input_error("outcome is invalid")
    raw_error_code = event.get("error_code")
    if raw_outcome == "success":
        normalized_error_code = None
    elif isinstance(raw_error_code, str) and _TELEMETRY_ERROR_CODE_RE.fullmatch(raw_error_code):
        normalized_error_code = raw_error_code
    else:
        normalized_error_code = raw_outcome if raw_outcome != "tool_error" else "tool_error"
    return raw_outcome, normalized_error_code


def _normalize_telemetry_event(
    req: dict[str, object],
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    event_obj = req.get("event")
    if not isinstance(event_obj, dict):
        return None, _telemetry_input_error("event must be a JSON object")
    event = dict(cast(Mapping[str, object], event_obj))
    tool_name = event.get("tool_name", "")
    if not isinstance(tool_name, str) or len(tool_name) > _TELEMETRY_TOOL_NAME_MAX_LEN:
        return None, _telemetry_input_error("tool_name must be a string (max 200 chars)")
    event["tool_name"] = tool_name
    event["tool_capability"], event["contract_version"] = tool_telemetry_identity(tool_name)
    return event, None


def _insert_telemetry_row(
    conn: sqlite3.Connection,
    event: Mapping[str, object],
) -> None:
    record_runtime_observation(
        conn,
        kind="mcp.call",
        observed_at_ms=int(float(cast(float, event.get("timestamp", time.time()))) * 1000),
        tool_name=str(event["tool_name"]),
        tool_capability=cast(str | None, event.get("tool_capability")),
        contract_version=cast(int | None, event.get("contract_version")),
        duration_ms=float(cast(float, event.get("duration_ms", 0))),
        result_count=int(cast(int, event.get("result_count", 0))),
        has_cursor=bool(event.get("has_cursor")),
        page_depth=int(cast(int, event.get("page_depth", 1))),
        has_filter=bool(event.get("has_filter")),
        outcome=str(event.get("outcome", "success")),
        reason_code=cast(str | None, event.get("error_code")),
        error_type=cast(str | None, event.get("error_type")),
    )


def _write_telemetry(
    conn: sqlite3.Connection,
    policy: DaemonApiPolicy,
    event: Mapping[str, object],
) -> None:
    global _runtime_event_write_count
    _insert_telemetry_row(conn, event)
    _runtime_event_write_count += 1
    if _runtime_event_write_count % 128 == 0:
        prune_runtime_observations(
            conn,
            ttl_seconds=policy.telemetry.retention_ttl_seconds,
            row_cap=policy.telemetry.runtime_observations.row_cap,
        )
    conn.commit()


class TelemetryPolicy(Protocol):
    """Hierarchical telemetry policy supplied by the typed config root."""

    @property
    def retention_ttl_seconds(self) -> int: ...

    @property
    def runtime_observations(self) -> RuntimeObservationPolicy: ...


@dataclasses.dataclass(frozen=True, slots=True)
class DaemonApiPolicy:
    """Operator-controlled cache and retention policy supplied by the daemon root."""

    read_at_ttl_seconds: int
    deleted_message_visibility_seconds: int
    entity_detail_ttl_seconds: int
    user_directory_ttl_seconds: int
    group_directory_ttl_seconds: int
    resolver_enrichment_ttl_seconds: int
    folder_snapshot_stale_after_seconds: int
    telemetry: TelemetryPolicy
    slow_request_seconds: float
    entity_profile: RefreshLimits
    full_user_pair_enabled: bool = False


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
    apply_disambiguation_hint,
    latinize,
)
from .resolver import (
    resolve as resolve_entity_sync,
)

logger = logging.getLogger(__name__)

type _DialogMetadata = Mapping[int, tuple[str | None, str | None, bool, str | None, bool | None]]
type _DialogDirectoryResult = tuple[
    dict[int, str],
    dict[int, str],
    dict[int, str],
    dict[int, str],
    dict[int, str],
    set[int],
    DialogDirectoryCoverage,
]


class ResolvedDialogId(int):
    """An int-compatible dialog id carrying the local directory receipt."""

    coverage: DialogDirectoryCoverage

    def __new__(cls, entity_id: int, coverage: DialogDirectoryCoverage) -> ResolvedDialogId:
        value = cast(ResolvedDialogId, int.__new__(cls, entity_id))
        value.coverage = coverage
        return value

    def __reduce_ex__(
        self, protocol: SupportsIndex
    ) -> tuple[type[ResolvedDialogId], tuple[int, DialogDirectoryCoverage]]:
        """Preserve the receipt when ``copy.deepcopy`` crosses message storage."""
        del protocol
        return type(self), (int(self), self.coverage)


def _selector_dialog_name(entity_id: int, dialog_name: str | None, entity_name: object) -> str | None:
    name = dialog_name or (entity_name if isinstance(entity_name, str) else None)
    if entity_id == 0 or not isinstance(name, str) or not name.strip():
        return None
    return name


@dataclasses.dataclass(slots=True)
class _LocalDialogDirectory:
    """Mutable assembly state for one local selector directory snapshot."""

    names: dict[int, str] = dataclasses.field(default_factory=dict)
    normalized: dict[int, str] = dataclasses.field(default_factory=dict)
    fuzzy_names: dict[int, str] = dataclasses.field(default_factory=dict)
    fuzzy_normalized: dict[int, str] = dataclasses.field(default_factory=dict)
    entity_types: dict[int, str] = dataclasses.field(default_factory=dict)
    ineligible_ids: set[int] = dataclasses.field(default_factory=set)

    @classmethod
    def from_metadata(cls, metadata: _DialogMetadata) -> _LocalDialogDirectory:
        return cls(
            ineligible_ids={
                entity_id
                for entity_id, (_name, _type, eligible, _username, _complete) in metadata.items()
                if not eligible
            }
        )

    def add(
        self,
        entity_id: int,
        *,
        stored_name: object,
        stored_type: str | None,
        dialog_metadata: _DialogMetadata,
    ) -> None:
        dialog_name, dialog_type, eligible, _username, _identity_complete = dialog_metadata.get(
            entity_id, (None, None, True, None, None)
        )
        if not eligible:
            return
        name = _selector_dialog_name(entity_id, dialog_name, stored_name)
        if name is None:
            return
        self.names[entity_id] = name
        self.normalized[entity_id] = latinize(name)
        selected_type = dialog_type if dialog_type is not None else stored_type
        if selected_type is not None:
            self.entity_types[entity_id] = selected_type
        if entity_id in dialog_metadata:
            self.fuzzy_names[entity_id] = name
            self.fuzzy_normalized[entity_id] = latinize(name)

    def result(self) -> _DialogDirectoryResult:
        return (
            self.names,
            self.normalized,
            self.fuzzy_names,
            self.fuzzy_normalized,
            self.entity_types,
            self.ineligible_ids,
            # Filled by the server after the candidate snapshot is assembled.
            DialogDirectoryCoverage("never", None, None, None, None, False, False),
        )


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
        folder_projection_reproject: Callable[[], object] | None = None,
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
        self._auth_scope: TelegramAuthScope | None = None
        # Set to True once Telegram is connected and all startup steps complete.
        # While False, handle_client returns daemon_not_ready with startup_detail.
        self._ready: bool = False
        self.startup_detail: str = "connecting to Telegram"
        self._reading_service: ReadingService | None = None
        self._topic_refresher = topic_refresher
        self._folder_projection_reproject = folder_projection_reproject
        self._hydration_requester = hydration_requester
        self._policy = policy
        self._health_status = health_status
        self._activity_stats_service: _activity_stats.DaemonActivityStatsService | None = None
        self._entity_info_service: DaemonEntityInfoService | None = None
        self._demand_sink: DemandOfferSink | None = None
        self._profile_observer: ProfilePairObservationHook | None = None
        self._conversation_changes_token_codec = ConversationChangesTokenCodec()

    def bind_demand_sink(self, sink: DemandOfferSink) -> None:
        """Attach the process-wide coordinator after daemon composition."""
        self._demand_sink = sink
        if self._entity_info_service is not None:
            self._entity_info_service.bind_demand_sink(sink)

    def bind_profile_observer(self, observer: ProfilePairObservationHook) -> None:
        """Attach the process-wide profile telemetry observer after startup composition."""
        self._profile_observer = observer
        if self._entity_info_service is not None:
            self._entity_info_service.bind_profile_observer(observer)

    def _publish_auth_scope(self, scope: TelegramAuthScope | None) -> None:
        """Publish private session identity to already-composed domain services."""
        changed = self._auth_scope != scope
        self._auth_scope = scope
        if changed and self._entity_info_service is not None:
            self._entity_info_service.auth_scope_changed()

    def _require_demand_sink(self) -> DemandOfferSink:
        sink = self._demand_sink
        if sink is None:
            raise RuntimeError("durable demand sink is not bound")
        return sink

    def _reproject_due_folder_memberships(self) -> None:
        if self._folder_projection_reproject is not None:
            self._folder_projection_reproject()

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
                    deleted_message_visibility_seconds=self._policy.deleted_message_visibility_seconds,
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
            response = await self._dispatch_with_error_projection(req, method=method, request_id=request_id)
        finally:
            _current_request_id.reset(token)

        self._log_request_completion(method, request_id, response, time.perf_counter() - started_at)

        if request_id:
            response = {**response, "request_id": request_id}
        return response, method, request_id

    async def _dispatch_with_error_projection(
        self,
        req: dict[str, object],
        *,
        method: str,
        request_id: str | None,
    ) -> dict[str, object]:
        try:
            return await self._dispatch(req)
        except TelegramRpcAdmissionDeferred as exc:
            logger.warning(
                "daemon_api_rpc_admission_deferred retry_after=%s request_id=%s",
                exc.retry_after_seconds,
                request_id,
            )
            return _rpc_busy_response()
        except RpcAdmissionError as exc:
            logger.warning(
                "daemon_api_rpc_admission_rejected source=%s service_class=%s error_type=%s request_id=%s",
                exc.source.value,
                exc.service_class.value,
                type(exc).__name__,
                request_id,
            )
            return _rpc_admission_response(exc) or {
                "ok": False,
                "error": "internal",
                "message": "internal error",
            }
        except UnclassifiedTelegramRpcError:
            logger.exception("daemon_api_unclassified_rpc method=%s request_id=%s", method, request_id)
            return {"ok": False, "error": "internal", "message": "internal error"}
        except Exception:
            logger.exception(
                "daemon_api_dispatch_error method=%s request_id=%s",
                method,
                request_id,
            )
            return {"ok": False, "error": "internal", "message": "internal error"}

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
        log = logger.warning if ok else logger.info
        event = "daemon_api_slow_request" if ok else "daemon_api_request_complete"
        log(
            "%s method=%s ok=%s duration_s=%.3f threshold_s=%.3f request_id=%s error=%s",
            event,
            method,
            ok,
            duration_s,
            self._policy.slow_request_seconds,
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
            "list_topics": self._list_topics,
            "get_me": self._get_me,
            "mark_dialog_for_sync": self._mark_dialog_for_sync,
            "get_sync_status": self._get_sync_status,
            "list_conversation_changes": self._list_conversation_changes,
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

        with _preserve_or_rpc_scope(TelegramRpcSource.MCP_INTERACTIVE):
            result = handler(req)
            if isinstance(result, dict):
                return result
            return cast(dict[str, object], await result)

    # ------------------------------------------------------------------
    # Dialog name resolution
    # ------------------------------------------------------------------

    def _local_dialog_metadata(self) -> dict[int, tuple[str | None, str | None, bool, str | None, bool | None]]:
        """Read dialog eligibility from the canonical snapshot without column-order assumptions."""
        cursor = self._conn.execute(
            "SELECT d.*, sd.status AS selector_sync_status "
            "FROM dialogs d LEFT JOIN synced_dialogs sd ON sd.dialog_id = d.dialog_id "
            "ORDER BY d.dialog_id"
        )
        columns = [str(description[0]) for description in cursor.description or ()]
        metadata: dict[int, tuple[str | None, str | None, bool, str | None, bool | None]] = {}
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
            raw_username = row.get("username")
            username = raw_username if isinstance(raw_username, str) else None
            raw_complete = row.get("identity_complete")
            identity_complete = bool(raw_complete) if raw_complete is not None else None
            metadata[dialog_id] = (name, entity_type, eligible, username, identity_complete)
        return metadata

    def _local_dialog_directory(  # noqa: PLR0914
        self,
    ) -> _DialogDirectoryResult:
        """Return exact-name cache entries and fuzzy-eligible dialog entries."""
        dialog_metadata = self._local_dialog_metadata()
        entity_rows = cast(
            list[tuple[object, ...]],
            self._conn.execute("SELECT id, name, username, type FROM entities ORDER BY id").fetchall(),
        )
        stored_entities = {
            _coerce_int(row[0], 0): (
                row[1],
                row[2] if isinstance(row[2], str) and row[2] else None,
                row[3] if isinstance(row[3], str) and row[3] else None,
            )
            for row in entity_rows
        }
        directory = _LocalDialogDirectory.from_metadata(dialog_metadata)
        for entity_id in sorted(dialog_metadata):
            dialog_name, dialog_type, eligible, _dialog_username, identity_complete = dialog_metadata[entity_id]
            if not eligible:
                continue
            stored_name, _stored_username, stored_type = stored_entities.get(entity_id, (None, None, None))
            # A complete canonical bundle is authoritative, including a known
            # absence (for example a username removed by Telegram).
            if identity_complete is True:
                stored_name = None
                stored_type = None
            name = dialog_name if dialog_name is not None else stored_name
            directory.add(
                entity_id,
                stored_name=name,
                stored_type=dialog_type or stored_type,
                dialog_metadata=dialog_metadata,
            )
        names, normalized, fuzzy_names, fuzzy_normalized, entity_types, ineligible_ids, _ = directory.result()
        coverage = read_dialog_directory_coverage(self._conn)
        return names, normalized, fuzzy_names, fuzzy_normalized, entity_types, ineligible_ids, coverage

    @staticmethod
    def _local_username_match(
        row: tuple[object, ...],
        *,
        username: str,
        dialog_metadata: _DialogMetadata,
    ) -> MatchInfo | None:
        entity_id = _coerce_int(row[0], 0)
        dialog_name, dialog_type, eligible, dialog_username, identity_complete = dialog_metadata.get(
            entity_id, (None, None, True, None, None)
        )
        if entity_id == 0 or not eligible:
            return None
        return {
            "entity_id": entity_id,
            "display_name": str(dialog_name or row[1] or f"@{username}"),
            "score": 100,
            "username": dialog_username
            or (str(row[2]) if row[2] is not None and identity_complete is not True else username),
            "entity_type": dialog_type
            or (str(row[3]) if row[3] is not None and identity_complete is not True else None),
            "disambiguation_hint": None,
        }

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
        if isinstance(result, Candidates):
            apply_disambiguation_hint(
                result.matches,
                collision_query=query,
                collision_count=len(exact_names),
            )
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
        entity_rows = cast(
            list[tuple[object, ...]],
            self._conn.execute("SELECT id, name, username, type FROM entities ORDER BY id").fetchall(),
        )
        entity_cache = {_coerce_int(row[0], 0): row for row in entity_rows}
        rows = [
            (
                entity_id,
                name
                or (
                    entity_cache.get(entity_id, (None, None, None, None))[1] if identity_complete is not True else None
                ),
                canonical_username
                or (
                    entity_cache.get(entity_id, (None, None, None, None))[2] if identity_complete is not True else None
                ),
                entity_type
                or (
                    entity_cache.get(entity_id, (None, None, None, None))[3] if identity_complete is not True else None
                ),
            )
            for entity_id, (
                name,
                entity_type,
                eligible,
                canonical_username,
                identity_complete,
            ) in dialog_metadata.items()
            if eligible
            and (
                canonical_username
                or (entity_cache.get(entity_id, (None, None, None, None))[2] if identity_complete is not True else None)
            )
            is not None
            and str(
                canonical_username
                or (entity_cache.get(entity_id, (None, None, None, None))[2] if identity_complete is not True else None)
            ).casefold()
            == username.casefold()
        ]
        matches = [
            match
            for row in rows
            if (match := self._local_username_match(row, username=username, dialog_metadata=dialog_metadata))
            is not None
        ]
        matches.sort(key=lambda item: (item["display_name"].casefold(), item["entity_id"]))
        if len(matches) == 1:
            match = matches[0]
            return Resolved(entity_id=match["entity_id"], display_name=match["display_name"])
        if matches:
            return Candidates(query=query, matches=matches)
        return NotFound(query=query)

    async def _resolve_dialog_entity(self, dialog: str) -> int | None:
        """Resolve a dialog selector through the live Telegram entity lookup."""
        with _preserve_or_rpc_scope(
            TelegramRpcSource.DIALOG_RESOLUTION,
            acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
        ):
            try:
                entity = await self._client.get_entity(dialog)
                return int(cast(int, telethon_utils.get_peer_id(entity)))
            except ValueError, KeyError:
                return None
            except TelegramRpcThrottled:
                raise
            except RPCError, TimeoutError:
                raise
            except Exception:
                logger.exception("unexpected get_entity failure for %r", dialog)
                raise

    async def _resolve_dialog_username(
        self,
        query: str,
        username: str,
        *,
        ineligible_ids: set[int],
    ) -> Resolved | Candidates | NotFound:
        local = self._resolve_local_dialog_username(username, query)
        if not isinstance(local, NotFound):
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
    ) -> Resolved | Candidates | NotFound:
        """Resolve one normalized natural selector without silent precedence."""
        tme = _parse_tme_link(dialog)
        username = tme[0] if tme is not None else dialog[1:] if dialog.startswith("@") else None
        if username is not None:
            ineligible_ids = {
                entity_id
                for entity_id, (_name, _type, eligible, _username, _complete) in self._local_dialog_metadata().items()
                if not eligible
            }
            return await self._resolve_dialog_username(dialog, username, ineligible_ids=ineligible_ids)

        (
            local_names,
            local_normalized,
            fuzzy_names,
            fuzzy_normalized,
            entity_types,
            ineligible_ids,
            _coverage,
        ) = self._local_dialog_directory()
        if not latinize(dialog):
            return NotFound(query=dialog)
        exact_result = self._resolve_exact_natural_name(dialog, local_names, local_normalized, entity_types)
        if exact_result is not None:
            return exact_result
        local_result = _fuzzy_resolve(dialog, fuzzy_names, normalized_name_map=fuzzy_normalized)
        self._apply_dialog_candidate_types(local_result, entity_types)
        # A local ambiguity is already a fail-closed selector outcome. It must
        # not turn an MCP read into Telegram work merely to expand the set.
        if isinstance(local_result, Candidates) and len(local_result.matches) > 1:
            return local_result
        return local_result

    @staticmethod
    def _dialog_resolution_retryable_response(
        *, retry_after: int | None = None, transient: bool = True
    ) -> dict[str, object]:
        response: dict[str, object] = {
            "ok": False,
            "error": "dialog_resolution_retryable",
            "message": "The targeted Telegram peer lookup did not complete; no dialog was selected.",
            "retryable": transient,
            "required_action": "Retry the request; use an exact dialog id when already known.",
        }
        if retry_after is not None:
            response["retry_after"] = retry_after
        return response

    async def _resolve_dialog_id(  # noqa: PLR0911 - stable resolution response branches
        self,
        selector: DialogSelector,
    ) -> int | dict:
        """Resolve a validated selector to one id or a stable failure response."""
        if selector.exact_id is not None:
            return ResolvedDialogId(selector.exact_id, read_dialog_directory_coverage(self._conn))
        assert selector.query is not None
        directory_coverage = read_dialog_directory_coverage(self._conn)
        try:
            result = await self._resolve_dialog_name(selector.query)
        except TelegramRpcThrottled as exc:
            retry_after = exc.retry_after_seconds
            return self._dialog_resolution_retryable_response(retry_after=retry_after, transient=not exc.latched)
        except RPCError, TimeoutError:
            retry_after = None
            return self._dialog_resolution_retryable_response(retry_after=retry_after)
        if isinstance(result, Resolved):
            return ResolvedDialogId(result.entity_id, directory_coverage)
        if isinstance(result, Candidates):
            candidates = self._sorted_dialog_matches(result.matches)
            coverage = directory_coverage.to_wire()
            if len(candidates) == 1:
                return {
                    "ok": False,
                    "error": "dialog_not_found",
                    "message": f"Dialog {selector.label!r} was not found; one approximate match is available.",
                    "suggestion": candidates[0],
                    "directory_coverage": coverage,
                    "required_action": "Retry with the suggestion's exact dialog id, or refine the dialog name.",
                }
            return {
                "ok": False,
                "error": "ambiguous_dialog",
                "message": f"Dialog {selector.label!r} matched multiple dialogs.",
                "candidates": candidates,
                "directory_coverage": coverage,
                "required_action": "Retry with an exact dialog id from candidates.",
            }
        if directory_coverage.status in {"never", "in_progress"} or not directory_coverage.lookup_complete:
            return {
                "ok": False,
                "error": "dialog_directory_incomplete",
                "message": "No local match; coverage incomplete/unknown.",
                "directory_coverage": directory_coverage.to_wire(),
                "required_action": "Wait for the local dialog directory and identity lookup to complete, then retry.",
            }
        if directory_coverage.status == "stale" or not directory_coverage.lookup_fresh:
            return {
                "ok": False,
                "error": "stale_local_directory",
                "message": "No local match; the local dialog directory or identity lookup is stale.",
                "directory_coverage": directory_coverage.to_wire(),
                "required_action": "Wait for a fresh local directory receipt, then retry.",
            }
        return {
            "ok": False,
            "error": "dialog_not_found",
            "message": f"Dialog {selector.label!r} was not found.",
            "directory_coverage": directory_coverage.to_wire(),
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
        self._reproject_due_folder_memberships()
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
        self._reproject_due_folder_memberships()
        return {
            "ok": True,
            "data": {
                "folders": folder_summaries(self._conn),
                "folder_snapshot": folder_snapshot(
                    self._conn,
                    stale_after_seconds=self._policy.folder_snapshot_stale_after_seconds,
                ),
            },
        }

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

        resolved = await self._resolve_dialog_id(selector)
        if isinstance(resolved, dict):
            return resolved
        dialog_id = resolved

        rows = self._topic_rows(dialog_id)
        empty_reason = None
        if not rows or _topic_icons_need_refresh(rows):
            if self._topic_refresher is None:
                if not rows:
                    empty_reason = "topic_catalog_not_refreshed"
            else:
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
        with _preserve_or_rpc_scope(
            TelegramRpcSource.TOPIC_RESOLUTION,
            acquisition_kind=AcquisitionKind.TOPIC_LOOKUP,
        ):
            try:
                entity = await self._client.get_entity(dialog_id)
                refreshed = await self._topic_refresher.refresh(dialog_id, entity)
            except TelegramRpcThrottled as exc:
                logger.info(
                    "list_topics_refresh_deferred_flood_wait dialog_id=%d seconds=%s",
                    dialog_id,
                    exc.retry_after_seconds,
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
        """Return the daemon-owned account identity snapshot.

        Request: no parameters.
        Response data: {"id", "first_name", "last_name", "username"}.
        Errors: not_found.
        """
        if self.self_profile is None:
            return {"ok": False, "error": "not_found", "message": "account info unavailable"}
        return {"ok": True, "data": dict(self.self_profile)}

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
        if enable:
            if outcome.action in {
                "queue_full_history",
                "already_syncing",
                "preserved_explicit_enable",
            }:
                offer_durable_demand(self._require_demand_sink(), DemandKind.FULL_SYNC_PAGE)
            elif outcome.action == "request_delta_refresh":
                offer_durable_demand(self._require_demand_sink(), DemandKind.DELTA_GAP_FILL)
            offer_durable_demand(
                self._require_demand_sink(),
                DemandKind.BACKFILL_HYDRATION_BATCH,
                DemandKind.READ_RECEIPT_BATCH,
            )
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
            persisted_status = cast(str, row[0])
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
            persisted_status = None
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
        sync_read_model = build_sync_read_model(
            persisted_status=persisted_status,
            enrollment_enabled=enrollment_enabled,
            last_synced_at=last_synced_at,
            last_event_at=last_event_at,
            last_delta_checked_at=last_delta_checked_at,
            saved_message_count=message_count,
            total_messages=total_messages,
            now=int(time.time()),
        )

        data: dict = {
            "dialog_id": dialog_id,
            "delta_refresh_requested_at": delta_refresh_requested_at,
            "sync_progress": sync_progress,
            "sync_progress_message_id": sync_progress,
            "delete_detection": "reliable (channel)" if dialog_id < 0 else "best-effort weekly (DM)",
            "access_lost_at": access_lost_at,
            "access_last_revalidated_at": access_revalidation[0],
            "access_next_revalidate_at": access_revalidation[1],
            "enrollment_source": enrollment_source,
            **sync_read_model.to_wire(),
        }
        if sync_read_model.sync_status is SyncStatus.ACCESS_LOST and total_messages is None:
            data["archived_message_count"] = message_count
        return {"ok": True, "data": data}

    # ------------------------------------------------------------------
    # list_conversation_changes
    # ------------------------------------------------------------------

    async def _list_conversation_changes(self, req: dict[str, object]) -> dict:
        """Return one globally ordered, snapshot-bounded conversation-change page."""
        return query_conversation_changes(self._conn, req, self._conversation_changes_token_codec)

    # ------------------------------------------------------------------
    # get_entity_info
    # ------------------------------------------------------------------

    def _get_entity_info_service(self) -> DaemonEntityInfoService:
        if self._entity_info_service is None:
            self._entity_info_service = DaemonEntityInfoService(
                EntityInfoDeps(
                    conn=self._conn,
                    client=cast(DaemonClientLike, self._client),
                    dm_peer_ids=self._dm_peer_ids,
                    self_id=self.self_id,
                    self_profile=self.self_profile,
                    get_peer_id=telethon_utils.get_peer_id,
                    rid=_rid,
                    logger=cast(logging.Logger, logger),
                    now_provider=lambda: time.time(),
                    detail_ttl_seconds=self._policy.entity_detail_ttl_seconds,
                    slow_stage_seconds=self._policy.slow_request_seconds,
                    get_common_chats_request=GetCommonChatsRequest,
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
                    get_dialog_placement=lambda entity_id: dialog_placement(self._conn, entity_id),
                    refresh_limits=self._policy.entity_profile,
                    enable_full_user_pair=self._policy.full_user_pair_enabled,
                    full_user_auth_scope=lambda: self._auth_scope,
                    profile_observer=self._profile_observer,
                )
            )
            if self._demand_sink is not None:
                self._entity_info_service.bind_demand_sink(self._demand_sink)
        return self._entity_info_service

    async def _get_entity_info(self, req: dict[str, object]) -> dict:
        """Type-tagged entity inspector covering 5 Telegram entity kinds."""
        self._reproject_due_folder_memberships()
        result = await self._get_entity_info_service().get_entity_info(req)
        if result.get("ok"):
            data = cast(dict[str, object], result.get("data", {}))
            entity_id = data.get("id")
            if isinstance(entity_id, int):
                data["dialog_placement"] = dialog_placement(self._conn, entity_id)
        return result

    async def shutdown(self) -> None:
        """Stop profile refresh tasks before the daemon closes SQLite."""
        if self._entity_info_service is not None:
            await self._entity_info_service.shutdown()

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
        """Write one bounded ``mcp.call`` observation to ``runtime_observations``.

        Retention runs on startup and every 128 MCP observations.
        """
        event, error = _normalize_telemetry_event(req)
        if error is not None:
            return error
        assert event is not None
        outcome = _normalize_telemetry_outcome(event)
        if isinstance(outcome, dict):
            return outcome
        event["outcome"], event["error_code"] = outcome
        try:
            _write_telemetry(self._conn, self._policy, event)
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
