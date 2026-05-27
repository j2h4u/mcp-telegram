"""Tests for activity_peer_sweep.py.

Covers:
  (a) dedup: a peer that is BOTH a direct supergroup and a channel's
      linked_chat appears exactly once.
  (b) channel with no discussion group contributes no row.
  (c) after build_working_set, activity_dialog_state.last_activity_at equals
      the peer's dialogs.last_message_at.
  (d) dialogs.type='group' is NOT selected as a supergroup; type='supergroup'
      IS selected (proves the source/casing fix — concern 4).
  (e) a channel whose resolver returns flood_wait_seconds is skipped without
      raising and without enrolling that channel.
  (f) (cycle-4 HIGH — DURABLE retry) the flooded channel writes a future
      activity_channel_resolution.next_retry_at; on the NEXT build_working_set
      pass the resolver is NOT called again (assert call count unchanged).
  (g) (restart proof) a channel whose linked-chat fallback last_activity_at
      comes from the CHANNEL's last_message_at when the linked chat has no
      direct dialogs row.
  (h) once the future next_retry_at is past, a successful resolution CLEARS
      activity_channel_resolution.next_retry_at.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_telegram.activity_peer_resolve import LinkedChatResolution
from mcp_telegram.activity_peer_sweep import (
    _DIALOG_STATE_COLUMNS,
    SkipReason,
    SweepResult,
    _channel_resolution_due,
    _clear_channel_resolution,
    _load_dialog_state,
    _record_channel_resolution_flood,
    _save_dialog_state,
    build_working_set,
    enroll_activity_dialog,
    sweep_peer_once,
)
from mcp_telegram.sync_db import _apply_migrations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


def _insert_dialog(conn: sqlite3.Connection, dialog_id: int, dtype: str, last_message_at: int = 1000) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO dialogs (dialog_id, name, type, hidden, last_message_at, snapshot_at)"
        " VALUES (?, ?, ?, 0, ?, ?)",
        (dialog_id, f"dialog_{dialog_id}", dtype, last_message_at, int(time.time())),
    )
    conn.commit()


def _get_activity_row(conn: sqlite3.Connection, dialog_id: int) -> dict | None:
    row = conn.execute(
        "SELECT dialog_id, source, last_activity_at, cold_status FROM activity_dialog_state"
        " WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    if row is None:
        return None
    return {"dialog_id": row[0], "source": row[1], "last_activity_at": row[2], "cold_status": row[3]}


def _count_activity_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM activity_dialog_state").fetchone()[0]


# ---------------------------------------------------------------------------
# Fake client and resolver
# ---------------------------------------------------------------------------

class _FakeClient:
    """Minimal fake client; sweep_peer_once won't be called in builder tests."""
    async def get_input_entity(self, dialog_id: int) -> Any:
        return MagicMock()

    async def __call__(self, request: Any) -> Any:
        raise AssertionError("_FakeClient.__call__ should not be invoked in builder tests")


class _FakeResolver:
    """Controllable linked-chat resolver for patching build_working_set."""
    def __init__(self, mapping: dict[int, LinkedChatResolution]):
        self._mapping = mapping
        self.call_count = 0
        self.called_with: list[int] = []

    async def __call__(self, client: Any, conn: Any, channel_id: int) -> LinkedChatResolution:
        self.call_count += 1
        self.called_with.append(channel_id)
        return self._mapping.get(
            channel_id, LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)
        )


# ---------------------------------------------------------------------------
# (d) Source casing: type='supergroup' selected, type='group' not selected
# ---------------------------------------------------------------------------

def test_supergroup_type_selected_not_group(monkeypatch):
    """dialogs.type='supergroup' IS enrolled; type='group' is NOT."""
    conn = _make_db()
    supergroup_id = -100100000001
    legacy_group_id = -200000001
    _insert_dialog(conn, supergroup_id, "supergroup", last_message_at=5000)
    _insert_dialog(conn, legacy_group_id, "group", last_message_at=6000)

    # Patch resolve_linked_chat_id to return no results for any channel
    resolver = _FakeResolver({})
    monkeypatch.setattr(
        "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
        resolver,
    )

    count = asyncio.run(
        build_working_set(_FakeClient(), conn)
    )

    assert count == 1, f"Expected 1 peer (only supergroup), got {count}"
    row = _get_activity_row(conn, supergroup_id)
    assert row is not None, "Supergroup should be enrolled"
    assert _get_activity_row(conn, legacy_group_id) is None, (
        "Legacy group must NOT be enrolled via supergroup path"
    )


