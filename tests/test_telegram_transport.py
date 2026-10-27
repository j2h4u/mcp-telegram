"""Exercise the guarded writer with Telethon's actual state and Queue(1)."""

from __future__ import annotations

import asyncio
import logging
import struct
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError, RPCError
from telethon.network import mtprotostate
from telethon.network.connection.tcpfull import FullPacketCodec
from telethon.network.requeststate import RequestState
from telethon.tl.core import MessageContainer, RpcResult, TLMessage
from telethon.tl.functions import PingRequest, ReqDHParamsRequest, ReqPqMultiRequest, SetClientDHParamsRequest
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.types import BadServerSalt, MsgsAck, MsgsStateInfo, MsgsStateReq, ResPQ, RpcError

from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_scheduler import RpcServiceClass, TelegramRpcScope, TelegramRpcSource
from mcp_telegram.telegram_transport import (
    GuardedConnectionTcpFull,
    GuardedMTProtoPlainSender,
    GuardedMTProtoSender,
    ProtocolOrigin,
    _OwnedPacket,
    _RequestState,
)


@dataclass(eq=False)
class _Owner:
    body: bytes
    scope: TelegramRpcScope | None = None
    origin: ProtocolOrigin | None = None
    cancelled: bool = False


@dataclass(frozen=True, eq=False)
class _Permit:
    owner: _Owner
    generation: int


class _Authority:
    def __init__(self) -> None:
        self.now = 0.0
        self.generation = 0
        self.floor = False
        self.owners: list[_Owner] = []
        self.claims: list[_Owner] = []
        self.released: list[_Permit] = []
        self.prepared: asyncio.Queue[_Permit] = asyncio.Queue()
        self.deferred: asyncio.Queue[_Permit] = asyncio.Queue()
        self.application_ready = asyncio.Event()
        self.application_ready.set()
        self.observed: list[tuple[object, BaseException | None]] = []
        self.protected: list[BaseException] = []
        self.receipt_started = asyncio.Event()
        self.receipt_ready = asyncio.Event()
        self.receipt_ready.set()
        self.receipt_error: Exception | None = None
        self.prepare_error: Exception | None = None
        self.terminal_notices: list[tuple[object, BaseException]] = []
        self.observation_inputs: list[tuple[object, BaseException | None, object]] = []
        self.result_rejection: BaseException | None = None
        self.terminations: list[BaseException | None] = []

    def register_application(self, request: object, body: bytes, scope: TelegramRpcScope) -> object:
        owner = _Owner(body, scope=scope)
        self.owners.append(owner)
        return owner

    def register_protocol(self, request: object, body: bytes, origin: ProtocolOrigin) -> object:
        owner = _Owner(body, origin=origin)
        self.owners.append(owner)
        return owner

    async def prepare_write(self, owner: object) -> object:
        assert isinstance(owner, _Owner)
        if self.prepare_error is not None:
            raise self.prepare_error
        if owner.scope is not None:
            await self.application_ready.wait()
        permit = _Permit(owner, self.generation)
        self.prepared.put_nowait(permit)
        return permit

    def claim_write(self, owner: object, permit: object) -> bool:
        assert isinstance(owner, _Owner)
        assert isinstance(permit, _Permit)
        self.claims.append(owner)
        if permit.owner is not owner or permit.generation != self.generation or owner.cancelled:
            return False
        if owner.scope is not None:
            if self.floor or (owner.scope.deadline is not None and self.now >= owner.scope.deadline):
                return False
            if owner.scope.attempt_budget is not None:
                if owner.scope.attempt_budget.exhausted:
                    return False
                owner.scope.attempt_budget.debit()
        return True

    def defer_write(self, owner: object, permit: object) -> None:
        assert isinstance(permit, _Permit)
        assert permit.owner is owner
        self.released.append(permit)
        self.deferred.put_nowait(permit)

    async def observe_result(
        self, owner: object, error: BaseException | None, result: object | None = None
    ) -> BaseException | None:
        self.observation_inputs.append((owner, error, result))
        self.receipt_started.set()
        await self.receipt_ready.wait()
        if self.receipt_error is not None:
            raise self.receipt_error
        self.observed.append((owner, error))
        return self.result_rejection

    def protect_transport(self, error: BaseException) -> None:
        self.protected.append(error)

    def observe_error_now(self, owner: object, error: BaseException) -> None:
        self.terminal_notices.append((owner, error))
        if isinstance(error, FloodWaitError):
            self.floor = True
            self.generation += 1

    def transport_terminated(self, error: BaseException | None) -> None:
        self.terminations.append(error)
        self.generation += 1


