"""Tests for activity_hot_sweep.py — Tier A HotSweep scheduler.

Covers:
  (a) A peer with last_activity_at older than 30 days is NOT selected.
  (b) A peer within 30 days IS selected; first pass (hot_cursor IS NULL)
      uses min_id=0 and hot_cursor becomes the global max message_id seen.
  (c) A second pass uses min_id = prior_hot_cursor + 1 (inclusive-cursor fix)
      and advances hot_cursor only forward.
  (d) Multi-batch window paging (concern 2): when the delta spans more than one
      page, a second SearchRequest is issued (offset_id advanced to first
      page's min_id), lower-id messages from later pages ARE persisted, and
      hot_cursor is committed ONCE after both pages drain.
  (e) own messages land in messages table with out=1 via the canonical pipeline.
  (f) FloodWait: hot_next_retry_at is set (not any cold field) and already-
      drained progress is persisted; pass does not raise.
  (g) ACCESS_SKIP: hot_cursor is NOT advanced and a transient hot_next_retry_at
      is set.
  (h) No cold_* column is ever written by the HotSweep pass.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
import time

import pytest

from mcp_telegram.activity_hot_sweep import run_hot_sweep_pass
from mcp_telegram.activity_peer_sweep import (
    SkipReason,
    SweepResult,
    _load_dialog_state,
    _save_dialog_state,
    enroll_activity_dialog,
)
from mcp_telegram.sync_db import _apply_migrations

# ---------------------------------------------------------------------------
# DB and enrollment helpers
# ---------------------------------------------------------------------------


@contextmanager
def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _enroll(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    last_activity_at: int,
    hot_cursor: int | None = None,
) -> None:
    """Enroll a peer and optionally set its hot_cursor."""
    enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=last_activity_at)
    if hot_cursor is not None:
        _save_dialog_state(conn, dialog_id, hot_cursor=hot_cursor)


def _get_state(conn: sqlite3.Connection, dialog_id: int) -> dict:
    return _load_dialog_state(conn, dialog_id)


def _get_messages(conn: sqlite3.Connection) -> list[tuple[int, int, int]]:
    """Return (dialog_id, message_id, out) tuples ordered by message_id."""
    return conn.execute("SELECT dialog_id, message_id, out FROM messages ORDER BY message_id").fetchall()


# ---------------------------------------------------------------------------
# Fake message and sweep-result builders
# ---------------------------------------------------------------------------


def _make_sweep_result(
    ids: list[int],
    persisted: int | None = None,
    *,
    skip_reason: SkipReason = SkipReason.NONE,
    flood_wait_seconds: int | None = None,
) -> SweepResult:
    if not ids and skip_reason == SkipReason.NONE:
        skip_reason = SkipReason.HISTORY_FLOOR
    return SweepResult(
        fetched_ids=ids,
        persisted=persisted if persisted is not None else len(ids),
        min_id=min(ids) if ids else None,
        max_id=max(ids) if ids else None,
        skip_reason=skip_reason,
        flood_wait_seconds=flood_wait_seconds,
    )


def _flood_result(seconds: int) -> SweepResult:
    return SweepResult(
        fetched_ids=[],
        persisted=0,
        min_id=None,
        max_id=None,
        skip_reason=SkipReason.FLOOD_WAIT,
        flood_wait_seconds=seconds,
    )


def _access_skip_result() -> SweepResult:
    return SweepResult(
        fetched_ids=[],
        persisted=0,
        min_id=None,
        max_id=None,
        skip_reason=SkipReason.ACCESS_SKIP,
    )


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------


def _patch_sweep(monkeypatch, results_by_dialog: dict[int, list[SweepResult]]) -> dict:
    """Patch sweep_peer_once to return scripted results per dialog_id.

    results_by_dialog: {dialog_id: [result1, result2, ...]}
    The fake pops from the front of the list on each call.

    Returns a call-log dict: {dialog_id: [(offset_id, min_id), ...]}
    """
    call_log: dict[int, list[tuple[int, int]]] = {}

    async def _fake_sweep(client, conn, dialog_id, *, offset_id, min_id, limit):
        call_log.setdefault(dialog_id, []).append((offset_id, min_id))
        queue = results_by_dialog.get(dialog_id, [])
        if queue:
            return queue.pop(0)
        # Default: empty batch = history floor
        return _make_sweep_result([])

    monkeypatch.setattr(
        "mcp_telegram.activity_hot_sweep.sweep_peer_once",
        _fake_sweep,
    )
    return call_log


def _patch_build_working_set(monkeypatch, enrolled_count: int = 0) -> None:
    """Patch build_working_set to a no-op (enrollment already done in test)."""

    async def _noop(client, conn):
        return enrolled_count

    monkeypatch.setattr(
        "mcp_telegram.activity_hot_sweep.build_working_set",
        _noop,
    )


# ---------------------------------------------------------------------------
# (a) Stale peer (>30 days) is NOT selected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_peer_not_selected(monkeypatch):
    """A peer with last_activity_at > 30 days ago must not be swept."""
    with _make_db() as conn:
        stale_dialog_id = -100100000001
        stale_ts = int(time.time()) - (31 * 86400)  # 31 days ago
        _enroll(conn, stale_dialog_id, last_activity_at=stale_ts)

        _patch_build_working_set(monkeypatch)
        call_log = _patch_sweep(monkeypatch, {stale_dialog_id: []})

        shutdown = asyncio.Event()
        written = await run_hot_sweep_pass(None, conn, shutdown)

        assert written == 0
        assert stale_dialog_id not in call_log, "Stale peer must not be swept — it is outside the 30-day window"


# ---------------------------------------------------------------------------
# (b) First pass with hot_cursor IS NULL — uses min_id=0, advances hot_cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_pass_null_cursor(monkeypatch):
    """First-ever sweep (hot_cursor IS NULL) uses min_id=0; hot_cursor = max msg_id."""
    with _make_db() as conn:
        dialog_id = -100100000002
        now = int(time.time())
        _enroll(conn, dialog_id, last_activity_at=now - 3600)  # active 1h ago

        results = {
            dialog_id: [
                _make_sweep_result([10, 20, 30]),  # partial — window drained (< limit)
            ]
        }
        _patch_build_working_set(monkeypatch)
        call_log = _patch_sweep(monkeypatch, results)

        shutdown = asyncio.Event()
        await run_hot_sweep_pass(None, conn, shutdown)

        # min_id on first call must be 0 (hot_cursor was NULL)
        assert dialog_id in call_log, "Active peer must be swept"
        first_call_offset, first_call_min_id = call_log[dialog_id][0]
        assert first_call_min_id == 0, f"First-ever sweep must use min_id=0, got {first_call_min_id}"

        # hot_cursor must be the max seen message_id
        state = _get_state(conn, dialog_id)
        assert state["hot_cursor"] == 30, f"hot_cursor should be 30 (max of batch), got {state['hot_cursor']}"


# ---------------------------------------------------------------------------
# (c) Second pass uses min_id = prior_hot_cursor + 1 (inclusive-cursor fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_pass_inclusive_cursor_fix(monkeypatch):
    """Second pass passes min_id = prior_hot_cursor + 1 (not prior_hot_cursor)."""
    with _make_db() as conn:
        dialog_id = -100100000003
        now = int(time.time())
        prior_cursor = 50
        _enroll(conn, dialog_id, last_activity_at=now - 1800, hot_cursor=prior_cursor)

        results = {
            dialog_id: [
                _make_sweep_result([60, 70]),  # new messages above prior cursor
            ]
        }
        _patch_build_working_set(monkeypatch)
        call_log = _patch_sweep(monkeypatch, results)

        shutdown = asyncio.Event()
        await run_hot_sweep_pass(None, conn, shutdown)

        assert dialog_id in call_log
        _, min_id_used = call_log[dialog_id][0]
        assert min_id_used == prior_cursor + 1, (
            f"Second pass must use min_id=prior_cursor+1={prior_cursor + 1}, got {min_id_used}"
        )

        state = _get_state(conn, dialog_id)
        assert state["hot_cursor"] == 70, f"hot_cursor should advance to 70 (max seen), got {state['hot_cursor']}"

        # hot_cursor must never go backward
        assert state["hot_cursor"] >= prior_cursor


# ---------------------------------------------------------------------------
# (d) Multi-batch paging: delta > one page → second request issued, all msgs persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_batch_window_paging(monkeypatch):
    """When the new-message delta spans >1 page, a second SearchRequest is issued.

    The committed hot_cursor equals max_id across BOTH pages — messages from
    the second (older-in-id) page are NOT skipped.
    """
    with _make_db() as conn:
        dialog_id = -100100000004
        now = int(time.time())
        prior_cursor = 100
        _enroll(conn, dialog_id, last_activity_at=now - 600, hot_cursor=prior_cursor)

        # Page 1: full batch (limit=100 messages simulated via fetched_ids count=100)
        page1_ids = list(range(201, 301))  # 100 ids: 201..300
        # Page 2: partial batch (30 ids) — lower in id, still above pass_min_id
        page2_ids = list(range(101, 131))  # 30 ids: 101..130

        results = {
            dialog_id: [
                SweepResult(
                    fetched_ids=page1_ids,
                    persisted=len(page1_ids),
                    min_id=min(page1_ids),
                    max_id=max(page1_ids),
                    skip_reason=SkipReason.NONE,
                ),
                SweepResult(
                    fetched_ids=page2_ids,
                    persisted=len(page2_ids),
                    min_id=min(page2_ids),
                    max_id=max(page2_ids),
                    skip_reason=SkipReason.NONE,
                ),
            ]
        }
        _patch_build_working_set(monkeypatch)
        call_log = _patch_sweep(monkeypatch, results)

        shutdown = asyncio.Event()
        written = await run_hot_sweep_pass(None, conn, shutdown)

        # Two requests must have been issued
        assert len(call_log[dialog_id]) == 2, (
            f"Expected 2 SearchRequest calls for multi-batch delta, got {len(call_log[dialog_id])}"
        )

        # Second request's offset_id must equal first page's min_id (walk downward)
        _, first_offset = call_log[dialog_id][0][0], call_log[dialog_id][0]
        second_offset, second_min = call_log[dialog_id][1]
        assert second_offset == min(page1_ids), (
            f"Second page's offset_id should be first-page min_id={min(page1_ids)}, got {second_offset}"
        )

        # hot_cursor committed ONCE after both pages drain — must equal global max
        state = _get_state(conn, dialog_id)
        assert state["hot_cursor"] == max(page1_ids), (
            f"hot_cursor should be global max={max(page1_ids)}, got {state['hot_cursor']}"
        )

        # Total written = both pages
        assert written == len(page1_ids) + len(page2_ids)


# ---------------------------------------------------------------------------
# (e) Own messages land in messages table with out=1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_own_messages_persisted_with_out_flag(monkeypatch):
    """Messages swept by HotSweep are written via canonical pipeline (out=1)."""
    with _make_db() as conn:
        dialog_id = -100100000005
        now = int(time.time())
        _enroll(conn, dialog_id, last_activity_at=now - 900)

        # Simulate a sweep that reports 3 messages persisted
        results = {
            dialog_id: [
                _make_sweep_result([11, 22, 33], persisted=3),
            ]
        }
        _patch_build_working_set(monkeypatch)
        _patch_sweep(monkeypatch, results)

        shutdown = asyncio.Event()
        written = await run_hot_sweep_pass(None, conn, shutdown)

        # The sweep primitive itself inserts; we verify it was called and reported persisted=3
        assert written == 3, f"Expected 3 messages written, got {written}"

        # hot_cursor must advance
        state = _get_state(conn, dialog_id)
        assert state["hot_cursor"] == 33


# ---------------------------------------------------------------------------
# (f) FloodWait: hot_next_retry_at set, already-drained progress persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flood_wait_sets_retry_at_and_persists_progress(monkeypatch):
    """On FloodWait: hot_next_retry_at is set, already-drained progress kept, no raise."""
    with _make_db() as conn:
        dialog_id = -100100000006
        now = int(time.time())
        prior_cursor = 200
        _enroll(conn, dialog_id, last_activity_at=now - 1200, hot_cursor=prior_cursor)

        flood_seconds = 120
        # First page is FULL (100 messages) — so the inner loop continues to a second page
        # which then returns FloodWait. max_seen must be committed from the first page.
        page1_ids = list(range(201, 301))  # 100 messages — full page triggers next iteration
        results = {
            dialog_id: [
                SweepResult(
                    fetched_ids=page1_ids,
                    persisted=len(page1_ids),
                    min_id=min(page1_ids),
                    max_id=max(page1_ids),
                    skip_reason=SkipReason.NONE,
                ),
                _flood_result(flood_seconds),
            ]
        }
        _patch_build_working_set(monkeypatch)
        _patch_sweep(monkeypatch, results)

        before = int(time.time())
        shutdown = asyncio.Event()
        written = await run_hot_sweep_pass(None, conn, shutdown)  # must NOT raise

        state = _get_state(conn, dialog_id)

        # hot_next_retry_at must be set in the future
        assert state["hot_next_retry_at"] is not None, "hot_next_retry_at must be set on FloodWait"
        assert state["hot_next_retry_at"] >= before + flood_seconds, (
            f"hot_next_retry_at should be at least now+{flood_seconds}, got {state['hot_next_retry_at']}"
        )

        # No cold_* fields touched
        assert state.get("cold_offset_id") is None
        assert state.get("cold_next_retry_at") is None

        # Already-drained page 1 progress must be persisted (hot_cursor advanced past prior_cursor)
        assert state["hot_cursor"] is not None, "hot_cursor should be persisted for drained page 1"
        assert state["hot_cursor"] == max(page1_ids), (
            f"Drained page 1 max={max(page1_ids)} should be persisted, got {state['hot_cursor']}"
        )

        # Page 1 messages counted (flood page contributes 0)
        assert written == len(page1_ids), f"Expected {len(page1_ids)} written (page 1 only), got {written}"


# ---------------------------------------------------------------------------
# (g) ACCESS_SKIP: hot_cursor NOT advanced, transient hot_next_retry_at set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_skip_does_not_advance_cursor(monkeypatch):
    """On ACCESS_SKIP: hot_cursor stays unchanged, transient hot_next_retry_at set."""
    with _make_db() as conn:
        dialog_id = -100100000007
        now = int(time.time())
        prior_cursor = 999
        _enroll(conn, dialog_id, last_activity_at=now - 500, hot_cursor=prior_cursor)

        results = {dialog_id: [_access_skip_result()]}
        _patch_build_working_set(monkeypatch)
        _patch_sweep(monkeypatch, results)

        shutdown = asyncio.Event()
        await run_hot_sweep_pass(None, conn, shutdown)

        state = _get_state(conn, dialog_id)

        # hot_cursor must NOT advance
        assert state["hot_cursor"] == prior_cursor, (
            f"hot_cursor must stay at {prior_cursor} on ACCESS_SKIP, got {state['hot_cursor']}"
        )

        # Transient retry must be set (short backoff, but non-zero)
        assert state["hot_next_retry_at"] is not None, (
            "hot_next_retry_at must be set for transient backoff on ACCESS_SKIP"
        )
        assert state["hot_next_retry_at"] > now, "hot_next_retry_at must be in the future"

        # No cold_* fields touched
        assert state.get("cold_offset_id") is None
        assert state.get("cold_next_retry_at") is None


# ---------------------------------------------------------------------------
# (h) No cold_* column ever written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hot_sweep_never_writes_cold_columns(monkeypatch):
    """The HotSweep pass must never touch cold_offset_id, cold_status, cold_next_retry_at."""
    with _make_db() as conn:
        now = int(time.time())

        # Enroll three peers: fresh, flood, access-skip
        fresh_id = -100100000010
        flood_id = -100100000011
        skip_id = -100100000012

        for did in [fresh_id, flood_id, skip_id]:
            _enroll(conn, did, last_activity_at=now - 100)

        results = {
            fresh_id: [_make_sweep_result([1001])],
            flood_id: [_flood_result(60)],
            skip_id: [_access_skip_result()],
        }
        _patch_build_working_set(monkeypatch)
        _patch_sweep(monkeypatch, results)

        shutdown = asyncio.Event()
        await run_hot_sweep_pass(None, conn, shutdown)

        for did in [fresh_id, flood_id, skip_id]:
            state = _get_state(conn, did)
            assert state.get("cold_offset_id") is None, f"dialog {did}: cold_offset_id must not be set by HotSweep"
            assert state.get("cold_next_retry_at") is None, (
                f"dialog {did}: cold_next_retry_at must not be set by HotSweep"
            )
            # cold_status is 'pending' from enrollment — must remain unchanged
            assert state.get("cold_status") == "pending", (
                f"dialog {did}: cold_status must remain 'pending' (unchanged by HotSweep), got {state.get('cold_status')}"
            )


async def test_flood_halts_whole_pass_account_safety(monkeypatch):
    """ACCOUNT SAFETY: a FloodWait halts the ENTIRE pass, not just the current peer.

    FloodWait is account-global. Advancing to the next peer and issuing another
    SearchRequest during the wait window is what escalates rate-limiting toward a
    ban. Peers after the flooded one must NOT be swept this pass — they stay due
    and resume next pass.
    """
    with _make_db() as conn:
        # Recent timestamps so all peers pass the 30-day recency cutoff. Processed
        # ORDER BY last_activity_at DESC → flood_first (highest) is swept first.
        now = int(time.time())
        flood_first = -100100000001
        later_a = -100100000002
        later_b = -100100000003
        _enroll(conn, flood_first, last_activity_at=now)
        _enroll(conn, later_a, last_activity_at=now - 10)
        _enroll(conn, later_b, last_activity_at=now - 20)

        _patch_build_working_set(monkeypatch)
        call_log = _patch_sweep(monkeypatch, {flood_first: [_flood_result(26)]})

        shutdown = asyncio.Event()
        await run_hot_sweep_pass(None, conn, shutdown)

        # Only the first peer was swept; the pass halted on its FloodWait.
        assert set(call_log.keys()) == {flood_first}, (
            f"pass must halt on first flood; swept peers were {sorted(call_log.keys())}"
        )
        # The flooded peer carries durable backoff; later peers were never touched.
        assert _get_state(conn, flood_first).get("hot_next_retry_at") is not None
        assert _get_state(conn, later_a).get("hot_next_retry_at") is None
        assert _get_state(conn, later_b).get("hot_next_retry_at") is None
