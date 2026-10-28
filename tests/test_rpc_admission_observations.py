from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions
from telethon.tl.alltlobjects import tlobjects
from telethon.tl.tlobject import TLRequest

from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.flood import FloodWaitObservation
from mcp_telegram.rpc_admission_observations import DemandEvidenceOutcome, RpcAdmissionObservationAggregator
from mcp_telegram.runtime_observations import MAX_PAYLOAD_BYTES, RuntimeObservationSink, encode_payload
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import AcquisitionKind, RpcAttemptBudget, RpcAttemptEvidence
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
from mcp_telegram.telegram_rpc import TelegramRpcGate
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    RPC_SOURCE_SERVICE_CLASS,
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    RpcServiceClass,
    TelegramRpcScope,
    TelegramRpcSource,
)


@dataclass
class _Recorder:
    rows: list[dict[str, object]] = field(default_factory=list)

    def record(self, **values: object) -> bool | None:
        self.rows.append(values)


def _method(kind: type) -> str:
    return f"{kind.__module__}.{kind.__name__}:{kind.CONSTRUCTOR_ID:08x}"


def _rpc_scope(target: str = "private-peer", revision: str = "cursor-secret") -> TelegramRpcScope:
    selected = WorkRevision(WorkKey(DemandKind.ENTITY_PROFILE_REFRESH, 12345, target), revision)
    return TelegramRpcScope(
        TelegramRpcSource.ENTITY_INFO_REFRESH,
        RpcServiceClass.BACKGROUND,
        1000.0,
        None,
        demand_kind=DemandKind.ENTITY_PROFILE_REFRESH,
        acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
        attempt_budget=RpcAttemptBudget(8, selected=selected),
        attempt_evidence=RpcAttemptEvidence(),
        request_method=_method(functions.users.GetFullUserRequest),
        owned_attempt_id="a" * 32,
    )


def _rpc_payloads(recorder: _Recorder, outcome: str) -> list[dict[str, object]]:
    return [cast(dict[str, object], row["payload"]) for row in recorder.rows if row["outcome"] == outcome]


def _event(
    kind: RpcAdmissionEventKind,
    *,
    source: TelegramRpcSource = TelegramRpcSource.MCP_INTERACTIVE,
    wait_seconds: float | None = None,
) -> RpcAdmissionEvent:
    return RpcAdmissionEvent(
        kind=kind,
        source=source,
        service_class=RPC_SOURCE_SERVICE_CLASS[source],
        queue_depth=2,
        total_depth=3,
        active_depth=1,
        total_outstanding=4,
        wait_seconds=wait_seconds,
    )


def test_routine_admissions_are_coalesced_into_one_source_summary() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)

    for wait_seconds in (0.1, 0.3):
        aggregator.observe(_event(RpcAdmissionEventKind.QUEUED))
        aggregator.observe(_event(RpcAdmissionEventKind.DISPATCHED, wait_seconds=wait_seconds))
    assert recorder.rows == []

    aggregator.flush(now=300.0)

    assert len(recorder.rows) == 1
    row = recorder.rows[0]
    assert row["outcome"] == "summary"
    assert row["result_count"] == 2
    assert row["duration_ms"] == 200.0
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert payload["source"] == "mcp_interactive"
    assert payload["dispatched_count"] == 2
    assert payload["max_wait_ms"] == 300.0
    assert payload["window_seconds"] == 300.0
    assert isinstance(payload["bucket_started_at_ms"], int)
    assert isinstance(payload["bucket_ended_at_ms"], int)
    assert row["observed_at_ms"] == payload["bucket_ended_at_ms"]


def test_admission_summary_records_actual_early_flush_span(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 10.0
    monkeypatch.setattr("mcp_telegram.rpc_admission_observations.time.time", lambda: 20_000.0)
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: now)
    aggregator.observe(_event(RpcAdmissionEventKind.DISPATCHED, wait_seconds=0.1))

    aggregator.flush(now=now + 7.5)

    row = recorder.rows[0]
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert payload["window_seconds"] == 7.5
    assert payload["bucket_started_at_ms"] == 19_992_500
    assert payload["bucket_ended_at_ms"] == 20_000_000
    assert row["observed_at_ms"] == 20_000_000


