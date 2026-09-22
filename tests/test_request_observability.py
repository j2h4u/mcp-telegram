from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import mcp_telegram.request_timing as request_timing_module
from mcp_telegram.correlation import correlation_context, current_operation_id
from mcp_telegram.daemon_api import DaemonAPIServer, _insert_telemetry_row, _normalize_telemetry_event, _served_source
from mcp_telegram.daemon_client import DaemonConnection
from mcp_telegram.operator_summary import _timing_contributor
from mcp_telegram.request_timing import (
    TIMING_PHASES,
    TIMING_REQUIRED_PHASES_BY_ROUTE,
    DaemonRequestTiming,
    current_timing,
    is_valid_request_id,
    standalone_operation_id,
    standalone_request_id,
    timing_context,
    timing_phase,
)
from mcp_telegram.runtime_observations import RuntimeObservationSink
from mcp_telegram.sync_db import _RUNTIME_OBSERVATIONS_V54_DDL
from mcp_telegram.telegram_rpc import TelegramRpcGate, _AdmissionAwareSender
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmission,
    RpcServiceClass,
    TelegramRpcScope,
    TelegramRpcSource,
)
from tests.test_daemon_api import (
    _insert_message,
    _insert_synced_dialog,
    _insert_topic_metadata,
    _make_db,
    _TestClient,
    make_server,
)


def test_timing_payload_keeps_missing_boundaries_unavailable_and_is_private() -> None:
    timing = DaemonRequestTiming(operation_id="op-1")
    timing.set_route("local_history")
    timing.add_phase("local_projection", 12.5)
    timing.record_rpc_attempt()

    payload = timing.payload()

    assert payload["route_attempted"] == "local_history"
    assert payload["local_projection_ms"] == 12.5
    assert payload["rpc_admission_ms"] is None
    assert payload["rpc_execution_ms"] is None
    assert payload["rpc_attempts"] == 1
    assert payload["fallback_attempted"] is False
    assert payload["attempts_capped"] is False
    assert not {"arguments", "message_text", "peer_id", "message_id", "cursor", "sql", "response"} & payload.keys()
    assert {key.removesuffix("_ms") for key in payload if key.endswith("_ms") and key != "unattributed_ms"} <= set(
        TIMING_PHASES
    )


def test_timing_phase_isolated_and_context_is_reset() -> None:
    with timing_context("op-2") as timing:
        assert current_timing() is timing
        assert timing is not None
        with timing_phase("rpc_admission"):
            timing.add_phase("rpc_admission", 3.0)
        with timing_phase("rpc_execution"):
            timing.add_phase("rpc_execution", 7.0)
        timing.set_route("telegram_fallback")
        timing.mark_fallback()
        payload = timing.payload()
    assert current_timing() is None
    assert cast(float, payload["rpc_admission_ms"]) >= 3.0
    assert cast(float, payload["rpc_execution_ms"]) >= 7.0
    assert payload["fallback_attempted"] is True
    assert payload["route_attempted"] == "telegram_fallback"


def test_timing_phase_retains_closed_nested_parent_metadata() -> None:
    with timing_context("nested") as timing:
        assert timing is not None
        with timing_phase("resolution"):
            with timing_phase("rpc_execution"):
                pass
        payload = timing.payload()

    nested = cast(dict[str, object], payload["nested_phases"])
    resolution = cast(dict[str, object], nested["resolution"])
    assert set(resolution) == {"rpc_execution_ms"}
    assert cast(float, resolution["rpc_execution_ms"]) >= 0


def test_response_shape_boundary_captures_controlled_shaping_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter((0.0, 1.0, 2.0, 12.0, 13.0))
    monkeypatch.setattr(request_timing_module.time, "monotonic", lambda: next(clock))
    with timing_context("response-shape") as timing:
        assert timing is not None
        timing.set_route("local_history")
        with timing_phase("local_projection"):
            pass
        with timing_phase("response_shape"):
            pass
        payload = timing.payload()
    assert payload["local_projection_ms"] == 1_000.0
    assert payload["response_shape_ms"] == 10_000.0
    assert _timing_contributor(payload) == "response_shape_exclusive=10.0s"


