"""Hydration snapshots and clearance estimates use durable queue evidence."""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.hydration_progress import clearance_estimate, queue_snapshots
from mcp_telegram.hydration_queue import HydrationJob, HydrationQueueRepository
from mcp_telegram.operator_summary import build_operator_summary
from mcp_telegram.runtime_observations import record_runtime_observation
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction


def sample(timestamp: int, active: int, *, runtime: str = "one", **extra: object) -> dict[str, object]:
    return {
        "observed_at_ms": timestamp,
        "runtime_instance_id": runtime,
        "payload": {"kind": "rich_composition", "active": active, "scan_complete": True, **extra},
    }


def test_estimate_conditions() -> None:
    now = 4_000_000
    rows = [sample(now - 1_800_000 + i * 300_000, 100 - i * 10) for i in range(7)]
    assert clearance_estimate(rows, now, None) == (
        "1970-01-01 01:26:40Z, 0.3h remaining; conditional on net drain 120.0 jobs/h over 30min; sample age 0.0min"
    )
    assert "stale" in clearance_estimate(rows, now + 900_001, None)
    assert "growing or flat" in clearance_estimate(
        [sample(now - 1_800_000 + i * 300_000, 10) for i in range(7)], now, None
    )
    assert "restart" in clearance_estimate(
        [*rows[:-3], *[sample(now - (2 - i) * 300_000, 20 - i, runtime="two") for i in range(3)]], now, None
    )
    assert "boundary" in clearance_estimate(rows, now, now - 600_000)
    assert "pause" in clearance_estimate([*rows[:-1], sample(now, 40, paused=True)], now, None)
    assert "incomplete" in clearance_estimate([*rows[:-1], sample(now, 40, scan_complete=False)], now, None)
    assert "gap" in clearance_estimate([rows[0], rows[-1]], now, None)
    assert "gap" in clearance_estimate([rows[-1], rows[-1]], now, None)


def test_snapshot_to_summary_with_narrow_window(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    now = 2_000_000_000
    try:
        with write_transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO daemon_state(key,value) VALUES ('rich_composition_hydration_cursor', ?)",
                (json.dumps({"done": True}),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO daemon_state(key,value) VALUES ('runtime_observations_history_started_at_ms', ?)",
                (str((now - 4000) * 1000),),
            )
            queue = HydrationQueueRepository(conn)
            for message_id in range(1, 103):
                queue.enqueue(HydrationJob("rich_composition", 1, message_id, due_at=0, attempts=0))
            for message_id in (101, 102):
                queue.mark_terminal(HydrationJob("rich_composition", 1, message_id, due_at=0, attempts=0))
            for i in range(7):
                if i:
                    for message_id in range((i - 1) * 10 + 1, i * 10 + 1):
                        queue.remove(HydrationJob("rich_composition", 1, message_id, due_at=0, attempts=0))
                snapshots = queue_snapshots(conn, now - 1800 + i * 300, paused=False)
                for payload in snapshots:
                    record_runtime_observation(
                        conn,
                        kind="hydration.queue_snapshot",
                        observed_at_ms=(now - 1800 + i * 300) * 1000,
                        payload=payload,
                    )
    finally:
        conn.close()
    before = path.read_bytes()
    text = build_operator_summary(path, since_seconds=60, now=now).text
    assert path.read_bytes() == before
    assert "rich_composition: jobs active=40" in text
    assert "terminal=2; queue-clearance ETA" in text
    assert "conditional on net drain" in text
    assert "media_metadata: jobs active=0" in text
    conn = _open_sync_db(path)
    try:
        with write_transaction(conn):
            conn.execute("DELETE FROM hydration_jobs WHERE terminal=0")
            for payload in queue_snapshots(conn, now + 300, paused=False):
                record_runtime_observation(
                    conn, kind="hydration.queue_snapshot", observed_at_ms=(now + 300) * 1000, payload=payload
                )
    finally:
        conn.close()
    text = build_operator_summary(path, since_seconds=60, now=now + 300).text
    assert "rich_composition: jobs active=0, ready=0, foreground=0, backfill=0, terminal=2" in text


async def test_lifetime_records_snapshots_every_five_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from mcp_telegram import daemon

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    clock = [0.0]
    stop = asyncio.Event()
    captured: list[dict[str, object]] = []

    async def writer_run(operation: Callable[[sqlite3.Connection], object]) -> object:
        if operation is daemon._prune_draft_retention:
            return None
        if not captured:
            clock[0] += 10
        return operation(conn)

    async def tick(_seconds: float) -> None:
        clock[0] += 150
        if clock[0] >= 750:
            stop.set()

    monkeypatch.setattr(daemon, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))
    monkeypatch.setattr(daemon, "_log_heartbeat", lambda *_args: None)
    monkeypatch.setattr(daemon, "_reconcile_initial_profile_history", AsyncMock())
    monkeypatch.setattr(daemon.asyncio, "sleep", tick)
    sink = SimpleNamespace(record=lambda **kwargs: captured.append(kwargs))
    ctx = SimpleNamespace(
        shutdown_event=stop,
        conn=conn,
        client=SimpleNamespace(account_safety_status=lambda: None),
        writer=SimpleNamespace(run=writer_run),
        handler_manager=None,
        coordinator=None,
        rpc_observation_sink=sink,
    )
    try:
        await daemon._run_daemon_lifetime(cast(daemon._SyncMainContext, ctx))
    finally:
        conn.close()
    assert len(captured) == 6
    assert [event["observed_at_ms"] for event in captured] == [310000] * 3 + [610000] * 3
