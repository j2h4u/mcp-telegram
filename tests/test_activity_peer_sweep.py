"""Tests for activity_peer_sweep.py.

Covers:
  (a) dedup: a peer that is BOTH a direct supergroup and a channel's
      linked_chat appears exactly once.
  (b) channel with no discussion group contributes no row.
  (c) after build_working_set, activity_dialog_state.last_activity_at equals
      the peer's dialogs.last_message_at.
  (d) dialogs.type='group' is NOT selected as a supergroup; type='supergroup'
      IS selected (proves the source/casing fix — concern 4).
  (e) FloodWait on the second of three channels halts the pass (break, not
      continue) — account-global wait invariant regression guard (Phase 54).

Note: Phase-53 durable backoff tests and helpers were removed in Phase 54
(plan 04). The new event-driven resolver model is tested in the
Phase 54 plan 02–04 test suite.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from typing import TypedDict, cast

import pytest

from mcp_telegram.activity_peer_resolve import LinkedChatResolution
from mcp_telegram.activity_peer_sweep import (
    _DIALOG_STATE_COLUMNS,
    PeerSweepRequest,
    SkipReason,
    SweepResult,
    _load_dialog_state,
    _save_dialog_state,
    build_working_set,
    enroll_activity_dialog,
    sweep_peer_once,
)
from mcp_telegram.sync_db import _apply_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ActivityRow(TypedDict):
    dialog_id: int
    source: str
    last_activity_at: int | None
    cold_status: str


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


def _get_activity_row(conn: sqlite3.Connection, dialog_id: int) -> _ActivityRow | None:
    row = cast(
        tuple[int, str, int | None, str] | None,
        conn.execute(
            "SELECT dialog_id, source, last_activity_at, cold_status FROM activity_dialog_state WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    if row is None:
        return None
    return {
        "dialog_id": row[0],
        "source": row[1],
        "last_activity_at": row[2],
        "cold_status": row[3],
    }


def _count_activity_rows(conn: sqlite3.Connection) -> int:
    row = cast(tuple[int] | None, conn.execute("SELECT COUNT(*) FROM activity_dialog_state").fetchone())
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Fake client and resolver
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal fake client; sweep_peer_once won't be called in builder tests."""

    async def get_input_entity(self, dialog_id: int) -> object:
        del dialog_id
        return object()

    async def __call__(self, request: object) -> object:
        del request
        raise AssertionError("_FakeClient.__call__ should not be invoked in builder tests")


class _FakeResolver:
    """Controllable linked-chat resolver for patching build_working_set."""

    def __init__(self, mapping: dict[int, LinkedChatResolution]):
        self._mapping = mapping
        self.call_count = 0
        self.called_with: list[int] = []

    async def __call__(self, client: object, conn: sqlite3.Connection, channel_id: int) -> LinkedChatResolution:
        del client, conn
        self.call_count += 1
        self.called_with.append(channel_id)
        return self._mapping.get(channel_id, LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None))


class _FakeSweepMessage:
    def __init__(self, msg_id: int, peer_id: object | None) -> None:
        self.id = msg_id
        self.peer_id = peer_id


class _FakeSweepResult:
    def __init__(self, messages: object) -> None:
        self.messages = messages


# ---------------------------------------------------------------------------
# (d) Source casing: type='supergroup' selected, type='group' not selected
# ---------------------------------------------------------------------------


def test_supergroup_type_selected_not_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """dialogs.type='supergroup' IS enrolled; type='group' is NOT."""
    with closing(_make_db()) as conn:
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

        count = asyncio.run(build_working_set(_FakeClient(), conn))

        assert count == 1, f"Expected 1 peer (only supergroup), got {count}"
        row = _get_activity_row(conn, supergroup_id)
        assert row is not None, "Supergroup should be enrolled"
        assert _get_activity_row(conn, legacy_group_id) is None, "Legacy group must NOT be enrolled via supergroup path"


# ---------------------------------------------------------------------------
# (c) last_activity_at populated from dialogs.last_message_at
# ---------------------------------------------------------------------------


