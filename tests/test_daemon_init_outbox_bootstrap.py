"""Tests for Phase 39.3-02 Task 3 — _initialize_read_positions outbox bootstrap.

Covers AC-4 (tightened): bootstrap fills BOTH read cursors from the SAME
batched GetPeerDialogs sweep. Call count == ceil(N/15). NULL preservation
LOCKED: Telethon None → DB NULL (never 0).

Test inventory (≥11):
  1.  test_bootstrap_populates_outbox_for_null_rows            — AC-4 happy path.
  2.  test_bootstrap_populates_inbox_too                       — Plan 01 primitive regression guard.
  3.  test_bootstrap_call_count_equals_ceil_n_over_15          — AC-4 tightened.
  4.  test_bootstrap_skips_when_both_cursors_populated         — empty SELECT → zero API calls.
  5.  test_bootstrap_runs_when_only_outbox_null                — extended SELECT picks up.
  6.  test_bootstrap_runs_when_only_inbox_null                 — Phase 38 preservation.
  7.  test_bootstrap_telethon_returns_none_outbox_preserves_null (HARD ASSERT None → NULL).
  8.  test_bootstrap_telethon_returns_zero_outbox_writes_zero  — None vs 0 NOT folded.
  9.  test_bootstrap_telethon_returns_none_inbox_preserves_null — symmetric rule.
  10. test_bootstrap_v12_upgrade_scenario                      — LOW-2 from review.
  11. test_bootstrap_live_event_race_newer_wins                — MEDIUM codex designed race safety.
"""
from __future__ import annotations

import asyncio
import math
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    from mcp_telegram.sync_db import _apply_migrations
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


def _seed(conn: sqlite3.Connection, rows):
    """rows: iterable of (dialog_id, inbox_max_id, outbox_max_id, status)."""
    for dialog_id, inbox_max, outbox_max, status in rows:
        conn.execute(
            "INSERT INTO synced_dialogs "
            "(dialog_id, status, read_inbox_max_id, read_outbox_max_id) "
            "VALUES (?, ?, ?, ?)",
            (dialog_id, status, inbox_max, outbox_max),
        )
    conn.commit()


