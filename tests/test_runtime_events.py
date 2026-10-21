from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

import mcp_telegram.runtime_observations as runtime_observations
from mcp_telegram.runtime_observations import (
    ALLOWED_KINDS,
    _apply_runtime_observations_plan,
    _plan_runtime_observations_batch,
    _prune_runtime_observations_batch,
    encode_payload,
    prune_runtime_observations,
    record_runtime_observation,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction


def test_runtime_event_payload_is_bounded_and_kind_is_allowlisted() -> None:
    assert encode_payload({"value": 1}) == '{"value":1}'
    assert "telegram.demand" in ALLOWED_KINDS
    assert "telegram.rpc_request" in ALLOWED_KINDS
    with pytest.raises(ValueError, match="1024"):
        encode_payload({"value": "x" * 1024})
    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(ValueError, match="unsupported"):
            record_runtime_observation(conn, kind="arbitrary.debug")


def test_runtime_event_pruning_applies_ttl_and_cap(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        for event_id, observed_at in enumerate((1_000, 9_000, 9_100, 9_200), start=1):
            record_runtime_observation(
                conn,
                kind="mcp.call",
                tool_name=f"tool_{event_id}",
                observed_at_ms=observed_at,
            )
        deleted = prune_runtime_observations(conn, ttl_seconds=5, row_cap=2, now_ms=10_000)
        conn.commit()
        assert deleted == 2
        assert conn.execute("SELECT tool_name FROM runtime_observations ORDER BY id").fetchall() == [
            ("tool_3",),
            ("tool_4",),
        ]
        assert conn.execute(
            "SELECT value FROM daemon_state WHERE key='runtime_observations_last_cap_truncation_ms'"
        ).fetchone() == ("10000",)
    finally:
        conn.close()


def test_runtime_event_pruning_batches_ttl_and_cap_without_overdeleting(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        for event_id in range(900):
            record_runtime_observation(
                conn,
                kind="mcp.call",
                tool_name=f"expired_{event_id}",
                observed_at_ms=1_000,
            )
        for event_id in range(200):
            record_runtime_observation(
                conn,
                kind="mcp.call",
                tool_name=f"recent_{event_id}",
                observed_at_ms=10_000,
            )
        conn.commit()

        batches: list[runtime_observations._PruneBatchResult] = []
        while True:
            with conn:
                result = _prune_runtime_observations_batch(
                    conn,
                    ttl_seconds=5,
                    row_cap=10,
                    now_ms=10_000,
                )
            batches.append(result)
            if not result.has_more:
                break

        assert all(result.deleted <= 1_000 for result in batches)
        assert batches[0].cap_deleted == 0
        assert any(result.cap_deleted > 0 for result in batches[1:])
        assert len(batches) > 1
        assert conn.execute("SELECT COUNT(*) FROM runtime_observations").fetchone() == (10,)
        assert conn.execute(
            "SELECT value FROM daemon_state WHERE key='runtime_observations_last_cap_truncation_ms'"
        ).fetchone() == ("10000",)
    finally:
        conn.close()


def test_runtime_event_pruning_does_not_mark_ttl_only_cleanup_as_cap_truncation(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        for event_id in range(20):
            record_runtime_observation(
                conn,
                kind="mcp.call",
                tool_name=f"expired_{event_id}",
                observed_at_ms=1_000,
            )
        conn.commit()
        prune_runtime_observations(conn, ttl_seconds=5, row_cap=100, now_ms=10_000)
        assert (
            conn.execute(
                "SELECT value FROM daemon_state WHERE key='runtime_observations_last_cap_truncation_ms'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_runtime_event_prune_planning_does_not_block_competing_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    setup = _open_sync_db(path)
    record_runtime_observation(setup, kind="mcp.call", tool_name="old", observed_at_ms=1_000)
    setup.commit()
    setup.close()
    planned = threading.Event()
    release = threading.Event()
    original_plan = runtime_observations._plan_runtime_observations_batch

    def slow_plan(*args: object, **kwargs: object) -> runtime_observations._PrunePlan:
        planned.set()
        release.wait(timeout=2)
        return original_plan(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_observations, "_plan_runtime_observations_batch", slow_plan)
    prune_result: list[runtime_observations._PruneBatchResult] = []

    def run_prune() -> None:
        prune_conn = _open_sync_db(path)
        prune_result.append(
            _prune_runtime_observations_batch(
                prune_conn,
                ttl_seconds=5,
                row_cap=1,
                now_ms=10_000,
            )
        )
        prune_conn.close()

    worker = threading.Thread(target=run_prune)
    worker.start()
    assert planned.wait(timeout=0.5)
    with closing(sqlite3.connect(path)) as competitor:
        record_runtime_observation(competitor, kind="mcp.call", tool_name="competitor", observed_at_ms=10_000)
        competitor.commit()
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert prune_result[0].deleted == 1
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("SELECT tool_name FROM runtime_observations").fetchall() == [("competitor",)]


def test_runtime_event_prune_applies_only_planned_ids_after_concurrent_append(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    planner = _open_sync_db(path)
    for event_id in range(5):
        record_runtime_observation(planner, kind="mcp.call", tool_name=f"old-{event_id}", observed_at_ms=10_000)
    planner.commit()
    plan = _plan_runtime_observations_batch(planner, ttl_seconds=5, row_cap=2, now_ms=10_000)
    assert plan.cap is True
    assert len(plan.ids) == 3

    with closing(sqlite3.connect(path)) as competitor:
        record_runtime_observation(competitor, kind="mcp.call", tool_name="fresh", observed_at_ms=10_000)
        competitor.commit()
    with write_transaction(planner):
        result = _apply_runtime_observations_plan(planner, plan, now_ms=10_000)
    planner.close()

    assert result.deleted == 3
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("SELECT tool_name FROM runtime_observations ORDER BY id").fetchall() == [
            ("old-3",),
            ("old-4",),
            ("fresh",),
        ]
