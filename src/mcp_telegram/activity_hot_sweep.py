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
    WorkingSetResult,
    _save_dialog_state,
    build_working_set,
    run_working_set_enrollment_slice,
    sweep_peer_once,
    working_set_enrollment_release_at,
)
from .activity_substrate import ActivityClient
from .flood import TelegramRpcThrottled
from .hydration_queue import HydrationPriority
from .maintenance_logging import log_maintenance_cycle
from .messages.sqlite_bundle import message_log_context
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
    RpcAdmissionClosedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)

logger = logging.getLogger(__name__)

_BACKFILL_BATCH_LIMIT = 100
# Short transient backoff for ACCESS_SKIP (peer unresolved / timeout).
_ACCESS_SKIP_RETRY_S = 300  # 5 minutes


def deterministic_hot_due_at(dialog_id: int, spread_seconds: float, *, now: int | None = None) -> int:
    """Return a stable enrollment offset for a peer within the configured spread."""
    if spread_seconds <= 0:
        return int(time.time() if now is None else now)
    span = max(1, int(spread_seconds))
    digest = hashlib.blake2b(str(dialog_id).encode("ascii"), digest_size=8).digest()
    offset = int.from_bytes(digest, "big") % span
    return int(time.time() if now is None else now) + offset


def seed_hot_sweep_schedule(
    conn: sqlite3.Connection,
    spread_seconds: float,
    *,
    now: int | None = None,
) -> int:
    """Stagger eligible rows that predate durable HotSweep due timestamps."""
    at = int(time.time() if now is None else now)
    cutoff = at - 30 * 86400
    rows = cast(
        list[tuple[int]],
        conn.execute(
            """
            SELECT ads.dialog_id
            FROM activity_dialog_state AS ads
            LEFT JOIN synced_dialogs AS sd ON sd.dialog_id = ads.dialog_id
            WHERE ads.hot_next_due_at IS NULL
              AND (ads.last_activity_at IS NULL OR ads.last_activity_at >= ?)
              AND COALESCE(sd.status, '') != 'access_lost'
            ORDER BY ads.dialog_id
            """,
            (cutoff,),
        ).fetchall(),
    )
    with conn:
        for (dialog_id,) in rows:
            conn.execute(
                "UPDATE activity_dialog_state SET hot_next_due_at = ?, updated_at = ? "
                "WHERE dialog_id = ? AND hot_next_due_at IS NULL",
                (deterministic_hot_due_at(dialog_id, spread_seconds, now=at), at, dialog_id),
            )
    return len(rows)


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

    @property
    def initial_spread_seconds(self) -> float: ...


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


@dataclass
class _HotSweepPeerOutcome:
    """Outcome for one peer within a hot sweep pass."""

    flooded: bool
    completed: bool
    pages_fetched: int
    rpc_calls: int
    extracted: int
    genuinely_new: int
    genuinely_new_keys: frozenset[tuple[int, int]] = frozenset()
    flood_wait_seconds: int | None = None


@dataclass
class _HotSweepPeerContext:
    """Context for processing a single peer in HotSweep."""

    client: ActivityClient
    conn: sqlite3.Connection
    dialog_id: int
    old_hot_cursor: int | None
    now: int
    shutdown_event: asyncio.Event
    timeout_s: float
    policy: HotSweepPolicy


@dataclass(frozen=True, slots=True)
class _HotPageContext:
    peer: _HotSweepPeerContext
    started_at: float
    pages_fetched: int
    max_seen: int
    result: SweepResult


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


