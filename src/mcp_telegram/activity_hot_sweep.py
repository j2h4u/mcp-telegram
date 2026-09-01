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
import sqlite3
import time
from dataclasses import dataclass
from typing import Protocol, cast

from .activity_peer_sweep import (
    SkipReason,
    SweepResult,
    WorkingSetResult,
    _save_dialog_state,
    build_working_set,
    sweep_peer_once,
)
from .activity_substrate import ActivityClient
from .flood import TelegramRpcThrottled
from .hydration_queue import HydrationPriority

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
    row = cast(
        tuple[int],
        conn.execute("SELECT hot_empty_streak FROM activity_dialog_state WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    streak = 0 if genuinely_new > 0 else int(row[0]) + 1
    base_due_seconds = float(policy.base_due_seconds)
    max_due_seconds = float(policy.max_due_seconds)
    interval = _capped_empty_interval(base_due_seconds, max_due_seconds, streak)
    next_due_at = int(completion_at + interval + _stable_jitter_seconds(dialog_id, policy.jitter_max_seconds))
    _save_dialog_state(
        conn,
        dialog_id,
        hot_cursor=hot_cursor,
        hot_last_sync_at=completion_at,
        hot_next_retry_at=None,
        hot_next_due_at=next_due_at,
        hot_empty_streak=streak,
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
    hot_cursor: int,
    now: int,
) -> None:
    """Persist an empty-but-non-drained page without logging a completion event."""
    _save_dialog_state(
        conn,
        dialog_id,
        hot_cursor=hot_cursor,
        hot_last_sync_at=now,
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
            old_hot_cursor=peer.old_hot_cursor or 0,
            max_seen=max_seen,
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
        _save_hot_min_id_gap_state(peer.conn, peer.dialog_id, hot_cursor=max_seen, now=peer.now)
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
    pass_min_id = (ctx.old_hot_cursor + 1) if ctx.old_hot_cursor else 0
    max_seen = ctx.old_hot_cursor or 0
    page_offset = 0
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
        if next_offset is None:
            if outcome.completed:
                if ctx.shutdown_event.is_set():
                    _save_hot_min_id_gap_state(ctx.conn, ctx.dialog_id, hot_cursor=max_seen, now=ctx.now)
                else:
                    completion_at = int(time.time())
                    _save_hot_completed_state(
                        ctx.conn,
                        ctx.dialog_id,
                        hot_cursor=max_seen,
                        completion_at=completion_at,
                        genuinely_new=len(genuinely_new_keys),
                        policy=ctx.policy,
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
    return await build_working_set(client, conn, timeout_s=timeout_s)


def _seed_hot_schedule_after_refresh(
    conn: sqlite3.Connection, policy: HotSweepPolicy, working_set: WorkingSetResult, *, now: int
) -> None:
    if working_set.flood_wait_seconds is None:
        seed_hot_sweep_schedule(conn, policy.initial_spread_seconds, now=now)


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
          AND (ads.hot_next_due_at IS NULL OR ads.hot_next_due_at <= :now
               OR (ads.hot_last_sync_at IS NOT NULL AND sd.last_event_at > ads.hot_last_sync_at))
          AND COALESCE(sd.status, '') != 'access_lost'
        """,
            {"cutoff": cutoff, "now": now},
        ).fetchone(),
    )
    return int(row[0]) if row is not None else 0


async def _run_hot_sweep_peer_safe(ctx: _HotSweepPeerContext) -> _HotSweepPeerOutcome:
    try:
        return await _run_hot_sweep_peer(ctx)
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


async def run_hot_sweep_pass(  # noqa: PLR0914 - explicit pass telemetry counters
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
                ads.hot_next_due_at IS NULL
                OR ads.hot_next_due_at <= :now
                OR (
                    ads.hot_last_sync_at IS NOT NULL
                    AND sd.last_event_at > ads.hot_last_sync_at
                )
          )
          AND COALESCE(sd.status, '') != 'access_lost'
          AND :working_set_flooded = 0
            ORDER BY
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

    logger.info("activity_hot_sweep_pass_start peers_selected=%d", len(rows))

    peers_processed = 0
    flooded = False
    pages_fetched = 0
    rpc_calls = 0
    extracted = 0
    genuinely_new = 0
    yielding_peers = 0
    flood_wait_seconds = working_set.flood_wait_seconds

    for dialog_id, old_hot_cursor in rows:
        if shutdown_event.is_set():
            break

        peer_result = await _run_hot_sweep_peer_safe(
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
        peers_processed += 1
        pages_fetched += peer_result.pages_fetched
        rpc_calls += peer_result.rpc_calls
        extracted += peer_result.extracted
        genuinely_new += peer_result.genuinely_new
        if peer_result.flood_wait_seconds is not None:
            flood_wait_seconds = peer_result.flood_wait_seconds
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
        "duration_s": time.monotonic() - started_at,
    }
    logger.info(
        "activity_hot_sweep_pass_done peers_selected=%d peers_processed=%d due_remaining=%d"
        " pages_fetched=%d rpc_calls=%d extracted=%d genuinely_new=%d yielding_peers=%d"
        " flooded=%s flood_wait_seconds=%r duration_s=%.3f",
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
        time.monotonic() - started_at,
    )
    return telemetry


async def run_hot_sweep_loop(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    policy: HotSweepPolicy,
    timeout_s: float,
) -> None:
    """Background task: run Tier-A HotSweep hourly, interruptible via shutdown_event.

    Mirrors the structure of run_activity_sync_loop.
    """
    while not shutdown_event.is_set():
        logger.info("activity_hot_sweep_loop_start")
        telemetry: dict[str, int | float | bool | None] = {"flood_wait_seconds": None}
        try:
            telemetry = await run_hot_sweep_pass(client, conn, shutdown_event, policy=policy, timeout_s=timeout_s)
            logger.info(
                "activity_hot_sweep_loop_done genuinely_new=%d flood_wait_seconds=%r",
                telemetry["genuinely_new"],
                telemetry["flood_wait_seconds"],
            )
        except TelegramRpcThrottled:
            raise
        except Exception:
            logger.warning("activity_hot_sweep_error", exc_info=True)
        wait_seconds = max(policy.loop_interval_seconds, float(telemetry.get("flood_wait_seconds") or 0))
        logger.info("activity_hot_sweep_loop_sleeping interval=%.0fs", wait_seconds)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=wait_seconds)
            return
        except TimeoutError:
            pass