# ---------------------------------------------------------------------------
# (c) last_activity_at populated from dialogs.last_message_at
# ---------------------------------------------------------------------------

def test_last_activity_at_from_dialogs(monkeypatch):
    """build_working_set populates last_activity_at from dialogs.last_message_at."""
    conn = _make_db()
    peer_id = -100111111111
    last_ts = 99999
    _insert_dialog(conn, peer_id, "supergroup", last_message_at=last_ts)

    resolver = _FakeResolver({})
    monkeypatch.setattr(
        "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
        resolver,
    )

    asyncio.run(
        build_working_set(_FakeClient(), conn)
    )

    row = _get_activity_row(conn, peer_id)
    assert row is not None
    assert row["last_activity_at"] == last_ts, (
        f"Expected last_activity_at={last_ts}, got {row['last_activity_at']}"
    )


# ---------------------------------------------------------------------------
# (b) Channel with no discussion group contributes no row
# ---------------------------------------------------------------------------

def test_channel_no_discussion_group_not_enrolled(monkeypatch):
    """A broadcast channel with no linked chat must not appear in activity_dialog_state."""
    conn = _make_db()
    channel_id = -100222222222
    _insert_dialog(conn, channel_id, "channel", last_message_at=3000)

    resolver = _FakeResolver({
        channel_id: LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)
    })
    monkeypatch.setattr(
        "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
        resolver,
    )

    count = asyncio.run(
        build_working_set(_FakeClient(), conn)
    )

    assert count == 0, f"Expected 0 enrolled peers, got {count}"
    assert _count_activity_rows(conn) == 0


# ---------------------------------------------------------------------------
# (a) Dedup: peer that is both a direct supergroup and a channel's linked_chat
# ---------------------------------------------------------------------------

def test_dedup_supergroup_and_linked_chat(monkeypatch):
    """A peer that is both a direct supergroup and a channel's linked_chat appears once."""
    conn = _make_db()
    supergroup_id = -100333333333
    channel_id = -100444444444
    _insert_dialog(conn, supergroup_id, "supergroup", last_message_at=7000)
    _insert_dialog(conn, channel_id, "channel", last_message_at=8000)

    resolver = _FakeResolver({
        channel_id: LinkedChatResolution(linked_chat_id=supergroup_id, flood_wait_seconds=None)
    })
    monkeypatch.setattr(
        "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
        resolver,
    )

    count = asyncio.run(
        build_working_set(_FakeClient(), conn)
    )

    assert count == 1, f"Expected 1 (deduped), got {count}"
    assert _count_activity_rows(conn) == 1
    row = _get_activity_row(conn, supergroup_id)
    assert row is not None


# ---------------------------------------------------------------------------
# (e) Flood-wait channel: skipped without raising, not enrolled
# ---------------------------------------------------------------------------

