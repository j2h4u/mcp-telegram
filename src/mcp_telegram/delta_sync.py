"""DeltaSyncWorker — forward gap-fill engine for v1.5 Persistent Sync.

Fetches messages newer than the max known message_id per dialog in bounded
maintenance cycles. Idempotent: dialogs with no gap complete instantly when
iter_messages returns empty.

Architecture:
- Mirrors FullSyncWorker structural pattern (client/conn/shutdown_event).
- Fetches FORWARD (min_id + reverse=True) vs FullSyncWorker's backward.
- Runs as a paced background maintenance loop, not as a blocking startup sweep.
- Only processes dialogs with status='synced' — FullSyncWorker handles
  'syncing' and 'not_synced' dialogs.
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import wraps
from typing import Protocol, TypedDict, Unpack, cast

from telethon.errors import RPCError  # type: ignore[import-untyped]

from .access_lifecycle import (
    due_access_revalidations,
    restore_access_after_revalidation,
    set_access_lost,
    stamp_access_revalidation,
)
from .flood import TelegramRpcThrottled, _raise_if_latched, sleep_through_flood
from .history_enrollment import full_history_enabled
from .hydration_queue import HydrationPriority
from .maintenance_logging import log_maintenance_cycle
from .message_contracts import ExtractedMessage
from .messages.sqlite_bundle import insert_messages_with_fts
from .messages.telegram_adapter import extract_message_row
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
    rpc_attempt_budget,
    rpc_scope,
)

logger = logging.getLogger(__name__)


@contextmanager
def _delta_demand_scope(kind: DemandKind, acquisition_kind: AcquisitionKind) -> Iterator[None]:
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


def _delta_rpc_scope[**P, R](
    kind: DemandKind,
    acquisition_kind: AcquisitionKind,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Give delta and access-recovery operations precise demand identity."""

    def decorate(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with _delta_demand_scope(kind, acquisition_kind):
                with rpc_scope(TelegramRpcSource.DELTA_SYNC):
                    return await func(*args, **kwargs)

        return wrapped

    return decorate


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeltaCatchUpPolicy:
    """Bounded background policy for forward gap-fill probes."""

    interval_seconds: float
    max_probes_per_cycle: int
    probe_pause_seconds: float

    @property
    def enabled(self) -> bool:
        return self.max_probes_per_cycle > 0

    def probe_budget_exhausted(self, probed: int) -> bool:
        return probed >= self.max_probes_per_cycle


@dataclass(frozen=True, slots=True)
class AccessProbePolicy:
    """Budgeted cold policy for access-lost archive revalidation."""

    interval_seconds: float
    max_dialogs_per_cycle: int
    cooldown_seconds: int
    probe_pause_seconds: float

    @property
    def enabled(self) -> bool:
        return self.max_dialogs_per_cycle > 0


# Skip delta probe for dialogs fully synced within this window — prevents
# GetHistoryRequest storm on quick restarts (D-01 expert panel).
# 1h covers typical dev iteration cycles (rebuild + edit + rebuild) where
# the user's account hasn't received meaningful new traffic worth probing.
RECENT_SYNC_SKIP_THRESHOLD_S: int = 3600
_DELTA_SLICE_MESSAGE_LIMIT = 100

_SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL = """
SELECT sd.dialog_id, sd.last_synced_at, sd.last_delta_checked_at, sd.delta_refresh_requested_at
FROM synced_dialogs sd
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
WHERE sd.status = 'synced'
ORDER BY
    CASE WHEN sd.delta_refresh_requested_at IS NULL THEN 1 ELSE 0 END,
    COALESCE(sd.delta_refresh_requested_at, sd.last_delta_checked_at, sd.last_synced_at, 0),
    sd.dialog_id
"""
_SELECT_MAX_MESSAGE_ID_SQL = "SELECT COALESCE(MAX(message_id), 0) FROM messages WHERE dialog_id = ?"
_SELECT_DELTA_OBSERVABILITY_SQL = """
SELECT
    COUNT(*) AS total_synced,
    SUM(last_delta_checked_at IS NOT NULL) AS checked_total,
    SUM(last_delta_checked_at IS NULL) AS never_checked,
    MIN(last_delta_checked_at) AS oldest_delta_checked_at,
    MAX(last_delta_checked_at) AS newest_delta_checked_at,
    SUM(delta_refresh_requested_at IS NOT NULL) AS pending_refresh
FROM synced_dialogs sd
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
WHERE sd.status = 'synced'
"""

# Stamp delta checkpoint on successful delta completion.
# Distinct from FullSyncWorker's _UPDATE_PROGRESS_DONE_SQL (different column set).
_UPDATE_DELTA_CHECKPOINT_SQL = (
    "UPDATE synced_dialogs "
    "SET last_synced_at = ?, last_delta_checked_at = ?, delta_refresh_requested_at = NULL "
    "WHERE dialog_id = ? AND EXISTS (SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1)"
)
_UPDATE_DELTA_CHECKED_SQL = (
    "UPDATE synced_dialogs SET last_delta_checked_at = ?, delta_refresh_requested_at = NULL WHERE dialog_id = ? "
    "AND EXISTS (SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1)"
)
_REQUEST_DELTA_CONTINUATION_SQL = (
    "UPDATE synced_dialogs SET delta_refresh_requested_at = COALESCE(delta_refresh_requested_at, ?) "
    "WHERE dialog_id = ? "
    "AND EXISTS (SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1)"
)


class AccessProbeLoopOptions(TypedDict, total=False):
    initial_delay: float


class _DeltaSyncClient(Protocol):
    def iter_messages(self, **_kwargs: object) -> AsyncIterator[object]: ...

    async def get_messages(self, **_kwargs: object) -> object: ...


@dataclass(frozen=True, slots=True)
class _DeltaFetchOutcome:
    rows: list[ExtractedMessage]
    result: int | None = None


@dataclass(frozen=True, slots=True)
class _AccessProbeOutcome:
    restored: int = 0
    still_lost: int = 0
    errors: int = 0
    flood_wait_hit: bool = False
    admission_deferred: bool = False
    counts_as_checked: bool = True


@dataclass(frozen=True, slots=True)
class _AccessProbeRequest:
    client: _DeltaSyncClient
    conn: sqlite3.Connection
    shutdown_event: asyncio.Event
    delta_worker: DeltaSyncWorker
    policy: AccessProbePolicy
    dialog_id: int


def _access_probe_rows(conn: sqlite3.Connection, policy: AccessProbePolicy, now: int) -> list[tuple[int]]:
    return [
        (dialog_id,)
        for dialog_id in due_access_revalidations(
            conn,
            now=now,
            cooldown_seconds=policy.cooldown_seconds,
            limit=policy.max_dialogs_per_cycle,
        )
    ]


def _row_first_int(row: tuple[object | None, ...] | None) -> int:
    if row is None:
        return 0
    value = row[0]
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


def _object_to_int_or_none(value: object | None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _object_to_int(value: object | None) -> int:
    parsed = _object_to_int_or_none(value)
    return parsed if parsed is not None else 0


def _recently_synced(last_synced_at: int | None, now: int) -> bool:
    return last_synced_at is not None and (now - last_synced_at) < RECENT_SYNC_SKIP_THRESHOLD_S


@dataclass(frozen=True, slots=True)
class DeltaCatchUpObservability:
    """Aggregate local progress state for the bounded delta catch-up loop."""

    total_synced: int
    checked_total: int
    never_checked: int
    pending_refresh: int
    oldest_delta_checked_age_s: int | None
    newest_delta_checked_age_s: int | None


def _delta_skip_anchor(last_synced_at: int | None, last_delta_checked_at: int | None) -> int | None:
    """Return the local recency anchor used by the delta quick-restart guard."""
    if last_delta_checked_at is not None:
        return last_delta_checked_at
    return last_synced_at


def _delta_release_at(
    last_synced_at: int | None,
    last_delta_checked_at: int | None,
    refresh_requested_at: int | None,
) -> float:
    if refresh_requested_at is not None:
        return float(refresh_requested_at)
    anchor = _delta_skip_anchor(last_synced_at, last_delta_checked_at)
    if anchor is None:
        return 0.0
    return float(anchor + RECENT_SYNC_SKIP_THRESHOLD_S)


def _remaining_probe_candidates(*, total_rows: int, skipped: int, probed: int) -> int:
    return max(0, total_rows - skipped - probed)


def _log_probe_budget_exhausted(policy: DeltaCatchUpPolicy, *, total_rows: int, skipped: int, probed: int) -> None:
    # A bounded cycle reaching its probe budget is normal while backlog is
    # being drained. The complete-cycle INFO summary below retains aggregate
    # coverage/backlog evidence; keep this per-cycle detail available at DEBUG
    # without emitting an INFO line forever.
    logger.debug(
        "delta_catch_up_probe_budget_exhausted max_probes=%d remaining=%d",
        policy.max_probes_per_cycle,
        _remaining_probe_candidates(total_rows=total_rows, skipped=skipped, probed=probed),
    )


def _delta_checked_age(timestamp: int | None, now: int) -> int | None:
    if timestamp is None:
        return None
    return max(0, now - timestamp)


async def _pause_after_probe(shutdown_event: asyncio.Event, policy: DeltaCatchUpPolicy | None) -> bool:
    if policy is None or policy.probe_pause_seconds <= 0 or shutdown_event.is_set():
        return False
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=policy.probe_pause_seconds)
        return True
    except TimeoutError:
        return False


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
        client: _DeltaSyncClient,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._client = client
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._last_fetch_admission_deferred = False

    def _stamp_delta_checkpoint(self, dialog_id: int, checked_at: int) -> None:
        self._conn.execute(_UPDATE_DELTA_CHECKPOINT_SQL, (checked_at, checked_at, dialog_id, dialog_id))

    def _stamp_delta_checked(self, dialog_id: int, checked_at: int) -> None:
        self._conn.execute(_UPDATE_DELTA_CHECKED_SQL, (checked_at, dialog_id, dialog_id))

    def _delta_observability(self, now: int) -> DeltaCatchUpObservability:
        row = cast(
            tuple[object | None, object | None, object | None, object | None, object | None, object | None] | None,
            self._conn.execute(_SELECT_DELTA_OBSERVABILITY_SQL).fetchone(),
        )
        if row is None:
            return DeltaCatchUpObservability(
                total_synced=0,
                checked_total=0,
                never_checked=0,
                pending_refresh=0,
                oldest_delta_checked_age_s=None,
                newest_delta_checked_age_s=None,
            )
        oldest_checked_at = _object_to_int_or_none(row[3])
        newest_checked_at = _object_to_int_or_none(row[4])
        return DeltaCatchUpObservability(
            total_synced=_object_to_int(row[0]),
            checked_total=_object_to_int(row[1]),
            never_checked=_object_to_int(row[2]),
            pending_refresh=_object_to_int(row[5]),
            oldest_delta_checked_age_s=_delta_checked_age(oldest_checked_at, now),
            newest_delta_checked_age_s=_delta_checked_age(newest_checked_at, now),
        )

    @_delta_rpc_scope(DemandKind.DELTA_GAP_FILL, AcquisitionKind.MESSAGE_HISTORY_PAGE)
    async def run_delta_catch_up(
        self,
        *,
        policy: DeltaCatchUpPolicy | None = None,
    ) -> int:
        """Fetch messages newer than max known id for all 'synced' dialogs.

        Returns:
            Total count of new messages stored across all dialogs.

        Idempotent: dialogs with no gap complete instantly (empty first
        batch from iter_messages). Skips dialogs with no baseline
        (max_known_id=0) — FullSyncWorker handles those.

        Quick-restart guard: dialogs whose last delta check or completed sync
        is within RECENT_SYNC_SKIP_THRESHOLD_S are skipped to prevent a
        GetHistoryRequest storm after a rapid daemon restart (D-01). Explicit
        refresh requests bypass this skip once, but still consume the same
        bounded probe budget.
        """
        rows = cast(
            list[tuple[int, int | None, int | None, int | None]],
            self._conn.execute(_SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL).fetchall(),
        )
        now = int(time.time())
        total_new = 0
        skipped = 0
        probed = 0
        for dialog_id, last_synced_at, last_delta_checked_at, refresh_requested_at in rows:
            if self._shutdown_event.is_set():
                break
            if policy is not None and policy.probe_budget_exhausted(probed):
                _log_probe_budget_exhausted(policy, total_rows=len(rows), skipped=skipped, probed=probed)
                break
            skip_anchor = _delta_skip_anchor(last_synced_at, last_delta_checked_at)
            if refresh_requested_at is None and _recently_synced(skip_anchor, now):
                assert skip_anchor is not None
                age_s = now - skip_anchor
                # DEBUG, not INFO — with 300+ skipped dialogs this floods the
                # log and obscures real signal. The aggregate count lives in
                # the delta_catch_up complete summary at the end of this loop.
                logger.debug(
                    "delta_catch_up_skip dialog_id=%d age_s=%d",
                    dialog_id,
                    age_s,
                )
                skipped += 1
                continue
            probed += 1
            total_new += await self.fetch_delta_for_dialog(dialog_id)
            if self._last_fetch_admission_deferred:
                probed -= 1
                break
            if await _pause_after_probe(self._shutdown_event, policy):
                break
        observability = self._delta_observability(int(time.time()))
        log_maintenance_cycle(
            logger,
            any((total_new, observability.never_checked, observability.pending_refresh)),
            "delta_catch_up complete — new_messages=%d skipped=%d probed=%d "
            "total_synced=%d checked_total=%d never_checked=%d pending_refresh=%d "
            "oldest_delta_checked_age_s=%s newest_delta_checked_age_s=%s",
            total_new,
            skipped,
            probed,
            observability.total_synced,
            observability.checked_total,
            observability.never_checked,
            observability.pending_refresh,
            observability.oldest_delta_checked_age_s,
            observability.newest_delta_checked_age_s,
        )
        return total_new

    async def _handle_delta_throttling(
        self, dialog_id: int, new_message_rows: list[ExtractedMessage], exc: TelegramRpcThrottled
    ) -> int:
        _raise_if_latched(exc)
        logger.warning(
            "FloodWait delta dialog_id=%d — %ss (preserving %d already-fetched messages)",
            dialog_id,
            exc.retry_after_seconds,
            len(new_message_rows),
        )
        now = int(time.time())
        with self._conn:
            if not full_history_enabled(self._conn, dialog_id):
                logger.info("delta_discarded_disabled dialog_id=%d fetched=%d", dialog_id, len(new_message_rows))
                return 0
            if new_message_rows:
                insert_messages_with_fts(self._conn, new_message_rows, priority=HydrationPriority.BACKFILL)
            self._stamp_delta_checkpoint(dialog_id, now)
        if new_message_rows:
            logger.info("delta dialog_id=%d preserved_messages=%d before FloodWait", dialog_id, len(new_message_rows))
        await sleep_through_flood(self._shutdown_event, exc.retry_after_seconds or 1)
        return len(new_message_rows)

    async def _collect_delta_rows(self, dialog_id: int, max_known_id: int) -> _DeltaFetchOutcome:
        new_message_rows: list[ExtractedMessage] = []
        try:
            async for msg in self._client.iter_messages(
                entity=dialog_id, min_id=max_known_id, reverse=True, limit=None
            ):
                if self._shutdown_event.is_set():
                    break
                new_message_rows.append(extract_message_row(dialog_id, msg))
        except TelegramRpcAdmissionDeferred as exc:
            self._last_fetch_admission_deferred = True
            logger.info(
                "delta admission_deferred dialog_id=%d retry_after=%s — preserving checkpoint",
                dialog_id,
                exc.retry_after_seconds,
            )
            if exc.retry_after_seconds is not None:
                await sleep_through_flood(self._shutdown_event, exc.retry_after_seconds)
            return _DeltaFetchOutcome([], 0)
        except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            self._last_fetch_admission_deferred = True
            logger.info(
                "delta admission_deferred dialog_id=%d error_type=%s — preserving checkpoint",
                dialog_id,
                type(exc).__name__,
            )
            return _DeltaFetchOutcome([], 0)
        except TelegramRpcThrottled as exc:
            return _DeltaFetchOutcome([], await self._handle_delta_throttling(dialog_id, new_message_rows, exc))
        except ACCESS_LOST_ERRORS as exc:
            now = int(time.time())
            set_access_lost(self._conn, dialog_id, now, reason=type(exc).__name__)
            self._conn.commit()
            return _DeltaFetchOutcome([], 0)
        except RPCError as exc:
            logger.exception(
                "RPC error delta dialog_id=%d — skipping: %s",
                dialog_id,
                exc,
            )
            return _DeltaFetchOutcome([], 0)
        return _DeltaFetchOutcome(new_message_rows)

    @_delta_rpc_scope(DemandKind.DELTA_GAP_FILL, AcquisitionKind.MESSAGE_HISTORY_PAGE)
    async def fetch_delta_for_dialog(self, dialog_id: int) -> int:
        """Fetch all messages newer than max known message_id for one dialog.

        Public API: used by probe-worker for gap-fill after access recovery.
        Uses iter_messages(min_id=max_known_id, reverse=True) to fetch
        the gap in chronological order. INSERT OR REPLACE ensures
        idempotency across restarts.

        Returns:
            Count of new messages stored. 0 if no gap, no baseline, or error.
        """
        self._last_fetch_admission_deferred = False
        if not full_history_enabled(self._conn, dialog_id):
            return 0
        row = cast(
            tuple[object | None, ...] | None, self._conn.execute(_SELECT_MAX_MESSAGE_ID_SQL, (dialog_id,)).fetchone()
        )
        max_known_id = _row_first_int(row)
        if max_known_id == 0:
            # No baseline yet — FullSyncWorker handles this dialog
            with self._conn:
                self._stamp_delta_checked(dialog_id, int(time.time()))
            return 0

        outcome = await self._collect_delta_rows(dialog_id, max_known_id)
        if outcome.result is not None:
            return outcome.result
        new_message_rows = outcome.rows

        if new_message_rows:
            with self._conn:
                if not full_history_enabled(self._conn, dialog_id):
                    logger.info("delta_discarded_disabled dialog_id=%d fetched=%d", dialog_id, len(new_message_rows))
                    return 0
                insert_messages_with_fts(self._conn, new_message_rows, priority=HydrationPriority.BACKFILL)
            logger.info("delta dialog_id=%d new_messages=%d", dialog_id, len(new_message_rows))
        # Stamp last_synced_at unconditionally on the success path so that
        # run_delta_catch_up's quick-restart skip check has a fresh anchor.
        with self._conn:
            now = int(time.time())
            if not full_history_enabled(self._conn, dialog_id):
                return 0
            self._stamp_delta_checkpoint(dialog_id, now)
        return len(new_message_rows)

    @_delta_rpc_scope(DemandKind.DELTA_GAP_FILL, AcquisitionKind.MESSAGE_HISTORY_PAGE)
    async def fetch_delta_slice_for_dialog(self, dialog_id: int) -> int:  # noqa: PLR0911, PLR0912
        """Fetch and commit one resumable forward-history page."""
        self._last_fetch_admission_deferred = False
        if not full_history_enabled(self._conn, dialog_id):
            return 0
        row = cast(
            tuple[object | None, ...] | None,
            self._conn.execute(_SELECT_MAX_MESSAGE_ID_SQL, (dialog_id,)).fetchone(),
        )
        max_known_id = _row_first_int(row)
        if max_known_id == 0:
            with self._conn:
                self._stamp_delta_checked(dialog_id, int(time.time()))
            return 0

        rows: list[ExtractedMessage] = []
        completed = True
        try:
            async for message in self._client.iter_messages(
                entity=dialog_id,
                min_id=max_known_id,
                reverse=True,
                limit=_DELTA_SLICE_MESSAGE_LIMIT,
            ):
                if self._shutdown_event.is_set():
                    completed = False
                    break
                rows.append(extract_message_row(dialog_id, message))
        except TelegramRpcAdmissionDeferred:
            self._last_fetch_admission_deferred = True
            return 0
        except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError):
            self._last_fetch_admission_deferred = True
            return 0
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            return 0
        except ACCESS_LOST_ERRORS as exc:
            set_access_lost(self._conn, dialog_id, int(time.time()), reason=type(exc).__name__)
            self._conn.commit()
            return 0
        except RPCError as exc:
            logger.warning("delta_slice_rpc_error dialog_id=%d error=%s", dialog_id, exc)
            return 0

        continuation_required = not completed or len(rows) == _DELTA_SLICE_MESSAGE_LIMIT
        now = int(time.time())
        with self._conn:
            if not full_history_enabled(self._conn, dialog_id):
                return 0
            if rows:
                insert_messages_with_fts(self._conn, rows, priority=HydrationPriority.BACKFILL)
            if continuation_required:
                self._conn.execute(_REQUEST_DELTA_CONTINUATION_SQL, (now, dialog_id, dialog_id))
            else:
                self._stamp_delta_checkpoint(dialog_id, now)
        return len(rows)


