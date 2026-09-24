"""FullSyncWorker — bulk history fetch engine for v1.5 Persistent Sync.

Fetches all historical messages for marked dialogs in batches of 100,
checkpointing progress after each batch so restarts resume without
re-scanning from scratch.

FloodWait causes an interruptible sleep — progress is never lost on
rate limits.

DM enrollment consumes completed canonical dialog publications locally.

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
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Protocol, cast

from .access_lifecycle import set_access_lost
from .config import AutomaticGroupHistoryConfig
from .entity_store import EntitySnapshot, upsert_entity_stub
from .flood import TelegramRpcThrottled, _raise_if_latched, sleep_through_flood
from .history_enrollment import (
    ensure_automatic_dm_enrollment,
    full_history_enabled,
    read_intent,
    record_automatic_group_decision,
)
from .hydration_queue import HydrationPriority
from .message_contracts import ExtractedMessage as _ExtractedMessage
from .message_history.contracts import (
    MESSAGE_HISTORY_PAGE_SIZE,
    TOPIC_ATTRIBUTION_EXTRACTOR_VERSION,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from .message_history.ports import FullHistoryPagePort
from .messages.sqlite_bundle import insert_messages_with_fts
from .read_state import apply_read_cursor
from .resolver import latinize
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

logger = logging.getLogger(__name__)

_BATCH_SIZE = MESSAGE_HISTORY_PAGE_SIZE
_DM_ENROLLMENT_KEY_LAST_PUBLICATION_GENERATION = "full_sync_dm_enrollment_last_publication_generation"
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
    "AND NOT EXISTS (SELECT 1 FROM dialogs directory WHERE directory.dialog_id=sd.dialog_id AND directory.hidden=1) "
    "ORDER BY rowid LIMIT 1"
)
_NEXT_TOTAL_MESSAGES_REPAIR_SQL = (
    "SELECT sd.dialog_id FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.total_messages IS NULL AND sd.status NOT IN ('not_synced', 'access_lost') "
    "AND NOT EXISTS (SELECT 1 FROM dialogs directory WHERE directory.dialog_id=sd.dialog_id AND directory.hidden=1) "
    "ORDER BY sd.rowid LIMIT 1"
)
_UPDATE_TOTAL_MESSAGES_SQL = (
    "UPDATE synced_dialogs SET total_messages = ? WHERE dialog_id = ? AND total_messages IS NULL "
    "AND status NOT IN ('not_synced', ?) "
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


class TotalMessagesProbe(Protocol):
    """Single-message access probe used by total repair, outside page ports."""

    async def probe_total_messages(self, dialog_id: int) -> int | None: ...


@dataclass(frozen=True, slots=True)
class _FetchedBatchPage:
    total_messages: int | None
    batch: tuple[_ExtractedMessage, ...]
    retry: tuple[int, bool] | None = None
    reaction_observed_at: int | None = None


def _dm_enrollment_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = cast(
        tuple[str | None] | None, conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    )
    return None if row is None else row[0]


def _set_dm_enrollment_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO daemon_state(key, value) VALUES (?, ?)", (key, value))


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


# ---------------------------------------------------------------------------
# FullSyncWorker
# ---------------------------------------------------------------------------


class FullSyncWorker:
    """Core bulk-fetch engine for the v1.5 sync daemon.

    Fetches historical Telegram messages in batches and stores them in
    sync.db.  One instance is created per daemon run; it is called
    between heartbeat ticks in sync_main().

    Args:
        history_port: Transport-neutral backward history page port.
        conn: Open SQLite writer connection to sync.db.
        shutdown_event: asyncio.Event set when SIGTERM is received.
            Used to make FloodWait sleeps interruptible.
        total_messages_probe: Narrow access/total probe kept outside page ports.
    """

    def __init__(
        self,
        history_port: FullHistoryPagePort,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        *,
        total_messages_probe: TotalMessagesProbe | None = None,
        automatic_group_history: AutomaticGroupHistoryConfig | None = None,
    ) -> None:
        self._history_port = history_port
        self._total_messages_probe = total_messages_probe
        self._automatic_group_history = automatic_group_history or AutomaticGroupHistoryConfig()
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._last_page_error: BaseException | None = None
        self._last_total_repair_error: BaseException | None = None

    def _published_dm_generation(self) -> int | None:
        row = cast(
            tuple[int] | None,
            self._conn.execute(
                "SELECT generation FROM dialog_directory_publication WHERE singleton = 1 AND generation IS NOT NULL"
            ).fetchone(),
        )
        return None if row is None else int(row[0])

    def _last_consumed_dm_generation(self) -> int:
        value = _dm_enrollment_state(self._conn, _DM_ENROLLMENT_KEY_LAST_PUBLICATION_GENERATION)
        if value is None:
            return 0
        try:
            return int(value)
        except ValueError:
            logger.warning("dm_publication_consumed_generation_corrupt value=%r", value)
            return 0

    def dm_enrollment_pending(self) -> bool:
        """Return whether a completed canonical publication needs local consumption."""
        generation = self._published_dm_generation()
        return generation is not None and generation > self._last_consumed_dm_generation()

    def _upsert_local_entity_stub(self, dialog_id: int, dialog_type: str, name: str | None, now: int) -> None:
        """Create a local identity row while retaining richer existing fields."""
        upsert_entity_stub(
            self._conn,
            EntitySnapshot(
                entity_id=dialog_id,
                entity_type=dialog_type,
                name=name,
                username=None,
                name_normalized=latinize(name) if name else None,
                updated_at=now,
            ),
        )

    def _consume_one_canonical_dm(self, row: tuple[object, ...], now: int) -> int:
        dialog_id = int(cast(int, row[0]))
        dialog_type = str(row[1])
        name = cast(str | None, row[2])
        inbox = cast(int | None, row[3])
        outbox = cast(int | None, row[4])
        outcome = ensure_automatic_dm_enrollment(self._conn, dialog_id, now=now)
        self._upsert_local_entity_stub(dialog_id, dialog_type, name, now)
        if inbox is not None:
            apply_read_cursor(self._conn, dialog_id, "inbox", inbox)
        if outbox is not None:
            apply_read_cursor(self._conn, dialog_id, "outbox", outbox)
        return int(outcome.action == "queue_full_history")

    def consume_canonical_dm_publication(self) -> int:
        """Consume one completed canonical dialog publication entirely locally."""
        generation = self._published_dm_generation()
        if generation is None or generation <= self._last_consumed_dm_generation():
            return 0
        rows = cast(
            list[tuple[object, ...]],
            self._conn.execute(
                "SELECT dialog_id,type,name,read_inbox_max_id,read_outbox_max_id FROM dialogs "
                "WHERE type IN ('user','bot') AND identity_complete=1 AND hidden=0 ORDER BY dialog_id"
            ).fetchall(),
        )
        now = int(time.time())
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            enrolled = sum(self._consume_one_canonical_dm(row, now) for row in rows)
            _set_dm_enrollment_state(
                self._conn,
                _DM_ENROLLMENT_KEY_LAST_PUBLICATION_GENERATION,
                str(generation),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        logger.info("dm_publication_consumed generation=%d enrolled=%d", generation, enrolled)
        return enrolled

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
            return not await self._process_one_automatic_group()

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
        if self._total_messages_probe is None:
            error = RuntimeError("total-message repair requires an explicit access probe")
            self._last_total_repair_error = error
            return False
        try:
            total_messages = await self._total_messages_probe.probe_total_messages(dialog_id)
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
        except MessageHistoryAccessLostError as exc:
            set_access_lost(self._conn, dialog_id, int(time.time()), reason=exc.reason_code)
            self._conn.commit()
            return False
        except MessageHistoryUnavailableError as exc:
            logger.warning("sync_total_repair_failed dialog_id=%d error=%s", dialog_id, exc)
            self._set_total_repair_retry(None)
            self._last_total_repair_error = exc
            return False

        if total_messages is None:
            logger.warning("sync_total_repair_missing_total dialog_id=%d", dialog_id)
            self._set_total_repair_retry(None)
            return False
        with self._conn:
            self._conn.execute(
                _UPDATE_TOTAL_MESSAGES_SQL,
                (total_messages, dialog_id, "access_lost", dialog_id),
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

    def _next_automatic_group(self) -> int | None:
        cutoff = int(time.time()) - self._automatic_group_history.recent_days * 86_400
        return self._eligible_automatic_group(cutoff=cutoff)

    def _eligible_automatic_group(self, *, cutoff: int) -> int | None:
        row = cast(tuple[int] | None, self._conn.execute(
            "SELECT d.dialog_id FROM dialogs d "
            "LEFT JOIN full_history_enrollment fhe ON fhe.dialog_id=d.dialog_id "
            "LEFT JOIN synced_dialogs sd ON sd.dialog_id=d.dialog_id "
            "WHERE fhe.dialog_id IS NULL AND d.hidden=0 AND d.type IN ('group','supergroup','forum') "
            "AND d.members BETWEEN 1 AND ? AND d.created >= ? "
            "AND (sd.status IS NULL OR sd.status NOT IN ('access_lost', 'synced')) "
            "ORDER BY d.dialog_id LIMIT 1",
            (self._automatic_group_history.max_members, cutoff),
        ).fetchone())
        return None if row is None else int(row[0])

    def _is_eligible_automatic_group(self, dialog_id: int, *, cutoff: int) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM dialogs d LEFT JOIN full_history_enrollment fhe ON fhe.dialog_id=d.dialog_id "
            "LEFT JOIN synced_dialogs sd ON sd.dialog_id=d.dialog_id "
            "WHERE d.dialog_id=? AND fhe.dialog_id IS NULL AND d.hidden=0 "
            "AND d.type IN ('group','supergroup','forum') AND d.members BETWEEN 1 AND ? AND d.created >= ? "
            "AND (sd.status IS NULL OR sd.status NOT IN ('access_lost', 'synced'))",
            (dialog_id, self._automatic_group_history.max_members, cutoff),
        ).fetchone() is not None

    async def _process_one_automatic_group(self) -> bool:
        dialog_id = self._next_automatic_group()
        if dialog_id is None:
            return False
        page = await self._fetch_batch_page(dialog_id, 0)
        if page.retry is not None:
            return True
        with self._conn:
            intent = read_intent(self._conn, dialog_id)
            if intent.source is not None:
                if intent.enabled:
                    self._store_batch_page_locked(
                        dialog_id, 0, page.total_messages, page.batch, reaction_observed_at=page.reaction_observed_at
                    )
                return True
            if not self._is_eligible_automatic_group(
                dialog_id,
                cutoff=int(time.time()) - self._automatic_group_history.recent_days * 86_400,
            ):
                return True
            accepted = page.total_messages is not None and page.total_messages <= self._automatic_group_history.max_messages
            intent = record_automatic_group_decision(
                self._conn, dialog_id, enabled=accepted, total_messages=page.total_messages
            )
            if accepted and intent.enabled:
                self._store_batch_page_locked(
                    dialog_id, 0, page.total_messages, page.batch, reaction_observed_at=page.reaction_observed_at
                )
        return True

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
        return _FetchedBatchPage(None, (), (sync_progress, False))

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
            return _FetchedBatchPage(None, (), (sync_progress, False))
        if isinstance(exc, MessageHistoryAccessLostError):
            now = int(time.time())
            set_access_lost(self._conn, dialog_id, now, reason=exc.reason_code)
            self._conn.commit()
            return _FetchedBatchPage(None, (), (sync_progress, True))
        raise exc

    async def _fetch_batch_page(self, dialog_id: int, sync_progress: int) -> _FetchedBatchPage:
        self._last_page_error = None
        reaction_observed_at = int(time.time())
        try:
            page = await self._history_port.fetch_page(dialog_id, before_message_id=sync_progress)
        except MessageHistoryAccessLostError as exc:
            return await self._handle_batch_page_error(dialog_id, sync_progress, exc)
        except (TelegramRpcThrottled, RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            return await self._handle_batch_page_error(dialog_id, sync_progress, exc)
        except MessageHistoryUnavailableError as exc:
            logger.exception(
                "sync_batch_rpc_error dialog_id=%d error=%s — dialog NOT marked synced, will retry",
                dialog_id,
                exc,
            )
            self._last_page_error = exc
            return _FetchedBatchPage(None, (), (sync_progress, False))
        total_messages = page.total_messages if sync_progress == 0 else None
        return _FetchedBatchPage(total_messages, page.messages, reaction_observed_at=reaction_observed_at)

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_HISTORY_PAGE)
    async def _fetch_batch(self, dialog_id: int, sync_progress: int) -> tuple[int, bool]:
        """Fetch up to 100 messages for dialog_id older than sync_progress.

        Uses offset_id=sync_progress (exclusive) so each batch fetches
        messages strictly older than the last committed checkpoint.
        After a full batch (100 msgs), sync_progress advances to the min
        message_id; a partial or empty batch marks the dialog 'synced'.

        On TelegramRpcThrottled: sleep interruptibly, return (same_progress, False).
        On an ordinary remote error: log ERROR, return (same_progress, False) — dialog stays
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
        return await self._store_batch_page(
            dialog_id,
            sync_progress,
            page.total_messages,
            page.batch,
            reaction_observed_at=page.reaction_observed_at,
        )

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_HISTORY_PAGE)
    async def _store_batch_page(
        self,
        dialog_id: int,
        sync_progress: int,
        total_messages: int | None,
        batch: Sequence[_ExtractedMessage],
        *,
        reaction_observed_at: int | None = None,
    ) -> tuple[int, bool]:
        """Persist one fetched batch and update sync progress."""
        with self._conn:
            return self._store_batch_page_locked(
                dialog_id, sync_progress, total_messages, batch, reaction_observed_at=reaction_observed_at
            )

    def _store_batch_page_locked(
        self,
        dialog_id: int,
        sync_progress: int,
        total_messages: int | None,
        batch: Sequence[_ExtractedMessage],
        *,
        reaction_observed_at: int | None = None,
    ) -> tuple[int, bool]:
        if not batch:
            if not full_history_enabled(self._conn, dialog_id):
                logger.info("sync_batch_discarded_disabled dialog_id=%d fetched=0", dialog_id)
                return sync_progress, True
            now = int(time.time())
            self._begin_topic_attribution_pass(dialog_id, sync_progress, now)
            self._conn.execute(
                _UPDATE_PROGRESS_DONE_SQL,
                (sync_progress, "synced", total_messages, now, dialog_id, dialog_id),
            )
            self._complete_topic_attribution_pass(dialog_id, now)
            logger.info("sync_done dialog_id=%d status=synced (empty batch)", dialog_id)
            return sync_progress, True

        rows = list(batch)
        new_progress = min(item.message.message_id for item in batch)
        is_done = len(batch) < _BATCH_SIZE
        new_status = "synced" if is_done else "syncing"

        # Single atomic transaction: messages + FTS + progress update
        if not full_history_enabled(self._conn, dialog_id):
            logger.info("sync_batch_discarded_disabled dialog_id=%d fetched=%d", dialog_id, len(rows))
            return sync_progress, True
        now = int(time.time())
        self._begin_topic_attribution_pass(dialog_id, sync_progress, now)
        insert_messages_with_fts(
            self._conn,
            rows,
            priority=HydrationPriority.BACKFILL,
            reaction_observed_at=reaction_observed_at,
        )
        self._record_no_topic_attribution(dialog_id, rows)
        if is_done:
            self._conn.execute(
                _UPDATE_PROGRESS_DONE_SQL,
                (new_progress, new_status, total_messages, now, dialog_id, dialog_id),
            )
            self._complete_topic_attribution_pass(dialog_id, now)
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

    def _begin_topic_attribution_pass(self, dialog_id: int, sync_progress: int, observed_at: int) -> None:
        """Start or safely resume a current-extractor full-history receipt."""
        del sync_progress
        self._conn.execute(
            "UPDATE synced_dialogs SET topic_attribution_version=?, topic_attribution_state='partial', "
            "topic_attribution_observed_at=?, topic_attribution_completed_at=NULL, topic_attribution_no_topic_count=0 "
            "WHERE dialog_id=? AND status IN ('not_synced', 'syncing') "
            "AND (topic_attribution_version<>? OR topic_attribution_state<>'partial')",
            (TOPIC_ATTRIBUTION_EXTRACTOR_VERSION, observed_at, dialog_id, TOPIC_ATTRIBUTION_EXTRACTOR_VERSION),
        )

    def _record_no_topic_attribution(self, dialog_id: int, batch: Sequence[_ExtractedMessage]) -> None:
        """Record legal extractor NULL outcomes from one current history page."""
        count = sum(message.message.forum_topic_id is None for message in batch)
        if count:
            self._conn.execute(
                "UPDATE synced_dialogs SET topic_attribution_no_topic_count=topic_attribution_no_topic_count+? "
                "WHERE dialog_id=? AND topic_attribution_version=? AND topic_attribution_state='partial'",
                (count, dialog_id, TOPIC_ATTRIBUTION_EXTRACTOR_VERSION),
            )

    def _complete_topic_attribution_pass(self, dialog_id: int, completed_at: int) -> None:
        """Publish a full current-extractor traversal, including legal NULL outcomes."""
        self._conn.execute(
            "UPDATE synced_dialogs SET topic_attribution_state='complete', topic_attribution_completed_at=? "
            "WHERE dialog_id=? AND topic_attribution_version=? AND topic_attribution_state='partial'",
            (completed_at, dialog_id, TOPIC_ATTRIBUTION_EXTRACTOR_VERSION),
        )


