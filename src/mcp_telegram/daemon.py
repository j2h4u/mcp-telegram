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
from typing import Any

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import FloodWaitError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetPeerDialogsRequest  # type: ignore[import-untyped]
from telethon.tl.types import InputDialogPeer  # type: ignore[import-untyped]

from .daemon_api import DaemonAPIServer, get_daemon_socket_path
from .activity_sync import run_activity_sync_loop
from .delta_sync import DeltaSyncWorker, run_access_probe_loop
from .event_handlers import EventHandlerManager
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

# Bootstrap sweep batch size for GetPeerDialogsRequest. Telethon's per-call
# limit is 100; we intentionally stay in the 10-20 range to avoid the
# FloodWait burst that broke the 260416-ifp incident. 15 is the sweet spot
# documented in Plan 39.3-02 (R4) and the _initialize_read_positions docstring.
# Paired with a 1.5s inter-batch pause in the loop body.
_BOOTSTRAP_BATCH_SIZE: int = 15

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
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=float(exc.seconds))
                return filled  # shutdown during flood wait
            except TimeoutError:
                pass  # flood wait elapsed normally
        except Exception as exc:
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
        try:
            input_peers = []
            for did in batch_ids:
                try:
                    peer = await client.get_input_entity(did)
                    input_peers.append(InputDialogPeer(peer=peer))
                except Exception as exc:
                    logger.debug("read_pos_bootstrap skip dialog_id=%d error=%s", did, exc)
            if input_peers:
                result = await client(GetPeerDialogsRequest(peers=input_peers))
                for d in result.dialogs:
                    chat_id = telethon_utils.get_peer_id(d.peer)
                    # D-03 LOCKED: None → skip (preserve NULL). NEVER fold
                    # None → 0; that would lie with [all read] during the
                    # bootstrap window. The DB cursor stays NULL and Plan 03
                    # renders [unknown (sync pending)]. 0 is a legitimate
                    # distinct value (peer/me has read nothing) — writes 0.
                    inbox_max = getattr(d, "read_inbox_max_id", None)
                    outbox_max = getattr(d, "read_outbox_max_id", None)
                    wrote_any = False
                    if inbox_max is not None:
                        # Monotonic via shared primitive — see read_state.py.
                        rowcount = apply_read_cursor(conn, chat_id, "inbox", inbox_max)
                        if rowcount > 0:
                            wrote_any = True
                    if outbox_max is not None:
                        rowcount = apply_read_cursor(conn, chat_id, "outbox", outbox_max)
                        if rowcount > 0:
                            wrote_any = True
                    if wrote_any:
                        filled += 1
                conn.commit()
        except FloodWaitError as exc:
            logger.warning("read_pos_bootstrap flood_wait seconds=%d", exc.seconds)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=float(exc.seconds))
                return filled
            except TimeoutError:
                pass
        except Exception as exc:
            logger.debug("read_pos_bootstrap batch_failed error=%s", exc)

        # Inter-batch pause: 1.5s, SIGTERM-responsive
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=1.5)
            break
        except TimeoutError:
            pass

    logger.info("initialize_read_positions filled=%d/%d", filled, len(dialog_ids))
    return filled


# ---------------------------------------------------------------------------
# Heartbeat — standalone for testability (no nonlocal / closure)
# ---------------------------------------------------------------------------


def _log_heartbeat(conn: sqlite3.Connection, client: Any, sync_start: float) -> None:
    """Log heartbeat with sync stats, rate, and ETA from sync.db."""
    try:
        stats = dict(conn.execute("SELECT status, COUNT(*) FROM synced_dialogs GROUP BY status").fetchall())
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    except Exception:
        logger.warning("heartbeat_stats_failed", exc_info=True)
        stats = {}
        msg_count = 0
    synced = stats.get("synced", 0)
    syncing = stats.get("syncing", 0)
    total = synced + syncing + stats.get("not_synced", 0)

    elapsed = time.monotonic() - sync_start
    rate = msg_count / elapsed if elapsed > 0 else 0

    eta_str = ""
    if synced > 0 and synced < total:
        remaining = total - synced
        secs_per_dialog = elapsed / synced
        eta_secs = int(remaining * secs_per_dialog)
        if eta_secs >= 3600:
            eta_str = f" eta={eta_secs // 3600}h{(eta_secs % 3600) // 60}m"
        elif eta_secs >= 60:
            eta_str = f" eta={eta_secs // 60}m{eta_secs % 60}s"
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