class _Writer:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.written: asyncio.Queue[bytes] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.append(data)
        self.written.put_nowait(data)

    async def drain(self) -> None:
        return

    def close(self) -> None:
        return

    async def wait_closed(self) -> None:
        return


class _Harness:
    def __init__(self) -> None:
        self.authority = _Authority()
        loggers = defaultdict(lambda: logging.getLogger(__name__))
        self.sender = GuardedMTProtoSender(AuthKey(bytes(range(256))), authority=self.authority, loggers=loggers)
        self.connection = GuardedConnectionTcpFull("127.0.0.1", 443, 1, loggers=loggers)
        self.connection.bind_owner(self.sender)
        self.connection._connected = True
        self.writer = _Writer()
        self.connection._writer = cast(asyncio.StreamWriter, self.writer)
        self.codec = FullPacketCodec(self.connection)
        self.connection._codec = self.codec
        self.sender._connection = self.connection
        self.sender._user_connected = True
        self.tasks: list[asyncio.Task[object]] = []
        self.futures: list[asyncio.Future[object]] = []

    def enqueue(self, *, budget: int = 2, request: object | None = None) -> _RequestState:
        scope = TelegramRpcScope(
            TelegramRpcSource.MCP_INTERACTIVE,
            RpcServiceClass.INTERACTIVE,
            10.0,
            asyncio.current_task(),
            attempt_budget=RpcAttemptBudget(budget),
        )
        self.futures.append(self.sender.send_owned(GetConfigRequest() if request is None else request, scope))
        return next(reversed(self.sender._owned_states))

    async def prepare(self, state: _RequestState) -> _OwnedPacket:
        assert await self.sender._send_queue.next_state() is state
        await self.sender._prepare_state(state)
        return self.connection._send_queue.get_nowait()

    def start_writer(self) -> None:
        task = asyncio.create_task(self.connection._send_loop())
        self.connection._send_task = task
        self.tasks.append(task)

    async def close(self) -> None:
        self.authority.receipt_ready.set()
        self.sender._user_connected = False
        for task in [*self.tasks, *self.sender._preparing]:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.sender._preparing, return_exceptions=True)
        for state in tuple(self.sender._owned_states):
            if not state.future.done():
                state.future.cancel()
            elif not state.future.cancelled():
                state.future.exception()
        for future in self.futures:
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
        await _event_loop_turn()
        await asyncio.gather(*self.sender._preparing, return_exceptions=True)
        await asyncio.gather(*self.sender._observing, return_exceptions=True)
        await self.connection.disconnect()


async def _event_loop_turn() -> None:
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


@pytest.fixture
async def wire() -> AsyncIterator[_Harness]:
    harness = _Harness()
    try:
        yield harness
    finally:
        await harness.close()


async def test_owned_queue_does_not_serialize_before_final_claim(wire: _Harness) -> None:
    state = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is state
    await wire.sender._prepare_state(state)
    assert wire.connection._send_queue.maxsize == 1
    assert wire.connection._send_queue.full()
    assert state.msg_id is None
    assert wire.sender._state._sequence == 0
    assert wire.sender._state._last_msg_id == 0
    assert wire.codec._send_counter == 0
    assert not wire.authority.claims
    assert not wire.writer.frames
    wire.start_writer()
    await asyncio.wait_for(wire.writer.written.get(), 1)
    assert state.msg_id is not None
    assert wire.sender._state._sequence == 1
    assert wire.codec._send_counter == 1
    assert wire.sender._pending_state[state.msg_id] is state


@pytest.mark.parametrize("revocation", ["cancel", "deadline", "floor", "generation"])
async def test_occupied_queue_revalidates_before_first_bytes(wire: _Harness, revocation: str) -> None:
    first = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is first
    await wire.sender._prepare_state(first)
    await wire.authority.prepared.get()
    second = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is second
    preparing = asyncio.create_task(wire.sender._prepare_state(second))
    wire.tasks.append(preparing)
    permit = await asyncio.wait_for(wire.authority.prepared.get(), 1)
    assert not preparing.done()
    assert second.msg_id is None
    assert wire.sender._state._sequence == 0
    # Release the first permit before invalidating the queued second request.
    first_packet = wire.connection._send_queue.get_nowait()
    assert wire.sender.write_packet(first_packet, wire.connection._send)
    await preparing
    if revocation == "cancel":
        permit.owner.cancelled = True
    elif revocation == "deadline":
        wire.authority.now = 10.0
    elif revocation == "floor":
        wire.authority.floor = True
    else:
        wire.authority.generation += 1
    wire.start_writer()
    assert await asyncio.wait_for(wire.authority.deferred.get(), 1) is permit
    assert len(wire.writer.frames) == 1
    assert second.msg_id is None
    assert wire.sender._state._sequence == 1
    assert wire.codec._send_counter == 1


