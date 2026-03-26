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
"""
from __future__ import annotations

import asyncio
import logging
import time

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


async def sync_main() -> None:
    """Main entry point for the sync daemon process.

    Sequence:
    1. Ensure sync.db schema is at current version.
    2. Open the long-lived writer connection.
    3. Register SIGTERM shutdown handler (checkpoints WAL on signal).
    4. Connect to Telegram — log error and exit cleanly on ConnectionError.
    5. Bootstrap DM dialogs (D-06): enroll all User-type dialogs once.
    6. Run tight sync loop: process_one_batch() + periodic heartbeat (D-10).
    7. When all synced, enter idle heartbeat-only mode (D-11).
    8. Disconnect TelegramClient and return.
    """
    db_path = get_sync_db_path()
    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)
    loop = asyncio.get_running_loop()
    shutdown_event = register_shutdown_handler(conn, loop)

    client = create_client()
    try:
        try:
            await client.connect()
        except ConnectionError as exc:
            logger.error("sync-daemon connection failed: %s", exc)
            conn.close()
            return

        logger.info("sync-daemon started — connected=%s", client.is_connected())

        # Phase 1 — Bootstrap (D-06): enroll all DM dialogs once at startup
        worker = FullSyncWorker(client, conn, shutdown_event)
        enrolled = await worker.bootstrap_dms()
        logger.info("dm_bootstrap complete — enrolled=%d", enrolled)

        # Phase 2 — Tight sync loop with heartbeat (D-10, D-11)
        last_heartbeat = time.monotonic()

        while not shutdown_event.is_set():
            all_synced = await worker.process_one_batch()

            # Periodic heartbeat logging during active sync
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                logger.info("heartbeat — connected=%s", client.is_connected())
                last_heartbeat = now

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
                    last_heartbeat = time.monotonic()

    finally:
        await client.disconnect()
        logger.info("sync-daemon stopped")
