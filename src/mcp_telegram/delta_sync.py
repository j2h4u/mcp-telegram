"""DeltaSyncWorker — forward gap-fill engine for v1.5 Persistent Sync.

Fetches messages newer than the max known message_id per dialog in bounded
maintenance cycles. Idempotent: dialogs with no gap complete instantly when
the forward history port returns an empty page.

Architecture:
- Mirrors FullSyncWorker structural pattern (history port/conn/shutdown_event).
- Fetches forward pages vs FullSyncWorker's backward pages.
- Runs as a paced background maintenance loop, not as a blocking startup sweep.
- Only processes dialogs with status='synced' — FullSyncWorker handles
  'syncing' and 'not_synced' dialogs.
"""

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Protocol, cast

from .access_lifecycle import (
    complete_access_revalidation,
    restore_access_after_revalidation,
    set_access_lost,
    stamp_access_revalidation,
)
from .flood import TelegramRpcThrottled, _raise_if_latched
from .history_enrollment import full_history_enabled
from .hydration_queue import HydrationPriority
from .message_contracts import ExtractedMessage
from .message_history.contracts import MessageHistoryAccessLostError, MessageHistoryUnavailableError
from .message_history.ports import ForwardGapPagePort
from .messages.sqlite_bundle import insert_messages_with_fts
from .reactions.contracts import ReactionAggregateSource
from .telegram_demand import (
    AcquisitionKind,
    DeltaGapFillObservationHook,
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
class AccessProbePolicy:
    """Budgeted cold policy for access-lost archive revalidation."""

    interval_seconds: float
    max_dialogs_per_cycle: int
    cooldown_seconds: int

    @property
    def enabled(self) -> bool:
        return self.max_dialogs_per_cycle > 0


# Automatic forward-delta fallback cadence, measured from the durable
# last_delta_checked_at (or last_synced_at when no delta checkpoint exists).
DELTA_AUTOMATIC_REFRESH_INTERVAL_S: int = 2 * 60 * 60
_DELTA_SLICE_MESSAGE_LIMIT = 100
_DM_GAP_SCAN_RPC_CHUNK = 100
_DM_GAP_SCAN_PERIOD_S = 7 * 24 * 60 * 60
_DM_GAP_SCAN_STATE_KEY = "delta_dm_gap_scan_state"

_SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL = """
SELECT sd.dialog_id, sd.last_synced_at, sd.last_delta_checked_at, sd.delta_refresh_requested_at
FROM synced_dialogs sd
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
WHERE sd.status = 'synced'
  AND NOT EXISTS (SELECT 1 FROM dialogs directory WHERE directory.dialog_id=sd.dialog_id AND directory.hidden=1)
ORDER BY
    CASE WHEN sd.delta_refresh_requested_at IS NULL THEN 1 ELSE 0 END,
    COALESCE(sd.delta_refresh_requested_at, sd.last_delta_checked_at, sd.last_synced_at, 0),
    sd.dialog_id
"""
_SELECT_MAX_MESSAGE_ID_SQL = "SELECT COALESCE(MAX(message_id), 0) FROM messages WHERE dialog_id = ?"
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

_SELECT_DM_GAP_DIALOGS_SQL = """
SELECT sd.dialog_id
  FROM synced_dialogs AS sd
  JOIN full_history_enrollment AS fhe
    ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
  JOIN entities AS entity ON entity.id = sd.dialog_id
 WHERE sd.status = 'synced'
   AND entity.type IN ('user', 'bot')
   AND NOT EXISTS (SELECT 1 FROM dialogs directory WHERE directory.dialog_id=sd.dialog_id AND directory.hidden=1)
 ORDER BY sd.dialog_id
"""


@dataclass(frozen=True, slots=True)
class _DmGapScanState:
    """Restart-safe cursor for the weekly DM deletion verification pass."""

    status: str
    generation: int
    scan_started_at: int
    dialog_id_cursor: int | None
    message_cursor: int
    next_run_at: int


class DmGapScanPage(Protocol):
    """Event-handler seam for one bounded Telegram deletion lookup page."""

    async def run_dm_gap_scan_page(self, dialog_id: int, message_ids: Sequence[int]) -> int: ...


def _delta_gap_fill_error_result(exc: BaseException) -> tuple[str, str]:
    for error_type, result in (
        (TelegramRpcAdmissionDeferred, ("deferred", "admission_deferred")),
        (RpcAdmissionSaturatedError, ("deferred", "admission_saturated")),
        (RpcAdmissionExpiredError, ("deferred", "admission_expired")),
        (RpcAttemptBudgetExhaustedError, ("deferred", "budget_exhausted")),
        (TelegramRpcThrottled, ("deferred", "flood_wait")),
        (asyncio.CancelledError, ("failed", "cancelled")),
        (MessageHistoryUnavailableError, ("failed", "history_unavailable")),
    ):
        if isinstance(exc, error_type):
            return result
    return "failed", "error"


def _load_dm_gap_scan_state(conn: sqlite3.Connection) -> _DmGapScanState | None:
    row = cast(
        tuple[str | None] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key = ?", (_DM_GAP_SCAN_STATE_KEY,)).fetchone(),
    )
    if row is None or not row[0]:
        return None
    try:
        value = cast(dict[str, object], json.loads(row[0]))
        state = _DmGapScanState(
            status=str(value["status"]),
            generation=int(cast(int | str, value["generation"])),
            scan_started_at=int(cast(int | str, value["scan_started_at"])),
            dialog_id_cursor=(
                int(cast(int | str, value["dialog_id_cursor"])) if value["dialog_id_cursor"] is not None else None
            ),
            message_cursor=int(cast(int | str, value.get("message_cursor", value.get("message_offset", 0)))),
            next_run_at=int(cast(int | str, value["next_run_at"])),
        )
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        logger.warning("dm_gap_scan_state_corrupt — restarting deletion verification")
        return None
    if not _valid_dm_gap_scan_state(state):
        logger.warning("dm_gap_scan_state_invalid — restarting deletion verification")
        return None
    return state


def _valid_dm_gap_scan_state(state: _DmGapScanState) -> bool:
    if state.status not in {"running", "idle"}:
        return False
    return all(
        value >= 0
        for value in (
            state.generation,
            state.scan_started_at,
            state.message_cursor,
            state.next_run_at,
        )
    )


def _store_dm_gap_scan_state(conn: sqlite3.Connection, state: _DmGapScanState) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO daemon_state(key, value) VALUES (?, ?)",
        (
            _DM_GAP_SCAN_STATE_KEY,
            json.dumps(
                {
                    "status": state.status,
                    "generation": state.generation,
                    "scan_started_at": state.scan_started_at,
                    "dialog_id_cursor": state.dialog_id_cursor,
                    "message_cursor": state.message_cursor,
                    "next_run_at": state.next_run_at,
                },
                sort_keys=True,
            ),
        ),
    )