def _handle_hot_sweep_page_result(ctx: _HotPageContext) -> tuple[_HotSweepPeerOutcome, int | None, int]:
    """Apply one fetched page result and emit the matching telemetry."""
    result = ctx.result
    peer = ctx.peer
    max_seen = ctx.max_seen
    if result.flood_wait_seconds is not None:
        next_retry_at = int(time.time()) + result.flood_wait_seconds
        _save_hot_flood_state(
            peer.conn,
            peer.dialog_id,
            next_retry_at=next_retry_at,
        )
        logger.warning(
            "activity_hot_sweep_flood dialog_id=%r flood_wait_seconds=%d"
            " retry_delay_s=%d max_seen=%d pages_fetched=%d duration_s=%.3f"
            " — halting pass (account-global wait)",
            peer.dialog_id,
            result.flood_wait_seconds,
            result.flood_wait_seconds,
            max_seen,
            ctx.pages_fetched,
            time.monotonic() - ctx.started_at,
        )
        return (
            _HotSweepPeerOutcome(
                flooded=True,
                completed=False,
                pages_fetched=ctx.pages_fetched,
                rpc_calls=result.rpc_calls,
                extracted=result.extracted,
                genuinely_new=result.genuinely_new,
                genuinely_new_keys=result.genuinely_new_keys,
                flood_wait_seconds=result.flood_wait_seconds,
            ),
            None,
            max_seen,
        )

    if result.skip_reason is SkipReason.ACCESS_SKIP:
        transient_retry_at = int(time.time()) + _ACCESS_SKIP_RETRY_S
        _save_hot_access_skip_state(peer.conn, peer.dialog_id, retry_at=transient_retry_at)
        logger.debug(
            "activity_hot_sweep_access_skip dialog_id=%r retry_at=%d pages_fetched=%d retry_delay_s=%d duration_s=%.3f",
            peer.dialog_id,
            transient_retry_at,
            ctx.pages_fetched,
            _ACCESS_SKIP_RETRY_S,
            time.monotonic() - ctx.started_at,
        )
        return (
            _HotSweepPeerOutcome(
                flooded=False,
                completed=False,
                pages_fetched=ctx.pages_fetched,
                rpc_calls=result.rpc_calls,
                extracted=result.extracted,
                genuinely_new=result.genuinely_new,
                genuinely_new_keys=result.genuinely_new_keys,
            ),
            None,
            max_seen,
        )

    if result.max_id is not None:
        max_seen = max(max_seen, result.max_id)

    if _is_hot_page_drained(result):
        logger.debug(
            "activity_hot_sweep_peer_done dialog_id=%r hot_cursor=%d pages_fetched=%d duration_s=%.3f",
            peer.dialog_id,
            max_seen,
            ctx.pages_fetched,
            time.monotonic() - ctx.started_at,
        )
        return (
            _HotSweepPeerOutcome(
                flooded=False,
                completed=True,
                pages_fetched=ctx.pages_fetched,
                rpc_calls=result.rpc_calls,
                extracted=result.extracted,
                genuinely_new=result.genuinely_new,
                genuinely_new_keys=result.genuinely_new_keys,
            ),
            None,
            max_seen,
        )

    if result.min_id is None:
        _save_hot_min_id_gap_state(peer.conn, peer.dialog_id, now=peer.now)
        logger.debug(
            "activity_hot_sweep_min_id_gap dialog_id=%r hot_cursor=%d pages_fetched=%d duration_s=%.3f",
            peer.dialog_id,
            max_seen,
            ctx.pages_fetched,
            time.monotonic() - ctx.started_at,
        )
        return (
            _HotSweepPeerOutcome(
                flooded=False,
                completed=False,
                pages_fetched=ctx.pages_fetched,
                rpc_calls=result.rpc_calls,
                extracted=result.extracted,
                genuinely_new=result.genuinely_new,
                genuinely_new_keys=result.genuinely_new_keys,
            ),
            None,
            max_seen,
        )

    return (
        _HotSweepPeerOutcome(
            flooded=False,
            completed=False,
            pages_fetched=ctx.pages_fetched,
            rpc_calls=result.rpc_calls,
            extracted=result.extracted,
            genuinely_new=result.genuinely_new,
            genuinely_new_keys=result.genuinely_new_keys,
        ),
        result.min_id,
        max_seen,
    )