def test_last_activity_at_from_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_working_set populates last_activity_at from dialogs.last_message_at."""
    with closing(_make_db()) as conn:
        peer_id = -100111111111
        last_ts = 99999
        _insert_dialog(conn, peer_id, "supergroup", last_message_at=last_ts)

        resolver = _FakeResolver({})
        monkeypatch.setattr(
            "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
            resolver,
        )

        asyncio.run(build_working_set(_FakeClient(), conn))

        row = _get_activity_row(conn, peer_id)
        assert row is not None
        assert row["last_activity_at"] == last_ts, f"Expected last_activity_at={last_ts}, got {row['last_activity_at']}"


# Note: tests (b) channel_no_discussion_group_not_enrolled and (a)
# test_dedup_supergroup_and_linked_chat are restored below (Phase 54, plan 04)
# now that the dead-code backoff gate has been removed from build_working_set.


# ---------------------------------------------------------------------------
# (b) channel with no discussion group contributes no row
# ---------------------------------------------------------------------------


def test_channel_no_discussion_group_not_enrolled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broadcast channel with no linked_chat_id must not appear in activity_dialog_state."""
    with closing(_make_db()) as conn:
        channel_id = -100200000001
        _insert_dialog(conn, channel_id, "channel", last_message_at=1000)

        # Resolver returns no linked chat
        resolver = _FakeResolver(
            {
                channel_id: LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None),
            }
        )
        monkeypatch.setattr(
            "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
            resolver,
        )

        count = asyncio.run(build_working_set(_FakeClient(), conn))

        assert count == 0, f"Expected 0 peers enrolled, got {count}"
        assert _get_activity_row(conn, channel_id) is None, "Channel with no discussion group must NOT be enrolled"


# ---------------------------------------------------------------------------
# (a) dedup: peer that is both a direct supergroup and a channel's linked_chat
# ---------------------------------------------------------------------------


def test_dedup_supergroup_and_linked_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer that is both a supergroup and a channel's linked_chat appears exactly once."""
    with closing(_make_db()) as conn:
        supergroup_id = -100300000001
        channel_id = -100300000002
        _insert_dialog(conn, supergroup_id, "supergroup", last_message_at=2000)
        _insert_dialog(conn, channel_id, "channel", last_message_at=3000)

        # Channel resolves to the same peer as the supergroup
        resolver = _FakeResolver(
            {
                channel_id: LinkedChatResolution(linked_chat_id=supergroup_id, flood_wait_seconds=None),
            }
        )
        monkeypatch.setattr(
            "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
            resolver,
        )

        count = asyncio.run(build_working_set(_FakeClient(), conn))

        assert count == 1, f"Expected 1 peer (dedup), got {count}"
        row = _get_activity_row(conn, supergroup_id)
        assert row is not None
        # Source must be 'supergroup' (direct enrollment wins over linked_chat)
        assert row["source"] == "supergroup", f"Deduped peer source must be 'supergroup', got {row['source']!r}"


# ---------------------------------------------------------------------------
# (e) FloodWait on second channel halts the pass — account-global invariant
# ---------------------------------------------------------------------------