def test_missing_operation_clears_inherited_timing_context() -> None:
    with timing_context("parent") as parent:
        assert parent is not None
        with timing_context(None) as child:
            assert child is None
            assert current_timing() is None
        assert current_timing() is parent
    assert current_timing() is None


def test_route_attribution_distinguishes_complete_partial_and_unavailable() -> None:
    assert TIMING_REQUIRED_PHASES_BY_ROUTE["local_history"] == (
        "resolution",
        "local_projection",
        "response_shape",
    )
    complete = DaemonRequestTiming(operation_id="complete")
    complete.set_route("local_history")
    for phase in TIMING_REQUIRED_PHASES_BY_ROUTE["local_history"]:
        complete.add_phase(phase, 0.0)
    complete_payload = complete.payload()
    assert complete_payload["measured_required_phase_count"] == complete_payload["required_phase_count"]
    assert complete_payload["measured_required_phase_count"] == 3
    assert complete_payload["required_phase_count"] == 3
    assert complete_payload["not_applicable_phases"] == ["telegram_fallback"]
    assert complete_payload["telegram_fallback_ms"] is None
    assert complete_payload["unattributed_ms"] is not None

    partial = DaemonRequestTiming(operation_id="partial")
    partial.set_route("local_history")
    partial.add_phase("resolution", 1.0)
    partial_payload = partial.payload()
    assert cast(int, partial_payload["measured_required_phase_count"]) < cast(
        int, partial_payload["required_phase_count"]
    )
    assert partial_payload["unattributed_ms"] is None

    unavailable = DaemonRequestTiming(operation_id="unavailable")
    unavailable.set_route("local_history")
    unavailable_payload = unavailable.payload()
    assert unavailable_payload["measured_required_phase_count"] == 0
    assert unavailable_payload["unattributed_ms"] is None

    ordinary = DaemonRequestTiming(operation_id="ordinary")
    ordinary.set_route("telegram_fallback")
    for phase in TIMING_REQUIRED_PHASES_BY_ROUTE["telegram_fallback"]:
        ordinary.add_phase(phase, 0.0)
    ordinary_payload = ordinary.payload()
    assert ordinary_payload["measured_required_phase_count"] == ordinary_payload["required_phase_count"]
    assert ordinary_payload["required_phase_count"] == 3
    assert ordinary_payload["not_applicable_phases"] == ["local_projection"]
    assert ordinary_payload["unattributed_ms"] is not None

    context_fallback = DaemonRequestTiming(operation_id="context-fallback")
    context_fallback.set_route("telegram_context_fallback")
    for phase in ("resolution", "telegram_fallback", "response_shape"):
        context_fallback.add_phase(phase, 0.0)
    partial_context_payload = context_fallback.payload()
    assert cast(int, partial_context_payload["measured_required_phase_count"]) < cast(
        int, partial_context_payload["required_phase_count"]
    )
    assert partial_context_payload["required_phase_count"] == 4
    assert partial_context_payload["unattributed_ms"] is None
    context_fallback.add_phase("local_projection", 0.0)
    complete_context_payload = context_fallback.payload()
    assert complete_context_payload["measured_required_phase_count"] == 4


def test_rpc_attempt_cap_is_explicit() -> None:
    timing = DaemonRequestTiming(operation_id="attempts")
    for _ in range(1001):
        timing.record_rpc_attempt()
    payload = timing.payload()
    assert payload["rpc_attempts"] == 1000
    assert payload["attempts_capped"] is True


def test_standalone_request_id_is_bounded_and_generated() -> None:
    assert standalone_request_id("abcdef12") == "abcdef12"
    assert standalone_request_id(None) is None
    assert standalone_request_id("request-123") is None
    assert is_valid_request_id("abcdef12") is True
    assert is_valid_request_id("request-123") is False