async def _run_hot_sweep_peer(ctx: _HotSweepPeerContext) -> _HotSweepPeerOutcome:
    """Process one peer across all needed pages for the current hot sweep pass."""
    started_at = time.monotonic()
    state = _begin_hot_window(ctx.conn, ctx.dialog_id)
    if state is None:
        return _HotSweepPeerOutcome(False, False, 0, 0, 0, 0)
    pass_min_id = state.committed_cursor + 1 if state.committed_cursor else 0
    max_seen = state.window_max_id
    page_offset = state.page_offset_id
    window_had_new = state.had_new
    pages_fetched = 0
    total_rpc_calls = 0
    total_extracted = 0
    genuinely_new_keys: set[tuple[int, int]] = set()

    while not ctx.shutdown_event.is_set():
        result: SweepResult = await sweep_peer_once(
            ctx.client,
            ctx.conn,
            ctx.dialog_id,
            offset_id=page_offset,
            min_id=pass_min_id,
            limit=_BACKFILL_BATCH_LIMIT,
            timeout_s=ctx.timeout_s,
            hydration_priority=HydrationPriority.FOREGROUND,
        )
        pages_fetched += result.pages_fetched
        outcome, next_offset, max_seen = _handle_hot_sweep_page_result(
            _HotPageContext(
                peer=ctx,
                started_at=started_at,
                pages_fetched=pages_fetched,
                max_seen=max_seen,
                result=result,
            )
        )
        total_rpc_calls += result.rpc_calls
        total_extracted += result.extracted
        genuinely_new_keys.update(result.genuinely_new_keys)
        window_had_new = window_had_new or result.genuinely_new > 0
        if next_offset is None:
            if outcome.completed:
                if ctx.shutdown_event.is_set():
                    _save_hot_min_id_gap_state(ctx.conn, ctx.dialog_id, now=ctx.now)
                    _save_hot_window_progress(
                        ctx.conn,
                        ctx.dialog_id,
                        page_offset_id=page_offset,
                        window_max_id=max_seen,
                        had_new=window_had_new,
                    )
                else:
                    completion_at = int(time.time())
                    _save_hot_completed_state(
                        ctx.conn,
                        ctx.dialog_id,
                        hot_cursor=max_seen,
                        completion_at=completion_at,
                        genuinely_new=int(window_had_new),
                        policy=ctx.policy,
                    )
            elif result.flood_wait_seconds is None and result.skip_reason is not SkipReason.ACCESS_SKIP:
                _save_hot_window_progress(
                    ctx.conn,
                    ctx.dialog_id,
                    page_offset_id=page_offset,
                    window_max_id=max_seen,
                    had_new=window_had_new,
                )
            return _HotSweepPeerOutcome(
                flooded=outcome.flooded,
                completed=outcome.completed,
                pages_fetched=pages_fetched,
                rpc_calls=total_rpc_calls,
                extracted=total_extracted,
                genuinely_new=len(genuinely_new_keys),
                genuinely_new_keys=frozenset(genuinely_new_keys),
                flood_wait_seconds=outcome.flood_wait_seconds,
            )
        _save_hot_window_progress(
            ctx.conn,
            ctx.dialog_id,
            page_offset_id=next_offset,
            window_max_id=max_seen,
            had_new=window_had_new,
        )
        page_offset = next_offset

    logger.debug(
        "activity_hot_sweep_peer_shutdown dialog_id=%r pages_fetched=%d duration_s=%.3f",
        ctx.dialog_id,
        pages_fetched,
        time.monotonic() - started_at,
    )
    return _HotSweepPeerOutcome(
        flooded=False,
        completed=False,
        pages_fetched=pages_fetched,
        rpc_calls=total_rpc_calls,
        extracted=total_extracted,
        genuinely_new=len(genuinely_new_keys),
        genuinely_new_keys=frozenset(genuinely_new_keys),
    )


async def _refresh_hot_working_set(
    client: ActivityClient,
    conn: sqlite3.Connection,
    *,
    timeout_s: float,
) -> WorkingSetResult:
    with rpc_scope(
        TelegramRpcSource.ACTIVITY_HOT_SWEEP,
        timeout_seconds=timeout_s,
        acquisition_kind=AcquisitionKind.DIALOG_TRAVERSAL,
    ):
        return await build_working_set(client, conn, timeout_s=timeout_s)


