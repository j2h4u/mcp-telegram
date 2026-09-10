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
import json
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
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
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    current_demand_token,
    demand_context,
)
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)
from .telethon_dialog import classify_dialog_type

logger = logging.getLogger(__name__)
_BATCH_SIZE = 100
_DM_ENROLLMENT_KEY_STATUS = "full_sync_dm_enrollment_status"
_DM_ENROLLMENT_KEY_OFFSET_DATE = "full_sync_dm_enrollment_offset_date"
_DM_ENROLLMENT_KEY_OFFSET_ID = "full_sync_dm_enrollment_offset_id"
_DM_ENROLLMENT_KEY_OFFSET_PEER = "full_sync_dm_enrollment_offset_peer"
_DM_ENROLLMENT_KEY_COMPLETED_AT = "full_sync_dm_enrollment_completed_at"
_DM_ENROLLMENT_CURSOR_KEYS = (
    _DM_ENROLLMENT_KEY_OFFSET_DATE,
    _DM_ENROLLMENT_KEY_OFFSET_ID,
    _DM_ENROLLMENT_KEY_OFFSET_PEER,
)
_DM_ENROLLMENT_IN_PROGRESS = "in_progress"
_DM_ENROLLMENT_COMPLETE = "complete"
_DM_BOOTSTRAP_MAX_ADMISSION_WAIT_SECONDS = 30
_TOTAL_MESSAGES_REPAIR_GATE_STATE_KEY = "full_sync_total_messages_repair_retry_at"
_TOTAL_MESSAGES_REPAIR_FAILURE_DELAY_S = 60


@contextmanager
def _full_sync_demand_scope(kind: DemandKind, acquisition_kind: AcquisitionKind) -> Iterator[None]:
    """Install a precise root for direct calls while preserving an adapter root."""
    try:
        current_demand_token()
    except UnclassifiedTelegramDemandError:
        with demand_context(kind):
            with acquisition_context(acquisition_kind):
                yield
    else:
        with acquisition_context(acquisition_kind):
            yield