@pytest.mark.parametrize("terminal", ["cancel", "result"])
async def test_terminal_state_is_denied_synchronously_before_authority_callback(wire: _Harness, terminal: str) -> None:
    state = wire.enqueue()
    packet = await wire.prepare(state)
    if terminal == "cancel":
        state.future.cancel()
    else:
        state.future.set_result(None)
    # No event-loop turn: asynchronous result observation cannot protect this gap.
    assert not wire.sender.write_packet(packet, wire.connection._send)
    assert not wire.writer.frames
    assert not wire.authority.claims
    assert state.msg_id is None
    assert wire.sender._state._sequence == 0
    assert len(wire.authority.released) == 1


async def test_queued_terminal_packet_is_harmless_after_result_retirement(wire: _Harness) -> None:
    state = wire.enqueue()
    packet = await wire.prepare(state)
    state.future.set_result(None)
    assert await wire.futures[-1] is None
    assert state not in wire.sender._owned_states
    assert not wire.sender.write_packet(packet, wire.connection._send)
    assert wire.authority.released == [packet.permit]
    assert not wire.authority.claims
    assert not wire.authority.protected
    assert not wire.writer.frames


async def test_raw_and_forged_protocol_states_are_rejected(wire: _Harness) -> None:
    with pytest.raises(RuntimeError):
        wire.sender.send(GetConfigRequest())
    for request in (GetConfigRequest(), MsgsAck([1]), PingRequest(2)):
        with pytest.raises(RuntimeError):
            wire.sender._send_queue.append(cast(_RequestState, RequestState(request)))
    with pytest.raises(RuntimeError):
        await wire.connection.send(b"unowned bytes")
    token = wire.sender._control_origin.set((asyncio.current_task(), "ack"))
    try:
        with pytest.raises(RuntimeError):
            wire.sender._send_queue.append(cast(_RequestState, RequestState(GetConfigRequest())))

        async def inherited_context() -> None:
            with pytest.raises(RuntimeError):
                wire.sender._send_queue.append(cast(_RequestState, RequestState(MsgsAck([1]))))

        await asyncio.create_task(inherited_context())
    finally:
        wire.sender._control_origin.reset(token)
    assert not wire.authority.owners
    assert not wire.writer.frames


async def test_owned_payload_mutation_is_rejected_before_admission(wire: _Harness) -> None:
    state = wire.enqueue()
    state.data = bytes(PingRequest(1))
    with pytest.raises(RuntimeError):
        wire.sender.require_owned(state)
    assert not wire.authority.claims
    assert not wire.writer.frames


async def test_vendor_bad_salt_replay_reuses_original_owner_future_and_budget(wire: _Harness) -> None:
    state = wire.enqueue(budget=1)
    original_future = state.future
    original_owner = wire.sender.require_owned(state).owner
    original_body = state.data
    packet = await wire.prepare(state)
    assert wire.sender.write_packet(packet, wire.connection._send)
    assert state.msg_id is not None
    await wire.sender._handle_bad_server_salt(TLMessage(99, 0, BadServerSalt(state.msg_id, 1, 48, 9)))
    assert await wire.sender._send_queue.next_state() is state
    assert wire.sender.require_owned(state).owner is original_owner
    assert state.future is original_future
    assert state.data == original_body
    await wire.sender._prepare_state(state)
    replay = wire.connection._send_queue.get_nowait()
    assert not wire.sender.write_packet(replay, wire.connection._send)
    assert len(wire.authority.owners) == 1
    assert len(wire.authority.claims) == 2
    assert len(wire.writer.frames) == 1
    assert wire.sender._state._sequence == 1
    assert isinstance(original_owner, _Owner)
    assert original_owner.scope is not None
    assert original_owner.scope.deadline == 10.0
    assert original_owner.scope.attempt_budget is not None
    assert original_owner.scope.attempt_budget.attempts == 1


async def test_ack_progresses_while_application_prepare_is_held(wire: _Harness) -> None:
    wire.authority.application_ready.clear()
    application = wire.enqueue()
    wire.sender._pending_ack.add(123)
    wire.start_writer()
    sending = asyncio.create_task(wire.sender._send_loop())
    wire.tasks.append(sending)
    await asyncio.wait_for(wire.writer.written.get(), 1)
    assert [owner.origin for owner in wire.authority.claims] == ["ack"]
    assert application.msg_id is None
    assert not application.future.done()
    assert len(wire.writer.frames) == 1