def _seed_hot_schedule_after_refresh(
    conn: sqlite3.Connection, policy: HotSweepPolicy, working_set: WorkingSetResult, *, now: int
) -> None:
    if working_set.flood_wait_seconds is None:
        seed_hot_sweep_schedule(conn, policy.initial_spread_seconds, now=now)


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


def _count_due_hot_peers(conn: sqlite3.Connection, *, now: int) -> int:
    cutoff = now - 30 * 86400
    row = cast(
        tuple[int] | None,
        conn.execute(
            """
        SELECT COUNT(*)
        FROM activity_dialog_state AS ads
        LEFT JOIN synced_dialogs AS sd ON sd.dialog_id = ads.dialog_id
        WHERE (ads.last_activity_at IS NULL OR ads.last_activity_at >= :cutoff)
          AND (ads.hot_next_retry_at IS NULL OR ads.hot_next_retry_at <= :now)
          AND (ads.hot_page_offset_id IS NOT NULL
               OR ads.hot_next_due_at IS NULL OR ads.hot_next_due_at <= :now
               OR (ads.hot_last_sync_at IS NOT NULL AND sd.last_event_at > ads.hot_last_sync_at))
          AND COALESCE(sd.status, '') != 'access_lost'
        """,
            {"cutoff": cutoff, "now": now},
        ).fetchone(),
    )
    return int(row[0]) if row is not None else 0


async def _run_hot_sweep_peer_safe(ctx: _HotSweepPeerContext) -> _HotSweepPeerOutcome:
    try:
        with rpc_scope(
            TelegramRpcSource.ACTIVITY_HOT_SWEEP,
            timeout_seconds=ctx.timeout_s,
            acquisition_kind=AcquisitionKind.MESSAGE_SEARCH_PAGE,
        ):
            return await _run_hot_sweep_peer(ctx)
    except RpcAdmissionClosedError, TelegramRpcAdmissionDeferred:
        raise
    except TelegramRpcThrottled:
        raise
    except Exception:
        _save_hot_access_skip_state(ctx.conn, ctx.dialog_id, retry_at=int(time.time()) + _ACCESS_SKIP_RETRY_S)
        logger.warning("activity_hot_sweep_peer_error dialog_id=%r", ctx.dialog_id, exc_info=True)
        return _HotSweepPeerOutcome(
            flooded=False,
            completed=False,
            pages_fetched=0,
            rpc_calls=0,
            extracted=0,
            genuinely_new=0,
        )


async def _run_hot_sweep_peer_for_pass(
    ctx: _HotSweepPeerContext,
) -> tuple[_HotSweepPeerOutcome | None, int | None]:
    """Run one peer and turn recoverable admission pressure into a pass result."""
    try:
        return await _run_hot_sweep_peer_safe(ctx), None
    except TelegramRpcAdmissionDeferred as exc:
        return None, exc.retry_after_seconds


def _select_hot_flood_wait_seconds(
    current: int | None,
    peer_result: _HotSweepPeerOutcome,
) -> int | None:
    """Keep the latest account-wide FloodWait duration for pass telemetry."""
    return peer_result.flood_wait_seconds if peer_result.flood_wait_seconds is not None else current


def _log_recovered_messages(
    conn: sqlite3.Connection,
    outcome: _HotSweepPeerOutcome,
    *,
    prior_hot_cursor: int | None,
    discovered_at: int,
) -> None:
    """Log safe coordinates for messages recovered by the hourly safety net."""
    discovery_scope = "baseline" if prior_hot_cursor is None else "incremental"
    for dialog_id, message_id in sorted(outcome.genuinely_new_keys):
        context = message_log_context(conn, dialog_id, message_id)
        telegram_sent_at = context.telegram_sent_at
        discovery_lag_s = max(0, discovered_at - telegram_sent_at) if telegram_sent_at is not None else None
        logger.info(
            "activity_hot_sweep_message_recovered dialog_id=%d message_id=%d"
            " telegram_sent_at=%r discovered_at=%d discovery_lag_s=%r"
            " discovery_scope=%s prior_hot_cursor=%r",
            context.dialog_id,
            context.message_id,
            telegram_sent_at,
            discovered_at,
            discovery_lag_s,
            discovery_scope,
            prior_hot_cursor,
        )