class DeltaGapFillDemandAdapter:
    """One-page durable adapter over delta timestamps and refresh requests."""

    demand_kind = DemandKind.DELTA_GAP_FILL

    def __init__(self, worker: DeltaSyncWorker) -> None:
        self._worker = worker

    def status(self, now: float) -> DemandStatus | None:
        """Report the oldest local delta release boundary without writes."""
        del now
        rows = cast(
            list[tuple[int, int | None, int | None, int | None]],
            self._worker._conn.execute(_SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL).fetchall(),
        )
        if not rows:
            return None
        release_at = min(
            _delta_release_at(last_synced, last_checked, requested)
            for _, last_synced, last_checked, requested in rows
        )
        return DemandStatus(release_at=release_at)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Fetch one due dialog page and leave continuation in domain state."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        now = time.time()
        rows = cast(
            list[tuple[int, int | None, int | None, int | None]],
            self._worker._conn.execute(_SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL).fetchall(),
        )
        dialog_id = next(
            (row[0] for row in rows if _delta_release_at(row[1], row[2], row[3]) <= now),
            None,
        )
        if dialog_id is None:
            return
        with demand_context(DemandKind.DELTA_GAP_FILL):
            with rpc_attempt_budget(budget):
                try:
                    await self._worker.fetch_delta_slice_for_dialog(dialog_id)
                except RpcAttemptBudgetExhaustedError:
                    return