def test_unknown_route_does_not_claim_phases_not_applicable() -> None:
    timing = DaemonRequestTiming(operation_id="unknown-route")
    timing.route_attempted = "future_route"  # type: ignore[assignment]
    payload = timing.payload()
    assert payload["route_attempted"] is None
    assert payload["required_phase_count"] == 0
    assert payload["not_applicable_phases"] == []
    assert payload["measured_required_phase_count"] == 0


def test_unknown_served_source_remains_unknown() -> None:
    assert _served_source({"ok": True, "data": {"source": "draft_current"}}) == "local"
    assert _served_source({"ok": True, "data": {"source": "sync_db+scheduled_messages+draft_current"}}) == "local"
    assert _served_source({"ok": True, "data": {"source": "future"}}) == "unknown"
    assert _served_source({"ok": False, "error": "failed"}) == "error"


def test_standalone_operation_id_accepts_only_generated_id_shape() -> None:
    valid = "a" * 32
    assert standalone_operation_id(valid) == valid
    for invalid in ("A" * 32, "short", "f" * 65, "g" * 32):
        generated = standalone_operation_id(invalid)
        assert generated != invalid
        assert len(generated) == 32
        assert generated == generated.lower()
        assert all(character in "0123456789abcdef" for character in generated)


@pytest.mark.asyncio
async def test_invalid_socket_operation_id_fails_before_dispatch_and_telemetry_join() -> None:
    server = make_server()

    async def unexpected_dispatch(_req: dict[str, object]) -> dict[str, object]:
        raise AssertionError("invalid operation ID must be rejected before dispatch")

    server._dispatch = unexpected_dispatch  # type: ignore[method-assign]
    response, method, request_id = await server._handle_client_line(
        json.dumps({"method": "list_messages", "operation_id": "operation-123"}).encode(),
        "",
        None,
    )

    assert response == {"ok": False, "error": "invalid_operation_id", "message": "operation_id is invalid"}
    assert method == "list_messages"
    assert request_id is None
    event, error = _normalize_telemetry_event(
        {"event": {"tool_name": "list_messages", "operation_id": "operation-123"}}
    )
    assert event is None
    assert error == {"ok": False, "error": "invalid_input", "message": "operation_id is invalid"}


@pytest.mark.asyncio
async def test_invalid_socket_request_id_fails_closed_before_dispatch() -> None:
    server = make_server()
    dispatch = AsyncMock()
    server._dispatch = dispatch  # type: ignore[method-assign]
    response, _, _ = await server._handle_client_line(
        json.dumps({"method": "list_messages", "request_id": "request-123"}).encode(),
        "",
        None,
    )
    assert response == {"ok": False, "error": "invalid_request_id", "message": "request_id is invalid"}
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_operation_and_timing_contexts_do_not_mix_concurrent_calls() -> None:
    barrier = asyncio.Barrier(2)

    async def collect(operation_id: str) -> tuple[str | None, str | None]:
        with correlation_context(operation_id), timing_context(operation_id) as timing:
            assert timing is not None
            timing.set_route("local_context")
            await barrier.wait()
            return current_operation_id(), cast(DaemonRequestTiming, current_timing()).operation_id

    first, second = await asyncio.gather(collect("first"), collect("second"))
    assert {first, second} == {("first", "first"), ("second", "second")}
    assert current_operation_id() is None


@pytest.mark.asyncio
async def test_daemon_envelope_carries_active_operation_id() -> None:
    reader = asyncio.StreamReader()
    writer = MagicMock()
    sent: list[bytes] = []
    writer.write = lambda data: sent.append(data)
    writer.drain = AsyncMock()
    reader.feed_data(b'{"ok":true}\n')

    operation_id = "a" * 32
    with correlation_context(operation_id):
        await DaemonConnection(reader, writer).request({"method": "list_messages"})

    payload = cast(dict[str, object], json.loads(sent[0]))
    assert payload["operation_id"] == operation_id


