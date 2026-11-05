import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions
from telethon.tl.functions.updates import GetStateRequest

from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator, work_references
from mcp_telegram.runtime_observations import RuntimeObservationSink, _ObservationJob
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, load_rpc_safety_record
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    RpcAttemptEvidence,
)
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from mcp_telegram.telegram_rpc import TelegramRpcGate, _TransportBoundaryState
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    BudgetRejection,
    BudgetRejectionBoundary,
    ResendCause,
    RpcServiceClass,
    TelegramRpcScope,
    TelegramRpcSource,
)
from tests.test_telegram_rpc_wire_integration import _WireHarness, wire  # noqa: F401 - shared transport fixture

METHOD = (
    f"{functions.users.GetUsersRequest.__module__}.GetUsersRequest:{functions.users.GetUsersRequest.CONSTRUCTOR_ID:08x}"
)


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.reject = False

    def record(self, **row: object) -> bool:
        if self.reject:
            return False
        self.rows.append(row)
        return True


def test_budget_rejection_survives_sampling_and_has_stable_private_refs() -> None:
    recorder = Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder, policy=RuntimeObservationConfig(), method_names=frozenset({METHOD})
    )
    selected = WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL, 42, "dialog:private"), "private-revision")
    scope = TelegramRpcScope(
        TelegramRpcSource.DELTA_SYNC,
        RpcServiceClass.LIVE_SYNC,
        None,
        None,
        demand_kind=DemandKind.DELTA_GAP_FILL,
        acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE,
        attempt_budget=RpcAttemptBudget(1, attempts=1, selected=selected),
        owned_attempt_id="a" * 32,
    )
    for number in range(3):
        sampled = WorkRevision(selected.key, f"other-{number}")
        observer.observe_rpc_event(
            scope=replace(scope, attempt_budget=RpcAttemptBudget(1, selected=sampled)), phase="write_claim"
        )
    rejection = BudgetRejection(
        scope=scope,
        method=METHOD,
        boundary=BudgetRejectionBoundary.OWNER_PREPARE,
        enqueue_trace=((ResendCause.INITIAL, None), (ResendCause.BAD_SERVER_SALT, None)),
        enqueue_truncated=0,
        claimed_count=1,
        first_claimed_at_ms=123,
        owner_attempt_id="a" * 32,
        attempted_ordinal=2,
        budget_limit=1,
        budget_used=1,
        budget_remaining=0,
        write_returned=True,
        last_method=METHOD,
    )
    observer.observe_rpc_event(scope=scope, phase="budget_rejected", request_method=METHOD, rejection=rejection)
    observer.flush()
    payload = cast(
        dict[str, object], next(row for row in recorder.rows if row["outcome"] == "budget_rejected")["payload"]
    )
    assert (payload["work_ref"], payload["selected_ref"]) == work_references(selected)
    assert payload["enqueue_trace"] == [{"cause": "initial"}, {"cause": "bad_server_salt"}]
    assert payload["boundary"] == "existing_owner_prepare" and payload["disposition"] == "blocked_before_write"
    assert payload["write_returned"] is True and "response_received" not in payload
    assert len(json.dumps(payload).encode()) <= 1024 and "private" not in repr(payload)
    assert work_references(selected) == work_references(WorkRevision(selected.key, selected.revision))
    assert (
        work_references(selected)[0]
        != work_references(WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL, 43, "dialog:private"), selected.revision))[0]
    )
    assert work_references(selected)[1] != work_references(WorkRevision(selected.key, "another"))[1]
    command = (
        "from mcp_telegram.rpc_admission_observations import work_references;"
        "from mcp_telegram.telegram_demand_progress import WorkKey,WorkRevision;"
        "from mcp_telegram.telegram_rpc_consumers import DemandKind;"
        "print(*work_references(WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL,42,'dialog:private'),'private-revision')))"
    )
    result = subprocess.run([sys.executable, "-c", command], check=True, capture_output=True, text=True)
    assert tuple(result.stdout.split()) == work_references(selected)

    long_method = "m" * 250
    wide = RpcAdmissionObservationAggregator(
        recorder, policy=RuntimeObservationConfig(), method_names=frozenset({long_method})
    )
    wide_rejection = replace(
        rejection,
        method=long_method,
        last_method=long_method,
        enqueue_trace=(
            *((ResendCause.BAD_MSG_NOTIFICATION, 16) for _ in range(7)),
            (ResendCause.BAD_SERVER_SALT, None),
        ),
        enqueue_truncated=2,
    )
    wide.observe_rpc_event(scope=scope, phase="budget_rejected", rejection=wide_rejection)
    projected = cast(
        dict[str, object], [row for row in recorder.rows if row["outcome"] == "budget_rejected"][-1]["payload"]
    )
    assert len(json.dumps(projected, separators=(",", ":")).encode()) <= 1024
    assert cast(list[dict[str, object]], projected["enqueue_trace"])[-1] == {"cause": "bad_server_salt"}
    assert projected["enqueue_truncated"] == 6


