"""Recovery telemetry remains bounded and independent of the safety owner."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.runtime_observations import RecoveryObservationAdapter, RuntimeObservationSink
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_rpc_recovery import (
    RecoveryAction,
    RecoveryEvent,
    RecoveryObservation,
    RecoveryObservationEvent,
    RecoveryStage,
)


@pytest.mark.asyncio
async def test_recovery_observations_use_sink_and_preserve_unsampled_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    clock = [10.0]
    monkeypatch.setattr("mcp_telegram.runtime_observations.time.monotonic", lambda: clock[0])
    adapter = RecoveryObservationAdapter(sink, summary_interval_seconds=60)
    decision = RecoveryObservation(RecoveryStage.DECISION, RecoveryObservationEvent.DECISION, 1000)
    adapter.observe(decision)
    adapter.observe(replace(decision, observed_at_ms=2000, episode_id="a" * 32))
    adapter.observe(replace(decision, attempts_total=1))
    clock[0] += 61
    adapter.observe(decision)
    completed = RecoveryObservation(RecoveryStage.AWAIT, RecoveryObservationEvent.AWAIT_COMPLETED, 3000)
    entered = replace(completed, event=RecoveryObservationEvent.AWAIT_ENTERED)
    adapter.observe(entered)
    adapter.observe(completed)
    adapter.observe(entered)
    adapter.observe(completed)
    await sink.aclose()
    await writer.aclose()
    with closing(sqlite3.connect(path)) as conn:
        rows = cast(
            list[tuple[str, str]],
            conn.execute("SELECT kind, payload_json FROM runtime_observations ORDER BY id").fetchall(),
        )
    assert len(rows) == 7
    assert all(kind == "telegram.recovery" and len(payload.encode()) <= 1024 for kind, payload in rows)
    assert [json.loads(payload)["event"] for _, payload in rows][-4:] == [
        "await_entered",
        "await_completed",
        "await_entered",
        "await_completed",
    ]


@pytest.mark.asyncio
async def test_recovery_rejection_is_counted_and_unsafe_identifiers_are_not_recorded(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(
        path, writer=writer, retention_ttl_seconds=3600, policy=replace(RuntimeObservationConfig(), queue_capacity=1)
    )
    adapter = RecoveryObservationAdapter(sink, summary_interval_seconds=60)
    event = RecoveryObservation(
        RecoveryStage.GUARD, RecoveryObservationEvent.PENDING_LEDGER, 1000, episode_id="private content" * 200
    )
    adapter.observe(event)
    adapter.observe(replace(event, attempts_total=1))
    assert sink.counters.queue_full_drops == 1
    await sink.aclose()
    adapter.observe(replace(event, attempts_total=2))
    assert sink.counters.rejected_submissions == 1
    await writer.aclose()
    with closing(sqlite3.connect(path)) as conn:
        row = cast(tuple[str], conn.execute("SELECT payload_json FROM runtime_observations").fetchone())
        payload = row[0]
    assert "private content" not in payload
    assert "episode_id" not in json.loads(payload)


def test_recovery_observer_swallows_sink_errors() -> None:
    class BrokenSink:
        def record(self, **_kwargs: object) -> bool:
            raise RuntimeError("sensitive error")

    adapter = RecoveryObservationAdapter(BrokenSink(), summary_interval_seconds=1)  # type: ignore[arg-type]
    adapter.observe(RecoveryObservation(RecoveryStage.COMMIT, RecoveryObservationEvent.CONSUMED, 1000))


def test_interleaved_decisions_and_guard_transitions() -> None:
    class Sink:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []
            self.accept = True

        def record(self, **kwargs: object) -> bool:
            if self.accept:
                self.events.append(kwargs)
            return self.accept

    sink = Sink()
    adapter = RecoveryObservationAdapter(sink, summary_interval_seconds=60)  # type: ignore[arg-type]
    a = RecoveryObservation(
        RecoveryStage.DECISION,
        RecoveryObservationEvent.DECISION,
        1000,
        action=RecoveryAction.MAINTENANCE,
        policy_event=RecoveryEvent.DUE,
    )
    b = replace(a, action=RecoveryAction.QUIESCE, policy_event=RecoveryEvent.QUIESCED)
    adapter.observe(a)
    adapter.observe(b)
    adapter.observe(replace(a, elapsed_ms=7, episode_id="a" * 32, grant_id="b" * 32))
    adapter.observe(b)
    assert len(sink.events) == 2

    guard_a = RecoveryObservation(RecoveryStage.GUARD, RecoveryObservationEvent.PENDING_LEDGER, 1000)
    guard_b = replace(guard_a, event=RecoveryObservationEvent.PENDING_RECEIPT)
    adapter.observe(guard_a)
    adapter.observe(guard_b)
    adapter.observe(guard_a)
    assert [entry["payload"]["event"] for entry in sink.events[2:]] == [  # type: ignore[index]
        "pending_ledger",
        "pending_receipt",
        "pending_ledger",
    ]

    rejected = replace(a, policy_event=RecoveryEvent.STARTUP_RECONCILE)
    sink.accept = False
    adapter.observe(rejected)
    sink.accept = True
    adapter.observe(rejected)
    assert len(sink.events) == 6