def _read_cursors(conn: sqlite3.Connection, dialog_id: int):
    row = conn.execute(
        "SELECT read_inbox_max_id, read_outbox_max_id FROM synced_dialogs "
        "WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    return row if row is None else (row[0], row[1])


def _patch_get_peer_id(mapping):
    """Patch telethon_utils.get_peer_id to return mapping[peer] per call order.

    Tests usually pass a dict keyed by an identity-free SimpleNamespace and rely
    on call order — we simplify via a side_effect counter.
    """
    iterator = iter(mapping)

    def _side_effect(peer):
        return next(iterator)

    return patch(
        "mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=_side_effect
    )


def _fake_client_with_dialogs(dialogs_per_call):
    """Build a mock client where calling it (for GetPeerDialogsRequest) returns
    the next per-batch list of Dialog objects.

    dialogs_per_call: list[list[SimpleNamespace]] — one entry per expected
    batch call. Each inner list is the .dialogs attribute of the response.
    """
    client = MagicMock()

    input_peer_counter = {"n": 0}

    async def _get_input_entity(did):
        input_peer_counter["n"] += 1
        return SimpleNamespace(_did=did)

    client.get_input_entity = _get_input_entity

    call_count = {"n": 0}

    async def _call(req):
        idx = call_count["n"]
        call_count["n"] += 1
        batch = dialogs_per_call[idx] if idx < len(dialogs_per_call) else []
        return SimpleNamespace(dialogs=batch)

    # Make the client callable (client(request) in daemon.py).
    client.side_effect = _call
    client._call_count = call_count
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_populates_outbox_for_null_rows():
    """AC-4 happy path: NULL outbox cursor gets filled from Telethon Dialog."""
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _make_conn()
    _seed(conn, [(1001, None, None, "synced")])

    fake_dialog = SimpleNamespace(
        peer=SimpleNamespace(),
        read_inbox_max_id=100,
        read_outbox_max_id=555,
    )
    client = _fake_client_with_dialogs([[fake_dialog]])

    with _patch_get_peer_id([1001]):
        await _initialize_read_positions(client, conn, asyncio.Event())

    inbox, outbox = _read_cursors(conn, 1001)
    assert inbox == 100
    assert outbox == 555


@pytest.mark.asyncio
async def test_bootstrap_populates_inbox_too():
    """Plan 01 primitive regression guard — inbox still gets written."""
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _make_conn()
    _seed(conn, [(1001, None, None, "synced")])

    fake_dialog = SimpleNamespace(
        peer=SimpleNamespace(),
        read_inbox_max_id=777,
        read_outbox_max_id=555,
    )
    client = _fake_client_with_dialogs([[fake_dialog]])

    with _patch_get_peer_id([1001]):
        await _initialize_read_positions(client, conn, asyncio.Event())

    inbox, outbox = _read_cursors(conn, 1001)
    assert inbox == 777
    assert outbox == 555


@pytest.mark.asyncio
async def test_bootstrap_call_count_equals_ceil_n_over_15():
    """AC-4 tightened: GetPeerDialogsRequest call count == ceil(37/15) == 3."""
    from mcp_telegram.daemon import _initialize_read_positions

    N = 37
    conn = _make_conn()
    _seed(conn, [(1000 + i, None, None, "synced") for i in range(N)])

    # Build 3 batches of Dialog objects (15 + 15 + 7).
    def _dialogs(start, count):
        return [
            SimpleNamespace(
                peer=SimpleNamespace(),
                read_inbox_max_id=start + i,
                read_outbox_max_id=start + i + 10_000,
            )
            for i in range(count)
        ]

    batches = [_dialogs(0, 15), _dialogs(15, 15), _dialogs(30, 7)]
    client = _fake_client_with_dialogs(batches)

    # get_peer_id returns sequential dialog_ids in the same order as we seeded.
    dialog_ids_in_order = [1000 + i for i in range(N)]
    with _patch_get_peer_id(dialog_ids_in_order):
        await _initialize_read_positions(client, conn, asyncio.Event())

    expected = math.ceil(N / 15)
    assert client._call_count["n"] == expected, (
        f"Expected {expected} GetPeerDialogsRequest calls, "
        f"got {client._call_count['n']}"
    )


@pytest.mark.asyncio
async def test_bootstrap_skips_when_both_cursors_populated():
    """Both cursors non-NULL → SELECT is empty → zero API calls."""
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _make_conn()
    _seed(conn, [(1001, 100, 200, "synced")])

    client = _fake_client_with_dialogs([])
    filled = await _initialize_read_positions(client, conn, asyncio.Event())

    assert filled == 0
    assert client._call_count["n"] == 0


@pytest.mark.asyncio
async def test_bootstrap_runs_when_only_outbox_null():
    """inbox=populated, outbox=NULL → row picked up by extended SELECT."""
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _make_conn()
    _seed(conn, [(1001, 100, None, "synced")])

    fake_dialog = SimpleNamespace(
        peer=SimpleNamespace(), read_inbox_max_id=150, read_outbox_max_id=42
    )
    client = _fake_client_with_dialogs([[fake_dialog]])

    with _patch_get_peer_id([1001]):
        await _initialize_read_positions(client, conn, asyncio.Event())

    inbox, outbox = _read_cursors(conn, 1001)
    # inbox: monotonic MAX(100, 150) = 150.
    assert inbox == 150
    assert outbox == 42


@pytest.mark.asyncio
async def test_bootstrap_runs_when_only_inbox_null():
    """Phase 38 preservation: inbox=NULL, outbox=populated → still picked up."""
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _make_conn()
    _seed(conn, [(1001, None, 200, "synced")])

    fake_dialog = SimpleNamespace(
        peer=SimpleNamespace(), read_inbox_max_id=100, read_outbox_max_id=250
    )
    client = _fake_client_with_dialogs([[fake_dialog]])

    with _patch_get_peer_id([1001]):
        await _initialize_read_positions(client, conn, asyncio.Event())

    inbox, outbox = _read_cursors(conn, 1001)
    assert inbox == 100
    # outbox: monotonic MAX(200, 250) = 250.
    assert outbox == 250


@pytest.mark.asyncio
async def test_bootstrap_telethon_returns_none_outbox_preserves_null():
    """LOCKED (HARD ASSERT): Telethon returns None for outbox → DB stays NULL.

    Regression-rejects the old `getattr(d, "read_outbox_max_id", 0) or 0`
    pattern. NEVER convert None → 0; NEVER call _apply_read_cursor with 0 as
    a stand-in.
    """
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _make_conn()
    _seed(conn, [(1001, None, None, "synced")])

    # Dialog with read_outbox_max_id=None — not just missing, explicitly None.
    fake_dialog = SimpleNamespace(
        peer=SimpleNamespace(),
        read_inbox_max_id=42,
        read_outbox_max_id=None,
    )
    client = _fake_client_with_dialogs([[fake_dialog]])

    with _patch_get_peer_id([1001]):
        await _initialize_read_positions(client, conn, asyncio.Event())

    inbox, outbox = _read_cursors(conn, 1001)
    assert inbox == 42
    assert outbox is None, (
        f"Expected outbox NULL (Telethon returned None), got {outbox!r} "
        "— implementation must NOT fold None → 0"
    )


@pytest.mark.asyncio
async def test_bootstrap_telethon_returns_zero_outbox_writes_zero():
    """Companion: 0 is a legitimate value distinct from None.

    Telethon may return 0 for a dialog where the peer has never read anything.
    Bootstrap must write 0, not leave NULL. Confirms None and 0 are not folded.
    """
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _make_conn()
    _seed(conn, [(1001, None, None, "synced")])

    fake_dialog = SimpleNamespace(
        peer=SimpleNamespace(),
        read_inbox_max_id=42,
        read_outbox_max_id=0,
    )
    client = _fake_client_with_dialogs([[fake_dialog]])

    with _patch_get_peer_id([1001]):
        await _initialize_read_positions(client, conn, asyncio.Event())

    inbox, outbox = _read_cursors(conn, 1001)
    assert outbox == 0, (
        f"Expected outbox == 0 (legitimate zero), got {outbox!r}"
    )


@pytest.mark.asyncio
async def test_bootstrap_telethon_returns_none_inbox_preserves_null():
    """Symmetric: None → NULL for inbox too (consistency tightening)."""
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _make_conn()
    _seed(conn, [(1001, None, None, "synced")])

    fake_dialog = SimpleNamespace(
        peer=SimpleNamespace(),
        read_inbox_max_id=None,
        read_outbox_max_id=42,
    )
    client = _fake_client_with_dialogs([[fake_dialog]])

    with _patch_get_peer_id([1001]):
        await _initialize_read_positions(client, conn, asyncio.Event())

    inbox, outbox = _read_cursors(conn, 1001)
    assert inbox is None, (
        f"Expected inbox NULL (Telethon returned None), got {inbox!r} "
        "— must NOT fold None → 0"
    )
    assert outbox == 42


@pytest.mark.asyncio
async def test_bootstrap_v12_upgrade_scenario():
    """LOW-2 from cross-AI review: first daemon start after v12 migration.

    Pre-seed N=20 synced dialogs with populated inbox (100..119) but
    NULL outbox (simulating fresh v12 migration on a Phase 38 DB).
    After bootstrap:
      - Every row has both cursors populated.
      - GetPeerDialogsRequest call count == ceil(20/15) == 2 batches.
      - Inbox cursors preserved (monotonic MAX — Telethon's value wins only
        if it is larger than the stored value).
    """
    from mcp_telegram.daemon import _initialize_read_positions

    N = 20
    conn = _make_conn()
    _seed(conn, [(1000 + i, 100 + i, None, "synced") for i in range(N)])

    # Telethon returns inbox matching (so monotonic keeps existing) + outbox
    # ranging over 1000..1019.
    def _batch(start_idx, count):
        return [
            SimpleNamespace(
                peer=SimpleNamespace(),
                read_inbox_max_id=100 + start_idx + i,
                read_outbox_max_id=1000 + start_idx + i,
            )
            for i in range(count)
        ]

    batches = [_batch(0, 15), _batch(15, 5)]
    client = _fake_client_with_dialogs(batches)

    dialog_ids_in_order = [1000 + i for i in range(N)]
    with _patch_get_peer_id(dialog_ids_in_order):
        await _initialize_read_positions(client, conn, asyncio.Event())

    expected_calls = math.ceil(N / 15)
    assert client._call_count["n"] == expected_calls, (
        f"Expected {expected_calls} batched GetPeerDialogsRequest calls for "
        f"v12 upgrade re-bootstrap of N={N}; got {client._call_count['n']}"
    )

    # Every row must now have both cursors populated; inbox preserved (they
    # equalled Telethon's values, monotonic MAX keeps them).
    for i in range(N):
        inbox, outbox = _read_cursors(conn, 1000 + i)
        assert inbox == 100 + i, f"row {i}: expected inbox {100+i}, got {inbox}"
        assert outbox == 1000 + i, f"row {i}: expected outbox {1000+i}, got {outbox}"


@pytest.mark.asyncio
async def test_bootstrap_live_event_race_newer_wins():
    """MEDIUM codex: designed race safety, not accidental.

    Scenario: bootstrap is about to write outbox=500 for a dialog, but while
    the GetPeerDialogs request is in flight a live on_outbox_read event
    arrives and applies 999. The monotonic MAX semantics of _apply_read_cursor
    must ensure the final cursor == 999 (not 500).
    """
    from mcp_telegram.daemon import _initialize_read_positions
    from mcp_telegram.read_state import _apply_read_cursor

    conn = _make_conn()
    _seed(conn, [(1001, None, None, "synced")])

    # Slow/interruptible client: when called with GetPeerDialogsRequest,
    # simulate a concurrent live event landing BEFORE we return the stale
    # Dialog(500).
    async def _slow_call(req):
        # Concurrent live event landed first.
        _apply_read_cursor(conn, 1001, "outbox", 999)
        conn.commit()
        return SimpleNamespace(
            dialogs=[
                SimpleNamespace(
                    peer=SimpleNamespace(),
                    read_inbox_max_id=42,
                    read_outbox_max_id=500,  # stale
                ),
            ]
        )

    client = MagicMock()

    async def _get_input_entity(did):
        return SimpleNamespace(_did=did)

    client.get_input_entity = _get_input_entity
    client.side_effect = _slow_call

    with _patch_get_peer_id([1001]):
        await _initialize_read_positions(client, conn, asyncio.Event())

    inbox, outbox = _read_cursors(conn, 1001)
    assert outbox == 999, (
        f"Expected 999 (newer live event wins via monotonic MAX), got {outbox} "
        "— bootstrap's stale 500 must not regress the cursor"
    )
