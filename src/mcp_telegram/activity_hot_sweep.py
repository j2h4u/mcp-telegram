"""Tier A — HotSweep — hourly incremental per-peer self-search scheduler.

Selects enrolled peers whose last_activity_at is within 30 days and whose
hot_next_retry_at is due, pages the ENTIRE newest-side message window for
each peer (concern 2 — multi-batch fix), and persists hot_cursor ONCE after
the window drains.

No scheduling state from Tier B (cold_*) is touched here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from typing import Any

from .activity_peer_sweep import (
    SkipReason,
    SweepResult,
    _save_dialog_state,
    build_working_set,
    sweep_peer_once,
)

logger = logging.getLogger(__name__)

_HOT_SWEEP_INTERVAL_S = float(os.environ.get("ACTIVITY_HOT_SWEEP_SECONDS", "3600"))
_BACKFILL_BATCH_LIMIT = 100
# Short transient backoff for ACCESS_SKIP (peer unresolved / timeout).
_ACCESS_SKIP_RETRY_S = 300  # 5 minutes


async def run_hot_sweep_pass(
    client: Any,
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
    rows = conn.execute(
        """
        SELECT dialog_id, hot_cursor
        FROM activity_dialog_state
        WHERE last_activity_at IS NOT NULL
          AND last_activity_at >= :cutoff
          AND (hot_next_retry_at IS NULL OR hot_next_retry_at <= :now)
        ORDER BY last_activity_at DESC
        """,
        {"cutoff": cutoff, "now": now},
    ).fetchall()

    logger.info("activity_hot_sweep_pass_start peers_selected=%d", len(rows))

    total_written = 0
    # FloodWait from Telegram is account-global. Once we hit one, every further
    # SearchRequest this pass is sent during the wait window — exactly what
    # escalates rate-limiting toward a ban. Stop the whole pass on the first flood
    # and let the per-peer hot_next_retry_at backoff resume work next pass.
    pass_flooded = False

    for dialog_id, old_hot_cursor in rows:
        if shutdown_event.is_set():
            break

        # Inclusive-cursor fix (concern 2): MTProto min_id is inclusive, so
        # anchoring on old_hot_cursor re-fetches that message every pass.
        # Use old_hot_cursor + 1 to fetch strictly-newer messages.
        pass_min_id = (old_hot_cursor + 1) if old_hot_cursor else 0

        # Local accumulator — NOT persisted until the window fully drains
        max_seen: int = old_hot_cursor or 0

        # page_offset starts at 0 (newest), advances to result.min_id each page
        page_offset = 0
        pages_fetched = 0

        while not shutdown_event.is_set():
            result: SweepResult = await sweep_peer_once(
                client,
                conn,
                dialog_id,
                offset_id=page_offset,
                min_id=pass_min_id,
                limit=_BACKFILL_BATCH_LIMIT,
            )

            pages_fetched += 1

            # --- FloodWait (concern 5): Tier A owns hot_next_retry_at ---
            if result.flood_wait_seconds is not None:
                # Read the clock at the event, not the pass-start snapshot: a long
                # multi-peer pass would otherwise back-date next_retry_at into the
                # past and grant the peer zero effective backoff.
                next_retry_at = int(time.time()) + result.flood_wait_seconds
                # Persist any already-drained progress first (do not lose pages
                # drained so far this pass)
                save_fields: dict[str, Any] = {
                    "hot_next_retry_at": next_retry_at,
                }
                if max_seen > (old_hot_cursor or 0):
                    save_fields["hot_cursor"] = max_seen
                _save_dialog_state(conn, dialog_id, **save_fields)
                logger.warning(
                    "activity_hot_sweep_flood dialog_id=%r flood_wait_seconds=%d"
                    " max_seen=%d pages_fetched=%d — halting pass (account-global wait)",
                    dialog_id,
                    result.flood_wait_seconds,
                    max_seen,
                    pages_fetched,
                )
                pass_flooded = True
                break  # Stop this peer; pass_flooded halts the whole pass below

            # --- ACCESS_SKIP (concern 3): transient miss, do NOT advance cursor ---
            if result.skip_reason is SkipReason.ACCESS_SKIP:
                transient_retry_at = int(time.time()) + _ACCESS_SKIP_RETRY_S
                _save_dialog_state(
                    conn,
                    dialog_id,
                    hot_next_retry_at=transient_retry_at,
                )
                logger.debug(
                    "activity_hot_sweep_access_skip dialog_id=%r retry_at=%d pages_fetched=%d",
                    dialog_id,
                    transient_retry_at,
                    pages_fetched,
                )
                break  # Transient — retry next pass

            # --- Update running max seen across pages ---
            if result.max_id is not None:
                max_seen = max(max_seen, result.max_id)

            total_written += result.persisted

            # --- Drain check: window exhausted when page < limit or hit floor ---
            page_drained = (
                result.hit_floor
                or result.skip_reason is SkipReason.HISTORY_FLOOR
                or len(result.fetched_ids) < _BACKFILL_BATCH_LIMIT
            )

            if page_drained:
                # Window fully drained — commit hot_cursor ONCE (concern 2)
                _save_dialog_state(
                    conn,
                    dialog_id,
                    hot_cursor=max_seen,
                    hot_last_sync_at=now,
                    hot_next_retry_at=None,
                )
                logger.debug(
                    "activity_hot_sweep_peer_done dialog_id=%r hot_cursor=%d pages_fetched=%d written=%d",
                    dialog_id,
                    max_seen,
                    pages_fetched,
                    result.persisted,
                )
                break

            # --- More pages remain: advance offset_id downward within window ---
            # result.min_id is the smallest id on this page; next page starts below it
            if result.min_id is None:
                # No min_id means batch was empty — treat as drained
                _save_dialog_state(
                    conn,
                    dialog_id,
                    hot_cursor=max_seen,
                    hot_last_sync_at=now,
                    hot_next_retry_at=None,
                )
                break

            page_offset = result.min_id  # Walk down within [pass_min_id, page_offset)

        # Account-global FloodWait hit on this peer — do not advance to the next
        # peer (that would send another request during the wait window).
        if pass_flooded:
            break

    logger.info(
        "activity_hot_sweep_pass_done peers=%d total_written=%d",
        len(rows),
        total_written,
    )
    return total_written


async def run_hot_sweep_loop(
    client: Any,
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