def test_flooded_channel_not_enrolled(monkeypatch):
    """A channel whose resolver returns flood_wait_seconds is skipped without enrolling."""
    conn = _make_db()
    channel_id = -100555555555
    _insert_dialog(conn, channel_id, "channel", last_message_at=4000)

    resolver = _FakeResolver({
        channel_id: LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=60)
    })
    monkeypatch.setattr(
        "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
        resolver,
    )

    # Must not raise
    count = asyncio.run(
        build_working_set(_FakeClient(), conn)
    )

    assert count == 0
    assert _count_activity_rows(conn) == 0

    # Durable backoff written to activity_channel_resolution
    row = conn.execute(
        "SELECT next_retry_at, last_error FROM activity_channel_resolution WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    assert row is not None, "FloodWait must write to activity_channel_resolution"
    assert row[0] is not None and row[0] > int(time.time()), (
        "next_retry_at must be in the future"
    )
    assert "FloodWaitError" in (row[1] or "")


# ---------------------------------------------------------------------------
# (f) Durable retry: resolver NOT called again while backoff is active
# ---------------------------------------------------------------------------

def test_durable_retry_resolver_not_recalled(monkeypatch):
    """Flooded channel: resolver is not re-called while next_retry_at is in the future."""
    conn = _make_db()
    channel_id = -100666666666
    _insert_dialog(conn, channel_id, "channel", last_message_at=4000)

    call_log: list[int] = []

    async def _resolver(client: Any, conn_: Any, cid: int) -> LinkedChatResolution:
        call_log.append(cid)
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=3600)

    monkeypatch.setattr(
        "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
        _resolver,
    )

    # First pass: resolver is called, durable backoff written
    asyncio.run(build_working_set(_FakeClient(), conn))
    assert len(call_log) == 1, "Resolver should be called once on first pass"

    # Verify backoff was written
    row = conn.execute(
        "SELECT next_retry_at FROM activity_channel_resolution WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    assert row is not None and row[0] is not None and row[0] > int(time.time())

    # Second pass: resolver must NOT be called again (backoff still active)
    asyncio.run(build_working_set(_FakeClient(), conn))
    assert len(call_log) == 1, (
        f"Resolver must NOT be re-called while backoff active, got {len(call_log)} calls"
    )


def test_flood_halts_resolution_pass_account_safety(monkeypatch):
    """ACCOUNT SAFETY: the FIRST FloodWait halts the whole resolution pass.

    FloodWait is account-global. Continuing to call GetFullChannel for the
    remaining channels during the wait window is what escalates rate-limiting
    toward a ban. So on the first flood the loop MUST stop — later channels are
    left untouched (still due) and drain over subsequent passes.
    """
    conn = _make_db()
    # Three broadcast channels, ALL of which would flood. Iteration order is by
    # dialog_id (PK), not insertion order — so we make every channel flood and
    # assert the resolver is called exactly ONCE: a correct break halts the pass
    # on whichever channel is visited first, regardless of order.
    ch_a = -100111111111
    ch_b = -100222222222
    ch_c = -100333333333
    _insert_dialog(conn, ch_a, "channel", last_message_at=9000)
    _insert_dialog(conn, ch_b, "channel", last_message_at=8000)
    _insert_dialog(conn, ch_c, "channel", last_message_at=7000)

    resolver = _FakeResolver({
        ch_a: LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=26),
        ch_b: LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=26),
        ch_c: LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=26),
    })
    monkeypatch.setattr(
        "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
        resolver,
    )

    asyncio.run(build_working_set(_FakeClient(), conn))

    # Exactly ONE resolver call — the pass stopped on the first flood and never
    # issued further GetFullChannel requests during the account-global wait window.
    assert resolver.call_count == 1, (
        f"pass must halt on first flood; resolver called {resolver.call_count} times"
    )
    # Only the first-visited channel has a backoff row; the other two were untouched.
    backoff_rows = conn.execute(
        "SELECT COUNT(*) FROM activity_channel_resolution"
    ).fetchone()[0]
    assert backoff_rows == 1, (
        f"only the flooded channel should have a backoff row, got {backoff_rows}"
    )


# ---------------------------------------------------------------------------
# (g) Restart-proof: linked-chat last_activity_at from channel's last_message_at
# ---------------------------------------------------------------------------

def test_linked_chat_fallback_last_activity_at(monkeypatch):
    """Linked chat with no direct dialogs row gets channel's last_message_at as fallback."""
    conn = _make_db()
    channel_id = -100777777777
    linked_chat_id = -100888888888
    channel_last_ts = 12345
    # Channel has a dialogs row; linked chat has NO direct dialogs row
    _insert_dialog(conn, channel_id, "channel", last_message_at=channel_last_ts)
    # Do NOT insert a dialogs row for linked_chat_id

    resolver = _FakeResolver({
        channel_id: LinkedChatResolution(linked_chat_id=linked_chat_id, flood_wait_seconds=None)
    })
    monkeypatch.setattr(
        "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
        resolver,
    )

    asyncio.run(
        build_working_set(_FakeClient(), conn)
    )

    row = _get_activity_row(conn, linked_chat_id)
    assert row is not None, "Linked chat should be enrolled"
    assert row["last_activity_at"] == channel_last_ts, (
        f"Expected fallback last_activity_at={channel_last_ts}, got {row['last_activity_at']}"
    )


# ---------------------------------------------------------------------------
# (h) After backoff expires, resolution clears next_retry_at
# ---------------------------------------------------------------------------

