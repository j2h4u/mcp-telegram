"""Tests for activity_cold_backfill.py — Tier B ColdBackfill scheduler.

Covers:
  (a) cold_offset_id decreases across passes (backward walk).
  (b) SkipReason.HISTORY_FLOOR sets cold_status='complete'; peer no longer selected.
  (c) SkipReason.ACCESS_SKIP sets cold_next_retry_at + cold_status='pending',
      leaves cold_offset_id UNCHANGED, does NOT complete (concern 3).
  (d) Messages older than 30 days ARE collected — no time ceiling (D-04).
  (e) SkipReason.FLOOD_WAIT sets cold_next_retry_at; does NOT touch any hot_* column.
  (f) Structured ColdPassResult: outcome ∈ {NO_DUE_PEER, WROTE, ZERO_PERSISTED, FLOOD_WAIT}
      matches each scenario (cycle-4 MEDIUM — idle vs zero-write distinction).
  (g) FloodWait tier isolation: no hot_* column written by Tier B (concern 5).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from mcp_telegram.activity_cold_backfill import (
    ColdPassOutcome,
    run_cold_backfill_pass,
)
from mcp_telegram.activity_peer_sweep import (
    SkipReason,
    SweepResult,
    _load_dialog_state,
    _save_dialog_state,
    enroll_activity_dialog,
)
from mcp_telegram.sync_db import _apply_migrations

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


def _enroll(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    cold_offset_id: int | None = None,
    cold_status: str = "pending",
    cold_next_retry_at: int | None = None,
) -> None:
    """Enroll a peer in activity_dialog_state and optionally set cold fields."""
    enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=int(time.time()))
    fields: dict = {}
    if cold_offset_id is not None:
        fields["cold_offset_id"] = cold_offset_id
    if cold_status != "pending":
        fields["cold_status"] = cold_status
    if cold_next_retry_at is not None:
        fields["cold_next_retry_at"] = cold_next_retry_at
    if fields:
        _save_dialog_state(conn, dialog_id, **fields)


def _get_state(conn: sqlite3.Connection, dialog_id: int) -> dict:
    return _load_dialog_state(conn, dialog_id)


# ---------------------------------------------------------------------------
# Fake SweepResult builders
# ---------------------------------------------------------------------------


def _normal_result(ids: list[int], persisted: int | None = None) -> SweepResult:
    """A normal non-empty batch (SkipReason.NONE)."""
    return SweepResult(
        fetched_ids=ids,
        persisted=persisted if persisted is not None else len(ids),
        min_id=min(ids) if ids else None,
        max_id=max(ids) if ids else None,
        skip_reason=SkipReason.NONE,
    )


def _floor_result() -> SweepResult:
    """Genuine empty batch from a reachable peer — history floor."""
    return SweepResult(
        fetched_ids=[],
        persisted=0,
        min_id=None,
        max_id=None,
        skip_reason=SkipReason.HISTORY_FLOOR,
    )


def _access_skip_result() -> SweepResult:
    """Transient access-loss (resolve_input_peer returned None or timeout)."""
    return SweepResult(
        fetched_ids=[],
        persisted=0,
        min_id=None,
        max_id=None,
        skip_reason=SkipReason.ACCESS_SKIP,
    )


def _flood_result(seconds: int) -> SweepResult:
    """FloodWaitError surfaced during the sweep."""
    return SweepResult(
        fetched_ids=[],
        persisted=0,
        min_id=None,
        max_id=None,
        skip_reason=SkipReason.FLOOD_WAIT,
        flood_wait_seconds=seconds,
    )


# ---------------------------------------------------------------------------
# Patch helper
# ---------------------------------------------------------------------------


def _patch_sweep(monkeypatch, scripted: dict[int, list[SweepResult]]) -> dict:
    """Patch sweep_peer_once to return scripted results per dialog_id.

    Returns a call_log: {dialog_id: [(offset_id, min_id), ...]}
    """
    call_log: dict[int, list[tuple[int, int]]] = {}

    async def _fake(client, conn, dialog_id, *, offset_id, min_id, limit):
        call_log.setdefault(dialog_id, []).append((offset_id, min_id))
        queue = scripted.get(dialog_id, [])
        if queue:
            return queue.pop(0)
        # Default: history floor (empty batch from reachable peer)
        return _floor_result()

    monkeypatch.setattr(
        "mcp_telegram.activity_cold_backfill.sweep_peer_once",
        _fake,
    )
    return call_log


def _patch_build_working_set(monkeypatch) -> None:
    async def _noop(client, conn):
        return 0

    monkeypatch.setattr(
        "mcp_telegram.activity_cold_backfill.build_working_set",
        _noop,
    )


# ---------------------------------------------------------------------------
# (f/idle) NO_DUE_PEER when no peer enrolled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_due_peer_when_none_enrolled(monkeypatch):
    """With no enrolled peers, run_cold_backfill_pass returns NO_DUE_PEER."""
    conn = _make_db()
    shutdown = asyncio.Event()
    result = await run_cold_backfill_pass(None, conn, shutdown)
    assert result.outcome == ColdPassOutcome.NO_DUE_PEER
    assert result.persisted == 0


@pytest.mark.asyncio
async def test_no_due_peer_when_all_complete(monkeypatch):
    """With all peers cold_status='complete', returns NO_DUE_PEER."""
    conn = _make_db()
    _enroll(conn, -100100000001, cold_status="complete")
    _patch_sweep(monkeypatch, {})
    shutdown = asyncio.Event()
    result = await run_cold_backfill_pass(None, conn, shutdown)
    assert result.outcome == ColdPassOutcome.NO_DUE_PEER


@pytest.mark.asyncio
async def test_no_due_peer_when_retry_not_due(monkeypatch):
    """With cold_next_retry_at in the future, peer is not selected."""
    conn = _make_db()
    future = int(time.time()) + 7200
    _enroll(conn, -100100000002, cold_next_retry_at=future)
    _patch_sweep(monkeypatch, {})
    shutdown = asyncio.Event()
    result = await run_cold_backfill_pass(None, conn, shutdown)
    assert result.outcome == ColdPassOutcome.NO_DUE_PEER


# ---------------------------------------------------------------------------
# (a) cold_offset_id decreases across passes (backward walk, concern 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backward_walk_cold_offset_id_decreases(monkeypatch):
    """Each non-empty batch sets cold_offset_id = result.min_id (walks backward)."""
    conn = _make_db()
    dialog_id = -100200000001
    _enroll(conn, dialog_id)  # cold_offset_id starts as None

    # Two passes: first returns ids 100-200, second returns ids 50-99
    scripted = {
        dialog_id: [
            _normal_result(list(range(100, 201))),  # pass 1: min_id=100
            _normal_result(list(range(50, 100))),  # pass 2: min_id=50
        ]
    }
    _patch_sweep(monkeypatch, scripted)
    shutdown = asyncio.Event()

    # Pass 1
    r1 = await run_cold_backfill_pass(None, conn, shutdown)
    assert r1.outcome == ColdPassOutcome.WROTE
    assert r1.persisted > 0
    state1 = _get_state(conn, dialog_id)
    offset_after_pass1 = state1["cold_offset_id"]
    assert offset_after_pass1 == 100, f"After pass 1, cold_offset_id should be min_id=100, got {offset_after_pass1}"

    # Pass 2
    r2 = await run_cold_backfill_pass(None, conn, shutdown)
    assert r2.outcome == ColdPassOutcome.WROTE
    state2 = _get_state(conn, dialog_id)
    offset_after_pass2 = state2["cold_offset_id"]
    assert offset_after_pass2 == 50, f"After pass 2, cold_offset_id should be min_id=50, got {offset_after_pass2}"
    # Confirm offset decreased (walked backward)
    assert offset_after_pass2 < offset_after_pass1


# ---------------------------------------------------------------------------
# (b) HISTORY_FLOOR sets cold_status='complete'; peer no longer selected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_floor_completes_peer(monkeypatch):
    """SkipReason.HISTORY_FLOOR is the ONLY path that sets cold_status='complete'."""
    conn = _make_db()
    dialog_id = -100200000002
    _enroll(conn, dialog_id)

    scripted = {dialog_id: [_floor_result()]}
    call_log = _patch_sweep(monkeypatch, scripted)
    shutdown = asyncio.Event()

    result = await run_cold_backfill_pass(None, conn, shutdown)

    # Returns ZERO_PERSISTED (a peer was processed, just wrote 0 rows)
    assert result.outcome == ColdPassOutcome.ZERO_PERSISTED, (
        f"HISTORY_FLOOR should return ZERO_PERSISTED, got {result.outcome}"
    )
    assert result.persisted == 0

    state = _get_state(conn, dialog_id)
    assert state["cold_status"] == "complete", (
        f"cold_status should be 'complete' after HISTORY_FLOOR, got {state['cold_status']}"
    )
    assert state["cold_next_retry_at"] is None

    # On next pass the peer must NOT be selected (cold_status='complete')
    r2 = await run_cold_backfill_pass(None, conn, shutdown)
    assert r2.outcome == ColdPassOutcome.NO_DUE_PEER, "Completed peer must not be selected on next pass"
    # sweep_peer_once was called only once (for the HISTORY_FLOOR pass)
    assert len(call_log.get(dialog_id, [])) == 1


# ---------------------------------------------------------------------------
# (c) ACCESS_SKIP — concern 3: transient miss never completes backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_skip_does_not_complete(monkeypatch):
    """ACCESS_SKIP sets cold_next_retry_at+pending, leaves cold_offset_id unchanged."""
    conn = _make_db()
    dialog_id = -100200000003
    prior_offset = 500
    _enroll(conn, dialog_id, cold_offset_id=prior_offset)

    scripted = {dialog_id: [_access_skip_result()]}
    _patch_sweep(monkeypatch, scripted)
    shutdown = asyncio.Event()

    before = int(time.time())
    result = await run_cold_backfill_pass(None, conn, shutdown)

    # Returns ZERO_PERSISTED (a peer was processed)
    assert result.outcome == ColdPassOutcome.ZERO_PERSISTED, (
        f"ACCESS_SKIP should return ZERO_PERSISTED, got {result.outcome}"
    )
    assert result.persisted == 0

    state = _get_state(conn, dialog_id)

    # Must NOT complete — concern 3
    assert state["cold_status"] != "complete", "ACCESS_SKIP must NEVER set cold_status='complete' (concern 3)"
    assert state["cold_status"] == "pending"

    # cold_offset_id must remain unchanged (resume from same point on retry)
    assert state["cold_offset_id"] == prior_offset, (
        f"cold_offset_id must be unchanged after ACCESS_SKIP, expected {prior_offset}, got {state['cold_offset_id']}"
    )

    # cold_next_retry_at must be set in the future
    assert state["cold_next_retry_at"] is not None, (
        "cold_next_retry_at must be set for transient backoff on ACCESS_SKIP"
    )
    assert state["cold_next_retry_at"] > before, "cold_next_retry_at must be in the future"

    # Peer must remain re-selectable (next_retry_at is in the future now,
    # but the field is set — a later pass after the backoff will re-select it)


# ---------------------------------------------------------------------------
# (d) Old messages (>30 days) ARE collected — no time ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_time_ceiling_old_messages_collected(monkeypatch):
    """Messages well beyond 30 days are collected — Tier B has NO time ceiling."""
    conn = _make_db()
    dialog_id = -100200000004
    _enroll(conn, dialog_id)

    # Message IDs that correspond to old timestamps (conceptually; the sweep
    # does not filter by date, only by message_id range)
    # The test verifies that the pass returns WROTE and advances cold_offset_id,
    # regardless of message age — no date filter is applied by Tier B.
    old_msg_ids = [1001, 1002, 1003]  # small IDs represent old messages
    scripted = {dialog_id: [_normal_result(old_msg_ids, persisted=3)]}
    _patch_sweep(monkeypatch, scripted)
    shutdown = asyncio.Event()

    result = await run_cold_backfill_pass(None, conn, shutdown)

    assert result.outcome == ColdPassOutcome.WROTE, (
        f"Old messages should be collected (no time ceiling), got {result.outcome}"
    )
    assert result.persisted == 3

    state = _get_state(conn, dialog_id)
    # cold_offset_id advanced to min of old ids (walked backward)
    assert state["cold_offset_id"] == min(old_msg_ids), (
        f"cold_offset_id should be {min(old_msg_ids)}, got {state['cold_offset_id']}"
    )
    # NOT complete — more history may exist below
    assert state["cold_status"] != "complete"


# ---------------------------------------------------------------------------
# (e) FloodWait: cold_next_retry_at set; NO hot_* column touched (concern 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flood_wait_sets_cold_retry_not_hot(monkeypatch):
    """FloodWait sets cold_next_retry_at and NEVER writes any hot_* column."""
    conn = _make_db()
    dialog_id = -100200000005
    _enroll(conn, dialog_id)

    flood_seconds = 180
    scripted = {dialog_id: [_flood_result(flood_seconds)]}
    _patch_sweep(monkeypatch, scripted)
    shutdown = asyncio.Event()

    before = int(time.time())
    result = await run_cold_backfill_pass(None, conn, shutdown)

    assert result.outcome == ColdPassOutcome.FLOOD_WAIT, (
        f"FloodWait should return FLOOD_WAIT outcome, got {result.outcome}"
    )
    assert result.persisted == 0

    state = _get_state(conn, dialog_id)

    # cold_next_retry_at set in the future
    assert state["cold_next_retry_at"] is not None, "cold_next_retry_at must be set on FloodWait"
    assert state["cold_next_retry_at"] >= before + flood_seconds, (
        f"cold_next_retry_at should be at least now+{flood_seconds}"
    )

    # cold_status must be 'pending' (not 'complete')
    assert state["cold_status"] == "pending"

    # Tier isolation (concern 5): NO hot_* column must be written
    assert state.get("hot_cursor") is None, "FloodWait must NOT write hot_cursor (tier isolation — concern 5)"
    assert state.get("hot_next_retry_at") is None, (
        "FloodWait must NOT write hot_next_retry_at (tier isolation — concern 5)"
    )
    assert state.get("hot_last_sync_at") is None, (
        "FloodWait must NOT write hot_last_sync_at (tier isolation — concern 5)"
    )


# ---------------------------------------------------------------------------
# (g) Comprehensive tier isolation — FloodWait + normal pass leave hot_* clean
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_pass_never_writes_hot_columns(monkeypatch):
    """A normal Tier B pass must NEVER write any hot_* column."""
    conn = _make_db()
    dialog_id = -100200000006
    _enroll(conn, dialog_id)

    scripted = {dialog_id: [_normal_result([200, 300, 400])]}
    _patch_sweep(monkeypatch, scripted)
    shutdown = asyncio.Event()

    await run_cold_backfill_pass(None, conn, shutdown)

    state = _get_state(conn, dialog_id)
    assert state.get("hot_cursor") is None, "Normal cold pass must NOT write hot_cursor"
    assert state.get("hot_next_retry_at") is None, "Normal cold pass must NOT write hot_next_retry_at"
    assert state.get("hot_last_sync_at") is None, "Normal cold pass must NOT write hot_last_sync_at"
    assert state.get("hot_last_error") is None, "Normal cold pass must NOT write hot_last_error"


# ---------------------------------------------------------------------------
# (f) ColdPassResult structured outcomes — full scenario matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_pass_result_outcomes_matrix(monkeypatch):
    """run_cold_backfill_pass returns the correct ColdPassResult for each scenario."""
    conn = _make_db()

    # Enroll four peers for the four scenarios
    peer_wrote = -100300000001
    peer_floor = -100300000002
    peer_access = -100300000003
    peer_flood = -100300000004

    for did in [peer_wrote, peer_floor, peer_access, peer_flood]:
        _enroll(conn, did)

    scripted = {
        peer_wrote: [_normal_result([10, 20, 30])],
        peer_floor: [_floor_result()],
        peer_access: [_access_skip_result()],
        peer_flood: [_flood_result(60)],
    }
    _patch_sweep(monkeypatch, scripted)
    shutdown = asyncio.Event()

    # Collect all four pass results (each pass picks ONE peer; ORDER BY updated_at ASC)
    outcomes: dict[str, ColdPassOutcome] = {}
    for _ in range(4):
        r = await run_cold_backfill_pass(None, conn, shutdown)
        # Re-enable any access/flood peers so they are selectable on the next pass
        # (we just want one result per pass — this is a matrix, not chained state)
        outcomes[r.outcome] = r  # type: ignore[assignment]

    # All four distinct outcomes must appear across the four passes
    assert ColdPassOutcome.WROTE in outcomes, "WROTE outcome missing"
    assert ColdPassOutcome.ZERO_PERSISTED in outcomes, "ZERO_PERSISTED outcome missing"
    assert ColdPassOutcome.FLOOD_WAIT in outcomes, "FLOOD_WAIT outcome missing"

    # WROTE outcome must carry persisted > 0
    wrote_result = outcomes[ColdPassOutcome.WROTE]
    assert wrote_result.persisted > 0, "WROTE outcome must have persisted > 0"

    # ZERO_PERSISTED must carry persisted == 0
    zero_result = outcomes[ColdPassOutcome.ZERO_PERSISTED]
    assert zero_result.persisted == 0, "ZERO_PERSISTED outcome must have persisted == 0"


@pytest.mark.asyncio
async def test_no_due_peer_outcome_when_all_gated(monkeypatch):
    """NO_DUE_PEER is returned when all enrolled peers are either complete or in future retry."""
    conn = _make_db()
    future = int(time.time()) + 3600
    _enroll(conn, -100300000010, cold_status="complete")
    _enroll(conn, -100300000011, cold_next_retry_at=future)

    _patch_sweep(monkeypatch, {})
    shutdown = asyncio.Event()

    result = await run_cold_backfill_pass(None, conn, shutdown)
    assert result.outcome == ColdPassOutcome.NO_DUE_PEER
    assert result.persisted == 0