def test_invalid_or_lost_receipts_never_claim_committed_progress() -> None:
    recorder = Recorder()
    observer = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), method_names=frozenset())
    selected = WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL, 42, "private-target"), "private-cursor")
    wrong = WorkRevision(selected.key, "different")
    observer.observe_slice_receipt(
        selected=selected,
        status="outcome",
        outcome=SliceOutcome(wrong, SliceOutcomeKind.PROGRESSED, "committed"),
        actual_attempts=1,
        retry_not_before_monotonic=None,
    )
    receipt = next(row for row in recorder.rows if row["outcome"] == "slice_receipt")
    assert receipt["reason_code"] == "invalid_slice_outcome"
    assert "committed_ref" not in cast(dict[str, object], receipt["payload"])
    assert "private" not in repr(receipt)
    recorder.reject = True
    observer.observe_slice_receipt(
        selected=selected, status="deferred", outcome=None, actual_attempts=1, retry_not_before_monotonic=None
    )
    recorder.reject = False
    observer.flush()
    coverage = next(row for row in recorder.rows if row["outcome"] == "coverage")
    assert cast(dict[str, object], coverage["payload"])["lost_rows"] == 1


def test_unsampled_incident_has_canonical_correlation_and_no_private_data() -> None:
    recorder = Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder, policy=RuntimeObservationConfig(), method_names=frozenset({METHOD})
    )
    scope = TelegramRpcScope(
        TelegramRpcSource.ENTITY_INFO_REFRESH,
        RpcServiceClass.BACKGROUND,
        None,
        None,
        demand_kind=DemandKind.ENTITY_PROFILE_REFRESH,
        acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
        attempt_evidence=RpcAttemptEvidence(),
        request_method=METHOD,
        owned_attempt_id="a" * 32,
    )
    # Fill ordinary root samples before this attempt becomes unknown.
    for _ in range(3):
        observer.observe_rpc_event(scope=replace(scope, attempt_evidence=RpcAttemptEvidence()), phase="write_claim")
    observer.observe_rpc_event(
        scope=scope,
        phase="incident_unknown",
        request_method=METHOD,
        incident_run="b" * 32,
        incident_claimed_at_ms=123,
        incident_settled_at_ms=456,
        incident_cause="private request body",
    )
    observer.flush()
    incidents = [row for row in recorder.rows if row["outcome"] == "incident_unknown"]
    assert len(incidents) == 1
    payload = incidents[0]["payload"]
    assert payload == {
        "attempt_id": "a" * 32,
        "run": "b" * 32,
        "method": METHOD,
        "source": scope.source.value,
        "service_class": scope.service_class.value,
        "claim_at_ms": 123,
        "settled_at_ms": 456,
        "termination": "unspecified",
        "demand_kind": DemandKind.ENTITY_PROFILE_REFRESH.value,
        "acquisition_kind": AcquisitionKind.ENTITY_LOOKUP.value,
    }
    assert incidents[0]["observed_at_ms"] == 456
    assert "private" not in repr(recorder.rows)
    coverage = next(cast(dict[str, object], row["payload"]) for row in recorder.rows if row["outcome"] == "coverage")
    assert cast(int, coverage["unsampled_events"]) > 0