def test_terminal_admission_outcomes_remain_raw() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)

    aggregator.observe(_event(RpcAdmissionEventKind.EXPIRED, wait_seconds=15.0))

    assert recorder.rows[0]["outcome"] == "expired"
    assert recorder.rows[0]["duration_ms"] == 15_000.0
    payload = recorder.rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["source"] == "mcp_interactive"


def test_flood_wait_records_content_free_sender_provenance_immediately() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)

    aggregator.observe_flood_wait(
        FloodWaitObservation(
            source=TelegramRpcSource.MCP_INTERACTIVE,
            service_class=RPC_SOURCE_SERVICE_CLASS[TelegramRpcSource.MCP_INTERACTIVE],
            demand_kind=DemandKind.MCP_REMOTE_ACQUISITION,
            acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE,
            seconds=23,
            cooldown_until_utc_ms=1_700_000_023_000,
            circuit_open=False,
            request_method="GetHistoryRequest",
            origin="vendor_cache",
            actual_dispatch=False,
            admission_sequence=None,
            dispatch_at_monotonic=None,
            observed_at_ms=1_700_000_000_000,
        )
    )

    assert len(recorder.rows) == 1
    row = recorder.rows[0]
    assert row["kind"] == "telegram.rpc_admission"
    assert row["outcome"] == "flood_wait"
    assert row["reason_code"] == "vendor_cache"
    assert row["duration_ms"] == 23_000
    assert row["observed_at_ms"] == 1_700_000_000_000
    assert row["payload"] == {
        "source": "mcp_interactive",
        "service_class": "interactive",
        "request_method": "GetHistoryRequest",
        "origin": "vendor_cache",
        "actual_dispatch": False,
        "cooldown_until_utc_ms": 1_700_000_023_000,
        "circuit_open": False,
        "demand_kind": "mcp_remote_acquisition",
        "acquisition_kind": "message_history_page",
    }


@pytest.mark.asyncio
async def test_flood_wait_observation_persists_through_runtime_sink(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    aggregator = RpcAdmissionObservationAggregator(sink, policy=RuntimeObservationConfig(), clock=lambda: 0.0)

    aggregator.observe_flood_wait(
        FloodWaitObservation(
            source=TelegramRpcSource.MCP_INTERACTIVE,
            service_class=RPC_SOURCE_SERVICE_CLASS[TelegramRpcSource.MCP_INTERACTIVE],
            demand_kind=DemandKind.MCP_REMOTE_ACQUISITION,
            acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE,
            seconds=23,
            cooldown_until_utc_ms=1_700_000_023_000,
            circuit_open=True,
            request_method="GetHistoryRequest",
            origin="actual_send",
            actual_dispatch=True,
            admission_sequence=17,
            dispatch_at_monotonic=42.5,
            observed_at_ms=1_700_000_000_000,
        )
    )
    await sink.aclose()
    await writer.aclose()

    with closing(sqlite3.connect(path)) as conn:
        row = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT kind, outcome, reason_code, duration_ms, observed_at_ms, payload_json "
                "FROM runtime_observations WHERE kind = 'telegram.rpc_admission'"
            ).fetchone(),
        )
    assert row is not None
    assert len(row) == 6
    assert row[:5] == ("telegram.rpc_admission", "flood_wait", "actual_send", 23_000, 1_700_000_000_000)
    payload_json = row[5]
    assert isinstance(payload_json, str)
    assert json.loads(payload_json) == {
        "source": "mcp_interactive",
        "service_class": "interactive",
        "request_method": "GetHistoryRequest",
        "origin": "actual_send",
        "actual_dispatch": True,
        "cooldown_until_utc_ms": 1_700_000_023_000,
        "circuit_open": True,
        "demand_kind": "mcp_remote_acquisition",
        "acquisition_kind": "message_history_page",
        "admission_sequence": 17,
        "dispatch_at_monotonic": 42.5,
    }