async def test_vendor_ping_and_state_info_are_classified_protocol(wire: _Harness) -> None:
    wire.sender._keepalive_ping(7)
    ping = await wire.sender._send_queue.next_state()
    assert wire.sender.require_owned(ping).owner is wire.authority.owners[-1]
    assert wire.authority.owners[-1].origin == "ping"
    await wire.sender._handle_state_forgotten(TLMessage(9, 0, MsgsStateReq([1, 2])))
    state_info = await wire.sender._send_queue.next_state()
    assert wire.authority.owners[-1].origin == "state_info"
    assert isinstance(state_info.request, MsgsStateInfo)
    assert state_info.request.req_msg_id == 9
    for state in (ping, state_info):
        await wire.sender._prepare_state(state)
        assert wire.sender.write_packet(wire.connection._send_queue.get_nowait(), wire.connection._send)
    assert len(wire.writer.frames) == 2


async def test_acquired_permit_is_released_when_connection_rejects_send(wire: _Harness) -> None:
    state = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is state
    wire.connection._connected = False
    await wire.sender._prepare_state(state)
    permit = await wire.authority.prepared.get()
    assert wire.authority.released == [permit]
    assert not wire.writer.frames
    assert state.msg_id is None


async def test_cancelled_queue_put_releases_exact_permit_once(wire: _Harness) -> None:
    first = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is first
    await wire.sender._prepare_state(first)
    await wire.authority.prepared.get()
    second = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is second
    preparing = asyncio.create_task(wire.sender._prepare_state(second))
    permit = await asyncio.wait_for(wire.authority.prepared.get(), 1)
    preparing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await preparing
    assert wire.authority.released == [permit]
    assert second.msg_id is None
    assert not wire.writer.frames


async def test_disconnect_does_not_leave_blocked_put_in_dead_connection(wire: _Harness) -> None:
    first = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is first
    await wire.sender._prepare_state(first)
    first_permit = await wire.authority.prepared.get()
    second = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is second
    preparing = asyncio.create_task(wire.sender._prepare_state(second))
    wire.tasks.append(preparing)
    second_permit = await asyncio.wait_for(wire.authority.prepared.get(), 1)
    assert not preparing.done()
    await wire.connection.disconnect()
    await asyncio.wait_for(preparing, 1)
    assert wire.connection._send_queue.empty()
    assert wire.authority.released.count(first_permit) == 1
    assert wire.authority.released.count(second_permit) == 1
    assert not wire.writer.frames
    assert first.msg_id is None
    assert second.msg_id is None


async def test_oversize_body_is_rejected_before_claim_or_sequence(wire: _Harness) -> None:
    class OversizeRequest(GetConfigRequest):
        def _bytes(self) -> bytes:
            return b"x" * MessageContainer.MAXIMUM_SIZE

    state = wire.enqueue(request=OversizeRequest())
    packet = await wire.prepare(state)
    assert not wire.sender.write_packet(packet, wire.connection._send)
    with pytest.raises(ValueError, match="too large"):
        await wire.futures[-1]
    assert not wire.authority.claims
    assert wire.authority.released == [packet.permit]
    assert not wire.writer.frames
    assert state.msg_id is None
    assert wire.sender._state._sequence == 0
    assert state not in wire.sender._owned_states


async def test_preparation_started_on_old_connection_cannot_target_replacement(wire: _Harness) -> None:
    wire.authority.application_ready.clear()
    state = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is state
    preparing = asyncio.create_task(wire.sender._prepare_state(state))
    wire.tasks.append(preparing)
    await _event_loop_turn()
    assert not preparing.done()
    await wire.connection.disconnect()
    replacement = GuardedConnectionTcpFull(
        "127.0.0.1", 443, 1, loggers=defaultdict(lambda: logging.getLogger(__name__))
    )
    replacement.bind_owner(wire.sender)
    replacement._connected = True
    wire.sender._connection = replacement
    wire.authority.application_ready.set()
    await asyncio.wait_for(preparing, 1)
    assert replacement._send_queue.empty()
    assert wire.connection._send_queue.empty()
    assert len(wire.authority.released) == 1
    assert not wire.authority.claims
    assert state.msg_id is None
    replacement._connected = False


@pytest.mark.parametrize("failed", [False, True])
async def test_public_result_waits_for_durable_observation(wire: _Harness, failed: bool) -> None:
    state = wire.enqueue()
    wire.authority.receipt_ready.clear()
    failure = RuntimeError("server result")
    if failed:
        state.future.set_exception(failure)
    else:
        state.future.set_result("retained result")
    await asyncio.wait_for(wire.authority.receipt_started.wait(), 1)
    assert not wire.futures[-1].done()
    assert not wire.authority.observed
    wire.authority.receipt_ready.set()
    if failed:
        with pytest.raises(RuntimeError, match="server result") as caught:
            await wire.futures[-1]
        assert caught.value is failure
    else:
        assert await wire.futures[-1] == "retained result"
    assert len(wire.authority.observed) == 1
    assert state not in wire.sender._owned_states