@_delta_rpc_scope(DemandKind.DELTA_GAP_FILL, AcquisitionKind.MESSAGE_HISTORY_PAGE)
async def run_delta_catch_up_loop(
    worker: DeltaSyncWorker,
    shutdown_event: asyncio.Event,
    policy: DeltaCatchUpPolicy,
) -> None:
    """Run forward gap-fill as a bounded maintenance loop, not startup burst."""
    if not policy.enabled:
        logger.info("delta_catch_up_loop disabled — max_probes_per_cycle=%d", policy.max_probes_per_cycle)
        return

    while not shutdown_event.is_set():
        total_new = await worker.run_delta_catch_up(policy=policy)
        logger.debug("delta_catch_up_cycle complete — new_messages=%d", total_new)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=policy.interval_seconds)
            break
        except TimeoutError:
            continue


# ---------------------------------------------------------------------------
# Probe-worker — access recovery for access_lost dialogs
# ---------------------------------------------------------------------------


async def _handle_probe_throttling(
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    policy: AccessProbePolicy,
    dialog_id: int,
    exc: TelegramRpcThrottled,
) -> None:
    _raise_if_latched(exc)
    logger.warning("probe_flood_wait dialog_id=%d seconds=%s", dialog_id, exc.retry_after_seconds)
    stamp_access_revalidation(
        conn,
        dialog_id,
        int(time.time()),
        max(policy.cooldown_seconds, exc.retry_after_seconds or policy.cooldown_seconds),
    )
    await sleep_through_flood(shutdown_event, exc.retry_after_seconds or 1)


