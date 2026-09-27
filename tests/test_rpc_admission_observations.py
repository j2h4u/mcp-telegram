from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path

from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.flood import FloodWaitObservation
from mcp_telegram.rpc_admission_observations import DemandEvidenceOutcome, RpcAdmissionObservationAggregator
from mcp_telegram.runtime_observations import MAX_PAYLOAD_BYTES, RuntimeObservationSink, encode_payload
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import AcquisitionKind
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    RPC_SOURCE_SERVICE_CLASS,
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    TelegramRpcSource,
)


@dataclass
class _Recorder:
    rows: list[dict[str, object]] = field(default_factory=list)

    def record(self, **values: object) -> bool | None:
        self.rows.append(values)


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
    assert row["payload"] == {
        "source": "mcp_interactive",
        "service_class": "interactive",
        "queued_count": 2,
        "dispatched_count": 2,
        "max_wait_ms": 300.0,
        "queue_depth_max": 2,
        "total_depth_max": 3,
        "active_depth_max": 1,
        "total_outstanding_max": 4,
        "window_seconds": 300,
    }


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
            source=TelegramRpcSource.MESSAGE_READ_FALLBACK,
            service_class=RPC_SOURCE_SERVICE_CLASS[TelegramRpcSource.MESSAGE_READ_FALLBACK],
            demand_kind=DemandKind.MESSAGE_READ_FALLBACK,
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
        "source": "message_read_fallback",
        "service_class": "interactive",
        "request_method": "GetHistoryRequest",
        "origin": "vendor_cache",
        "actual_dispatch": False,
        "cooldown_until_utc_ms": 1_700_000_023_000,
        "circuit_open": False,
        "demand_kind": "message_read_fallback",
        "acquisition_kind": "message_history_page",
    }


def test_flood_wait_observation_persists_through_runtime_sink(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    sink = RuntimeObservationSink(path, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    aggregator = RpcAdmissionObservationAggregator(sink, policy=RuntimeObservationConfig(), clock=lambda: 0.0)

    aggregator.observe_flood_wait(
        FloodWaitObservation(
            source=TelegramRpcSource.MESSAGE_READ_FALLBACK,
            service_class=RPC_SOURCE_SERVICE_CLASS[TelegramRpcSource.MESSAGE_READ_FALLBACK],
            demand_kind=DemandKind.MESSAGE_READ_FALLBACK,
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
    sink.close()

    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT kind, outcome, reason_code, duration_ms, observed_at_ms, payload_json "
            "FROM runtime_observations WHERE kind = 'telegram.rpc_admission'"
        ).fetchone()
    assert row is not None
    assert row[:5] == ("telegram.rpc_admission", "flood_wait", "actual_send", 23_000, 1_700_000_000_000)
    assert json.loads(row[5]) == {
        "source": "message_read_fallback",
        "service_class": "interactive",
        "request_method": "GetHistoryRequest",
        "origin": "actual_send",
        "actual_dispatch": True,
        "cooldown_until_utc_ms": 1_700_000_023_000,
        "circuit_open": True,
        "demand_kind": "message_read_fallback",
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
    assert [row["payload"] for row in recorder.rows] == [
        {
            "source": "realtime_event",
            "service_class": "live_sync",
            "queued_count": 0,
            "dispatched_count": 1,
            "max_wait_ms": 200.0,
            "queue_depth_max": 2,
            "total_depth_max": 3,
            "active_depth_max": 1,
            "total_outstanding_max": 4,
            "window_seconds": 300.0,
        }
    ]

    aggregator.flush(now=600.0)
    assert len(recorder.rows) == 2
    retried_payload = recorder.rows[1]["payload"]
    assert isinstance(retried_payload, dict)
    assert retried_payload["source"] == "mcp_interactive"


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
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    dimensions = {
        "request_class": "get_full_channel",
        "source": TelegramRpcSource.DIALOG_RESOLUTION,
        "service_class": RPC_SOURCE_SERVICE_CLASS[TelegramRpcSource.DIALOG_RESOLUTION],
        "demand_kind": DemandKind.ENTITY_LOOKUP,
        "acquisition_kind": AcquisitionKind.ENTITY_LOOKUP,
    }

    aggregator.observe_request_attempt(**dimensions)
    aggregator.observe_request_attempt(**dimensions)
    aggregator.flush(now=300.0)

    assert len(recorder.rows) == 1
    assert recorder.rows[0]["kind"] == "telegram.rpc_request"
    assert recorder.rows[0]["result_count"] == 2
    assert recorder.rows[0]["payload"] == {
        "request_class": "get_full_channel",
        "source": "dialog_resolution",
        "service_class": "interactive",
        "demand_kind": "entity_lookup",
        "acquisition_kind": "entity_lookup",
        "actual_attempts": 2,
        "window_seconds": 300,
    }


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