def test_successful_resolution_clears_backoff(monkeypatch):
    """A successful resolution after an expired backoff clears next_retry_at."""
    conn = _make_db()
    channel_id = -100999999999
    linked_chat_id = -100111111119
    _insert_dialog(conn, channel_id, "channel", last_message_at=5000)

    # Manually write an already-expired backoff
    past_ts = int(time.time()) - 10
    conn.execute(
        "INSERT OR REPLACE INTO activity_channel_resolution"
        " (channel_id, next_retry_at, last_error, updated_at) VALUES (?, ?, ?, ?)",
        (channel_id, past_ts, "FloodWaitError(60s)", int(time.time())),
    )
    conn.commit()

    resolver = _FakeResolver({
        channel_id: LinkedChatResolution(linked_chat_id=linked_chat_id, flood_wait_seconds=None)
    })
    monkeypatch.setattr(
        "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
        resolver,
    )

    asyncio.run(
        build_working_set(_FakeClient(), conn)
    )

    row = conn.execute(
        "SELECT next_retry_at FROM activity_channel_resolution WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is None, (
        f"Successful resolution must clear next_retry_at, got {row[0]}"
    )
    # And the peer should be enrolled
    assert _get_activity_row(conn, linked_chat_id) is not None


# ---------------------------------------------------------------------------
# enroll_activity_dialog: ON CONFLICT doesn't overwrite cursor columns
# ---------------------------------------------------------------------------

def test_enroll_does_not_overwrite_cursors():
    """enroll_activity_dialog ON CONFLICT must not clobber per-tier cursor state."""
    conn = _make_db()
    peer_id = -100000000001

    # First enrollment
    enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=1000)

    # Simulate scheduler setting per-tier cursor state
    conn.execute(
        "UPDATE activity_dialog_state SET hot_cursor = 999, cold_status = 'running' WHERE dialog_id = ?",
        (peer_id,),
    )
    conn.commit()

    # Re-enroll (as happens on every build_working_set pass)
    enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=2000)

    row = conn.execute(
        "SELECT hot_cursor, cold_status FROM activity_dialog_state WHERE dialog_id = ?",
        (peer_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == 999, "hot_cursor must be preserved across re-enrollment"
    assert row[1] == "running", "cold_status must be preserved across re-enrollment"


# ---------------------------------------------------------------------------
# enroll_activity_dialog: synced_dialogs INSERT OR IGNORE never downgrades
# ---------------------------------------------------------------------------

def test_enroll_never_downgrades_synced_dialogs():
    """enroll_activity_dialog must not downgrade an existing higher-status synced_dialogs row."""
    conn = _make_db()
    peer_id = -100000000002

    # Pre-insert with a higher status
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (peer_id,),
    )
    conn.commit()

    enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=1000)

    row = conn.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id = ?", (peer_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "synced", (
        f"Status must not be downgraded from 'synced' to 'own_only', got {row[0]!r}"
    )


# ---------------------------------------------------------------------------
# _load_dialog_state / _save_dialog_state
# ---------------------------------------------------------------------------

def test_save_and_load_dialog_state():
    """_save_dialog_state writes whitelisted columns; _load_dialog_state reads them back."""
    conn = _make_db()
    peer_id = -100000000003
    enroll_activity_dialog(conn, peer_id, "supergroup")

    _save_dialog_state(conn, peer_id, hot_cursor=42, cold_status="running")
    state = _load_dialog_state(conn, peer_id)

    assert state["hot_cursor"] == 42
    assert state["cold_status"] == "running"


def test_save_dialog_state_rejects_unknown_columns():
    """_save_dialog_state raises ValueError for unknown column names."""
    conn = _make_db()
    peer_id = -100000000004
    enroll_activity_dialog(conn, peer_id, "supergroup")

    with pytest.raises(ValueError, match="unknown fields"):
        _save_dialog_state(conn, peer_id, nonexistent_col=1)


# ---------------------------------------------------------------------------
# _channel_resolution_due helpers
# ---------------------------------------------------------------------------

def test_channel_resolution_due_absent():
    """No row → due."""
    conn = _make_db()
    assert _channel_resolution_due(conn, -100001, now=int(time.time()))


