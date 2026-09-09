"""Sync daemon — long-running process that exclusively owns the TelegramClient.

Started via ``mcp-telegram sync``. Connects to Telegram, ensures sync.db schema,
bootstraps DM dialogs, then runs FullSyncWorker in a tight batch loop with
periodic heartbeat logging and clean SIGTERM handling.

Architecture:
- sync-daemon is the sole owner of TelegramClient — connects once, holds it.
- MCP server runs separately with disable_telegram_session() active and reads
  sync.db via open_sync_db_reader(); it never calls client.connect().
- SIGTERM triggers shutdown_event (set by daemon_shutdown), which checkpoints
  WAL before the daemon disconnects.

Event handlers:
- EventHandlerManager is registered BEFORE Telegram connect() so Telethon
  catch_up=True replays missed updates into live handlers, not an empty handler
  set.  It also remains registered BEFORE FullSyncWorker starts so no real-time
  events are missed during initial bulk fetch.  INSERT OR REPLACE handles any
  overlap between real-time and bulk paths idempotently.
- synced_dialogs set is refreshed every heartbeat so newly enrolled dialogs
  are picked up within one interval without re-registering handlers.
- Weekly gap scan detects tombstoned DM messages that MTProto delete events
  cannot report.

Delta catch-up:
- connect() called with catch_up=True — Telethon replays missed updates via PTS
  on reconnect after handlers are already registered.
- reconnect_catch_up_loop polls public connection state and invokes public
  catch_up() once per observed disconnected→connected transition.
- DeltaSyncWorker.run_delta_catch_up() fills forward gaps for all 'synced'
  dialogs before bootstrap_dms() enrolls new ones.

Daemon API:
- DaemonAPIServer runs on a Unix socket alongside the sync loop, serving
  list_messages / search_messages / list_dialogs requests from MCP server.
- FTS backfill runs once at startup for messages without FTS index entries.
- Socket file cleaned up on shutdown (and stale file removed on startup).
"""

import asyncio
import logging
import math
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextvars import Context
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Protocol, cast

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetPeerDialogsRequest  # type: ignore[import-untyped]
from telethon.tl.functions.users import GetFullUserRequest  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    InputDialogPeer,
    TypeInputDialogPeer,
    TypeInputPeer,
    TypeInputUser,
)

from . import daemon_shutdown
from .activity_cold_backfill import ColdBackfillPacing, run_cold_backfill_loop
from .activity_contracts import InputPeerResolver
from .activity_hot_sweep import run_hot_sweep_loop
from .activity_peer_resolve import resolve_input_peer
from .activity_substrate import ActivityClient
from .activity_sync import run_activity_sync_loop
from .config import McpTelegramConfig, SchedulingConfig, load_config, resolve_scheduling_config
from .daemon_api import DaemonApiPolicy, DaemonAPIServer, DaemonClientLike, DaemonHealthStatus
from .delta_sync import (
    AccessProbePolicy,
    DeltaCatchUpPolicy,
    DeltaSyncWorker,
    _DeltaSyncClient,
    run_access_probe_loop,
    run_delta_catch_up_loop,
)
from .demand_composition import (
    DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS,
    DemandCompositionClient,
    DemandCompositionDependencies,
    TelegramDemandShadow,
    build_durable_adapter_map,
)
from .demand_shadow_wiring import DemandCycleRunner, offer_durable_demand, run_legacy_demand_cycle
from .dialog_sync import (
    DialogReconciliationWorker,
    DialogsBootstrapWorker,
    _read_last_full_reconciliation_at,
)
from .entity_profile.refresh import RefreshLimits
from .event_handlers import EventHandlerManager
from .fact_hydration import MessageFactHydrationWorker
from .feedback_db import SQLiteFeedbackStore, ensure_feedback_schema
from .feedback_service import FeedbackApplicationService
from .flood import (
    FloodWaitKillSwitchPolicy,
    TelegramRpcThrottled,
    _raise_if_latched,
    configure_flood_wait_kill_switch,
    flood_wait_kill_switch_status,
    maybe_log_flood_wait_rollup,
    sleep_through_flood,
)
from .folders.refresh import FolderRefresher
from .folders.sqlite_repository import SQLiteFolderSnapshotRepository
from .folders.telegram_adapter import FolderClient, TelethonTelegramFolderGateway
from .folders.worker import FolderProjectionWorker
from .fts import backfill_fts_index
from .hydration_queue import HydrationPriority
from .media_hydration import MediaFactHydrationHandler
from .message_fact_refresh import (
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    MessageFactRefreshResult,
    refresh_message_facts_once,
)
from .messages.sqlite_hydration_jobs import reconcile_fact_hydration_jobs_for_dialog
from .own_only import OwnOnlyContext, ensure_own_only_schema
from .reactions.refresh import ReactionFreshener
from .reactions.sqlite_repository import SQLiteReactionSnapshotRepository
from .reactions.telegram_adapter import TelethonTelegramReactionGateway
from .read_state import apply_read_cursor, apply_reconciled_unread_count
from .reconnect import run_reconnect_catch_up_loop
from .rpc_admission_observations import RpcAdmissionObservationAggregator
from .runtime_observations import RuntimeObservationSink, prune_runtime_observations, record_runtime_observation
from .scheduled_messages import ScheduledMessageReconciler, ScheduledReconciliationPolicy
from .state import StatePaths, ensure_private_state_dir
from .sync_db import (
    _open_sync_db,
    ensure_sync_schema,
    load_account_cooldown_until_utc,
    load_self_profile_last_success_at,
    migrate_legacy_databases,
    open_sync_db_reader,
    save_account_cooldown_until_utc,
    save_self_profile_last_success_at,
)
from .sync_worker import FullSyncWorker
from .telegram import create_client
from .telegram_demand import AcquisitionKind, DemandStatus, acquisition_context, demand_context
from .telegram_read_receipts import TelethonTelegramReadReceiptGateway
from .telegram_rpc import TelegramRpcCooldownPersistence
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    AdmissionObserver,
    RpcAdmissionClosedError,
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    rpc_scope,
)
from .topics.refresh import TopicRefresher
from .topics.sqlite_repository import SQLiteTopicSnapshotRepository
from .topics.telegram_adapter import TelethonTelegramTopicGateway, TopicClient
from .transcription_hydration import TranscriptionHydrationHandler

logger = logging.getLogger(__name__)

_OWN_ONLY_ADMISSION_MAX_WAIT_SECONDS = 30.0