def test_incident_rejection_and_invalid_legacy_metadata_report_loss() -> None:
    recorder = Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder, policy=RuntimeObservationConfig(), method_names=frozenset({METHOD})
    )
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE, RpcServiceClass.INTERACTIVE, None, None, owned_attempt_id="a" * 32
    )
    recorder.reject = True
    observer.observe_rpc_event(
        scope=scope,
        phase="incident_unknown",
        request_method=METHOD,
        incident_run="b" * 32,
        incident_claimed_at_ms=1,
        incident_settled_at_ms=2,
        incident_cause="shutdown",
    )
    recorder.reject = False
    observer.observe_rpc_event(
        scope=scope,
        phase="incident_unknown",
        request_method=METHOD,
        incident_run="b" * 32,
        incident_claimed_at_ms=None,
        incident_settled_at_ms=2,
    )
    observer.flush()
    coverage = next(row for row in recorder.rows if row["outcome"] == "coverage")
    assert cast(dict[str, object], coverage["payload"])["lost_rows"] == 2
    assert not any(row["outcome"] == "incident_unknown" for row in recorder.rows)


@pytest.mark.parametrize(
    "case",
    [
        (
            "terminal_settlement",
            "admission_expired",
            "admission_expired",
            "terminal_settlement",
            "admission_expired",
            "admission_expired",
        ),
        ("secret stage", "private exception body", "private reason", "unknown", "other_exception", None),
    ],
)
def test_incident_terminal_fields_are_allowlisted(case: tuple[str, str, str, str, str, str | None]) -> None:
    stage, error_type, reason, expected_stage, expected_error, expected_reason = case
    recorder = Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder, policy=RuntimeObservationConfig(), method_names=frozenset({METHOD})
    )
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE, RpcServiceClass.INTERACTIVE, None, None, owned_attempt_id="a" * 32
    )
    observer.observe_rpc_event(
        scope=scope,
        phase="incident_unknown",
        request_method=METHOD,
        incident_run="b" * 32,
        incident_claimed_at_ms=1,
        incident_settled_at_ms=2,
        incident_terminal_stage=stage,
        incident_error_type=error_type,
        incident_terminal_reason=reason,
        incident_owner_attempt="a" * 32,
    )
    observer.flush()
    row = next(row for row in recorder.rows if row["outcome"] == "incident_unknown")
    payload = cast(dict[str, object], row["payload"])
    assert payload["terminal_stage"] == expected_stage and payload["error_type"] == expected_error
    assert payload["owner_attempt_id"] == "a" * 32
    assert payload.get("terminal_reason") == expected_reason
    assert "private" not in repr(payload) and "secret" not in repr(payload)