def test_channel_resolution_due_cleared():
    """Row with NULL next_retry_at → due."""
    conn = _make_db()
    _clear_channel_resolution(conn, -100002, now=int(time.time()))
    assert _channel_resolution_due(conn, -100002, now=int(time.time()))


def test_channel_resolution_not_due_future():
    """Row with future next_retry_at → NOT due."""
    conn = _make_db()
    now = int(time.time())
    _record_channel_resolution_flood(
        conn, -100003, next_retry_at=now + 3600, last_error="FloodWaitError(3600s)"
    )
    assert not _channel_resolution_due(conn, -100003, now=now)


def test_channel_resolution_due_past():
    """Row with past next_retry_at → due again."""
    conn = _make_db()
    now = int(time.time())
    _record_channel_resolution_flood(
        conn, -100004, next_retry_at=now - 10, last_error="FloodWaitError(60s)"
    )
    assert _channel_resolution_due(conn, -100004, now=now)


# ---------------------------------------------------------------------------
# SweepResult.hit_floor contract
# ---------------------------------------------------------------------------

def test_hit_floor_only_for_history_floor():
    """hit_floor is True ONLY for HISTORY_FLOOR, False for all other SkipReasons."""
    for reason in SkipReason:
        r = SweepResult(fetched_ids=[], persisted=0, min_id=None, max_id=None, skip_reason=reason)
        expected = reason is SkipReason.HISTORY_FLOOR
        assert r.hit_floor == expected, (
            f"hit_floor expected {expected} for {reason!r}, got {r.hit_floor}"
        )


# ---------------------------------------------------------------------------
# WR-01: allowlist/DDL drift guard for _save_dialog_state
# ---------------------------------------------------------------------------

def test_dialog_state_column_allowlist_matches_table():
    """_DIALOG_STATE_COLUMNS must stay in sync with the real activity_dialog_state
    columns. _save_dialog_state interpolates these names into SQL, so a drifted
    allowlist either fails at runtime (name not in table) or silently permits
    updating an identity/bookkeeping column. Guard both directions.
    """
    conn = _make_db()
    real_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(activity_dialog_state)").fetchall()
    }
    # Every allowlisted column must exist in the table.
    missing = _DIALOG_STATE_COLUMNS - real_cols
    assert not missing, f"allowlist references non-existent columns: {missing}"
    # The allowlist must NOT include identity / bookkeeping columns — those are
    # never updated through _save_dialog_state.
    forbidden = {"dialog_id", "source", "created_at", "updated_at", "last_activity_at"}
    leaked = _DIALOG_STATE_COLUMNS & forbidden
    assert not leaked, f"allowlist must not expose identity/bookkeeping columns: {leaked}"


# ---------------------------------------------------------------------------
# WR-03: enrollment provenance precedence (no supergroup → linked_chat downgrade)
# ---------------------------------------------------------------------------

def test_enroll_does_not_downgrade_supergroup_source():
    """A peer enrolled as 'supergroup' keeps that provenance even if a later
    trace-driven call tries to enroll it as 'linked_chat'. Other sources refresh
    normally (including linked_chat -> supergroup upgrade).
    """
    conn = _make_db()
    peer = -100123123123

    # Direct supergroup membership first.
    enroll_activity_dialog(conn, peer, "supergroup", last_activity_at=1000)
    assert _source_of(conn, peer) == "supergroup"

    # A trace later resolves the same peer as a channel's linked discussion group.
    enroll_activity_dialog(conn, peer, "linked_chat", last_activity_at=2000)
    assert _source_of(conn, peer) == "supergroup", "must not downgrade supergroup → linked_chat"

    # Upgrade path still works: a linked_chat peer found to be a direct supergroup.
    other = -100456456456
    enroll_activity_dialog(conn, other, "linked_chat", last_activity_at=1000)
    assert _source_of(conn, other) == "linked_chat"
    enroll_activity_dialog(conn, other, "supergroup", last_activity_at=2000)
    assert _source_of(conn, other) == "supergroup", "linked_chat → supergroup upgrade must apply"


def _source_of(conn: sqlite3.Connection, dialog_id: int) -> str | None:
    row = conn.execute(
        "SELECT source FROM activity_dialog_state WHERE dialog_id = ?", (dialog_id,)
    ).fetchone()
    return row[0] if row else None
