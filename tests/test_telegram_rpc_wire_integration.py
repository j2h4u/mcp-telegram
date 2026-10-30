"""The production gate owns actual vendor writes and SQLite safety receipts."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Coroutine
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
from telethon.tl.tlobject import TLObject

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
from mcp_telegram.telegram_rpc_consumers import DemandKind, demand_contract
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    RpcServiceClass,
    TelegramRpcScope,
    TelegramRpcSource,
)
from mcp_telegram.telegram_transport import (
    GuardedConnectionTcpFull,
    GuardedMTProtoPlainSender,
    GuardedMTProtoSender,
    TransportWriteError,
    _RequestState,
)
from tests.helpers import reset_rpc_process_policy
from tests.test_rpc_admission_observations import _Recorder
from tests.test_telegram_transport import _Writer


@dataclass(frozen=True)
class _Circuit:
    open: bool = False

    def detail(self) -> str:
        return "test_hold"


type _WireHarness = tuple[TelegramRpcGate, GuardedMTProtoSender, GuardedConnectionTcpFull, _Writer]


def _enqueue_ack_snapshot(sender: GuardedMTProtoSender, message_ids: list[int]) -> _RequestState:
    token = sender._control_origin.set((asyncio.current_task(), "ack"))
    try:
        state = cast(_RequestState, RequestState(types.MsgsAck(message_ids)))
        sender._send_queue.append(state)
    finally:
        sender._control_origin.reset(token)
    return state


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
        "automatic_resume",
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


async def test_unsent_terminal_ack_retains_ids_for_next_natural_snapshot(wire: _WireHarness) -> None:
    gate, sender, connection, sink = wire
    state = _enqueue_ack_snapshot(sender, [99, 100])
    assert await sender._send_queue.next_state() is state
    owned = sender.require_owned(state)
    owner = gate._require_wire_owner(owned.owner)
    old_budget = owner.scope.attempt_budget
    expired_deadline = time.monotonic() - 1
    owner.scope = replace(owner.scope, deadline=expired_deadline)
    sender._pending_ack.update({100, 101})
    with pytest.raises(RpcAdmissionExpiredError):
        await sender._prepare_state(state)
    await asyncio.sleep(0)
    await asyncio.gather(*sender._observing)
    assert owned.claims == 0 and sink.frames == []
    assert sender._pending_ack == {99, 100, 101}
    assert not sender._last_acks and state not in sender._owned_states
    assert owner.scope.deadline == expired_deadline
    assert owner.scope.attempt_budget is old_budget and old_budget is not None and old_budget.attempts == 0
    assert not sender._preparing and not sender._observing
    assert connection._send_queue.empty()

    # Only an already-existing natural sender turn may give the retained IDs a
    # fresh bounded snapshot; cleanup above creates no replacement owner/task.
    send_loop = asyncio.create_task(sender._send_loop())
    try:
        packet = await asyncio.wait_for(connection._send_queue.get(), 1)
        fresh_state = packet.state.state
        fresh_owned = sender.require_owned(fresh_state)
        fresh_owner = gate._require_wire_owner(fresh_owned.owner)
        assert fresh_state is not state and isinstance(fresh_state.request, types.MsgsAck)
        assert set(fresh_state.request.msg_ids) == {99, 100, 101}
        assert fresh_owned.owner is not owned.owner
        assert fresh_owner.scope.deadline is not None and fresh_owner.scope.deadline > time.monotonic()
        assert fresh_owner.scope.attempt_budget is not old_budget
        assert fresh_owner.scope.attempt_budget is not None
        assert fresh_owner.scope.attempt_budget.limit == old_budget.limit
        assert gate._limiter.max_rate == 30 and gate._limiter.time_period == 60
        assert not sender._pending_ack and sink.frames == []
        assert sender.write_packet(packet, connection._send)
        assert fresh_owned.claims == 1
        assert fresh_owner.scope.attempt_budget.attempts == 1 and len(sink.frames) == 1
    finally:
        send_loop.cancel()
        await asyncio.gather(send_loop, return_exceptions=True)


async def test_unsent_terminal_ack_capacity_rejection_retains_without_new_owner_or_task(
    wire: _WireHarness,
) -> None:
    gate, sender, connection, sink = wire
    source_limit = demand_contract(DemandKind.TELETHON_RECONNECT_PROBE).source_outstanding_limit
    states: list[_RequestState] = []
    for message_id in range(source_limit):
        state = _enqueue_ack_snapshot(sender, [message_id])
        assert await sender._send_queue.next_state() is state
        states.append(state)
        sender._offer_state(state)
    for _ in range(source_limit):
        await asyncio.sleep(0)
        if (
            gate._admission_scheduler._source_outstanding_depth(TelegramRpcSource.TELETHON_RECONNECT_PROBE)
            == source_limit
        ):
            break
    assert (
        gate._admission_scheduler._source_outstanding_depth(TelegramRpcSource.TELETHON_RECONNECT_PROBE) == source_limit
    )
    preparing_before = len(sender._preparing)

    rejected = _enqueue_ack_snapshot(sender, [99, 100])
    assert await sender._send_queue.next_state() is rejected
    rejected_owned = sender.require_owned(rejected)
    rejected_owner = gate._require_wire_owner(rejected_owned.owner)
    rejected_budget = rejected_owner.scope.attempt_budget
    sender._pending_ack.update({100, 101})
    sender._offer_state(rejected)
    for _ in range(10):
        await asyncio.sleep(0)
        if rejected.future.done():
            break
    assert rejected.future.done()
    assert isinstance(rejected.future.exception(), RpcAdmissionSaturatedError)
    await asyncio.sleep(0)
    await asyncio.gather(*sender._observing)

    assert rejected_owned.claims == 0 and sink.frames == []
    assert rejected_budget is not None and rejected_budget.attempts == 0
    assert sender._pending_ack == {99, 100, 101}
    assert rejected not in sender._owned_states and not sender._last_acks
    assert len(sender._preparing) == preparing_before - 1
    assert (
        gate._admission_scheduler._source_outstanding_depth(TelegramRpcSource.TELETHON_RECONNECT_PROBE) == source_limit
    )
    assert connection._send_queue.qsize() <= 1

    # Release the real outstanding capacity tickets without creating writes.
    for state in states:
        if not state.future.done():
            state.future.cancel()
    await gate._admission_scheduler.close()
    await asyncio.gather(*tuple(sender._preparing), return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.gather(*sender._observing, return_exceptions=True)


async def test_claimed_ack_replay_expiry_does_not_restore_fresh_ids(wire: _WireHarness) -> None:
    gate, sender, connection, sink = wire
    state = _enqueue_ack_snapshot(sender, [99])
    assert await sender._send_queue.next_state() is state
    owned = sender.require_owned(state)
    owner = gate._require_wire_owner(owned.owner)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    assert owned.claims == 1 and len(sender._last_acks) == 1

    expired_deadline = time.monotonic() - 1
    owner.scope = replace(owner.scope, deadline=expired_deadline)
    with pytest.raises(RpcAdmissionExpiredError):
        await sender._prepare_state(state)
    await asyncio.gather(*sender._observing)
    assert owned.claims == 1 and owner.scope.deadline == expired_deadline
    assert state in sender._last_acks and state in sender._owned_states
    assert not sender._pending_ack and len(sink.frames) == 1


async def test_claimed_ack_unknown_write_failure_does_not_restore_fresh_ids(wire: _WireHarness) -> None:
    gate, sender, connection, sink = wire
    state = _enqueue_ack_snapshot(sender, [99])
    assert await sender._send_queue.next_state() is state
    owned = sender.require_owned(state)
    owner = gate._require_wire_owner(owned.owner)
    await sender._prepare_state(state)

    def fail_write(_data: bytes) -> None:
        raise OSError("indeterminate test write")

    with pytest.raises(TransportWriteError, match="Telegram connection writer failed") as failure:
        sender.write_packet(connection._send_queue.get_nowait(), fail_write)
    assert isinstance(failure.value.__cause__, OSError)
    assert str(failure.value.__cause__) == "indeterminate test write"
    await asyncio.sleep(0)
    await asyncio.gather(*sender._observing)
    assert owned.claims == 1 and owner.scope.attempt_budget is not None
    assert owner.scope.attempt_budget.attempts == 1
    assert not sender._pending_ack and not sender._last_acks and state not in sender._owned_states
    assert sink.frames == [] and connection._send_queue.empty()


@pytest.mark.parametrize("terminal", ["cancelled", "closed"])
async def test_unsent_ack_cancel_or_closed_admission_does_not_promote_ids(wire: _WireHarness, terminal: str) -> None:
    gate, sender, connection, sink = wire
    state = _enqueue_ack_snapshot(sender, [99])
    assert await sender._send_queue.next_state() is state
    owned = sender.require_owned(state)
    if terminal == "cancelled":
        state.future.cancel()
    else:
        await gate._admission_scheduler.close()
        with pytest.raises(RpcAdmissionClosedError):
            await sender._prepare_state(state)
    await asyncio.sleep(0)
    await asyncio.gather(*sender._observing)
    assert owned.claims == 0 and not sender._pending_ack
    assert not sender._last_acks and state not in sender._owned_states
    assert sink.frames == [] and connection._send_queue.empty()


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


async def test_photo_methods_reject_unbound_physical_sender(wire: _WireHarness) -> None:
    gate, sender, connection, sink = wire
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(2),
        attempt_evidence=RpcAttemptEvidence(),
    )
    location = types.InputPhotoFileLocation(id=1, access_hash=1, file_reference=b"ref", thumb_size="m")
    requests = (
        functions.upload.GetFileRequest(location, 0, 4096),
        functions.auth.ExportAuthorizationRequest(2),
        functions.auth.ImportAuthorizationRequest(id=12, bytes=b"private"),
    )
    for request in requests:
        with pytest.raises(RuntimeError, match="gate-owned capability"):
            sender.send_owned(request, scope)
    assert not sink.frames
    assert connection._send_queue.empty()
    assert scope.attempt_budget is not None and scope.attempt_budget.attempts == 0
    assert gate._ensure_safety_ledger()._record["intents"] == {}


@pytest.fixture
async def image_wire(wire: _WireHarness, tmp_path: Path) -> AsyncIterator[_WireHarness]:
    from mcp_telegram.state import telegram_session_lease

    gate = wire[0]
    with telegram_session_lease(tmp_path) as lease:
        gate.bind_session_lease(lease)
        try:
            yield wire
        finally:
            await gate.close_rpc_scheduler()


class _PhotoBudget:
    def __init__(self, debits: list[bool] | None = None) -> None:
        self.debits = debits

    async def reserve(self, *, setup: bool) -> None:
        if self.debits is not None:
            self.debits.append(setup)

    async def current_redirect(self) -> int | None:
        return None

    async def reserve_redirect(self, dc_id: int) -> None:
        del dc_id


class _PhotoFence:
    async def prepare(self) -> _PhotoFence:
        return self

    def is_current(self) -> bool:
        return True


def _photo_lease(gate: TelegramRpcGate, budget: _PhotoBudget, attempts: int) -> telegram_rpc._AuxPhotoLease:
    from mcp_telegram.telegram_image_download import PhotoLocator, PhotoWork

    binding = gate.retained_auth_scope()
    task = asyncio.current_task()
    assert binding is not None and task is not None
    scope = TelegramRpcScope(
        TelegramRpcSource.MESSAGE_IMAGE_ACQUISITION,
        RpcServiceClass.BACKGROUND,
        time.monotonic() + 10,
        task,
        demand_kind=DemandKind.MESSAGE_IMAGE_ACQUISITION,
        attempt_budget=RpcAttemptBudget(attempts),
        attempt_evidence=RpcAttemptEvidence(),
    )
    location = types.InputPhotoFileLocation(id=5, access_hash=3, file_reference=b"ref", thumb_size="m")
    lease = telegram_rpc._AuxPhotoLease(
        gate,
        binding,
        task,
        scope,
        budget,
        _PhotoFence(),
        3,
        "lease",
        PhotoWork("w", "a", "s"),
        PhotoLocator(5, "m", 3, 50, 8, 8, location),
    )
    gate._photo_aux_lease = lease
    return lease


def _connected_photo_sender(
    gate: TelegramRpcGate, lease: telegram_rpc._AuxPhotoLease
) -> tuple[GuardedMTProtoSender, GuardedConnectionTcpFull, _Writer]:
    sender = GuardedMTProtoSender(
        AuthKey(bytes(reversed(range(256)))), authority=telegram_rpc._AuxPhotoAuthority(lease), loggers=gate._log
    )
    lease.sender = sender
    connection = GuardedConnectionTcpFull("127.0.0.1", 443, 3, loggers=gate._log)
    connection.bind_owner(sender)
    connection._connected = True
    sink = _Writer()
    connection._writer = cast(asyncio.StreamWriter, sink)
    connection._codec = FullPacketCodec(connection)
    sender._connection = connection
    sender._user_connected = True
    return sender, connection, sink


async def _prepare_photo_request(
    sender: GuardedMTProtoSender,
    lease: telegram_rpc._AuxPhotoLease,
    request: TLObject,
    *,
    setup: bool,
    auxiliary: bool,
) -> tuple[telegram_rpc._PhotoWireCapability, asyncio.Future[object], _RequestState]:
    cap = telegram_rpc._PhotoWireCapability(
        lease.gate._photo_issuer,
        lease.scope,
        lease.owner_task,
        bytes(request),
        lease.budget,
        lease.source_fence,
        setup,
        lease=lease,
        use_auxiliary=auxiliary,
    )
    if setup:
        cap.aux_bootstrap = lease.gate._photo_bootstrap_provenance(lease, not auxiliary)
    context = telegram_rpc._PHOTO_CAPABILITY.set(cap)
    try:
        future = sender.send_owned(request, lease.rpc_scope)
    finally:
        telegram_rpc._PHOTO_CAPABILITY.reset(context)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    return cap, future, state


async def test_photo_permit_reserves_before_physical_claim_and_checks_source(image_wire: _WireHarness) -> None:
    gate, sender, connection, sink = image_wire
    binding = gate.retained_auth_scope()
    assert binding is not None
    reserved: list[bool] = []

    class Token:
        current = True

        def is_current(self) -> bool:
            return self.current

    class Fence:
        def __init__(self) -> None:
            self.token = Token()

        async def prepare(self) -> Token:
            return self.token

    fence = Fence()
    task = asyncio.current_task()
    assert task is not None
    request = functions.upload.GetFileRequest(
        types.InputPhotoFileLocation(id=1, access_hash=1, file_reference=b"ref", thumb_size="m"), 0, 4096
    )
    capability = telegram_rpc._PhotoWireCapability(
        gate._photo_issuer, binding, task, bytes(request), _PhotoBudget(reserved), fence, False
    )
    scope = TelegramRpcScope(
        TelegramRpcSource.MESSAGE_IMAGE_ACQUISITION,
        RpcServiceClass.BACKGROUND,
        time.monotonic() + 10,
        task,
        demand_kind=DemandKind.MESSAGE_IMAGE_ACQUISITION,
        attempt_budget=RpcAttemptBudget(2),
        attempt_evidence=RpcAttemptEvidence(),
    )
    context = telegram_rpc._PHOTO_CAPABILITY.set(capability)
    try:
        future = sender.send_owned(request, scope)
    finally:
        telegram_rpc._PHOTO_CAPABILITY.reset(context)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    assert reserved == [False]
    fence.token.current = False
    assert not sender.write_packet(packet, connection._send)
    assert not sink.frames
    assert scope.attempt_budget is not None and scope.attempt_budget.attempts == 0
    future.cancel()


async def test_exact_photo_resolution_uses_guarded_wire_and_durable_budget(
    image_wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.telegram_demand import durable_demand_context
    from mcp_telegram.telegram_image_download import PhotoObservation, PhotoTarget

    gate, sender, connection, sink = image_wire
    binding = gate.retained_auth_scope()
    assert binding is not None and gate.session is not None
    monkeypatch.setattr(gate.session, "get_input_entity", lambda _dialog_id: types.InputPeerUser(100, 9))
    debits: list[bool] = []

    async def acquire() -> PhotoObservation:
        with durable_demand_context(DemandKind.MESSAGE_IMAGE_ACQUISITION):
            return await gate.resolve_photo(
                PhotoTarget(100, 7),
                scope=binding,
                observation_order=1,
                budget=_PhotoBudget(debits),
                source_fence=_PhotoFence(),
            )

    task = asyncio.create_task(acquire())
    state = await asyncio.wait_for(sender._send_queue.next_state(), timeout=2)
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    assert debits == [True]
    assert sender.write_packet(packet, connection._send)
    assert len(sink.frames) == 1
    photo = types.Photo(
        id=5, access_hash=3, file_reference=b"ref", date=None, dc_id=2, sizes=[types.PhotoSize("m", 8, 8, 50)]
    )
    message = types.Message(
        id=7, peer_id=types.PeerUser(100), date=None, message="", media=types.MessageMediaPhoto(photo=photo)
    )
    state.future.set_result(types.messages.Messages(messages=[message], chats=[], users=[], topics=[]))
    result = await asyncio.wait_for(task, timeout=2)
    assert result.locator.photo_id == 5


async def test_same_dc_photo_iterator_uses_guarded_physical_file_request(
    image_wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.telegram_demand import durable_demand_context
    from mcp_telegram.telegram_image_download import PhotoTarget, PhotoWork

    gate, sender, connection, sink = image_wire
    binding = gate.retained_auth_scope()
    assert binding is not None and gate.session is not None
    monkeypatch.setattr(gate.session, "get_input_entity", lambda _dialog_id: types.InputPeerUser(100, 9))
    debits: list[bool] = []

    async def acquire() -> list[bytes]:
        with durable_demand_context(DemandKind.MESSAGE_IMAGE_ACQUISITION):
            budget = _PhotoBudget(debits)
            observation = await gate.resolve_photo(
                PhotoTarget(100, 7), scope=binding, observation_order=1, budget=budget, source_fence=_PhotoFence()
            )
            session = await gate.open_photo_download(
                observation.locator,
                scope=binding,
                work=PhotoWork("work", "attempt", "source"),
                budget=budget,
                source_fence=_PhotoFence(),
            )
            try:
                return [chunk async for chunk in session.chunks(offset=0)]
            finally:
                await session.close()

    task = asyncio.create_task(acquire())
    state = await asyncio.wait_for(sender._send_queue.next_state(), timeout=2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    photo = types.Photo(
        id=5, access_hash=3, file_reference=b"ref", date=None, dc_id=2, sizes=[types.PhotoSize("m", 8, 8, 50)]
    )
    message = types.Message(
        id=7, peer_id=types.PeerUser(100), date=None, message="", media=types.MessageMediaPhoto(photo=photo)
    )
    state.future.set_result(types.messages.Messages(messages=[message], chats=[], users=[], topics=[]))
    state = await asyncio.wait_for(sender._send_queue.next_state(), timeout=2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result(types.upload.File(type=types.storage.FileJpeg(), mtime=0, bytes=b"x" * 50))
    assert await asyncio.wait_for(task, timeout=2) == [b"x" * 50]
    assert debits == [True, False]
    assert len(sink.frames) == 2


async def test_auxiliary_owned_file_claim_uses_auxiliary_identity_and_shared_gate(image_wire: _WireHarness) -> None:
    gate, main_sink = image_wire[0], image_wire[3]
    debits: list[bool] = []
    lease = _photo_lease(gate, _PhotoBudget(debits), 2)
    sender, connection, sink = _connected_photo_sender(gate, lease)
    request = functions.upload.GetFileRequest(cast(types.InputPhotoFileLocation, lease.locator.location), 0, 524288)
    cap, future, state = await _prepare_photo_request(sender, lease, request, setup=False, auxiliary=True)
    raw = gate._safety_store.load_record()
    assert raw is not None
    intent = next(iter(cast(dict[str, dict[str, object]], json.loads(raw)["intents"]).values()))
    assert intent["scope"] == {"dc_id": 3, "key_id": sender.auth_key.key_id, "account_id": 42}
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result(types.upload.File(type=types.storage.FileJpeg(), mtime=0, bytes=b"x" * 50))
    await future
    assert debits == [False] and len(sink.frames) == 1 and not main_sink.frames
    await gate._close_photo_aux_lease()


async def _drive_foreign_photo_exchange(
    image_wire: _WireHarness,
    aux_sink: _Writer,
    connected: asyncio.Event,
    debits: list[bool],
    acquire: Callable[[], Coroutine[object, object, list[bytes]]],
) -> None:
    gate, main, main_conn, main_sink = image_wire
    task = asyncio.create_task(acquire())
    state = await asyncio.wait_for(main._send_queue.next_state(), 2)
    await main._prepare_state(state)
    assert main.write_packet(main_conn._send_queue.get_nowait(), main_conn._send)
    photo = types.Photo(
        id=5, access_hash=3, file_reference=b"ref", date=None, dc_id=3, sizes=[types.PhotoSize("m", 8, 8, 50)]
    )
    message = types.Message(
        id=7, peer_id=types.PeerUser(100), date=None, message="", media=types.MessageMediaPhoto(photo=photo)
    )
    state.future.set_result(types.messages.Messages(messages=[message], chats=[], users=[], topics=[]))
    await asyncio.wait_for(connected.wait(), 2)
    state = await asyncio.wait_for(main._send_queue.next_state(), 2)
    await main._prepare_state(state)
    assert main.write_packet(main_conn._send_queue.get_nowait(), main_conn._send)
    state.future.set_result(types.auth.ExportedAuthorization(id=12, bytes=b"secret"))
    lease = gate._photo_aux_lease
    assert lease is not None and lease.sender is not None
    aux = lease.sender
    assert aux._retries == 0 and aux._auto_reconnect is False
    aux_conn = cast(GuardedConnectionTcpFull, aux._connection)
    state = await asyncio.wait_for(aux._send_queue.next_state(), 2)
    await aux._prepare_state(state)
    assert aux.write_packet(aux_conn._send_queue.get_nowait(), aux_conn._send)
    state.future.set_result(types.auth.Authorization(user=types.User(id=42)))
    state = await asyncio.wait_for(aux._send_queue.next_state(), 2)
    await aux._prepare_state(state)
    assert aux.write_packet(aux_conn._send_queue.get_nowait(), aux_conn._send)
    state.future.set_result(types.upload.File(type=types.storage.FileJpeg(), mtime=0, bytes=b"x" * 50))
    assert await asyncio.wait_for(task, 2) == [b"x" * 50]
    assert debits == [True, True, True, False]
    assert len(main_sink.frames) == 2 and len(aux_sink.frames) == 2
    assert gate._photo_aux_lease is None and main.physical_connected()


async def test_foreign_dc_photo_bootstrap_and_download_use_guarded_senders(
    image_wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.telegram_demand import durable_demand_context
    from mcp_telegram.telegram_image_download import PhotoTarget, PhotoWork

    gate, main, main_conn, main_sink = image_wire
    scope = gate.retained_auth_scope()
    assert scope is not None and gate.session is not None
    monkeypatch.setattr(gate.session, "get_input_entity", lambda _dialog_id: types.InputPeerUser(100, 9))
    aux_sink = _Writer()
    connected = asyncio.Event()
    debits: list[bool] = []

    async def fake_dc(_session: object, _lease: object) -> object:
        return SimpleNamespace(ip_address="127.0.0.1", port=443, id=3)

    async def fake_connect(sender: GuardedMTProtoSender, connection: GuardedConnectionTcpFull) -> bool:
        connection.bind_owner(sender)
        connection._connected = True
        connection._writer = cast(asyncio.StreamWriter, aux_sink)
        connection._codec = FullPacketCodec(connection)
        sender._connection = connection
        sender._user_connected = True
        sender.auth_key.key = bytes(reversed(range(256)))
        connected.set()
        return True

    monkeypatch.setattr(gate, "_photo_dc_option", fake_dc)
    monkeypatch.setattr(GuardedMTProtoSender, "connect", fake_connect)

    async def acquire() -> list[bytes]:
        with durable_demand_context(DemandKind.MESSAGE_IMAGE_ACQUISITION):
            budget = _PhotoBudget(debits)
            observation = await gate.resolve_photo(
                PhotoTarget(100, 7), scope=scope, observation_order=1, budget=budget, source_fence=_PhotoFence()
            )
            session = await gate.open_photo_download(
                observation.locator,
                scope=scope,
                work=PhotoWork("work", "attempt", "source"),
                budget=budget,
                source_fence=_PhotoFence(),
            )
            try:
                return [chunk async for chunk in session.chunks(offset=0)]
            finally:
                await session.close()

    await _drive_foreign_photo_exchange(image_wire, aux_sink, connected, debits, acquire)


async def test_image_scope_claim_rejects_identity_transition_aba(image_wire: _WireHarness) -> None:
    gate = image_wire[0]
    token = gate.capture_image_scope()
    assert token is not None and gate.claim_image_scope(token)
    with gate._image_identity_change():
        assert not gate.claim_image_scope(token)
        assert gate.capture_image_scope() is None
    replacement = gate.capture_image_scope()
    assert replacement is not None and gate.claim_image_scope(replacement)
    assert not gate.claim_image_scope(token)


@pytest.mark.parametrize("late_flood", [False, True])
async def test_unknown_photo_export_is_retired_without_replay_or_late_credentials(
    image_wire: _WireHarness, late_flood: bool
) -> None:
    gate, main, connection, sink = image_wire
    lease = _photo_lease(gate, _PhotoBudget(), 2)
    aux = GuardedMTProtoSender(
        AuthKey(bytes(reversed(range(256)))), authority=telegram_rpc._AuxPhotoAuthority(lease), loggers=gate._log
    )
    lease.sender = aux
    request = functions.auth.ExportAuthorizationRequest(3)
    cap, future, state = await _prepare_photo_request(main, lease, request, setup=True, auxiliary=False)
    assert main.write_packet(connection._send_queue.get_nowait(), connection._send)
    owner = cap.wire_owner
    assert owner is not None and owner.claimed == 1
    await gate._retire_uncertain_photo_bootstrap(cap, lease)
    intent = cast(dict[str, dict[str, object]], gate._ensure_safety_ledger()._record["intents"])[owner.attempt_id]
    assert intent["phase"] == "unknown"
    assert cast(dict[str, object], intent["aux_bootstrap"])["contained"] is True
    assert gate._wire_fault is None and main.physical_connected()
    with pytest.raises(asyncio.CancelledError):
        await main._prepare_state(state)
    if late_flood:
        state.future.set_exception(FloodWaitError(request, capture=7))
        with pytest.raises(FloodWaitError):
            await future
        assert gate._ensure_safety_ledger().floor_deadline > time.monotonic()
    else:
        state.future.set_result(types.auth.ExportedAuthorization(id=12, bytes=b"private"))
        with pytest.raises(RuntimeError, match="discarded"):
            await future
    assert len(sink.frames) == 1
    aux.auth_key.key = None
    gate._photo_aux_lease = None


async def test_actual_aux_sender_connect_authenticates_through_guarded_plain_frames(
    image_wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import struct

    from telethon.network import authenticator

    gate, main, _main_conn, main_sink = image_wire
    debits: list[bool] = []
    lease = _photo_lease(gate, _PhotoBudget(debits), 16)
    scope = lease.rpc_scope
    sender = GuardedMTProtoSender(
        None,
        authority=telegram_rpc._AuxPhotoAuthority(lease),
        loggers=gate._log,
        retries=0,
        delay=0,
        auto_reconnect=False,
    )
    lease.sender = sender
    connection = GuardedConnectionTcpFull("127.0.0.1", 443, 3, loggers=gate._log)
    sink = _Writer()

    async def socket_connect(*_args: object, **_kwargs: object) -> None:
        connection._owned_epoch += 1
        connection._connected = True
        connection._writer = cast(asyncio.StreamWriter, sink)
        connection._codec = FullPacketCodec(connection)
        connection._send_task = asyncio.create_task(connection._send_loop())

    async def scripted_auth(plain: object) -> tuple[AuthKey, int]:
        result = await cast(GuardedMTProtoPlainSender, plain).send(functions.ReqPqMultiRequest(1))
        assert isinstance(result, types.ResPQ)
        return AuthKey(bytes(reversed(range(256)))), 0

    monkeypatch.setattr(connection, "connect", socket_connect)
    monkeypatch.setattr(authenticator, "do_authentication", scripted_auth)
    connecting = asyncio.create_task(sender.connect(connection))
    frame = await asyncio.wait_for(sink.written.get(), 2)
    assert frame and debits == [True]
    response = bytes(types.ResPQ(1, 2, b"pq", []))
    connection._recv_queue.put_nowait((struct.pack("<qqi", 0, 4, len(response)) + response, None))
    assert await asyncio.wait_for(connecting, 2)
    assert sender.auth_key and sender._auto_reconnect is False
    assert scope.attempt_budget is not None and scope.attempt_budget.attempts == 1
    assert not main_sink.frames and main.physical_connected()
    await gate._close_photo_aux_lease()


@pytest.mark.parametrize("transport_failure", [False, True])
async def test_unknown_aux_import_requires_teardown_and_key_discard(
    image_wire: _WireHarness, transport_failure: bool
) -> None:
    gate, main, main_sink = image_wire[0], image_wire[1], image_wire[3]
    lease = _photo_lease(gate, _PhotoBudget(), 2)
    aux, connection, _sink = _connected_photo_sender(gate, lease)
    request = functions.auth.ImportAuthorizationRequest(id=12, bytes=b"private")
    cap, future, state = await _prepare_photo_request(aux, lease, request, setup=True, auxiliary=True)
    assert aux.write_packet(connection._send_queue.get_nowait(), connection._send)
    owner = cap.wire_owner
    assert owner is not None and owner.claimed == 1
    if transport_failure:
        error = ConnectionError("auxiliary receive failed")
        with pytest.raises(ConnectionError, match="auxiliary receive failed"):
            await aux._disconnect(error)
    await gate._retire_uncertain_photo_bootstrap(cap, lease)
    with pytest.raises((asyncio.CancelledError, ConnectionError)):
        await future
    intent = cast(dict[str, dict[str, object]], gate._ensure_safety_ledger()._record["intents"])[owner.attempt_id]
    assert intent["phase"] == "unknown" and cast(dict[str, object], intent["aux_bootstrap"])["contained"] is True
    assert aux.auth_key.key is None and gate._photo_aux_lease is None
    assert gate._wire_fault is None and main.physical_connected() and not main_sink.frames


async def test_failed_gate_teardown_keeps_session_flock_pinned(
    wire: _WireHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.state import telegram_session_lease

    gate, sender, connection, _sink = wire
    with telegram_session_lease(tmp_path) as lease:
        gate.bind_session_lease(lease)

        async def failed_disconnect() -> None:
            raise RuntimeError("test teardown failure")

        monkeypatch.setattr(sender, "disconnect", failed_disconnect)
        with pytest.raises(RuntimeError, match="test teardown failure"):
            await gate.close_rpc_scheduler()
    with pytest.raises(RuntimeError, match="already owned"), telegram_session_lease(tmp_path):
        pass
    await connection.disconnect()
    pin = gate._session_pin
    assert pin is not None
    pin._release()
    gate._session_pin = None
    with telegram_session_lease(tmp_path) as replacement:
        assert replacement.is_held()


async def test_invalid_account_identity_revokes_image_scope(image_wire: _WireHarness) -> None:
    gate = image_wire[0]
    token = gate.capture_image_scope()
    assert token is not None and gate.claim_image_scope(token)
    await gate._ensure_safety_ledger().reject_identity_result(mismatched_account=False)
    assert not gate.claim_image_scope(token)
    assert gate.capture_image_scope() is None
