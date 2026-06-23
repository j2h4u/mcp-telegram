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
- run_cold_backfill_pass returns a structured ColdPassResult (cycle-4 MEDIUM) so the
  loop can distinguish idle (NO_DUE_PEER) from zero-write work (ZERO_PERSISTED).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from .activity_peer_sweep import (
    SkipReason,
    _save_dialog_state,
    build_working_set,
    sweep_peer_once,
)
from .activity_sync import _ActivityClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (all env-overridable for operator tuning / UAT)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColdBackfillHistoryPacing:
    batch_s: float = float(os.environ.get("ACTIVITY_COLD_BACKFILL_BATCH_PAUSE", "5"))
    enroll_s: float = float(os.environ.get("ACTIVITY_COLD_ENROLL_SECONDS", "1800"))
    access_retry_s: float = float(os.environ.get("ACTIVITY_COLD_ACCESS_RETRY_SECONDS", "3600"))


@dataclass(frozen=True, slots=True)
class ColdBackfillPacing:
    idle_s: float = float(os.environ.get("ACTIVITY_COLD_BACKFILL_SECONDS", "300"))
    history: ColdBackfillHistoryPacing = ColdBackfillHistoryPacing()


_PACING = ColdBackfillPacing()

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


# ---------------------------------------------------------------------------
# Single-pass implementation
# ---------------------------------------------------------------------------


async def _run_cold_backfill_pass_safe(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> ColdPassResult:
    try:
        return await run_cold_backfill_pass(client, conn, shutdown_event)
    except Exception:
        logger.warning("activity_cold_backfill_error", exc_info=True)
        # Treat as NO_DUE_PEER for sleep purposes to avoid tight error loops.
        return ColdPassResult(outcome=ColdPassOutcome.NO_DUE_PEER, persisted=0)


def _cold_backfill_sleep_seconds(pass_result: ColdPassResult, idle_interval: float) -> float:
    if pass_result.outcome is ColdPassOutcome.NO_DUE_PEER:
        logger.debug("activity_cold_backfill_idle sleeping=%.0fs", idle_interval)
        return idle_interval

    logger.debug(
        "activity_cold_backfill_loop outcome=%s persisted=%d sleeping=%.0fs",
        pass_result.outcome,
        pass_result.persisted,
        _PACING.history.batch_s,
    )
    return _PACING.history.batch_s


async def _maybe_enroll_activity_peers(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    last_enroll_at: float,
) -> float:
    now_mono = asyncio.get_running_loop().time()
    if now_mono - last_enroll_at < _PACING.history.enroll_s:
        return last_enroll_at

    try:
        enrolled = await build_working_set(client, conn)
        logger.debug("activity_cold_backfill_enroll enrolled=%d", enrolled)
    except Exception:
        logger.warning("activity_cold_backfill_enroll_error", exc_info=True)
    return asyncio.get_running_loop().time()


async def run_cold_backfill_pass(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
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
    now = int(time.time())

    # Select ONE due peer — oldest-updated first (round-robin anti-starvation)
    row = cast(
        tuple[int, int | None] | None,
        conn.execute(
            """
        SELECT dialog_id, cold_offset_id
        FROM activity_dialog_state
        WHERE cold_status != 'complete'
          AND (cold_next_retry_at IS NULL OR cold_next_retry_at <= :now)
        ORDER BY updated_at ASC, dialog_id ASC
        LIMIT 1
        """,
            {"now": now},
        ).fetchone(),
    )

    if row is None:
        logger.debug("activity_cold_backfill_pass_no_due_peer")
        return ColdPassResult(outcome=ColdPassOutcome.NO_DUE_PEER, persisted=0)

    dialog_id, cold_offset_id = row

    # Mark as running so the peer is not double-selected if the pass is slow
    _save_dialog_state(conn, dialog_id, cold_status="running")

    # offset_id=0 means "start from newest and walk down"; thereafter use the
    # stored cold_offset_id which shrinks toward the history floor each pass.
    offset_id = cold_offset_id or 0

    logger.debug(
        "activity_cold_backfill_pass_start dialog_id=%r offset_id=%d",
        dialog_id,
        offset_id,
    )

    result = await sweep_peer_once(
        client,
        conn,
        dialog_id,
        offset_id=offset_id,
        min_id=0,  # no time/id ceiling — full history walk
        limit=_BACKFILL_BATCH_LIMIT,
    )

    # --- Branch STRICTLY on result.skip_reason (concern 3) ---

    if result.skip_reason is SkipReason.FLOOD_WAIT:
        # Tier B owns durable FloodWait retry — concern 5.
        # Set cold_next_retry_at; mark pending so peer is re-selectable.
        # NEVER touch any hot_* field.
        next_retry_at = now + (result.flood_wait_seconds or 0)
        _save_dialog_state(
            conn,
            dialog_id,
            cold_status="pending",
            cold_next_retry_at=next_retry_at,
        )
        logger.warning(
            "activity_cold_backfill_flood dialog_id=%r flood_wait_seconds=%d cold_next_retry_at=%d",
            dialog_id,
            result.flood_wait_seconds,
            next_retry_at,
        )
        return ColdPassResult(outcome=ColdPassOutcome.FLOOD_WAIT, persisted=0)

    if result.skip_reason is SkipReason.ACCESS_SKIP:
        # Transient miss — resolve_input_peer returned None or a timeout.
        # Concern 3: ACCESS_SKIP must NEVER set cold_status='complete'.
        # cold_offset_id is left UNCHANGED so the walk resumes from the same point.
        next_retry_at = int(now + _PACING.history.access_retry_s)
        _save_dialog_state(
            conn,
            dialog_id,
            cold_status="pending",
            cold_next_retry_at=next_retry_at,
            cold_last_error="access_skip",
        )
        logger.debug(
            "activity_cold_backfill_access_skip dialog_id=%r cold_next_retry_at=%d",
            dialog_id,
            next_retry_at,
        )
        # A peer WAS processed — return ZERO_PERSISTED so loop does not idle
        return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0)

    if result.skip_reason is SkipReason.HISTORY_FLOOR:
        # Genuine empty batch from a reachable peer — the ONLY path that completes.
        # hit_floor is True only here (see SweepResult.hit_floor property).
        _save_dialog_state(
            conn,
            dialog_id,
            cold_status="complete",
            cold_next_retry_at=None,
        )
        logger.info(
            "activity_cold_backfill_complete dialog_id=%r offset_id=%d",
            dialog_id,
            offset_id,
        )
        # Peer was processed; return ZERO_PERSISTED (not NO_DUE_PEER)
        return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0)

    # SkipReason.NONE — normal non-empty batch
    # Advance cold_offset_id downward to result.min_id (backward walk — concern 2)
    new_offset = result.min_id
    _save_dialog_state(
        conn,
        dialog_id,
        cold_offset_id=new_offset,
        cold_status="pending",
        cold_next_retry_at=None,
    )
    logger.debug(
        "activity_cold_backfill_batch dialog_id=%r old_offset=%d new_offset=%r persisted=%d",
        dialog_id,
        offset_id,
        new_offset,
        result.persisted,
    )

    if result.persisted and result.persisted > 0:
        return ColdPassResult(outcome=ColdPassOutcome.WROTE, persisted=result.persisted)
    # NONE batch but persisted==0 (degenerate: fetched_ids non-empty but no rows
    # extracted) — still a peer-processed outcome, not idle
    return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0)