class FullSyncDemandAdapter:
    """Bounded durable adapter over ``synced_dialogs.sync_progress``."""

    demand_kind = DemandKind.FULL_SYNC_PAGE

    def __init__(self, worker: FullSyncWorker) -> None:
        self._worker = worker

    def status(self, now: float) -> DemandStatus | None:
        """Report pending history without changing local or Telegram state."""
        if self._worker._next_pending_dialog() is not None:
            return DemandStatus(release_at=0.0)
        if self._worker._next_automatic_group() is not None:
            return DemandStatus(release_at=0.0)
        repair_release_at = self._worker._total_messages_repair_release_at(now)
        return None if repair_release_at is None else DemandStatus(release_at=repair_release_at)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Fetch at most one history page under the transport attempt budget."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        now = time.time()
        status = self.status(now)
        if status is None or not status.is_ready(now):
            return
        with demand_context(DemandKind.FULL_SYNC_PAGE):
            with rpc_attempt_budget(budget):
                if self._worker._next_pending_dialog() is not None:
                    await self._worker.process_one_batch()
                    if self._worker._last_page_error is not None:
                        raise self._worker._last_page_error
                elif self._worker._next_total_messages_repair_dialog() is not None:
                    await self._worker.repair_one_total_messages()
                    if self._worker._last_total_repair_error is not None:
                        raise self._worker._last_total_repair_error


class FullSyncDmEnrollmentDemandAdapter:
    """Consume completed canonical dialog publications into local sync state."""

    demand_kind = DemandKind.FULL_SYNC_DM_ENROLLMENT

    def __init__(self, worker: FullSyncWorker) -> None:
        self._worker = worker

    def status(self, now: float) -> DemandStatus | None:
        """Report an unconsumed completed publication without writing."""
        del now
        return DemandStatus(release_at=0.0) if self._worker.dm_enrollment_pending() else None

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Consume the pending publication without making a Telegram request."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        if self.status(time.time()) is None:
            return
        with demand_context(DemandKind.FULL_SYNC_DM_ENROLLMENT):
            with rpc_attempt_budget(budget):
                self._worker.consume_canonical_dm_publication()


_EXPORTED_SYMBOLS = (
    FullSyncDmEnrollmentDemandAdapter,
    FullSyncDemandAdapter,
    FullSyncWorker,
    FullSyncWorker.process_one_batch,
    FullSyncWorker.repair_one_total_messages,
)

__all__ = [
    "FullSyncDemandAdapter",
    "FullSyncDmEnrollmentDemandAdapter",
    "FullSyncWorker",
    "TotalMessagesProbe",
]
