"""Tier A — HotSweep — hourly incremental per-peer self-search scheduler.

Selects enrolled peers whose last_activity_at is within 30 days and whose
hot_next_retry_at is due, pages the ENTIRE newest-side message window for
each peer (concern 2 — multi-batch fix), and persists hot_cursor ONCE after
the window drains.

No scheduling state from Tier B (cold_*) is touched here.
"""

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import cast

from .activity_peer_sweep import (
    SkipReason,
    SweepResult,
    _save_dialog_state,
    build_working_set,
    sweep_peer_once,
)
from .activity_sync import _ActivityClient

logger = logging.getLogger(__name__)

_HOT_SWEEP_INTERVAL_S = float(os.environ.get("ACTIVITY_HOT_SWEEP_SECONDS", "3600"))
_BACKFILL_BATCH_LIMIT = 100
# Short transient backoff for ACCESS_SKIP (peer unresolved / timeout).
_ACCESS_SKIP_RETRY_S = 300  # 5 minutes


@dataclass
class _HotSweepPeerOutcome:
    """Outcome for one peer within a hot sweep pass."""

    written: int
    flooded: bool


@dataclass
class _HotSweepPeerContext:
    """Context for processing a single peer in HotSweep."""

    client: _ActivityClient
    conn: sqlite3.Connection
    dialog_id: int
    old_hot_cursor: int | None
    now: int
    shutdown_event: asyncio.Event


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
    old_hot_cursor: int,
    max_seen: int,
    next_retry_at: int,
) -> None:
    """Persist hot-state after a FloodWait and keep already-drained progress."""
    save_fields: dict[str, object] = {"hot_next_retry_at": next_retry_at}
    if max_seen > old_hot_cursor:
        save_fields["hot_cursor"] = max_seen
    _save_dialog_state(conn, dialog_id, **save_fields)


def _save_hot_access_skip_state(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    retry_at: int,
) -> None:
    """Persist a transient retry window for ACCESS_SKIP."""
    _save_dialog_state(conn, dialog_id, hot_next_retry_at=retry_at)


def _save_hot_drained_state(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    hot_cursor: int,
    now: int,
) -> None:
    """Persist the committed hot cursor after the window has fully drained."""
    _save_dialog_state(
        conn,
        dialog_id,
        hot_cursor=hot_cursor,
        hot_last_sync_at=now,
        hot_next_retry_at=None,
    )


def _save_hot_min_id_gap_state(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    hot_cursor: int,
    now: int,
) -> None:
    """Persist an empty-but-non-drained page without logging a completion event."""
    _save_dialog_state(
        conn,
        dialog_id,
        hot_cursor=hot_cursor,
        hot_last_sync_at=now,
        hot_next_retry_at=None,
    )


