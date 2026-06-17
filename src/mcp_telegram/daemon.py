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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import FloodWaitError, RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetPeerDialogsRequest  # type: ignore[import-untyped]
from telethon.tl.types import InputDialogPeer  # type: ignore[import-untyped]

from .activity_cold_backfill import run_cold_backfill_loop
from .activity_hot_sweep import run_hot_sweep_loop
from .activity_sync import run_activity_sync_loop
from .daemon_api import DaemonAPIServer
from .daemon_ipc import get_daemon_socket_path
from .delta_sync import DeltaSyncWorker, run_access_probe_loop
from .dialog_sync import DialogsBootstrapWorker, run_reconciliation_loop
from .event_handlers import EventHandlerManager
from .feedback_db import ensure_feedback_schema, get_feedback_db_path
from .flood import flood_seconds, sleep_through_flood
from .fts import backfill_fts_index
from .read_state import apply_read_cursor
from .sync_db import (
    _open_sync_db,
    ensure_sync_schema,
    get_sync_db_path,
    migrate_legacy_databases,
    register_shutdown_handler,
)
from .sync_worker import FullSyncWorker
from .telegram import create_client

logger = logging.getLogger(__name__)

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
    client: Any
    api_server: DaemonAPIServer
    socket_path: Path
    unix_server: asyncio.AbstractServer | None = None
    handler_manager: EventHandlerManager | None = None
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)