def test_flood_wait_sink_failure_does_not_escape_after_circuit_latches() -> None:
    from mcp_telegram.flood import FloodWaitAccumulator, FloodWaitKillSwitchPolicy

    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(FloodWaitKillSwitchPolicy(True, 600, 1, 900))
    accumulator.observe(source="mcp_interactive", seconds=20, now_mono=1.0)
    assert accumulator.kill_switch_status(now_mono=1.0).open

    class _FailingRecorder:
        def record(self, **_values: object) -> None:
            raise RuntimeError("sink unavailable")

    aggregator = RpcAdmissionObservationAggregator(
        _FailingRecorder(), policy=RuntimeObservationConfig(), clock=lambda: 0.0
    )
    aggregator.observe_flood_wait(
        FloodWaitObservation(
            source=TelegramRpcSource.MCP_INTERACTIVE,
            service_class=RPC_SOURCE_SERVICE_CLASS[TelegramRpcSource.MCP_INTERACTIVE],
            demand_kind=DemandKind.MCP_REMOTE_ACQUISITION,
            acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE,
            seconds=20,
            cooldown_until_utc_ms=1_700_000_020_000,
            circuit_open=True,
            request_method="GetHistoryRequest",
            origin="actual_send",
            actual_dispatch=True,
            admission_sequence=1,
            dispatch_at_monotonic=1.0,
            observed_at_ms=1_700_000_000_000,
        )
    )

    assert accumulator.kill_switch_status(now_mono=1.0).open


def test_due_event_flushes_completed_window() -> None:
    now = 0.0
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        policy=replace(RuntimeObservationConfig(), rpc_summary_interval_seconds=5),
        clock=lambda: now,
    )
    aggregator.observe(_event(RpcAdmissionEventKind.QUEUED))
    now = 5.0

    aggregator.observe(_event(RpcAdmissionEventKind.DISPATCHED, wait_seconds=0.2))

    assert len(recorder.rows) == 1
    assert recorder.rows[0]["result_count"] == 1


def test_recorder_failure_never_escapes_scheduler_callback() -> None:
    class _FailingRecorder:
        def record(self, **_values: object) -> None:
            raise RuntimeError("offline")

    aggregator = RpcAdmissionObservationAggregator(
        _FailingRecorder(), policy=RuntimeObservationConfig(), clock=lambda: 0.0
    )
    aggregator.observe(_event(RpcAdmissionEventKind.CANCELLED))


def test_failed_summary_is_retained_without_replaying_successful_summaries() -> None:
    class _FlakyRecorder(_Recorder):
        fail_source = TelegramRpcSource.MCP_INTERACTIVE.value

        def record(self, **values: object) -> None:
            payload = values["payload"]
            assert isinstance(payload, dict)
            if payload["source"] == self.fail_source:
                self.fail_source = ""
                raise RuntimeError("offline")
            super().record(**values)

    recorder = _FlakyRecorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    aggregator.observe(_event(RpcAdmissionEventKind.DISPATCHED, wait_seconds=0.1))
    aggregator.observe(
        _event(
            RpcAdmissionEventKind.DISPATCHED,
            source=TelegramRpcSource.REALTIME_EVENT,
            wait_seconds=0.2,
        )
    )

    aggregator.flush(now=300.0)
    assert len(recorder.rows) == 1
    first_payload = recorder.rows[0]["payload"]
    assert isinstance(first_payload, dict)
    assert first_payload["source"] == "realtime_event"
    assert first_payload["dispatched_count"] == 1
    assert first_payload["window_seconds"] == 300.0

    aggregator.flush(now=600.0)
    assert len(recorder.rows) == 2
    retried_payload = recorder.rows[1]["payload"]
    assert isinstance(retried_payload, dict)
    assert retried_payload["source"] == "mcp_interactive"


def test_failed_admission_summary_retry_keeps_earliest_bucket_start(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    monkeypatch.setattr("mcp_telegram.rpc_admission_observations.time.time", lambda: 50_000.0)

    class _FlakyRecorder(_Recorder):
        fail_once = True

        def record(self, **values: object) -> None:
            if values.get("kind") == "telegram.rpc_admission" and self.fail_once:
                self.fail_once = False
                raise RuntimeError("offline")
            super().record(**values)

    recorder = _FlakyRecorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: now)
    aggregator.observe(_event(RpcAdmissionEventKind.DISPATCHED, wait_seconds=0.1))
    aggregator.flush(now=5.0)

    now = 45.0
    aggregator.observe(_event(RpcAdmissionEventKind.DISPATCHED, wait_seconds=0.2))
    aggregator.flush(now=45.0)

    assert len(recorder.rows) == 1
    payload = recorder.rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["dispatched_count"] == 2
    assert payload["window_seconds"] == 45.0
    assert payload["bucket_started_at_ms"] == 49_955_000
    assert payload["bucket_ended_at_ms"] == 50_000_000