def test_mcp_telemetry_persists_operation_id_without_product_content() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_RUNTIME_OBSERVATIONS_V54_DDL.replace("runtime_observations_v54", "runtime_observations"))
    conn.execute("ALTER TABLE runtime_observations ADD COLUMN tool_capability TEXT")
    conn.execute("ALTER TABLE runtime_observations ADD COLUMN contract_version INTEGER")
    _insert_telemetry_row(
        conn,
        {
            "tool_name": "list_messages",
            "operation_id": "b" * 32,
            "timestamp": 1.0,
            "duration_ms": 12.0,
            "outcome": "success",
            "arguments": "secret",
            "message_text": "secret",
        },
    )
    row = cast(
        tuple[str, str] | None, conn.execute("SELECT operation_id, payload_json FROM runtime_observations").fetchone()
    )
    assert row == ("b" * 32, "{}")
    conn.close()


def test_daemon_timing_record_is_content_free_and_correlated() -> None:
    events: list[dict[str, object]] = []

    class Sink:
        def record(self, **kwargs: object) -> None:
            events.append(kwargs)

    server = object.__new__(DaemonAPIServer)
    server._runtime_observation_sink = cast(RuntimeObservationSink, Sink())
    timing = DaemonRequestTiming(operation_id="c" * 32)
    timing.set_route("local_context")
    timing.add_phase("resolution", 2.0)

    server._record_request_timing(timing, {"ok": True, "data": {"message_id": 42}})

    assert len(events) == 1
    assert events[0]["operation_id"] == "c" * 32
    assert events[0]["dialog_id"] is None
    assert "message_id" not in cast(dict[str, object], events[0]["payload"])


@pytest.mark.asyncio
async def test_admission_wait_and_rpc_execution_are_separate_boundaries() -> None:
    admission_started = asyncio.Event()
    release_admission = asyncio.Event()

    class Scheduler:
        def record_dispatch(self, _admission: RpcAdmission) -> None:
            return None

        def complete(self, _admission: RpcAdmission) -> None:
            return None

    async def admit(_scope: TelegramRpcScope) -> RpcAdmission:
        admission_started.set()
        await release_admission.wait()
        return RpcAdmission(TelegramRpcSource.MCP_INTERACTIVE, RpcServiceClass.INTERACTIVE, 0.0, 1)

    gate = SimpleNamespace(
        _admit=admit,
        _scheduler_transport_ready=lambda: True,
        _admission_scheduler=Scheduler(),
    )
    scope = TelegramRpcScope(
        source=TelegramRpcSource.MCP_INTERACTIVE,
        service_class=RpcServiceClass.INTERACTIVE,
        deadline=None,
        owner_task=None,
    )

    async def send_attempt(request: object, *, ordered: bool = False) -> str:
        del request
        del ordered
        return "ok"

    with timing_context("op-rpc") as timing:
        task = asyncio.create_task(
            _AdmissionAwareSender(cast(TelegramRpcGate, gate), send_attempt, scope).send("request")
        )
        await admission_started.wait()
        assert timing is not None
        assert timing.phases == {}
        release_admission.set()
        assert await task == "ok"
        payload = timing.payload()

    assert payload["rpc_admission_ms"] is not None
    assert payload["rpc_execution_ms"] is not None
    assert payload["rpc_attempts"] == 1


@pytest.mark.asyncio
async def test_daemon_cancellation_emits_closed_cancelled_outcome() -> None:
    events: list[dict[str, object]] = []

    class Sink:
        def record(self, **kwargs: object) -> None:
            events.append(kwargs)

    server = make_server()
    server._runtime_observation_sink = cast(RuntimeObservationSink, Sink())

    async def cancel(_req: dict[str, object]) -> dict[str, object]:
        raise asyncio.CancelledError

    server._dispatch = cancel  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await server._handle_client_line(
            json.dumps({"method": "list_messages", "operation_id": "d" * 32}).encode(),
            "",
            None,
        )

    assert len(events) == 1
    assert events[0]["outcome"] == "cancelled"
    assert events[0]["reason_code"] == "cancelled"