async def _run_hot_sweep_pass(  # noqa: PLR0914 - explicit pass telemetry counters
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    policy: HotSweepPolicy,
    timeout_s: float,
) -> dict[str, int | float | bool | None]:
    """Run one Tier-A HotSweep pass.

    1. Calls build_working_set to refresh last_activity_at for enrolled peers.
    2. Selects hot, due peers (active within 30 days, retry due).
    3. For each peer, pages the ENTIRE newest window (concern 2 multi-batch fix)
       before committing hot_cursor = max_seen.
    4. Handles FloodWait (concern 5) and ACCESS_SKIP (concern 3) per-tier.

    Returns pass telemetry.
    """
    started_at = time.monotonic()
    now = int(time.time())

    # Step 1: cheap working-set refresh — also refreshes last_activity_at
    working_set = await _refresh_hot_working_set(
        client,
        conn,
        timeout_s=timeout_s,
    )

    if shutdown_event.is_set():
        return {
            "peers_selected": 0,
            "peers_processed": 0,
            "due_remaining": _count_due_hot_peers(conn, now=now),
            "pages_fetched": 0,
            "rpc_calls": 0,
            "extracted": 0,
            "genuinely_new": 0,
            "yielding_peers": 0,
            "flooded": working_set.flood_wait_seconds is not None,
            "flood_wait_seconds": working_set.flood_wait_seconds,
            "duration_s": time.monotonic() - started_at,
        }
    _seed_hot_schedule_after_refresh(conn, policy, working_set, now=now)

    # Step 2: select hot, due peers — recency-bounded to 30 days
    cutoff = now - 30 * 86400
    rows = cast(
        list[tuple[int, int | None]],
        conn.execute(
            """
        SELECT ads.dialog_id, ads.hot_cursor
        FROM activity_dialog_state AS ads
        LEFT JOIN synced_dialogs AS sd ON sd.dialog_id = ads.dialog_id
        WHERE (ads.last_activity_at IS NULL OR ads.last_activity_at >= :cutoff)
          AND (ads.hot_next_retry_at IS NULL OR ads.hot_next_retry_at <= :now)
          AND (
                ads.hot_page_offset_id IS NOT NULL
                OR ads.hot_next_due_at IS NULL
                OR ads.hot_next_due_at <= :now
                OR (
                    ads.hot_last_sync_at IS NOT NULL
                    AND sd.last_event_at > ads.hot_last_sync_at
                )
          )
          AND COALESCE(sd.status, '') != 'access_lost'
          AND :working_set_flooded = 0
            ORDER BY
            CASE WHEN ads.hot_page_offset_id IS NULL THEN 1 ELSE 0 END,
            COALESCE(ads.hot_next_retry_at, ads.hot_next_due_at, 0) ASC,
            ads.dialog_id ASC
        LIMIT :max_peers
        """,
            {
                "cutoff": cutoff,
                "now": now,
                "max_peers": policy.max_peers_per_pass,
                "working_set_flooded": int(working_set.flood_wait_seconds is not None),
            },
        ).fetchall(),
    )

    logger.debug("activity_hot_sweep_pass_start peers_selected=%d", len(rows))

    peers_processed = 0
    flooded = False
    pages_fetched = 0
    rpc_calls = 0
    extracted = 0
    genuinely_new = 0
    yielding_peers = 0
    flood_wait_seconds = working_set.flood_wait_seconds
    admission_deferred = False
    retry_after_seconds: int | None = None

    for dialog_id, old_hot_cursor in rows:
        if shutdown_event.is_set():
            break

        peer_result, retry_after_seconds = await _run_hot_sweep_peer_for_pass(
            _HotSweepPeerContext(
                client=client,
                conn=conn,
                dialog_id=dialog_id,
                old_hot_cursor=old_hot_cursor,
                now=now,
                shutdown_event=shutdown_event,
                timeout_s=timeout_s,
                policy=policy,
            )
        )
        if peer_result is None:
            admission_deferred = True
            logger.warning(
                "activity_hot_sweep_admission_deferred dialog_id=%r retry_after_seconds=%s"
                " — preserving deferred peer cursor",
                dialog_id,
                retry_after_seconds,
            )
            break
        peers_processed += 1
        pages_fetched += peer_result.pages_fetched
        rpc_calls += peer_result.rpc_calls
        extracted += peer_result.extracted
        genuinely_new += peer_result.genuinely_new
        _log_recovered_messages(
            conn,
            peer_result,
            prior_hot_cursor=old_hot_cursor,
            discovered_at=int(time.time()),
        )
        flood_wait_seconds = _select_hot_flood_wait_seconds(flood_wait_seconds, peer_result)
        if peer_result.completed and peer_result.genuinely_new > 0:
            yielding_peers += 1

        # Account-global FloodWait hit on this peer — do not advance to the next
        # peer (that would send another request during the wait window).
        if peer_result.flooded:
            flooded = True
            break

    due_remaining = _count_due_hot_peers(conn, now=int(time.time()))
    telemetry: dict[str, int | float | bool | None] = {
        "peers_selected": len(rows),
        "peers_processed": peers_processed,
        "due_remaining": due_remaining,
        "pages_fetched": pages_fetched,
        "rpc_calls": rpc_calls,
        "extracted": extracted,
        "genuinely_new": genuinely_new,
        "yielding_peers": yielding_peers,
        "flooded": flooded or working_set.flood_wait_seconds is not None,
        "flood_wait_seconds": flood_wait_seconds,
        "admission_deferred": admission_deferred,
        "retry_after_seconds": retry_after_seconds,
        "duration_s": time.monotonic() - started_at,
    }
    log_maintenance_cycle(
        logger,
        any((genuinely_new, flooded, admission_deferred, working_set.flood_wait_seconds is not None, due_remaining)),
        "activity_hot_sweep_pass_done peers_selected=%d peers_processed=%d due_remaining=%d"
        " pages_fetched=%d rpc_calls=%d extracted=%d genuinely_new=%d yielding_peers=%d"
        " flooded=%s flood_wait_seconds=%r admission_deferred=%s retry_after_seconds=%r duration_s=%.3f",
        len(rows),
        peers_processed,
        due_remaining,
        pages_fetched,
        rpc_calls,
        extracted,
        genuinely_new,
        yielding_peers,
        flooded or working_set.flood_wait_seconds is not None,
        flood_wait_seconds,
        admission_deferred,
        retry_after_seconds,
        time.monotonic() - started_at,
    )
    return telemetry