def test_build_working_set_floodwait_halts_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """FloodWait on the 2nd of 3 channels must halt the pass (break, not continue).

    Verifies the account-global FloodWait invariant: once Telegram issues a
    wait, every further request in the same pass is sent during the wait window.
    The resolver must be called exactly TWICE (the first and second channels
    visited in iteration order); the third channel must never be reached.

    Uses position-based flood assignment so the test is order-agnostic with
    respect to SQLite's internal rowid iteration.
    """
    with closing(_make_db()) as conn:
        channel_a = -100400000001
        channel_b = -100400000002
        channel_c = -100400000003
        linked_first = -100400000010

        _insert_dialog(conn, channel_a, "channel", last_message_at=1000)
        _insert_dialog(conn, channel_b, "channel", last_message_at=2000)
        _insert_dialog(conn, channel_c, "channel", last_message_at=3000)

        all_channel_ids = {channel_a, channel_b, channel_c}
        call_log: list[int] = []

        async def mock_resolver(client: object, conn: sqlite3.Connection, channel_id: int) -> LinkedChatResolution:
            del client, conn
            call_log.append(channel_id)
            if len(call_log) == 1:
                # First channel visited → clean resolution with a linked chat
                return LinkedChatResolution(linked_chat_id=linked_first, flood_wait_seconds=None)
            if len(call_log) == 2:
                # Second channel visited → FloodWait; pass must halt here
                return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=30)
            # Third channel must never be reached
            raise AssertionError(f"resolve_linked_chat_id called a 3rd time for channel_id={channel_id!r}")

        monkeypatch.setattr(
            "mcp_telegram.activity_peer_sweep.resolve_linked_chat_id",
            mock_resolver,
        )

        asyncio.run(build_working_set(_FakeClient(), conn))

        # Resolver called exactly twice: first (ok) + second (flood) → break
        assert len(call_log) == 2, f"Expected exactly 2 resolver calls, got {len(call_log)}: {call_log}"
        flood_channel = call_log[1]  # second channel visited triggered FloodWait
        skipped_channel = (all_channel_ids - set(call_log)).pop()  # third, never reached

        # Working set contains linked_first (from the first channel) only
        assert _get_activity_row(conn, linked_first) is not None, "linked_first (from first channel) must be enrolled"
        assert _get_activity_row(conn, flood_channel) is None, "FloodWait channel must NOT be enrolled"
        assert _get_activity_row(conn, skipped_channel) is None, "Skipped (never-reached) channel must NOT be enrolled"

        # flood_channel's dialogs.linked_chat_resolved_at stays NULL (resolver's FloodWait
        # branch did not write to it — plan 02 task 3 guarantee)
        row = cast(
            tuple[int | None] | None,
            conn.execute(
                "SELECT linked_chat_resolved_at FROM dialogs WHERE dialog_id = ?",
                (flood_channel,),
            ).fetchone(),
        )
        assert row is not None
        assert row[0] is None, f"flood_channel linked_chat_resolved_at must stay NULL after FloodWait, got {row[0]!r}"


# ---------------------------------------------------------------------------
# sweep_peer_once: exit-path coverage and persistence gating
# ---------------------------------------------------------------------------


def test_sweep_peer_once_resolve_none_returns_access_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing peer resolution returns ACCESS_SKIP and never calls SearchRequest."""
    with closing(_make_db()) as conn:
        calls: list[object] = []

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object | None:
            del client, dialog_id
            return None

        async def fake_call_with_timeout(client: object, request: object) -> object:
            del client, request
            calls.append(object())
            raise AssertionError("call_with_timeout must not be called when resolve_input_peer returns None")

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)

        result = asyncio.run(
            sweep_peer_once(
                PeerSweepRequest(client=_FakeClient(), conn=conn, dialog_id=123, offset_id=7, min_id=3, limit=25)
            )
        )

        assert calls == []
        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.ACCESS_SKIP,
        )


def test_sweep_peer_once_floodwait_reports_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """FloodWait becomes a non-sleeping FLOOD_WAIT result with seconds preserved."""
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.messages import SearchRequest

    with closing(_make_db()) as conn:
        captured: dict[str, object] = {}

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object) -> object:
            del client
            captured["request"] = request
            raise FloodWaitError(request=None, capture=37)

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=456,
                offset_id=11,
                min_id=5,
                limit=50,
            )
        )

        assert "request" in captured
        request = cast(SearchRequest, captured["request"])
        assert request.offset_id == 11
        assert request.min_id == 5
        assert request.limit == 50
        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.FLOOD_WAIT,
            flood_wait_seconds=37,
        )


def test_sweep_peer_once_timeout_returns_access_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TimeoutError is treated as ACCESS_SKIP, not history-floor completion."""
    with closing(_make_db()) as conn:
        called = False

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object) -> object:
            nonlocal called
            del client, request
            called = True
            raise TimeoutError

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=789,
                offset_id=4,
                min_id=2,
                limit=10,
            )
        )

        assert called is True
        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.ACCESS_SKIP,
        )


