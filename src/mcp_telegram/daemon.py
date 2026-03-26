"""Sync daemon — long-running process that exclusively owns the TelegramClient.

Started via ``mcp-telegram sync``. Connects to Telegram, ensures sync.db schema,
then runs a heartbeat loop until SIGTERM.  Future phases add FullSyncWorker,
event handlers, and delta-sync on top of this skeleton.

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

from .sync_db import (
    _open_sync_db,
    ensure_sync_schema,
    get_sync_db_path,
    register_shutdown_handler,
)
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
    5. Run heartbeat loop until shutdown_event is set.
    6. Disconnect TelegramClient and return.
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

        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=HEARTBEAT_INTERVAL_S,
                )
                # shutdown_event was set — exit loop
                break
            except asyncio.TimeoutError:
                logger.info("heartbeat — connected=%s", client.is_connected())

    finally:
        await client.disconnect()
        logger.info("sync-daemon stopped")
