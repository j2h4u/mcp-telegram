"""Sync daemon — long-running process that exclusively owns the TelegramClient.

Started via ``mcp-telegram sync``. Connects to Telegram, ensures sync.db schema,
bootstraps DM dialogs, then runs FullSyncWorker in a tight batch loop with
periodic heartbeat logging and clean SIGTERM handling.

Architecture (DAEMON-01 / DAEMON-02):
- sync-daemon is the sole owner of TelegramClient — connects once, holds it.
- MCP server runs separately with disable_telegram_session() active and reads
  sync.db via open_sync_db_reader(); it never calls client.connect().
- SIGTERM triggers shutdown_event (set by register_shutdown_handler), which
  checkpoints WAL and closes the DB connection before the daemon disconnects.

Phase 27 (event handlers):
- EventHandlerManager is registered BEFORE FullSyncWorker starts (D-06) so
  no real-time events are missed during initial bulk fetch.  INSERT OR REPLACE
  handles any overlap between real-time and bulk paths idempotently (D-07).
- synced_dialogs set is refreshed every heartbeat so newly enrolled dialogs
  are picked up within one interval without re-registering handlers (D-08/D-09).
- Weekly gap scan detects tombstoned DM messages that MTProto delete events
  cannot report (D-14/D-15).

Phase 28 (delta catch-up):
- connect() called with catch_up=True — Telethon replays missed updates via PTS
  on reconnect (D-05).
- DeltaSyncWorker.run_delta_catch_up() fills forward gaps for all 'synced'
  dialogs before bootstrap_dms() enrolls new ones (D-08).

Phase 29 (daemon API):
- DaemonAPIServer runs on a Unix socket alongside the sync loop, serving
  list_messages / search_messages / list_dialogs requests from MCP server.
- FTS backfill runs once at startup for messages without FTS index entries.
- Socket file cleaned up on shutdown (and stale file removed on startup).
"""
from __future__ import annotations

import asyncio
import logging
import time

from .daemon_api import DaemonAPIServer, get_daemon_socket_path
from .delta_sync import DeltaSyncWorker
from .event_handlers import EventHandlerManager
from .fts import backfill_fts_index
from .sync_db import (
    _open_sync_db,
    ensure_sync_schema,
    get_sync_db_path,
    register_shutdown_handler,
)
from .sync_worker import FullSyncWorker
from .telegram import create_client

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S: float = 60.0
GAP_SCAN_INTERVAL_S: float = 7 * 24 * 3600.0  # Weekly (D-14)


async def sync_main() -> None:
    """Main entry point for the sync daemon process.

    Sequence:
    1. Ensure sync.db schema is at current version.
    2. Open the long-lived writer connection.
    3. Register SIGTERM shutdown handler (checkpoints WAL on signal).
    4. Connect to Telegram with catch_up=True — replay missed updates via PTS.
    5. Register event handlers (D-06): BEFORE delta catch-up and FullSyncWorker.
    6. Run DeltaSyncWorker.run_delta_catch_up() — fill gaps for 'synced' dialogs.
    7. Bootstrap DM dialogs (D-06): enroll all User-type dialogs once.
    8. Refresh synced_dialogs after bootstrap adds new dialogs.
    9. Run tight sync loop: process_one_batch() + periodic heartbeat (D-10).
    10. When all synced, enter idle heartbeat-only mode (D-11).
    11. Unregister event handlers and disconnect TelegramClient on shutdown.
    """
    db_path = get_sync_db_path()
    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)

    # Phase 29: One-time FTS backfill for messages without index entries
    backfilled = backfill_fts_index(conn)
    if backfilled:
        logger.info("fts_backfill=%d messages indexed", backfilled)

    loop = asyncio.get_running_loop()
    shutdown_event = register_shutdown_handler(conn, loop)

    client = create_client(catch_up=True)
    handler_manager: EventHandlerManager | None = None
    unix_server = None
    try:
        try:
            await client.connect()
        except ConnectionError as exc:
            logger.error("sync-daemon connection failed: %s", exc)
            conn.close()
            return

        logger.info("sync-daemon started — connected=%s", client.is_connected())

        # Phase 29: Start daemon API server on Unix socket
        api_server = DaemonAPIServer(conn, client, shutdown_event)
        socket_path = get_daemon_socket_path()
        socket_path.unlink(missing_ok=True)
        unix_server = await asyncio.start_unix_server(
            api_server.handle_client, path=str(socket_path)
        )
        logger.info("daemon API listening on %s", socket_path)

        # Phase 27 (D-06): Register event handlers BEFORE FullSyncWorker
        handler_manager = EventHandlerManager(client, conn, shutdown_event)
        handler_manager.register()
        logger.info("event handlers registered")

        # Phase 28 (D-08): Delta catch-up for synced dialogs before bootstrap
        delta_worker = DeltaSyncWorker(client, conn, shutdown_event)
        delta_new = await delta_worker.run_delta_catch_up()
        logger.info("delta_catch_up=%d new messages from gap-fill", delta_new)

        # Phase 1 — Bootstrap (D-06): enroll all DM dialogs once at startup
        worker = FullSyncWorker(client, conn, shutdown_event)
        enrolled = await worker.bootstrap_dms()
        logger.info("dm_bootstrap complete — enrolled=%d", enrolled)

        # Refresh synced_dialogs after bootstrap adds new dialogs
        handler_manager.refresh_synced_dialogs()

        # Phase 2 — Tight sync loop with heartbeat (D-10, D-11)
        last_heartbeat = time.monotonic()
        last_gap_scan = time.monotonic()

        while not shutdown_event.is_set():
            all_synced = await worker.process_one_batch()

            now_mono = time.monotonic()

            # Periodic heartbeat logging and synced_dialogs refresh
            if now_mono - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                logger.info("heartbeat — connected=%s", client.is_connected())
                handler_manager.refresh_synced_dialogs()
                last_heartbeat = now_mono

            # Weekly gap scan (D-14): detect tombstoned DM messages
            if now_mono - last_gap_scan >= GAP_SCAN_INTERVAL_S:
                deleted_count = await handler_manager.run_dm_gap_scan()
                logger.info("gap_scan complete — marked_deleted=%d", deleted_count)
                last_gap_scan = now_mono

            if all_synced:
                # D-11: idle mode — wait for HEARTBEAT_INTERVAL_S or shutdown
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=HEARTBEAT_INTERVAL_S,
                    )
                    break  # shutdown requested
                except asyncio.TimeoutError:
                    logger.info("heartbeat — connected=%s", client.is_connected())
                    handler_manager.refresh_synced_dialogs()
                    last_heartbeat = time.monotonic()

                    # Check gap scan during idle too
                    if time.monotonic() - last_gap_scan >= GAP_SCAN_INTERVAL_S:
                        deleted_count = await handler_manager.run_dm_gap_scan()
                        logger.info("gap_scan complete — marked_deleted=%d", deleted_count)
                        last_gap_scan = time.monotonic()

    finally:
        if unix_server is not None:
            unix_server.close()
            await unix_server.wait_closed()
        get_daemon_socket_path().unlink(missing_ok=True)
        if handler_manager is not None:
            handler_manager.unregister()
        await client.disconnect()
        logger.info("sync-daemon stopped")
