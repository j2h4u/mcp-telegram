"""The production gate owns actual vendor writes and SQLite safety receipts."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError, ServerError
from telethon.network.connection.tcpfull import FullPacketCodec
from telethon.network.requeststate import RequestState
from telethon.sessions import MemorySession
from telethon.tl import functions, types
from telethon.tl.functions import channels, messages
from telethon.tl.functions.updates import GetStateRequest

from mcp_telegram import telegram_rpc
from mcp_telegram.config import RuntimeObservationConfig, TelegramRpcSchedulerConfig
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import (
    ensure_sync_schema,
    load_account_cooldown_until_utc,
    load_rpc_safety_owners,
    load_rpc_safety_record,
    open_sync_db_reader,
    save_rpc_safety_record,
)
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptBudgetExhaustedError, RpcAttemptEvidence
from mcp_telegram.telegram_rpc import (
    TelegramRpcBudget,
    TelegramRpcGate,
    TelegramRpcSafetyStore,
    _AdmissionAwareSender,
    _TransportBoundaryState,
)
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionExpiredError,
    RpcServiceClass,
    TelegramRpcScope,
    TelegramRpcSource,
)
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender, _RequestState
from tests.helpers import reset_rpc_process_policy
from tests.test_rpc_admission_observations import _Recorder
from tests.test_telegram_transport import _Writer


@dataclass(frozen=True)
class _Circuit:
    open: bool = False

    def detail(self) -> str:
        return "test_hold"


type _WireHarness = tuple[TelegramRpcGate, GuardedMTProtoSender, GuardedConnectionTcpFull, _Writer]


@pytest.fixture
async def wire(
    tmp_path: Path,
) -> AsyncIterator[_WireHarness]:
    reset_rpc_process_policy()
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)

    async def save(expected: str | None, encoded: str) -> None:
        await writer.run(lambda conn: save_rpc_safety_record(conn, expected, encoded))

    store = TelegramRpcSafetyStore(
        lambda: load_rpc_safety_record(reader),
        save,
        lambda: load_account_cooldown_until_utc(reader),
        lambda: load_rpc_safety_owners(reader),
    )
    session = MemorySession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    gate = TelegramRpcGate(
        session,
        1,
        "test",
        rpc_budget=TelegramRpcBudget(30, 60),
        circuit_status=_Circuit,
        fallback_wait_seconds=1,
        cooldown_buffer_seconds=0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=store,
    )
    await gate.verify_startup_account(42)
    sender = cast(GuardedMTProtoSender, gate._main_sender)
    connection = GuardedConnectionTcpFull("127.0.0.1", 443, 2, loggers=gate._log)
    connection.bind_owner(sender)
    connection._connected = True
    sink = _Writer()
    connection._writer = cast(asyncio.StreamWriter, sink)
    connection._codec = FullPacketCodec(connection)
    sender._connection = connection
    sender._user_connected = True
    gate._transport_state = _TransportBoundaryState.READY
    try:
        yield gate, sender, connection, sink
    finally:
        for state in tuple(sender._owned_states):
            if not state.future.done():
                state.future.cancel()
        await asyncio.sleep(0)
        await asyncio.gather(*sender._observing, return_exceptions=True)
        await gate._admission_scheduler.close()
        await connection.disconnect()
        await writer.aclose()
        reader.close()
        reset_rpc_process_policy()


async def test_actual_write_has_durable_intent_before_bytes_and_settles_after_result(wire: _WireHarness) -> None:
    gate, sender, connection, sink = wire
    budget = RpcAttemptBudget(2)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=budget,
        attempt_evidence=RpcAttemptEvidence(),
    )
    result = sender.send_owned(GetStateRequest(), scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    encoded = gate._safety_store.load_record()
    assert encoded is not None
    record = cast(dict[str, object], json.loads(encoded))
    assert len(cast(dict[str, object], record["intents"])) == 1
    assert sink.frames == [] and budget.attempts == 0 and state.msg_id is None
    assert sender.write_packet(packet, connection._send)
    assert len(sink.frames) == 1 and budget.attempts == 1 and state.msg_id is not None
    state.future.set_result("complete")
    assert await result == "complete"
    settled = gate._safety_store.load_record()
    assert settled is not None
    assert cast(dict[str, object], json.loads(settled))["intents"] == {}


def test_canonical_method_identity_distinguishes_namespaces() -> None:
    from mcp_telegram.telegram_rpc import _canonical_wire_method

    assert _canonical_wire_method(messages.GetMessagesRequest([])) != _canonical_wire_method(
        channels.GetMessagesRequest(types.InputChannelEmpty(), [])
    )


async def test_raw_flood_revokes_a_pregranted_write_before_any_receipt_task_runs(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    now = [time.monotonic()]
    monkeypatch.setattr(telegram_rpc, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    gate._admission_scheduler._clock = lambda: now[0]
    futures: list[asyncio.Future[object]] = []
    packets = []
    budgets = [RpcAttemptBudget(2), RpcAttemptBudget(2)]
    for budget in budgets:
        scope = TelegramRpcScope(
            TelegramRpcSource.MCP_INTERACTIVE,
            RpcServiceClass.INTERACTIVE,
            now[0] + 10,
            asyncio.current_task(),
            attempt_budget=budget,
            attempt_evidence=RpcAttemptEvidence(),
        )
        futures.append(sender.send_owned(GetStateRequest(), scope))
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        packets.append(connection._send_queue.get_nowait())
    assert sender.write_packet(packets[0], connection._send)
    now[0] += 2
    assert gate._admission_scheduler._next_write_at <= now[0]
    packets[0].state.state.future.set_exception(FloodWaitError(GetStateRequest(), capture=7))
    # No event-loop yield: the durable receipt and async observer have not run.
    assert not sender.write_packet(packets[1], connection._send)
    assert len(sink.frames) == 1
    assert [budget.attempts for budget in budgets] == [1, 0]
    with pytest.raises(FloodWaitError):
        await futures[0]
    assert gate._ensure_safety_ledger().floor_deadline > now[0]


async def test_cancelled_logical_owner_cannot_revive_after_task_uncancel(wire: _WireHarness) -> None:
    gate, sender, connection, sink = wire
    cancelled = asyncio.Event()
    release = asyncio.Event()
    budget = RpcAttemptBudget(1)

    async def caller() -> None:
        scope = TelegramRpcScope(
            TelegramRpcSource.MCP_INTERACTIVE,
            RpcServiceClass.INTERACTIVE,
            time.monotonic() + 10,
            asyncio.current_task(),
            attempt_budget=budget,
            attempt_evidence=RpcAttemptEvidence(),
        )
        try:
            await _AdmissionAwareSender(gate, scope).send(GetStateRequest())
        except asyncio.CancelledError:
            task = asyncio.current_task()
            assert task is not None
            task.uncancel()
            cancelled.set()
            await release.wait()

    task = asyncio.create_task(caller())
    state = await sender._send_queue.next_state()
    try:
        task.cancel()
        await cancelled.wait()
        assert not task.done() and not task.cancelling()
        with pytest.raises(asyncio.CancelledError):
            await sender._prepare_state(state)
        assert sink.frames == [] and budget.attempts == 0
        assert connection._send_queue.empty()
        assert gate._wire_fault is None
    finally:
        release.set()
        await task


async def test_pregranted_shared_budget_exhaustion_does_not_drop_transport(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    now = [time.monotonic()]
    monkeypatch.setattr(telegram_rpc, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    gate._admission_scheduler._clock = lambda: now[0]
    budget = RpcAttemptBudget(1)
    evidence = RpcAttemptEvidence()
    futures: list[asyncio.Future[object]] = []
    packets = []
    for _ in range(2):
        scope = TelegramRpcScope(
            TelegramRpcSource.MCP_INTERACTIVE,
            RpcServiceClass.INTERACTIVE,
            now[0] + 10,
            asyncio.current_task(),
            attempt_budget=budget,
            attempt_evidence=evidence,
        )
        futures.append(sender.send_owned(GetStateRequest(), scope))
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        packets.append(connection._send_queue.get_nowait())
    assert sender.write_packet(packets[0], connection._send)
    now[0] += 2
    assert not sender.write_packet(packets[1], connection._send)
    state = await sender._send_queue.next_state()
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await sender._prepare_state(state)
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await futures[1]
    packets[0].state.state.future.set_result("first")
    assert await futures[0] == "first"
    assert len(sink.frames) == 1 and budget.attempts == 1
    assert connection._connected and gate._wire_fault is None
    assert not any(gate._admission_scheduler.active_depths().values())


async def test_revoked_written_owner_keeps_its_late_flood_receipt(wire: _WireHarness) -> None:
    gate, sender, connection, sink = wire
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def caller() -> None:
        scope = TelegramRpcScope(
            TelegramRpcSource.MCP_INTERACTIVE,
            RpcServiceClass.INTERACTIVE,
            time.monotonic() + 10,
            asyncio.current_task(),
            attempt_budget=RpcAttemptBudget(1),
            attempt_evidence=RpcAttemptEvidence(),
        )
        try:
            await _AdmissionAwareSender(gate, scope).send(GetStateRequest())
        except asyncio.CancelledError:
            task = asyncio.current_task()
            assert task is not None
            task.uncancel()
            cancelled.set()
            await release.wait()

    task = asyncio.create_task(caller())
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    public = sender._owned_states[state].result
    assert public is not None
    assert sender.write_packet(packet, connection._send)
    try:
        task.cancel()
        await cancelled.wait()
        assert not state.future.done() and len(sink.frames) == 1
        assert gate._wire_fault is None
        state.future.set_exception(FloodWaitError(GetStateRequest(), capture=7))
        with pytest.raises(FloodWaitError):
            await public
        assert gate._ensure_safety_ledger().floor_deadline > time.monotonic()
        assert not any(gate._admission_scheduler.active_depths().values())
    finally:
        release.set()
        await task


@pytest.mark.parametrize("reason", ["unresolved_outbound_outcome", "flood_wait_circuit_open"])
async def test_protocol_controls_remain_owned_during_application_protection(wire: _WireHarness, reason: str) -> None:
    gate, _, _, _ = wire
    ledger = gate._ensure_safety_ledger()
    ledger._record["protection"] = reason
    assert gate._control_scope_valid()
    for request, origin in (
        (types.MsgsAck([1]), "ack"),
        (functions.PingRequest(1), "ping"),
        (types.MsgsStateInfo(1, ""), "state_info"),
    ):
        owner = gate._require_wire_owner(
            gate.register_protocol(request, bytes(request), cast(telegram_rpc.ProtocolOrigin, origin))
        )
        assert owner.scope.transport_control and not owner.scope.transport_bootstrap
        assert owner.scope.attempt_budget is not None
        assert gate._wire_release_at(owner.scope) == 0
    with pytest.raises(RuntimeError, match="unclassified"):
        gate.register_protocol(GetStateRequest(), bytes(GetStateRequest()), "ack")
    request = functions.ReqPqMultiRequest(1)
    with pytest.raises(RuntimeError, match="lifecycle capability"):
        gate.register_protocol(request, bytes(request), "auth")
    gate._connection_capability = gate._capture_connection_capability()
    owner = gate._require_wire_owner(gate.register_protocol(request, bytes(request), "auth"))
    assert owner.scope.transport_bootstrap and not owner.scope.transport_control
    assert owner.scope.attempt_budget is gate._connection_capability.attempt_budget


@pytest.mark.parametrize(
    "reason", ["safety_auth_scope_mismatch", "safety_canonical_owner_mismatch", "authentication_account_mismatch"]
)
async def test_live_identity_rejection_denies_protocol_controls(wire: _WireHarness, reason: str) -> None:
    gate, _, _, _ = wire
    gate._ensure_safety_ledger()._record["protection"] = reason
    assert not gate._control_scope_valid()


async def test_protocol_controls_require_healthy_storage_and_current_transport(wire: _WireHarness) -> None:
    gate, _, _, _ = wire
    ledger = gate._ensure_safety_ledger()
    assert gate._control_scope_valid()
    ledger._storage_failed = True
    assert not gate._control_scope_valid()
    ledger._storage_failed = False
    gate._transport_state = _TransportBoundaryState.FAILED
    assert not gate._control_scope_valid()
    gate._transport_state = _TransportBoundaryState.READY
    scope = cast(dict[str, object], ledger._record["scope"])
    scope["key_id"] = None
    assert not gate._control_scope_valid()


async def test_account_safety_status_reports_protection_without_session_material(wire: _WireHarness) -> None:
    gate, _, _, _ = wire
    assert gate.account_safety_status() is None
    ledger = gate._ensure_safety_ledger()
    ledger._record["protection"] = "unresolved_outbound_outcome"
    status = gate.account_safety_status()
    assert status is not None and set(status) == {
        "generation",
        "reason",
        "known_retry_until_utc",
        "unresolved_request_count",
        "unresolved_requests",
        "unresolved_requests_truncated",
    }
    assert status["unresolved_request_count"] == 0 and status["unresolved_requests"] == []
    assert status["reason"] == "unresolved_outbound_outcome"
    gate._wire_fault = "storage_failure"
    status = gate.account_safety_status()
    assert status is not None and status["reason"] == "storage_failure"
    gate._wire_fault = None
    ledger._record["protection"] = None
    gate._transport_state = _TransportBoundaryState.FAILED
    status = gate.account_safety_status()
    assert status is not None and status["reason"] == "transport_terminated"
    gate._transport_state = _TransportBoundaryState.READY
    gate._logout_mode = True
    status = gate.account_safety_status()
    assert status is not None and status["reason"] == "operator_logout"


async def test_cancelled_receipt_drain_retains_storage_ownership_until_completion(wire: _WireHarness) -> None:
    gate, _, _, _ = wire
    entered = asyncio.Event()
    release = asyncio.Event()

    async def persist() -> None:
        entered.set()
        await release.wait()
        await gate._ensure_safety_ledger().record_floor(time.time() + 7)

    receipt = asyncio.create_task(persist())
    gate._cooldown_save_tasks[1] = receipt
    drain = asyncio.create_task(gate._drain_cooldown_saves())
    await entered.wait()
    drain.cancel()
    await asyncio.sleep(0)
    assert not drain.done() and not receipt.cancelled()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await drain
    assert receipt.done() and not gate._cooldown_save_tasks
    assert gate._ensure_safety_ledger().floor_deadline > time.monotonic()


@pytest.mark.parametrize("failure", ["expired", "closed"])
async def test_unsent_failed_ping_allows_fresh_keepalive_without_reconnect(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    gate, sender, connection, sink = wire
    reconnects: list[Exception | None] = []
    monkeypatch.setattr(sender, "_start_reconnect", lambda error: reconnects.append(error))
    before = gate._safety_store.load_record()
    sender._keepalive_ping(7)
    state = await sender._send_queue.next_state()
    owned = sender.require_owned(state)
    owner = gate._require_wire_owner(owned.owner)
    if failure == "expired":
        owner.scope = replace(owner.scope, deadline=time.monotonic() - 1)
        expected_error = RpcAdmissionExpiredError
    else:
        await gate._admission_scheduler.close()
        expected_error = RpcAdmissionClosedError
    with pytest.raises(expected_error):
        await sender._prepare_state(state)
    await asyncio.sleep(0)
    await asyncio.gather(*sender._observing)
    assert owned.claims == 0 and sender._ping is None and sink.frames == []
    assert owner.scope.attempt_budget is not None and owner.scope.attempt_budget.attempts == 0
    assert gate._safety_store.load_record() == before
    sender._keepalive_ping(8)
    fresh = await asyncio.wait_for(sender._send_queue.next_state(), 0.2)
    assert isinstance(fresh.request, functions.PingRequest) and fresh.request.ping_id == 8
    assert sender._ping == 8 and reconnects == []
    if failure == "expired":
        await sender._prepare_state(fresh)
        packet = connection._send_queue.get_nowait()
        assert sender.write_packet(packet, connection._send)
        assert len(sink.frames) == 1


@pytest.mark.parametrize("claimed,newer_marker", [(True, False), (False, True)])
async def test_failed_ping_cleanup_preserves_claimed_or_newer_ping(
    wire: _WireHarness, claimed: bool, newer_marker: bool
) -> None:
    gate, sender, connection, sink = wire
    sender._keepalive_ping(7)
    state = await sender._send_queue.next_state()
    owned = sender.require_owned(state)
    if claimed:
        await sender._prepare_state(state)
        packet = connection._send_queue.get_nowait()
        assert sender.write_packet(packet, connection._send)
        assert owned.claims == 1 and len(sink.frames) == 1
    if newer_marker:
        sender._ping = 8
    state.future.set_exception(
        RpcAdmissionExpiredError(gate._require_wire_owner(owned.owner).scope, "terminal ping failure")
    )
    await asyncio.sleep(0)
    await asyncio.gather(*sender._observing)
    assert sender._ping == (8 if newer_marker else 7)


async def test_unsent_ack_is_not_evicted_by_sent_replay_cache_pressure(wire: _WireHarness) -> None:
    _gate, sender, connection, sink = wire
    states: list[_RequestState] = []
    token = sender._control_origin.set((asyncio.current_task(), "ack"))
    try:
        for message_id in range(11):
            state = cast(_RequestState, RequestState(types.MsgsAck([message_id])))
            states.append(state)
            sender._send_queue.append(state)
            if message_id == 0:
                first = await sender._send_queue.next_state()
                await sender._prepare_state(first)
    finally:
        sender._control_origin.reset(token)
    first = states[0]
    packet = connection._send_queue.get_nowait()
    assert sender.require_owned(first).claims == 0
    assert not sender.require_owned(first).retired and len(sender._last_acks) == 0
    assert sender.write_packet(packet, connection._send)
    assert len(sink.frames) == 1 and sender.require_owned(first).claims == 1
    for expected in states[1:]:
        state = await sender._send_queue.next_state()
        assert state is expected
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        await asyncio.gather(*sender._observing)
    assert len(sink.frames) == 11 and len(sender._last_acks) == 10
    assert set(sender._owned_states) == set(sender._last_acks)
    assert first not in sender._owned_states


async def test_unsent_terminal_ack_does_not_enter_sent_replay_cache(wire: _WireHarness) -> None:
    gate, sender, _connection, sink = wire
    token = sender._control_origin.set((asyncio.current_task(), "ack"))
    try:
        state = cast(_RequestState, RequestState(types.MsgsAck([99])))
        sender._send_queue.append(state)
    finally:
        sender._control_origin.reset(token)
    assert await sender._send_queue.next_state() is state
    owned = sender.require_owned(state)
    owner = gate._require_wire_owner(owned.owner)
    owner.scope = replace(owner.scope, deadline=time.monotonic() - 1)
    with pytest.raises(RpcAdmissionExpiredError):
        await sender._prepare_state(state)
    await asyncio.sleep(0)
    await asyncio.gather(*sender._observing)
    assert owned.claims == 0 and sink.frames == []
    assert not sender._last_acks and state not in sender._owned_states


@pytest.mark.parametrize("outcome", ["success", "rpc_error", "write_failure", "response_lost"])
async def test_rpc_observations_follow_writer_and_provider_boundaries(  # noqa: PLR0914, PLR0915 - four real transport outcomes share the owner fixture
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    gate, sender, connection, sink = wire
    recorder = _Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder, method_names=TelegramRpcGate.rpc_observation_methods(), policy=RuntimeObservationConfig()
    )
    gate.set_rpc_request_observer(observer.observe_rpc_event)
    budget = RpcAttemptBudget(2)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=budget,
        attempt_evidence=RpcAttemptEvidence(),
    )
    result = sender.send_owned(GetStateRequest(), scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    owner = gate._require_wire_owner(packet.state.owner)
    if outcome == "write_failure":

        def fail_write(_data: bytes) -> None:
            raise OSError("private error text must not enter telemetry")

        monkeypatch.setattr(sink, "write", fail_write)
        with pytest.raises(OSError):
            sender.write_packet(packet, connection._send)
        with pytest.raises(OSError):
            await result
    else:
        assert sender.write_packet(packet, connection._send)
        gate.observe_write_return(owner)  # A duplicate callback is not a second local write.
        if outcome == "success":
            state.future.set_result("private result body")
            assert await result == "private result body"
            await gate.observe_result(owner, None, "private result body")
        elif outcome == "rpc_error":
            state.future.set_exception(ServerError(None, "private server error"))
            with pytest.raises(ServerError):
                await result
        else:
            state.future.cancel()
            with pytest.raises(asyncio.CancelledError):
                await result
    observer.flush()
    summaries = [cast(dict[str, object], row["payload"]) for row in recorder.rows if row["outcome"] == "method_summary"]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["write_claim"] == budget.attempts == 1
    assert summary["write_return"] == (0 if outcome == "write_failure" else 1)
    assert summary["known_response"] == (1 if outcome in {"success", "rpc_error"} else 0)
    assert summary["unknown_outcome"] == (1 if outcome in {"write_failure", "response_lost"} else 0)
    samples = [cast(dict[str, object], row["payload"]) for row in recorder.rows if row["outcome"] == "causal_sample"]
    terminal = [p for p in samples if p["phase"] in {"known_response", "unknown_outcome"}]
    assert len(terminal) == 1
    assert "attempt_ordinal" not in terminal[0] and "admission_sequence" not in terminal[0]
    assert "private" not in repr(recorder.rows)


async def test_rpc_replay_claims_keep_same_owner_and_distinct_physical_ordinals(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    now = [time.monotonic()]
    monkeypatch.setattr(telegram_rpc, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    gate._admission_scheduler._clock = lambda: now[0]
    recorder = _Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: now[0],
    )
    gate.set_rpc_request_observer(observer.observe_rpc_event)
    budget = RpcAttemptBudget(2)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        now[0] + 10,
        asyncio.current_task(),
        attempt_budget=budget,
        attempt_evidence=RpcAttemptEvidence(),
    )
    result = sender.send_owned(GetStateRequest(), scope)
    state = await sender._send_queue.next_state()
    for _ in range(2):
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        now[0] += 2
    state.future.set_result("complete")
    assert await result == "complete"
    observer.flush()
    samples = [cast(dict[str, object], row["payload"]) for row in recorder.rows if row["outcome"] == "causal_sample"]
    claims = [p for p in samples if p["phase"] == "write_claim"]
    assert len(sink.frames) == budget.attempts == 2
    assert [p["attempt_ordinal"] for p in claims] == [1, 2]
    assert [p["replay_ordinal"] for p in claims] == [1, 2]
    assert claims[0]["attempt_ref"] == claims[1]["attempt_ref"]
    assert claims[0]["admission_sequence"] != claims[1]["admission_sequence"]
    assert claims[1]["reason"] == "replay"
    assert len([p for p in samples if p["phase"] == "known_response"]) == 1


async def test_rpc_control_synthetic_success_is_not_a_provider_response(wire: _WireHarness) -> None:
    gate, sender, connection, _sink = wire
    recorder = _Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder, method_names=TelegramRpcGate.rpc_observation_methods(), policy=RuntimeObservationConfig()
    )
    gate.set_rpc_request_observer(observer.observe_rpc_event)
    token = sender._control_origin.set((asyncio.current_task(), "ack"))
    try:
        state = cast(_RequestState, RequestState(types.MsgsAck([99])))
        sender._send_queue.append(state)
    finally:
        sender._control_origin.reset(token)
    assert await sender._send_queue.next_state() is state
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    await asyncio.gather(*sender._observing)
    observer.flush()
    summary = next(
        cast(dict[str, object], row["payload"]) for row in recorder.rows if row["outcome"] == "method_summary"
    )
    assert summary["write_claim"] == summary["write_return"] == 1
    assert summary["known_response"] == summary["unknown_outcome"] == 0


async def test_throwing_rpc_observer_cannot_change_write_receipt_or_returned_result(
    wire: _WireHarness, caplog: pytest.LogCaptureFixture
) -> None:
    gate, sender, connection, sink = wire

    def broken(**_values: object) -> None:
        raise RuntimeError("private observer error")

    gate.set_rpc_request_observer(broken)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(1),
        attempt_evidence=RpcAttemptEvidence(),
    )
    result = sender.send_owned(GetStateRequest(), scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result("unchanged")
    assert await result == "unchanged"
    record = gate._safety_store.load_record()
    assert record is not None and json.loads(record)["intents"] == {}
    assert len(sink.frames) == 1 and gate._wire_fault is None
    assert "private observer error" not in caplog.text


async def test_due_flush_keeps_paired_admission_and_method_claim_in_one_window(wire: _WireHarness) -> None:
    gate, sender, connection, _sink = wire
    now = [0.0]
    recorder = _Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder,
        method_names=TelegramRpcGate.rpc_observation_methods(),
        policy=RuntimeObservationConfig(),
        clock=lambda: now[0],
    )
    gate.set_rpc_request_observer(observer.observe_rpc_event)
    gate.set_rpc_admission_observer(observer.observe)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(1),
        attempt_evidence=RpcAttemptEvidence(),
    )
    result = sender.send_owned(GetStateRequest(), scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    now[0] = 300.0
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    admission = [
        cast(dict[str, object], row["payload"]) for row in recorder.rows if row["kind"] == "telegram.rpc_admission"
    ]
    methods = [cast(dict[str, object], row["payload"]) for row in recorder.rows if row["outcome"] == "method_summary"]
    assert (
        sum(cast(int, p["dispatched_count"]) for p in admission)
        == sum(cast(int, p["write_claim"]) for p in methods)
        == 1
    )
    assert len([row for row in recorder.rows if row["outcome"] == "coverage"]) == 1
    state.future.set_result("complete")
    assert await result == "complete"