def test_failed_flush_restores_only_its_unpersisted_summary_during_concurrent_callbacks() -> None:
    class _InterleavedRecorder(_Recorder):
        first_write_started = threading.Event()
        allow_first_write = threading.Event()
        fail_first_write = True

        def record(self, **values: object) -> None:
            payload = values["payload"]
            assert isinstance(payload, dict)
            if payload["source"] == TelegramRpcSource.MCP_INTERACTIVE.value and self.fail_first_write:
                self.fail_first_write = False
                self.first_write_started.set()
                assert self.allow_first_write.wait(timeout=1.0)
                raise RuntimeError("offline")
            super().record(**values)

    recorder = _InterleavedRecorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    aggregator.observe(_event(RpcAdmissionEventKind.DISPATCHED, wait_seconds=0.1))
    aggregator.observe(
        _event(
            RpcAdmissionEventKind.DISPATCHED,
            source=TelegramRpcSource.REALTIME_EVENT,
            wait_seconds=0.2,
        )
    )

    first_flush = threading.Thread(target=aggregator.flush, kwargs={"now": 300.0})
    first_flush.start()
    assert recorder.first_write_started.wait(timeout=1.0)

    aggregator.observe(
        _event(
            RpcAdmissionEventKind.DISPATCHED,
            source=TelegramRpcSource.REALTIME_EVENT,
            wait_seconds=0.3,
        )
    )
    concurrent_flush = threading.Thread(target=aggregator.flush, kwargs={"now": 300.0})
    concurrent_flush.start()
    recorder.allow_first_write.set()
    first_flush.join(timeout=1.0)
    concurrent_flush.join(timeout=1.0)
    assert not first_flush.is_alive()
    assert not concurrent_flush.is_alive()

    aggregator.flush(now=600.0)
    summaries_by_source: list[tuple[str, int]] = []
    for row in recorder.rows:
        payload = row["payload"]
        assert isinstance(payload, dict)
        source = payload["source"]
        dispatched_count = payload["dispatched_count"]
        assert isinstance(source, str)
        assert isinstance(dispatched_count, int)
        summaries_by_source.append((source, dispatched_count))
    assert summaries_by_source.count((TelegramRpcSource.MCP_INTERACTIVE.value, 1)) == 1
    assert summaries_by_source.count((TelegramRpcSource.REALTIME_EVENT.value, 1)) == 2


def test_demand_evidence_records_final_coordinator_outcomes_and_bounded_dimensions() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)

    aggregator.observe_demand(
        outcome=DemandEvidenceOutcome.SELECTED,
        demand_kind=DemandKind.SCHEDULED_REPAIR,
        acquisition_kind=AcquisitionKind.SCHEDULED_MESSAGES_SNAPSHOT,
        queue_age_seconds=8.0,
        freshness_debt_seconds=3.5,
    )
    aggregator.observe_demand(
        outcome=DemandEvidenceOutcome.COMPLETED,
        demand_kind=DemandKind.SCHEDULED_REPAIR,
        actual_attempts=2,
    )
    aggregator.observe_demand(
        outcome="deferred",
        demand_kind=DemandKind.SCHEDULED_DISCOVERY,
        actual_attempts=1,
        reason="capacity",
    )
    aggregator.observe_demand(
        outcome=DemandEvidenceOutcome.FAILED,
        demand_kind=DemandKind.ENTITY_PROFILE_REFRESH,
        actual_attempts=1,
        reason="x" * 80,
    )
    aggregator.flush(now=300.0)

    assert [row["kind"] for row in recorder.rows] == ["telegram.demand"] * 4
    assert [row["outcome"] for row in recorder.rows] == ["selected", "completed", "deferred", "failed"]
    selected, completed, deferred, failed = recorder.rows
    assert selected["payload"] == {
        "demand_kind": "scheduled_repair",
        "acquisition_kind": "scheduled_messages_snapshot",
        "demand_units": 1,
        "actual_attempts": 0,
        "queue_age_seconds": 8.0,
        "freshness_debt_seconds": 3.5,
        "window_seconds": 300,
    }
    assert completed["payload"] == {
        "demand_kind": "scheduled_repair",
        "demand_units": 1,
        "actual_attempts": 2,
        "window_seconds": 300,
    }
    assert deferred["reason_code"] == "capacity"
    assert deferred["payload"] == {
        "demand_kind": "scheduled_discovery",
        "demand_units": 1,
        "actual_attempts": 1,
        "window_seconds": 300,
    }
    assert failed["reason_code"] == "x" * 64
    assert failed["payload"] == {
        "demand_kind": "entity_profile_refresh",
        "demand_units": 1,
        "actual_attempts": 1,
        "window_seconds": 300,
    }