@pytest.mark.parametrize("stage", ["serialization", "encryption", "socket_write"])
async def test_claimed_write_failure_settles_application_and_protects_transport(
    wire: _Harness, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    state = wire.enqueue()
    packet = await wire.prepare(state)
    failure = RuntimeError(stage)

    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    if stage == "serialization":
        monkeypatch.setattr(wire.sender._state, "write_data_as_message", fail)
    elif stage == "encryption":
        monkeypatch.setattr(wire.sender._state, "encrypt_message_data", fail)
    else:
        monkeypatch.setattr(wire.writer, "write", fail)
    with pytest.raises(RuntimeError) as caught:
        wire.sender.write_packet(packet, wire.connection._send)
    assert caught.value is failure
    with pytest.raises(RuntimeError) as caught:
        await wire.futures[-1]
    assert caught.value is failure
    assert wire.authority.protected == [failure]
    assert len(wire.authority.claims) == 1
    assert not wire.authority.released
    assert not wire.writer.frames
    assert state not in wire.sender._owned_states
    assert state not in wire.sender._pending_state.values()


@pytest.mark.parametrize("server_failed", [False, True])
async def test_receipt_persistence_failure_protects_transport_and_preserves_server_error(
    wire: _Harness, server_failed: bool
) -> None:
    state = wire.enqueue()
    persistence_error = OSError("receipt unavailable")
    server_error = RuntimeError("server rejected request")
    wire.authority.receipt_error = persistence_error
    if server_failed:
        state.future.set_exception(server_error)
    else:
        state.future.set_result("server accepted request")
    with pytest.raises((OSError, RuntimeError)) as caught:
        await wire.futures[-1]
    assert caught.value is (server_error if server_failed else persistence_error)
    assert wire.authority.protected == [persistence_error]
    assert state not in wire.sender._owned_states
    assert not wire.authority.observed


async def test_state_info_receipts_retire_without_waiting_for_server_response(wire: _Harness) -> None:
    for message_id in range(12):
        await wire.sender._handle_state_forgotten(TLMessage(message_id, 0, MsgsStateReq([1])))
        state = await wire.sender._send_queue.next_state()
        owner = wire.sender.require_owned(state).owner
        await wire.sender._prepare_state(state)
        assert wire.sender.write_packet(wire.connection._send_queue.get_nowait(), wire.connection._send)
        await asyncio.gather(*wire.sender._observing)
        assert state not in wire.sender._owned_states
        assert (owner, None) in wire.authority.observed
    assert not wire.sender._owned_states
    assert not wire.sender._pending_state
    assert len(wire.authority.observed) == 12


async def test_ack_ownership_is_bounded_by_vendor_retention_and_replay_keeps_owner(wire: _Harness) -> None:
    wire.start_writer()
    for message_id in range(12):
        wire.sender._pending_ack.add(message_id)
        sending = asyncio.create_task(wire.sender._send_loop())
        wire.tasks.append(sending)
        await asyncio.wait_for(wire.writer.written.get(), 1)
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        await asyncio.gather(*wire.sender._observing)
    assert len(wire.sender._last_acks) == 10
    assert set(wire.sender._owned_states) == set(wire.sender._last_acks)
    assert len(wire.authority.observed) == 12
    last_ack = wire.sender._last_acks[-1]
    owner = wire.sender.require_owned(last_ack).owner
    assert last_ack.msg_id is not None
    await wire.sender._handle_bad_server_salt(TLMessage(99, 0, BadServerSalt(last_ack.msg_id, 0, 48, 9)))
    assert await wire.sender._send_queue.next_state() is last_ack
    assert wire.sender.require_owned(last_ack).owner is owner
    await wire.sender._prepare_state(last_ack)
    await asyncio.wait_for(wire.writer.written.get(), 1)
    await asyncio.gather(*wire.sender._observing)
    assert len(wire.authority.owners) == 12
    assert len(wire.authority.claims) == 13
    assert len(wire.authority.observed) == 13


async def test_plain_sender_rejects_raw_unclassified_and_subclass_requests(wire: _Harness) -> None:
    class ForgedAuthentication(ReqPqMultiRequest):
        pass

    plain = GuardedMTProtoPlainSender(wire.sender)
    for request in (b"raw auth", GetConfigRequest(), MsgsAck([1]), ForgedAuthentication(1)):
        with pytest.raises(RuntimeError, match="unclassified"):
            await plain.send(request)
    assert not wire.authority.owners
    assert not wire.writer.frames


@pytest.mark.parametrize(
    "auth_request",
    [
        ReqPqMultiRequest(1),
        ReqDHParamsRequest(1, 2, b"p", b"q", 3, b"encrypted"),
        SetClientDHParamsRequest(1, 2, b"encrypted"),
    ],
)
async def test_vendor_plain_frame_requires_final_claim_and_preserves_frozen_body(
    wire: _Harness, auth_request: object
) -> None:
    plain = GuardedMTProtoPlainSender(wire.sender)
    sending = asyncio.create_task(plain.send(auth_request))
    wire.tasks.append(sending)
    packet = await asyncio.wait_for(wire.connection._send_queue.get(), 1)
    assert packet.state.origin == "auth"
    assert not wire.authority.claims
    assert not wire.writer.frames
    assert wire.sender.write_packet(packet, wire.connection._send)
    assert wire.authority.claims == [packet.state.owner]
    raw_frame = wire.writer.frames[-1][8:-4]
    key_id, message_id, body_length = struct.unpack("<qqi", raw_frame[:20])
    assert key_id == 0
    assert message_id > 0
    assert body_length == len(packet.state.body)
    assert raw_frame[20:] == packet.state.body
    assert wire.sender._state._sequence == 0
    response = bytes(ResPQ(1, 2, b"pq", []))
    wire.connection._recv_queue.put_nowait((struct.pack("<qqi", 0, 4, len(response)) + response, None))
    result = await asyncio.wait_for(sending, 1)
    assert isinstance(result, ResPQ)
    assert result.nonce == 1
    assert wire.authority.observed == [(packet.state.owner, None)]
    assert not wire.sender._owned_states


@pytest.mark.parametrize("revocation", ["cancel", "generation"])
async def test_authentication_revocation_prevents_first_write(wire: _Harness, revocation: str) -> None:
    plain = GuardedMTProtoPlainSender(wire.sender)
    sending = asyncio.create_task(plain.send(ReqPqMultiRequest(1)))
    wire.tasks.append(sending)
    packet = await asyncio.wait_for(wire.connection._send_queue.get(), 1)
    initial_id = plain._state._last_msg_id
    if revocation == "cancel":
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
    else:
        wire.authority.generation += 1
    assert not wire.sender.write_packet(packet, wire.connection._send)
    assert plain._state._last_msg_id == initial_id
    assert wire.authority.released == [packet.permit]
    assert not wire.writer.frames
    assert wire.sender._state._sequence == 0
    assert not wire.authority.protected


async def test_authentication_proxy_rejects_replaced_body(wire: _Harness) -> None:
    plain = GuardedMTProtoPlainSender(wire.sender)
    sending = asyncio.create_task(plain.send(ReqPqMultiRequest(1)))
    wire.tasks.append(sending)
    packet = await asyncio.wait_for(wire.connection._send_queue.get(), 1)
    frame = struct.pack("<qqi", 0, 4, len(packet.state.body)) + packet.state.body
    forged = frame[:-1] + bytes([frame[-1] ^ 1])
    with pytest.raises(RuntimeError, match="differs"):
        await plain._connection.send(forged)
    assert not wire.authority.claims
    assert not wire.writer.frames


async def test_admission_expiry_reaches_caller_without_protecting_healthy_transport(wire: _Harness) -> None:
    state = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is state
    wire.authority.prepare_error = TimeoutError("admission expired")
    wire.sender._offer_state(state)
    with pytest.raises(TimeoutError, match="admission expired"):
        await wire.futures[-1]
    await asyncio.gather(*wire.sender._preparing, return_exceptions=True)
    assert not wire.authority.protected
    assert not wire.writer.frames
    assert not wire.authority.claims


async def test_delayed_auth_header_uses_final_claim_clock_and_same_vendor_id_state(
    wire: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wall_clock = [1_800_000_000.0]
    monkeypatch.setattr(mtprotostate, "time", SimpleNamespace(time=lambda: wall_clock[0]))
    plain = GuardedMTProtoPlainSender(wire.sender)
    ids: list[int] = []
    for _ in range(2):
        sending = asyncio.create_task(plain.send(ReqPqMultiRequest(1)))
        wire.tasks.append(sending)
        packet = await asyncio.wait_for(wire.connection._send_queue.get(), 1)
        frozen_body = packet.state.body
        initial_id = plain._state._last_msg_id
        assert isinstance(initial_id, int)
        wall_clock[0] += 360
        assert wire.authority.now == 0.0
        assert wire.sender.write_packet(packet, wire.connection._send)
        raw_frame = wire.writer.frames[-1][8:-4]
        _, message_id, _ = struct.unpack("<qqi", raw_frame[:20])
        assert message_id >> 32 == int(wall_clock[0])
        assert message_id > initial_id
        assert message_id % 4 == 0
        assert plain._state._last_msg_id == message_id
        assert raw_frame[20:] == frozen_body
        assert wire.sender._state._sequence == 0
        ids.append(message_id)
        response = bytes(ResPQ(1, 2, b"pq", []))
        wire.connection._recv_queue.put_nowait((struct.pack("<qqi", 0, 4, len(response)) + response, None))
        await asyncio.wait_for(sending, 1)
    assert ids[1] > ids[0]


async def test_auth_denial_resumes_before_encrypted_sender_loop_exists(
    wire: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wall_clock = [1_800_000_000.0]
    monkeypatch.setattr(mtprotostate, "time", SimpleNamespace(time=lambda: wall_clock[0]))
    plain = GuardedMTProtoPlainSender(wire.sender)
    sending = asyncio.create_task(plain.send(ReqPqMultiRequest(1)))
    wire.tasks.append(sending)
    first = await asyncio.wait_for(wire.connection._send_queue.get(), 1)
    initial_id = plain._state._last_msg_id
    wire.authority.generation += 1
    wall_clock[0] += 360
    assert not wire.sender.write_packet(first, wire.connection._send)
    assert plain._state._last_msg_id == initial_id
    assert not wire.writer.frames
    retry = await asyncio.wait_for(wire.connection._send_queue.get(), 1)
    assert retry.state is first.state
    assert retry.state.owner is first.state.owner
    assert not wire.sender._send_queue._deque
    wall_clock[0] += 360
    assert wire.sender.write_packet(retry, wire.connection._send)
    raw_frame = wire.writer.frames[-1][8:-4]
    _, final_id, _ = struct.unpack("<qqi", raw_frame[:20])
    assert final_id >> 32 == int(wall_clock[0])
    assert raw_frame[20:] == first.state.body
    response = bytes(ResPQ(1, 2, b"pq", []))
    wire.connection._recv_queue.put_nowait((struct.pack("<qqi", 0, 4, len(response)) + response, None))
    assert isinstance(await asyncio.wait_for(sending, 1), ResPQ)
    assert len(wire.authority.owners) == 1
    assert len(wire.authority.claims) == 2
    assert wire.authority.released == [first.permit]
    assert len(wire.writer.frames) == 1
    assert wire.sender._state._sequence == 0


async def test_auth_retry_admission_expiry_settles_plain_sender(wire: _Harness) -> None:
    plain = GuardedMTProtoPlainSender(wire.sender)
    sending = asyncio.create_task(plain.send(ReqPqMultiRequest(1)))
    wire.tasks.append(sending)
    first = await asyncio.wait_for(wire.connection._send_queue.get(), 1)
    initial_id = plain._state._last_msg_id
    wire.authority.generation += 1
    wire.authority.prepare_error = TimeoutError("admission expired")
    assert not wire.sender.write_packet(first, wire.connection._send)
    with pytest.raises(TimeoutError, match="admission expired"):
        await asyncio.wait_for(sending, 1)
    assert not wire.authority.protected
    assert not wire.writer.frames
    assert plain._state._last_msg_id == initial_id
    assert wire.sender._state._sequence == 0


async def test_flood_blocks_pregranted_queued_owner_before_async_observation(wire: _Harness) -> None:
    first = wire.enqueue()
    first_packet = await wire.prepare(first)
    assert wire.sender.write_packet(first_packet, wire.connection._send)
    assert len(wire.writer.frames) == 1
    second = wire.enqueue()
    assert await wire.sender._send_queue.next_state() is second
    await wire.sender._prepare_state(second)
    assert wire.connection._send_queue.full()
    assert second.msg_id is None
    second_owner = wire.sender.require_owned(second).owner
    wire.authority.receipt_ready.clear()
    # Writer is runnable before the result callback. No event-loop turn separates
    # the server's error delivery from the writer's chance to consume Queue(1).
    wire.start_writer()
    flood = FloodWaitError(GetConfigRequest(), capture=30)
    first.future.set_exception(flood)
    assert wire.authority.terminal_notices == [(first_packet.state.owner, flood)]
    assert not wire.authority.receipt_started.is_set()
    released = await asyncio.wait_for(wire.authority.deferred.get(), 1)
    assert released.owner is second_owner
    assert len(wire.writer.frames) == 1
    assert second.msg_id is None
    assert wire.sender._state._sequence == 1
    assert wire.codec._send_counter == 1
    assert isinstance(second_owner, _Owner)
    assert second_owner.scope is not None
    assert second_owner.scope.attempt_budget is not None
    assert second_owner.scope.attempt_budget.attempts == 0
    assert not wire.authority.observed
    assert not wire.futures[0].done()
    wire.authority.receipt_ready.set()
    with pytest.raises(FloodWaitError) as caught:
        await wire.futures[0]
    assert caught.value is flood


async def test_raw_result_reaches_paused_authority_before_publication(wire: _Harness) -> None:
    state = wire.enqueue()
    owner = wire.sender.require_owned(state).owner
    result = ResPQ(1, 2, b"known result", [])
    wire.authority.receipt_ready.clear()
    state.future.set_result(result)
    await asyncio.wait_for(wire.authority.receipt_started.wait(), 1)
    assert wire.authority.observation_inputs == [(owner, None, result)]
    assert wire.authority.observation_inputs[0][2] is result
    assert not wire.futures[-1].done()
    assert not wire.authority.observed
    wire.authority.receipt_ready.set()
    assert await wire.futures[-1] is result


async def test_known_result_policy_rejection_is_published_without_transport_protection(wire: _Harness) -> None:
    state = wire.enqueue()
    result = ResPQ(1, 2, b"known result", [])
    rejection = RuntimeError("result account rejected")
    wire.authority.result_rejection = rejection
    state.future.set_result(result)
    with pytest.raises(RuntimeError, match="result account rejected") as caught:
        await wire.futures[-1]
    assert caught.value is rejection
    assert state.future.result() is result
    assert wire.authority.observation_inputs[0][2] is result
    assert not wire.authority.protected
    assert not wire.authority.terminal_notices
    assert state not in wire.sender._owned_states


async def test_vendor_rpc_error_ack_ownership_stays_within_retention_window(wire: _Harness) -> None:
    for response_id in range(12):
        state = wire.enqueue()
        packet = await wire.prepare(state)
        assert wire.sender.write_packet(packet, wire.connection._send)
        await wire.sender._handle_rpc_result(
            TLMessage(response_id + 100, 0, RpcResult(state.msg_id, None, RpcError(400, "BAD_REQUEST")))
        )
        with pytest.raises(RPCError):
            await wire.futures[-1]
        ack = await wire.sender._send_queue.next_state()
        assert isinstance(ack.request, MsgsAck)
        await wire.sender._prepare_state(ack)
        assert wire.sender.write_packet(wire.connection._send_queue.get_nowait(), wire.connection._send)
        await asyncio.gather(*wire.sender._observing)
        assert len(wire.sender._owned_states) <= 10
    assert len(wire.sender._last_acks) == 10
    assert set(wire.sender._owned_states) == set(wire.sender._last_acks)
    assert len(wire.authority.observed) == 24


@pytest.mark.parametrize("had_receive_error", [False, True])
async def test_terminal_disconnect_fences_even_without_pending_requests_and_retains_failure(
    wire: _Harness, monkeypatch: pytest.MonkeyPatch, had_receive_error: bool
) -> None:
    terminal_error = OSError("receive failure") if had_receive_error else None
    close_error = OSError("socket close unconfirmed")
    calls = 0

    async def fail_close() -> None:
        nonlocal calls
        calls += 1
        assert wire.authority.terminations == [terminal_error]
        raise close_error

    monkeypatch.setattr(wire.connection, "disconnect", fail_close)
    assert not wire.sender._pending_state
    try:
        for _ in range(2):
            with pytest.raises(OSError) as caught:
                await wire.sender._disconnect(error=terminal_error)
            assert caught.value is (terminal_error or close_error)
        assert calls == 1
        assert wire.authority.terminations == [terminal_error]
    finally:
        monkeypatch.undo()


async def test_overlapping_terminal_disconnect_survives_caller_cancellation(
    wire: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    original = wire.connection.disconnect

    async def held_close() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(wire.connection, "disconnect", held_close)
    first = asyncio.create_task(wire.sender._disconnect())
    wire.tasks.append(first)
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(wire.sender._disconnect())
    wire.tasks.append(second)
    first.cancel()
    await _event_loop_turn()
    assert calls == 1
    assert not second.done()
    assert wire.authority.terminations == [None]
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, 1)
    assert calls == 1
    assert wire.sender._connection is None
    monkeypatch.undo()


async def test_internal_receive_loop_terminal_disconnect_does_not_join_itself(wire: _Harness) -> None:
    async def receive_loop() -> None:
        wire.sender._recv_loop_handle = asyncio.current_task()
        await wire.sender._disconnect()

    receiving = asyncio.create_task(receive_loop())
    wire.tasks.append(receiving)
    await asyncio.wait_for(receiving, 1)
    assert wire.authority.terminations == [None]
    assert wire.sender._connection is None


async def test_connection_pause_for_reconnect_is_not_terminal_sender_disconnect(wire: _Harness) -> None:
    await wire.connection.disconnect()
    assert not wire.authority.terminations
