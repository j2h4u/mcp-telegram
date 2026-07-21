"""FullSyncWorker — bulk history fetch engine for v1.5 Persistent Sync.

Fetches all historical messages for marked dialogs in batches of 100,
checkpointing progress after each batch so restarts resume without
re-scanning from scratch.

FloodWait causes an interruptible sleep — progress is never lost on
rate limits.

DM bootstrap auto-enrolls all User-type dialogs at daemon startup.

Architecture:
- Standalone module so daemon.py stays focused on process lifecycle.
- FullSyncWorker is a stateful class instantiated once per daemon run.
- Plugs into daemon.py sync_main() between heartbeat ticks.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import datetime
from typing import Protocol, cast

from telethon.errors import FloodWaitError, RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]

from .dialog_sync import _ACCESS_LOST_ERRORS, _set_access_lost
from .flood import flood_seconds, sleep_through_flood
from .messages.sqlite_repository import insert_messages_with_fts
from .messages.telegram_adapter import PeerNameClient, extract_message_row, resolve_forward_entity_name_map
from .resolver import latinize
from .telethon_dialog import classify_dialog_type

logger = logging.getLogger(__name__)
_BATCH_SIZE = 100

_NEXT_PENDING_SQL = (
    "SELECT dialog_id, sync_progress FROM synced_dialogs "
    "WHERE status IN ('syncing', 'not_synced') "
    "ORDER BY rowid LIMIT 1"
)
_UPDATE_PROGRESS_SQL = "UPDATE synced_dialogs SET sync_progress = ?, status = ?, total_messages = ? WHERE dialog_id = ?"
_UPDATE_PROGRESS_DONE_SQL = (
    "UPDATE synced_dialogs SET sync_progress = ?, status = ?, total_messages = ?, "
    "last_synced_at = ? WHERE dialog_id = ?"
)

INSERT_DIALOG_SQL = "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'syncing')"
UPSERT_ENTITY_SQL = (
    "INSERT OR REPLACE INTO entities (id, type, name, username, name_normalized, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
)


class _EntityLike(Protocol):
    id: int
    title: str | None
    first_name: str | None
    last_name: str | None
    username: str | None
    access_hash: int | None
    bot: bool
    broadcast: bool
    date: datetime | None


class _DraftLike(Protocol):
    message: str | None


class _MessageLike(Protocol):
    id: int


class _DialogLike(Protocol):
    id: int
    entity: _EntityLike
    message: _MessageLike | None
    unread_mentions_count: int | None
    unread_reactions_count: int | None
    draft: _DraftLike | None
    date: datetime | None
    pinned: bool
    folder_id: int | None


class _ForumTopicLike(Protocol):
    id: int
    title: str | None
    icon_emoji_id: int | None
    date: datetime | None


class _ForumTopicsResultLike(Protocol):
    topics: Sequence[_ForumTopicLike]


class _MessagesPageLike(Protocol):
    total: int

    def __iter__(self) -> Iterator[_MessageLike]: ...


class _SyncWorkerClient(Protocol):
    def iter_dialogs(self, **_kwargs: object) -> AsyncIterator[_DialogLike]: ...

    async def get_messages(self, **_kwargs: object) -> _MessagesPageLike: ...

    async def get_entity(self, _entity_id: object) -> _EntityLike: ...

    async def __call__(self, _request: object) -> _ForumTopicsResultLike: ...


_DialogRow = dict[str, object]


# ---------------------------------------------------------------------------
# FullSyncWorker
# ---------------------------------------------------------------------------


class FullSyncWorker:
    """Core bulk-fetch engine for the v1.5 sync daemon.

    Fetches historical Telegram messages in batches and stores them in
    sync.db.  One instance is created per daemon run; it is called
    between heartbeat ticks in sync_main().

    Args:
        client: Telethon TelegramClient (daemon owns the connection).
        conn: Open SQLite writer connection to sync.db.
        shutdown_event: asyncio.Event set when SIGTERM is received.
            Used to make FloodWait sleeps interruptible.
    """

    def __init__(
        self,
        client: object,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._client = cast(_SyncWorkerClient, client)
        self._conn = conn
        self._shutdown_event = shutdown_event

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def bootstrap_dms(self) -> int:
        """Enroll all DM dialogs into synced_dialogs with status='syncing'.

        Idempotent — uses INSERT OR IGNORE so existing rows (with real
        progress) are not overwritten.  Only types.User dialogs are
        enrolled; groups and channels require explicit opt-in (Phase 30).

        Handles FloodWaitError with interruptible sleep and RPCError
        gracefully — a transient Telegram error does not kill the daemon.

        Returns:
            Count of newly enrolled dialogs (0 if all already present).
        """
        enrolled = 0
        now = int(time.time())
        try:
            async for dialog in self._client.iter_dialogs():
                if not isinstance(dialog.entity, types.User):
                    continue
                cursor = self._conn.execute(INSERT_DIALOG_SQL, (dialog.id,))
                if cursor.rowcount > 0:
                    enrolled += 1
                entity = dialog.entity
                first = getattr(entity, "first_name", None) or ""
                last = getattr(entity, "last_name", None) or ""
                name: str | None = f"{first} {last}".strip() or None
                entity_type_str = classify_dialog_type(entity).value
                self._conn.execute(
                    UPSERT_ENTITY_SQL,
                    (
                        dialog.id,
                        entity_type_str,
                        name,
                        getattr(entity, "username", None),
                        latinize(name) if name else None,
                        now,
                    ),
                )
        except FloodWaitError as exc:
            wait_seconds = getattr(exc, "seconds", 60)
            logger.warning(
                "dm_bootstrap flood_wait=%ds enrolled_so_far=%d — committing partial progress",
                wait_seconds,
                enrolled,
            )
        except RPCError as exc:
            logger.warning(
                "dm_bootstrap rpc_error=%s enrolled_so_far=%d — committing partial progress",
                exc,
                enrolled,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "dm_bootstrap network_error=%s enrolled_so_far=%d — committing partial progress",
                exc,
                enrolled,
            )
        self._conn.commit()
        logger.info("dm_bootstrap enrolled=%d new DM dialogs", enrolled)
        return enrolled

    async def process_one_batch(self) -> bool:
        """Fetch one batch of messages for the next pending dialog.

        Picks the first dialog with status in ('syncing', 'not_synced'),
        fetches up to 100 messages from where it left off, stores them,
        and updates sync_progress atomically.

        Returns:
            True  — all dialogs are fully synced (idle mode safe).
            False — more work remains (same dialog or other pending dialogs).
        """
        pending = self._next_pending_dialog()
        if pending is None:
            return True  # nothing to do — all synced

        dialog_id, sync_progress = pending
        _, is_done = await self._fetch_batch(dialog_id, sync_progress)
        if not is_done:
            return False  # more batches needed for this dialog
        # Dialog done — check if more pending dialogs remain
        return self._next_pending_dialog() is None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_pending_dialog(self) -> tuple[int, int] | None:
        """Return (dialog_id, sync_progress) for the next pending dialog.

        Selects in rowid (insertion) order — no prioritization.
        Returns None when no dialogs have status in ('syncing', 'not_synced').
        """
        row = cast(tuple[int, int | None] | None, self._conn.execute(_NEXT_PENDING_SQL).fetchone())
        if row is None:
            return None
        return int(row[0]), int(row[1]) if row[1] is not None else 0

    async def _fetch_batch(self, dialog_id: int, sync_progress: int) -> tuple[int, bool]:
        """Fetch up to 100 messages for dialog_id older than sync_progress.

        Uses offset_id=sync_progress (exclusive) so each batch fetches
        messages strictly older than the last committed checkpoint.
        After a full batch (100 msgs), sync_progress advances to the min
        message_id; a partial or empty batch marks the dialog 'synced'.

        On FloodWaitError: sleep interruptibly, return (same_progress, False).
        On other RPCError: log ERROR, return (same_progress, False) — dialog stays
        in-progress for retry on the next sync cycle.

        Returns:
            (new_progress, is_done)
        """
        if sync_progress == 0:
            logger.info("sync_start dialog_id=%d", dialog_id)
        try:
            result = await self._client.get_messages(entity=dialog_id, limit=_BATCH_SIZE, offset_id=sync_progress)
            total_messages = result.total  # Telegram-side count from TotalList
            batch = list(result)
            # Note: batch size 100 keeps memory bounded; get_messages needed for .total
        except FloodWaitError as exc:
            logger.warning("FloodWait dialog_id=%d — sleeping %ds", dialog_id, exc.seconds)
            # Either outcome (shutdown or full sleep) retries the same batch on
            # the next call, so the shutdown signal is not distinguished here.
            await sleep_through_flood(self._shutdown_event, flood_seconds(exc))
            return sync_progress, False
        except _ACCESS_LOST_ERRORS as exc:
            logger.warning("access_lost dialog_id=%d — %s: %s", dialog_id, type(exc).__name__, exc)
            now = int(time.time())
            _set_access_lost(self._conn, dialog_id, now)
            return sync_progress, True
        except RPCError as exc:
            logger.exception(
                "sync_batch_rpc_error dialog_id=%d error=%s — dialog NOT marked synced, will retry",
                dialog_id,
                exc,
            )
            return sync_progress, False  # leave dialog in-progress for retry
        return await self._store_batch_page(dialog_id, sync_progress, total_messages, batch)

    async def _resolve_batch_entity_name_map(self, batch: Sequence[_MessageLike]) -> dict[int, str]:
        """Resolve forward source names for messages in a fetched batch."""
        return await resolve_forward_entity_name_map(batch, cast(PeerNameClient, self._client))

    async def _store_batch_page(
        self,
        dialog_id: int,
        sync_progress: int,
        total_messages: int,
        batch: Sequence[_MessageLike],
    ) -> tuple[int, bool]:
        """Persist one fetched batch and update sync progress."""
        if not batch:
            now = int(time.time())
            with self._conn:
                self._conn.execute(
                    _UPDATE_PROGRESS_DONE_SQL,
                    (sync_progress, "synced", total_messages, now, dialog_id),
                )
            logger.info("sync_done dialog_id=%d status=synced (empty batch)", dialog_id)
            return sync_progress, True

        # Resolve forward-source names from the batch entity cache.
        # Telegram includes users/chats for forward sources in the same
        # GetHistory response, so get_entity() hits the local cache — no
        # extra API round-trips in the common case.
        entity_name_map = await self._resolve_batch_entity_name_map(batch)

        rows = [extract_message_row(dialog_id, msg, entity_name_map=entity_name_map) for msg in batch]
        new_progress = min(msg.id for msg in batch)
        is_done = len(batch) < _BATCH_SIZE
        new_status = "synced" if is_done else "syncing"

        # Single atomic transaction: messages + FTS + progress update
        with self._conn:
            insert_messages_with_fts(self._conn, rows)
            if is_done:
                now = int(time.time())
                self._conn.execute(
                    _UPDATE_PROGRESS_DONE_SQL,
                    (new_progress, new_status, total_messages, now, dialog_id),
                )
            else:
                self._conn.execute(
                    _UPDATE_PROGRESS_SQL,
                    (new_progress, new_status, total_messages, dialog_id),
                )

        logger.debug(
            "sync_batch dialog_id=%d fetched=%d progress=%d done=%s",
            dialog_id,
            len(batch),
            new_progress,
            is_done,
        )
        if is_done:
            logger.info("sync_done dialog_id=%d status=synced total_messages=%d", dialog_id, total_messages)
        return new_progress, is_done


_EXPORTED_SYMBOLS = (
    FullSyncWorker,
    FullSyncWorker.bootstrap_dms,
    FullSyncWorker.process_one_batch,
)