def test_transport_summary_carries_root_and_acquisition_dimensions() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    event = _event(RpcAdmissionEventKind.DISPATCHED, wait_seconds=0.1)
    event = replace(
        event,
        demand_kind=DemandKind.MCP_REMOTE_ACQUISITION,
        acquisition_kind=AcquisitionKind.MESSAGE_LOOKUP,
    )
    aggregator.observe(event)
    aggregator.flush(now=300.0)

    payload = recorder.rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["demand_kind"] == "mcp_remote_acquisition"
    assert payload["acquisition_kind"] == "message_lookup"
    assert payload["actual_attempts"] == 1


def test_get_full_channel_attempts_aggregate_by_source_and_demand_without_ids() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: 0.0,
    )
    scope = TelegramRpcScope(
        TelegramRpcSource.DIALOG_RESOLUTION,
        RpcServiceClass.INTERACTIVE,
        None,
        None,
        demand_kind=DemandKind.ENTITY_LOOKUP,
        acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
        request_method=_method(functions.channels.GetFullChannelRequest),
    )
    aggregator.observe_rpc_event(scope=scope, phase="write_claim")
    aggregator.observe_rpc_event(scope=scope, phase="write_claim")
    aggregator.flush(now=300.0)

    assert len(recorder.rows) == 2
    assert recorder.rows[0]["kind"] == "telegram.rpc_request"
    assert recorder.rows[0]["result_count"] == 2
    payload = cast(dict[str, object], recorder.rows[0]["payload"])
    assert payload["request_class"] == "get_full_channel"
    assert payload["source"] == "dialog_resolution"
    assert payload["demand_kind"] == "entity_lookup"
    assert payload["actual_attempts"] == payload["write_claim"] == 2
    assert payload["dispatch_semantics"] == "write_claim_v1"
    assert payload["window_seconds"] == 300
    assert "operation_id" not in payload and "target_ref" not in payload


def test_delta_gap_fill_summary_reconciles_and_stays_private_and_bounded() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    aggregator.observe_delta_gap_fill(
        {
            "slice_count": 1,
            "completed": 1,
            "scheduled": 1,
            "actual_attempts": 2,
            "attempted_slices": 1,
            "page_count": 1,
            "fetched_count": 3,
            "new_key_count": 1,
            "preexisting_key_count": 1,
            "uncommitted_unique_count": 0,
            "duplicate_count": 1,
            "terminal_count": 1,
        },
        reason="terminal",
    )
    aggregator.flush(now=300)

    assert len(recorder.rows) == 1
    row = recorder.rows[0]
    assert row["kind"] == "sync.delta_gap_fill"
    assert row["outcome"] == "summary"
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert payload["slice_count"] == payload["completed"] + payload["deferred"] + payload["failed"] == 1
    assert payload["slice_outcome_scope"] == "forward_slice"
    assert payload["fetched_count"] == sum(
        payload[key]
        for key in ("new_key_count", "preexisting_key_count", "uncommitted_unique_count", "duplicate_count")
    )
    assert len(encode_payload(payload).encode()) <= MAX_PAYLOAD_BYTES
    assert "dialog_id" not in payload and "message_id" not in payload and "content" not in payload
    assert payload["reason_counts"] == {"terminal": 1}


def test_empty_delta_gap_fill_window_advances_before_next_active_window() -> None:
    now = 0.0
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: now)

    aggregator.flush(now=300.0)
    now = 600.0
    aggregator.observe_delta_gap_fill({"slice_count": 1, "completed": 1}, reason="completed")

    assert len(recorder.rows) == 1
    payload = recorder.rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["window_seconds"] == 300.0