def _dm_gap_scan_dialog_ids(conn: sqlite3.Connection) -> tuple[int, ...]:
    rows = cast(Sequence[tuple[object]], conn.execute(_SELECT_DM_GAP_DIALOGS_SQL).fetchall())
    return tuple(int(cast(int, dialog_id)) for (dialog_id,) in rows)


def _dm_gap_scan_page_ids(
    conn: sqlite3.Connection,
    dialog_id: int,
    scan_started_at: int,
    message_cursor: int,
) -> tuple[int, ...]:
    rows = cast(
        Sequence[tuple[object]],
        conn.execute(
            "SELECT message_id FROM messages "
            "WHERE dialog_id = ? AND is_deleted = 0 AND sent_at < ? AND message_id > ? "
            "ORDER BY message_id LIMIT ?",
            (dialog_id, scan_started_at, message_cursor, _DM_GAP_SCAN_RPC_CHUNK),
        ).fetchall(),
    )
    return tuple(int(cast(int, message_id)) for (message_id,) in rows)


def _dm_gap_scan_release_at(conn: sqlite3.Connection, now: float) -> float | None:
    state = _load_dm_gap_scan_state(conn)
    if state is None or state.status == "running":
        return 0.0
    return float(state.next_run_at) if state.next_run_at > now else 0.0


class AccessProbe(Protocol):
    """Existing narrow access probe kept separate from paged history ports."""

    async def probe_total_messages(self, dialog_id: int) -> int | None: ...


@dataclass(frozen=True, slots=True)
class _DeltaFetchOutcome:
    rows: list[ExtractedMessage]
    result: int | None = None
    completed: bool = True
    reaction_observed_at: int | None = None


@dataclass(frozen=True, slots=True)
class _DeltaGapFillSliceOutcome:
    outcome: str
    attempts_before: int
    recovery_completed: bool | None = None


def _row_first_int(row: tuple[object | None, ...] | None) -> int:
    if row is None:
        return 0
    value = row[0]
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


def _delta_skip_anchor(last_synced_at: int | None, last_delta_checked_at: int | None) -> int | None:
    """Return the durable checkpoint anchoring the automatic refresh cadence."""
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
    return float(anchor + DELTA_AUTOMATIC_REFRESH_INTERVAL_S)