async def _run_hot_sweep_peer(ctx: _HotSweepPeerContext) -> _HotSweepPeerOutcome:
    """Process one peer across all needed pages for the current hot sweep pass."""
    pass_min_id = (ctx.old_hot_cursor + 1) if ctx.old_hot_cursor else 0
    max_seen = ctx.old_hot_cursor or 0
    page_offset = 0
    pages_fetched = 0
    total_written = 0

    while not ctx.shutdown_event.is_set():
        result: SweepResult = await sweep_peer_once(
            ctx.client,
            ctx.conn,
            ctx.dialog_id,
            offset_id=page_offset,
            min_id=pass_min_id,
            limit=_BACKFILL_BATCH_LIMIT,
        )
        pages_fetched += 1

        if result.flood_wait_seconds is not None:
            next_retry_at = int(time.time()) + result.flood_wait_seconds
            _save_hot_flood_state(
                ctx.conn,
                ctx.dialog_id,
                old_hot_cursor=ctx.old_hot_cursor or 0,
                max_seen=max_seen,
                next_retry_at=next_retry_at,
            )
            logger.warning(
                "activity_hot_sweep_flood dialog_id=%r flood_wait_seconds=%d"
                " max_seen=%d pages_fetched=%d — halting pass (account-global wait)",
                ctx.dialog_id,
                result.flood_wait_seconds,
                max_seen,
                pages_fetched,
            )
            return _HotSweepPeerOutcome(written=total_written, flooded=True)

        if result.skip_reason is SkipReason.ACCESS_SKIP:
            transient_retry_at = int(time.time()) + _ACCESS_SKIP_RETRY_S
            _save_hot_access_skip_state(ctx.conn, ctx.dialog_id, retry_at=transient_retry_at)
            logger.debug(
                "activity_hot_sweep_access_skip dialog_id=%r retry_at=%d pages_fetched=%d",
                ctx.dialog_id,
                transient_retry_at,
                pages_fetched,
            )
            return _HotSweepPeerOutcome(written=total_written, flooded=False)

        if result.max_id is not None:
            max_seen = max(max_seen, result.max_id)

        total_written += result.persisted

        if _is_hot_page_drained(result):
            _save_hot_drained_state(ctx.conn, ctx.dialog_id, hot_cursor=max_seen, now=ctx.now)
            logger.debug(
                "activity_hot_sweep_peer_done dialog_id=%r hot_cursor=%d pages_fetched=%d written=%d",
                ctx.dialog_id,
                max_seen,
                pages_fetched,
                result.persisted,
            )
            return _HotSweepPeerOutcome(written=total_written, flooded=False)

        if result.min_id is None:
            _save_hot_min_id_gap_state(ctx.conn, ctx.dialog_id, hot_cursor=max_seen, now=ctx.now)
            return _HotSweepPeerOutcome(written=total_written, flooded=False)

        page_offset = result.min_id

    return _HotSweepPeerOutcome(written=total_written, flooded=False)


async def run_hot_sweep_pass(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> int:
    """Run one Tier-A HotSweep pass.

    1. Calls build_working_set to refresh last_activity_at for enrolled peers.
    2. Selects hot, due peers (active within 30 days, retry due).
    3. For each peer, pages the ENTIRE newest window (concern 2 multi-batch fix)
       before committing hot_cursor = max_seen.
    4. Handles FloodWait (concern 5) and ACCESS_SKIP (concern 3) per-tier.

    Returns total messages written this pass.
    """
    now = int(time.time())

    # Step 1: cheap working-set refresh — also refreshes last_activity_at
    await build_working_set(client, conn)

    # Step 2: select hot, due peers — recency-bounded to 30 days
    cutoff = now - 30 * 86400
    rows = cast(
        list[tuple[int, int | None]],
        conn.execute(
        """
        SELECT dialog_id, hot_cursor
        FROM activity_dialog_state
        WHERE last_activity_at IS NOT NULL
          AND last_activity_at >= :cutoff
          AND (hot_next_retry_at IS NULL OR hot_next_retry_at <= :now)
        ORDER BY last_activity_at DESC
        """,
        {"cutoff": cutoff, "now": now},
        ).fetchall(),
    )

    logger.info("activity_hot_sweep_pass_start peers_selected=%d", len(rows))

    total_written = 0

    for dialog_id, old_hot_cursor in rows:
        if shutdown_event.is_set():
            break

        peer_result = await _run_hot_sweep_peer(
            _HotSweepPeerContext(
                client=client,
                conn=conn,
                dialog_id=dialog_id,
                old_hot_cursor=old_hot_cursor,
                now=now,
                shutdown_event=shutdown_event,
            )
        )
        total_written += peer_result.written

        # Account-global FloodWait hit on this peer — do not advance to the next
        # peer (that would send another request during the wait window).
        if peer_result.flooded:
            break

    logger.info(
        "activity_hot_sweep_pass_done peers=%d total_written=%d",
        len(rows),
        total_written,
    )
    return total_written


async def run_hot_sweep_loop(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    interval: float = _HOT_SWEEP_INTERVAL_S,
) -> None:
    """Background task: run Tier-A HotSweep hourly, interruptible via shutdown_event.

    Mirrors the structure of run_activity_sync_loop.
    """
    while not shutdown_event.is_set():
        logger.info("activity_hot_sweep_loop_start")
        try:
            written = await run_hot_sweep_pass(client, conn, shutdown_event)
            logger.info("activity_hot_sweep_loop_done total_written=%d", written)
        except Exception:
            logger.warning("activity_hot_sweep_error", exc_info=True)
        logger.info("activity_hot_sweep_loop_sleeping interval=%.0fs", interval)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