async def _probe_access_lost_dialog(request: _AccessProbeRequest) -> _AccessProbeOutcome:
    client = request.client
    conn = request.conn
    dialog_id = request.dialog_id
    try:
        result = await client.get_messages(entity=dialog_id, limit=1)
        total = cast(int | None, getattr(result, "total", None))
        return await _finish_access_probe(conn, request.delta_worker, dialog_id, total)
    except (TelegramRpcAdmissionDeferred, RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
        deferred = isinstance(exc, TelegramRpcAdmissionDeferred)
        logger.info(
            "access_probe admission_deferred dialog_id=%d error_type=%s — preserving revalidation budget",
            dialog_id,
            type(exc).__name__,
        )
        retry_after = cast(float | None, getattr(exc, "retry_after_seconds", None))
        if deferred and retry_after is not None:
            await sleep_through_flood(request.shutdown_event, retry_after)
        return _AccessProbeOutcome(admission_deferred=True, counts_as_checked=not deferred)
    except RpcAdmissionClosedError:
        raise
    except ACCESS_LOST_ERRORS:
        logger.debug("access_still_lost dialog_id=%d", dialog_id)
        stamp_access_revalidation(conn, dialog_id, int(time.time()), request.policy.cooldown_seconds)
        return _AccessProbeOutcome(still_lost=1)
    except TelegramRpcThrottled as exc:
        await _handle_probe_throttling(request.conn, request.shutdown_event, request.policy, dialog_id, exc)
        return _AccessProbeOutcome(flood_wait_hit=True)
    except (RPCError, TimeoutError, OSError) as exc:
        error_kind = "probe_rpc_error" if isinstance(exc, RPCError) else "probe_network_error"
        logger.warning("%s dialog_id=%d error=%s", error_kind, dialog_id, exc)
        stamp_access_revalidation(request.conn, dialog_id, int(time.time()), request.policy.cooldown_seconds)
        return _AccessProbeOutcome(errors=1)


async def _finish_access_probe(
    conn: sqlite3.Connection,
    delta_worker: DeltaSyncWorker,
    dialog_id: int,
    total_messages: int | None,
) -> _AccessProbeOutcome:
    if not full_history_enabled(conn, dialog_id):
        return _AccessProbeOutcome(restored=_restore_revalidated_access(conn, dialog_id, total_messages=total_messages))
    new_msgs = await delta_worker.fetch_delta_for_dialog(dialog_id)
    logger.debug("access_restore_gap_fill dialog_id=%d new=%d", dialog_id, new_msgs)
    if delta_worker._last_fetch_admission_deferred:
        return _AccessProbeOutcome(admission_deferred=True)
    return _AccessProbeOutcome(restored=_restore_revalidated_access(conn, dialog_id, total_messages=total_messages))


@_delta_rpc_scope(DemandKind.DELTA_ACCESS_PROBE, AcquisitionKind.MESSAGE_LOOKUP)
async def _probe_access_lost_dialogs(
    client: _DeltaSyncClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    delta_worker: DeltaSyncWorker,
    policy: AccessProbePolicy,
) -> int:
    """Probe due access_lost dialogs under a tiny cold budget.

    Recovery sequence: probe -> gap-fill -> THEN reset status.
    If gap-fill fails, status stays access_lost (safe rollback).
    """
    now = int(time.time())
    rows = _access_probe_rows(conn, policy, now)

    restored = 0
    checked = 0
    still_lost = 0
    errors = 0
    flood_wait_hit = False
    for (dialog_id,) in rows:
        if shutdown_event.is_set():
            break
        checked += 1
        outcome = await _probe_access_lost_dialog(
            _AccessProbeRequest(client, conn, shutdown_event, delta_worker, policy, dialog_id)
        )
        restored += outcome.restored
        still_lost += outcome.still_lost
        errors += outcome.errors
        flood_wait_hit = outcome.flood_wait_hit
        if outcome.admission_deferred:
            if not outcome.counts_as_checked:
                checked -= 1
            break
        if outcome.flood_wait_hit:
            break

        if policy.probe_pause_seconds > 0 and not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=policy.probe_pause_seconds)
                break
            except TimeoutError:
                pass

    log_maintenance_cycle(
        logger,
        any((rows, restored, errors, flood_wait_hit)),
        "access_probe complete — selected=%d checked=%d restored=%d still_lost=%d errors=%d flood_wait_hit=%s",
        len(rows),
        checked,
        restored,
        still_lost,
        errors,
        flood_wait_hit,
    )
    return restored