# ---------------------------------------------------------------------------
# DeltaSyncWorker
# ---------------------------------------------------------------------------


class DeltaSyncWorker:
    """Forward gap-fill engine for one bounded page per demand slice."""

    def __init__(
        self,
        history_port: ForwardGapPagePort,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._history_port = history_port
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._last_delta_slice_completed = False
        self._last_delta_slice_succeeded = False
        self._last_delta_slice_error: BaseException | None = None
        self._last_delta_slice_access_lost = False
        self._last_delta_slice_skipped = False
        self._last_delta_page_metrics: dict[str, int] = {}
        self._delta_gap_fill_observation_enabled = False

    def _stamp_delta_checkpoint(self, dialog_id: int, checked_at: int) -> None:
        self._conn.execute(_UPDATE_DELTA_CHECKPOINT_SQL, (checked_at, checked_at, dialog_id, dialog_id))

    def _stamp_delta_checked(self, dialog_id: int, checked_at: int) -> None:
        self._conn.execute(_UPDATE_DELTA_CHECKED_SQL, (checked_at, dialog_id, dialog_id))

    @_delta_rpc_scope(DemandKind.DELTA_GAP_FILL, AcquisitionKind.MESSAGE_HISTORY_PAGE)
    async def fetch_delta_slice_for_dialog(self, dialog_id: int) -> int:
        """Fetch and commit one resumable forward-history page."""
        self._reset_delta_slice_state()
        if not full_history_enabled(self._conn, dialog_id):
            return 0
        max_known_id = self._max_known_message_id(dialog_id)
        if max_known_id == 0:
            self._complete_empty_delta_slice(dialog_id)
            return 0

        outcome = await self._collect_delta_slice(dialog_id, max_known_id)
        if outcome.result is not None:
            return outcome.result
        return self._commit_delta_slice(dialog_id, outcome)

    def _reset_delta_slice_state(self) -> None:
        self._last_delta_slice_completed = False
        self._last_delta_slice_succeeded = False
        self._last_delta_slice_error = None
        self._last_delta_slice_access_lost = False
        self._last_delta_slice_skipped = False
        self._last_delta_page_metrics = {}

    def _max_known_message_id(self, dialog_id: int) -> int:
        row = cast(
            tuple[object | None, ...] | None,
            self._conn.execute(_SELECT_MAX_MESSAGE_ID_SQL, (dialog_id,)).fetchone(),
        )
        return _row_first_int(row)

    def _complete_empty_delta_slice(self, dialog_id: int) -> None:
        with self._conn:
            self._stamp_delta_checked(dialog_id, int(time.time()))
        self._last_delta_slice_completed = True
        self._last_delta_slice_succeeded = True

    async def _collect_delta_slice(self, dialog_id: int, max_known_id: int) -> _DeltaFetchOutcome:
        reaction_observed_at = int(time.time())
        try:
            page = await self._history_port.fetch_page(
                dialog_id,
                after_message_id=max_known_id,
                should_stop=self._shutdown_event.is_set,
            )
        except TelegramRpcAdmissionDeferred as exc:
            self._last_delta_slice_error = exc
            return _DeltaFetchOutcome([], 0)
        except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            self._last_delta_slice_error = exc
            return _DeltaFetchOutcome([], 0)
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            self._last_delta_slice_error = exc
            return _DeltaFetchOutcome([], 0)
        except MessageHistoryAccessLostError as exc:
            self._last_delta_slice_access_lost = True
            set_access_lost(self._conn, dialog_id, int(time.time()), reason=exc.reason_code)
            self._conn.commit()
            return _DeltaFetchOutcome([], 0)
        except MessageHistoryUnavailableError as exc:
            logger.warning("delta_slice_rpc_error dialog_id=%d error=%s", dialog_id, exc)
            self._last_delta_slice_error = exc
            return _DeltaFetchOutcome([], 0)
        rows = list(page.messages)
        if self._delta_gap_fill_observation_enabled:
            unique_keys = {(row.message.dialog_id, row.message.message_id) for row in rows}
            self._last_delta_page_metrics = {
                "page_count": 1,
                "empty_page_count": int(not rows),
                "fetched_count": len(rows),
                "duplicate_count": len(rows) - len(unique_keys),
                "preexisting_key_count": 0,
                "new_key_count": 0,
                "uncommitted_unique_count": len(unique_keys),
            }
        return _DeltaFetchOutcome(rows, completed=page.complete, reaction_observed_at=reaction_observed_at)

    def _commit_delta_slice(self, dialog_id: int, outcome: _DeltaFetchOutcome) -> int:
        continuation_required = not outcome.completed or len(outcome.rows) == _DELTA_SLICE_MESSAGE_LIMIT
        now = int(time.time())
        result = self._commit_delta_transaction(dialog_id, outcome, continuation_required, now)
        if result is None:
            return 0
        unique_message_ids, existing_ids, committed_new_count = result
        self._last_delta_slice_completed = not continuation_required
        self._last_delta_slice_succeeded = True
        if self._delta_gap_fill_observation_enabled and outcome.rows:
            self._last_delta_page_metrics.update(
                new_key_count=committed_new_count,
                uncommitted_unique_count=len(unique_message_ids) - len(existing_ids) - committed_new_count,
            )
        return len(outcome.rows)

    def _commit_delta_transaction(
        self, dialog_id: int, outcome: _DeltaFetchOutcome, continuation_required: bool, now: int
    ) -> tuple[list[int], set[int], int] | None:
        with self._conn:
            unique_message_ids, existing_ids = self._existing_delta_message_ids(dialog_id, outcome.rows)
            if not full_history_enabled(self._conn, dialog_id):
                self._record_uncommitted_delta_counts(unique_message_ids, existing_ids)
                return None
            committed_new_count = self._insert_delta_messages(dialog_id, outcome, unique_message_ids, existing_ids, now)
            if continuation_required:
                self._conn.execute(_REQUEST_DELTA_CONTINUATION_SQL, (now, dialog_id, dialog_id))
            else:
                self._stamp_delta_checkpoint(dialog_id, now)
        return unique_message_ids, existing_ids, committed_new_count

    def _existing_delta_message_ids(
        self, dialog_id: int, rows: Sequence[ExtractedMessage]
    ) -> tuple[list[int], set[int]]:
        if not self._delta_gap_fill_observation_enabled:
            return [], set()
        unique_message_ids = sorted({row.message.message_id for row in rows})
        if not unique_message_ids:
            return unique_message_ids, set()
        placeholders = ",".join("?" for _ in unique_message_ids)
        rows_found = cast(
            Sequence[tuple[int]],
            self._conn.execute(
                f"SELECT message_id FROM messages WHERE dialog_id=? AND message_id IN ({placeholders})",
                (dialog_id, *unique_message_ids),
            ).fetchall(),
        )
        existing_ids = {message_id for (message_id,) in rows_found}
        self._record_uncommitted_delta_counts(unique_message_ids, existing_ids)
        return unique_message_ids, existing_ids

    def _record_uncommitted_delta_counts(self, unique_message_ids: Sequence[int], existing_ids: set[int]) -> None:
        if not self._delta_gap_fill_observation_enabled:
            return
        self._last_delta_page_metrics.update(
            preexisting_key_count=len(existing_ids),
            new_key_count=0,
            uncommitted_unique_count=len(unique_message_ids) - len(existing_ids),
        )

    def _insert_delta_messages(
        self,
        dialog_id: int,
        outcome: _DeltaFetchOutcome,
        unique_message_ids: Sequence[int],
        existing_ids: set[int],
        now: int,
    ) -> int:
        if not outcome.rows:
            return 0
        insert_messages_with_fts(
            self._conn,
            outcome.rows,
            priority=HydrationPriority.BACKFILL,
            reaction_source=ReactionAggregateSource.DELTA,
            reaction_observed_at=outcome.reaction_observed_at or now,
        )
        if not self._delta_gap_fill_observation_enabled:
            return 0
        # INSERT OR REPLACE publishes every unique fetched key; only absent keys count.
        return len(unique_message_ids) - len(existing_ids)


def _delta_gap_fill_slice_metrics(
    worker: DeltaSyncWorker,
    budget: RpcAttemptBudget,
    slice_outcome: _DeltaGapFillSliceOutcome,
) -> dict[str, int]:
    page = worker._last_delta_page_metrics
    attempts = max(0, budget.attempts - slice_outcome.attempts_before)
    recovery = slice_outcome.recovery_completed is not None
    terminal = (
        worker._last_delta_slice_completed
        if slice_outcome.recovery_completed is None
        else slice_outcome.recovery_completed
    ) and not worker._last_delta_slice_skipped
    return {
        "slice_count": 1,
        "completed": int(slice_outcome.outcome == "completed"),
        "deferred": int(slice_outcome.outcome == "deferred"),
        "failed": int(slice_outcome.outcome == "failed"),
        "scheduled": int(not recovery),
        "recovery": int(recovery),
        "actual_attempts": attempts,
        "attempted_slices": int(attempts > 0),
        "page_count": page.get("page_count", 0),
        "empty_page_count": page.get("empty_page_count", 0),
        "fetched_count": page.get("fetched_count", 0),
        "new_key_count": page.get("new_key_count", 0),
        "preexisting_key_count": page.get("preexisting_key_count", 0),
        "uncommitted_unique_count": page.get("uncommitted_unique_count", 0),
        "duplicate_count": page.get("duplicate_count", 0),
        "continuation_count": int(worker._last_delta_slice_succeeded and not terminal),
        "terminal_count": int(terminal),
        "recovery_completion_count": int(slice_outcome.recovery_completed is True),
    }


class DeltaGapFillDemandAdapter:
    """One-page durable adapter over delta and DM tombstone subqueues."""

    demand_kind = DemandKind.DELTA_GAP_FILL

    def __init__(
        self,
        worker: DeltaSyncWorker,
        dm_gap_scanner: DmGapScanPage | None = None,
        observer: DeltaGapFillObservationHook | None = None,
    ) -> None:
        self._worker = worker
        self._dm_gap_scanner = dm_gap_scanner
        self._observer = observer
        if observer is not None:
            self._worker._delta_gap_fill_observation_enabled = True

    def _observe_forward_slice(
        self, budget: RpcAttemptBudget, attempts_before: int, *, outcome: str, reason: str, recovery: bool = False
    ) -> None:
        if self._observer is None:
            return
        metrics = _delta_gap_fill_slice_metrics(
            self._worker,
            budget,
            _DeltaGapFillSliceOutcome(outcome, attempts_before, True if recovery else None),
        )
        try:
            self._observer.observe_delta_gap_fill(metrics, reason=reason)
        except Exception:  # noqa: BLE001 - telemetry cannot change sync behavior
            logger.debug("delta_gap_fill_observation_failed")

    async def _run_forward_slice(self, budget: RpcAttemptBudget, dialog_id: int) -> None:
        attempts_before = budget.attempts
        outcome, reason = "completed", "completed"
        try:
            await self._worker.fetch_delta_slice_for_dialog(dialog_id)
            if self._worker._last_delta_slice_error is not None:
                raise self._worker._last_delta_slice_error
            if self._worker._last_delta_slice_access_lost:
                outcome, reason = "failed", "access_lost"
            elif self._worker._last_delta_slice_completed:
                if self._worker._last_delta_page_metrics.get("empty_page_count", 0):
                    reason = "empty_page"
                else:
                    reason = "terminal" if self._worker._last_delta_page_metrics.get("page_count", 0) else "completed"
            elif self._worker._last_delta_slice_succeeded:
                reason = "continuation"
        except BaseException as exc:
            outcome, reason = _delta_gap_fill_error_result(exc)
            raise
        finally:
            self._observe_forward_slice(budget, attempts_before, outcome=outcome, reason=reason)

    def _ordinary_candidate(self, now: float) -> tuple[float, int] | None:
        rows = cast(
            list[tuple[int, int | None, int | None, int | None]],
            self._worker._conn.execute(_SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL).fetchall(),
        )
        candidates = [
            (_delta_release_at(last_synced, last_checked, requested), int(dialog_id))
            for dialog_id, last_synced, last_checked, requested in rows
        ]
        return min(candidates, key=lambda candidate: (candidate[0], candidate[1])) if candidates else None

    def _candidate(self, now: float) -> tuple[float, int, int | None] | None:
        candidates: list[tuple[float, int, int | None]] = []
        ordinary = self._ordinary_candidate(now)
        if ordinary is not None:
            release_at, dialog_id = ordinary
            candidates.append((release_at, 0, dialog_id))
        if self._dm_gap_scanner is not None:
            dm_release_at: float | None = _dm_gap_scan_release_at(self._worker._conn, now)
            if dm_release_at is not None:
                candidates.append((dm_release_at, 1, None))
        return (
            min(candidates, key=lambda candidate: (candidate[0], candidate[1], candidate[2] or -1))
            if candidates
            else None
        )

    def status(self, now: float) -> DemandStatus | None:
        """Report the oldest local delta release boundary without writes."""
        candidate = self._candidate(now)
        if candidate is None:
            return None
        return DemandStatus(release_at=candidate[0])

    def _start_dm_gap_scan(self, now: float) -> _DmGapScanState | None:
        state = _load_dm_gap_scan_state(self._worker._conn)
        if state is None or (state.status == "idle" and state.next_run_at <= now):
            previous_generation = 0 if state is None else state.generation
            state = _DmGapScanState("running", previous_generation + 1, int(now), None, 0, 0)
            with self._worker._conn:
                _store_dm_gap_scan_state(self._worker._conn, state)
        return None if state.status == "idle" else state

    def _next_dm_gap_dialog(self, state: _DmGapScanState, dialog_ids: Sequence[int]) -> int | None:
        if state.message_cursor > 0 and state.dialog_id_cursor in dialog_ids:
            return state.dialog_id_cursor
        return next(
            (
                candidate
                for candidate in dialog_ids
                if state.dialog_id_cursor is None or candidate > state.dialog_id_cursor
            ),
            None,
        )

    def _complete_dm_gap_scan(self, state: _DmGapScanState, now: float) -> None:
        completed = _DmGapScanState(
            "idle",
            state.generation,
            state.scan_started_at,
            None,
            0,
            int(now) + _DM_GAP_SCAN_PERIOD_S,
        )
        with self._worker._conn:
            _store_dm_gap_scan_state(self._worker._conn, completed)

    def _advance_empty_dm_dialog(self, state: _DmGapScanState, dialog_id: int) -> None:
        advanced = _DmGapScanState(
            "running",
            state.generation,
            state.scan_started_at,
            dialog_id,
            0,
            0,
        )
        with self._worker._conn:
            _store_dm_gap_scan_state(self._worker._conn, advanced)

    def _advance_dm_gap_page(self, state: _DmGapScanState, dialog_id: int, next_cursor: int) -> None:
        advanced = _DmGapScanState(
            "running",
            state.generation,
            state.scan_started_at,
            dialog_id,
            next_cursor,
            0,
        )
        with self._worker._conn:
            _store_dm_gap_scan_state(self._worker._conn, advanced)

    async def _run_dm_gap_slice(self, now: float) -> None:
        """Verify one persisted DM page, or advance one empty dialog locally."""
        state = self._start_dm_gap_scan(now)
        if state is None:
            return

        dialog_ids = _dm_gap_scan_dialog_ids(self._worker._conn)
        dialog_id = self._next_dm_gap_dialog(state, dialog_ids)
        if dialog_id is None:
            self._complete_dm_gap_scan(state, now)
            return

        page = _dm_gap_scan_page_ids(
            self._worker._conn,
            dialog_id,
            state.scan_started_at,
            state.message_cursor,
        )
        if not page:
            self._advance_empty_dm_dialog(state, dialog_id)
            return

        # Leave the cursor at the page start until the collaborator commits. A
        # process crash during the RPC repeats an idempotent tombstone page.
        await self._dm_gap_scanner.run_dm_gap_scan_page(dialog_id, page)  # type: ignore[union-attr]
        self._advance_dm_gap_page(state, dialog_id, page[-1])

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Fetch one due dialog page and leave continuation in domain state."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        now = time.time()
        candidate = self._candidate(now)
        if candidate is None or candidate[0] > now:
            return
        with demand_context(DemandKind.DELTA_GAP_FILL):
            with rpc_attempt_budget(budget):
                if candidate[1] == 1:
                    await self._run_dm_gap_slice(now)
                else:
                    assert candidate[2] is not None
                    await self._run_forward_slice(budget, candidate[2])


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


@dataclass(frozen=True, slots=True)
class _DurableAccessRecovery:
    dialog_id: int
    total_messages: int | None
    retry_at: int | None


def _due_access_recovery(conn: sqlite3.Connection, *, now: int) -> _DurableAccessRecovery | None:
    row = cast(
        tuple[int, int | None, int | None] | None,
        conn.execute(
            """
            SELECT recovery.dialog_id, recovery.total_messages, recovery.retry_at
              FROM delta_access_recovery_state AS recovery
              JOIN synced_dialogs AS synced ON synced.dialog_id=recovery.dialog_id
             WHERE synced.status='access_lost'
               AND (recovery.retry_at IS NULL OR recovery.retry_at <= ?)
             ORDER BY COALESCE(recovery.retry_at, 0), recovery.probe_succeeded_at, recovery.dialog_id
             LIMIT 1
            """,
            (now,),
        ).fetchone(),
    )
    if row is None:
        return None
    return _DurableAccessRecovery(*row)


def _set_access_recovery_retry(conn: sqlite3.Connection, dialog_id: int, retry_at: int | None) -> None:
    with conn:
        conn.execute(
            "UPDATE delta_access_recovery_state SET retry_at=?, updated_at=? WHERE dialog_id=?",
            (retry_at, int(time.time()), dialog_id),
        )


def _finish_durable_access_recovery(
    conn: sqlite3.Connection,
    recovery: _DurableAccessRecovery,
) -> None:
    now = int(time.time())
    with conn:
        restore_access_after_revalidation(
            conn,
            recovery.dialog_id,
            now,
            total_messages=recovery.total_messages,
        )
        conn.execute("DELETE FROM delta_access_recovery_state WHERE dialog_id=?", (recovery.dialog_id,))


class DeltaAccessProbeDemandAdapter:
    """Resume one access probe or one post-probe gap-fill page per slice."""

    demand_kind = DemandKind.DELTA_ACCESS_PROBE

    def __init__(
        self,
        worker: DeltaSyncWorker,
        policy: AccessProbePolicy,
        probe: AccessProbe,
        observer: DeltaGapFillObservationHook | None = None,
    ) -> None:
        self._worker = worker
        self._policy = policy
        self._probe = probe
        self._observer = observer
        if observer is not None:
            self._worker._delta_gap_fill_observation_enabled = True

    def status(self, now: float) -> DemandStatus | None:
        """Report the earliest access revalidation boundary without writes."""
        del now
        if not self._policy.enabled:
            return None
        row = cast(
            tuple[int | None] | None,
            self._worker._conn.execute(
                """
                SELECT MIN(release_at)
                  FROM (
                    SELECT COALESCE(recovery.retry_at, 0) AS release_at
                      FROM delta_access_recovery_state AS recovery
                      JOIN synced_dialogs AS synced ON synced.dialog_id=recovery.dialog_id
                     WHERE synced.status='access_lost'
                    UNION ALL
                    SELECT COALESCE(synced.access_next_revalidate_at,
                                    COALESCE(synced.access_lost_at, 0) + ?) AS release_at
                      FROM synced_dialogs AS synced
                     WHERE synced.status='access_lost'
                       AND NOT EXISTS (
                           SELECT 1 FROM delta_access_recovery_state AS recovery
                            WHERE recovery.dialog_id=synced.dialog_id
                       )
                  )
                """,
                (self._policy.cooldown_seconds,),
            ).fetchone(),
        )
        if row is None or row[0] is None:
            return None
        return DemandStatus(release_at=float(row[0]))

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Commit probe success before a later slice performs gap fill."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        with demand_context(DemandKind.DELTA_ACCESS_PROBE):
            with rpc_attempt_budget(budget):
                recovery = _due_access_recovery(self._worker._conn, now=int(time.time()))
                if recovery is not None:
                    await self._run_observed_recovery_slice(budget, recovery)
                    return
                await self._run_probe_slice()

    async def _run_observed_recovery_slice(self, budget: RpcAttemptBudget, recovery: _DurableAccessRecovery) -> None:
        attempts_before = budget.attempts
        self._worker._reset_delta_slice_state()
        outcome, reason = "completed", "completed"
        recovery_completed = False
        try:
            recovery_completed = await self._run_gap_fill_slice(recovery)
            if self._worker._last_delta_slice_error is not None:
                outcome, reason = _delta_gap_fill_error_result(self._worker._last_delta_slice_error)
            elif self._worker._last_delta_slice_access_lost:
                outcome, reason = "failed", "access_lost"
            elif recovery_completed:
                reason = "skipped" if self._worker._last_delta_slice_skipped else "terminal"
            elif self._worker._last_delta_slice_succeeded:
                reason = "continuation"
        except BaseException as exc:
            outcome, reason = _delta_gap_fill_error_result(exc)
            raise
        finally:
            if self._observer is not None:
                metrics = _delta_gap_fill_slice_metrics(
                    self._worker,
                    budget,
                    _DeltaGapFillSliceOutcome(outcome, attempts_before, recovery_completed),
                )
                try:
                    self._observer.observe_delta_gap_fill(metrics, reason=reason)
                except Exception:  # noqa: BLE001 - telemetry cannot change sync behavior
                    logger.debug("delta_gap_fill_observation_failed")

    async def _run_probe_slice(self) -> None:
        now = int(time.time())
        dialog_id = self._due_probe_dialog_id(now)
        if dialog_id is None:
            return
        await self._probe_dialog_for_recovery(dialog_id, now)

    def _due_probe_dialog_id(self, now: int) -> int | None:
        policy = self._policy
        row = cast(
            tuple[int] | None,
            self._worker._conn.execute(
                """
                SELECT synced.dialog_id
                  FROM synced_dialogs AS synced
                 WHERE synced.status='access_lost'
                   AND COALESCE(synced.access_next_revalidate_at,
                                COALESCE(synced.access_lost_at, 0) + ?) <= ?
                   AND NOT EXISTS (
                       SELECT 1 FROM delta_access_recovery_state AS recovery
                        WHERE recovery.dialog_id=synced.dialog_id
                   )
                 ORDER BY COALESCE(synced.access_next_revalidate_at,
                                   COALESCE(synced.access_lost_at, 0) + ?), synced.dialog_id
                 LIMIT 1
                """,
                (policy.cooldown_seconds, now, policy.cooldown_seconds),
            ).fetchone(),
        )
        return None if row is None else row[0]

    async def _request_probe(self, dialog_id: int) -> int | None:
        with acquisition_context(AcquisitionKind.MESSAGE_LOOKUP):
            with rpc_scope(TelegramRpcSource.DELTA_SYNC):
                return await self._probe.probe_total_messages(dialog_id)

    async def _probe_dialog_for_recovery(self, dialog_id: int, now: int) -> None:
        try:
            result = await self._request_probe(dialog_id)
        except (
            TelegramRpcAdmissionDeferred,
            TelegramRpcThrottled,
        ) as exc:
            self._handle_probe_error(dialog_id, now, exc)
            return
        except MessageHistoryAccessLostError as exc:
            self._handle_probe_error(dialog_id, now, exc)
            return
        except MessageHistoryUnavailableError as exc:
            self._handle_probe_error(dialog_id, now, exc)
            return

        self._persist_probe_success(dialog_id, now, result)

    def _handle_probe_error(self, dialog_id: int, now: int, exc: BaseException) -> None:
        conn = self._worker._conn
        if isinstance(exc, TelegramRpcAdmissionDeferred):
            logger.info(
                "access_probe admission_deferred dialog_id=%d error_type=%s — preserving revalidation budget",
                dialog_id,
                type(exc).__name__,
            )
            retry = max(1, int(exc.retry_after_seconds or 1))
            stamp_access_revalidation(conn, dialog_id, now, retry)
            conn.commit()
            return
        if isinstance(exc, MessageHistoryAccessLostError):
            logger.debug("access_still_lost dialog_id=%d", dialog_id)
            stamp_access_revalidation(conn, dialog_id, now, self._policy.cooldown_seconds)
            conn.commit()
            return
        if isinstance(exc, TelegramRpcThrottled):
            _raise_if_latched(exc)
            retry = max(self._policy.cooldown_seconds, exc.retry_after_seconds or self._policy.cooldown_seconds)
            stamp_access_revalidation(conn, dialog_id, now, retry)
            conn.commit()
            return
        logger.warning("probe_rpc_error dialog_id=%d error=%s", dialog_id, exc)
        stamp_access_revalidation(conn, dialog_id, now, self._policy.cooldown_seconds)
        conn.commit()

    def _persist_probe_success(self, dialog_id: int, now: int, total_messages: int | None) -> None:
        if not full_history_enabled(self._worker._conn, dialog_id):
            _restore_revalidated_access(
                self._worker._conn,
                dialog_id,
                total_messages=total_messages,
            )
            return
        with self._worker._conn:
            self._worker._conn.execute(
                """
                INSERT INTO delta_access_recovery_state(
                    dialog_id, stage, total_messages, probe_succeeded_at, retry_at, updated_at
                ) VALUES (?, 'gap_fill', ?, ?, NULL, ?)
                ON CONFLICT(dialog_id) DO UPDATE SET
                    stage='gap_fill', total_messages=excluded.total_messages,
                    probe_succeeded_at=excluded.probe_succeeded_at,
                    retry_at=NULL, updated_at=excluded.updated_at
                """,
                (dialog_id, total_messages, now, now),
            )
            complete_access_revalidation(self._worker._conn, dialog_id, now)

    async def _run_gap_fill_slice(self, recovery: _DurableAccessRecovery) -> bool:
        if not full_history_enabled(self._worker._conn, recovery.dialog_id):
            self._worker._reset_delta_slice_state()
            self._worker._last_delta_slice_skipped = True
            _finish_durable_access_recovery(self._worker._conn, recovery)
            return True
        await self._worker.fetch_delta_slice_for_dialog(recovery.dialog_id)
        if self._worker._last_delta_slice_completed:
            _finish_durable_access_recovery(self._worker._conn, recovery)
            return True
        if self._worker._last_delta_slice_succeeded:
            _set_access_recovery_retry(self._worker._conn, recovery.dialog_id, None)
            return False
        if (
            self._worker._conn.execute(
                "SELECT 1 FROM delta_access_recovery_state WHERE dialog_id=?", (recovery.dialog_id,)
            ).fetchone()
            is not None
        ):
            _set_access_recovery_retry(
                self._worker._conn,
                recovery.dialog_id,
                int(time.time()) + self._policy.cooldown_seconds,
            )
        return False


_EXPORTED_SYMBOLS = (
    AccessProbePolicy,
    AccessProbe,
    DeltaAccessProbeDemandAdapter,
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
)
