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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Protocol, cast

from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]

from .access_lifecycle import set_access_lost
from .entity_store import EntitySnapshot, upsert_entity_snapshots
from .flood import TelegramRpcThrottled, _raise_if_latched, sleep_through_flood
from .history_enrollment import ensure_automatic_dm_enrollment, full_history_enabled
from .hydration_queue import HydrationPriority
from .messages.sqlite_bundle import insert_messages_with_fts
from .messages.telegram_adapter import PeerNameClient, extract_message_row, resolve_forward_entity_name_map
from .resolver import latinize
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    rpc_scope,
)
from .telethon_dialog import classify_dialog_type

logger = logging.getLogger(__name__)
_BATCH_SIZE = 100
_DM_BOOTSTRAP_MAX_ADMISSION_WAIT_SECONDS = 30


def _full_sync_rpc_scope[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Give full/history synchronization an explicit RPC source."""

    @wraps(func)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with rpc_scope(TelegramRpcSource.FULL_SYNC):
            return await func(*args, **kwargs)

    return wrapped


_NEXT_PENDING_SQL = (
    "SELECT sd.dialog_id, sd.sync_progress FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.status IN ('syncing', 'not_synced') "
    "ORDER BY rowid LIMIT 1"
)
_UPDATE_PROGRESS_SQL = (
    "UPDATE synced_dialogs SET sync_progress = ?, status = ?, "
    "total_messages = COALESCE(?, total_messages) WHERE dialog_id = ? "
    "AND EXISTS (SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1)"
)
_UPDATE_PROGRESS_DONE_SQL = (
    "UPDATE synced_dialogs SET sync_progress = ?, status = ?, total_messages = COALESCE(?, total_messages), "
    "last_synced_at = ? WHERE dialog_id = ? "
    "AND EXISTS (SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1)"
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


@dataclass(frozen=True, slots=True)
class _FetchedBatchPage:
    total_messages: int | None
    batch: list[_MessageLike]
    retry: tuple[int, bool] | None = None


@dataclass(slots=True)
class _BootstrapProgress:
    enrolled: int = 0


def _enroll_dm_dialog(conn: sqlite3.Connection, dialog: _DialogLike, now: int) -> int:
    if not isinstance(dialog.entity, types.User):
        return 0
    outcome = ensure_automatic_dm_enrollment(conn, dialog.id, now=now)
    entity = dialog.entity
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    name: str | None = f"{first} {last}".strip() or None
    upsert_entity_snapshots(
        conn,
        (
            EntitySnapshot(
                entity_id=dialog.id,
                entity_type=classify_dialog_type(entity).value,
                name=name,
                username=getattr(entity, "username", None),
                name_normalized=latinize(name) if name else None,
                updated_at=now,
            ),
        ),
    )
    return int(outcome.action == "queue_full_history")


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

    async def _bootstrap_dm_pass(self, now: int, progress: _BootstrapProgress) -> None:
        async for dialog in self._client.iter_dialogs():
            progress.enrolled += _enroll_dm_dialog(self._conn, dialog, now)

    async def _retry_dm_bootstrap_admission(
        self, exc: TelegramRpcAdmissionDeferred, progress: _BootstrapProgress
    ) -> bool:
        self._conn.commit()
        retry_after = min(
            max(exc.retry_after_seconds or 1, 1),
            _DM_BOOTSTRAP_MAX_ADMISSION_WAIT_SECONDS,
        )
        logger.info(
            "dm_bootstrap admission_deferred retry_after=%s enrolled_so_far=%d — preserving enrollment progress",
            exc.retry_after_seconds,
            progress.enrolled,
        )
        return await sleep_through_flood(self._shutdown_event, retry_after)

    @_full_sync_rpc_scope
    async def bootstrap_dms(self) -> int:
        """Enroll all DM dialogs into synced_dialogs with status='syncing'.

        Idempotent — uses INSERT OR IGNORE so existing rows (with real
        progress) are not overwritten.  Only types.User dialogs are
        enrolled; groups and channels require explicit opt-in (Phase 30).

        Handles TelegramRpcThrottled with interruptible sleep and RPCError
        gracefully — a transient Telegram error does not kill the daemon.

        Returns:
            Count of newly enrolled dialogs (0 if all already present).
        """
        progress = _BootstrapProgress()
        now = int(time.time())
        while not self._shutdown_event.is_set():
            try:
                await self._bootstrap_dm_pass(now, progress)
                break
            except TelegramRpcAdmissionDeferred as exc:
                if await self._retry_dm_bootstrap_admission(exc, progress):
                    break
            except RpcAdmissionClosedError:
                self._conn.commit()
                logger.info("dm_bootstrap admission_closed enrolled_so_far=%d", progress.enrolled)
                raise
            except (TelegramRpcThrottled, RPCError) as exc:
                _raise_if_latched(exc)
                wait_seconds = getattr(exc, "retry_after_seconds", None)
                logger.warning(
                    "dm_bootstrap flood_wait=%ss enrolled_so_far=%d — committing partial progress",
                    wait_seconds,
                    progress.enrolled,
                )
                break
            except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
                logger.info(
                    "dm_bootstrap admission_deferred error_type=%s enrolled_so_far=%d — preserving enrollment progress",
                    type(exc).__name__,
                    progress.enrolled,
                )
                break
            except (TimeoutError, OSError) as exc:
                logger.warning(
                    "dm_bootstrap network_error=%s enrolled_so_far=%d — committing partial progress",
                    exc,
                    progress.enrolled,
                )
                break
        self._conn.commit()
        logger.info("dm_bootstrap enrolled=%d new DM dialogs", progress.enrolled)
        return progress.enrolled

    @_full_sync_rpc_scope
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

    async def _fetch_batch_page(self, dialog_id: int, sync_progress: int) -> _FetchedBatchPage:
        try:
            result = await self._client.get_messages(entity=dialog_id, limit=_BATCH_SIZE, offset_id=sync_progress)
            total_messages = result.total if sync_progress == 0 else None
            return _FetchedBatchPage(total_messages, list(result))
        except TelegramRpcAdmissionDeferred as exc:
            logger.info(
                "sync_batch admission_deferred dialog_id=%d retry_after=%s — preserving checkpoint",
                dialog_id,
                exc.retry_after_seconds,
            )
            if exc.retry_after_seconds is not None:
                await sleep_through_flood(self._shutdown_event, exc.retry_after_seconds)
            return _FetchedBatchPage(None, [], (sync_progress, False))
        except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            logger.info(
                "sync_batch admission_deferred dialog_id=%d error_type=%s — preserving checkpoint",
                dialog_id,
                type(exc).__name__,
            )
            return _FetchedBatchPage(None, [], (sync_progress, False))
        except TelegramRpcThrottled as exc:
            logger.warning("Telegram RPC throttled dialog_id=%d — retry_after=%s", dialog_id, exc.retry_after_seconds)
            if exc.retry_after_seconds is not None:
                await sleep_through_flood(self._shutdown_event, exc.retry_after_seconds)
            return _FetchedBatchPage(None, [], (sync_progress, False))
        except ACCESS_LOST_ERRORS as exc:
            now = int(time.time())
            set_access_lost(self._conn, dialog_id, now, reason=type(exc).__name__)
            self._conn.commit()
            return _FetchedBatchPage(None, [], (sync_progress, True))
        except RPCError as exc:
            logger.exception(
                "sync_batch_rpc_error dialog_id=%d error=%s — dialog NOT marked synced, will retry",
                dialog_id,
                exc,
            )
            return _FetchedBatchPage(None, [], (sync_progress, False))

    @_full_sync_rpc_scope
    async def _fetch_batch(self, dialog_id: int, sync_progress: int) -> tuple[int, bool]:
        """Fetch up to 100 messages for dialog_id older than sync_progress.

        Uses offset_id=sync_progress (exclusive) so each batch fetches
        messages strictly older than the last committed checkpoint.
        After a full batch (100 msgs), sync_progress advances to the min
        message_id; a partial or empty batch marks the dialog 'synced'.

        On TelegramRpcThrottled: sleep interruptibly, return (same_progress, False).
        On other RPCError: log ERROR, return (same_progress, False) — dialog stays
        in-progress for retry on the next sync cycle.

        Returns:
            (new_progress, is_done)
        """
        if not full_history_enabled(self._conn, dialog_id):
            return sync_progress, True
        if sync_progress == 0:
            logger.info("sync_start dialog_id=%d", dialog_id)
        page = await self._fetch_batch_page(dialog_id, sync_progress)
        if page.retry is not None:
            return page.retry
        return await self._store_batch_page(dialog_id, sync_progress, page.total_messages, page.batch)

    @_full_sync_rpc_scope
    async def _resolve_batch_entity_name_map(self, batch: Sequence[_MessageLike]) -> dict[int, str]:
        """Resolve forward source names for messages in a fetched batch."""
        return await resolve_forward_entity_name_map(batch, cast(PeerNameClient, self._client))

    @_full_sync_rpc_scope
    async def _store_batch_page(
        self,
        dialog_id: int,
        sync_progress: int,
        total_messages: int | None,
        batch: Sequence[_MessageLike],
    ) -> tuple[int, bool]:
        """Persist one fetched batch and update sync progress."""
        if not batch:
            now = int(time.time())
            with self._conn:
                self._conn.execute(
                    _UPDATE_PROGRESS_DONE_SQL,
                    (sync_progress, "synced", total_messages, now, dialog_id, dialog_id),
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
            if not full_history_enabled(self._conn, dialog_id):
                logger.info("sync_batch_discarded_disabled dialog_id=%d fetched=%d", dialog_id, len(rows))
                return sync_progress, True
            insert_messages_with_fts(self._conn, rows, priority=HydrationPriority.BACKFILL)
            if is_done:
                now = int(time.time())
                self._conn.execute(
                    _UPDATE_PROGRESS_DONE_SQL,
                    (new_progress, new_status, total_messages, now, dialog_id, dialog_id),
                )
            else:
                self._conn.execute(
                    _UPDATE_PROGRESS_SQL,
                    (new_progress, new_status, total_messages, dialog_id, dialog_id),
                )

        logger.debug(
            "sync_batch dialog_id=%d fetched=%d progress=%d done=%s",
            dialog_id,
            len(batch),
            new_progress,
            is_done,
        )
        if is_done:
            logger.info("sync_done dialog_id=%d status=synced total_messages=%s", dialog_id, total_messages)
        return new_progress, is_done


_EXPORTED_SYMBOLS = (
    FullSyncWorker,
    FullSyncWorker.bootstrap_dms,
    FullSyncWorker.process_one_batch,
)