def test_sweep_peer_once_empty_batch_is_history_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable peer with no messages returns HISTORY_FLOOR."""
    with closing(_make_db()) as conn:

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object) -> _FakeSweepResult:
            del client, request
            return _FakeSweepResult(messages=[])

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=111,
                offset_id=9,
                min_id=1,
                limit=20,
            )
        )

        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.HISTORY_FLOOR,
        )


def test_sweep_peer_once_persists_only_extractable_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only messages with a resolvable dialog_id are extracted and persisted."""
    with closing(_make_db()) as conn:
        inserted: list[list[tuple[int, str]]] = []

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object) -> _FakeSweepResult:
            del client, request
            return _FakeSweepResult(
                messages=[
                    _FakeSweepMessage(8, peer_id="keep"),
                    _FakeSweepMessage(3, peer_id="drop"),
                    _FakeSweepMessage(5, peer_id="keep"),
                ]
            )

        def fake_extract_dialog_id(message: _FakeSweepMessage) -> int | None:
            return 101 if message.peer_id == "keep" else None

        def fake_extract_message_row(dialog_id: int, message: _FakeSweepMessage) -> tuple[int, str]:
            return (dialog_id, f"msg-{message.id}")

        def fake_insert_messages_with_fts(conn: sqlite3.Connection, rows: list[tuple[int, str]]) -> None:
            del conn
            inserted.append(rows)

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.extract_dialog_id", fake_extract_dialog_id)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.extract_message_row", fake_extract_message_row)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.insert_messages_with_fts", fake_insert_messages_with_fts)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=222,
                offset_id=13,
                min_id=6,
                limit=30,
            )
        )

        assert inserted == [[(101, "msg-8"), (101, "msg-5")]]
        assert result == SweepResult(
            fetched_ids=[8, 3, 5],
            persisted=2,
            min_id=3,
            max_id=8,
            skip_reason=SkipReason.NONE,
        )


# ---------------------------------------------------------------------------
# enroll_activity_dialog: ON CONFLICT doesn't overwrite cursor columns
# ---------------------------------------------------------------------------


def test_enroll_does_not_overwrite_cursors():
    """enroll_activity_dialog ON CONFLICT must not clobber per-tier cursor state."""
    with closing(_make_db()) as conn:
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

        row = cast(
            tuple[int, str] | None,
            conn.execute(
                "SELECT hot_cursor, cold_status FROM activity_dialog_state WHERE dialog_id = ?",
                (peer_id,),
            ).fetchone(),
        )
        assert row is not None
        assert row[0] == 999, "hot_cursor must be preserved across re-enrollment"
        assert row[1] == "running", "cold_status must be preserved across re-enrollment"


# ---------------------------------------------------------------------------
# enroll_activity_dialog: synced_dialogs INSERT OR IGNORE never downgrades
# ---------------------------------------------------------------------------


def test_enroll_never_downgrades_synced_dialogs():
    """enroll_activity_dialog must not downgrade an existing higher-status synced_dialogs row."""
    with closing(_make_db()) as conn:
        peer_id = -100000000002

        # Pre-insert with a higher status
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
            (peer_id,),
        )
        conn.commit()

        enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=1000)

        row = cast(
            tuple[str] | None,
            conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (peer_id,)).fetchone(),
        )
        assert row is not None
        assert row[0] == "synced", f"Status must not be downgraded from 'synced' to 'own_only', got {row[0]!r}"


# ---------------------------------------------------------------------------
# _load_dialog_state / _save_dialog_state
# ---------------------------------------------------------------------------


def test_save_and_load_dialog_state():
    """_save_dialog_state writes whitelisted columns; _load_dialog_state reads them back."""
    with closing(_make_db()) as conn:
        peer_id = -100000000003
        enroll_activity_dialog(conn, peer_id, "supergroup")

        _save_dialog_state(conn, peer_id, hot_cursor=42, cold_status="running")
        state = _load_dialog_state(conn, peer_id)

        assert state["hot_cursor"] == 42
        assert state["cold_status"] == "running"


