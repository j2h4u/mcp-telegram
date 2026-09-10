"""Tier B — ColdBackfill — full-history low-priority per-peer self-search scheduler.

Walks each enrolled peer's complete authored history BACKWARD (no time ceiling),
advancing cold_offset_id toward the oldest message and marking cold_status='complete'
only when a genuine HISTORY_FLOOR is reached.

Key design constraints (from plan reviews):
- cold_status='complete' is set ONLY on SkipReason.HISTORY_FLOOR — a genuine empty
  batch from a reachable peer (concern 3).
- SkipReason.ACCESS_SKIP sets cold_next_retry_at + cold_status='pending' and leaves
  cold_offset_id unchanged — a transient cache/session miss can NEVER permanently
  mark cold backfill complete.
- SkipReason.FLOOD_WAIT sets cold_next_retry_at (not any hot_* field) — Tier B is
  the sole owner of durable FloodWait retry for the cold path (concern 5).
- cold_offset_id walks downward: each non-empty batch sets
  cold_offset_id = result.min_id.
- NO hot_* column is ever written here.
- run_cold_backfill_pass returns a structured ColdPassResult for one bounded demand
  slice, preserving the distinction between idle and zero-write work.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from .activity_peer_sweep import (
    SkipReason,
    SweepResult,
    run_working_set_enrollment_slice,
    sweep_peer_once,
    working_set_enrollment_release_at,
)
from .activity_substrate import ActivityClient
from .hydration_queue import HydrationPriority
from .sync_read_model import SyncStatus
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
    current_demand_token,
    demand_context,
)
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import TelegramRpcSource, rpc_attempt_budget, rpc_scope

logger = logging.getLogger(__name__)


@contextmanager
def _cold_demand_scope() -> Iterator[None]:
    """Install the cold-page root for direct legacy or adapter execution."""
    try:
        token = current_demand_token()
    except UnclassifiedTelegramDemandError:
        with demand_context(DemandKind.COLD_PEER_PAGE):
            yield
        return
    if token.kind is not DemandKind.COLD_PEER_PAGE:
        raise RuntimeError(f"active demand kind {token.kind.value} cannot execute cold peer page")
    yield


class _ColdBackfillScheduling(Protocol):
    """Scheduling values injected by the daemon-owned configuration tree."""

    @property
    def activity_cold_backfill_batch_pause_seconds(self) -> float: ...

    @property
    def activity_cold_enroll_seconds(self) -> float: ...

    @property
    def activity_cold_access_retry_seconds(self) -> float: ...


# ---------------------------------------------------------------------------
# Pacing is owned by the frozen runtime configuration model.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColdBackfillHistoryPacing:
    batch_s: float
    enroll_s: float
    access_retry_s: float


@dataclass(frozen=True, slots=True)
class ColdBackfillPacing:
    history: ColdBackfillHistoryPacing

    @classmethod
    def from_scheduling(cls, scheduling: _ColdBackfillScheduling) -> ColdBackfillPacing:
        return cls(
            history=ColdBackfillHistoryPacing(
                batch_s=scheduling.activity_cold_backfill_batch_pause_seconds,
                enroll_s=scheduling.activity_cold_enroll_seconds,
                access_retry_s=scheduling.activity_cold_access_retry_seconds,
            ),
        )


@dataclass(slots=True)
class ColdPeerPageDemandAdapter(DurableDemandAdapter):
    """Expose cold peer-page readiness over ``activity_dialog_state``."""

    client: ActivityClient
    conn: sqlite3.Connection
    shutdown_event: asyncio.Event
    pacing: ColdBackfillPacing
    timeout_s: float
    demand_kind = DemandKind.COLD_PEER_PAGE

    def status(self, now: float) -> DemandStatus | None:
        """Return the earliest release among enrollment and peer pages."""
        page_release_at = _next_cold_release_at(self.conn, now=now)
        enrollment_release_at = working_set_enrollment_release_at(
            self.conn,
            now=now,
            cadence_s=self.pacing.history.enroll_s,
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

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Run one enrollment unit or one claimed peer page."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        if self.shutdown_event.is_set():
            return
        with _cold_demand_scope():
            with rpc_attempt_budget(budget):
                enrollment_release_at = working_set_enrollment_release_at(
                    self.conn,
                    now=float(int(time.time())),
                    cadence_s=self.pacing.history.enroll_s,
                )
                if enrollment_release_at is not None and enrollment_release_at <= time.time():
                    enrollment = await run_working_set_enrollment_slice(
                        self.client,
                        self.conn,
                        source=TelegramRpcSource.ACTIVITY_COLD_BACKFILL,
                        cadence_s=self.pacing.history.enroll_s,
                        timeout_s=self.timeout_s,
                    )
                    if enrollment.consumed:
                        # Keep the enrollment cursor at the candidate when the
                        # local attempt budget is exhausted; the next slice gets
                        # a fresh budget and resumes the same bounded unit.
                        return
                await run_cold_backfill_pass(
                    self.client,
                    self.conn,
                    self.shutdown_event,
                    pacing=self.pacing,
                    timeout_s=self.timeout_s,
                )


_BACKFILL_BATCH_LIMIT = 100


# ---------------------------------------------------------------------------
# Structured result — cycle-4 MEDIUM: NOT a bare int
# ---------------------------------------------------------------------------


class ColdPassOutcome(StrEnum):
    NO_DUE_PEER = "no_due_peer"
    # No peer is due; loop should sleep the long idle interval.
    WROTE = "wrote"
    # A peer was processed and persisted > 0 rows.
    ZERO_PERSISTED = "zero_persisted"
    # A peer WAS processed but wrote 0 rows (HISTORY_FLOOR or ACCESS_SKIP).
    # The loop must NOT idle 300s while more peers are due.
    FLOOD_WAIT = "flood_wait"
    # A peer encountered FloodWait; cold_next_retry_at was written.


@dataclass
class ColdPassResult:
    """Structured result from run_cold_backfill_pass."""

    outcome: ColdPassOutcome
    persisted: int  # count of rows written this pass; 0 unless outcome==WROTE
    flood_wait_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class _ColdPeerFinishContext:
    conn: sqlite3.Connection
    dialog_id: int
    offset_id: int
    started_at: float
    now: int
    claim_until: int
    result: SweepResult


def _next_cold_release_at(conn: sqlite3.Connection, *, now: float) -> float | None:
    """Return the earliest authoritative cold-page release boundary."""
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise ValueError("now must be a finite non-negative timestamp")
    row = cast(
        tuple[int] | None,
        conn.execute(
            """
            SELECT MIN(COALESCE(ads.cold_next_retry_at, 0))
            FROM activity_dialog_state AS ads
            LEFT JOIN synced_dialogs AS sd ON sd.dialog_id = ads.dialog_id
            WHERE ads.cold_status != 'complete'
              AND COALESCE(sd.status, '') != :ineligible_status
            """,
            {"ineligible_status": SyncStatus.ACCESS_LOST.value},
        ).fetchone(),
    )
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _claim_cold_backfill_peer(
    conn: sqlite3.Connection,
    *,
    now: int,
    claim_until: int,
) -> tuple[int, int | None] | None:
    """Atomically claim the oldest due peer using the existing retry field as a lease."""
    with conn:
        return cast(
            tuple[int, int | None] | None,
            conn.execute(
                """
                UPDATE activity_dialog_state
                SET cold_status = 'running',
                    cold_next_retry_at = :claim_until,
                    updated_at = :now
                WHERE dialog_id = (
                    SELECT ads.dialog_id
                    FROM activity_dialog_state AS ads
                    LEFT JOIN synced_dialogs AS sd ON sd.dialog_id = ads.dialog_id
                    WHERE ads.cold_status != 'complete'
                      AND (ads.cold_next_retry_at IS NULL OR ads.cold_next_retry_at <= :now)
                      AND COALESCE(sd.status, '') != :ineligible_status
                    ORDER BY ads.updated_at ASC, ads.dialog_id ASC
                    LIMIT 1
                )
                  AND cold_status != 'complete'
                  AND (cold_next_retry_at IS NULL OR cold_next_retry_at <= :now)
                RETURNING dialog_id, cold_offset_id
                """,
                {
                    "now": now,
                    "claim_until": claim_until,
                    "ineligible_status": SyncStatus.ACCESS_LOST.value,
                },
            ).fetchone(),
        )


def _save_claimed_cold_state(ctx: _ColdPeerFinishContext, **fields: object) -> bool:
    """Apply a result only while this slice still owns the peer lease."""
    allowed = {"cold_offset_id", "cold_status", "cold_next_retry_at", "cold_last_error"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown claimed cold fields {unknown!r}")
    assignments = ", ".join(f"{column} = ?" for column in fields)
    values = [*fields.values(), int(time.time()), ctx.dialog_id, ctx.claim_until]
    with ctx.conn:
        cursor = ctx.conn.execute(
            f"UPDATE activity_dialog_state SET {assignments}, updated_at = ? "
            "WHERE dialog_id = ? AND cold_status = 'running' AND cold_next_retry_at = ?",
            values,
        )
    return cursor.rowcount == 1


def _release_cold_claim(conn: sqlite3.Connection, *, dialog_id: int, claim_until: int) -> bool:
    """Return an unfinished peer to the queue after a slice boundary."""
    with conn:
        cursor = conn.execute(
            "UPDATE activity_dialog_state "
            "SET cold_status = 'pending', cold_next_retry_at = NULL, updated_at = ? "
            "WHERE dialog_id = ? AND cold_status = 'running' AND cold_next_retry_at = ?",
            (int(time.time()), dialog_id, claim_until),
        )
    return cursor.rowcount == 1


def _finish_cold_backfill_peer(ctx: _ColdPeerFinishContext, pacing: ColdBackfillPacing) -> ColdPassResult:
    """Apply one peer result and emit the matching telemetry."""
    result = ctx.result
    if result.skip_reason is SkipReason.FLOOD_WAIT:
        flood_wait_seconds = cast(int, result.flood_wait_seconds)
        next_retry_at = ctx.now + flood_wait_seconds
        _save_claimed_cold_state(
            ctx,
            cold_status="pending",
            cold_next_retry_at=next_retry_at,
        )
        logger.warning(
            "activity_cold_backfill_flood dialog_id=%r flood_wait_seconds=%d retry_delay_s=%d"
            " cold_next_retry_at=%d duration_s=%.3f",
            ctx.dialog_id,
            flood_wait_seconds,
            flood_wait_seconds,
            next_retry_at,
            time.monotonic() - ctx.started_at,
        )
        return ColdPassResult(
            outcome=ColdPassOutcome.FLOOD_WAIT,
            persisted=0,
            flood_wait_seconds=flood_wait_seconds,
        )

    if result.skip_reason is SkipReason.ACCESS_SKIP:
        next_retry_at = int(ctx.now + pacing.history.access_retry_s)
        _save_claimed_cold_state(
            ctx,
            cold_status="pending",
            cold_next_retry_at=next_retry_at,
            cold_last_error="access_skip",
        )
        logger.debug(
            "activity_cold_backfill_access_skip dialog_id=%r retry_delay_s=%.3f cold_next_retry_at=%d duration_s=%.3f",
            ctx.dialog_id,
            pacing.history.access_retry_s,
            next_retry_at,
            time.monotonic() - ctx.started_at,
        )
        return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0)

    if result.skip_reason is SkipReason.HISTORY_FLOOR:
        _save_claimed_cold_state(
            ctx,
            cold_status="complete",
            cold_next_retry_at=None,
        )
        logger.info(
            "activity_cold_backfill_complete dialog_id=%r offset_id=%d duration_s=%.3f",
            ctx.dialog_id,
            ctx.offset_id,
            time.monotonic() - ctx.started_at,
        )
        return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0)

    new_offset = result.min_id
    _save_claimed_cold_state(
        ctx,
        cold_offset_id=new_offset,
        cold_status="pending",
        cold_next_retry_at=None,
    )
    logger.debug(
        "activity_cold_backfill_batch dialog_id=%r old_offset=%d new_offset=%r persisted=%d duration_s=%.3f",
        ctx.dialog_id,
        ctx.offset_id,
        new_offset,
        result.persisted,
        time.monotonic() - ctx.started_at,
    )
    if result.persisted and result.persisted > 0:
        return ColdPassResult(outcome=ColdPassOutcome.WROTE, persisted=result.persisted)
    return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0)


async def run_cold_backfill_pass(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    pacing: ColdBackfillPacing,
    timeout_s: float,
) -> ColdPassResult:
    """Run one cold peer-page operation under its precise durable root."""
    with _cold_demand_scope():
        return await _run_cold_backfill_pass(
            client,
            conn,
            shutdown_event,
            pacing=pacing,
            timeout_s=timeout_s,
        )


async def _run_cold_backfill_pass(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    pacing: ColdBackfillPacing,
    timeout_s: float,
) -> ColdPassResult:
    """Run one Tier-B ColdBackfill pass.

    Selects ONE due peer (cold_status != 'complete', cold_next_retry_at due),
    calls sweep_peer_once with offset_id=cold_offset_id (backward walk), and
    branches strictly on result.skip_reason.

    Due-peer selection uses ORDER BY updated_at ASC, dialog_id ASC — the
    intentional round-robin anti-starvation mechanism so no single deep-history
    peer monopolises the queue.

    Returns a ColdPassResult distinguishing idle (NO_DUE_PEER) from processed
    outcomes (WROTE / ZERO_PERSISTED / FLOOD_WAIT) so the caller can choose
    the correct sleep interval.

    Only cold_* columns are written — hot_* columns are never touched.
    """
    started_at = time.monotonic()
    now = int(time.time())

    claim_until = now + max(1, math.ceil(timeout_s + pacing.history.batch_s))
    row = _claim_cold_backfill_peer(conn, now=now, claim_until=claim_until)

    if row is None:
        logger.debug("activity_cold_backfill_pass_no_due_peer")
        return ColdPassResult(outcome=ColdPassOutcome.NO_DUE_PEER, persisted=0)

    dialog_id, cold_offset_id = row

    # offset_id=0 means "start from newest and walk down"; thereafter use the
    # stored cold_offset_id which shrinks toward the history floor each pass.
    offset_id = cold_offset_id or 0

    logger.debug(
        "activity_cold_backfill_pass_start dialog_id=%r offset_id=%d",
        dialog_id,
        offset_id,
    )

    try:
        with rpc_scope(
            TelegramRpcSource.ACTIVITY_COLD_BACKFILL,
            timeout_seconds=timeout_s,
            acquisition_kind=AcquisitionKind.MESSAGE_SEARCH_PAGE,
        ):
            result = await sweep_peer_once(
                client,
                conn,
                dialog_id,
                offset_id=offset_id,
                min_id=0,  # no time/id ceiling — full history walk
                limit=_BACKFILL_BATCH_LIMIT,
                timeout_s=timeout_s,
                hydration_priority=HydrationPriority.BACKFILL,
            )
    except RpcAttemptBudgetExhaustedError:
        # A slice bound is not an access failure. Release the short claim so
        # status() exposes immediate durable continuation on the next slice.
        _release_cold_claim(conn, dialog_id=dialog_id, claim_until=claim_until)
        return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0)

    return _finish_cold_backfill_peer(
        _ColdPeerFinishContext(
            conn=conn,
            dialog_id=dialog_id,
            offset_id=offset_id,
            started_at=started_at,
            now=now,
            claim_until=claim_until,
            result=result,
        ),
        pacing,
    )