def _full_sync_rpc_scope[**P, R](
    kind: DemandKind,
    acquisition_kind: AcquisitionKind,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Give full/history synchronization precise demand identity."""

    def decorate(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with _full_sync_demand_scope(kind, acquisition_kind):
                with rpc_scope(TelegramRpcSource.FULL_SYNC):
                    return await func(*args, **kwargs)

        return wrapped

    return decorate


_NEXT_PENDING_SQL = (
    "SELECT sd.dialog_id, sd.sync_progress FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.status IN ('syncing', 'not_synced') "
    "ORDER BY rowid LIMIT 1"
)
_NEXT_TOTAL_MESSAGES_REPAIR_SQL = (
    "SELECT sd.dialog_id FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.total_messages IS NULL AND sd.status NOT IN ('not_synced', 'access_lost') "
    "ORDER BY sd.rowid LIMIT 1"
)
_UPDATE_TOTAL_MESSAGES_SQL = (
    "UPDATE synced_dialogs SET total_messages = ? WHERE dialog_id = ? AND total_messages IS NULL "
    "AND status NOT IN ('not_synced', 'access_lost') "
    "AND EXISTS (SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1)"
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


def _dm_enrollment_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = cast(
        tuple[str | None] | None, conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    )
    return None if row is None else row[0]


def _set_dm_enrollment_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO daemon_state(key, value) VALUES (?, ?)", (key, value))


def _clear_dm_enrollment_cursor(conn: sqlite3.Connection) -> None:
    conn.executemany("DELETE FROM daemon_state WHERE key = ?", [(key,) for key in _DM_ENROLLMENT_CURSOR_KEYS])


def _total_messages_repair_retry_at(conn: sqlite3.Connection) -> int | None:
    value = _dm_enrollment_state(conn, _TOTAL_MESSAGES_REPAIR_GATE_STATE_KEY)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("sync_total_repair_retry_corrupt value=%r", value)
        return None


def _set_total_messages_repair_retry(conn: sqlite3.Connection, retry_at: int | None) -> None:
    _set_dm_enrollment_state(
        conn,
        _TOTAL_MESSAGES_REPAIR_GATE_STATE_KEY,
        None if retry_at is None else str(retry_at),
    )


def _encode_dm_enrollment_peer(entity: _EntityLike, dialog_id: int) -> str | None:
    entity_id = getattr(entity, "id", None)
    peer_id = entity_id if isinstance(entity_id, int) else dialog_id
    access_hash = getattr(entity, "access_hash", None) or 0
    if isinstance(entity, types.User):
        kind = "user"
    elif isinstance(entity, (types.Chat, types.ChatForbidden)):
        kind = "chat"
    elif isinstance(entity, (types.Channel, types.ChannelForbidden)):
        kind = "channel"
    else:
        return None
    return json.dumps({"type": kind, "id": peer_id, "access_hash": access_hash})


def _decode_dm_enrollment_peer(value: str) -> object:
    payload = cast(dict[str, object], json.loads(value))
    kind = payload["type"]
    peer_id = int(cast(int | str, payload["id"]))
    access_hash = int(cast(int | str, payload.get("access_hash", 0) or 0))
    if kind == "user":
        return types.InputPeerUser(peer_id, access_hash)
    if kind == "chat":
        return types.InputPeerChat(peer_id)
    if kind == "channel":
        return types.InputPeerChannel(peer_id, access_hash)
    raise ValueError(f"unknown DM enrollment peer type: {kind!r}")


def _dm_enrollment_offset_id(dialog: _DialogLike) -> int:
    if not hasattr(dialog, "message") or dialog.message is None:
        return 0
    return int(dialog.message.id)


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
        self._last_page_error: BaseException | None = None
        self._last_total_repair_error: BaseException | None = None
        self._last_dm_enrollment_error: BaseException | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _reconstruct_dm_enrollment_cursor(self) -> tuple[datetime | None, int, object | None]:
        offset_date_text = _dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_OFFSET_DATE)
        offset_id_text = _dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_OFFSET_ID)
        offset_peer_text = _dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_OFFSET_PEER)
        try:
            offset_date = datetime.fromisoformat(offset_date_text) if offset_date_text else None
            offset_id = int(offset_id_text) if offset_id_text else 0
            offset_peer = _decode_dm_enrollment_peer(offset_peer_text) if offset_peer_text else None
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("dm_bootstrap cursor corrupt (%s) — restarting traversal", exc)
            with self._conn:
                _clear_dm_enrollment_cursor(self._conn)
            return None, 0, None
        return offset_date, offset_id, offset_peer

    def _checkpoint_dm_enrollment(self, dialog: _DialogLike, now: int) -> int:
        with self._conn:
            enrolled = _enroll_dm_dialog(self._conn, dialog, now)
            dialog_date = getattr(dialog, "date", None)
            _set_dm_enrollment_state(
                self._conn,
                _DM_ENROLLMENT_KEY_OFFSET_DATE,
                dialog_date.isoformat() if isinstance(dialog_date, datetime) else None,
            )
            _set_dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_OFFSET_ID, str(_dm_enrollment_offset_id(dialog)))
            _set_dm_enrollment_state(
                self._conn,
                _DM_ENROLLMENT_KEY_OFFSET_PEER,
                _encode_dm_enrollment_peer(dialog.entity, int(dialog.id)),
            )
            _set_dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_STATUS, _DM_ENROLLMENT_IN_PROGRESS)
        return enrolled

    async def _bootstrap_dm_pass(self, now: int, progress: _BootstrapProgress) -> bool:
        offset_date, offset_id, offset_peer = self._reconstruct_dm_enrollment_cursor()
        cursor_present = any((offset_date is not None, offset_id != 0, offset_peer is not None))
        iterator_options: dict[str, object] = {}
        if cursor_present:
            iterator_options = {
                "offset_date": offset_date,
                "offset_id": offset_id,
                "offset_peer": offset_peer if offset_peer is not None else types.InputPeerEmpty(),
                "ignore_pinned": True,
            }
        async for dialog in self._client.iter_dialogs(**iterator_options):
            if self._shutdown_event.is_set():
                return False
            progress.enrolled += self._checkpoint_dm_enrollment(dialog, now)
        return True

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
        if current_rpc_scope().attempt_budget is not None:
            return True
        return await sleep_through_flood(self._shutdown_event, retry_after)

    def _prepare_dm_enrollment(self, *, start_new_if_complete: bool) -> bool:
        status = _dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_STATUS)
        if status == _DM_ENROLLMENT_COMPLETE:
            if not start_new_if_complete:
                return False
            with self._conn:
                _clear_dm_enrollment_cursor(self._conn)
                _set_dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_STATUS, _DM_ENROLLMENT_IN_PROGRESS)
        elif status != _DM_ENROLLMENT_IN_PROGRESS:
            with self._conn:
                _set_dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_STATUS, _DM_ENROLLMENT_IN_PROGRESS)
        return True

    async def _handle_dm_enrollment_error(self, exc: Exception, progress: _BootstrapProgress) -> bool | None:
        self._last_dm_enrollment_error = exc
        if isinstance(exc, TelegramRpcAdmissionDeferred):
            return False if await self._retry_dm_bootstrap_admission(exc, progress) else None
        if isinstance(exc, (TelegramRpcThrottled, RPCError)):
            _raise_if_latched(exc)
            wait_seconds = getattr(exc, "retry_after_seconds", None)
            logger.warning(
                "dm_bootstrap flood_wait=%ss enrolled_so_far=%d — committing partial progress",
                wait_seconds,
                progress.enrolled,
            )
            return False
        if isinstance(exc, (RpcAdmissionSaturatedError, RpcAdmissionExpiredError)):
            logger.info(
                "dm_bootstrap admission_deferred error_type=%s enrolled_so_far=%d — preserving enrollment progress",
                type(exc).__name__,
                progress.enrolled,
            )
            return False
        logger.warning(
            "dm_bootstrap network_error=%s enrolled_so_far=%d — committing partial progress",
            exc,
            progress.enrolled,
        )
        return False

    async def _run_dm_enrollment_passes(self, now: int, progress: _BootstrapProgress) -> bool:
        while not self._shutdown_event.is_set():
            try:
                return await self._bootstrap_dm_pass(now, progress)
            except RpcAdmissionClosedError:
                self._conn.commit()
                logger.info("dm_bootstrap admission_closed enrolled_so_far=%d", progress.enrolled)
                raise
            except (
                TelegramRpcAdmissionDeferred,
                TelegramRpcThrottled,
                RPCError,
                RpcAdmissionSaturatedError,
                RpcAdmissionExpiredError,
                TimeoutError,
                OSError,
            ) as exc:
                outcome = await self._handle_dm_enrollment_error(exc, progress)
                if outcome is not None:
                    return outcome
                self._last_dm_enrollment_error = None
        return False

    def _finish_dm_enrollment(self, completed: bool, progress: _BootstrapProgress) -> int:
        if completed:
            with self._conn:
                _clear_dm_enrollment_cursor(self._conn)
                _set_dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_STATUS, _DM_ENROLLMENT_COMPLETE)
                _set_dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_COMPLETED_AT, str(int(time.time())))
        else:
            self._conn.commit()
        logger.info("dm_bootstrap enrolled=%d new DM dialogs", progress.enrolled)
        return progress.enrolled

    async def _run_dm_enrollment(self, *, start_new_if_complete: bool) -> int:
        self._last_dm_enrollment_error = None
        if not self._prepare_dm_enrollment(start_new_if_complete=start_new_if_complete):
            return 0

        progress = _BootstrapProgress()
        now = int(time.time())
        completed = await self._run_dm_enrollment_passes(now, progress)
        return self._finish_dm_enrollment(completed, progress)

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_DM_ENROLLMENT, AcquisitionKind.DIALOG_TRAVERSAL)
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
        return await self._run_dm_enrollment(start_new_if_complete=True)

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_DM_ENROLLMENT, AcquisitionKind.DIALOG_TRAVERSAL)
    async def resume_dm_enrollment(self) -> int:
        """Resume a coordinator slice without reopening a completed cycle."""
        return await self._run_dm_enrollment(start_new_if_complete=False)

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_HISTORY_PAGE)
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

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_LOOKUP)
    async def repair_one_total_messages(self) -> bool:  # noqa: PLR0911
        """Repair one missing Telegram history total under a single RPC budget."""
        self._last_total_repair_error = None
        dialog_id = self._next_total_messages_repair_dialog()
        if dialog_id is None:
            return True
        try:
            result = await self._client.get_messages(entity=dialog_id, limit=1)
        except RpcAttemptBudgetExhaustedError:
            raise
        except RpcAdmissionClosedError:
            raise
        except (TelegramRpcAdmissionDeferred, RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            logger.info(
                "sync_total_repair admission_deferred dialog_id=%d error_type=%s",
                dialog_id,
                type(exc).__name__,
            )
            self._set_total_repair_retry(getattr(exc, "retry_after_seconds", None))
            self._last_total_repair_error = exc
            return False
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            logger.warning(
                "sync_total_repair flood_wait dialog_id=%d seconds=%s",
                dialog_id,
                exc.retry_after_seconds,
            )
            self._set_total_repair_retry(exc.retry_after_seconds)
            self._last_total_repair_error = exc
            return False
        except ACCESS_LOST_ERRORS as exc:
            set_access_lost(self._conn, dialog_id, int(time.time()), reason=type(exc).__name__)
            self._conn.commit()
            return False
        except (RPCError, TimeoutError, OSError) as exc:
            logger.warning("sync_total_repair_failed dialog_id=%d error=%s", dialog_id, exc)
            self._set_total_repair_retry(None)
            self._last_total_repair_error = exc
            return False

        total_messages = cast(int | None, getattr(result, "total", None))
        if total_messages is None:
            logger.warning("sync_total_repair_missing_total dialog_id=%d", dialog_id)
            self._set_total_repair_retry(None)
            return False
        with self._conn:
            self._conn.execute(
                _UPDATE_TOTAL_MESSAGES_SQL,
                (total_messages, dialog_id, dialog_id),
            )
            _set_total_messages_repair_retry(self._conn, None)
        logger.info("sync_total_repair_complete dialog_id=%d total_messages=%d", dialog_id, total_messages)
        return self._next_total_messages_repair_dialog() is None

    def _set_total_repair_retry(self, retry_after_seconds: object | None) -> None:
        try:
            failure_delay = max(1, int(cast(float, retry_after_seconds))) if retry_after_seconds is not None else 0
        except TypeError, ValueError:
            failure_delay = 0
        release_at = int(time.time()) + max(_TOTAL_MESSAGES_REPAIR_FAILURE_DELAY_S, failure_delay)
        with self._conn:
            _set_total_messages_repair_retry(self._conn, release_at)

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

    def _next_total_messages_repair_dialog(self) -> int | None:
        """Return the next enrolled accessible dialog missing its Telegram total."""
        retry_at = _total_messages_repair_retry_at(self._conn)
        if retry_at is not None and retry_at > int(time.time()):
            return None
        row = cast(tuple[int] | None, self._conn.execute(_NEXT_TOTAL_MESSAGES_REPAIR_SQL).fetchone())
        return None if row is None else int(row[0])

    def _total_messages_repair_release_at(self, now: float) -> float | None:
        row = cast(tuple[int] | None, self._conn.execute(_NEXT_TOTAL_MESSAGES_REPAIR_SQL).fetchone())
        if row is None:
            return None
        retry_at = _total_messages_repair_retry_at(self._conn)
        if retry_at is None or retry_at <= now:
            return 0.0
        return float(retry_at)

    async def _sleep_for_batch_retry(self, retry_after_seconds: int | None) -> None:
        if retry_after_seconds is not None and current_rpc_scope().attempt_budget is None:
            await sleep_through_flood(self._shutdown_event, retry_after_seconds)

    def _deferred_batch_page(self, dialog_id: int, sync_progress: int, exc: Exception) -> _FetchedBatchPage:
        if isinstance(exc, TelegramRpcAdmissionDeferred):
            logger.info(
                "sync_batch admission_deferred dialog_id=%d retry_after=%s — preserving checkpoint",
                dialog_id,
                exc.retry_after_seconds,
            )
        else:
            logger.info(
                "sync_batch admission_deferred dialog_id=%d error_type=%s — preserving checkpoint",
                dialog_id,
                type(exc).__name__,
            )
        self._last_page_error = exc
        return _FetchedBatchPage(None, [], (sync_progress, False))

    async def _handle_batch_page_error(
        self,
        dialog_id: int,
        sync_progress: int,
        exc: Exception,
    ) -> _FetchedBatchPage:
        if isinstance(exc, TelegramRpcAdmissionDeferred):
            await self._sleep_for_batch_retry(exc.retry_after_seconds)
            return self._deferred_batch_page(dialog_id, sync_progress, exc)
        if isinstance(exc, (RpcAdmissionSaturatedError, RpcAdmissionExpiredError)):
            return self._deferred_batch_page(dialog_id, sync_progress, exc)
        if isinstance(exc, TelegramRpcThrottled):
            logger.warning("Telegram RPC throttled dialog_id=%d — retry_after=%s", dialog_id, exc.retry_after_seconds)
            await self._sleep_for_batch_retry(exc.retry_after_seconds)
            self._last_page_error = exc
            return _FetchedBatchPage(None, [], (sync_progress, False))
        if isinstance(exc, ACCESS_LOST_ERRORS):
            now = int(time.time())
            set_access_lost(self._conn, dialog_id, now, reason=type(exc).__name__)
            self._conn.commit()
            return _FetchedBatchPage(None, [], (sync_progress, True))
        raise exc

    async def _fetch_batch_page(self, dialog_id: int, sync_progress: int) -> _FetchedBatchPage:
        self._last_page_error = None
        try:
            result = await self._client.get_messages(entity=dialog_id, limit=_BATCH_SIZE, offset_id=sync_progress)
        except ACCESS_LOST_ERRORS as exc:
            return await self._handle_batch_page_error(dialog_id, sync_progress, exc)
        except (TelegramRpcThrottled, RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            return await self._handle_batch_page_error(dialog_id, sync_progress, exc)
        except RPCError as exc:
            logger.exception(
                "sync_batch_rpc_error dialog_id=%d error=%s — dialog NOT marked synced, will retry",
                dialog_id,
                exc,
            )
            self._last_page_error = exc
            return _FetchedBatchPage(None, [], (sync_progress, False))
        total_messages = result.total if sync_progress == 0 else None
        return _FetchedBatchPage(total_messages, list(result))

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_HISTORY_PAGE)
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

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.ENTITY_LOOKUP)
    async def _resolve_batch_entity_name_map(self, batch: Sequence[_MessageLike]) -> dict[int, str]:
        """Resolve forward source names for messages in a fetched batch."""
        return await resolve_forward_entity_name_map(batch, cast(PeerNameClient, self._client))

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_HISTORY_PAGE)
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


class FullSyncDemandAdapter:
    """Bounded durable adapter over ``synced_dialogs.sync_progress``."""

    demand_kind = DemandKind.FULL_SYNC_PAGE

    def __init__(self, worker: FullSyncWorker) -> None:
        self._worker = worker

    def status(self, now: float) -> DemandStatus | None:
        """Report pending history without changing local or Telegram state."""
        if self._worker._next_pending_dialog() is not None:
            return DemandStatus(release_at=0.0)
        repair_release_at = self._worker._total_messages_repair_release_at(now)
        if repair_release_at is None:
            return None
        return DemandStatus(release_at=repair_release_at)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Fetch at most one history page under the transport attempt budget."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        status = self.status(time.time())
        if status is None or not status.is_ready(time.time()):
            return
        with demand_context(DemandKind.FULL_SYNC_PAGE):
            with rpc_attempt_budget(budget):
                try:
                    if self._worker._next_pending_dialog() is not None:
                        await self._worker.process_one_batch()
                        if self._worker._last_page_error is not None:
                            raise self._worker._last_page_error
                    else:
                        await self._worker.repair_one_total_messages()
                        if self._worker._last_total_repair_error is not None:
                            raise self._worker._last_total_repair_error
                except RpcAttemptBudgetExhaustedError:
                    return


class FullSyncDmEnrollmentDemandAdapter:
    """Bounded DM enrollment traversal over its committed daemon-state cursor."""

    demand_kind = DemandKind.FULL_SYNC_DM_ENROLLMENT

    def __init__(self, worker: FullSyncWorker) -> None:
        self._worker = worker
        self._startup_cycle_started = False

    def status(self, now: float) -> DemandStatus | None:
        """Report incomplete enrollment without scanning Telegram or writing."""
        del now
        status = _dm_enrollment_state(self._worker._conn, _DM_ENROLLMENT_KEY_STATUS)
        if status == _DM_ENROLLMENT_COMPLETE and self._startup_cycle_started:
            return None
        return DemandStatus(release_at=0.0)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Resume traversal until completion or the sender exhausts the slice."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        if self.status(time.time()) is None:
            return
        self._startup_cycle_started = True
        with demand_context(DemandKind.FULL_SYNC_DM_ENROLLMENT):
            with rpc_attempt_budget(budget):
                try:
                    await self._worker.bootstrap_dms()
                    if self._worker._last_dm_enrollment_error is not None:
                        raise self._worker._last_dm_enrollment_error
                except RpcAttemptBudgetExhaustedError:
                    return


_EXPORTED_SYMBOLS = (
    FullSyncDmEnrollmentDemandAdapter,
    FullSyncDemandAdapter,
    FullSyncWorker,
    FullSyncWorker.bootstrap_dms,
    FullSyncWorker.process_one_batch,
    FullSyncWorker.repair_one_total_messages,
    FullSyncWorker.resume_dm_enrollment,
)