def _restore_revalidated_access(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    total_messages: int | None,
) -> int:
    """Persist one verified restoration and return its counter contribution."""
    changed = restore_access_after_revalidation(conn, dialog_id, int(time.time()), total_messages=total_messages)
    conn.commit()
    return int(changed)


class DurableAccessRecoveryStateRequiredError(RuntimeError):
    """Raised until successful probes can hand off to bounded gap fill."""


class DeltaAccessProbeDemandAdapter:
    """Durable access probe over access-loss retry timestamps."""

    demand_kind = DemandKind.DELTA_ACCESS_PROBE

    def __init__(self, worker: DeltaSyncWorker, policy: AccessProbePolicy) -> None:
        self._worker = worker
        self._policy = policy

    def status(self, now: float) -> DemandStatus | None:
        """Report the earliest access revalidation boundary without writes."""
        del now
        if not self._policy.enabled:
            return None
        row = cast(
            tuple[int | None] | None,
            self._worker._conn.execute(
                "SELECT MIN(COALESCE(access_next_revalidate_at, COALESCE(access_lost_at, 0) + ?)) "
                "FROM synced_dialogs WHERE status = 'access_lost'",
                (self._policy.cooldown_seconds,),
            ).fetchone(),
        )
        if row is None or row[0] is None:
            return None
        return DemandStatus(release_at=float(row[0]))

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Probe one due peer when restoration is one-attempt atomic."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        policy = replace(self._policy, max_dialogs_per_cycle=1, probe_pause_seconds=0.0)
        rows = _access_probe_rows(self._worker._conn, policy, int(time.time()))
        if not rows:
            return
        dialog_id = rows[0][0]
        if full_history_enabled(self._worker._conn, dialog_id):
            raise DurableAccessRecoveryStateRequiredError(
                "access-probe slice needs a durable probe-success-to-gap-fill handoff before enrolled dialogs can run"
            )
        request = _AccessProbeRequest(
            self._worker._client,
            self._worker._conn,
            self._worker._shutdown_event,
            self._worker,
            policy,
            dialog_id,
        )
        with demand_context(DemandKind.DELTA_ACCESS_PROBE):
            with rpc_attempt_budget(budget):
                with acquisition_context(AcquisitionKind.MESSAGE_LOOKUP):
                    await _probe_access_lost_dialog(request)