async def test_claimed_unknown_is_recorded_once_with_owner_identity(wire: _WireHarness) -> None:  # noqa: F811
    gate, sender, connection, _ = wire
    recorder = Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder, policy=RuntimeObservationConfig(), method_names=TelegramRpcGate.rpc_observation_methods()
    )
    gate.set_rpc_request_observer(observer.observe_rpc_event)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(2),
        attempt_evidence=RpcAttemptEvidence(),
    )
    result = sender.send_owned(GetStateRequest(), scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    owner = gate._require_wire_owner(packet.state.owner)
    assert sender.write_packet(packet, connection._send)
    state.future.cancel()
    try:
        await result
    except asyncio.CancelledError:
        pass
    await asyncio.gather(*sender._observing)
    await gate.observe_result(owner, asyncio.CancelledError())
    incidents = [row for row in recorder.rows if row["outcome"] == "incident_unknown"]
    assert len(incidents) == 1
    payload = cast(dict[str, object], incidents[0]["payload"])
    assert payload["attempt_id"] == owner.attempt_id
    assert payload["owner_attempt_id"] == owner.attempt_id
    assert payload["terminal_stage"] == "terminal_settlement"
    assert payload["error_type"] == "cancelled"
    assert "disposition" not in payload and "boundary" not in payload and "response_received" not in payload
    assert payload["run"] == gate._ensure_safety_ledger()._run
    assert payload["claim_at_ms"] == owner.incident_claimed_at_ms
    assert cast(int, payload["settled_at_ms"]) >= cast(int, payload["claim_at_ms"])


async def test_real_vendor_rejection_persists_unsampled_work_correlation(wire: _WireHarness, tmp_path: Path) -> None:  # noqa: F811, PLR0914, PLR0915
    from telethon.tl import types
    from telethon.tl.core.tlmessage import TLMessage

    gate, sender, connection, _ = wire
    path = tmp_path / "observations.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    observer = RpcAdmissionObservationAggregator(
        sink, policy=RuntimeObservationConfig(), method_names=gate.rpc_observation_methods()
    )
    gate.set_rpc_request_observer(observer.observe_rpc_event)
    selected = WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL, 42, "dialog:private"), "private-revision")
    budget = RpcAttemptBudget(1, selected=selected)
    scope = TelegramRpcScope(
        TelegramRpcSource.DELTA_SYNC,
        RpcServiceClass.LIVE_SYNC,
        time.monotonic() + 10,
        asyncio.current_task(),
        demand_kind=DemandKind.DELTA_GAP_FILL,
        acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE,
        attempt_budget=budget,
        attempt_evidence=RpcAttemptEvidence(),
    )
    for number in range(3):
        sample = replace(scope, attempt_budget=RpcAttemptBudget(1, selected=WorkRevision(selected.key, str(number))))
        observer.observe_rpc_event(scope=sample, phase="write_claim")
    request = functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0)
    try:
        result = sender.send_owned(request, scope)
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        assert state.msg_id is not None
        await sender._handle_bad_server_salt(TLMessage(99, 0, types.BadServerSalt(state.msg_id, 1, 48, 9)))
        assert await sender._send_queue.next_state() is state
        with pytest.raises(RpcAttemptBudgetExhaustedError):
            await sender._prepare_state(state)
        await asyncio.gather(*sender._observing)
        with pytest.raises(RpcAttemptBudgetExhaustedError):
            await result
        observer.flush()
    finally:
        await sink.aclose()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as db:
        rows = cast(
            list[tuple[str]],
            db.execute("SELECT payload_json FROM runtime_observations WHERE outcome='budget_rejected'").fetchall(),
        )
        assert len(rows) == 1
        payload = cast(dict[str, object], json.loads(rows[0][0]))
        assert (payload["work_ref"], payload["selected_ref"]) == work_references(selected)
        assert cast(list[dict[str, object]], payload["enqueue_trace"])[-1]["cause"] == "bad_server_salt"
        assert payload["claimed_count"] == 1 and payload["disposition"] == "blocked_before_write"
        assert "private" not in repr(payload)
        incident_rows = cast(
            list[tuple[str]],
            db.execute("SELECT payload_json FROM runtime_observations WHERE outcome='incident_unknown'").fetchall(),
        )
        assert len(incident_rows) == 1
        incident = cast(dict[str, object], json.loads(incident_rows[0][0]))
        assert (incident["work_ref"], incident["selected_ref"]) == work_references(selected)
        assert incident["boundary"] == payload["boundary"]