def test_save_dialog_state_rejects_unknown_columns():
    """_save_dialog_state raises ValueError for unknown column names."""
    with closing(_make_db()) as conn:
        peer_id = -100000000004
        enroll_activity_dialog(conn, peer_id, "supergroup")

        with pytest.raises(ValueError, match="unknown fields"):
            _save_dialog_state(conn, peer_id, nonexistent_col=1)


# ---------------------------------------------------------------------------
# SweepResult.hit_floor contract
# ---------------------------------------------------------------------------


def test_hit_floor_only_for_history_floor():
    """hit_floor is True ONLY for HISTORY_FLOOR, False for all other SkipReasons."""
    for reason in SkipReason:
        r = SweepResult(fetched_ids=[], persisted=0, min_id=None, max_id=None, skip_reason=reason)
        expected = reason is SkipReason.HISTORY_FLOOR
        assert r.hit_floor == expected, f"hit_floor expected {expected} for {reason!r}, got {r.hit_floor}"


# ---------------------------------------------------------------------------
# WR-01: allowlist/DDL drift guard for _save_dialog_state
# ---------------------------------------------------------------------------


def test_dialog_state_column_allowlist_matches_table():
    """_DIALOG_STATE_COLUMNS must stay in sync with the real activity_dialog_state
    columns. _save_dialog_state interpolates these names into SQL, so a drifted
    allowlist either fails at runtime (name not in table) or silently permits
    updating an identity/bookkeeping column. Guard both directions.
    """
    with closing(_make_db()) as conn:
        real_cols = {
            row[1]
            for row in cast(
                list[tuple[int, str, str, int, str | None, int]],
                conn.execute("PRAGMA table_info(activity_dialog_state)").fetchall(),
            )
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
    with closing(_make_db()) as conn:
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
    row = cast(
        tuple[str] | None,
        conn.execute("SELECT source FROM activity_dialog_state WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    return row[0] if row else None


# ---------------------------------------------------------------------------
# enroll_activity_dialog: thin dialogs row (needs_refresh=1) — Bug #1 fix
# ---------------------------------------------------------------------------


def test_enroll_creates_thin_dialogs_row():
    """enroll_activity_dialog must create a thin dialogs row with needs_refresh=1, hidden=0,
    name IS NULL for a peer that has no prior dialogs entry."""
    with closing(_make_db()) as conn:
        peer_id = -100777000001

        enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=1000)

        row = cast(
            tuple[int, int, str | None] | None,
            conn.execute(
                "SELECT needs_refresh, hidden, name FROM dialogs WHERE dialog_id = ?",
                (peer_id,),
            ).fetchone(),
        )
        assert row is not None, "enroll_activity_dialog must create a dialogs row"
        assert row[0] == 1, f"needs_refresh must be 1, got {row[0]!r}"
        assert row[1] == 0, f"hidden must be 0, got {row[1]!r}"
        assert row[2] is None, f"name must be NULL until reconciliation fills it, got {row[2]!r}"


def test_enroll_does_not_clobber_resolved_dialog():
    """enroll_activity_dialog must NOT overwrite an already-resolved dialogs row.
    INSERT OR IGNORE means the existing row (name, type, needs_refresh) is unchanged."""
    with closing(_make_db()) as conn:
        peer_id = -100777000002

        # Pre-insert a fully resolved dialogs row
        conn.execute(
            "INSERT INTO dialogs (dialog_id, name, type, needs_refresh, snapshot_at,"
            " archived, pinned, hidden, unread_mentions_count, unread_reactions_count)"
            " VALUES (?, 'Resolved Chat', 'user', 0, 1700000000, 0, 0, 0, 0, 0)",
            (peer_id,),
        )
        conn.commit()

        enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=1000)

        row = cast(
            tuple[str, str, int] | None,
            conn.execute(
                "SELECT name, type, needs_refresh FROM dialogs WHERE dialog_id = ?",
                (peer_id,),
            ).fetchone(),
        )
        assert row is not None
        assert row[0] == "Resolved Chat", f"name must not be clobbered, got {row[0]!r}"
        assert row[1] == "user", f"type must not be clobbered, got {row[1]!r}"
        assert row[2] == 0, f"needs_refresh must stay 0 (not reset to 1), got {row[2]!r}"