# ---------------------------------------------------------------------------
# Low-priority loop wrapper
# ---------------------------------------------------------------------------


async def run_cold_backfill_loop(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    idle_interval: float = _PACING.idle_s,
) -> None:
    """Background task: run Tier-B ColdBackfill, low-priority, self-enrolling.

    Loop sleep policy (cycle-4 MEDIUM — must NOT idle 300s after zero-write work):
    - outcome == NO_DUE_PEER → sleep idle_interval (long: no work exists)
    - outcome in {WROTE, ZERO_PERSISTED, FLOOD_WAIT} → sleep _PACING.history.batch_s
      (short: a peer was processed, more may be due)

    Enrollment: build_working_set is called on entry and then no more often than
    every _PACING.history.enroll_s so Tier B is self-sufficient for enrollment and
    does not depend on Tier A having run (review MEDIUM).

    Logs use the activity_cold_backfill_* prefix.
    """
    last_enroll_at: float = 0.0  # sentinel: force enroll on first iteration

    while not shutdown_event.is_set():
        # Throttled enrollment — call build_working_set no more than once per
        # _PACING.history.enroll_s so peer set stays current without over-calling.
        last_enroll_at = await _maybe_enroll_activity_peers(client, conn, last_enroll_at)

        pass_result = await _run_cold_backfill_pass_safe(client, conn, shutdown_event)

        sleep_s = _cold_backfill_sleep_seconds(pass_result, idle_interval)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=sleep_s)
            return  # shutdown signalled
        except TimeoutError:
            pass  # normal — continue loop