@_delta_rpc_scope(DemandKind.DELTA_ACCESS_PROBE, AcquisitionKind.MESSAGE_LOOKUP)
async def run_access_probe_loop(
    client: _DeltaSyncClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    delta_worker: DeltaSyncWorker,
    policy: AccessProbePolicy,
    **options: Unpack[AccessProbeLoopOptions],
) -> None:
    """Cold probe of due access_lost dialogs. Restores access and triggers gap-fill.

    The loop may run daily, but each dialog is paced by a durable cooldown and
    each cycle has a small global budget.
    """
    if not policy.enabled:
        logger.info("access_probe_loop disabled — max_dialogs_per_cycle=%d", policy.max_dialogs_per_cycle)
        return

    initial_delay = options.get("initial_delay", 0.0)
    if initial_delay > 0:
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=initial_delay)
            return  # shutdown during initial delay
        except TimeoutError:
            pass  # initial delay elapsed normally; proceed with first probe

    while not shutdown_event.is_set():
        try:
            await _probe_access_lost_dialogs(client, conn, shutdown_event, delta_worker, policy)
        except RpcAdmissionClosedError:
            raise
        except Exception:
            logger.warning("access_probe_error", exc_info=True)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=policy.interval_seconds)
            return  # shutdown during sleep
        except TimeoutError:
            pass  # interval elapsed, run again


_EXPORTED_SYMBOLS = (
    AccessProbePolicy,
    DeltaAccessProbeDemandAdapter,
    DeltaCatchUpPolicy,
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
    DurableAccessRecoveryStateRequiredError,
    DeltaSyncWorker.run_delta_catch_up,
    run_access_probe_loop,
    run_delta_catch_up_loop,
)