def test_failed_delta_gap_fill_flush_is_requeued() -> None:
    class _FlakyRecorder(_Recorder):
        failures = 1

        def record(self, **values: object) -> None:
            if values.get("kind") == "sync.delta_gap_fill" and self.failures:
                self.failures -= 1
                raise RuntimeError("offline")
            super().record(**values)

    recorder = _FlakyRecorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    aggregator.observe_delta_gap_fill({"slice_count": 1, "completed": 1}, reason="completed")

    aggregator.flush(now=300)
    assert recorder.rows == []
    aggregator.flush()  # the daemon's shutdown drain uses an unconditional flush
    assert len(recorder.rows) == 1
    assert recorder.rows[0]["payload"]["slice_count"] == 1  # type: ignore[index]


def test_queue_rejected_delta_gap_fill_retries_with_actual_merged_window() -> None:
    now = 0.0

    class _QueueFullRecorder(_Recorder):
        reject_once = True

        def record(self, **values: object) -> bool | None:
            if values.get("kind") == "sync.delta_gap_fill" and self.reject_once:
                self.reject_once = False
                return False
            super().record(**values)
            return True

    recorder = _QueueFullRecorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: now)

    aggregator.observe_delta_gap_fill({"slice_count": 1, "completed": 1}, reason="completed")
    aggregator.flush(now=300.0)
    assert recorder.rows == []

    now = 600.0
    aggregator.observe_delta_gap_fill({"slice_count": 1, "completed": 1}, reason="completed")

    assert len(recorder.rows) == 1
    payload = recorder.rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["slice_count"] == 2
    assert payload["window_seconds"] == 600.0
    assert payload["slice_outcome_scope"] == "forward_slice"


def test_delta_gap_fill_reason_cardinality_is_fixed_and_payload_remains_bounded() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    reasons = (
        "admission_deferred",
        "admission_saturated",
        "admission_expired",
        "flood_wait",
        "access_lost",
        "history_unavailable",
        "cancelled",
        "error",
        "skipped",
        "empty_page",
        "continuation",
        "terminal",
        "completed",
        "private dialog 123",
    )
    for reason in reasons:
        aggregator.observe_delta_gap_fill({"slice_count": 1, "completed": 1}, reason=reason)
    aggregator.flush(now=300)

    payload = recorder.rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["slice_count"] == len(reasons)
    assert payload["reason_counts"]["error"] == 2  # type: ignore[index]
    assert len(payload["reason_counts"]) <= 13  # type: ignore[arg-type]
    assert len(encode_payload(payload).encode()) <= MAX_PAYLOAD_BYTES


def test_invalid_delta_gap_fill_slice_is_reported_without_corrupting_valid_totals() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    aggregator.observe_delta_gap_fill({"slice_count": 1, "completed": 0}, reason="private dialog 123")
    aggregator.flush(now=300)

    assert len(recorder.rows) == 1
    payload = recorder.rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["slice_count"] == payload["completed"] + payload["deferred"] + payload["failed"] == 0
    assert payload["rejected_slice_count"] == 1
    assert recorder.rows[0]["result_count"] == 1
    assert "private dialog 123" not in encode_payload(payload)


