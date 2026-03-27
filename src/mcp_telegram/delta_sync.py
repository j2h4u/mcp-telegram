"""DeltaSyncWorker — forward gap-fill engine for v1.5 Persistent Sync.

Fetches messages newer than the max known message_id per dialog on every
daemon startup (DAEMON-12). Idempotent: dialogs with no gap complete
instantly when iter_messages returns empty.

Architecture:
- Mirrors FullSyncWorker structural pattern (client/conn/shutdown_event).
- Fetches FORWARD (min_id + reverse=True) vs FullSyncWorker's backward.
- Runs once at startup, before bootstrap_dms and FullSyncWorker loop.
- Only processes dialogs with status='synced' — FullSyncWorker handles
  'syncing' and 'not_synced' dialogs.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any

from telethon.errors import FloodWaitError  # type: ignore[import-untyped]
from telethon.errors import RPCError  # type: ignore[import-untyped]

from .fts import INSERT_FTS_SQL, stem_text
from .sync_worker import (
    _ACCESS_LOST_ERRORS,
    _INSERT_MESSAGE_SQL,
    _SET_ACCESS_LOST_SQL,
    extract_message_row,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_SELECT_SYNCED_DIALOG_IDS_SQL = (
    "SELECT dialog_id FROM synced_dialogs WHERE status = 'synced'"
)

_SELECT_MAX_MESSAGE_ID_SQL = (
    "SELECT COALESCE(MAX(message_id), 0) FROM messages WHERE dialog_id = ?"
)


# ---------------------------------------------------------------------------
# DeltaSyncWorker
# ---------------------------------------------------------------------------


class DeltaSyncWorker:
    """Forward gap-fill engine for the v1.5 sync daemon.

    Fetches messages newer than the max known message_id per dialog in a
    single pass at daemon startup. One instance is created per daemon run
    and called once before FullSyncWorker's bootstrap loop.

    Args:
        client: Telethon TelegramClient (daemon owns the connection).
        conn: Open SQLite writer connection to sync.db.
        shutdown_event: asyncio.Event set when SIGTERM is received.
            Used to make FloodWait sleeps and the dialog loop interruptible.
    """

    def __init__(
        self,
        client: Any,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._client = client
        self._conn = conn
        self._shutdown_event = shutdown_event

    async def run_delta_catch_up(self) -> int:
        """Fetch messages newer than max known id for all 'synced' dialogs.

        Returns:
            Total count of new messages stored across all dialogs.

        Idempotent: dialogs with no gap complete instantly (empty first
        batch from iter_messages). Skips dialogs with no baseline
        (max_known_id=0) — FullSyncWorker handles those.
        """
        rows = self._conn.execute(_SELECT_SYNCED_DIALOG_IDS_SQL).fetchall()
        total_new = 0
        for (dialog_id,) in rows:
            if self._shutdown_event.is_set():
                break
            total_new += await self._fetch_delta_for_dialog(dialog_id)
        logger.info("delta_catch_up complete — new_messages=%d", total_new)
        return total_new

    async def _fetch_delta_for_dialog(self, dialog_id: int) -> int:
        """Fetch all messages newer than max known message_id for one dialog.

        Uses iter_messages(min_id=max_known_id, reverse=True) to fetch
        the gap in chronological order. INSERT OR REPLACE ensures
        idempotency across restarts.

        Returns:
            Count of new messages stored. 0 if no gap, no baseline, or error.
        """
        row = self._conn.execute(
            _SELECT_MAX_MESSAGE_ID_SQL, (dialog_id,)
        ).fetchone()
        max_known_id = row[0] if row else 0
        if max_known_id == 0:
            # No baseline yet — FullSyncWorker handles this dialog
            return 0

        new_msgs: list[tuple[object, ...]] = []
        try:
            async for msg in self._client.iter_messages(
                entity=dialog_id, min_id=max_known_id, reverse=True, limit=None
            ):
                if self._shutdown_event.is_set():
                    break
                new_msgs.append(extract_message_row(dialog_id, msg))
        except FloodWaitError as exc:
            logger.warning(
                "FloodWait delta dialog_id=%d — %ds", dialog_id, exc.seconds
            )
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=float(exc.seconds)
                )
            except asyncio.TimeoutError:
                pass  # slept the full duration; return what we have so far
            return 0
        except _ACCESS_LOST_ERRORS as exc:
            logger.warning(
                "access_lost delta dialog_id=%d — %s",
                dialog_id, type(exc).__name__,
            )
            now = int(time.time())
            with self._conn:
                self._conn.execute(_SET_ACCESS_LOST_SQL, (now, dialog_id))
            return 0
        except RPCError as exc:
            logger.error(
                "RPC error delta dialog_id=%d — skipping: %s", dialog_id, exc
            )
            return 0

        if new_msgs:
            with self._conn:
                self._conn.executemany(_INSERT_MESSAGE_SQL, new_msgs)
                self._conn.executemany(
                    INSERT_FTS_SQL,
                    ((row[0], row[1], stem_text(row[3])) for row in new_msgs),  # type: ignore[arg-type]
                )
            logger.debug(
                "delta dialog_id=%d new_messages=%d", dialog_id, len(new_msgs)
            )
        return len(new_msgs)