@pytest.mark.asyncio
async def test_unexpected_base_exception_is_not_recorded_as_cancellation() -> None:
    events: list[dict[str, object]] = []

    class Sink:
        def record(self, **kwargs: object) -> None:
            events.append(kwargs)

    class UnexpectedBaseException(BaseException):
        pass

    server = make_server()
    server._runtime_observation_sink = cast(RuntimeObservationSink, Sink())

    async def fail(_req: dict[str, object]) -> dict[str, object]:
        raise UnexpectedBaseException

    server._dispatch = fail  # type: ignore[method-assign]
    with pytest.raises(UnexpectedBaseException):
        await server._handle_client_line(
            json.dumps({"method": "list_messages", "operation_id": "3" * 32}).encode(),
            "",
            None,
        )

    assert events[0]["outcome"] != "cancelled"
    assert events[0]["reason_code"] == "request_failed"


@pytest.mark.asyncio
async def test_real_list_messages_seams_record_local_shape_and_fallback_attempt() -> None:  # noqa: PLR0914
    class Sink:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def record(self, **kwargs: object) -> None:
            self.events.append(kwargs)

    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 100, text="local")
    local_sink = Sink()
    local_server = make_server(conn, _TestClient())
    local_server._runtime_observation_sink = cast(RuntimeObservationSink, local_sink)
    local_response, _, _ = await local_server._handle_client_line(
        json.dumps(
            {
                "method": "list_messages",
                "operation_id": "e" * 32,
                "request_id": "abcdef12",
                "dialog_id": 1,
                "message_state": "sent",
            }
        ).encode(),
        "",
        None,
    )
    assert local_response["ok"] is True
    local_payload = cast(dict[str, object], local_sink.events[0]["payload"])
    assert local_sink.events[0]["operation_id"] == "e" * 32
    assert local_payload["route_attempted"] == "local_history"
    assert isinstance(local_payload["request_id"], str)
    assert local_payload["request_id"] == "abcdef12"
    assert local_payload["served_source"] == "local"
    assert local_payload["response_shape_ms"] is not None

    fallback_client = _TestClient()

    async def empty_iter(*_args: object, **_kwargs: object):
        if False:
            yield None

    fallback_client.iter_messages = empty_iter
    fallback_sink = Sink()
    fallback_server = make_server(_make_db(), fallback_client)
    fallback_server._runtime_observation_sink = cast(RuntimeObservationSink, fallback_sink)
    fallback_response, _, _ = await fallback_server._handle_client_line(
        json.dumps(
            {"method": "list_messages", "operation_id": "f" * 32, "dialog_id": 2, "message_state": "sent"}
        ).encode(),
        "",
        None,
    )
    assert fallback_response["ok"] is True
    fallback_payload = cast(dict[str, object], fallback_sink.events[0]["payload"])
    assert fallback_payload["fallback_attempted"] is True
    assert fallback_payload["route_attempted"] == "telegram_fallback"
    assert fallback_payload["served_source"] == "telegram"

    async def failed_iter(*_args: object, **_kwargs: object):
        raise RuntimeError("telegram unavailable")
        yield None

    failed_client = _TestClient()
    failed_client.iter_messages = failed_iter
    failed_sink = Sink()
    failed_server = make_server(_make_db(), failed_client)
    failed_server._runtime_observation_sink = cast(RuntimeObservationSink, failed_sink)
    failed_response, _, _ = await failed_server._handle_client_line(
        json.dumps(
            {"method": "list_messages", "operation_id": "1" * 32, "dialog_id": 2, "message_state": "sent"}
        ).encode(),
        "",
        None,
    )
    assert failed_response["ok"] is False
    failed_payload = cast(dict[str, object], failed_sink.events[0]["payload"])
    assert failed_payload["fallback_attempted"] is True
    assert failed_payload["route_attempted"] == "telegram_fallback"
    assert failed_payload["served_source"] == "error"


