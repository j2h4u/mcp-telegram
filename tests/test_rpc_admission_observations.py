from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace

from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.rpc_admission_observations import DemandEvidenceOutcome, RpcAdmissionObservationAggregator
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

    def record(self, **values: object) -> None:
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
