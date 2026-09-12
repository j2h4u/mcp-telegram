"""Sync daemon — long-running process that exclusively owns the TelegramClient.

Started via ``mcp-telegram sync``. Connects to Telegram, ensures sync.db schema,
then runs the process-wide durable demand coordinator with periodic heartbeat
logging and clean SIGTERM handling.

Architecture:
- sync-daemon is the sole owner of TelegramClient — connects once, holds it.
- MCP server runs separately with disable_telegram_session() active and reads
  sync.db via open_sync_db_reader(); it never calls client.connect().
- SIGTERM triggers shutdown_event (set by daemon_shutdown), which checkpoints
  WAL before the daemon disconnects.

Event handlers:
- A startup barrier is attached before connect. Telethon's persisted catch-up
  reaches the registered callbacks but they wait until account bind succeeds.
- synced_dialogs set is refreshed every heartbeat so newly enrolled dialogs
  are picked up within one interval without re-registering handlers.
- Durable Telegram work is selected and executed by one coordinator task.

Delta catch-up:
- connect() retains Telethon's catch_up=True persisted update recovery; the
  reconnect loop invokes public catch_up() after later reconnects.
- reconnect_catch_up_loop polls public connection state and invokes public
  catch_up() once per observed disconnected→connected transition.
- Delta gap and access recovery are durable demand slices selected by the
  process-wide coordinator.

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
from .activity_cold_backfill import ColdBackfillPacing
from .activity_contracts import InputPeerResolver
from .activity_peer_resolve import resolve_input_peer
from .activity_substrate import ActivityClient
from .auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from .config import McpTelegramConfig, SchedulingConfig, load_config, resolve_scheduling_config
from .daemon_api import DaemonApiPolicy, DaemonAPIServer, DaemonClientLike, DaemonHealthStatus
from .delta_sync import AccessProbePolicy, DeltaSyncWorker, DmGapScanPage, _DeltaSyncClient
from .demand_composition import (
    DemandCompositionClient,
    DemandCompositionDependencies,
    build_durable_coordinator,
)
from .dialog_directory import CanonicalDialogDirectory
from .dialog_sync import DialogReconciliationWorker
from .entity_profile.refresh import RefreshLimits
from .event_handlers import EventHandlerManager, UpdateProcessingBarrier
from .fact_hydration import MessageFactHydrationWorker
from .feedback_db import SQLiteFeedbackStore, ensure_feedback_schema
from .feedback_service import FeedbackApplicationService
from .flood import (
    FloodWaitKillSwitchPolicy,
    TelegramRpcThrottled,
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
)
from .messages.sqlite_hydration_jobs import reconcile_fact_hydration_jobs_for_dialog
from .own_only import ensure_own_only_schema
from .own_only_contracts import OwnOnlyContext
from .reactions.refresh import ReactionFreshener
from .reactions.sqlite_repository import SQLiteReactionSnapshotRepository
from .reactions.telegram_adapter import TelethonTelegramReactionGateway
from .read_state import apply_read_cursor, apply_reconciled_unread_count
from .reconnect import run_reconnect_catch_up_loop
from .rpc_admission_observations import RpcAdmissionObservationAggregator
from .runtime_observations import RuntimeObservationSink, prune_runtime_observations, record_runtime_observation
from .scheduled_messages import ScheduledMessageReconciler, ScheduledReconciliationPolicy
from .self_profile_maintenance import (
    SelfProfileMaintenanceDemandAdapter,
    SelfProfileMaintenanceDependencies,
)
from .startup_identity import (
    StartupIdentityResult,
    StartupIdentityState,
)
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
from .telegram_demand import DemandStatus, RpcAttemptBudget, demand_context
from .telegram_demand_coordinator import TelegramDemandCoordinator
from .telegram_read_receipts import TelethonTelegramReadReceiptGateway
from .telegram_rpc import TelegramRpcCooldownPersistence
from .telegram_rpc_consumers import DemandKind, demand_contract
from .telegram_rpc_scheduler import (
    AdmissionObserver,
    RpcAdmissionClosedError,
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    TelegramRpcAdmissionDeferred,
)
from .topics.refresh import TopicRefresher
from .topics.sqlite_repository import SQLiteTopicSnapshotRepository
from .topics.telegram_adapter import TelethonTelegramTopicGateway, TopicClient
from .transcription_hydration import TranscriptionHydrationHandler

logger = logging.getLogger(__name__)


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


HEARTBEAT_INTERVAL_S: float = 60.0
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE

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
    coordinator: TelegramDemandCoordinator | None = None
    demand_runtime: _DemandRuntime | None = None
    unix_server: asyncio.AbstractServer | None = None
    handler_manager: EventHandlerManager | None = None
    own_only_context: OwnOnlyContext | None = None
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    background_tasks: set[asyncio.Task[object]] = field(default_factory=set)
    flood_wait_kill_switch_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class _DemandRuntime:
    """Coordinator and daemon-owned dependencies that need shutdown cleanup."""

    coordinator: TelegramDemandCoordinator
    scheduled_reconciler: ScheduledMessageReconciler
    dialog_reconciliation_worker: DialogReconciliationWorker
    dialog_directory: CanonicalDialogDirectory
    message_fact_refresh_deps: MessageFactRefreshDeps
    read_receipt_batch: Callable[[], Awaitable[object]]
    startup_identity: StartupIdentityState


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


async def _run_daemon_lifetime(ctx: _SyncMainContext) -> None:
    """Keep local refresh and heartbeat work alive beside the coordinator."""
    sync_start = time.monotonic()
    while not ctx.shutdown_event.is_set():
        _log_heartbeat(ctx.conn, ctx.client, sync_start)
        if ctx.handler_manager is not None:
            ctx.handler_manager.refresh_synced_dialogs()
        shutdown_wait = asyncio.create_task(ctx.shutdown_event.wait())
        heartbeat_wait = asyncio.create_task(asyncio.sleep(HEARTBEAT_INTERVAL_S))
        try:
            await asyncio.wait((shutdown_wait, heartbeat_wait), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (shutdown_wait, heartbeat_wait):
                if not task.done():
                    task.cancel()


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
        _handle_tracked_task_completion(ctx, t, critical=critical)

    task.add_done_callback(_on_done)
    return task


def _handle_tracked_task_completion(ctx: _SyncMainContext, task: asyncio.Task[object], *, critical: bool) -> None:
    """Record and route an exception from a tracked background task."""
    exc = task.exception() if not task.cancelled() else None
    if exc is None:
        return
    try:
        with ctx.conn:
            record_runtime_observation(
                ctx.conn,
                kind="runtime.task_failed",
                outcome="failed",
                reason_code=type(exc).__name__,
                payload={"task_name": task.get_name()},
            )
    except Exception:
        logger.exception("runtime_event_record_failed kind=runtime.task_failed")
    if critical:
        ctx.api_server._ready = False
        ctx.api_server.startup_detail = f"critical background task failed: {task.get_name()}"
        ctx.shutdown_event.set()
        logger.critical("critical_background_task_failed name=%s error=%s", task.get_name(), exc, exc_info=exc)
    else:
        logger.error("background_task_failed name=%s error=%s", task.get_name(), exc, exc_info=exc)


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


def _message_fact_refresh_policy_from_config(config: McpTelegramConfig) -> MessageFactRefreshPolicy:
    return MessageFactRefreshPolicy(
        interval_seconds=config.scheduling.message_fact_refresh_seconds,
        reaction_max_messages_per_cycle=config.scheduling.message_fact_refresh_reaction_max_messages_per_cycle,
        read_at_max_messages_per_cycle=config.scheduling.message_fact_refresh_read_at_max_messages_per_cycle,
        pause_seconds=config.scheduling.message_fact_refresh_pause_seconds,
        reaction_ttl_seconds=config.freshness.reactions.freshness_ttl_seconds,
        read_at_ttl_seconds=config.freshness.read_receipts.read_at_ttl_seconds,
    )


def _access_probe_policy_from_scheduling(scheduling: SchedulingConfig) -> AccessProbePolicy:
    return AccessProbePolicy(
        interval_seconds=scheduling.access_probe_interval_seconds,
        max_dialogs_per_cycle=scheduling.access_probe_max_dialogs_per_cycle,
        cooldown_seconds=scheduling.access_probe_cooldown_seconds,
        probe_pause_seconds=scheduling.access_probe_pause_seconds,
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
        folder_projection_reproject=lambda: folder_repository.ensure_mute_projection(now=int(time.time())),
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
            full_user_pair_enabled=config.entity_profile.full_user_pair_enabled,
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
    api_server.bind_profile_observer(rpc_admission_observer)
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


async def _acquire_startup_identity_before_updates(
    ctx: _SyncMainContext,
    directory: CanonicalDialogDirectory,
) -> StartupIdentityState:
    """Run the established classified startup identity path while updates wait."""
    startup = StartupIdentityState.begin()

    async def get_self_input_entity(account_id: int) -> object:
        return await ctx.client.get_input_entity(account_id)

    async def get_full_self_user(input_user: object) -> object:
        return await ctx.client(GetFullUserRequest(id=cast(TypeInputUser, input_user)))

    def publish(profile: object, own_only_context: OwnOnlyContext) -> None:
        directory.bind_account_id(own_only_context.account_id)
        _publish_startup_identity(ctx, profile, own_only_context)

    adapter = SelfProfileMaintenanceDemandAdapter(
        SelfProfileMaintenanceDependencies(
            cadence=ctx.self_profile_cadence,
            get_me=ctx.client.get_me,
            update_profile=lambda profile: _update_self_profile(ctx.api_server, cast(_MeLike, profile)),
            startup=startup,
            get_input_entity=get_self_input_entity,
            get_full_user=get_full_self_user,
            publish_startup_identity=publish,
        )
    )
    limit = demand_contract(DemandKind.SELF_PROFILE_MAINTENANCE).max_rpc_attempts_per_slice
    if limit is None:
        raise RuntimeError("startup identity has no RPC attempt bound")
    while startup.pending:
        if ctx.shutdown_event.is_set():
            raise asyncio.CancelledError
        try:
            await adapter.run_slice(RpcAttemptBudget(limit))
        except TelegramRpcAdmissionDeferred as exc:
            if await sleep_through_flood(ctx.shutdown_event, exc.retry_after_seconds or 1):
                raise asyncio.CancelledError from None
        except TelegramRpcThrottled as exc:
            if exc.latched:
                raise
            if await sleep_through_flood(ctx.shutdown_event, exc.retry_after_seconds or 1):
                raise asyncio.CancelledError from None
        except RpcAdmissionClosedError:
            raise
        else:
            await asyncio.sleep(0)
    startup.result()
    return startup


async def _wait_for_startup_identity(
    startup: StartupIdentityState,
    shutdown_event: asyncio.Event,
) -> StartupIdentityResult:
    """Wait for coordinator publication, shutdown, or the contract-derived bound."""
    settled_wait = asyncio.create_task(startup.done_event.wait())
    shutdown_wait = asyncio.create_task(shutdown_event.wait())
    expiry_wait = asyncio.create_task(asyncio.sleep(startup.remaining(time.time())))
    try:
        await asyncio.wait(
            (settled_wait, shutdown_wait, expiry_wait),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in (settled_wait, shutdown_wait, expiry_wait):
            if not task.done():
                task.cancel()

    if startup.done_event.is_set():
        return startup.result()
    if shutdown_event.is_set():
        raise asyncio.CancelledError
    startup.fail("startup identity deadline expired")
    return startup.result()


async def _prime_runtime(ctx: _SyncMainContext) -> None:
    """Wait for coordinator-owned startup identity before enabling readers."""
    if ctx.coordinator is None or ctx.demand_runtime is None:
        raise RuntimeError("demand runtime is unavailable")
    ctx.api_server.startup_detail = "fetching account info"
    _ = ctx.api_server.startup_detail

    ctx.coordinator.offer(DemandKind.SELF_PROFILE_MAINTENANCE)
    identity = await _wait_for_startup_identity(ctx.demand_runtime.startup_identity, ctx.shutdown_event)
    assert ctx.api_server.self_id is not None
    assert ctx.own_only_context == identity.own_only_context
    # The directory's account fence is durable and must settle before event
    # ownership and API readiness can expose the authenticated account.
    ctx.demand_runtime.dialog_directory.bind_account_id(ctx.api_server.self_id)
    assert ctx.handler_manager is not None
    ctx.handler_manager.set_self_id(ctx.api_server.self_id)
    ensure_own_only_schema(ctx.conn)
    logger.info("daemon self_id cached: %s", ctx.api_server.self_id)

    ctx.api_server.startup_detail = "refreshing Telegram folders"
    ctx.coordinator.offer(DemandKind.FOLDER_SNAPSHOT)

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


def _offer_startup_demands(ctx: _SyncMainContext) -> None:
    """Wake all durable startup slices through the process-wide coordinator."""
    if ctx.coordinator is None:
        raise RuntimeError("demand coordinator is unavailable")
    for kind in (
        DemandKind.FULL_SYNC_DM_ENROLLMENT,
        DemandKind.DIALOG_BOOTSTRAP,
        DemandKind.FULL_SYNC_PAGE,
        DemandKind.READ_RECEIPT_BATCH,
    ):
        ctx.coordinator.offer(kind)


def capture_auth_scope(profile: object, client: object) -> TelegramAuthScope | None:
    """Read account and primary permanent-session identity without an RPC."""
    account_id = getattr(profile, "id", None)
    session = getattr(client, "session", None)
    dc_id = getattr(session, "dc_id", None)
    auth_key = getattr(session, "auth_key", None)
    auth_key_id = getattr(auth_key, "key_id", None)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (account_id, dc_id, auth_key_id)
    ):
        return None
    account_id = cast(int, account_id)
    dc_id = cast(int, dc_id)
    auth_key_id = cast(int, auth_key_id)
    try:
        return TelegramAuthScope(
            version=AUTH_SCOPE_VERSION,
            account_id=account_id,
            dc_id=dc_id,
            auth_key_id=auth_key_id,
        )
    except ValueError:
        return None


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
    api_server._publish_auth_scope(capture_auth_scope(me, api_server._client))


def _publish_startup_identity(ctx: _SyncMainContext, profile: object, own_only_context: OwnOnlyContext) -> None:
    """Publish all startup identity facts before waking the readiness waiter."""
    _update_self_profile(ctx.api_server, cast(_MeLike, profile))
    ctx.own_only_context = own_only_context


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


def _build_demand_runtime(
    ctx: _SyncMainContext,
    full_sync_worker: FullSyncWorker,
    delta_sync_worker: DeltaSyncWorker,
    dialog_directory: CanonicalDialogDirectory,
    startup_identity: StartupIdentityState,
) -> _DemandRuntime:
    """Build the exhaustive durable coordinator after event handlers exist."""
    if ctx.handler_manager is None:
        raise RuntimeError("event handlers must be constructed before demand composition")
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

    async def get_self_input_entity(account_id: int) -> object:
        return await ctx.client.get_input_entity(account_id)

    async def get_full_self_user(input_user: object) -> object:
        return await ctx.client(GetFullUserRequest(id=cast(TypeInputUser, input_user)))

    def publish_startup_identity(profile: object, own_only_context: OwnOnlyContext) -> None:
        _publish_startup_identity(ctx, profile, own_only_context)
        scheduled_reconciler._own_only_context = own_only_context
        scheduled_reconciler._resolved_context = own_only_context

    try:
        dependencies = DemandCompositionDependencies(
            client=cast(DemandCompositionClient, ctx.client),
            conn=ctx.conn,
            db_path=ctx.db_path,
            shutdown_event=ctx.shutdown_event,
            full_sync_worker=full_sync_worker,
            delta_sync_worker=delta_sync_worker,
            dm_gap_scanner=cast(DmGapScanPage, ctx.handler_manager),
            dialog_directory=dialog_directory,
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
            startup_identity=startup_identity,
            get_self_input_entity=get_self_input_entity,
            get_full_self_user=get_full_self_user,
            publish_startup_identity=publish_startup_identity,
            startup_detail_setter=lambda detail: setattr(ctx.api_server, "startup_detail", detail),
        )
        coordinator = build_durable_coordinator(dependencies, observer=ctx.rpc_admission_observer)
    except BaseException:
        message_fact_refresh_deps.conn.close()
        raise
    return _DemandRuntime(
        coordinator=coordinator,
        scheduled_reconciler=scheduled_reconciler,
        dialog_reconciliation_worker=dialog_reconciliation_worker,
        dialog_directory=dialog_directory,
        message_fact_refresh_deps=message_fact_refresh_deps,
        read_receipt_batch=read_receipt_batch,
        startup_identity=startup_identity,
    )


def _ensure_demand_runtime(
    ctx: _SyncMainContext,
    full_sync_worker: FullSyncWorker,
    delta_sync_worker: DeltaSyncWorker,
    dialog_directory: CanonicalDialogDirectory,
    startup_identity: StartupIdentityState,
) -> _DemandRuntime:
    """Install and wire the single process-wide durable coordinator."""
    if ctx.demand_runtime is not None:
        return ctx.demand_runtime
    demand_runtime = _build_demand_runtime(
        ctx,
        full_sync_worker,
        delta_sync_worker,
        dialog_directory,
        startup_identity,
    )
    ctx.demand_runtime = demand_runtime
    ctx.coordinator = demand_runtime.coordinator
    ctx.api_server.bind_demand_sink(demand_runtime.coordinator)
    if ctx.handler_manager is not None:
        ctx.handler_manager.bind_demand_sink(demand_runtime.coordinator)
    entity_service = ctx.api_server._get_entity_info_service()
    entity_service.bind_demand_sink(demand_runtime.coordinator)
    for producer in (ctx.fact_hydration_worker, ctx.folder_projection_worker):
        bind = getattr(producer, "bind_demand_sink", None)
        if callable(bind):
            bind(demand_runtime.coordinator)
    _create_tracked_task(
        ctx,
        demand_runtime.coordinator.run(),
        name="telegram_demand_coordinator",
        critical=True,
    )
    return demand_runtime


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
    if ctx.coordinator is not None:
        ctx.coordinator.shutdown()
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
    counts = _runtime_observation_loss_counts(sink)
    if not any(counts):
        return
    queue_full_drops, _shutdown_drops, _startup_drops, _rejected_submissions, writer_failures = counts
    with ctx.conn:
        try:
            record_runtime_observation(
                ctx.conn,
                kind="runtime.telemetry_loss",
                outcome="loss",
                payload={
                    "queue_full_drops": queue_full_drops,
                    "shutdown_grace_drops": counts[1],
                    "startup_drops": counts[2],
                    "rejected_submissions": counts[3],
                    "writer_failures": writer_failures,
                },
            )
        except sqlite3.Error:
            logger.debug("runtime_observation_loss_event_failed")
        ctx.conn.executemany(
            "INSERT OR REPLACE INTO daemon_state(key,value) VALUES (?,?)",
            (
                ("runtime_observations_last_loss_ms", str(int(time.time() * 1000))),
                ("runtime_observations_last_queue_full_drops", str(queue_full_drops)),
                ("runtime_observations_last_writer_failures", str(writer_failures)),
            ),
        )


def _runtime_observation_loss_counts(sink: object) -> tuple[int, int, int, int, int]:
    """Read sink loss counters once, tolerating sinks from older runtimes."""
    return (
        int(getattr(sink, "queue_full_drops", 0) or 0),
        int(getattr(sink, "shutdown_grace_drops", 0) or 0),
        int(getattr(sink, "startup_drops", 0) or 0),
        int(getattr(sink, "rejected_submissions", 0) or 0),
        int(getattr(sink, "permanent_failures", 0) or 0),
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

        update_barrier = UpdateProcessingBarrier(closed=True)
        input_peer_resolver = cast(InputPeerResolver, partial(resolve_input_peer, cast(ActivityClient, ctx.client)))
        ctx.handler_manager = EventHandlerManager(
            ctx.client,
            ctx.conn,
            ctx.shutdown_event,
            input_peer_resolver,
            update_barrier=update_barrier,
        )
        ctx.handler_manager.register()
        logger.info("event handlers registered behind startup account barrier")

        if not await _connect_telegram(ctx):
            return

        dialog_directory = CanonicalDialogDirectory(
            ctx.client,
            ctx.db_path,
            ctx.shutdown_event,
            startup_detail_setter=lambda detail: setattr(ctx.api_server, "startup_detail", detail),
        )
        startup_identity = await _acquire_startup_identity_before_updates(ctx, dialog_directory)
        assert ctx.api_server.self_id is not None
        ctx.handler_manager.set_self_id(ctx.api_server.self_id)

        delta_worker = DeltaSyncWorker(cast(_DeltaSyncClient, ctx.client), ctx.conn, ctx.shutdown_event)
        worker = FullSyncWorker(ctx.client, ctx.conn, ctx.shutdown_event)
        _ensure_demand_runtime(ctx, worker, delta_worker, dialog_directory, startup_identity)
        update_barrier.open()

        # Keep the transition watcher live for later reconnects. Initial
        # catch-up is already retained by Telethon behind the startup barrier.
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

        await _prime_runtime(ctx)
        _offer_startup_demands(ctx)
        await _run_daemon_lifetime(ctx)
    finally:
        await _shutdown_sync_main_context(ctx)


_SYNC_MAIN = sync_main