async def _backfill_total_messages(
    client: Any,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> int:
    """One-time sweep to populate total_messages for dialogs with NULL."""
    rows = conn.execute(_SELECT_NULL_TOTAL_SQL).fetchall()
    if not rows:
        logger.info("backfill_total_messages — no NULL rows, skipping")
        return 0

    filled = 0
    for (dialog_id,) in rows:
        if shutdown_event.is_set():
            break
        try:
            result = await client.get_messages(entity=dialog_id, limit=1)
            total = getattr(result, "total", None)
            if total is not None:
                conn.execute(_UPDATE_TOTAL_SQL, (total, dialog_id))
                conn.commit()
                filled += 1
        except FloodWaitError as exc:
            logger.warning("backfill_total flood_wait dialog_id=%d seconds=%d", dialog_id, exc.seconds)
            if await sleep_through_flood(shutdown_event, flood_seconds(exc)):
                return filled  # shutdown during flood wait
            # flood wait elapsed normally — fall through to next dialog
        except _BACKFILL_TOTAL_MESSAGES_SKIP_EXCEPTIONS as exc:
            logger.debug("backfill_total skip dialog_id=%d error=%s", dialog_id, exc)
            await asyncio.sleep(1.0)

    logger.info("backfill_total_messages filled=%d/%d", filled, len(rows))
    return filled


async def _initialize_read_positions(
    client: Any,
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
    rows = conn.execute(_SELECT_NULL_READ_CURSORS_SQL).fetchall()
    if not rows:
        logger.info("initialize_read_positions — no NULL rows, skipping")
        return 0

    dialog_ids = [row[0] for row in rows]
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
            result = await client(GetPeerDialogsRequest(peers=input_peers))
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


async def _build_read_position_input_peers(client: Any, batch_ids: list[int]) -> list[InputDialogPeer]:
    input_peers: list[InputDialogPeer] = []
    for dialog_id in batch_ids:
        try:
            peer = await client.get_input_entity(dialog_id)
            input_peers.append(InputDialogPeer(peer=peer))
        except (RPCError, TypeError, ValueError) as exc:
            logger.debug("read_pos_bootstrap skip dialog_id=%d error=%s", dialog_id, exc)
    return input_peers


def _apply_read_positions_from_dialogs(conn: sqlite3.Connection, result: Any) -> int:
    """Apply read cursors from a GetPeerDialogsRequest result."""
    filled = 0
    for dialog in result.dialogs:
        chat_id = telethon_utils.get_peer_id(dialog.peer)
        # D-03 LOCKED: None → skip (preserve NULL). NEVER fold
        # None → 0; that would lie with [all read] during the
        # bootstrap window. The DB cursor stays NULL and Plan 03
        # renders [unknown (sync pending)]. 0 is a legitimate
        # distinct value (peer/me has read nothing) — writes 0.
        inbox_max = getattr(dialog, "read_inbox_max_id", None)
        outbox_max = getattr(dialog, "read_outbox_max_id", None)
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
    # Inter-batch pause: 1.5s, SIGTERM-responsive
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=1.5)
        return False
    except TimeoutError:
        return True


# ---------------------------------------------------------------------------
# Heartbeat — standalone for testability (no nonlocal / closure)
# ---------------------------------------------------------------------------


def _log_heartbeat(
    conn: sqlite3.Connection,
    client: Any,
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
        stats = dict(conn.execute("SELECT status, COUNT(*) FROM synced_dialogs GROUP BY status").fetchall())
        msg_count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
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

    eta_str = ""
    if synced > 0 and synced < total:
        remaining = total - synced
        elapsed = now_mono - sync_start
        secs_per_dialog = elapsed / synced
        eta_secs = int(remaining * secs_per_dialog)
        if eta_secs >= SECONDS_PER_HOUR:
            eta_str = f" eta={eta_secs // SECONDS_PER_HOUR}h{(eta_secs % SECONDS_PER_HOUR) // SECONDS_PER_MINUTE}m"
        elif eta_secs >= SECONDS_PER_MINUTE:
            eta_str = f" eta={eta_secs // SECONDS_PER_MINUTE}m{eta_secs % SECONDS_PER_MINUTE}s"
        else:
            eta_str = f" eta={eta_secs}s"
    elif synced >= total:
        eta_str = " eta=done"

    logger.info(
        "heartbeat — connected=%s dialogs=%d/%d messages=%d rate=%.0fmsg/s%s",
        client.is_connected(),
        synced,
        total,
        msg_count,
        rate,
        eta_str,
    )
    return msg_count, now_mono


# ---------------------------------------------------------------------------
# Sync loop — batch processing + idle wait
# ---------------------------------------------------------------------------


async def _maybe_heartbeat_and_gap_scan(
    conn: sqlite3.Connection,
    client: Any,
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
    client: Any,
) -> None:
    """Run the batch-sync loop with periodic heartbeat and gap scan."""
    sync_start = time.monotonic()
    try:
        last_hb_msg_count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
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


def _create_tracked_task(ctx: _SyncMainContext, coro: Any, *, name: str | None = None) -> asyncio.Task:
    """Create an asyncio task and track it for shutdown cancellation."""
    task = asyncio.create_task(coro, name=name)
    ctx.background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        ctx.background_tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            logger.error("background_task_failed name=%s error=%s", t.get_name(), exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


async def _build_sync_main_context() -> _SyncMainContext:
    db_path = get_sync_db_path()
    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)
    migrate_legacy_databases(conn, db_path.parent)

    # Open feedback.db before registering the shutdown handler so the SIGTERM
    # handler can checkpoint it.  feedback_conn is opened on the asyncio thread
    # (sync_main coroutine) — the same thread the SIGTERM handler runs on via
    # loop.add_signal_handler — so no cross-thread SQLite sharing occurs.
    feedback_db_path = get_feedback_db_path()
    feedback_conn = ensure_feedback_schema(feedback_db_path)
    logger.info("feedback.db ready at %s", feedback_db_path)

    loop = asyncio.get_running_loop()
    shutdown_event = register_shutdown_handler(conn, loop, feedback_conn=feedback_conn)

    client = create_client(catch_up=True)
    api_server = DaemonAPIServer(conn, client, shutdown_event, feedback_conn)
    socket_path = get_daemon_socket_path()
    # Ensure the runtime/state dir exists before binding — do not assume a prior
    # get_sync_db_path() call (or a Docker volume mount) already created it.
    socket_path.parent.mkdir(parents=True, exist_ok=True)
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
        await ctx.client.connect()
    except (TimeoutError, OSError) as exc:
        ctx.api_server.startup_detail = f"connection failed: {exc}"
        logger.error("sync-daemon connection failed: %s", exc, exc_info=True)
        return False

    logger.info("sync-daemon started — connected=%s", ctx.client.is_connected())
    return True


async def _prime_runtime(ctx: _SyncMainContext) -> None:
    # Phase 39.1: cache authenticated user id once at startup so query-build
    # paths (Plan 39.1-02) can bind it as a SQL parameter without calling
    # Telethon per request. Failure propagates — daemon cannot serve reads
    # correctly without a stable self_id.
    ctx.api_server.startup_detail = "fetching account info"
    me = await ctx.client.get_me()
    ctx.api_server.self_id = int(me.id)
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
    logger.info("daemon ready — serving requests on %s", ctx.socket_path)


async def _start_bootstrap_background_tasks(
    ctx: _SyncMainContext,
    worker: FullSyncWorker,
    delta_worker: DeltaSyncWorker,
) -> None:
    assert ctx.handler_manager is not None

    # Keep the worker alive only as long as the sync loop needs it.
    ctx.api_server.startup_detail = "running delta catch-up"
    delta_new = await delta_worker.run_delta_catch_up()
    logger.info("delta_catch_up=%d new messages from gap-fill", delta_new)

    ctx.api_server.startup_detail = "bootstrapping DMs"
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
    task_specs: list[tuple[Any, str]] = [
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
    _create_tracked_task(
        ctx,
        run_access_probe_loop(ctx.client, ctx.conn, ctx.shutdown_event, delta_worker),
        name="access_probe_loop",
    )
    _create_tracked_task(
        ctx, run_activity_sync_loop(ctx.client, ctx.conn, ctx.shutdown_event), name="activity_sync_loop"
    )
    _create_tracked_task(ctx, run_hot_sweep_loop(ctx.client, ctx.conn, ctx.shutdown_event), name="activity_hot_sweep")
    _create_tracked_task(
        ctx, run_cold_backfill_loop(ctx.client, ctx.conn, ctx.shutdown_event), name="activity_cold_backfill"
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
    get_daemon_socket_path().unlink(missing_ok=True)
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
    ctx = await _build_sync_main_context()
    try:
        await _run_fts_backfill(ctx)

        if not await _connect_telegram(ctx):
            return

        await _prime_runtime(ctx)

        ctx.handler_manager = EventHandlerManager(ctx.client, ctx.conn, ctx.shutdown_event)
        ctx.handler_manager.register()
        logger.info("event handlers registered")

        delta_worker = DeltaSyncWorker(ctx.client, ctx.conn, ctx.shutdown_event)
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