def test_rpc_methods_reconstruct_retry_without_confusing_product_units(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: now,
    )
    monkeypatch.setattr("mcp_telegram.rpc_admission_observations.time.monotonic", lambda: now)
    scope = _rpc_scope()
    assert scope.attempt_budget is not None
    for ordinal in (1, 2):
        scope.attempt_budget.debit()
        aggregator.observe_rpc_event(scope=scope, phase="write_claim", admission_sequence=ordinal)
        aggregator.observe_rpc_event(scope=scope, phase="write_return", admission_sequence=ordinal)
        aggregator.observe_rpc_event(scope=scope, phase="known_response", admission_sequence=ordinal)
        if ordinal == 1:
            aggregator.observe_rpc_event(scope=scope, phase="retry", reason="server_transient")
    second = replace(scope, request_method=_method(functions.channels.GetFullChannelRequest))
    scope.attempt_budget.debit()
    aggregator.observe_rpc_event(scope=second, phase="write_claim", admission_sequence=3)
    aggregator.observe_rpc_event(scope=second, phase="write_return", admission_sequence=3)
    aggregator.observe_rpc_event(scope=second, phase="known_response", admission_sequence=3)
    aggregator.observe_demand(outcome="completed", demand_kind=DemandKind.ENTITY_PROFILE_REFRESH, actual_attempts=3)
    aggregator.flush(now=300.0)

    methods = _rpc_payloads(recorder, "method_summary")
    assert len(methods) == 2
    assert sum(cast(int, p["write_claim"]) for p in methods) == 3
    assert sum(cast(int, p["retry"]) for p in methods) == 1
    samples = _rpc_payloads(recorder, "causal_sample")
    assert [p["attempt_ordinal"] for p in samples if p["phase"] == "write_claim"] == [1, 2, 3]
    assert [p["reason"] for p in samples if p["phase"] == "retry"] == ["server_transient"]
    assert all(p["trigger"] == "durable_selection" and p["deadline_remaining_ms"] == 1_000_000 for p in samples)
    assert samples[-1]["budget_remaining"] == 5
    roots = {row["operation_id"] for row in recorder.rows if row["outcome"] == "causal_sample"}
    assert len(roots) == 1
    demand = [row for row in recorder.rows if row["kind"] == "telegram.demand"]
    assert cast(dict[str, object], demand[0]["payload"])["demand_units"] == 1
    assert "private-peer" not in repr(recorder.rows) and "cursor-secret" not in repr(recorder.rows)


def test_rpc_owner_revision_identity_is_stable_private_and_runtime_scoped() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: 0.0,
    )
    first, same, other = _rpc_scope(), _rpc_scope(), _rpc_scope("other-private-peer")
    for scope in (first, same, other):
        aggregator.observe_rpc_event(scope=scope, phase="vendor_cached")
    aggregator.flush(now=300.0)
    rows = [row for row in recorder.rows if row["outcome"] == "causal_sample"]
    assert rows[0]["operation_id"] == rows[1]["operation_id"] != rows[2]["operation_id"]
    target = cast(dict[str, object], rows[0]["payload"])["target_ref"]
    recorder.rows.clear()
    aggregator.observe_rpc_event(scope=_rpc_scope(revision="new-revision"), phase="rejected", reason="deadline_elapsed")
    aggregator.flush(now=600.0)
    changed = [row for row in recorder.rows if row["outcome"] == "causal_sample"][0]
    assert changed["operation_id"] != rows[0]["operation_id"]
    assert cast(dict[str, object], changed["payload"])["target_ref"] == target
    fresh = RpcAdmissionObservationAggregator(
        _Recorder(), method_names=TelegramRpcGate.rpc_observation_methods(), policy=RuntimeObservationConfig()
    )
    assert fresh._rpc_identity(first) != aggregator._rpc_identity(first)
    assert all(p["write_claim"] == p["write_return"] == 0 for p in _rpc_payloads(recorder, "method_summary"))


def test_rpc_method_and_sample_limits_preserve_totals_and_declare_bias() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: 0.0,
    )
    methods = [_method(kind) for kind in tlobjects.values() if issubclass(kind, TLRequest)][:20]
    roots = [_rpc_scope(str(number)) for number in range(3)]
    for index in range(60):
        aggregator.observe_rpc_event(
            scope=replace(roots[index % 3], request_method=methods[index % len(methods)]), phase="write_claim"
        )
    assert len(aggregator._method_aggregates) == 16
    assert len(aggregator._rpc_samples) == 2
    assert all(len(events) == 12 for events in aggregator._rpc_samples.values())
    aggregator.flush(now=300.0)
    assert len(recorder.rows) == 42
    assert sum(cast(int, p["write_claim"]) for p in _rpc_payloads(recorder, "method_summary")) == 60
    coverage = _rpc_payloads(recorder, "coverage")[0]
    assert coverage["overflow_events"] == 12
    assert coverage["method_attribution_complete"] is False
    assert coverage["unsampled_events"] == 20
    assert coverage["truncated_events"] == 16
    assert coverage["sampling"] == "first_roots_partial"
    assert all(
        len(encode_payload(cast(dict[str, object], row["payload"])).encode()) <= MAX_PAYLOAD_BYTES
        for row in recorder.rows
    )
    aggregator.flush(now=900.0)
    assert len(recorder.rows) == 42


