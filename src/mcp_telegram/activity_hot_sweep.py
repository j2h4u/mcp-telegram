"""Tier A — HotSweep — hourly incremental per-peer self-search scheduler.

Selects enrolled peers whose last_activity_at is within 30 days and whose
hot_next_retry_at is due, pages the ENTIRE newest-side message window for
each peer (concern 2 — multi-batch fix), and persists hot_cursor ONCE after
the window drains.

No scheduling state from Tier B (cold_*) is touched here.
"""

import asyncio
import hashlib
import logging
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, cast

from .activity_peer_sweep import (
    SkipReason,
    SweepResult,
    _load_working_set_enrollment_state,
    _save_dialog_state,
    run_working_set_enrollment_slice,
    sweep_peer_once,
    working_set_enrollment_release_at,
)
from .activity_substrate import ActivityClient
from .hydration_queue import HydrationPriority
from .sqlite_writer import SQLiteWriter
from .sync_transactions import write_transaction
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    durable_demand_context,
)
from .telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)

logger = logging.getLogger(__name__)

_BACKFILL_BATCH_LIMIT = 100
# Short transient backoff for ACCESS_SKIP (peer unresolved / timeout).
_ACCESS_SKIP_RETRY_S = 300  # 5 minutes


class HotSweepPolicy(Protocol):
    """Immutable policy port supplied by the daemon composition root."""

    @property
    def loop_interval_seconds(self) -> float: ...

    @property
    def max_peers_per_pass(self) -> int: ...

    @property
    def base_due_seconds(self) -> float: ...

    @property
    def max_due_seconds(self) -> float: ...

    @property
    def jitter_max_seconds(self) -> float: ...


@contextmanager
def _hot_demand_scope() -> Iterator[None]:
    """Install the exact hot-page root for a legacy pass."""
    with durable_demand_context(DemandKind.HOT_ACTIVITY_PAGE):
        yield


@dataclass(frozen=True, slots=True)
class _HotWindowState:
    dialog_id: int
    committed_cursor: int
    page_offset_id: int
    window_max_id: int
    had_new: bool
    last_sync_at: int | None
    empty_streak: int


def _hot_revision(account_id: int, state: _HotWindowState) -> WorkRevision:
    return WorkRevision(
        WorkKey(DemandKind.HOT_ACTIVITY_PAGE, account_id, f"peer:{state.dialog_id}"),
        repr((state.committed_cursor, state.page_offset_id, state.window_max_id, state.had_new, state.empty_streak)),
    )


def _hot_state_matches(conn: sqlite3.Connection, state: _HotWindowState) -> bool:
    row = cast(
        tuple[int, int | None, int | None, int | None, int | None, int | None] | None,
        conn.execute(
            "SELECT COALESCE(hot_cursor,0),hot_page_offset_id,hot_window_max_id,hot_window_had_new,hot_last_sync_at,hot_empty_streak "
            "FROM activity_dialog_state WHERE dialog_id=?",
            (state.dialog_id,),
        ).fetchone(),
    )
    return row is not None and tuple(row) == (
        state.committed_cursor,
        state.page_offset_id,
        state.window_max_id,
        int(state.had_new),
        state.last_sync_at,
        state.empty_streak,
    )


