"""Focused contract tests for adaptive HotSweep scheduling."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.activity_hot_sweep import (
    _capped_empty_interval,
    deterministic_hot_due_at,
    run_hot_sweep_loop,
    run_hot_sweep_pass,
    seed_hot_sweep_schedule,
)
from mcp_telegram.activity_peer_sweep import (
    SkipReason,
    SweepResult,
    WorkingSetResult,
    enroll_activity_dialog,
)
from mcp_telegram.config import ActivityHotSweepConfig, load_config, resolve_scheduling_config
from mcp_telegram.sync_db import _apply_migrations


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


class _FakeClient:
    async def __call__(self, request: object) -> object:
        del request
        return object()

    async def get_input_entity(self, dialog_id: int) -> object:
        del dialog_id
        return object()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    value = _db()
    try:
        yield value
    finally:
        value.close()


def test_v39_hot_schedule_columns_and_due_index(conn: sqlite3.Connection) -> None:
    rows = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(activity_dialog_state)").fetchall())
    columns = {cast(str, row[1]) for row in rows}
    assert {"hot_next_due_at", "hot_empty_streak"} <= columns
    assert (
        conn.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_activity_dialog_state_hot_due'").fetchone()
        is not None
    )
    info = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(activity_dialog_state)").fetchall())
    assert next(row[4] for row in info if row[1] == "hot_empty_streak") == "0"


def test_enrollment_spread_is_stable_and_preserves_existing_due(conn: sqlite3.Connection) -> None:
    now = 1_700_000_000
    first = deterministic_hot_due_at(-1001, 86_400, now=now)
    assert first == deterministic_hot_due_at(-1001, 86_400, now=now)
    assert now <= first < now + 86_400
    enroll_activity_dialog(conn, -1001, "supergroup")
    assert seed_hot_sweep_schedule(conn, 86_400, now=now) == 1
    due = cast(
        int, conn.execute("SELECT hot_next_due_at FROM activity_dialog_state WHERE dialog_id = -1001").fetchone()[0]
    )
    enroll_activity_dialog(conn, -1001, "supergroup")
    assert (
        conn.execute("SELECT hot_next_due_at FROM activity_dialog_state WHERE dialog_id = -1001").fetchone()[0] == due
    )


def test_missing_signal_is_seeded_but_stale_known_signal_is_not(conn: sqlite3.Connection) -> None:
    now = 1_700_000_000
    enroll_activity_dialog(conn, -1002, "supergroup")
    enroll_activity_dialog(conn, -1003, "supergroup", last_activity_at=now - 31 * 86400)
    assert seed_hot_sweep_schedule(conn, 86_400, now=now) == 1
    assert conn.execute("SELECT hot_next_due_at FROM activity_dialog_state WHERE dialog_id = -1002").fetchone()[
        0
    ] == deterministic_hot_due_at(-1002, 86_400, now=now)
    assert (
        conn.execute("SELECT hot_next_due_at FROM activity_dialog_state WHERE dialog_id = -1003").fetchone()[0] is None
    )


def test_adaptive_pass_caps_fairly_and_reports_telemetry(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    now = int(time.time())
    for dialog_id in (-3, -2, -1):
        enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=now)
        conn.execute("UPDATE activity_dialog_state SET hot_next_due_at = ? WHERE dialog_id = ?", (now - 1, dialog_id))
    conn.commit()
    calls: list[int] = []

    async def build(*_args: object, **_kwargs: object) -> WorkingSetResult:
        return WorkingSetResult(enrolled_count=3)

    async def sweep(*args: object, **_kwargs: object) -> SweepResult:
        dialog_id = cast(int, args[2])
        calls.append(dialog_id)
        has_new = dialog_id == -3
        return SweepResult(
            [1],
            1,
            1,
            1,
            rpc_calls=2,
            pages_fetched=1,
            extracted=1,
            genuinely_new=int(has_new),
            genuinely_new_keys=frozenset({(dialog_id, 1)}) if has_new else frozenset(),
            completed=True,
        )

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.build_working_set", build)
    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.sweep_peer_once", sweep)
    telemetry = asyncio.run(
        run_hot_sweep_pass(
            _FakeClient(),
            conn,
            asyncio.Event(),
            policy=ActivityHotSweepConfig(max_peers_per_pass=2, jitter_max_seconds=0),
            timeout_s=1,
        )
    )
    assert calls == [-3, -2]
    assert telemetry["peers_selected"] == 2
    assert telemetry["due_remaining"] == 1
    assert telemetry["rpc_calls"] == 4
    assert telemetry["yielding_peers"] == 1
    assert conn.execute("SELECT hot_empty_streak FROM activity_dialog_state WHERE dialog_id = -3").fetchone()[0] == 0
    assert conn.execute("SELECT hot_empty_streak FROM activity_dialog_state WHERE dialog_id = -2").fetchone()[0] == 1


def test_last_event_wakes_future_due_peer_but_retry_gate_wins(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    now = int(time.time())
    wake_id = -20
    retry_id = -21
    for dialog_id in (wake_id, retry_id):
        enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=now)
        conn.execute(
            "UPDATE activity_dialog_state SET hot_next_due_at = ?, hot_last_sync_at = ? WHERE dialog_id = ?",
            (now + 3600, now - 100, dialog_id),
        )
    conn.execute("UPDATE synced_dialogs SET last_event_at = ? WHERE dialog_id = ?", (now, wake_id))
    conn.execute("UPDATE activity_dialog_state SET hot_next_retry_at = ? WHERE dialog_id = ?", (now + 3600, retry_id))
    conn.commit()

    calls: list[int] = []

    async def build(*_args: object, **_kwargs: object) -> WorkingSetResult:
        return WorkingSetResult(enrolled_count=2)

    async def sweep(*args: object, **_kwargs: object) -> SweepResult:
        calls.append(cast(int, args[2]))
        return SweepResult(
            [],
            0,
            None,
            None,
            skip_reason=SkipReason.HISTORY_FLOOR,
            rpc_calls=2,
            pages_fetched=1,
            completed=True,
        )

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.build_working_set", build)
    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.sweep_peer_once", sweep)
    telemetry = asyncio.run(
        run_hot_sweep_pass(
            _FakeClient(),
            conn,
            asyncio.Event(),
            policy=ActivityHotSweepConfig(jitter_max_seconds=0),
            timeout_s=1,
        )
    )
    assert calls == [wake_id]
    assert telemetry["peers_selected"] == 1


def test_working_set_flood_skips_hot_searches(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    now = int(time.time())
    enroll_activity_dialog(conn, -30, "supergroup", last_activity_at=now)
    conn.execute("UPDATE activity_dialog_state SET hot_next_due_at = ?", (now - 1,))
    conn.commit()
    searches: list[int] = []

    async def build(*_args: object, **_kwargs: object) -> WorkingSetResult:
        return WorkingSetResult(enrolled_count=1, flood_wait_seconds=45)

    async def sweep(*args: object, **_kwargs: object) -> SweepResult:
        searches.append(cast(int, args[2]))
        raise AssertionError("HotSweep must not search after working-set FloodWait")

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.build_working_set", build)
    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.sweep_peer_once", sweep)
    telemetry = asyncio.run(
        run_hot_sweep_pass(
            _FakeClient(), conn, asyncio.Event(), policy=ActivityHotSweepConfig(jitter_max_seconds=0), timeout_s=1
        )
    )
    assert searches == []
    assert telemetry["flooded"] is True
    assert telemetry["flood_wait_seconds"] == 45
    assert telemetry["peers_processed"] == 0


def test_repeated_access_skip_moves_behind_other_due_peer(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    now = int(time.time())
    for dialog_id in (-40, -41):
        enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=now)
        conn.execute("UPDATE activity_dialog_state SET hot_next_due_at = ? WHERE dialog_id = ?", (now - 1, dialog_id))
    conn.commit()
    selected: list[int] = []

    async def build(*_args: object, **_kwargs: object) -> WorkingSetResult:
        return WorkingSetResult(enrolled_count=2)

    async def sweep(*args: object, **_kwargs: object) -> SweepResult:
        selected.append(cast(int, args[2]))
        return SweepResult([], 0, None, None, skip_reason=SkipReason.ACCESS_SKIP, rpc_calls=1)

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.build_working_set", build)
    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.sweep_peer_once", sweep)
    policy = ActivityHotSweepConfig(max_peers_per_pass=1, jitter_max_seconds=0)
    asyncio.run(run_hot_sweep_pass(_FakeClient(), conn, asyncio.Event(), policy=policy, timeout_s=1))
    asyncio.run(run_hot_sweep_pass(_FakeClient(), conn, asyncio.Event(), policy=policy, timeout_s=1))
    assert selected == [-41, -40]


def test_event_does_not_bypass_uninitialized_spread(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    now = int(time.time())
    dialog_id = -50
    enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=now)
    conn.execute(
        "UPDATE activity_dialog_state SET hot_next_due_at = ?, hot_last_sync_at = NULL WHERE dialog_id = ?",
        (now + 3600, dialog_id),
    )
    conn.execute("UPDATE synced_dialogs SET last_event_at = ? WHERE dialog_id = ?", (now, dialog_id))
    conn.commit()
    selected: list[int] = []

    async def build(*_args: object, **_kwargs: object) -> WorkingSetResult:
        return WorkingSetResult(enrolled_count=1)

    async def sweep(*args: object, **_kwargs: object) -> SweepResult:
        selected.append(cast(int, args[2]))
        raise AssertionError("future initial spread must not be bypassed")

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.build_working_set", build)
    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.sweep_peer_once", sweep)
    asyncio.run(
        run_hot_sweep_pass(
            _FakeClient(), conn, asyncio.Event(), policy=ActivityHotSweepConfig(jitter_max_seconds=0), timeout_s=1
        )
    )
    assert selected == []


def test_attempt_recency_rotates_repeated_incomplete_peer(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    now = int(time.time())
    for dialog_id in (-51, -52):
        enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=now)
        conn.execute("UPDATE activity_dialog_state SET hot_next_due_at = ? WHERE dialog_id = ?", (now - 1, dialog_id))
    conn.commit()
    selected: list[int] = []

    async def build(*_args: object, **_kwargs: object) -> WorkingSetResult:
        return WorkingSetResult(enrolled_count=2)

    async def sweep(*args: object, **_kwargs: object) -> SweepResult:
        selected.append(cast(int, args[2]))
        return SweepResult(
            list(range(100)),
            100,
            None,
            100,
            rpc_calls=2,
            pages_fetched=1,
            extracted=100,
        )

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.build_working_set", build)
    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.sweep_peer_once", sweep)
    policy = ActivityHotSweepConfig(max_peers_per_pass=1, jitter_max_seconds=0)
    asyncio.run(run_hot_sweep_pass(_FakeClient(), conn, asyncio.Event(), policy=policy, timeout_s=1))
    asyncio.run(run_hot_sweep_pass(_FakeClient(), conn, asyncio.Event(), policy=policy, timeout_s=1))
    assert len(selected) == 2
    assert selected[0] != selected[1]
    assert set(selected) == {-51, -52}


def test_shutdown_after_working_set_does_not_seed_or_select(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    now = int(time.time())
    enroll_activity_dialog(conn, -53, "supergroup", last_activity_at=now)
    shutdown = asyncio.Event()

    async def build(*_args: object, **_kwargs: object) -> WorkingSetResult:
        shutdown.set()
        return WorkingSetResult(enrolled_count=1)

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.build_working_set", build)
    telemetry = asyncio.run(
        run_hot_sweep_pass(
            _FakeClient(), conn, shutdown, policy=ActivityHotSweepConfig(jitter_max_seconds=0), timeout_s=1
        )
    )
    assert telemetry["peers_selected"] == 0
    assert telemetry["peers_processed"] == 0
    assert telemetry["due_remaining"] == 1
    assert conn.execute("SELECT hot_next_due_at FROM activity_dialog_state WHERE dialog_id = -53").fetchone()[0] is None


def test_shutdown_during_success_does_not_cool_peer(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    now = int(time.time())
    dialog_id = -60
    enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=now)
    conn.execute(
        "UPDATE activity_dialog_state SET hot_next_due_at = ?, hot_empty_streak = 4 WHERE dialog_id = ?",
        (now - 1, dialog_id),
    )
    conn.commit()
    shutdown = asyncio.Event()

    async def build(*_args: object, **_kwargs: object) -> WorkingSetResult:
        return WorkingSetResult(enrolled_count=1)

    async def sweep(*_args: object, **_kwargs: object) -> SweepResult:
        shutdown.set()
        return SweepResult(
            [], 0, None, None, skip_reason=SkipReason.HISTORY_FLOOR, rpc_calls=2, pages_fetched=1, completed=True
        )

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.build_working_set", build)
    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.sweep_peer_once", sweep)
    asyncio.run(
        run_hot_sweep_pass(
            _FakeClient(), conn, shutdown, policy=ActivityHotSweepConfig(jitter_max_seconds=0), timeout_s=1
        )
    )
    row = cast(
        tuple[int, int] | None,
        conn.execute(
            "SELECT hot_empty_streak, hot_next_due_at FROM activity_dialog_state WHERE dialog_id = ?", (dialog_id,)
        ).fetchone(),
    )
    assert row == (4, now - 1)


def test_capped_empty_interval_handles_large_streak() -> None:
    assert _capped_empty_interval(3600, 604800, 1024) == 604800


@pytest.mark.asyncio
async def test_hot_loop_waits_for_longer_flood_duration(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    shutdown = asyncio.Event()
    pass_calls = 0
    wait_timeouts: list[float] = []

    async def fake_pass(*_args: object, **_kwargs: object) -> dict[str, int | float | bool | None]:
        nonlocal pass_calls
        pass_calls += 1
        return {"genuinely_new": 0, "flood_wait_seconds": 999}

    async def fake_wait_for(awaitable: object, timeout: float) -> bool:
        wait_timeouts.append(timeout)
        if hasattr(awaitable, "close"):
            awaitable.close()  # type: ignore[union-attr]
        shutdown.set()
        return True

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.run_hot_sweep_pass", fake_pass)
    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.asyncio.wait_for", fake_wait_for)
    await run_hot_sweep_loop(
        _FakeClient(), conn, shutdown, policy=ActivityHotSweepConfig(loop_interval_seconds=60), timeout_s=1
    )
    assert pass_calls == 1
    assert wait_timeouts == [999]


def test_config_nested_toml_and_env_override(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[state]\ndir = "/state"\n[scheduling.activity_hot_sweep]\nmax_peers_per_pass = 4\njitter_max_seconds = 0\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.scheduling.activity_hot_sweep.max_peers_per_pass == 4
    resolved = resolve_scheduling_config(
        config.scheduling,
        {"ACTIVITY_HOT_SWEEP_MAX_PEERS_PER_PASS": "7", "ACTIVITY_HOT_SWEEP_JITTER_MAX_SECONDS": "0"},
    )
    assert resolved.activity_hot_sweep.max_peers_per_pass == 7
    assert resolved.activity_hot_sweep.jitter_max_seconds == 0
