"""Acceptance coverage for paired refresh lifetime and telemetry boundaries."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import mcp_telegram.runtime_observations as runtime_observations
from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.daemon import _persist_runtime_observation_loss, _SyncMainContext
from mcp_telegram.entity_profile.refresh import (
    DurableRefreshSliceResult,
    DurableRefreshTerminal,
    EntityProfileDemandAdapter,
    EntityRefreshCoordinator,
    RefreshEnqueueResult,
)
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient, _prepare


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def record(self, **values: object) -> None:
        self.rows.append(values)


@pytest.mark.asyncio
async def test_closed_adapter_does_not_read_or_acquire_unknown_entity() -> None:
    coordinator = EntityRefreshCoordinator()
    status_calls = 0
    acquisition_calls = 0

    def status(_now: float, _excluded: frozenset[object]) -> DemandStatus:
        nonlocal status_calls
        status_calls += 1
        return DemandStatus(release_at=0.0)

    async def run_slice(_budget: RpcAttemptBudget) -> DurableRefreshSliceResult:
        nonlocal acquisition_calls
        acquisition_calls += 1
        return DurableRefreshSliceResult(999, DurableRefreshTerminal.SUCCESS)

    coordinator.bind_durable_executor(status, run_slice)
    await coordinator.shutdown()
    selected = WorkRevision(WorkKey(DemandKind.ENTITY_PROFILE_REFRESH, 1, "unknown"), "prior")
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1, selected=selected))

    assert status_calls == 0
    assert acquisition_calls == 0


@pytest.mark.asyncio
async def test_actual_sink_loss_is_durable_and_does_not_change_pair_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "pair-telemetry-loss.sqlite"
    conn, service = _prepare(path, migrated=True)
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    client = cast(_PairClient, service._deps.client)  # type: ignore[attr-defined]
    await run_ready_slice(EntityProfileDemandAdapter(coordinator))
    assert client.full_user_calls == 1
    assert conn.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=42 "
        "AND section IN ('full_profile', 'personal_channel') ORDER BY section"
    ).fetchall() == [("fresh",), ("fresh",)]

    # Use thread events in the writer hook so the test never depends on an
    # event-loop call from the sink's worker thread.
    writer_started = threading.Event()
    writer_release = threading.Event()

    def blocked_write_thread(*_args: object, **_kwargs: object) -> None:
        writer_started.set()
        writer_release.wait(timeout=2.0)

    writer = await SQLiteWriter.open(path)
    sink = runtime_observations.RuntimeObservationSink(
        path,
        writer=writer,
        retention_ttl_seconds=60,
        policy=replace(RuntimeObservationConfig(), queue_capacity=1),
    )
    monkeypatch.setattr(sink, "_write_batch", blocked_write_thread)
    sink.record(kind="entity_profile.pair", outcome="head")
    assert await asyncio.to_thread(writer_started.wait, 1.0)
    for index in range(8):
        sink.record(kind="entity_profile.pair", outcome=f"overflow-{index}")
    assert sink.queue_full_drops > 0
    writer_release.set()
    await sink.aclose()

    ctx = cast(_SyncMainContext, SimpleNamespace(conn=conn, writer=writer, rpc_observation_sink=sink))
    await _persist_runtime_observation_loss(ctx)
    await writer.aclose()
    loss_row = cast(
        tuple[object, object] | None,
        conn.execute(
            "SELECT outcome, payload_json FROM runtime_observations WHERE kind='runtime.telemetry_loss'"
        ).fetchone(),
    )
    assert loss_row is not None
    assert loss_row[0] == "loss"
    payload = cast(dict[str, object], json.loads(cast(str, loss_row[1])))
    assert payload["queue_full_drops"] == sink.queue_full_drops
    assert conn.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=42 "
        "AND section IN ('full_profile', 'personal_channel') ORDER BY section"
    ).fetchall() == [("fresh",), ("fresh",)]
    assert (
        conn.execute("SELECT value FROM daemon_state WHERE key='runtime_observations_last_loss_ms'").fetchone()
        is not None
    )

    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_file_backed_pending_pair_shutdown_preserves_state_then_reopens_for_one_pair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pending-pair-shutdown.sqlite"
    conn, raw_service = _prepare(path, migrated=True)
    service = cast(object, raw_service)
    service._deps = replace(service._deps, full_user_auth_scope=None)  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
    before = {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        for table in ("entities", "entity_details", "entity_detail_sections", "entity_profile_refresh_state")
    }
    client = cast(_PairClient, service._deps.client)  # type: ignore[attr-defined]

    first = asyncio.create_task(service.get_entity_info({"entity_id": 42}))  # type: ignore[attr-defined]
    second = asyncio.create_task(service.get_entity_info({"entity_id": 42}))  # type: ignore[attr-defined]
    await asyncio.sleep(0)
    await service.shutdown()  # type: ignore[attr-defined]
    results = await asyncio.gather(first, second)

    assert all(result["ok"] is True for result in results)
    assert all(result["data"]["completeness"] == "partial" for result in results)
    assert client.full_user_calls == 0
    after = {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall() for table in before}
    assert after == before
    conn.close()

    reopened = sqlite3.connect(path)
    restarted = _pair_service(reopened, _PairClient(), enabled=True)
    restarted_coordinator = restarted.refresh_coordinator  # type: ignore[attr-defined]
    assert restarted_coordinator is not None
    restarted_client = cast(_PairClient, restarted._deps.client)  # type: ignore[attr-defined]
    await run_ready_slice(EntityProfileDemandAdapter(restarted_coordinator))

    assert restarted_client.full_user_calls == 1
    assert reopened.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=42 "
        "AND section IN ('full_profile', 'personal_channel') ORDER BY section"
    ).fetchall() == [("fresh",), ("fresh",)]
    await restarted.shutdown()  # type: ignore[attr-defined]
    reopened.close()


def test_profile_summary_flush_is_idempotent_for_denominator() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    aggregator.observe_profile_pair(
        mode="enabled",
        eligible_pair=True,
        outcome="committed",
        actual_attempts=1,
        full_profile_outcome="usable",
        personal_channel_outcome="absent",
        pair_ready=True,
    )
    aggregator.flush(now=300.0)
    aggregator.flush(now=600.0)

    assert len(recorder.rows) == 1
    payload = recorder.rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["event_count"] == 1
    assert payload["actual_attempts"] == 1