@pytest.mark.parametrize("raises", [False, True])
def test_rpc_sink_rejection_then_ten_minute_delay_keeps_bounded_real_window(raises: bool) -> None:
    class RejectingRecorder(_Recorder):
        reject = True

        def record(self, **values: object) -> bool:
            if self.reject:
                if raises:
                    raise RuntimeError("private sink error")
                return False
            self.rows.append(values)
            return True

    now = 0.0
    recorder = RejectingRecorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: now,
    )
    scope = _rpc_scope()
    aggregator.observe_rpc_event(scope=scope, phase="write_claim")
    aggregator.flush(now=300.0)
    assert len(aggregator._method_aggregates) == 1 and aggregator._rpc_samples == {}
    recorder.reject = False
    now = 900.0
    aggregator.observe_rpc_event(scope=scope, phase="write_return")
    summaries = _rpc_payloads(recorder, "method_summary")
    assert len(summaries) == 1
    assert summaries[0]["write_claim"] == summaries[0]["write_return"] == 1
    assert summaries[0]["window_seconds"] == 900.0
    coverage = _rpc_payloads(recorder, "coverage")
    assert len(coverage) == 1 and coverage[0]["lost_rows"] == 2
    assert coverage[0]["window_seconds"] == 600.0
    assert coverage[0]["configured_window_seconds"] == 300.0
    assert len(_rpc_payloads(recorder, "causal_sample")) == 1


def test_rpc_untrusted_dimensions_are_redacted_and_samples_fit_payload_cap() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: 0.0,
    )
    private = "private-value-do-not-log"
    scope = replace(_rpc_scope(private, private), request_method=private, owned_attempt_id=private)
    aggregator.observe_rpc_event(scope=scope, phase="retry", reason=private)
    aggregator.flush(now=300.0)
    assert private not in repr(recorder.rows)
    sample = _rpc_payloads(recorder, "causal_sample")[0]
    assert sample["method"] == sample["reason"] == "unknown"
    assert "attempt_ref" not in sample
    assert len(encode_payload(sample).encode()) <= MAX_PAYLOAD_BYTES


def test_rpc_scheduler_rejection_and_retry_keep_scope_and_zero_physical_attempts() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: 0.0,
    )
    scope = _rpc_scope()
    for kind, reason in (
        (RpcAdmissionEventKind.REJECTED, "attempt_budget_exhausted"),
        (RpcAdmissionEventKind.RESUBMITTED, "server_transient"),
    ):
        aggregator.observe(replace(_event(kind), observation_scope=scope, reason=reason))
    aggregator.flush(now=300.0)
    summary = _rpc_payloads(recorder, "method_summary")[0]
    assert summary["write_claim"] == summary["write_return"] == 0
    assert summary["rejected"] == summary["retry"] == 1
    assert all(p["root_attempts"] == 0 and "attempt_ordinal" not in p for p in _rpc_payloads(recorder, "causal_sample"))


def test_rpc_maximum_sample_representation_is_finite_and_bounded() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: 0.0,
    )
    method = max((_method(kind) for kind in tlobjects.values() if issubclass(kind, TLRequest)), key=len)
    original = _rpc_scope()
    assert original.attempt_budget is not None
    scope = replace(
        original,
        request_method=method,
        deadline=1e308,
        source=max(TelegramRpcSource, key=lambda value: len(value.value)),
        demand_kind=max(DemandKind, key=lambda value: len(value.value)),
        acquisition_kind=max(AcquisitionKind, key=lambda value: len(value.value)),
        attempt_budget=RpcAttemptBudget(2**63 - 1, 2**63 - 2, selected=original.attempt_budget.selected),
    )
    aggregator.observe_rpc_event(
        scope=scope,
        phase="write_claim",
        admission_sequence=2**63 - 1,
        replay_ordinal=2**63 - 1,
        reason="attempt_budget_exhausted",
    )
    aggregator.flush(now=300.0)
    assert len(_rpc_payloads(recorder, "causal_sample")) == 1
    sample = _rpc_payloads(recorder, "causal_sample")[0]
    assert sample["payload_truncated"] is True and "deadline_remaining_ms" not in sample
    assert all(
        len(encode_payload(cast(dict[str, object], row["payload"])).encode()) <= MAX_PAYLOAD_BYTES
        for row in recorder.rows
    )
    assert all("Infinity" not in encode_payload(cast(dict[str, object], row["payload"])) for row in recorder.rows)
