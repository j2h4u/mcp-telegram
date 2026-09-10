"""Acceptance coverage for paired refresh lifetime and telemetry boundaries."""

from __future__ import annotations

import asyncio
import json
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
    RefreshLimits,
)
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget
from tests.test_entity_profile_full_user_pair import _PairClient, _prepare


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def record(self, **values: object) -> None:
        self.rows.append(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("cancelled", "timed_out"))
async def test_one_waiter_lifetime_does_not_cancel_durable_pair_work(mode: str) -> None:
    coordinator = EntityRefreshCoordinator(limits=RefreshLimits(max_queued_refreshes=1))
    assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def run_slice(_budget: RpcAttemptBudget) -> DurableRefreshSliceResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return DurableRefreshSliceResult(42, DurableRefreshTerminal.SUCCESS)

    coordinator.bind_durable_executor(lambda _now: DemandStatus(release_at=0.0), run_slice)
    first_timeout = 0.01 if mode == "timed_out" else 1.0
    first = asyncio.create_task(coordinator.wait_for_completion(42, first_timeout))
    second = asyncio.create_task(coordinator.wait_for_completion(42, 1.0))
    await asyncio.sleep(0)

    durable = asyncio.create_task(EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1)))
    assert await asyncio.wait_for(started.wait(), timeout=1.0) is True
    if mode == "cancelled":
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    else:
        assert await first is False
    assert coordinator.queue_depth == 1

    release.set()
    await asyncio.wait_for(durable, timeout=1.0)
    assert calls == 1
    assert await asyncio.wait_for(second, timeout=1.0) is True
    assert coordinator.queue_depth == 0
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_duplicate_waiters_under_queue_pressure_shutdown_without_extra_work() -> None:
    coordinator = EntityRefreshCoordinator(limits=RefreshLimits(max_queued_refreshes=1))
    assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
    assert coordinator.enqueue(42) is RefreshEnqueueResult.COALESCED
    assert coordinator.enqueue(43) is RefreshEnqueueResult.REJECTED
    calls = 0

    async def run_slice(_budget: RpcAttemptBudget) -> DurableRefreshSliceResult:
        nonlocal calls
        calls += 1
        return DurableRefreshSliceResult(42, DurableRefreshTerminal.SUCCESS)

    coordinator.bind_durable_executor(lambda _now: DemandStatus(release_at=0.0), run_slice)
    waiters = [asyncio.create_task(coordinator.wait_for_completion(42, 1.0)) for _ in range(2)]
    await asyncio.sleep(0)
    await coordinator.shutdown()
    assert await asyncio.gather(*waiters) == [True, True]
    assert calls == 0
    assert coordinator.queue_depth == 0


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
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert client.full_user_calls == 1
    assert conn.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=42 "
        "AND section IN ('full_profile', 'personal_channel') ORDER BY section"
    ).fetchall() == [("fresh",), ("fresh",)]

    # Use thread events in the writer hook so the test never depends on an
    # event-loop call from the sink's worker thread.
    writer_started = threading.Event()
    writer_release = threading.Event()

    def blocked_write_thread(*_args: object, **_kwargs: object) -> bool:
        writer_started.set()
        writer_release.wait(timeout=2.0)
        return True

    sink = runtime_observations.RuntimeObservationSink(
        path,
        retention_ttl_seconds=60,
        policy=replace(RuntimeObservationConfig(), queue_capacity=1),
    )
    monkeypatch.setattr(sink, "_write_job", blocked_write_thread)
    sink.record(kind="entity_profile.pair", outcome="head")
    assert writer_started.wait(timeout=1.0)
    for index in range(8):
        sink.record(kind="entity_profile.pair", outcome=f"overflow-{index}")
    assert sink.queue_full_drops > 0
    writer_release.set()
    sink.close()

    ctx = cast(_SyncMainContext, SimpleNamespace(conn=conn, rpc_observation_sink=sink))
    _persist_runtime_observation_loss(ctx)
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
    assert conn.execute(
        "SELECT value FROM daemon_state WHERE key='runtime_observations_last_loss_ms'"
    ).fetchone() is not None

    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


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
