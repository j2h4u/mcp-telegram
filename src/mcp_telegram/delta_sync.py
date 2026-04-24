"""DeltaSyncWorker — forward gap-fill engine for v1.5 Persistent Sync.

Fetches messages newer than the max known message_id per dialog on every
daemon startup. Idempotent: dialogs with no gap complete
instantly when iter_messages returns empty.

Architecture:
- Mirrors FullSyncWorker structural pattern (client/conn/shutdown_event).
- Fetches FORWARD (min_id + reverse=True) vs FullSyncWorker's backward.
- Runs once at startup, before bootstrap_dms and FullSyncWorker loop.
- Only processes dialogs with status='synced' — FullSyncWorker handles
  'syncing' and 'not_synced' dialogs.
"""

import asyncio
import logging
import sqlite3
import time
from typing import Any

from telethon.errors import (
    FloodWaitError,  # type: ignore[import-untyped]
    RPCError,  # type: ignore[import-untyped]
)

from .sync_worker import (
    _ACCESS_LOST_ERRORS,
    _SET_ACCESS_LOST_SQL,
    ExtractedMessage,
    extract_message_row,
    insert_messages_with_fts,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

# Skip delta probe for dialogs fully synced within this window — prevents
# GetHistoryRequest storm on quick restarts (D-01 expert panel).
RECENT_SYNC_SKIP_THRESHOLD_S: float = 300.0

_SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL = (
    "SELECT dialog_id, last_synced_at FROM synced_dialogs WHERE status = 'synced'"
)
# Backward-compat alias (no external importers, kept for safety)
_SELECT_SYNCED_DIALOG_IDS_SQL = _SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL

_SELECT_MAX_MESSAGE_ID_SQL = "SELECT COALESCE(MAX(message_id), 0) FROM messages WHERE dialog_id = ?"

# Stamp last_synced_at on successful delta completion.
# Distinct from FullSyncWorker's _UPDATE_PROGRESS_DONE_SQL (different column set).
_UPDATE_DELTA_LAST_SYNCED_AT_SQL = (
    "UPDATE synced_dialogs SET last_synced_at = ? WHERE dialog_id = ?"
)

_SELECT_ACCESS_LOST_SQL = "SELECT dialog_id FROM synced_dialogs WHERE status = 'access_lost'"

_RESTORE_ACCESS_SQL = "UPDATE synced_dialogs SET status = 'syncing', access_lost_at = NULL WHERE dialog_id = ?"

_UPDATE_TOTAL_MESSAGES_SQL = "UPDATE synced_dialogs SET total_messages = ? WHERE dialog_id = ?"


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

        Quick-restart guard: dialogs whose last_synced_at is within
        RECENT_SYNC_SKIP_THRESHOLD_S are skipped to prevent a
        GetHistoryRequest storm after a rapid daemon restart (D-01).
        """
        rows = self._conn.execute(_SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL).fetchall()
        now = int(time.time())
        total_new = 0
        skipped = 0
        probed = 0
        for (dialog_id, last_synced_at) in rows:
            if self._shutdown_event.is_set():
                break
            if (
                last_synced_at is not None
                and (now - last_synced_at) < RECENT_SYNC_SKIP_THRESHOLD_S
            ):
                age_s = now - last_synced_at
                logger.info(
                    "delta_catch_up_skip dialog_id=%d age_s=%d",
                    dialog_id,
                    age_s,
                )
                skipped += 1
                continue
            probed += 1
            total_new += await self.fetch_delta_for_dialog(dialog_id)
        logger.info(
            "delta_catch_up complete — new_messages=%d skipped=%d probed=%d",
            total_new,
            skipped,
            probed,
        )
        return total_new

    async def fetch_delta_for_dialog(self, dialog_id: int) -> int:
        """Fetch all messages newer than max known message_id for one dialog.

        Public API: used by probe-worker for gap-fill after access recovery.
        Uses iter_messages(min_id=max_known_id, reverse=True) to fetch
        the gap in chronological order. INSERT OR REPLACE ensures
        idempotency across restarts.

        Returns:
            Count of new messages stored. 0 if no gap, no baseline, or error.
        """
        row = self._conn.execute(_SELECT_MAX_MESSAGE_ID_SQL, (dialog_id,)).fetchone()
        max_known_id = row[0] if row else 0
        if max_known_id == 0:
            # No baseline yet — FullSyncWorker handles this dialog
            return 0

        new_message_rows: list[ExtractedMessage] = []
        try:
            async for msg in self._client.iter_messages(
                entity=dialog_id, min_id=max_known_id, reverse=True, limit=None
            ):
                if self._shutdown_event.is_set():
                    break
                new_message_rows.append(extract_message_row(dialog_id, msg))
        except FloodWaitError as exc:
            logger.warning(
                "FloodWait delta dialog_id=%d — %ds (preserving %d already-fetched messages)",
                dialog_id,
                exc.seconds,
                len(new_message_rows),
            )
            if new_message_rows:
                with self._conn:
                    insert_messages_with_fts(self._conn, new_message_rows)
                logger.info(
                    "delta dialog_id=%d preserved_messages=%d before FloodWait",
                    dialog_id,
                    len(new_message_rows),
                )
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=float(exc.seconds))
            except TimeoutError:
                pass  # slept the full duration; caller will retry remaining gap
            return len(new_message_rows)
        except _ACCESS_LOST_ERRORS as exc:
            logger.warning(
                "access_lost delta dialog_id=%d — %s",
                dialog_id,
                type(exc).__name__,
            )
            now = int(time.time())
            with self._conn:
                self._conn.execute(_SET_ACCESS_LOST_SQL, (now, dialog_id))
            return 0
        except RPCError as exc:
            logger.error(
                "RPC error delta dialog_id=%d — skipping: %s",
                dialog_id,
                exc,
                exc_info=True,
            )
            return 0

        if new_message_rows:
            with self._conn:
                insert_messages_with_fts(self._conn, new_message_rows)
            logger.info("delta dialog_id=%d new_messages=%d", dialog_id, len(new_message_rows))
        # Stamp last_synced_at unconditionally on the success path so that
        # run_delta_catch_up's quick-restart skip check has a fresh anchor.
        with self._conn:
            self._conn.execute(_UPDATE_DELTA_LAST_SYNCED_AT_SQL, (int(time.time()), dialog_id))
        return len(new_message_rows)


# ---------------------------------------------------------------------------
# Probe-worker — access recovery for access_lost dialogs
# ---------------------------------------------------------------------------


async def _probe_access_lost_dialogs(
    client: Any,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    delta_worker: DeltaSyncWorker,
) -> int:
    """Probe all access_lost dialogs. Returns count of restored dialogs.

    Recovery sequence: probe -> gap-fill -> THEN reset status.
    If gap-fill fails, status stays access_lost (safe rollback).
    """
    rows = conn.execute(_SELECT_ACCESS_LOST_SQL).fetchall()
    if not rows:
        return 0

    restored = 0
    for (dialog_id,) in rows:
        try:
            result = await client.get_messages(entity=dialog_id, limit=1)
            # Success — access restored. Capture total before gap-fill.
            total = getattr(result, "total", None)

            # Gap-fill FIRST, while status is still access_lost.
            # If this fails, we skip the dialog — status stays access_lost.
            new_msgs = await delta_worker.fetch_delta_for_dialog(dialog_id)
            logger.info("access_restored_gap_fill dialog_id=%d new=%d", dialog_id, new_msgs)

            # Gap-fill succeeded — NOW reset status to syncing.
            with conn:
                conn.execute(_RESTORE_ACCESS_SQL, (dialog_id,))
                if total is not None:
                    conn.execute(_UPDATE_TOTAL_MESSAGES_SQL, (total, dialog_id))
            logger.info("access_restored dialog_id=%d total=%s", dialog_id, total)
            restored += 1
        except _ACCESS_LOST_ERRORS:
            logger.debug("access_still_lost dialog_id=%d", dialog_id)
        except FloodWaitError as exc:
            logger.warning("probe_flood_wait dialog_id=%d seconds=%d", dialog_id, exc.seconds)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=float(exc.seconds))
                return restored  # shutdown during flood wait
            except TimeoutError:
                pass  # flood wait elapsed normally
        except RPCError as exc:
            logger.warning("probe_rpc_error dialog_id=%d error=%s", dialog_id, exc)
        except (TimeoutError, OSError) as exc:
            logger.warning("probe_network_error dialog_id=%d error=%s", dialog_id, exc)

        await asyncio.sleep(1.0)  # rate limit between probes

    logger.info("access_probe complete — checked=%d restored=%d", len(rows), restored)
    return restored


async def run_access_probe_loop(
    client: Any,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    delta_worker: DeltaSyncWorker,
    *,
    initial_delay: float = 0.0,
    interval: float = 86400.0,
) -> None:
    """Daily probe of access_lost dialogs. Restores access and triggers gap-fill.

    Runs immediately at startup (initial_delay=0), then every 24h.
    """
    if initial_delay > 0:
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=initial_delay)
            return  # shutdown during initial delay
        except TimeoutError:
            pass  # initial delay elapsed normally; proceed with first probe

    while not shutdown_event.is_set():
        try:
            await _probe_access_lost_dialogs(client, conn, shutdown_event, delta_worker)
        except Exception:
            logger.warning("access_probe_error", exc_info=True)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return  # shutdown during sleep
        except TimeoutError:
            pass  # interval elapsed, run again