@pytest.mark.asyncio
async def test_non_sent_routes_measure_projection_and_response_shape() -> None:
    class Sink:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def record(self, **kwargs: object) -> None:
            self.events.append(kwargs)

    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 100, text="local")
    for index, message_state in enumerate(("scheduled", "all"), start=1):
        sink = Sink()
        server = make_server(conn)
        server._runtime_observation_sink = cast(RuntimeObservationSink, sink)
        response, _, _ = await server._handle_client_line(
            json.dumps(
                {
                    "method": "list_messages",
                    "operation_id": f"{index:032x}",
                    "dialog_id": 1,
                    "message_state": message_state,
                }
            ).encode(),
            "",
            None,
        )
        assert response["ok"] is True
        payload = cast(dict[str, object], sink.events[0]["payload"])
        assert payload["route_attempted"] == "local_non_sent_state"
        assert payload["request_id"] is None
        assert payload["local_projection_ms"] is not None
        assert payload["response_shape_ms"] is not None
        assert payload["measured_required_phase_count"] == payload["required_phase_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("message_state", ["scheduled", "all"])
async def test_scheduled_inner_shape_is_measured_independently(
    message_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.reading import service as reading_service_module
    from tests.test_scheduled_read_surface import _create_scheduled_table, _insert_scheduled

    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 100, text="local")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 200, 2_000_000_000, "scheduled")
    original_mapper = reading_service_module.scheduled_row_to_wire

    def delayed_mapper(row: object, *, inclusion_basis: object) -> dict[str, object]:
        item = original_mapper(cast(dict[str, object], row), inclusion_basis=cast(tuple[str, ...], inclusion_basis))
        timing = current_timing()
        assert timing is not None
        timing.add_phase("response_shape", 41.0)
        return item

    monkeypatch.setattr(reading_service_module, "scheduled_row_to_wire", delayed_mapper)
    sink_events: list[dict[str, object]] = []

    class Sink:
        def record(self, **kwargs: object) -> None:
            sink_events.append(kwargs)

    server = make_server(conn)
    server._runtime_observation_sink = cast(RuntimeObservationSink, Sink())
    response, _, _ = await server._handle_client_line(
        json.dumps(
            {
                "method": "list_messages",
                "operation_id": ("a" if message_state == "scheduled" else "b") * 32,
                "dialog_id": 1,
                "message_state": message_state,
            }
        ).encode(),
        "",
        None,
    )

    assert response["ok"] is True
    payload = cast(dict[str, object], sink_events[0]["payload"])
    assert cast(float, payload["response_shape_ms"]) >= 41.0


@pytest.mark.asyncio
async def test_empty_topic_selection_records_local_route_without_acquisition() -> None:
    class Sink:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def record(self, **kwargs: object) -> None:
            self.events.append(kwargs)

    conn = _make_db()
    dialog_id = 7
    topic_id = 9
    _insert_synced_dialog(conn, dialog_id)
    _insert_topic_metadata(conn, dialog_id, topic_id=topic_id, title="topic")
    _insert_message(conn, dialog_id, 100, text="without topic")
    client = _TestClient()

    client.iter_messages = AsyncMock(side_effect=AssertionError("no topic fallback acquisition"))
    sink = Sink()
    server = make_server(conn, client)
    server._runtime_observation_sink = cast(RuntimeObservationSink, sink)
    response, _, _ = await server._handle_client_line(
        json.dumps(
            {
                "method": "list_messages",
                "operation_id": f"{8:032x}",
                "dialog_id": dialog_id,
                "topic_id": topic_id,
                "message_state": "sent",
            }
        ).encode(),
        "",
        None,
    )
    assert response["ok"] is True
    assert response["data"]["source"] == "sync_db"
    payload = cast(dict[str, object], sink.events[0]["payload"])
    assert payload["route_attempted"] == "local_history"
    assert payload["served_source"] == "local"


@pytest.mark.asyncio
async def test_timing_sink_failure_does_not_change_product_result() -> None:
    class FailingSink:
        def record(self, **_kwargs: object) -> None:
            raise RuntimeError("sink unavailable")

    conn = _make_db()
    _insert_synced_dialog(conn, 1)
    _insert_message(conn, 1, 100)
    server = make_server(conn)
    server._runtime_observation_sink = cast(RuntimeObservationSink, FailingSink())
    response, _, _ = await server._handle_client_line(
        json.dumps({"method": "list_messages", "operation_id": "2" * 32, "dialog_id": 1}).encode(),
        "",
        None,
    )

    assert response["ok"] is True
