"""Sync daemon — long-running process that exclusively owns the TelegramClient.

Started via ``mcp-telegram sync``. Connects to Telegram, ensures sync.db schema,
bootstraps DM dialogs, then runs FullSyncWorker in a tight batch loop with
periodic heartbeat logging and clean SIGTERM handling.

Architecture:
- sync-daemon is the sole owner of TelegramClient — connects once, holds it.
- MCP server runs separately with disable_telegram_session() active and reads
  sync.db via open_sync_db_reader(); it never calls client.connect().
- SIGTERM triggers shutdown_event (set by register_shutdown_handler), which
  checkpoints WAL and closes the DB connection before the daemon disconnects.

Event handlers:
- EventHandlerManager is registered BEFORE FullSyncWorker starts so no
  real-time events are missed during initial bulk fetch.  INSERT OR REPLACE
  handles any overlap between real-time and bulk paths idempotently.
- synced_dialogs set is refreshed every heartbeat so newly enrolled dialogs
  are picked up within one interval without re-registering handlers.
- Weekly gap scan detects tombstoned DM messages that MTProto delete events
  cannot report.

Delta catch-up:
- connect() called with catch_up=True — Telethon replays missed updates via PTS
  on reconnect.
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
import os
import sqlite3
import time
from collections.abc import Coroutine, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import FloodWaitError, RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetPeerDialogsRequest  # type: ignore[import-untyped]
from telethon.tl.functions.users import GetFullUserRequest  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    InputDialogPeer,
    TypeInputDialogPeer,
    TypeInputPeer,
    TypeInputUser,
)

from .activity_cold_backfill import run_cold_backfill_loop
from .activity_hot_sweep import run_hot_sweep_loop
from .activity_sync import _ActivityClient, run_activity_sync_loop
from .config import load_config
from .daemon_api import DaemonApiPolicy, DaemonAPIServer, _DaemonClientLike
from .delta_sync import DeltaSyncWorker, _DeltaSyncClient, run_access_probe_loop
from .dialog_sync import DialogsBootstrapWorker, run_reconciliation_loop
from .event_handlers import EventHandlerManager
from .feedback_db import ensure_feedback_schema
from .flood import (
    flood_seconds,
    install_telethon_flood_wait_metrics_filter,
    maybe_log_flood_wait_rollup,
    sleep_through_flood,
)
from .fts import backfill_fts_index
from .messages.sqlite_repository import insert_messages_with_fts
from .messages.telegram_adapter import extract_message_row
from .own_only import OwnOnlyContext, ensure_own_only_schema
from .reactions.refresh import ReactionFreshener
from .reactions.sqlite_repository import SQLiteReactionSnapshotRepository
from .reactions.telegram_adapter import TelethonTelegramReactionGateway
from .read_state import apply_read_cursor
from .scheduled_messages import run_scheduled_reconciliation_loop
from .state import StatePaths, ensure_private_state_dir
from .sync_db import (
    _open_sync_db,
    ensure_sync_schema,
    migrate_legacy_databases,
    register_shutdown_handler,
)
from .sync_worker import FullSyncWorker
from .telegram import create_client

logger = logging.getLogger(__name__)


class _DaemonClient(Protocol):
    def add_event_handler(self, _callback: object, _event: object) -> None: ...

    def remove_event_handler(self, _callback: object) -> None: ...

    def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_me(self) -> object: ...

    async def get_input_entity(self, _dialog_id: int) -> object: ...

    async def get_entity(self, _dialog_id: int) -> object: ...

    async def get_messages(self, *_args: object, **_kwargs: object) -> object: ...

    async def __call__(self, _request: object) -> object: ...


class _ReadPositionDialogLike(Protocol):
    peer: object
    read_inbox_max_id: int | None
    read_outbox_max_id: int | None


class _ReadPositionsResultLike(Protocol):
    dialogs: Sequence[_ReadPositionDialogLike]


class _MessagesTotalLike(Protocol):
    total: int | None


class _MeLike(Protocol):
    id: int


@dataclass(frozen=True, slots=True)
class DaemonHistoryPacing:
    backfill_skip_s: float = 1.0


@dataclass(frozen=True, slots=True)
class DaemonReadPacing:
    batch_s: float = 1.5


@dataclass(frozen=True, slots=True)
class DaemonPacing:
    history: DaemonHistoryPacing = DaemonHistoryPacing()
    read: DaemonReadPacing = DaemonReadPacing()


_PACING = DaemonPacing()


HEARTBEAT_INTERVAL_S: float = 60.0
GAP_SCAN_INTERVAL_S: float = 7 * 24 * 3600.0
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE

# Bootstrap sweep batch size for GetPeerDialogsRequest. Telethon's per-call
# limit is 100; we intentionally stay in the 10-20 range to avoid the
# FloodWait burst that broke the 260416-ifp incident. 15 is the sweet spot
# documented in Plan 39.3-02 (R4) and the _initialize_read_positions docstring.
# Paired with a 1.5s inter-batch pause in the loop body.
_BOOTSTRAP_BATCH_SIZE: int = 15
_UNSUPPORTED_TRANSCRIPTION_BACKFILL_BATCH_SIZE: int = 25
_UNSUPPORTED_TRANSCRIPTION_BACKFILL_LIMIT: int = 500
_UNSUPPORTED_MEDIA_DESCRIPTIONS = ("MessageMediaUnsupported", "[неподдерживаемый тип]")

_BACKFILL_TOTAL_MESSAGES_SKIP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RPCError,
    sqlite3.DatabaseError,
    Exception,
)

_SELECT_NULL_TOTAL_SQL = "SELECT dialog_id FROM synced_dialogs WHERE total_messages IS NULL AND status != 'not_synced'"

_UPDATE_TOTAL_SQL = "UPDATE synced_dialogs SET total_messages = ? WHERE dialog_id = ?"

_SELECT_NULL_READ_CURSORS_SQL = (
    # Phase 39.3-02: picks up dialogs with EITHER cursor NULL. Post-v12
    # migration, every existing synced row has read_outbox_max_id = NULL, so
    # this re-bootstraps all of them in batched GetPeerDialogsRequest calls.
    "SELECT dialog_id FROM synced_dialogs "
    "WHERE (read_inbox_max_id IS NULL OR read_outbox_max_id IS NULL) "
    "AND status = 'synced'"
)

_SELECT_BLANK_UNSUPPORTED_MESSAGES_SQL = (
    "SELECT dialog_id, message_id FROM messages "
    "WHERE COALESCE(text, '') = '' AND media_description IN (?, ?) "
    "ORDER BY dialog_id, message_id "
    "LIMIT ?"
)


@dataclass(slots=True)
class _SyncLoopState:
    sync_start: float
    last_heartbeat: float
    last_gap_scan: float
    last_hb_msg_count: int
    last_hb_mono: float
    was_idle: bool = False


@dataclass(slots=True)
class _SyncMainContext:
    db_path: Path
    conn: sqlite3.Connection
    feedback_conn: sqlite3.Connection
    shutdown_event: asyncio.Event
    client: _DaemonClient
    api_server: DaemonAPIServer
    socket_path: Path
    unix_server: asyncio.AbstractServer | None = None
    handler_manager: EventHandlerManager | None = None
    own_only_context: OwnOnlyContext | None = None
    background_tasks: set[asyncio.Task[object]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _BackfillTotalDialogResult:
    filled: int
    pause_after: bool
    stop: bool = False


async def _backfill_blank_unsupported_messages(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> int:
    """Re-fetch blank unsupported media rows and materialize text when Telegram exposes it."""
    rows = cast(
        list[tuple[int, int]],
        conn.execute(
            _SELECT_BLANK_UNSUPPORTED_MESSAGES_SQL,
            (*_UNSUPPORTED_MEDIA_DESCRIPTIONS, _UNSUPPORTED_TRANSCRIPTION_BACKFILL_LIMIT),
        ).fetchall(),
    )
    if not rows:
        logger.info("backfill_blank_unsupported_messages — no rows, skipping")
        return 0

    filled = 0
    for dialog_id, message_ids in _group_message_ids_by_dialog(rows).items():
        if shutdown_event.is_set():
            break
        for chunk in _chunk_message_ids(message_ids):
            if shutdown_event.is_set():
                break
            result = await _backfill_blank_unsupported_chunk(client, conn, shutdown_event, dialog_id, chunk)
            filled += result.filled
            if result.stop:
                logger.info("backfill_blank_unsupported_messages filled=%d/%d", filled, len(rows))
                return filled
            if result.pause_after and not await _sleep_between_backfill_total_dialogs(shutdown_event):
                logger.info("backfill_blank_unsupported_messages filled=%d/%d", filled, len(rows))
                return filled

    logger.info("backfill_blank_unsupported_messages filled=%d/%d", filled, len(rows))
    return filled


def _group_message_ids_by_dialog(rows: Sequence[tuple[int, int]]) -> dict[int, list[int]]:
    grouped: dict[int, list[int]] = {}
    for dialog_id, message_id in rows:
        grouped.setdefault(dialog_id, []).append(message_id)
    return grouped


def _chunk_message_ids(message_ids: Sequence[int]) -> Iterator[list[int]]:
    for index in range(0, len(message_ids), _UNSUPPORTED_TRANSCRIPTION_BACKFILL_BATCH_SIZE):
        yield list(message_ids[index : index + _UNSUPPORTED_TRANSCRIPTION_BACKFILL_BATCH_SIZE])


async def _backfill_blank_unsupported_chunk(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    dialog_id: int,
    message_ids: Sequence[int],
) -> _BackfillTotalDialogResult:
    try:
        fetched = cast(Sequence[object], await client.get_messages(entity=dialog_id, ids=list(message_ids)))
    except FloodWaitError as exc:
        logger.warning("backfill_blank_unsupported flood_wait dialog_id=%d seconds=%d", dialog_id, exc.seconds)
        if await sleep_through_flood(shutdown_event, flood_seconds(exc)):
            return _BackfillTotalDialogResult(filled=0, pause_after=False, stop=True)
        return _BackfillTotalDialogResult(filled=0, pause_after=False)
    except _BACKFILL_TOTAL_MESSAGES_SKIP_EXCEPTIONS as exc:
        logger.debug("backfill_blank_unsupported skip dialog_id=%d error=%s", dialog_id, exc)
        return _BackfillTotalDialogResult(filled=0, pause_after=True)

    extracted = [extract_message_row(dialog_id, msg) for msg in fetched if msg is not None]
    materialized = [item for item in extracted if item.message.text]
    if not materialized:
        return _BackfillTotalDialogResult(filled=0, pause_after=True)

    with conn:
        insert_messages_with_fts(conn, materialized)
    return _BackfillTotalDialogResult(filled=len(materialized), pause_after=True)


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
            conn.execute(_UPDATE_TOTAL_SQL, (total, dialog_id))
            conn.commit()
            return _BackfillTotalDialogResult(filled=1, pause_after=True)
        return _BackfillTotalDialogResult(filled=0, pause_after=True)
    except FloodWaitError as exc:
        logger.warning("backfill_total flood_wait dialog_id=%d seconds=%d", dialog_id, exc.seconds)
        if await sleep_through_flood(shutdown_event, flood_seconds(exc)):
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


async def _initialize_read_positions(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> int:
    """One-time sweep to populate BOTH read cursors for synced dialogs.

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

    Batch size 15, 1.5s inter-batch pause (10-20 range to avoid
    FloodWait burst that broke 260416-ifp). Runs once at daemon startup
    in the background.

    All writes use monotonic UPDATE — ``MAX(COALESCE(existing, 0), incoming)``
    via the shared primitive — so a live MessageRead / outbox-read event
    that arrives during the bootstrap window cannot be overwritten by a
    stale bootstrap reply (designed race safety, not accidental).
    """
    rows = cast(list[tuple[int]], conn.execute(_SELECT_NULL_READ_CURSORS_SQL).fetchall())
    if not rows:
        logger.info("initialize_read_positions — no NULL rows, skipping")
        return 0

    dialog_ids = [dialog_id for (dialog_id,) in rows]
    filled = 0

    for i in range(0, len(dialog_ids), _BOOTSTRAP_BATCH_SIZE):
        if shutdown_event.is_set():
            break
        batch_ids = dialog_ids[i : i + _BOOTSTRAP_BATCH_SIZE]
        input_peers = await _build_read_position_input_peers(client, batch_ids)
        if not input_peers:
            if not await _sleep_read_pos_batch(shutdown_event):
                break
            continue

        try:
            result = cast(_ReadPositionsResultLike, await client(GetPeerDialogsRequest(peers=input_peers)))
            filled += _apply_read_positions_from_dialogs(conn, result)
            conn.commit()
        except FloodWaitError as exc:
            logger.warning("read_pos_bootstrap flood_wait seconds=%d", exc.seconds)
            if await sleep_through_flood(shutdown_event, flood_seconds(exc)):
                return filled
        except (RPCError, sqlite3.DatabaseError) as exc:
            logger.debug("read_pos_bootstrap batch_failed error=%s", exc)

        if not await _sleep_read_pos_batch(shutdown_event):
            break

    logger.info("initialize_read_positions filled=%d/%d", filled, len(dialog_ids))
    return filled


async def _build_read_position_input_peers(client: _DaemonClient, batch_ids: list[int]) -> list[TypeInputDialogPeer]:
    input_peers: list[TypeInputDialogPeer] = []
    for dialog_id in batch_ids:
        try:
            peer = cast(TypeInputPeer, await client.get_input_entity(dialog_id))
            input_peers.append(InputDialogPeer(peer=peer))
        except (RPCError, TypeError, ValueError) as exc:
            logger.debug("read_pos_bootstrap skip dialog_id=%d error=%s", dialog_id, exc)
    return input_peers


def _apply_read_positions_from_dialogs(conn: sqlite3.Connection, result: _ReadPositionsResultLike) -> int:
    """Apply read cursors from a GetPeerDialogsRequest result."""
    filled = 0
    for dialog in result.dialogs:
        chat_id = int(cast(int, telethon_utils.get_peer_id(dialog.peer)))
        # D-03 LOCKED: None → skip (preserve NULL). NEVER fold
        # None → 0; that would lie with [all read] during the
        # bootstrap window. The DB cursor stays NULL and Plan 03
        # renders [unknown (sync pending)]. 0 is a legitimate
        # distinct value (peer/me has read nothing) — writes 0.
        inbox_max = cast(int | None, getattr(dialog, "read_inbox_max_id", None))
        outbox_max = cast(int | None, getattr(dialog, "read_outbox_max_id", None))
        wrote_any = False
        if inbox_max is not None and apply_read_cursor(conn, chat_id, "inbox", inbox_max) > 0:
            # Monotonic via shared primitive — see read_state.py.
            wrote_any = True
        if outbox_max is not None and apply_read_cursor(conn, chat_id, "outbox", outbox_max) > 0:
            wrote_any = True
        if wrote_any:
            filled += 1
    return filled


async def _sleep_read_pos_batch(shutdown_event: asyncio.Event) -> bool:
    # Inter-batch pause: SIGTERM-responsive
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=_PACING.read.batch_s)
        return False
    except TimeoutError:
        return True


# ---------------------------------------------------------------------------
# Heartbeat — standalone for testability (no nonlocal / closure)
# ---------------------------------------------------------------------------


def _fetch_heartbeat_stats(conn: sqlite3.Connection) -> tuple[dict[str, int], int]:
    stats_rows = cast(
        list[tuple[str, int]],
        conn.execute("SELECT status, COUNT(*) FROM synced_dialogs GROUP BY status").fetchall(),
    )
    stats = dict(stats_rows)
    msg_count_row = cast(tuple[int], conn.execute("SELECT COUNT(*) FROM messages").fetchone())
    return stats, int(msg_count_row[0])


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
    prev_msg_count: int,
    prev_mono: float,
) -> tuple[int, float]:
    """Log heartbeat with sync stats, interval-based rate, and ETA from sync.db.

    Rate is computed over the heartbeat interval (since the last call), not
    since daemon startup — so an idle daemon shows 0msg/s instead of a stale
    decaying lifetime average.

    Returns (current_msg_count, current_mono) for the caller to feed into the
    next invocation.
    """
    try:
        stats, msg_count = _fetch_heartbeat_stats(conn)
    except sqlite3.DatabaseError:
        logger.warning("heartbeat_stats_failed", exc_info=True)
        stats = {}
        msg_count = 0
    synced = int(stats.get("synced", 0) or 0)
    syncing = int(stats.get("syncing", 0) or 0)
    total = synced + syncing + int(stats.get("not_synced", 0) or 0)

    now_mono = time.monotonic()
    interval = now_mono - prev_mono
    delta = max(0, msg_count - int(prev_msg_count or 0))
    rate = delta / interval if interval > 0 else 0.0

    logger.info(
        "heartbeat — connected=%s dialogs=%d/%d messages=%d rate=%.0fmsg/s%s",
        client.is_connected(),
        synced,
        total,
        msg_count,
        rate,
        _format_heartbeat_eta(sync_start, synced, total, now_mono),
    )
    maybe_log_flood_wait_rollup(logger)
    return msg_count, now_mono


# ---------------------------------------------------------------------------
# Sync loop — batch processing + idle wait
# ---------------------------------------------------------------------------


async def _maybe_heartbeat_and_gap_scan(
    conn: sqlite3.Connection,
    client: _DaemonClient,
    handler_manager: EventHandlerManager,
    state: _SyncLoopState,
) -> _SyncLoopState:
    """Run heartbeat and gap scan if their intervals have elapsed.

    Returns the updated loop state.
    """
    now_mono = time.monotonic()

    if now_mono - state.last_heartbeat >= HEARTBEAT_INTERVAL_S:
        state.last_hb_msg_count, state.last_hb_mono = _log_heartbeat(
            conn,
            client,
            state.sync_start,
            state.last_hb_msg_count,
            state.last_hb_mono,
        )
        handler_manager.refresh_synced_dialogs()
        state.last_heartbeat = now_mono

    if now_mono - state.last_gap_scan >= GAP_SCAN_INTERVAL_S:
        deleted_count = await handler_manager.run_dm_gap_scan()
        logger.info("gap_scan complete — marked_deleted=%d", deleted_count)
        state.last_gap_scan = now_mono

    return state


async def _run_sync_loop(
    worker: FullSyncWorker,
    handler_manager: EventHandlerManager,
    shutdown_event: asyncio.Event,
    conn: sqlite3.Connection,
    client: _DaemonClient,
) -> None:
    """Run the batch-sync loop with periodic heartbeat and gap scan."""
    sync_start = time.monotonic()
    try:
        last_hb_row = cast(tuple[int], conn.execute("SELECT COUNT(*) FROM messages").fetchone())
        last_hb_msg_count = int(last_hb_row[0])
    except sqlite3.DatabaseError:
        last_hb_msg_count = 0
    state = _SyncLoopState(
        sync_start=sync_start,
        last_heartbeat=sync_start,
        last_gap_scan=sync_start,
        last_hb_msg_count=last_hb_msg_count,
        last_hb_mono=sync_start,
    )

    while not shutdown_event.is_set():
        all_synced = await worker.process_one_batch()
        await asyncio.sleep(0)

        state = await _maybe_heartbeat_and_gap_scan(
            conn,
            client,
            handler_manager,
            state,
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
                )
        elif state.was_idle:
            logger.info("sync_resume — work appeared, exiting idle")
            state.was_idle = False


def _create_tracked_task(
    ctx: _SyncMainContext,
    coro: Coroutine[object, object, object],
    *,
    name: str | None = None,
) -> asyncio.Task[object]:
    """Create an asyncio task and track it for shutdown cancellation."""
    task = asyncio.create_task(coro, name=name)
    ctx.background_tasks.add(task)

    def _on_done(t: asyncio.Task[object]) -> None:
        ctx.background_tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            logger.error("background_task_failed name=%s error=%s", t.get_name(), exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


async def _build_sync_main_context() -> _SyncMainContext:
    config = load_config()
    state_paths = StatePaths.from_state_dir(ensure_private_state_dir(config.state.dir))
    db_path = state_paths.sync_db_path
    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)
    migrate_legacy_databases(
        conn,
        state_paths.state_dir,
        telemetry_retention_ttl_seconds=config.telemetry.retention_ttl_seconds,
    )

    # Open feedback.db before registering the shutdown handler so the SIGTERM
    # handler can checkpoint it.  feedback_conn is opened on the asyncio thread
    # (sync_main coroutine) — the same thread the SIGTERM handler runs on via
    # loop.add_signal_handler — so no cross-thread SQLite sharing occurs.
    feedback_db_path = state_paths.feedback_db_path
    feedback_conn = ensure_feedback_schema(feedback_db_path)
    logger.info("feedback.db ready at %s", feedback_db_path)

    loop = asyncio.get_running_loop()
    shutdown_event = register_shutdown_handler(conn, loop, feedback_conn=feedback_conn)

    client = cast(_DaemonClient, create_client(catch_up=True))
    reaction_freshener = ReactionFreshener(
        SQLiteReactionSnapshotRepository(conn),
        TelethonTelegramReactionGateway(client),
        freshness_ttl_seconds=config.freshness.reactions.freshness_ttl_seconds,
        log=logger,
    )
    api_server = DaemonAPIServer(
        conn,
        cast(_DaemonClientLike, client),
        shutdown_event,
        feedback_conn,
        db_path,
        reaction_freshener=reaction_freshener,
        policy=DaemonApiPolicy(
            read_at_ttl_seconds=config.freshness.read_receipts.read_at_ttl_seconds,
            entity_detail_ttl_seconds=config.freshness.entities.detail_ttl_seconds,
            user_directory_ttl_seconds=config.freshness.entities.user_directory_ttl_seconds,
            group_directory_ttl_seconds=config.freshness.entities.group_directory_ttl_seconds,
            resolver_enrichment_ttl_seconds=config.freshness.entities.resolver_enrichment_ttl_seconds,
            telemetry_retention_ttl_seconds=config.telemetry.retention_ttl_seconds,
        ),
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
    return _SyncMainContext(
        db_path=db_path,
        conn=conn,
        feedback_conn=feedback_conn,
        shutdown_event=shutdown_event,
        client=client,
        api_server=api_server,
        socket_path=socket_path,
        unix_server=unix_server,
    )


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


async def _load_own_only_context(client: _DaemonClient, account_id: int) -> OwnOnlyContext:
    context = OwnOnlyContext(account_id=account_id)
    try:
        input_user = cast(TypeInputUser, await client.get_input_entity(account_id))
        full_result = await client(GetFullUserRequest(id=input_user))
        user_full = getattr(full_result, "full_user", None)
        personal_channel_id = getattr(user_full, "personal_channel_id", None)
        if isinstance(personal_channel_id, int) and personal_channel_id > 0:
            return OwnOnlyContext(account_id=account_id, personal_channel_id=personal_channel_id)
    except (FloodWaitError, RPCError, TypeError, AttributeError, ValueError) as exc:
        logger.warning("own_only_account_facts_unavailable error=%s", exc)
    return context


async def _prime_runtime(ctx: _SyncMainContext) -> None:
    # Phase 39.1: cache authenticated user id once at startup so query-build
    # paths (Plan 39.1-02) can bind it as a SQL parameter without calling
    # Telethon per request. Failure propagates — daemon cannot serve reads
    # correctly without a stable self_id.
    ctx.api_server.startup_detail = "fetching account info"
    _ = ctx.api_server.startup_detail
    me = cast(_MeLike, await ctx.client.get_me())
    ctx.api_server.self_id = int(me.id)
    ctx.own_only_context = await _load_own_only_context(ctx.client, ctx.api_server.self_id)
    ensure_own_only_schema(ctx.conn)
    logger.info("daemon self_id cached: %s", ctx.api_server.self_id)

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


async def _start_bootstrap_background_tasks(
    ctx: _SyncMainContext,
    worker: FullSyncWorker,
    delta_worker: DeltaSyncWorker,
) -> None:
    assert ctx.handler_manager is not None

    # Keep the worker alive only as long as the sync loop needs it.
    ctx.api_server.startup_detail = "running delta catch-up"
    _ = ctx.api_server.startup_detail
    delta_new = await delta_worker.run_delta_catch_up()
    logger.info("delta_catch_up=%d new messages from gap-fill", delta_new)

    ctx.api_server.startup_detail = "bootstrapping DMs"
    _ = ctx.api_server.startup_detail
    enrolled = await worker.bootstrap_dms()
    logger.info("dm_bootstrap complete — enrolled=%d", enrolled)

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
    task_specs: list[tuple[Coroutine[object, object, object], str]] = [
        (
            DialogsBootstrapWorker(
                ctx.client,
                ctx.db_path,
                ctx.shutdown_event,
                startup_detail_setter=lambda s: setattr(ctx.api_server, "startup_detail", s),
            ).run(),
            "dialogs_bootstrap_sweep",
        ),
        (_backfill_total_messages(ctx.client, ctx.conn, ctx.shutdown_event), "backfill_total_messages"),
    ]
    for coro, name in task_specs:
        _create_tracked_task(ctx, coro, name=name)


async def _start_followup_background_tasks(
    ctx: _SyncMainContext,
    delta_worker: DeltaSyncWorker,
) -> None:
    activity_client = cast(_ActivityClient, ctx.client)
    delta_client = cast(_DeltaSyncClient, ctx.client)
    _create_tracked_task(
        ctx,
        _backfill_blank_unsupported_messages(ctx.client, ctx.conn, ctx.shutdown_event),
        name="backfill_blank_unsupported_messages",
    )
    _create_tracked_task(
        ctx,
        run_access_probe_loop(delta_client, ctx.conn, ctx.shutdown_event, delta_worker),
        name="access_probe_loop",
    )
    _create_tracked_task(
        ctx, run_activity_sync_loop(activity_client, ctx.conn, ctx.shutdown_event), name="activity_sync_loop"
    )
    _create_tracked_task(
        ctx, run_hot_sweep_loop(activity_client, ctx.conn, ctx.shutdown_event), name="activity_hot_sweep"
    )
    _create_tracked_task(
        ctx, run_cold_backfill_loop(activity_client, ctx.conn, ctx.shutdown_event), name="activity_cold_backfill"
    )
    scheduled_interval = float(os.environ.get("SCHEDULED_RECONCILIATION_SECONDS", "900"))
    _create_tracked_task(
        ctx,
        run_scheduled_reconciliation_loop(
            ctx.client,
            ctx.conn,
            ctx.shutdown_event,
            interval=scheduled_interval,
            own_only_context=ctx.own_only_context,
        ),
        name="scheduled_message_reconciliation",
    )

    # Phase 43 / RECON-01: hourly light pass + daily full pass keeps the
    # `dialogs` snapshot fresh; processes needs_refresh=1 rows written by
    # Phase 42 event handlers and soft-deletes left/kicked dialogs once a day.
    #
    # RECON_HOURLY_SECONDS env var override (43-REVIEWS.md MEDIUM): default is
    # 3600s (1h) for production; setting it to a smaller value (e.g. "30") lets
    # an operator observe a needs_refresh=1 -> 0 transition in seconds during
    # UAT. Daily interval stays at the default 86400s — there is no need for a
    # daily override yet, and the first iteration always runs a full pass
    # regardless of last_full_pass anyway.
    recon_hourly = float(os.environ.get("RECON_HOURLY_SECONDS", "3600"))
    _create_tracked_task(
        ctx,
        run_reconciliation_loop(
            ctx.client,
            ctx.conn,
            ctx.shutdown_event,
            hourly_interval=recon_hourly,
        ),
        name="reconciliation_loop",
    )


async def _shutdown_sync_main_context(ctx: _SyncMainContext) -> None:
    if ctx.unix_server is not None:
        ctx.unix_server.close()
        await ctx.unix_server.wait_closed()
    ctx.socket_path.unlink(missing_ok=True)
    if ctx.handler_manager is not None:
        ctx.handler_manager.unregister()
    # Cancel tracked background tasks
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
    await ctx.client.disconnect()
    try:
        ctx.feedback_conn.close()
    except Exception:
        logger.debug("feedback_conn close error", exc_info=True)
    ctx.conn.close()
    logger.info("sync-daemon stopped")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def sync_main() -> None:
    """Main entry point for the sync daemon process.

    Orchestrates: DB init → FTS backfill → Telegram connect → wire services →
    sync loop → cleanup.
    """
    install_telethon_flood_wait_metrics_filter()
    ctx = await _build_sync_main_context()
    try:
        await _run_fts_backfill(ctx)

        if not await _connect_telegram(ctx):
            return

        await _prime_runtime(ctx)

        ctx.handler_manager = EventHandlerManager(ctx.client, ctx.conn, ctx.shutdown_event)
        ctx.handler_manager.register()
        logger.info("event handlers registered")

        delta_worker = DeltaSyncWorker(cast(_DeltaSyncClient, ctx.client), ctx.conn, ctx.shutdown_event)
        worker = FullSyncWorker(ctx.client, ctx.conn, ctx.shutdown_event)
        await _start_bootstrap_background_tasks(ctx, worker, delta_worker)
        # Must come AFTER handler_manager.register() (startup-ordering invariant):
        # the on_message_read handler must be live before bootstrap starts so no
        # real-time MessageRead events are dropped during the bootstrap window.
        _create_tracked_task(
            ctx,
            _initialize_read_positions(ctx.client, ctx.conn, ctx.shutdown_event),
            name="initialize_read_positions",
        )
        await _start_followup_background_tasks(ctx, delta_worker)
        await _run_sync_loop(worker, ctx.handler_manager, ctx.shutdown_event, ctx.conn, ctx.client)
    finally:
        await _shutdown_sync_main_context(ctx)


_SYNC_MAIN = sync_main