def _hot_committed_revision(conn: sqlite3.Connection, dialog_id: int, account_id: int) -> str | None:
    row = cast(
        tuple[int, int | None, int | None, int | None, int | None, int | None] | None,
        conn.execute(
            "SELECT COALESCE(hot_cursor,0),hot_page_offset_id,hot_window_max_id,hot_window_had_new,hot_last_sync_at,hot_empty_streak "
            "FROM activity_dialog_state WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    if row is None:
        return None
    state = _HotWindowState(
        int(dialog_id),
        int(row[0]),
        int(row[1] or 0),
        int(row[2] if row[2] is not None else row[0]),
        bool(row[3]),
        row[4],
        int(row[5] or 0),
    )
    return _hot_revision(account_id, state).revision


def _hot_release_candidates(conn: sqlite3.Connection, now: float) -> list[tuple[float, _HotWindowState]]:
    cutoff = _hot_release_cutoff(now)
    rows = cast(
        list[
            tuple[
                int,
                int | None,
                int | None,
                int | None,
                int | None,
                int | None,
                int | None,
                int | None,
                int | None,
                int | None,
            ]
        ],
        conn.execute(
            "SELECT ads.dialog_id,COALESCE(ads.hot_cursor,0),ads.hot_page_offset_id,ads.hot_window_max_id,"
            "ads.hot_window_had_new,ads.hot_next_due_at,ads.hot_next_retry_at,ads.hot_last_sync_at,sd.last_event_at,ads.hot_empty_streak "
            "FROM activity_dialog_state ads LEFT JOIN synced_dialogs sd ON sd.dialog_id=ads.dialog_id "
            "WHERE (ads.last_activity_at IS NULL OR ads.last_activity_at>=?) AND COALESCE(sd.status,'')!='access_lost' "
            "ORDER BY CASE WHEN ads.hot_page_offset_id IS NULL THEN 1 ELSE 0 END,"
            "COALESCE(ads.hot_next_retry_at,ads.hot_next_due_at,0),ads.dialog_id",
            (cutoff,),
        ).fetchall(),
    )
    candidates = []
    for dialog_id, cursor, offset, window_max, had_new, due, retry, last_sync, last_event, empty_streak in rows:
        state = _HotWindowState(
            int(dialog_id),
            int(cursor or 0),
            int(offset or 0),
            int(window_max if window_max is not None else cursor or 0),
            bool(had_new),
            last_sync,
            int(empty_streak or 0),
        )
        release = _hot_row_release_at((due, retry, last_sync, last_event, offset))
        candidates.append((release, state))
    return sorted(candidates, key=lambda item: (item[0], item[1].dialog_id))


def _begin_hot_window(conn: sqlite3.Connection, dialog_id: int) -> _HotWindowState | None:
    """Atomically initialize or reload one peer's in-progress hot window."""
    with write_transaction(conn):
        row = cast(
            tuple[int | None, int | None, int | None, int, int | None, int] | None,
            conn.execute(
                "SELECT hot_cursor, hot_page_offset_id, hot_window_max_id, hot_window_had_new, hot_last_sync_at, hot_empty_streak "
                "FROM activity_dialog_state WHERE dialog_id = ?",
                (dialog_id,),
            ).fetchone(),
        )
        if row is None:
            return None
        committed_cursor = row[0] or 0
        page_offset_id = row[1]
        window_max_id = row[2]
        had_new = bool(row[3])
        if page_offset_id is None or window_max_id is None:
            page_offset_id = 0
            window_max_id = committed_cursor
            had_new = False
            conn.execute(
                "UPDATE activity_dialog_state "
                "SET hot_page_offset_id = 0, hot_window_max_id = ?, hot_window_had_new = 0, updated_at = ? "
                "WHERE dialog_id = ? AND hot_page_offset_id IS NULL",
                (window_max_id, int(time.time()), dialog_id),
            )
    return _HotWindowState(
        dialog_id, committed_cursor, page_offset_id, window_max_id, had_new, row[4], int(row[5] or 0)
    )


def _save_hot_window_progress(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    page_offset_id: int,
    window_max_id: int,
    had_new: bool,
) -> None:
    """Commit one complete page's continuation and observed window maximum."""
    with write_transaction(conn):
        conn.execute(
            "UPDATE activity_dialog_state "
            "SET hot_page_offset_id = ?, hot_window_max_id = ?, hot_window_had_new = ?, updated_at = ? "
            "WHERE dialog_id = ?",
            (page_offset_id, window_max_id, int(had_new), int(time.time()), dialog_id),
        )


@dataclass(slots=True)
class HotActivityDemandAdapter(DurableDemandAdapter):
    """Execute one restart-safe page of the oldest due hot window."""

    client: ActivityClient
    conn: sqlite3.Connection
    shutdown_event: asyncio.Event
    policy: HotSweepPolicy
    timeout_s: float
    writer: SQLiteWriter = field(kw_only=True)
    account_id: int = field(kw_only=True)
    demand_kind = DemandKind.HOT_ACTIVITY_PAGE

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Return the earliest release among enrollment and hot-page work."""
        candidates: list[tuple[float, int, int, WorkRevision]] = []
        enrollment = self._enrollment_candidate(now, excluded)
        if enrollment is not None:
            candidates.append((enrollment[0], 0, 0, enrollment[1]))
        candidates.extend(
            (release, 1, state.dialog_id, _hot_revision(self.account_id, state))
            for release, state in _hot_release_candidates(self.conn, now)
            if _hot_revision(self.account_id, state) not in excluded
        )
        if not candidates:
            return None
        release_at, _, _, selected = min(candidates)
        return DemandStatus(release_at=release_at, selected=selected)

    def _enrollment_candidate(self, now: float, excluded: frozenset[WorkRevision]) -> tuple[float, WorkRevision] | None:
        release_at = working_set_enrollment_release_at(self.conn, now=now, cadence_s=self.policy.loop_interval_seconds)
        if release_at is None:
            return None
        state = _load_working_set_enrollment_state(self.conn)
        selected = WorkRevision(
            WorkKey(self.demand_kind, self.account_id, "enrollment"),
            repr((state.get("activity_working_set_phase"), state.get("activity_working_set_cursor"))),
        )
        return None if selected in excluded else (release_at, selected)

    async def _fetch_hot_page(
        self,
        state: _HotWindowState,
        budget: RpcAttemptBudget,
    ) -> SweepResult | None:
        """Fetch one page under the exact hot demand and admission scopes."""
        with _hot_demand_scope():
            with rpc_attempt_budget(budget):
                with acquisition_context(AcquisitionKind.MESSAGE_SEARCH_PAGE):
                    with rpc_scope(TelegramRpcSource.ACTIVITY_HOT_SWEEP, timeout_seconds=self.timeout_s):
                        try:
                            return await sweep_peer_once(
                                self.client,
                                self.conn,
                                state.dialog_id,
                                offset_id=state.page_offset_id,
                                min_id=state.committed_cursor,
                                limit=_BACKFILL_BATCH_LIMIT,
                                timeout_s=self.timeout_s,
                                writer=self.writer,
                                hydration_priority=HydrationPriority.FOREGROUND,
                                accept_page=lambda db: _hot_state_matches(db, state),
                            )
                        except RpcAttemptBudgetExhaustedError:
                            return None

    def _persist_hot_page_result(
        self, conn: sqlite3.Connection, state: _HotWindowState, result: SweepResult, stopped: bool
    ) -> tuple[bool, str | None]:
        """Persist the page outcome while keeping an incomplete window resumable."""
        if not _hot_state_matches(conn, state):
            return False, None
        max_seen = max(state.window_max_id, result.max_id or 0)
        had_new = state.had_new or result.genuinely_new > 0
        if result.flood_wait_seconds is not None:
            return True, None
        if result.skip_reason is SkipReason.ACCESS_SKIP:
            _save_hot_access_skip_state(
                conn,
                state.dialog_id,
                retry_at=int(time.time()) + _ACCESS_SKIP_RETRY_S,
            )
            return True, None
        if _is_hot_page_drained(result) and not stopped:
            _save_hot_completed_state(
                conn,
                state.dialog_id,
                hot_cursor=max_seen,
                completion_at=int(time.time()),
                genuinely_new=int(had_new),
                policy=self.policy,
            )
            return True, _hot_committed_revision(conn, state.dialog_id, self.account_id)
        if result.min_id is None:
            _save_hot_min_id_gap_state(conn, state.dialog_id, now=int(time.time()))
            return True, _hot_committed_revision(conn, state.dialog_id, self.account_id)
        _save_hot_window_progress(
            conn,
            state.dialog_id,
            page_offset_id=result.min_id,
            window_max_id=max_seen,
            had_new=had_new,
        )
        return True, _hot_committed_revision(conn, state.dialog_id, self.account_id)

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Run one enrollment unit or one bounded hot page."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise RuntimeError("hot activity slice requires an owner-selected revision")
        if self.shutdown_event.is_set():
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        if selected.key.account_id != self.account_id or selected.key.demand_kind is not self.demand_kind:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        validation, page_state = await self._validate_hot_selection(selected, time.time())
        if validation is not None:
            return validation
        del page_state
        return await self._run_hot_slice(selected, budget)

    async def _validate_hot_selection(
        self, selected: WorkRevision, now: float
    ) -> tuple[SliceOutcome | None, _HotWindowState | None]:
        if selected.key.target == "enrollment":
            return self._validate_hot_enrollment(selected, now)
        return await self._validate_hot_page(selected, now)

    def _validate_hot_enrollment(
        self, selected: WorkRevision, now: float
    ) -> tuple[SliceOutcome | None, _HotWindowState | None]:
        state = _load_working_set_enrollment_state(self.conn)
        revision = repr((state.get("activity_working_set_phase"), state.get("activity_working_set_cursor")))
        release = working_set_enrollment_release_at(self.conn, now=now, cadence_s=self.policy.loop_interval_seconds)
        if revision != selected.revision or release is None:
            return SliceOutcome(selected, SliceOutcomeKind.STALE), None
        if release > now:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED), None
        return None, None

    async def _validate_hot_page(
        self, selected: WorkRevision, now: float
    ) -> tuple[SliceOutcome | None, _HotWindowState | None]:
        dialog_id = int(selected.key.target.split(":", 1)[1])
        candidate = next(
            (
                (release, item)
                for release, item in _hot_release_candidates(self.conn, now)
                if item.dialog_id == dialog_id
            ),
            None,
        )
        if candidate is None or _hot_revision(self.account_id, candidate[1]) != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE), None
        if candidate[0] > now:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED), None
        state = await self.writer.run(lambda db: _begin_hot_window(db, dialog_id))
        if state is None or _hot_revision(self.account_id, state) != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE), None
        return None, state

    async def _run_hot_slice(self, selected: WorkRevision, budget: RpcAttemptBudget) -> SliceOutcome:
        with _hot_demand_scope():
            with rpc_attempt_budget(budget):
                if selected.key.target == "enrollment":
                    enrollment = await run_working_set_enrollment_slice(
                        self.client,
                        self.conn,
                        source=TelegramRpcSource.ACTIVITY_HOT_SWEEP,
                        cadence_s=self.policy.loop_interval_seconds,
                        timeout_s=self.timeout_s,
                        writer=self.writer,
                    )
                    if enrollment.consumed:
                        return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
                prepared = await self._prepare_hot_page(selected)
                if isinstance(prepared, SliceOutcome):
                    return prepared
                state = prepared
                result = await self._fetch_hot_page(state, budget)
                if result is None or not result.accepted or result.flood_wait_seconds is not None:
                    return self._hot_fetch_outcome(selected, result)
                stopped = self.shutdown_event.is_set()
        accepted, successor = await self.writer.run(
            lambda db: self._persist_hot_page_result(db, state, result, stopped)
        )
        return self._persisted_hot_outcome(selected, result, accepted, successor, stopped)

    async def _prepare_hot_page(self, selected: WorkRevision) -> _HotWindowState | SliceOutcome:
        prefix, separator, raw_dialog_id = selected.key.target.partition(":")
        if prefix != "peer" or not separator or not raw_dialog_id.lstrip("-").isdecimal():
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        dialog_id = int(raw_dialog_id)
        if dialog_id == 0 or str(dialog_id) != raw_dialog_id:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        state = await self.writer.run(lambda db: _begin_hot_window(db, dialog_id))
        if state is None or self.shutdown_event.is_set():
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if _hot_revision(self.account_id, state) != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        return state

    @staticmethod
    def _hot_fetch_outcome(selected: WorkRevision, result: SweepResult | None) -> SliceOutcome:
        if result is None or result.flood_wait_seconds is not None:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        if not result.accepted:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)

    @staticmethod
    def _persisted_hot_outcome(
        selected: WorkRevision,
        result: SweepResult,
        accepted: bool,
        successor: str | None,
        stopped: bool,
    ) -> SliceOutcome:
        if not accepted or successor is None:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if result.skip_reason is SkipReason.ACCESS_SKIP or (result.min_id is None and not _is_hot_page_drained(result)):
            return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
        if successor == selected.revision:
            return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
        kind = (
            SliceOutcomeKind.TERMINAL if _is_hot_page_drained(result) and not stopped else SliceOutcomeKind.PROGRESSED
        )
        return SliceOutcome(selected, kind, successor)


def _is_hot_page_drained(result: SweepResult) -> bool:
    """Return True when the current page fully drained the newest-side window."""
    return (
        result.hit_floor
        or result.skip_reason is SkipReason.HISTORY_FLOOR
        or len(result.fetched_ids) < _BACKFILL_BATCH_LIMIT
    )


def _save_hot_access_skip_state(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    retry_at: int,
) -> None:
    """Persist a transient retry window for ACCESS_SKIP."""
    _save_dialog_state(conn, dialog_id, hot_next_retry_at=retry_at)


def _stable_jitter_seconds(dialog_id: int, jitter_max_seconds: float) -> float:
    if jitter_max_seconds <= 0:
        return 0.0
    digest = hashlib.blake2b(str(dialog_id).encode("ascii"), digest_size=8).digest()
    fraction = int.from_bytes(digest, "big") / float(2**64 - 1)
    return fraction * jitter_max_seconds


def _save_hot_completed_state(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    hot_cursor: int,
    completion_at: int,
    genuinely_new: int,
    policy: HotSweepPolicy,
) -> None:
    """Persist cursor and exponential empty-yield cadence after completion."""
    with write_transaction(conn):
        row = cast(
            tuple[int, int],
            conn.execute(
                "SELECT hot_empty_streak,COALESCE(hot_cursor,0) FROM activity_dialog_state WHERE dialog_id = ?",
                (dialog_id,),
            ).fetchone(),
        )
        streak = 0 if genuinely_new > 0 and hot_cursor > row[1] else int(row[0]) + 1
        base_due_seconds = float(policy.base_due_seconds)
        max_due_seconds = float(policy.max_due_seconds)
        interval = _capped_empty_interval(base_due_seconds, max_due_seconds, streak)
        next_due_at = int(completion_at + interval + _stable_jitter_seconds(dialog_id, policy.jitter_max_seconds))
        conn.execute(
            "UPDATE activity_dialog_state "
            "SET hot_cursor = ?, hot_last_sync_at = ?, hot_next_retry_at = NULL, "
            "hot_next_due_at = ?, hot_empty_streak = ?, hot_page_offset_id = NULL, "
            "hot_window_max_id = NULL, hot_window_had_new = 0, updated_at = ? "
            "WHERE dialog_id = ?",
            (hot_cursor, completion_at, next_due_at, streak, completion_at, dialog_id),
        )


def _capped_empty_interval(base_due_seconds: float, max_due_seconds: float, streak: int) -> float:
    """Double the empty interval without ever evaluating an unbounded exponent."""
    interval = min(base_due_seconds, max_due_seconds)
    for _ in range(streak):
        if interval >= max_due_seconds:
            break
        interval = min(interval * 2.0, max_due_seconds)
    return interval


def _save_hot_min_id_gap_state(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    now: int,
) -> None:
    """Back off a malformed page without committing its incomplete window."""
    _save_dialog_state(
        conn,
        dialog_id,
        hot_next_retry_at=int(now + _ACCESS_SKIP_RETRY_S),
    )


def _hot_release_cutoff(now: float) -> int:
    """Validate a status timestamp and return the active peer cutoff."""
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise ValueError("now must be a finite non-negative timestamp")

    return int(now) - 30 * 86400


def _hot_row_release_at(row: tuple[int | None, int | None, int | None, int | None, int | None]) -> float:
    """Return the effective release boundary for one eligible hot peer."""
    next_due_at, next_retry_at, last_sync_at, last_event_at, page_offset_id = row
    event_due = last_sync_at is not None and last_event_at is not None and last_event_at > last_sync_at
    due_at = 0 if page_offset_id is not None or next_due_at is None or event_due else next_due_at
    return float(max(due_at, next_retry_at or 0))
