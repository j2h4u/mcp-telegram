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
from dataclasses import dataclass
from typing import Protocol, cast

from .activity_peer_sweep import (
    SkipReason,
    SweepResult,
    _save_dialog_state,
    run_working_set_enrollment_slice,
    sweep_peer_once,
    working_set_enrollment_release_at,
)
from .activity_substrate import ActivityClient
from .hydration_queue import HydrationPriority
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    current_demand_token,
    demand_context,
)
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
    try:
        token = current_demand_token()
    except UnclassifiedTelegramDemandError:
        with demand_context(DemandKind.HOT_ACTIVITY_PAGE):
            yield
        return
    if token.kind is not DemandKind.HOT_ACTIVITY_PAGE:
        raise RuntimeError(f"active demand kind {token.kind.value} cannot execute hot activity page")
    yield


@dataclass(frozen=True, slots=True)
class _HotWindowState:
    dialog_id: int
    committed_cursor: int
    page_offset_id: int
    window_max_id: int
    had_new: bool


def _begin_hot_window(conn: sqlite3.Connection, dialog_id: int) -> _HotWindowState | None:
    """Atomically initialize or reload one peer's in-progress hot window."""
    with conn:
        row = cast(
            tuple[int | None, int | None, int | None, int] | None,
            conn.execute(
                "SELECT hot_cursor, hot_page_offset_id, hot_window_max_id, hot_window_had_new "
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
    return _HotWindowState(dialog_id, committed_cursor, page_offset_id, window_max_id, had_new)


def _select_due_hot_window(conn: sqlite3.Connection, *, now: int) -> _HotWindowState | None:
    """Claim the oldest due peer by durably opening its newest-side window."""
    cutoff = now - 30 * 86400
    row = cast(
        tuple[int] | None,
        conn.execute(
            """
            SELECT ads.dialog_id
            FROM activity_dialog_state AS ads
            LEFT JOIN synced_dialogs AS sd ON sd.dialog_id = ads.dialog_id
            WHERE (ads.last_activity_at IS NULL OR ads.last_activity_at >= :cutoff)
              AND (ads.hot_next_retry_at IS NULL OR ads.hot_next_retry_at <= :now)
              AND (
                    ads.hot_page_offset_id IS NOT NULL
                    OR ads.hot_next_due_at IS NULL
                    OR ads.hot_next_due_at <= :now
                    OR (ads.hot_last_sync_at IS NOT NULL AND sd.last_event_at > ads.hot_last_sync_at)
              )
              AND COALESCE(sd.status, '') != 'access_lost'
            ORDER BY
              CASE WHEN ads.hot_page_offset_id IS NULL THEN 1 ELSE 0 END,
              COALESCE(ads.hot_next_retry_at, ads.hot_next_due_at, 0),
              ads.dialog_id
            LIMIT 1
            """,
            {"cutoff": cutoff, "now": now},
        ).fetchone(),
    )
    if row is None:
        return None
    return _begin_hot_window(conn, row[0])


def _save_hot_window_progress(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    page_offset_id: int,
    window_max_id: int,
    had_new: bool,
) -> None:
    """Commit one complete page's continuation and observed window maximum."""
    with conn:
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
    demand_kind = DemandKind.HOT_ACTIVITY_PAGE

    def status(self, now: float) -> DemandStatus | None:
        """Return the earliest release among enrollment and hot-page work."""
        page_release_at = _next_hot_release_at(self.conn, now=now)
        enrollment_release_at = working_set_enrollment_release_at(
            self.conn,
            now=now,
            cadence_s=self.policy.loop_interval_seconds,
        )
        if enrollment_release_at is None:
            release_at = page_release_at
        elif page_release_at is None:
            release_at = enrollment_release_at
        else:
            release_at = min(page_release_at, enrollment_release_at)
        if release_at is None:
            return None
        return DemandStatus(release_at=release_at)

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
                                min_id=state.committed_cursor + 1 if state.committed_cursor else 0,
                                limit=_BACKFILL_BATCH_LIMIT,
                                timeout_s=self.timeout_s,
                                hydration_priority=HydrationPriority.FOREGROUND,
                            )
                        except RpcAttemptBudgetExhaustedError:
                            return None

    def _persist_hot_page_result(self, state: _HotWindowState, result: SweepResult) -> None:
        """Persist the page outcome while keeping an incomplete window resumable."""
        max_seen = max(state.window_max_id, result.max_id or 0)
        had_new = state.had_new or result.genuinely_new > 0
        if result.flood_wait_seconds is not None:
            _save_hot_flood_state(
                self.conn,
                state.dialog_id,
                next_retry_at=int(time.time()) + result.flood_wait_seconds,
            )
            return
        if result.skip_reason is SkipReason.ACCESS_SKIP:
            _save_hot_access_skip_state(
                self.conn,
                state.dialog_id,
                retry_at=int(time.time()) + _ACCESS_SKIP_RETRY_S,
            )
            return
        if _is_hot_page_drained(result) and not self.shutdown_event.is_set():
            _save_hot_completed_state(
                self.conn,
                state.dialog_id,
                hot_cursor=max_seen,
                completion_at=int(time.time()),
                genuinely_new=int(had_new),
                policy=self.policy,
            )
            return
        if result.min_id is None:
            _save_hot_min_id_gap_state(self.conn, state.dialog_id, now=int(time.time()))
            return
        _save_hot_window_progress(
            self.conn,
            state.dialog_id,
            page_offset_id=result.min_id,
            window_max_id=max_seen,
            had_new=had_new,
        )

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Run one enrollment unit or one bounded hot page."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        if self.shutdown_event.is_set():
            return
        with _hot_demand_scope():
            with rpc_attempt_budget(budget):
                enrollment_release_at = working_set_enrollment_release_at(
                    self.conn,
                    now=float(int(time.time())),
                    cadence_s=self.policy.loop_interval_seconds,
                )
                if enrollment_release_at is not None and enrollment_release_at <= time.time():
                    enrollment = await run_working_set_enrollment_slice(
                        self.client,
                        self.conn,
                        source=TelegramRpcSource.ACTIVITY_HOT_SWEEP,
                        cadence_s=self.policy.loop_interval_seconds,
                        timeout_s=self.timeout_s,
                    )
                    if enrollment.consumed:
                        # A due candidate, including a channel with no linked
                        # discussion group, consumed this bounded unit. Budget
                        # exhaustion leaves its durable cursor unchanged. The
                        # terminal scan marker also occupies this slice.
                        return

                state = _select_due_hot_window(self.conn, now=int(time.time()))
                if state is None or self.shutdown_event.is_set():
                    return
                result = await self._fetch_hot_page(state, budget)
                if result is None:
                    return
                self._persist_hot_page_result(state, result)


def _is_hot_page_drained(result: SweepResult) -> bool:
    """Return True when the current page fully drained the newest-side window."""
    return (
        result.hit_floor
        or result.skip_reason is SkipReason.HISTORY_FLOOR
        or len(result.fetched_ids) < _BACKFILL_BATCH_LIMIT
    )


def _save_hot_flood_state(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    next_retry_at: int,
) -> None:
    """Persist retry timing without committing an incomplete newest window."""
    _save_dialog_state(conn, dialog_id, hot_next_retry_at=next_retry_at)


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
    with conn:
        row = cast(
            tuple[int],
            conn.execute(
                "SELECT hot_empty_streak FROM activity_dialog_state WHERE dialog_id = ?", (dialog_id,)
            ).fetchone(),
        )
        streak = 0 if genuinely_new > 0 else int(row[0]) + 1
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


def _load_hot_release_rows(
    conn: sqlite3.Connection,
    *,
    cutoff: int,
) -> list[tuple[int | None, int | None, int | None, int | None, int | None]]:
    """Load eligible hot peers and their scheduling boundaries."""
    return cast(
        list[tuple[int | None, int | None, int | None, int | None, int | None]],
        conn.execute(
            """
            SELECT ads.hot_next_due_at,
                   ads.hot_next_retry_at,
                   ads.hot_last_sync_at,
                   sd.last_event_at,
                   ads.hot_page_offset_id
            FROM activity_dialog_state AS ads
            LEFT JOIN synced_dialogs AS sd ON sd.dialog_id = ads.dialog_id
            WHERE (ads.last_activity_at IS NULL OR ads.last_activity_at >= :cutoff)
              AND COALESCE(sd.status, '') != 'access_lost'
            """,
            {"cutoff": cutoff},
        ).fetchall(),
    )


def _hot_row_release_at(row: tuple[int | None, int | None, int | None, int | None, int | None]) -> float:
    """Return the effective release boundary for one eligible hot peer."""
    next_due_at, next_retry_at, last_sync_at, last_event_at, page_offset_id = row
    event_due = last_sync_at is not None and last_event_at is not None and last_event_at > last_sync_at
    due_at = 0 if page_offset_id is not None or next_due_at is None or event_due else next_due_at
    return float(max(due_at, next_retry_at or 0))


def _next_hot_release_at(conn: sqlite3.Connection, *, now: float) -> float | None:
    """Return the earliest authoritative hot-page release boundary."""
    cutoff = _hot_release_cutoff(now)
    rows = _load_hot_release_rows(conn, cutoff=cutoff)
    return min((_hot_row_release_at(row) for row in rows), default=None)