# ---------------------------------------------------------------------------
# Sync loop — batch processing + idle wait
# ---------------------------------------------------------------------------


async def _maybe_heartbeat_and_gap_scan(
    conn: sqlite3.Connection,
    client: Any,
    handler_manager: EventHandlerManager,
    sync_start: float,
    last_heartbeat: float,
    last_gap_scan: float,
) -> tuple[float, float]:
    """Run heartbeat and gap scan if their intervals have elapsed.

    Returns updated (last_heartbeat, last_gap_scan) timestamps.
    """
    now_mono = time.monotonic()

    if now_mono - last_heartbeat >= HEARTBEAT_INTERVAL_S:
        _log_heartbeat(conn, client, sync_start)
        handler_manager.refresh_synced_dialogs()
        last_heartbeat = now_mono

    if now_mono - last_gap_scan >= GAP_SCAN_INTERVAL_S:
        deleted_count = await handler_manager.run_dm_gap_scan()
        logger.info("gap_scan complete — marked_deleted=%d", deleted_count)
        last_gap_scan = now_mono

    return last_heartbeat, last_gap_scan


async def _run_sync_loop(
    worker: FullSyncWorker,
    handler_manager: EventHandlerManager,
    shutdown_event: asyncio.Event,
    conn: sqlite3.Connection,
    client: Any,
) -> None:
    """Run the batch-sync loop with periodic heartbeat and gap scan."""
    sync_start = time.monotonic()
    last_heartbeat = sync_start
    last_gap_scan = sync_start

    while not shutdown_event.is_set():
        all_synced = await worker.process_one_batch()
        await asyncio.sleep(0)

        last_heartbeat, last_gap_scan = await _maybe_heartbeat_and_gap_scan(
            conn,
            client,
            handler_manager,
            sync_start,
            last_heartbeat,
            last_gap_scan,
        )

        if all_synced:
            logger.info("sync_idle — all dialogs synced, waiting %ds", HEARTBEAT_INTERVAL_S)
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=HEARTBEAT_INTERVAL_S,
                )
                break
            except TimeoutError:
                last_heartbeat, last_gap_scan = await _maybe_heartbeat_and_gap_scan(
                    conn,
                    client,
                    handler_manager,
                    sync_start,
                    last_heartbeat,
                    last_gap_scan,
                )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def sync_main() -> None:
    """Main entry point for the sync daemon process.

    Orchestrates: DB init → Telegram connect → wire services → sync loop → cleanup.
    """
    db_path = get_sync_db_path()
    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)
    migrate_legacy_databases(conn, db_path.parent)

    loop = asyncio.get_running_loop()
    shutdown_event = register_shutdown_handler(conn, loop)

    client = create_client(catch_up=True)
    handler_manager: EventHandlerManager | None = None

    # Create API server and socket early so healthcheck and MCP tools get a
    # meaningful "daemon_not_ready" response (with detail) instead of
    # "connection refused" while Telegram is connecting.
    api_server = DaemonAPIServer(conn, client, shutdown_event)
    socket_path = get_daemon_socket_path()
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
    os.chmod(socket_path, 0o600)
    logger.info("daemon API listening on %s (not ready yet)", socket_path)

    # FTS backfill runs in a thread pool (stemming is CPU-bound) so it doesn't
    # block the event loop. Awaited here — before Telegram connect — so the
    # socket is already up and responding "not ready / indexing messages for
    # search" while we work. Total startup time = FTS time + Telegram time.
    api_server.startup_detail = "indexing messages for search"
    try:
        # Open a dedicated connection for the thread — sqlite3 connections are
        # not thread-safe and cannot be shared across threads.
        def _backfill_in_thread() -> int:
            thread_conn = _open_sync_db(db_path)
            try:
                return backfill_fts_index(thread_conn)
            finally:
                thread_conn.close()

        backfilled = await asyncio.to_thread(_backfill_in_thread)
        if backfilled:
            logger.info("fts_backfill=%d messages indexed", backfilled)
    except Exception:
        logger.warning("fts_backfill failed — FTS search may be incomplete until next restart", exc_info=True)

    # Local task set — scoped to this sync_main() invocation so that multiple
    # calls within the same process (e.g. in tests) never share stale tasks from
    # a previous event loop.
    background_tasks: set[asyncio.Task] = set()

    def _create_tracked_task(coro: Any, *, name: str | None = None) -> asyncio.Task:
        """Create an asyncio task and track it for shutdown cancellation."""
        task = asyncio.create_task(coro, name=name)
        background_tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            background_tasks.discard(t)
            exc = t.exception() if not t.cancelled() else None
            if exc is not None:
                logger.error("background_task_failed name=%s error=%s", t.get_name(), exc, exc_info=exc)

        task.add_done_callback(_on_done)
        return task

    try:
        try:
            api_server.startup_detail = "connecting to Telegram"
            await client.connect()
        except (TimeoutError, OSError) as exc:
            api_server.startup_detail = f"connection failed: {exc}"
            logger.error("sync-daemon connection failed: %s", exc, exc_info=True)
            conn.close()
            return

        logger.info("sync-daemon started — connected=%s", client.is_connected())

        # Phase 39.1: cache authenticated user id once at startup so query-build
        # paths (Plan 39.1-02) can bind it as a SQL parameter without calling
        # Telethon per request. Failure propagates — daemon cannot serve reads
        # correctly without a stable self_id.
        api_server.startup_detail = "fetching account info"
        me = await client.get_me()
        api_server.self_id = int(me.id)
        logger.info("daemon self_id cached: %s", api_server.self_id)

        # Post-v10 runtime backfill: mark historical outgoing DM rows as out=1
        # using sender_id=self_id (the authoritative signal). Pure-SQL v10
        # migration can only match sender_id IS NULL, but re-ingestion after
        # Phase 39.1 typically populates sender_id with the real peer/self
        # values — so the NULL-sender shape is rare in practice. This daemon
        # step closes the gap once self_id is known. Idempotent via out=0.
        try:
            cur = conn.execute(
                "UPDATE messages SET out = 1 WHERE out = 0 AND dialog_id > 0 AND sender_id = ?",
                (api_server.self_id,),
            )
            conn.commit()
            if cur.rowcount > 0:
                logger.info("backfilled out=1 on %d historical outgoing DM rows", cur.rowcount)
        except Exception:
            logger.warning("out=1 backfill skipped — non-fatal", exc_info=True)
        api_server._ready = True
        logger.info("daemon ready — serving requests on %s", socket_path)

        handler_manager = EventHandlerManager(client, conn, shutdown_event)
        handler_manager.register()
        logger.info("event handlers registered")

        api_server.startup_detail = "running delta catch-up"
        delta_worker = DeltaSyncWorker(client, conn, shutdown_event)
        delta_new = await delta_worker.run_delta_catch_up()
        logger.info("delta_catch_up=%d new messages from gap-fill", delta_new)

        api_server.startup_detail = "bootstrapping DMs"
        worker = FullSyncWorker(client, conn, shutdown_event)
        enrolled = await worker.bootstrap_dms()
        logger.info("dm_bootstrap complete — enrolled=%d", enrolled)

        handler_manager.refresh_synced_dialogs()

        # Background tasks — non-blocking, tracked for shutdown
        _create_tracked_task(
            _backfill_total_messages(client, conn, shutdown_event),
            name="backfill_total_messages",
        )
        # Must come AFTER handler_manager.register() (startup-ordering invariant):
        # the on_message_read handler must be live before bootstrap starts so no
        # real-time MessageRead events are dropped during the bootstrap window.
        _create_tracked_task(
            _initialize_read_positions(client, conn, shutdown_event),
            name="initialize_read_positions",
        )
        _create_tracked_task(
            run_access_probe_loop(client, conn, shutdown_event, delta_worker),
            name="access_probe_loop",
        )
        _create_tracked_task(
            run_activity_sync_loop(client, conn, shutdown_event),
            name="activity_sync_loop",
        )

        await _run_sync_loop(worker, handler_manager, shutdown_event, conn, client)

    finally:
        if unix_server is not None:
            unix_server.close()
            await unix_server.wait_closed()
        get_daemon_socket_path().unlink(missing_ok=True)
        if handler_manager is not None:
            handler_manager.unregister()
        # Cancel tracked background tasks
        for task in background_tasks:
            task.cancel()
        for task in list(background_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass  # expected on shutdown; task was cancelled cleanly
            except Exception:
                logger.warning("background_task_shutdown_error name=%s", task.get_name(), exc_info=True)
        background_tasks.clear()
        await client.disconnect()
        conn.close()
        logger.info("sync-daemon stopped")