async def test_claimed_owner_reports_typed_budget_failure_without_exception_text(wire: _WireHarness) -> None:  # noqa: F811
    gate, sender, connection, _ = wire
    recorder = Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder, policy=RuntimeObservationConfig(), method_names=TelegramRpcGate.rpc_observation_methods()
    )
    gate.set_rpc_request_observer(observer.observe_rpc_event)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(2),
        attempt_evidence=RpcAttemptEvidence(),
    )
    result = sender.send_owned(GetStateRequest(), scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    owner = gate._require_wire_owner(packet.state.owner)
    assert sender.write_packet(packet, connection._send)
    state.future.set_exception(RpcAttemptBudgetExhaustedError("private exception text"))
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await result
    await asyncio.gather(*sender._observing)
    incidents = [row for row in recorder.rows if row["outcome"] == "incident_unknown"]
    assert len(incidents) == 1
    payload = cast(dict[str, object], incidents[0]["payload"])
    assert payload["owner_attempt_id"] == owner.attempt_id
    assert payload["terminal_stage"] == "terminal_settlement"
    assert payload["error_type"] == payload["terminal_reason"] == "attempt_budget_exhausted"
    assert "private" not in repr(payload) and "replay" not in repr(payload)


async def test_unsampled_late_shutdown_incident_survives_sink_drain_and_reopen(  # noqa: PLR0914, PLR0915
    wire: _WireHarness,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender, connection, _ = wire
    path = tmp_path / "observations.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    started, release = threading.Event(), threading.Event()
    original = RuntimeObservationSink._write_batch

    def held_batch(conn: sqlite3.Connection, jobs: list[_ObservationJob]) -> None:
        started.set()
        assert release.wait(timeout=5)
        original(conn, jobs)

    monkeypatch.setattr(RuntimeObservationSink, "_write_batch", staticmethod(held_batch))
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    observer = RpcAdmissionObservationAggregator(
        sink, policy=RuntimeObservationConfig(), method_names=TelegramRpcGate.rpc_observation_methods()
    )
    gate.set_rpc_request_observer(observer.observe_rpc_event)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(2),
        attempt_evidence=RpcAttemptEvidence(),
    )
    try:
        for _ in range(2):
            observer.observe_rpc_event(scope=replace(scope, attempt_evidence=RpcAttemptEvidence()), phase="write_claim")
        result = sender.send_owned(GetStateRequest(), scope)
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        packet = connection._send_queue.get_nowait()
        owner = gate._require_wire_owner(packet.state.owner)
        assert sender.write_packet(packet, connection._send)
        gate._transport_state = _TransportBoundaryState.DISCONNECTING
        state.future.cancel()
        with pytest.raises(asyncio.CancelledError):
            await result
        await asyncio.gather(*sender._observing)
        await gate.observe_result(owner, asyncio.CancelledError())
        observer.flush()
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        closing_task = asyncio.create_task(sink.aclose())
        await asyncio.sleep(0.05)
        assert not closing_task.done()
        release.set()
        await closing_task
        await writer.aclose()
        with closing(sqlite3.connect(path)) as reopened:
            rows = cast(
                list[tuple[str, str, int, str]],
                reopened.execute(
                    "SELECT outcome, operation_id, observed_at_ms, payload_json FROM runtime_observations "
                    "WHERE kind = 'telegram.rpc_request'"
                ).fetchall(),
            )
        incident = [row for row in rows if row[0] == "incident_unknown"]
        assert len(incident) == 1
        payload = cast(dict[str, object], json.loads(incident[0][3]))
        assert incident[0][1] == payload["attempt_id"] == owner.attempt_id
        assert payload["run"] == gate._ensure_safety_ledger()._run
        assert payload["method"] == owner.method
        assert payload["claim_at_ms"] == owner.incident_claimed_at_ms
        assert incident[0][2] == payload["settled_at_ms"]
        assert payload["termination"] == "shutdown"
        with closing(sqlite3.connect(tmp_path / "sync.db")) as safety_db:
            safety_json = load_rpc_safety_record(safety_db)
        assert safety_json is not None
        safety = cast(dict[str, object], json.loads(safety_json))
        intents = cast(dict[str, dict[str, object]], safety["intents"])
        intent = intents[owner.attempt_id]
        assert (intent["run"], intent["method"], intent["phase"]) == (payload["run"], payload["method"], "unknown")
        coverage = next(cast(dict[str, object], json.loads(row[3])) for row in rows if row[0] == "coverage")
        assert cast(int, coverage["unsampled_events"]) > 0
        assert "private" not in repr(rows)
    finally:
        release.set()
        await sink.aclose()
        await writer.aclose()


async def test_sink_writer_failure_is_counted_without_exposing_private_error(
    wire: _WireHarness,  # noqa: F811
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender, connection, _ = wire
    path = tmp_path / "observations.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def fail(_conn: sqlite3.Connection, jobs: list[object]) -> None:
        assert jobs
        raise ValueError("private error and peer")

    monkeypatch.setattr(RuntimeObservationSink, "_write_batch", staticmethod(fail))
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    observer = RpcAdmissionObservationAggregator(
        sink, policy=RuntimeObservationConfig(), method_names=TelegramRpcGate.rpc_observation_methods()
    )
    gate.set_rpc_request_observer(observer.observe_rpc_event)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(2),
        attempt_evidence=RpcAttemptEvidence(),
    )
    result = sender.send_owned(GetStateRequest(), scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    owner = gate._require_wire_owner(packet.state.owner)
    assert sender.write_packet(packet, connection._send)
    state.future.cancel()
    with pytest.raises(asyncio.CancelledError):
        await result
    await asyncio.gather(*sender._observing)
    await sink.aclose()
    await writer.aclose()
    assert sink.counters.permanent_failures == 1
    assert sink.counters.successful_writes == 0
    assert "private error and peer" not in caplog.text
    intents = cast(dict[str, dict[str, object]], gate._ensure_safety_ledger()._record["intents"])
    assert intents[owner.attempt_id]["phase"] == "unknown"
