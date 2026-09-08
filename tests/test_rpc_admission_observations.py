from __future__ import annotations

from dataclasses import dataclass, field

from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    RpcServiceClass,
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
        service_class=RpcServiceClass.INTERACTIVE,
        queue_depth=2,
        total_depth=3,
        active_depth=1,
        total_outstanding=4,
        wait_seconds=wait_seconds,
    )


def test_routine_admissions_are_coalesced_into_one_source_summary() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, summary_interval_seconds=300, clock=lambda: 0.0)

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
    aggregator = RpcAdmissionObservationAggregator(recorder, summary_interval_seconds=300, clock=lambda: 0.0)

    aggregator.observe(_event(RpcAdmissionEventKind.EXPIRED, wait_seconds=15.0))

    assert recorder.rows[0]["outcome"] == "expired"
    assert recorder.rows[0]["duration_ms"] == 15_000.0
    payload = recorder.rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["source"] == "mcp_interactive"


def test_due_event_flushes_completed_window() -> None:
    now = 0.0
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, summary_interval_seconds=5, clock=lambda: now)
    aggregator.observe(_event(RpcAdmissionEventKind.QUEUED))
    now = 5.0

    aggregator.observe(_event(RpcAdmissionEventKind.DISPATCHED, wait_seconds=0.2))

    assert len(recorder.rows) == 1
    assert recorder.rows[0]["result_count"] == 1


def test_recorder_failure_never_escapes_scheduler_callback() -> None:
    class _FailingRecorder:
        def record(self, **_values: object) -> None:
            raise RuntimeError("offline")

    aggregator = RpcAdmissionObservationAggregator(_FailingRecorder(), summary_interval_seconds=1, clock=lambda: 0.0)
    aggregator.observe(_event(RpcAdmissionEventKind.CANCELLED))