def _admission_deferred_telemetry(
    conn: sqlite3.Connection, retry_after_seconds: int | None
) -> dict[str, int | float | bool | None]:
    """Return a retryable pass result without changing peer progress."""
    return {
        "peers_selected": 0,
        "peers_processed": 0,
        "due_remaining": _count_due_hot_peers(conn, now=int(time.time())),
        "pages_fetched": 0,
        "rpc_calls": 0,
        "extracted": 0,
        "genuinely_new": 0,
        "yielding_peers": 0,
        "flooded": False,
        "flood_wait_seconds": None,
        "retry_after_seconds": retry_after_seconds,
        "admission_deferred": True,
        "duration_s": 0.0,
    }


async def run_hot_sweep_pass(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    policy: HotSweepPolicy,
    timeout_s: float,
) -> dict[str, int | float | bool | None]:
    """Run one pass, turning local admission deferral into retry telemetry."""
    try:
        with _hot_demand_scope():
            return await _run_hot_sweep_pass(client, conn, shutdown_event, policy=policy, timeout_s=timeout_s)
    except TelegramRpcAdmissionDeferred as exc:
        logger.warning(
            "activity_hot_sweep_admission_deferred retry_after_seconds=%s",
            exc.retry_after_seconds,
        )
        return _admission_deferred_telemetry(conn, exc.retry_after_seconds)