def _operator_summary_row_factory(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> dict[str, object]:
    description = cursor.description or ()
    return {str(column[0]): row[index] for index, column in enumerate(description)}


def read_operator_summary_snapshot(
    db_path: Path, since_ms: int
) -> tuple[
    list[dict[str, object]],
    dict[str, object] | None,
    list[dict[str, object]],
    dict[str, object],
]:
    """Read the durable rows needed by the standalone operator summary."""
    conn = open_sync_db_reader(db_path)
    conn.row_factory = _operator_summary_row_factory
    try:
        observations = cast(
            list[dict[str, object]],
            conn.execute(
                "SELECT * FROM runtime_observations WHERE observed_at_ms>=? ORDER BY observed_at_ms,id",
                (since_ms,),
            ).fetchall(),
        )
        history_row = cast(
            dict[str, object] | None,
            conn.execute(
                "SELECT value FROM daemon_state WHERE key='runtime_observations_history_started_at_ms'"
            ).fetchone(),
        )
        dialog_rows = cast(
            list[dict[str, object]],
            conn.execute("SELECT status,COUNT(*) count FROM synced_dialogs GROUP BY status").fetchall(),
        )
        marker_rows = cast(
            list[dict[str, object]],
            conn.execute(
                "SELECT key,value FROM daemon_state "
                "WHERE key IN ("
                "'runtime_observations_last_cap_truncation_ms',"
                "'runtime_observations_last_loss_ms',"
                "'runtime_observations_last_queue_full_drops',"
                "'runtime_observations_last_writer_failures'"
                ")"
            ).fetchall(),
        )
    finally:
        conn.close()
    persisted_markers = {str(row["key"]): row["value"] for row in marker_rows}
    coverage_markers: dict[str, object] = {}
    cap_ms = persisted_markers.get("runtime_observations_last_cap_truncation_ms")
    if cap_ms is not None:
        coverage_markers["last_cap_truncation_ms"] = cap_ms
    loss_ms = persisted_markers.get("runtime_observations_last_loss_ms")
    if loss_ms is not None and int(str(loss_ms)) >= since_ms:
        coverage_markers["loss_observed"] = True
        coverage_markers["telemetry_gap_ms"] = loss_ms
        queue_full_drops = persisted_markers.get("runtime_observations_last_queue_full_drops")
        writer_failures = persisted_markers.get("runtime_observations_last_writer_failures")
        if queue_full_drops is not None:
            coverage_markers["queue_full_drops"] = int(str(queue_full_drops))
        if writer_failures is not None:
            coverage_markers["writer_failures"] = int(str(writer_failures))
    return observations, history_row, dialog_rows, coverage_markers


class _DaemonClient(Protocol):
    def add_event_handler(self, _callback: object, _event: object) -> None: ...

    def remove_event_handler(self, _callback: object) -> None: ...

    def is_connected(self) -> bool: ...

    async def catch_up(self) -> None: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def close_rpc_scheduler(self) -> None: ...

    def set_rpc_admission_observer(self, observer: AdmissionObserver | None) -> None: ...

    async def get_me(self) -> object: ...

    async def get_input_entity(self, _dialog_id: int) -> object: ...

    async def get_entity(self, _dialog_id: int) -> object: ...

    async def get_messages(self, *_args: object, **_kwargs: object) -> object: ...

    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class _ReadPositionDialogLike(Protocol):
    peer: object
    read_inbox_max_id: int | None
    read_outbox_max_id: int | None
    unread_count: int | None


class _ReadPositionsResultLike(Protocol):
    dialogs: Sequence[_ReadPositionDialogLike]


class _MessagesTotalLike(Protocol):
    total: int | None


@dataclass(frozen=True, slots=True)
class _StoredReadPositionState:
    inbox_max: int | None
    unread_observed_at: int | None


class _MeLike(Protocol):
    id: int


@dataclass(frozen=True, slots=True)
class _RpcSchedulerFailureStatus:
    """Health status exposed after the account RPC arbiter fails closed."""

    reason: str
    open: bool = True

    def detail(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class DaemonHistoryPacing:
    backfill_skip_s: float = 1.0


@dataclass(frozen=True, slots=True)
class DaemonPacing:
    history: DaemonHistoryPacing = DaemonHistoryPacing()


_PACING = DaemonPacing()


HEARTBEAT_INTERVAL_S: float = 60.0
GAP_SCAN_INTERVAL_S: float = 7 * 24 * 3600.0
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE

_BACKFILL_TOTAL_MESSAGES_SKIP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RPCError,
    sqlite3.DatabaseError,
    Exception,
)

_SELECT_NULL_TOTAL_SQL = (
    "SELECT sd.dialog_id FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.total_messages IS NULL AND sd.status != 'not_synced'"
)

_UPDATE_TOTAL_SQL = "UPDATE synced_dialogs SET total_messages = ? WHERE dialog_id = ?"

_SELECT_READ_POSITION_WORK_SQL = (
    # NULL cursors require bootstrap. Non-NULL inbox cursors are reconciled
    # when the local mirror still has an incoming unread candidate or Telegram's
    # last exact unread count remains positive. This bounds recovery after a
    # missed realtime read update without polling every enrolled dialog.
    "SELECT sd.dialog_id FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "LEFT JOIN dialogs d ON d.dialog_id = sd.dialog_id "
    "WHERE sd.status = 'synced' AND ("
    "sd.read_inbox_max_id IS NULL OR sd.read_outbox_max_id IS NULL "
    "OR COALESCE(d.unread_count, 0) > 0 "
    "OR EXISTS (SELECT 1 FROM messages m "
    "WHERE m.dialog_id = sd.dialog_id AND m.is_deleted = 0 AND m.is_service = 0 "
    "AND m.out = 0 AND m.message_id > COALESCE(sd.read_inbox_max_id, -1))"
    ")"
)


@dataclass(slots=True)
class _SyncLoopState:
    sync_start: float
    last_heartbeat: float
    last_gap_scan: float
    was_idle: bool = False


@dataclass(slots=True)
class _SyncMainContext:
    db_path: Path
    conn: sqlite3.Connection
    feedback_conn: sqlite3.Connection
    shutdown_event: asyncio.Event
    client: _DaemonClient
    reaction_freshener: ReactionFreshener
    reaction_freshness_ttl_seconds: int
    message_fact_refresh_policy: MessageFactRefreshPolicy
    api_server: DaemonAPIServer
    topic_refresher: TopicRefresher
    folder_projection_worker: FolderProjectionWorker
    fact_hydration_worker: MessageFactHydrationWorker
    socket_path: Path
    self_profile_cadence: SQLiteSelfProfileCadence
    rpc_observation_sink: RuntimeObservationSink | None = None
    rpc_admission_observer: RpcAdmissionObservationAggregator | None = None
    demand_shadow: TelegramDemandShadow | None = None
    demand_runtime: _DemandRuntime | None = None
    unix_server: asyncio.AbstractServer | None = None
    handler_manager: EventHandlerManager | None = None
    own_only_context: OwnOnlyContext | None = None
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    background_tasks: set[asyncio.Task[object]] = field(default_factory=set)
    flood_wait_kill_switch_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class _DemandRuntime:
    """Objects shared by PR1 shadow adapters and legacy launchers."""

    shadow: TelegramDemandShadow
    scheduled_reconciler: ScheduledMessageReconciler
    dialog_reconciliation_worker: DialogReconciliationWorker
    message_fact_refresh_deps: MessageFactRefreshDeps
    read_receipt_batch: Callable[[], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class SQLiteSelfProfileCadence:
    """Persist self-profile cadence through the daemon-owned sync database."""

    conn: sqlite3.Connection
    interval_seconds: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.interval_seconds) or self.interval_seconds <= 0:
            raise ValueError("self-profile interval_seconds must be finite and positive")

    def status(self, now: float) -> DemandStatus:
        """Return the persisted release boundary without changing cadence state."""
        del now
        last_success_at = load_self_profile_last_success_at(self.conn)
        release_at = 0.0 if last_success_at is None else last_success_at + self.interval_seconds
        return DemandStatus(release_at=release_at)

    def mark_refreshed(self, completed_at: float) -> None:
        """Commit a successful refresh timestamp atomically."""
        save_self_profile_last_success_at(self.conn, completed_at)


@dataclass(frozen=True, slots=True)
class _BackfillTotalDialogResult:
    filled: int
    pause_after: bool
    stop: bool = False


async def _backfill_total_messages(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> int:
    """One-time sweep to populate total_messages for dialogs with NULL."""
    rows = cast(list[tuple[int]], conn.execute(_SELECT_NULL_TOTAL_SQL).fetchall())
    if not rows:
        logger.info("backfill_total_messages — no NULL rows, skipping")
        return 0

    filled = 0
    for (dialog_id,) in rows:
        if shutdown_event.is_set():
            break
        result = await _backfill_total_message_dialog(client, conn, shutdown_event, dialog_id)
        filled += result.filled
        if result.stop:
            break
        if result.pause_after and not await _sleep_between_backfill_total_dialogs(shutdown_event):
            break

    logger.info("backfill_total_messages filled=%d/%d", filled, len(rows))
    return filled


async def _backfill_total_message_dialog(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    dialog_id: int,
) -> _BackfillTotalDialogResult:
    """Fetch and persist one total_messages value, or handle a single skip/flood."""
    try:
        result = cast(_MessagesTotalLike, await client.get_messages(entity=dialog_id, limit=1))
        total = result.total
        if total is not None:
            with conn:
                conn.execute(
                    _UPDATE_TOTAL_SQL + " AND EXISTS (SELECT 1 FROM full_history_enrollment fhe "
                    "WHERE fhe.dialog_id = synced_dialogs.dialog_id AND fhe.enabled = 1)",
                    (total, dialog_id),
                )
            return _BackfillTotalDialogResult(filled=1, pause_after=True)
        return _BackfillTotalDialogResult(filled=0, pause_after=True)
    except TelegramRpcThrottled as exc:
        logger.warning("backfill_total flood_wait dialog_id=%d seconds=%s", dialog_id, exc.retry_after_seconds)
        if exc.retry_after_seconds is None:
            raise
        if await sleep_through_flood(shutdown_event, exc.retry_after_seconds):
            return _BackfillTotalDialogResult(filled=0, pause_after=False, stop=True)
        return _BackfillTotalDialogResult(filled=0, pause_after=False)
    except _BACKFILL_TOTAL_MESSAGES_SKIP_EXCEPTIONS as exc:
        logger.debug("backfill_total skip dialog_id=%d error=%s", dialog_id, exc)
        return _BackfillTotalDialogResult(filled=0, pause_after=True)


async def _sleep_between_backfill_total_dialogs(shutdown_event: asyncio.Event) -> bool:
    """Pause between backfill_total dialogs; return False when shutdown fires."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=_PACING.history.backfill_skip_s)
        return False
    except TimeoutError:
        return True


async def _initialize_read_positions(  # noqa: PLR0913
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    max_dialogs: int | None = None,
    failure_cooldown_seconds: float | None = None,
    batch_size: int | None = None,
    batch_pause_seconds: float | None = None,
    success_recheck_seconds: float | None = None,
) -> int:
    """One bounded sweep to bootstrap or reconcile read state.

    Phase 39.3-02 R4: the same GetPeerDialogsRequest sweep that already
    populates ``read_inbox_max_id`` also populates ``read_outbox_max_id``
    from the same ``Dialog`` object — same endpoint, batched at
    ``ceil(N / 15)`` calls (Telethon's batch limit). No additional API
    endpoints introduced.

    D-03 LOCKED NULL preservation: if Telethon returns None for either
    cursor on a Dialog, ``apply_read_cursor`` is NOT called for that
    side. The DB cursor stays NULL so Plan 03's header renders
    ``[unknown (sync pending)]`` rather than lying with ``[all read]``.
    NEVER convert None → 0; NEVER call apply_read_cursor with 0 as a
    stand-in. This consistency rule applies symmetrically to inbox AND
    outbox. It tightens Phase 38's inbox-side behaviour (which used
    ``or 0``) — documented behavioural change.

    Batch size and inter-batch pacing are supplied by the hierarchical
    SchedulingConfig. The caller may also bound selected rows so a recurring
    pass cannot issue an unbounded number of Telegram actions.

    All writes use monotonic UPDATE — ``MAX(COALESCE(existing, 0), incoming)``
    via the shared primitive — so a live MessageRead / outbox-read event
    that arrives during the bootstrap window cannot be overwritten by a
    stale bootstrap reply (designed race safety, not accidental).
    """
    effective_batch_size, effective_batch_pause_seconds, effective_success_recheck_seconds = _read_position_pacing(
        batch_size, batch_pause_seconds, success_recheck_seconds
    )
    if effective_batch_size < 1:
        raise ValueError("batch_size must be positive")
    if effective_batch_pause_seconds <= 0:
        raise ValueError("batch_pause_seconds must be positive")

    now = int(time.time())
    rows = _select_read_position_work_rows(conn, max_dialogs, now=now)
    if not rows:
        logger.debug("initialize_read_positions — no due rows, skipping")
        return 0

    dialog_ids = [dialog_id for (dialog_id,) in rows]
    filled = 0

    for i in range(0, len(dialog_ids), effective_batch_size):
        if shutdown_event.is_set():
            break
        batch_ids = dialog_ids[i : i + effective_batch_size]
        batch_filled, stop = await _reconcile_read_position_batch(
            client,
            conn,
            shutdown_event,
            batch_ids,
            failure_cooldown_seconds,
            success_recheck_seconds=effective_success_recheck_seconds,
        )
        filled += batch_filled
        if stop:
            return filled

        if not await _sleep_read_pos_batch(shutdown_event, effective_batch_pause_seconds):
            break

    logger.info("initialize_read_positions filled=%d/%d", filled, len(dialog_ids))
    return filled


def _read_position_pacing(
    batch_size: int | None,
    batch_pause_seconds: float | None,
    success_recheck_seconds: float | None,
) -> tuple[int, float, float]:
    defaults = SchedulingConfig()
    return (
        defaults.read_position_reconciliation_batch_size if batch_size is None else batch_size,
        defaults.read_position_reconciliation_batch_pause_seconds
        if batch_pause_seconds is None
        else batch_pause_seconds,
        defaults.read_position_reconciliation_seconds if success_recheck_seconds is None else success_recheck_seconds,
    )


async def _reconcile_read_position_batch(  # noqa: PLR0913
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    batch_ids: list[int],
    failure_cooldown_seconds: float | None,
    *,
    success_recheck_seconds: float,
) -> tuple[int, bool]:
    retry_ids: set[int] = set()
    try:
        input_peers, unresolved_ids = await _build_read_position_input_peers(client, batch_ids)
        retry_ids.update(unresolved_ids)
        if input_peers:
            request_started_at = int(time.time())
            result = cast(_ReadPositionsResultLike, await client(GetPeerDialogsRequest(peers=input_peers)))
            returned_ids: set[int] = set()
            success_due_at = int(time.time()) + max(1, math.ceil(success_recheck_seconds))
            retry_at = _read_position_retry_at(int(time.time()), failure_cooldown_seconds)
            filled = _apply_read_positions_from_dialogs(
                conn,
                result,
                retry_at=retry_at,
                returned_ids=returned_ids,
                failed_ids=retry_ids,
                request_started_at=request_started_at,
                success_due_at=success_due_at,
            )
            retry_ids.update(set(batch_ids) - returned_ids)
        else:
            filled = 0
    except TelegramRpcThrottled as exc:
        if exc.retry_after_seconds is None:
            raise
        logger.warning("read_pos_bootstrap flood_wait seconds=%s", exc.retry_after_seconds)
        retry_ids.update(batch_ids)
        _mark_read_position_retry(
            conn,
            retry_ids,
            _read_position_retry_at(int(time.time()), failure_cooldown_seconds),
        )
        conn.commit()
        if exc.retry_after_seconds is not None:
            await sleep_through_flood(shutdown_event, exc.retry_after_seconds)
        return 0, True
    except (RPCError, sqlite3.DatabaseError) as exc:
        logger.debug("read_pos_bootstrap batch_failed error=%s", exc)
        retry_ids.update(batch_ids)
        filled = 0
    _mark_read_position_retry(
        conn,
        retry_ids,
        _read_position_retry_at(int(time.time()), failure_cooldown_seconds),
    )
    conn.commit()
    return filled, False


def _select_read_position_work_rows(
    conn: sqlite3.Connection,
    max_dialogs: int | None,
    *,
    now: int,
) -> list[tuple[int]]:
    """Select the durable NULL-cursor queue, optionally bounded for a pass."""
    if max_dialogs is None:
        return cast(
            list[tuple[int]],
            conn.execute(
                f"{_SELECT_READ_POSITION_WORK_SQL} "
                "AND (sd.read_position_next_attempt_at IS NULL OR sd.read_position_next_attempt_at <= ?) "
                "ORDER BY COALESCE(sd.read_position_next_attempt_at, 0), "
                "COALESCE(sd.read_position_attempt_count, 0), sd.dialog_id",
                (now,),
            ).fetchall(),
        )
    if max_dialogs < 0:
        raise ValueError("max_dialogs must be non-negative")
    return cast(
        list[tuple[int]],
        conn.execute(
            f"{_SELECT_READ_POSITION_WORK_SQL} "
            "AND (sd.read_position_next_attempt_at IS NULL OR sd.read_position_next_attempt_at <= ?) "
            "ORDER BY COALESCE(sd.read_position_next_attempt_at, 0), "
            "COALESCE(sd.read_position_attempt_count, 0), sd.dialog_id LIMIT ?",
            (now, max_dialogs),
        ).fetchall(),
    )


def _mark_read_position_retry(conn: sqlite3.Connection, dialog_ids: list[int] | set[int], retry_at: int | None) -> None:
    if retry_at is None or not dialog_ids:
        return
    placeholders = ", ".join("?" for _ in dialog_ids)
    conn.execute(
        "UPDATE synced_dialogs "
        "SET read_position_next_attempt_at = ?, "
        "read_position_attempt_count = COALESCE(read_position_attempt_count, 0) + 1 "
        f"WHERE status = 'synced' AND dialog_id IN ({placeholders})",
        (retry_at, *sorted(dialog_ids)),
    )


def _read_position_retry_at(now: int, cooldown_seconds: float | None) -> int | None:
    return None if cooldown_seconds is None else now + max(1, math.ceil(cooldown_seconds))


async def _run_read_position_reconciliation_loop(  # noqa: PLR0913 - daemon composition keeps policy explicit
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float,
    max_dialogs_per_pass: int,
    failure_cooldown_seconds: float | None = None,
    batch_size: int | None = None,
    batch_pause_seconds: float | None = None,
    demand_cycle_runner: DemandCycleRunner | None = None,
    run_batch: Callable[[], Awaitable[object]] | None = None,
) -> None:
    """Repeatedly reconcile durable read-position work after startup.

    The first pass is immediate. Each subsequent pass waits for the configured
    interval, and all passes execute in this single daemon-owned task, so no
    overlapping Telegram sweeps can occur. SQLite due-times provide fairness
    for bootstrap, stale-unread recovery, retries, and late enrollment.
    """
    while not shutdown_event.is_set():

        async def reconcile() -> object:
            if run_batch is None:
                return await _initialize_read_positions(
                    client,
                    conn,
                    shutdown_event,
                    max_dialogs=max_dialogs_per_pass,
                    failure_cooldown_seconds=failure_cooldown_seconds,
                    batch_size=batch_size,
                    batch_pause_seconds=batch_pause_seconds,
                    success_recheck_seconds=interval_seconds,
                )
            return await run_batch()

        if demand_cycle_runner is None:
            await run_legacy_demand_cycle(None, DemandKind.READ_RECEIPT_BATCH, reconcile)
        else:
            await demand_cycle_runner(DemandKind.READ_RECEIPT_BATCH, reconcile)
        if shutdown_event.is_set():
            break
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _build_read_position_input_peers(
    client: _DaemonClient, batch_ids: list[int]
) -> tuple[list[TypeInputDialogPeer], list[int]]:
    input_peers: list[TypeInputDialogPeer] = []
    unresolved_ids: list[int] = []
    for dialog_id in batch_ids:
        try:
            peer = await client.get_input_entity(dialog_id)
            if peer is None:
                unresolved_ids.append(dialog_id)
                continue
            input_peer = cast(TypeInputPeer, peer)
            input_peers.append(InputDialogPeer(peer=input_peer))
        except TelegramRpcThrottled:
            raise
        except (RPCError, TypeError, ValueError) as exc:
            logger.debug("read_pos_bootstrap skip dialog_id=%d error=%s", dialog_id, exc)
            unresolved_ids.append(dialog_id)
    return input_peers, unresolved_ids


def _apply_read_positions_from_dialogs(  # noqa: PLR0913
    conn: sqlite3.Connection,
    result: _ReadPositionsResultLike,
    *,
    retry_at: int | None = None,
    returned_ids: set[int] | None = None,
    failed_ids: set[int] | None = None,
    request_started_at: int | None = None,
    success_due_at: int | None = None,
) -> int:
    """Apply read cursors from a GetPeerDialogsRequest result."""
    filled = 0
    with conn:
        for dialog in result.dialogs:
            if _apply_read_position_dialog(
                conn,
                dialog,
                retry_at,
                returned_ids,
                failed_ids,
                request_started_at=request_started_at,
                success_due_at=success_due_at,
            ):
                filled += 1
    return filled


def _record_read_reconciliation_event(
    conn: sqlite3.Connection,
    dialog_id: int,
    state: _StoredReadPositionState,
    *,
    wrote_any: bool,
) -> None:
    after_row = cast(
        tuple[int | None, int | None, int | None] | None,
        conn.execute(
            "SELECT sd.read_inbox_max_id, sd.read_outbox_max_id, d.unread_count "
            "FROM synced_dialogs sd LEFT JOIN dialogs d ON d.dialog_id=sd.dialog_id WHERE sd.dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    try:
        record_runtime_observation(
            conn,
            kind="sync.read_reconciliation",
            dialog_id=dialog_id,
            outcome="applied" if wrote_any else "unchanged",
            payload={
                "cursor_before": state.inbox_max,
                "cursor_after": after_row[0] if after_row else None,
                "outbox_after": after_row[1] if after_row else None,
                "unread_after": after_row[2] if after_row else None,
            },
        )
    except Exception:
        logger.exception("runtime_event_record_failed kind=sync.read_reconciliation dialog_id=%d", dialog_id)


def _apply_read_position_dialog(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog: _ReadPositionDialogLike,
    retry_at: int | None,
    returned_ids: set[int] | None,
    failed_ids: set[int] | None,
    *,
    request_started_at: int | None = None,
    success_due_at: int | None = None,
) -> bool:
    chat_id = int(cast(int, telethon_utils.get_peer_id(dialog.peer)))
    # D-03 LOCKED: None -> skip (preserve NULL). NEVER fold None -> 0; that
    # would lie with [all read] during the bootstrap window. 0 is a valid
    # distinct value (peer/me has read nothing) and is written as-is.
    inbox_max = cast(int | None, getattr(dialog, "read_inbox_max_id", None))
    outbox_max = cast(int | None, getattr(dialog, "read_outbox_max_id", None))
    _add_returned_read_position_id(returned_ids, chat_id)
    state = _stored_reconcilable_read_position(conn, chat_id)
    if state is None:
        return False
    wrote_any = False
    if inbox_max is not None and apply_read_cursor(conn, chat_id, "inbox", inbox_max) > 0:
        wrote_any = True
    if outbox_max is not None and apply_read_cursor(conn, chat_id, "outbox", outbox_max) > 0:
        wrote_any = True
    _apply_reconciled_unread_count(
        conn,
        chat_id,
        dialog=dialog,
        state=state,
        inbox_max=inbox_max,
        request_started_at=request_started_at,
    )
    _schedule_read_position_result(
        conn,
        chat_id,
        inbox_max=inbox_max,
        outbox_max=outbox_max,
        retry_at=retry_at,
        success_due_at=success_due_at,
        failed_ids=failed_ids,
    )
    _record_read_reconciliation_event(conn, chat_id, state, wrote_any=wrote_any)
    return wrote_any


def _stored_reconcilable_read_position(conn: sqlite3.Connection, dialog_id: int) -> _StoredReadPositionState | None:
    row = cast(
        tuple[str, int | None, int | None] | None,
        conn.execute(
            "SELECT sd.status, sd.read_inbox_max_id, d.unread_count_observed_at "
            "FROM synced_dialogs sd "
            "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
            "LEFT JOIN dialogs d ON d.dialog_id = sd.dialog_id "
            "WHERE sd.dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    if row is None or row[0] != "synced":
        return None
    return _StoredReadPositionState(inbox_max=row[1], unread_observed_at=row[2])


def _apply_reconciled_unread_count(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    dialog: _ReadPositionDialogLike,
    state: _StoredReadPositionState,
    inbox_max: int | None,
    request_started_at: int | None,
) -> None:
    unread_count = getattr(dialog, "unread_count", None)
    if request_started_at is None or inbox_max is None:
        return
    if not isinstance(unread_count, int) or isinstance(unread_count, bool) or unread_count < 0:
        return
    if state.inbox_max is not None and inbox_max < state.inbox_max:
        return
    if state.unread_observed_at is not None and state.unread_observed_at >= request_started_at:
        return
    apply_reconciled_unread_count(
        conn,
        dialog_id,
        unread_count=unread_count,
        request_started_at=request_started_at,
    )


def _schedule_read_position_result(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    inbox_max: int | None,
    outbox_max: int | None,
    retry_at: int | None,
    success_due_at: int | None,
    failed_ids: set[int] | None,
) -> None:
    if inbox_max is None or outbox_max is None:
        if retry_at is not None and failed_ids is not None:
            failed_ids.add(dialog_id)
        return
    due_at = success_due_at if success_due_at is not None else None
    if success_due_at is None and retry_at is None:
        return
    conn.execute(
        "UPDATE synced_dialogs SET read_position_next_attempt_at = ?, "
        "read_position_attempt_count = 0 WHERE dialog_id = ?",
        (due_at, dialog_id),
    )


def _add_returned_read_position_id(returned_ids: set[int] | None, chat_id: int) -> None:
    if returned_ids is not None:
        returned_ids.add(chat_id)


async def _sleep_read_pos_batch(shutdown_event: asyncio.Event, pause_seconds: float | None = None) -> bool:
    # Inter-batch pause: SIGTERM-responsive
    effective_pause_seconds = (
        SchedulingConfig().read_position_reconciliation_batch_pause_seconds if pause_seconds is None else pause_seconds
    )
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=effective_pause_seconds)
        return False
    except TimeoutError:
        return True


# ---------------------------------------------------------------------------
# Heartbeat — standalone for testability (no nonlocal / closure)
# ---------------------------------------------------------------------------


def _fetch_heartbeat_stats(conn: sqlite3.Connection) -> dict[str, int]:
    stats_rows = cast(
        list[tuple[str, int]],
        conn.execute("SELECT status, COUNT(*) FROM synced_dialogs GROUP BY status").fetchall(),
    )
    return dict(stats_rows)


def _format_heartbeat_eta(sync_start: float, synced: int, total: int, now_mono: float) -> str:
    if synced <= 0 or synced >= total:
        return " eta=done" if synced >= total else ""

    remaining = total - synced
    elapsed = now_mono - sync_start
    secs_per_dialog = elapsed / synced
    eta_secs = int(remaining * secs_per_dialog)
    if eta_secs >= SECONDS_PER_HOUR:
        return f" eta={eta_secs // SECONDS_PER_HOUR}h{(eta_secs % SECONDS_PER_HOUR) // SECONDS_PER_MINUTE}m"
    if eta_secs >= SECONDS_PER_MINUTE:
        return f" eta={eta_secs // SECONDS_PER_MINUTE}m{eta_secs % SECONDS_PER_MINUTE}s"
    return f" eta={eta_secs}s"


def _log_heartbeat(
    conn: sqlite3.Connection,
    client: _DaemonClient,
    sync_start: float,
) -> None:
    """Log heartbeat with sync stats and ETA from sync.db."""
    try:
        stats = _fetch_heartbeat_stats(conn)
    except sqlite3.DatabaseError:
        logger.warning("heartbeat_stats_failed", exc_info=True)
        stats = {}
    synced = int(stats.get("synced", 0) or 0)
    syncing = int(stats.get("syncing", 0) or 0)
    total = synced + syncing + int(stats.get("not_synced", 0) or 0)

    now_mono = time.monotonic()
    logger.debug(
        "heartbeat — connected=%s dialogs=%d/%d%s",
        client.is_connected(),
        synced,
        total,
        _format_heartbeat_eta(sync_start, synced, total, now_mono),
    )
    maybe_log_flood_wait_rollup(logger)


# ---------------------------------------------------------------------------
# Sync loop — batch processing + idle wait
# ---------------------------------------------------------------------------


async def _maybe_heartbeat_and_gap_scan(
    conn: sqlite3.Connection,
    client: _DaemonClient,
    handler_manager: EventHandlerManager,
    state: _SyncLoopState,
    demand_cycle_runner: DemandCycleRunner | None = None,
) -> _SyncLoopState:
    """Run heartbeat and gap scan if their intervals have elapsed.

    Returns the updated loop state.
    """
    now_mono = time.monotonic()

    if now_mono - state.last_heartbeat >= HEARTBEAT_INTERVAL_S:
        _log_heartbeat(conn, client, state.sync_start)
        handler_manager.refresh_synced_dialogs()
        state.last_heartbeat = now_mono

    if now_mono - state.last_gap_scan >= GAP_SCAN_INTERVAL_S:

        async def scan_gap() -> object:
            with rpc_scope(TelegramRpcSource.DELTA_SYNC):
                return await handler_manager.run_dm_gap_scan()

        if demand_cycle_runner is None:
            deleted_count = cast(int, await run_legacy_demand_cycle(None, DemandKind.DELTA_GAP_FILL, scan_gap))
        else:
            deleted_count = cast(int, await demand_cycle_runner(DemandKind.DELTA_GAP_FILL, scan_gap))
        logger.info("gap_scan complete — marked_deleted=%d", deleted_count)
        state.last_gap_scan = now_mono

    return state


async def _run_sync_loop(  # noqa: PLR0913 - explicit legacy loop dependencies
    worker: FullSyncWorker,
    handler_manager: EventHandlerManager,
    shutdown_event: asyncio.Event,
    conn: sqlite3.Connection,
    client: _DaemonClient,
    *,
    demand_cycle_runner: DemandCycleRunner | None = None,
) -> None:
    """Run the batch-sync loop with periodic heartbeat and gap scan."""
    sync_start = time.monotonic()
    state = _SyncLoopState(
        sync_start=sync_start,
        last_heartbeat=sync_start,
        last_gap_scan=sync_start,
    )

    while not shutdown_event.is_set():
        kill_switch_status = flood_wait_kill_switch_status()
        if kill_switch_status.open:
            logger.critical("sync_loop_paused_flood_wait_kill_switch %s", kill_switch_status.detail())
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=HEARTBEAT_INTERVAL_S)
            except TimeoutError:
                continue
            break

        async def process_batch() -> object:
            return await worker.process_one_batch()

        if demand_cycle_runner is None:
            all_synced = cast(
                bool,
                await run_legacy_demand_cycle(None, DemandKind.FULL_SYNC_PAGE, process_batch),
            )
        else:
            all_synced = cast(bool, await demand_cycle_runner(DemandKind.FULL_SYNC_PAGE, process_batch))
        await asyncio.sleep(0)

        state = await _maybe_heartbeat_and_gap_scan(
            conn,
            client,
            handler_manager,
            state,
            demand_cycle_runner,
        )

        if all_synced:
            if not state.was_idle:
                logger.info("sync_idle — all dialogs synced, waiting %ds", HEARTBEAT_INTERVAL_S)
                state.was_idle = True
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=HEARTBEAT_INTERVAL_S,
                )
                break
            except TimeoutError:
                state = await _maybe_heartbeat_and_gap_scan(
                    conn,
                    client,
                    handler_manager,
                    state,
                    demand_cycle_runner,
                )
        elif state.was_idle:
            logger.info("sync_resume — work appeared, exiting idle")
            state.was_idle = False


def _create_tracked_task(
    ctx: _SyncMainContext,
    coro: Awaitable[object],
    *,
    name: str | None = None,
    critical: bool = False,
    demand_kind: DemandKind | None = None,
) -> asyncio.Task[object]:
    """Create an asyncio task and track it for shutdown cancellation."""
    concrete_coro = cast(Coroutine[object, object, object], coro)
    started = False

    async def run_with_demand() -> object:
        nonlocal started
        started = True
        if demand_kind is None:
            return await concrete_coro
        with demand_context(demand_kind):
            return await concrete_coro

    task = asyncio.get_running_loop().create_task(run_with_demand(), name=name, context=Context())
    ctx.background_tasks.add(task)

    def _on_done(t: asyncio.Task[object]) -> None:
        if not started:
            concrete_coro.close()
        ctx.background_tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            try:
                with ctx.conn:
                    record_runtime_observation(
                        ctx.conn,
                        kind="runtime.task_failed",
                        outcome="failed",
                        reason_code=type(exc).__name__,
                        payload={"task_name": t.get_name()},
                    )
            except Exception:
                logger.exception("runtime_event_record_failed kind=runtime.task_failed")
            if critical:
                ctx.api_server._ready = False
                ctx.api_server.startup_detail = f"critical background task failed: {t.get_name()}"
                ctx.shutdown_event.set()
                logger.critical("critical_background_task_failed name=%s error=%s", t.get_name(), exc, exc_info=exc)
            else:
                logger.error("background_task_failed name=%s error=%s", t.get_name(), exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


def _observe_runtime(ctx: _SyncMainContext, kind: str, outcome: str, reason_code: str | None) -> None:
    try:
        with ctx.conn:
            record_runtime_observation(ctx.conn, kind=kind, outcome=outcome, reason_code=reason_code)
    except Exception:
        logger.exception("runtime_event_record_failed kind=%s", kind)


def _record_rpc_admission(
    observer: RpcAdmissionObservationAggregator,
    event: RpcAdmissionEvent,
    fatal_callback: Callable[[RpcAdmissionEvent], None] | None = None,
) -> None:
    """Persist bounded scheduler outcomes without Telegram request content."""
    if fatal_callback is not None and event.kind is RpcAdmissionEventKind.CLOSED and event.reason == "limiter_failure":
        fatal_callback(event)
    observer.observe(event)


def _mark_rpc_scheduler_failed(
    ctx: _SyncMainContext,
    failure_state: dict[str, str | None],
    event: RpcAdmissionEvent,
) -> None:
    """Fail daemon readiness when the account RPC arbiter closes on error."""
    if failure_state["detail"] is not None:
        return
    detail = "Telegram service is temporarily unavailable; daemon is shutting down"
    failure_state["detail"] = detail
    ctx.api_server._ready = False
    ctx.api_server.startup_detail = detail
    ctx.shutdown_event.set()
    logger.critical(
        "telegram_rpc_scheduler_failed_closed reason=%s source=%s service_class=%s",
        event.reason,
        event.source.value if event.source is not None else None,
        event.service_class.value if event.service_class is not None else None,
    )


async def _monitor_flood_wait_kill_switch(ctx: _SyncMainContext) -> None:
    """Stop Telegram-facing work when the account-level FloodWait breaker opens."""
    await ctx.flood_wait_kill_switch_event.wait()
    status = flood_wait_kill_switch_status()
    if not status.open:
        return

    logger.critical("flood_wait_kill_switch_stopping_telegram_work %s", status.detail())
    current_task = asyncio.current_task()
    for task in list(ctx.background_tasks):
        if task is not current_task:
            task.cancel()
    await ctx.client.disconnect()
    logger.critical("flood_wait_kill_switch_telegram_disconnected")


def _install_flood_wait_kill_switch(config: McpTelegramConfig, event: asyncio.Event) -> None:
    policy_config = config.flood_wait
    configure_flood_wait_kill_switch(
        FloodWaitKillSwitchPolicy(
            enabled=policy_config.kill_switch_enabled,
            window_seconds=policy_config.kill_switch_window_seconds,
            max_events=policy_config.kill_switch_max_events,
            max_wait_seconds=policy_config.kill_switch_max_wait_seconds,
        ),
        event=event,
    )


def _delta_catch_up_policy_from_scheduling(scheduling: SchedulingConfig) -> DeltaCatchUpPolicy:
    return DeltaCatchUpPolicy(
        interval_seconds=scheduling.delta_catch_up_interval_seconds,
        max_probes_per_cycle=scheduling.delta_catch_up_max_probes_per_cycle,
        probe_pause_seconds=scheduling.delta_catch_up_probe_pause_seconds,
    )


def _access_probe_policy_from_scheduling(scheduling: SchedulingConfig) -> AccessProbePolicy:
    return AccessProbePolicy(
        interval_seconds=scheduling.access_probe_interval_seconds,
        max_dialogs_per_cycle=scheduling.access_probe_max_dialogs_per_cycle,
        cooldown_seconds=scheduling.access_probe_cooldown_seconds,
        probe_pause_seconds=scheduling.access_probe_pause_seconds,
    )


def _message_fact_refresh_policy_from_config(config: McpTelegramConfig) -> MessageFactRefreshPolicy:
    return MessageFactRefreshPolicy(
        interval_seconds=config.scheduling.message_fact_refresh_seconds,
        reaction_max_messages_per_cycle=config.scheduling.message_fact_refresh_reaction_max_messages_per_cycle,
        read_at_max_messages_per_cycle=config.scheduling.message_fact_refresh_read_at_max_messages_per_cycle,
        pause_seconds=config.scheduling.message_fact_refresh_pause_seconds,
        reaction_ttl_seconds=config.freshness.reactions.freshness_ttl_seconds,
        read_at_ttl_seconds=config.freshness.read_receipts.read_at_ttl_seconds,
    )


def _create_telegram_client(
    config: McpTelegramConfig,
    conn: sqlite3.Connection | None = None,
) -> _DaemonClient:
    """Create the daemon-owned Telethon subclass with account-wide policy."""
    if conn is None:
        return cast(_DaemonClient, create_client(catch_up=True, config=config))
    cooldown_persistence = TelegramRpcCooldownPersistence(
        load_until_utc=partial(load_account_cooldown_until_utc, conn),
        save_until_utc=partial(save_account_cooldown_until_utc, conn),
    )
    return cast(
        _DaemonClient,
        create_client(
            catch_up=True,
            config=config,
            cooldown_persistence=cooldown_persistence,
        ),
    )


async def _build_sync_main_context() -> _SyncMainContext:  # noqa: PLR0914, PLR0915 - composition root wires all daemon-owned services
    config = load_config()
    scheduling = resolve_scheduling_config(config.scheduling)
    state_paths = StatePaths.from_state_dir(ensure_private_state_dir(config.state.dir))
    db_path = state_paths.sync_db_path
    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)
    migrate_legacy_databases(
        conn,
        state_paths.state_dir,
        telemetry_retention_ttl_seconds=config.telemetry.retention_ttl_seconds,
    )
    with conn:
        prune_runtime_observations(
            conn,
            ttl_seconds=config.telemetry.retention_ttl_seconds,
            row_cap=config.telemetry.runtime_observations.row_cap,
        )
        record_runtime_observation(conn, kind="runtime.started", outcome="observed")

    # Open feedback.db before registering the shutdown handler so the SIGTERM
    # handler can checkpoint it.  feedback_conn is opened on the asyncio thread
    # (sync_main coroutine) — the same thread the SIGTERM handler runs on via
    # loop.add_signal_handler — so no cross-thread SQLite sharing occurs.
    feedback_db_path = state_paths.feedback_db_path
    feedback_conn = ensure_feedback_schema(feedback_db_path)
    feedback_service = FeedbackApplicationService(SQLiteFeedbackStore(feedback_conn))
    logger.info("feedback.db ready at %s", feedback_db_path)

    shutdown_event = daemon_shutdown.register_shutdown_handler(
        conn,
        asyncio.get_running_loop(),
        feedback_conn=feedback_conn,
    )
    flood_wait_kill_switch_event = asyncio.Event()
    _install_flood_wait_kill_switch(config, flood_wait_kill_switch_event)

    client = _create_telegram_client(config, conn)
    rpc_scheduler_failure: dict[str, str | None] = {"detail": None}

    def health_status() -> DaemonHealthStatus:
        if rpc_scheduler_failure["detail"] is not None:
            return _RpcSchedulerFailureStatus(rpc_scheduler_failure["detail"])
        return flood_wait_kill_switch_status()

    reaction_freshener = ReactionFreshener(
        SQLiteReactionSnapshotRepository(conn),
        TelethonTelegramReactionGateway(client),
        freshness_ttl_seconds=config.freshness.reactions.freshness_ttl_seconds,
        log=logger,
    )
    topic_refresher = TopicRefresher(
        TelethonTelegramTopicGateway(cast(TopicClient, client)),
        SQLiteTopicSnapshotRepository(conn),
    )
    folder_repository = SQLiteFolderSnapshotRepository(conn)
    folder_refresher = FolderRefresher(
        TelethonTelegramFolderGateway(cast(FolderClient, client)),
        folder_repository,
    )
    api_server = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, client),
        shutdown_event,
        feedback_service,
        db_path,
        reaction_freshener=reaction_freshener,
        hydration_requester=lambda hydration_conn, dialog_id, due_at: reconcile_fact_hydration_jobs_for_dialog(
            hydration_conn,
            dialog_id,
            due_at=due_at,
            priority=HydrationPriority.BACKFILL,
        ),
        topic_refresher=topic_refresher,
        policy=DaemonApiPolicy(
            read_at_ttl_seconds=config.freshness.read_receipts.read_at_ttl_seconds,
            deleted_message_visibility_seconds=config.freshness.inbox.deleted_message_visibility_seconds,
            entity_detail_ttl_seconds=config.freshness.entities.detail_ttl_seconds,
            user_directory_ttl_seconds=config.freshness.entities.user_directory_ttl_seconds,
            group_directory_ttl_seconds=config.freshness.entities.group_directory_ttl_seconds,
            resolver_enrichment_ttl_seconds=config.freshness.entities.resolver_enrichment_ttl_seconds,
            folder_snapshot_stale_after_seconds=config.scheduling.folder_projection.stale_threshold_seconds,
            telemetry=config.telemetry,
            slow_request_seconds=config.logging.daemon_api_slow_request_seconds,
            entity_profile=RefreshLimits(
                foreground_resolve_seconds=config.entity_profile.foreground_resolve_seconds,
                foreground_refresh_wait_seconds=config.entity_profile.foreground_refresh_wait_seconds,
                per_rpc_seconds=config.entity_profile.rpc_timeout_seconds,
                whole_refresh_seconds=config.entity_profile.refresh_timeout_seconds,
                max_concurrent_refreshes=config.entity_profile.max_concurrent_refreshes,
                max_queued_refreshes=config.entity_profile.max_queued_refreshes,
            ),
        ),
        health_status=health_status,
    )
    socket_path = state_paths.daemon_socket_path
    socket_path.unlink(missing_ok=True)
    old_umask = os.umask(0o177)
    try:
        unix_server = await asyncio.start_unix_server(
            api_server.handle_client,
            path=str(socket_path),
            limit=2 * 1024 * 1024,
        )
    finally:
        os.umask(old_umask)
        socket_path.chmod(0o600)
    logger.info("daemon API listening on %s (not ready yet)", socket_path)
    rpc_observation_sink = RuntimeObservationSink(
        db_path,
        retention_ttl_seconds=config.telemetry.retention_ttl_seconds,
        policy=config.telemetry.runtime_observations,
    )
    rpc_admission_observer = RpcAdmissionObservationAggregator(
        rpc_observation_sink,
        policy=config.telemetry.runtime_observations,
    )
    ctx = _SyncMainContext(
        db_path=db_path,
        conn=conn,
        feedback_conn=feedback_conn,
        shutdown_event=shutdown_event,
        client=client,
        reaction_freshener=reaction_freshener,
        reaction_freshness_ttl_seconds=config.freshness.reactions.freshness_ttl_seconds,
        message_fact_refresh_policy=_message_fact_refresh_policy_from_config(config),
        api_server=api_server,
        topic_refresher=topic_refresher,
        folder_projection_worker=FolderProjectionWorker(
            folder_refresher,
            folder_repository,
            shutdown_event,
            config.scheduling.folder_projection,
        ),
        fact_hydration_worker=MessageFactHydrationWorker(
            client,
            conn,
            shutdown_event,
            handlers=(
                MediaFactHydrationHandler(batch_size=scheduling.fact_hydration.batch_size),
                TranscriptionHydrationHandler(
                    recheck_delay_seconds=scheduling.fact_hydration.transcription_recheck_delay_seconds,
                ),
            ),
            interval_seconds=scheduling.fact_hydration.interval_seconds,
            max_requests_per_cycle=scheduling.fact_hydration.max_requests_per_cycle,
            max_jobs_per_cycle=scheduling.fact_hydration.max_jobs_per_cycle,
            pause_between_requests_seconds=scheduling.fact_hydration.pause_between_requests_seconds,
            retry_delay_seconds=scheduling.fact_hydration.retry_delay_seconds,
            circuit_retry_seconds=scheduling.fact_hydration.circuit_retry_seconds,
            max_attempts=scheduling.fact_hydration.max_attempts,
            backfill_debt_limit=scheduling.fact_hydration.backfill_debt_limit,
        ),
        socket_path=socket_path,
        self_profile_cadence=SQLiteSelfProfileCadence(
            conn,
            scheduling.self_profile_refresh_seconds,
        ),
        rpc_observation_sink=rpc_observation_sink,
        rpc_admission_observer=rpc_admission_observer,
        unix_server=unix_server,
        scheduling=scheduling,
        flood_wait_kill_switch_event=flood_wait_kill_switch_event,
    )

    sink_error = rpc_observation_sink.writer_error
    if sink_error is not None:
        detail = f"Runtime observation sink failed during startup: {sink_error}"
        rpc_scheduler_failure["detail"] = detail
        api_server._ready = False
        api_server.startup_detail = detail
        shutdown_event.set()
        logger.critical("runtime_observation_sink_startup_failed error=%s", sink_error)
    else:
        client.set_rpc_admission_observer(
            partial(
                _record_rpc_admission,
                rpc_admission_observer,
                fatal_callback=partial(_mark_rpc_scheduler_failed, ctx, rpc_scheduler_failure),
            )
        )
    return ctx


async def _run_fts_backfill(ctx: _SyncMainContext) -> None:
    # FTS backfill runs in a thread pool (stemming is CPU-bound) so it doesn't
    # block the event loop. Awaited here — before Telegram connect — so the
    # socket is already up and responding "not ready / indexing messages for
    # search" while we work. Total startup time = FTS time + Telegram time.
    ctx.api_server.startup_detail = "indexing messages for search"
    _ = ctx.api_server.startup_detail
    try:
        # Open a dedicated connection for the thread — sqlite3 connections are
        # not thread-safe and cannot be shared across threads.
        def _backfill_in_thread() -> int:
            thread_conn = _open_sync_db(ctx.db_path)
            try:
                return backfill_fts_index(thread_conn)
            finally:
                thread_conn.close()

        backfilled = await asyncio.to_thread(_backfill_in_thread)
        if backfilled:
            logger.info("fts_backfill=%d messages indexed", backfilled)
    except Exception:
        logger.warning("fts_backfill failed — FTS search may be incomplete until next restart", exc_info=True)


async def _connect_telegram(ctx: _SyncMainContext) -> bool:
    try:
        ctx.api_server.startup_detail = "connecting to Telegram"
        _ = ctx.api_server.startup_detail
        await ctx.client.connect()
    except (TimeoutError, OSError) as exc:
        ctx.api_server.startup_detail = f"connection failed: {exc}"
        logger.exception("sync-daemon connection failed: %s", exc)
        return False

    logger.info("sync-daemon started — connected=%s", ctx.client.is_connected())
    return True


async def _fetch_own_only_personal_channel_id(client: _DaemonClient, account_id: int) -> int | None:
    input_user = cast(TypeInputUser, await client.get_input_entity(account_id))
    full_result = await client(GetFullUserRequest(id=input_user))
    user_full = getattr(full_result, "full_user", None)
    personal_channel_id = getattr(user_full, "personal_channel_id", None)
    return personal_channel_id if isinstance(personal_channel_id, int) and personal_channel_id > 0 else None


async def _wait_for_own_only_admission_retry(
    shutdown: asyncio.Event,
    exc: TelegramRpcAdmissionDeferred,
    attempt: int,
) -> None:
    delay = min(
        max(exc.retry_after_seconds or 1, 1),
        _OWN_ONLY_ADMISSION_MAX_WAIT_SECONDS,
    )
    logger.info("own_only_account_facts_admission_deferred retry_after=%s attempt=%d", delay, attempt)
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=delay)
    except TimeoutError:
        return
    logger.info("own_only_account_facts_deferred_until_shutdown")
    raise asyncio.CancelledError from None


async def _load_own_only_context(
    client: _DaemonClient,
    account_id: int,
    shutdown_event: asyncio.Event | None = None,
) -> OwnOnlyContext:
    context = OwnOnlyContext(account_id=account_id)
    shutdown = asyncio.Event() if shutdown_event is None else shutdown_event
    attempt = 0
    while True:
        try:
            personal_channel_id = await _fetch_own_only_personal_channel_id(client, account_id)
        except RpcAdmissionClosedError:
            raise
        except TelegramRpcAdmissionDeferred as exc:
            attempt += 1
            await _wait_for_own_only_admission_retry(shutdown, exc, attempt)
            continue
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            logger.warning("own_only_account_facts_unavailable error=%s", exc)
            break
        except (RPCError, TypeError, AttributeError, ValueError) as exc:
            logger.warning("own_only_account_facts_unavailable error=%s", exc)
            break
        if personal_channel_id is not None:
            return OwnOnlyContext(account_id=account_id, personal_channel_id=personal_channel_id)
        break
    return context


async def _prime_runtime(ctx: _SyncMainContext) -> None:
    # Phase 39.1: cache authenticated user id once at startup so query-build
    # paths (Plan 39.1-02) can bind it as a SQL parameter without calling
    # Telethon per request. Failure propagates — daemon cannot serve reads
    # correctly without a stable self_id.
    ctx.api_server.startup_detail = "fetching account info"
    _ = ctx.api_server.startup_detail

    async def prime_account() -> object:
        with acquisition_context(AcquisitionKind.ACCOUNT_SELF_PROFILE):
            me = cast(_MeLike, await ctx.client.get_me())
            _update_self_profile(ctx.api_server, me)
        assert ctx.api_server.self_id is not None
        assert ctx.handler_manager is not None
        ctx.handler_manager.set_self_id(ctx.api_server.self_id)
        ctx.own_only_context = await _load_own_only_context(
            ctx.client,
            ctx.api_server.self_id,
            getattr(ctx, "shutdown_event", None),
        )
        cadence = cast(SQLiteSelfProfileCadence | None, getattr(ctx, "self_profile_cadence", None))
        if cadence is not None:
            cadence.mark_refreshed(time.time())
        return me

    await _run_ctx_demand_cycle(ctx, DemandKind.SELF_PROFILE_MAINTENANCE, prime_account)
    ensure_own_only_schema(ctx.conn)
    logger.info("daemon self_id cached: %s", ctx.api_server.self_id)

    ctx.api_server.startup_detail = "refreshing Telegram folders"
    await ctx.folder_projection_worker.prime()

    # Post-v10 runtime backfill: mark historical outgoing DM rows as out=1
    # using sender_id=self_id (the authoritative signal). Pure-SQL v10
    # migration can only match sender_id IS NULL, but re-ingestion after
    # Phase 39.1 typically populates sender_id with the real peer/self
    # values — so the NULL-sender shape is rare in practice. This daemon
    # step closes the gap once self_id is known. Idempotent via out=0.
    try:
        cur = ctx.conn.execute(
            "UPDATE messages SET out = 1 WHERE out = 0 AND dialog_id > 0 AND sender_id = ?",
            (ctx.api_server.self_id,),
        )
        ctx.conn.commit()
        if cur.rowcount > 0:
            logger.info("backfilled out=1 on %d historical outgoing DM rows", cur.rowcount)
    except Exception:
        logger.warning("out=1 backfill skipped — non-fatal", exc_info=True)

    ctx.api_server._ready = True
    if ctx.api_server._ready:
        pass
    logger.info("daemon ready — serving requests on %s", ctx.socket_path)


def _update_self_profile(api_server: DaemonAPIServer, me: _MeLike) -> None:
    """Atomically replace the account identity exposed to local readers."""
    self_id = int(me.id)
    api_server.self_id = self_id
    api_server.self_profile = {
        "id": self_id,
        "first_name": getattr(me, "first_name", None),
        "last_name": getattr(me, "last_name", None),
        "username": getattr(me, "username", None),
    }


async def _run_self_profile_refresh_loop(ctx: _SyncMainContext) -> None:
    """Refresh account display identity off the MCP request path."""
    while True:
        try:
            await asyncio.wait_for(
                ctx.shutdown_event.wait(),
                timeout=ctx.scheduling.self_profile_refresh_seconds,
            )
            return
        except TimeoutError:
            pass

        try:

            async def refresh_profile() -> object:
                with acquisition_context(AcquisitionKind.ACCOUNT_SELF_PROFILE):
                    me = cast(_MeLike, await ctx.client.get_me())
                    _update_self_profile(ctx.api_server, me)
                return me

            await _run_ctx_demand_cycle(ctx, DemandKind.SELF_PROFILE_MAINTENANCE, refresh_profile)
            cadence = cast(SQLiteSelfProfileCadence | None, getattr(ctx, "self_profile_cadence", None))
            if cadence is not None:
                cadence.mark_refreshed(time.time())
            offer_durable_demand(ctx.demand_shadow, DemandKind.SELF_PROFILE_MAINTENANCE)
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            logger.info("self_profile_refresh_deferred retry_after=%s", exc.retry_after_seconds)
        except (RPCError, OSError, TimeoutError) as exc:
            logger.warning("self_profile_refresh_failed error=%s", exc)


async def _start_bootstrap_background_tasks(
    ctx: _SyncMainContext,
    worker: FullSyncWorker,
) -> None:
    assert ctx.handler_manager is not None

    ctx.api_server.startup_detail = "bootstrapping DMs"
    _ = ctx.api_server.startup_detail

    async def bootstrap_dms() -> object:
        return await worker.bootstrap_dms()

    enrolled = cast(
        int,
        await _run_ctx_demand_cycle(ctx, DemandKind.FULL_SYNC_DM_ENROLLMENT, bootstrap_dms),
    )
    logger.info("dm_bootstrap complete — enrolled=%d", enrolled)
    offer_durable_demand(ctx.demand_shadow, DemandKind.FULL_SYNC_PAGE, DemandKind.READ_RECEIPT_BATCH)

    ctx.handler_manager.refresh_synced_dialogs()

    # Background tasks — non-blocking, tracked for shutdown
    # D-07 / BOOTSTRAP-05: handler_manager.register() and refresh_synced_dialogs()
    # are both above this line, so live events for any dialog the bootstrap
    # touches are guaranteed to be wired before the first UPSERT.
    # BOOTSTRAP-02: this is a background task — does not block api_server._ready
    # (already set) or the /health endpoint.
    # Phase 41 review HIGH: pass db_path (NOT conn) — the worker opens its own
    # dedicated SQLite connection inside __init__, isolating it from the
    # daemon's main conn used by the other background tasks.
    dialogs_bootstrap = DialogsBootstrapWorker(
        ctx.client,
        ctx.db_path,
        ctx.shutdown_event,
        startup_detail_setter=lambda s: setattr(ctx.api_server, "startup_detail", s),
    )
    task_specs: list[tuple[Awaitable[object], str]] = [
        (
            _run_ctx_demand_cycle(ctx, DemandKind.DIALOG_BOOTSTRAP, dialogs_bootstrap.run),
            "dialogs_bootstrap_sweep",
        ),
        (
            _run_ctx_demand_cycle(
                ctx,
                DemandKind.FULL_SYNC_PAGE,
                lambda: _backfill_total_messages(ctx.client, ctx.conn, ctx.shutdown_event),
            ),
            "backfill_total_messages",
        ),
    ]
    for coro, name in task_specs:
        _create_tracked_task(ctx, coro, name=name)


async def _run_message_fact_refresh_with_dedicated_connection(
    ctx: _SyncMainContext,
    dependencies: MessageFactRefreshDeps | None = None,
) -> None:
    """Run legacy fact refresh with the same dependencies exposed to shadow."""
    owns_dependencies = dependencies is None
    deps = dependencies or _build_message_fact_refresh_dependencies(ctx)
    try:
        policy = ctx.message_fact_refresh_policy
        if policy.reaction_max_messages_per_cycle <= 0 and policy.read_at_max_messages_per_cycle <= 0:
            logger.info(
                "message_fact_refresh_loop disabled — reaction_max_messages_per_cycle=%d "
                "read_at_max_messages_per_cycle=%d",
                policy.reaction_max_messages_per_cycle,
                policy.read_at_max_messages_per_cycle,
            )
            return
        while not ctx.shutdown_event.is_set():
            try:

                async def refresh_facts() -> MessageFactRefreshResult:
                    return await refresh_message_facts_once(
                        deps,
                        policy,
                        shutdown_event=ctx.shutdown_event,
                    )

                result = await _run_ctx_demand_cycle(ctx, DemandKind.MESSAGE_FACT_REFRESH, refresh_facts)
                logger.debug(
                    "message_fact_refresh_cycle complete — reaction_candidates=%d reaction_refreshed=%d "
                    "read_at_candidates=%d",
                    result.reaction_candidates,
                    result.reaction_refreshed,
                    result.read_at_candidates,
                )
            except Exception:
                logger.warning("message_fact_refresh_cycle failed", exc_info=True)
            try:
                await asyncio.wait_for(ctx.shutdown_event.wait(), timeout=policy.interval_seconds)
            except TimeoutError:
                continue
    finally:
        if owns_dependencies:
            deps.conn.close()


def _build_message_fact_refresh_dependencies(ctx: _SyncMainContext) -> MessageFactRefreshDeps:
    conn = _open_sync_db(ctx.db_path)
    return MessageFactRefreshDeps(
        conn,
        ReactionFreshener(
            SQLiteReactionSnapshotRepository(conn),
            TelethonTelegramReactionGateway(ctx.client),
            freshness_ttl_seconds=ctx.reaction_freshness_ttl_seconds,
            log=logger,
        ),
        TelethonTelegramReadReceiptGateway(ctx.client),
    )


def _scan_demand_shadow(ctx: _SyncMainContext) -> None:
    """Run an authoritative recovery scan outside a concrete legacy cycle."""
    shadow = cast(TelegramDemandShadow | None, getattr(ctx, "demand_shadow", None))
    if shadow is not None:
        try:
            shadow.after_cycle_scan()
        except Exception:
            logger.warning("telegram_demand_shadow_scan_failed", exc_info=True)


async def _run_ctx_demand_cycle[T](
    ctx: _SyncMainContext,
    kind: DemandKind,
    operation: Callable[[], Awaitable[T]],
) -> T:
    """Run one real legacy cycle through the installed shadow evidence API."""
    shadow = cast(TelegramDemandShadow | None, getattr(ctx, "demand_shadow", None))
    return await run_legacy_demand_cycle(shadow, kind, operation)


def _build_demand_runtime(
    ctx: _SyncMainContext,
    full_sync_worker: FullSyncWorker,
    delta_sync_worker: DeltaSyncWorker,
) -> _DemandRuntime:
    """Build exhaustive shadow adapters over the exact legacy-owned objects."""
    entity_service = ctx.api_server._get_entity_info_service()
    entity_refresh_coordinator = entity_service.refresh_coordinator
    if entity_refresh_coordinator is None:
        raise RuntimeError("entity profile refresh coordinator is unavailable")

    message_fact_refresh_deps = _build_message_fact_refresh_dependencies(ctx)
    scheduled_reconciler = ScheduledMessageReconciler(
        ctx.client,
        ctx.conn,
        ctx.shutdown_event,
        ctx.own_only_context,
        policy=ScheduledReconciliationPolicy(
            activity_rpc_timeout_seconds=ctx.scheduling.activity_rpc_timeout_seconds,
            state_scan_seconds=ctx.scheduling.scheduled_reconciliation_seconds,
        ),
    )
    dialog_reconciliation_worker = DialogReconciliationWorker(
        ctx.client,
        ctx.conn,
        ctx.shutdown_event,
        ctx.topic_refresher,
    )

    async def read_receipt_batch() -> object:
        return await _initialize_read_positions(
            ctx.client,
            ctx.conn,
            ctx.shutdown_event,
            max_dialogs=ctx.scheduling.read_position_reconciliation_max_dialogs_per_pass,
            failure_cooldown_seconds=ctx.scheduling.read_position_reconciliation_failure_cooldown_seconds,
            batch_size=ctx.scheduling.read_position_reconciliation_batch_size,
            batch_pause_seconds=ctx.scheduling.read_position_reconciliation_batch_pause_seconds,
            success_recheck_seconds=ctx.scheduling.read_position_reconciliation_seconds,
        )

    try:
        adapters = build_durable_adapter_map(
            DemandCompositionDependencies(
                client=cast(DemandCompositionClient, ctx.client),
                conn=ctx.conn,
                db_path=ctx.db_path,
                shutdown_event=ctx.shutdown_event,
                full_sync_worker=full_sync_worker,
                delta_sync_worker=delta_sync_worker,
                dialog_reconciliation_worker=dialog_reconciliation_worker,
                entity_refresh_coordinator=entity_refresh_coordinator,
                fact_hydration_worker=ctx.fact_hydration_worker,
                folder_projection_worker=ctx.folder_projection_worker,
                message_fact_refresh_deps=message_fact_refresh_deps,
                message_fact_refresh_policy=ctx.message_fact_refresh_policy,
                scheduled_reconciler=scheduled_reconciler,
                access_probe_policy=_access_probe_policy_from_scheduling(ctx.scheduling),
                hot_sweep_policy=ctx.scheduling.activity_hot_sweep,
                cold_backfill_pacing=ColdBackfillPacing.from_scheduling(ctx.scheduling),
                activity_rpc_timeout_seconds=ctx.scheduling.activity_rpc_timeout_seconds,
                read_receipt_batch=read_receipt_batch,
                self_profile_cadence=ctx.self_profile_cadence,
                update_self_profile=lambda me: _update_self_profile(ctx.api_server, cast(_MeLike, me)),
                startup_detail_setter=lambda detail: setattr(ctx.api_server, "startup_detail", detail),
            )
        )
        shadow = TelegramDemandShadow(
            adapters,
            ctx.shutdown_event,
            observer=ctx.rpc_admission_observer,
        )
    except BaseException:
        message_fact_refresh_deps.conn.close()
        raise
    return _DemandRuntime(
        shadow=shadow,
        scheduled_reconciler=scheduled_reconciler,
        dialog_reconciliation_worker=dialog_reconciliation_worker,
        message_fact_refresh_deps=message_fact_refresh_deps,
        read_receipt_batch=read_receipt_batch,
    )


def _ensure_demand_runtime(
    ctx: _SyncMainContext,
    full_sync_worker: FullSyncWorker,
    delta_sync_worker: DeltaSyncWorker,
) -> _DemandRuntime:
    """Install the always-on shadow once, before legacy background launchers."""
    if ctx.demand_runtime is not None:
        return ctx.demand_runtime
    demand_runtime = _build_demand_runtime(ctx, full_sync_worker, delta_sync_worker)
    ctx.demand_runtime = demand_runtime
    ctx.demand_shadow = demand_runtime.shadow
    ctx.api_server.bind_demand_shadow(demand_runtime.shadow)
    if ctx.handler_manager is not None:
        ctx.handler_manager.bind_demand_shadow(demand_runtime.shadow)
    ctx.fact_hydration_worker.bind_demand_shadow(demand_runtime.shadow)
    ctx.folder_projection_worker.bind_demand_shadow(demand_runtime.shadow)
    _create_tracked_task(
        ctx,
        demand_runtime.shadow.run(),
        name="telegram_demand_shadow",
    )
    return demand_runtime


async def _run_scheduled_reconciliation_loop(
    ctx: _SyncMainContext,
    reconciler: ScheduledMessageReconciler,
) -> None:
    """Preserve the legacy scheduler around the shared reconciler instance."""
    while not ctx.shutdown_event.is_set():
        for kind in (DemandKind.SCHEDULED_REPAIR, DemandKind.SCHEDULED_DISCOVERY):
            try:
                await _run_ctx_demand_cycle(
                    ctx,
                    kind,
                    partial(reconciler.run_demand_slice, kind),
                )
            except RpcAdmissionClosedError, asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("scheduled_reconcile_failed kind=%s", kind.value, exc_info=True)
        try:
            await asyncio.wait_for(ctx.shutdown_event.wait(), timeout=reconciler._wait_timeout())
        except TimeoutError:
            continue


async def _run_dialog_reconciliation_loop(
    ctx: _SyncMainContext,
    worker: DialogReconciliationWorker,
) -> None:
    """Preserve legacy dialog cadence around the shared worker instance."""
    last_full_pass = _read_last_full_reconciliation_at(ctx.conn)
    while not ctx.shutdown_event.is_set():
        now = time.time()
        try:
            await _run_ctx_demand_cycle(
                ctx,
                DemandKind.DIALOG_LIGHT_RECONCILIATION,
                worker.run_light_pass,
            )
        except RpcAdmissionClosedError:
            raise
        except Exception:
            logger.warning("recon_light_pass_error", exc_info=True)
        if last_full_pass is None or now - last_full_pass >= DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS:
            try:
                _count, completed = await _run_ctx_demand_cycle(
                    ctx,
                    DemandKind.DIALOG_FULL_RECONCILIATION,
                    worker.run_full_pass,
                )
                if completed:
                    last_full_pass = _read_last_full_reconciliation_at(ctx.conn) or time.time()
            except RpcAdmissionClosedError:
                raise
            except Exception:
                logger.warning("recon_full_pass_error", exc_info=True)
        try:
            await asyncio.wait_for(
                ctx.shutdown_event.wait(),
                timeout=ctx.scheduling.reconciliation_hourly_seconds,
            )
            return
        except TimeoutError:
            continue


async def _start_followup_background_tasks(
    ctx: _SyncMainContext,
    delta_worker: DeltaSyncWorker,
    full_sync_worker: FullSyncWorker,
) -> None:
    activity_client = cast(ActivityClient, ctx.client)
    delta_client = cast(_DeltaSyncClient, ctx.client)
    demand_runtime = _ensure_demand_runtime(ctx, full_sync_worker, delta_worker)
    if ctx.rpc_admission_observer is not None:
        _create_tracked_task(
            ctx,
            ctx.rpc_admission_observer.run_periodic_flush(ctx.shutdown_event),
            name="rpc_admission_observation_flush_loop",
        )
    _create_tracked_task(
        ctx,
        ctx.folder_projection_worker.run(),
        name="folder_projection_worker",
        critical=True,
    )
    _create_tracked_task(
        ctx,
        _run_self_profile_refresh_loop(ctx),
        name="self_profile_refresh_loop",
    )
    _create_tracked_task(
        ctx,
        run_delta_catch_up_loop(
            delta_worker,
            ctx.shutdown_event,
            _delta_catch_up_policy_from_scheduling(ctx.scheduling),
            demand_cycle_runner=partial(_run_ctx_demand_cycle, ctx),
        ),
        name="delta_catch_up_loop",
    )
    _create_tracked_task(
        ctx,
        _run_message_fact_refresh_with_dedicated_connection(
            ctx,
            demand_runtime.message_fact_refresh_deps,
        ),
        name="message_fact_refresh_loop",
    )
    _create_tracked_task(
        ctx,
        ctx.fact_hydration_worker.run(),
        name="message_fact_hydration_worker",
    )
    _create_tracked_task(
        ctx,
        run_access_probe_loop(
            delta_client,
            ctx.conn,
            ctx.shutdown_event,
            delta_worker,
            _access_probe_policy_from_scheduling(ctx.scheduling),
            demand_cycle_runner=partial(_run_ctx_demand_cycle, ctx),
        ),
        name="access_probe_loop",
    )
    _create_tracked_task(
        ctx,
        run_activity_sync_loop(
            activity_client,
            ctx.conn,
            ctx.shutdown_event,
            timeout_s=ctx.scheduling.activity_rpc_timeout_seconds,
            demand_cycle_runner=partial(_run_ctx_demand_cycle, ctx),
        ),
        name="activity_sync_loop",
    )
    _create_tracked_task(
        ctx,
        run_hot_sweep_loop(
            activity_client,
            ctx.conn,
            ctx.shutdown_event,
            policy=ctx.scheduling.activity_hot_sweep,
            timeout_s=ctx.scheduling.activity_rpc_timeout_seconds,
            demand_cycle_runner=partial(_run_ctx_demand_cycle, ctx),
        ),
        name="activity_hot_sweep",
    )
    _create_tracked_task(
        ctx,
        run_cold_backfill_loop(
            activity_client,
            ctx.conn,
            ctx.shutdown_event,
            pacing=ColdBackfillPacing.from_scheduling(ctx.scheduling),
            timeout_s=ctx.scheduling.activity_rpc_timeout_seconds,
            demand_cycle_runner=partial(_run_ctx_demand_cycle, ctx),
        ),
        name="activity_cold_backfill",
    )
    _create_tracked_task(
        ctx,
        _run_scheduled_reconciliation_loop(
            ctx,
            demand_runtime.scheduled_reconciler,
        ),
        name="scheduled_message_reconciliation",
    )

    # Phase 43 / RECON-01: hourly light pass + daily full pass keeps the
    # `dialogs` snapshot fresh; processes needs_refresh=1 rows written by
    # Phase 42 event handlers and soft-deletes left/kicked dialogs once a day.
    #
    # The scheduling config's RECON_HOURLY_SECONDS override (43-REVIEWS.md MEDIUM): default is
    # 3600s (1h) for production; setting it to a smaller value (e.g. "30") lets
    # an operator observe a needs_refresh=1 -> 0 transition in seconds during
    # UAT. Daily interval stays at the default 86400s — there is no need for a
    # daily override yet, and the first iteration always runs a full pass
    # regardless of last_full_pass anyway.
    _create_tracked_task(
        ctx,
        _run_dialog_reconciliation_loop(
            ctx,
            demand_runtime.dialog_reconciliation_worker,
        ),
        name="reconciliation_loop",
    )


async def _stop_daemon_api(ctx: _SyncMainContext) -> None:
    if ctx.unix_server is not None:
        ctx.unix_server.close()
        await ctx.unix_server.wait_closed()
    shutdown = getattr(ctx.api_server, "shutdown", None)
    if shutdown is not None:
        await shutdown()
    ctx.socket_path.unlink(missing_ok=True)


async def _cancel_background_tasks(ctx: _SyncMainContext) -> None:
    if ctx.handler_manager is not None:
        ctx.handler_manager.unregister()
    for task in ctx.background_tasks:
        task.cancel()
    for task in list(ctx.background_tasks):
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected on shutdown; task was cancelled cleanly
        except Exception:
            logger.warning("background_task_shutdown_error name=%s", task.get_name(), exc_info=True)
    ctx.background_tasks.clear()
    demand_runtime = cast(_DemandRuntime | None, getattr(ctx, "demand_runtime", None))
    if demand_runtime is not None:
        demand_runtime.message_fact_refresh_deps.conn.close()


def _close_runtime_connections(ctx: _SyncMainContext) -> None:
    try:
        ctx.feedback_conn.close()
    except Exception:
        logger.debug("feedback_conn close error", exc_info=True)
    try:
        with ctx.conn:
            record_runtime_observation(ctx.conn, kind="runtime.stopped", outcome="observed")
    except Exception:
        logger.exception("runtime_event_record_failed kind=runtime.stopped")
    ctx.conn.close()


def _persist_runtime_observation_loss(ctx: _SyncMainContext) -> None:
    """Persist a content-free boundary when the async telemetry sink lost rows."""
    sink = ctx.rpc_observation_sink
    if sink is None:
        return
    queue_full_drops = int(getattr(sink, "queue_full_drops", 0) or 0)
    shutdown_drops = int(getattr(sink, "shutdown_grace_drops", 0) or 0)
    startup_drops = int(getattr(sink, "startup_drops", 0) or 0)
    rejected_submissions = int(getattr(sink, "rejected_submissions", 0) or 0)
    writer_failures = int(getattr(sink, "permanent_failures", 0) or 0)
    if not any((queue_full_drops, shutdown_drops, startup_drops, rejected_submissions, writer_failures)):
        return
    with ctx.conn:
        ctx.conn.executemany(
            "INSERT OR REPLACE INTO daemon_state(key,value) VALUES (?,?)",
            (
                ("runtime_observations_last_loss_ms", str(int(time.time() * 1000))),
                ("runtime_observations_last_queue_full_drops", str(queue_full_drops)),
                ("runtime_observations_last_writer_failures", str(writer_failures)),
            ),
        )


async def _run_shutdown_stage(
    label: str,
    operation: Callable[[], Awaitable[object]],
    primary: BaseException | None,
) -> BaseException | None:
    try:
        await operation()
    except BaseException as exc:
        logger.exception("daemon_shutdown_stage_failed stage=%s", label)
        return primary if primary is not None else exc
    return primary


async def _shutdown_sync_main_context(ctx: _SyncMainContext) -> None:
    shutdown_event = cast(asyncio.Event | None, getattr(ctx, "shutdown_event", None))
    if shutdown_event is not None:
        shutdown_event.set()

    async def detach_observer() -> None:
        ctx.client.set_rpc_admission_observer(None)

    async def drain_telemetry() -> None:
        if ctx.rpc_admission_observer is not None:
            ctx.rpc_admission_observer.flush()
        if ctx.rpc_observation_sink is not None:
            try:
                await ctx.rpc_observation_sink.aclose()
            finally:
                _persist_runtime_observation_loss(ctx)

    async def close_runtime_connections() -> None:
        _close_runtime_connections(ctx)

    stages: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
        ("daemon_api", lambda: _stop_daemon_api(ctx)),
        ("background_tasks", lambda: _cancel_background_tasks(ctx)),
        ("telegram_disconnect", lambda: ctx.client.disconnect()),
        ("rpc_scheduler", lambda: ctx.client.close_rpc_scheduler()),
        ("rpc_observer_detach", detach_observer),
        ("telemetry", drain_telemetry),
        ("runtime_connections", close_runtime_connections),
    )
    primary: BaseException | None = None
    for label, operation in stages:
        primary = await _run_shutdown_stage(label, operation, primary)
    logger.info("sync-daemon stopped")
    if primary is not None:
        raise primary


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def sync_main() -> None:
    """Main entry point for the sync daemon process.

    Orchestrates: DB init → FTS backfill → Telegram connect → wire services →
    sync loop → cleanup.
    """
    ctx = await _build_sync_main_context()
    try:
        if ctx.rpc_observation_sink is not None and ctx.rpc_observation_sink.writer_error is not None:
            return
        _create_tracked_task(
            ctx,
            _monitor_flood_wait_kill_switch(ctx),
            name="flood_wait_kill_switch_monitor",
        )
        await _run_fts_backfill(ctx)

        input_peer_resolver = cast(InputPeerResolver, partial(resolve_input_peer, cast(ActivityClient, ctx.client)))
        ctx.handler_manager = EventHandlerManager(ctx.client, ctx.conn, ctx.shutdown_event, input_peer_resolver)
        ctx.handler_manager.register()
        logger.info("event handlers registered")

        if not await _connect_telegram(ctx):
            return

        # Telethon owns initial catch-up through catch_up=True. Keep the
        # application-owned transition watcher live for the rest of startup
        # and the daemon lifetime so transient reconnects are observed too.
        _create_tracked_task(
            ctx,
            run_reconnect_catch_up_loop(
                ctx.client,
                ctx.shutdown_event,
                interval_seconds=ctx.scheduling.reconnect_catch_up_interval_seconds,
                observe=lambda kind, outcome, reason: _observe_runtime(ctx, kind, outcome, reason),
            ),
            name="reconnect_catch_up_loop",
        )

        delta_worker = DeltaSyncWorker(cast(_DeltaSyncClient, ctx.client), ctx.conn, ctx.shutdown_event)
        worker = FullSyncWorker(ctx.client, ctx.conn, ctx.shutdown_event)
        demand_runtime = _ensure_demand_runtime(ctx, worker, delta_worker)
        await _prime_runtime(ctx)
        # Demand composition must exist before startup Telegram calls so those
        # calls emit shadow evidence. Account identity is learned by that first
        # observed cycle, before the scheduled reconciler can run.
        demand_runtime.scheduled_reconciler._own_only_context = ctx.own_only_context
        demand_runtime.scheduled_reconciler._resolved_context = ctx.own_only_context
        await _start_bootstrap_background_tasks(ctx, worker)
        # Must come AFTER handler_manager.register() (startup-ordering invariant):
        # the raw inbox read handler must be live before bootstrap starts so no
        # real-time cursor updates are dropped during the bootstrap window.
        _create_tracked_task(
            ctx,
            _run_read_position_reconciliation_loop(
                ctx.client,
                ctx.conn,
                ctx.shutdown_event,
                interval_seconds=ctx.scheduling.read_position_reconciliation_seconds,
                max_dialogs_per_pass=ctx.scheduling.read_position_reconciliation_max_dialogs_per_pass,
                failure_cooldown_seconds=ctx.scheduling.read_position_reconciliation_failure_cooldown_seconds,
                batch_size=ctx.scheduling.read_position_reconciliation_batch_size,
                batch_pause_seconds=ctx.scheduling.read_position_reconciliation_batch_pause_seconds,
                demand_cycle_runner=partial(_run_ctx_demand_cycle, ctx),
                run_batch=demand_runtime.read_receipt_batch,
            ),
            name="initialize_read_positions",
        )
        await _start_followup_background_tasks(ctx, delta_worker, worker)
        await _run_sync_loop(
            worker,
            ctx.handler_manager,
            ctx.shutdown_event,
            ctx.conn,
            ctx.client,
            demand_cycle_runner=partial(_run_ctx_demand_cycle, ctx),
        )
    finally:
        await _shutdown_sync_main_context(ctx)


_SYNC_MAIN = sync_main
