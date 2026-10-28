"""Pinned Telethon adapter carrying owned requests to the final write boundary."""

from __future__ import annotations

import asyncio
import io
import struct
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal, Never, Protocol, cast

from telethon import version  # type: ignore[import-untyped]
from telethon.errors import SecurityError  # type: ignore[import-untyped]
from telethon.extensions.messagepacker import MessagePacker  # type: ignore[import-untyped]
from telethon.network import authenticator  # type: ignore[import-untyped]
from telethon.network.connection.tcpfull import ConnectionTcpFull  # type: ignore[import-untyped]
from telethon.network.mtprotoplainsender import MTProtoPlainSender  # type: ignore[import-untyped]
from telethon.network.mtprotosender import MTProtoSender  # type: ignore[import-untyped]
from telethon.network.requeststate import RequestState as VendorRequestState  # type: ignore[import-untyped]
from telethon.tl.core.messagecontainer import MessageContainer  # type: ignore[import-untyped]
from telethon.tl.core.tlmessage import TLMessage  # type: ignore[import-untyped]
from telethon.tl.functions import PingRequest, ReqDHParamsRequest, ReqPqMultiRequest, SetClientDHParamsRequest  # type: ignore[import-untyped]
from telethon.tl.tlobject import TLRequest  # type: ignore[import-untyped]
from telethon.tl.types import MsgsAck, MsgsStateInfo  # type: ignore[import-untyped]

from .telegram_rpc_scheduler import TelegramRpcScope

type ProtocolOrigin = Literal["ack", "ping", "state_info", "auth"]


class _RequestState(Protocol):
    request: object
    data: bytes
    future: asyncio.Future[object]
    msg_id: int | None
    container_id: int | None
    after: _RequestState | None


class TransportAuthority(Protocol):
    """Policy remains in the existing gate; the adapter only owns wire state."""

    def register_application(self, request: object, body: bytes, scope: TelegramRpcScope) -> object: ...

    def register_protocol(self, request: object, body: bytes, origin: ProtocolOrigin) -> object: ...

    async def prepare_write(self, owner: object) -> object: ...

    def claim_write(self, owner: object, permit: object) -> bool: ...

    def observe_write_return(self, owner: object) -> None: ...

    def defer_write(self, owner: object, permit: object) -> None: ...

    async def observe_result(
        self, owner: object, error: BaseException | None, result: object = None
    ) -> BaseException | None: ...

    def protect_transport(self, error: BaseException) -> None: ...

    def transport_terminated(self, error: BaseException | None) -> BaseException | None: ...

    def observe_error_now(self, owner: object, error: BaseException) -> None: ...


class _OwnedFuture(asyncio.Future[object]):
    """Observe terminal errors before scheduled callbacks can allow another write."""

    def __init__(self, authority: TransportAuthority, owner: object) -> None:
        super().__init__()
        self._authority = authority
        self._owner = owner

    def set_exception(self, exception: BaseException | type[BaseException]) -> None:
        if not self.done():
            error = exception() if isinstance(exception, type) else exception
            self._authority.observe_error_now(self._owner, error)
        super().set_exception(exception)

    def cancel(self, msg: object = None) -> bool:
        if not self.done():
            self._authority.observe_error_now(self._owner, asyncio.CancelledError())
        return super().cancel(msg)


@dataclass(slots=True)
class _OwnedState:
    state: _RequestState
    owner: object
    body: bytes
    request_type: type[object]
    result: asyncio.Future[object] | None = None
    queued: bool = False
    claims: int = 0
    origin: ProtocolOrigin | None = None
    retired: bool = False
    plain_frame_validated: bool = False
    plain_message_id: Callable[[], int] | None = None


@dataclass(slots=True)
class _OwnedPacket:
    sender: GuardedMTProtoSender
    state: _OwnedState
    permit: object
    released: bool = False


class _OwnedPacker(MessagePacker):
    """Reuse the vendor queue without serializing or spending sequence numbers."""

    def __init__(self, sender: GuardedMTProtoSender) -> None:
        super().__init__(sender._state, loggers=sender._loggers)
        self._sender = sender

    def append(self, state: _RequestState) -> None:
        self._sender.require_owned(state)
        super().append(state)

    def extend(self, states: Iterable[_RequestState]) -> None:
        for state in tuple(states):
            self.append(state)

    async def next_state(self) -> _RequestState:
        while not self._deque:
            self._ready.clear()
            await self._ready.wait()
        return cast(_RequestState, self._deque.popleft())

    async def get(self) -> Never:
        raise RuntimeError("owned transport requires single-state frames")


class GuardedConnectionTcpFull(ConnectionTcpFull):
    """The existing bounded connection queue carries owned frames, never raw bytes."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        cast(Callable[..., None], super().__init__)(*args, **kwargs)
        self._owned_epoch = 0
        self._owned_putters: set[asyncio.Task[None]] = set()
        self._owned_sender: GuardedMTProtoSender | None = None

    def bind_owner(self, sender: GuardedMTProtoSender) -> None:
        previous = getattr(self, "_owned_sender", None)
        if previous is not None and previous is not sender:
            raise RuntimeError("connection transport owner changed")
        self._owned_sender = sender

    async def connect(self, timeout: float | None = None, ssl: object = None) -> None:
        self._owned_epoch = getattr(self, "_owned_epoch", 0) + 1
        self._owned_putters.clear()
        await super().connect(timeout=timeout, ssl=ssl)

    async def send(self, data: object) -> None:
        packet = data
        if not isinstance(packet, _OwnedPacket) or packet.sender is not getattr(self, "_owned_sender", None):
            raise RuntimeError("unowned Telegram transport payload")
        if not self._connected:
            raise ConnectionError("owned Telegram transport is disconnected")
        epoch = self._owned_epoch
        putter = asyncio.create_task(self._send_queue.put(packet))
        self._owned_putters.add(putter)
        try:
            await putter
        except asyncio.CancelledError:
            if epoch != self._owned_epoch or not self._connected:
                raise ConnectionError("owned Telegram connection closed during enqueue") from None
            raise
        finally:
            self._owned_putters.discard(putter)
        if epoch != self._owned_epoch or not self._connected:
            raise ConnectionError("owned Telegram connection changed during enqueue")

    async def _send_loop(self) -> None:
        try:
            while self._connected:
                packet = await self._send_queue.get()
                if not isinstance(packet, _OwnedPacket) or packet.sender is not self._owned_sender:
                    raise RuntimeError("unowned Telegram transport queue entry")
                writer = self._writer
                if writer is None:
                    raise ConnectionError("owned Telegram writer is unavailable")
                if packet.sender.write_packet(packet, self._send):
                    await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        self._owned_epoch = getattr(self, "_owned_epoch", 0) + 1
        putters = tuple(self._owned_putters)
        for task in putters:
            task.cancel()
        await super().disconnect()
        if putters:
            await asyncio.gather(*putters, return_exceptions=True)
        while not self._send_queue.empty():
            packet = self._send_queue.get_nowait()
            if isinstance(packet, _OwnedPacket):
                packet.sender.defer_packet(packet)


class GuardedMTProtoSender(MTProtoSender):
    """Bind every vendor request state before packing, including internal requeues."""

    _ping: int | None

    def __init__(self, auth_key: object, *, authority: TransportAuthority, **kwargs: object) -> None:
        if version.__version__ != "1.45.0":
            raise RuntimeError("unsupported Telethon transport contract")
        cast(Callable[..., None], super().__init__)(auth_key, **kwargs)
        self._authority = authority
        self._owned_states: dict[_RequestState, _OwnedState] = {}
        self._preparing: set[asyncio.Task[None]] = set()
        self._observing: set[asyncio.Task[None]] = set()
        self._settling: asyncio.Task[None] | None = None
        self._terminal_teardown: asyncio.Task[None] | None = None
        self._send_queue = _OwnedPacker(self)
        self._control_origin: ContextVar[tuple[asyncio.Task[object] | None, ProtocolOrigin] | None] = ContextVar(
            "telegram_protocol_origin", default=None
        )

    @property
    def write_generation(self) -> int:
        return self._connection._owned_epoch if isinstance(self._connection, GuardedConnectionTcpFull) else 0

    def physical_connected(self) -> bool:
        return isinstance(self._connection, GuardedConnectionTcpFull) and bool(self._connection._connected)

    def application_owner(self, result: asyncio.Future[object]) -> object:
        for owned in self._owned_states.values():
            if owned.result is result:
                return owned.owner
        raise RuntimeError("Telegram application result has no owned attempt")

    async def settle_shutdown(self) -> None:
        """Caller cancellation cannot abandon queued preparation or observers."""
        if self._settling is None or self._settling.done():
            self._settling = asyncio.create_task(self._settle_shutdown_owned())
        task = self._settling
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _settle_shutdown_owned(self) -> None:
        preparing = tuple(self._preparing)
        for task in preparing:
            task.cancel()
        await asyncio.gather(*preparing, return_exceptions=True)
        for state in tuple(self._owned_states):
            if not state.future.done():
                state.future.cancel()
        turn = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(turn.set_result, None)
        await turn
        primary: BaseException | None = None
        while self._observing:
            batch = tuple(self._observing)
            outcomes = await asyncio.gather(*batch, return_exceptions=True)
            self._observing.difference_update(batch)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    primary = primary or outcome
        if primary is not None:
            raise primary

    async def connect(self, connection: object) -> bool:
        if not isinstance(connection, GuardedConnectionTcpFull):
            raise RuntimeError("unsupported Telegram connection type")
        if self._terminal_teardown is not None:
            if not self._terminal_teardown.done():
                raise ConnectionError("Telegram terminal teardown is still owned")
            self._terminal_teardown.result()
            self._terminal_teardown = None
        connection.bind_owner(self)
        return await super().connect(connection)

    async def _disconnect(self, error: BaseException | None = None) -> None:
        if self._terminal_teardown is None:
            caller = asyncio.current_task()
            # Vendor terminal teardown can originate inside these loops. Its
            # separate owner must not cancel/join the loop awaiting that owner.
            for name in ("_recv_loop_handle", "_send_loop_handle"):
                if getattr(self, name) is caller:
                    setattr(self, name, None)
                    assert caller is not None
                    caller.add_done_callback(self._terminal_loop_done)
            terminal_error = self._authority.transport_terminated(error) or error
            self._terminal_teardown = asyncio.create_task(self._disconnect_terminal_owned(terminal_error))
        task = self._terminal_teardown
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _terminal_loop_done(task: asyncio.Task[object]) -> None:
        if not task.cancelled():
            task.exception()

    async def _disconnect_terminal_owned(self, error: BaseException | None) -> None:
        primary = error
        try:
            await super()._disconnect(error)
        except BaseException as failure:  # noqa: BLE001 - keep terminal ownership through all cleanup.
            primary = primary or failure
        try:
            await self.settle_shutdown()
        except BaseException as failure:  # noqa: BLE001 - preserve the first terminal failure.
            primary = primary or failure
        if primary is not None:
            raise primary

    def send(self, request: object, ordered: bool = False) -> asyncio.Future[object]:
        raise RuntimeError("fresh Telegram application state requires gate ownership")

    def send_owned(self, request: object, scope: TelegramRpcScope) -> asyncio.Future[object]:
        if not self._user_connected:
            raise ConnectionError("owned Telegram transport is disconnected")
        state = cast(_RequestState, VendorRequestState(request))
        owner = self._authority.register_application(request, state.data, scope)
        owned = self._remember(state, owner, application=True)
        self._send_queue.append(state)
        assert owned.result is not None
        return owned.result

    def _remember(
        self, state: _RequestState, owner: object, *, application: bool = False, origin: ProtocolOrigin | None = None
    ) -> _OwnedState:
        result = asyncio.get_running_loop().create_future() if application else None
        state.future = _OwnedFuture(self._authority, owner)
        owned = _OwnedState(state, owner, state.data, type(state.request), result, origin=origin)
        self._owned_states[state] = owned
        state.future.add_done_callback(lambda future: self._result_observed(state, future))
        return owned

    def require_owned(self, state: _RequestState) -> _OwnedState:
        owned = self._owned_states.get(state)
        if owned is None:
            origin = self._control_origin.get()
            if origin is None or origin[0] is not asyncio.current_task():
                raise RuntimeError("unowned Telegram request state")
            allowed = {"ack": MsgsAck, "ping": PingRequest, "state_info": MsgsStateInfo}
            if type(state.request) is not allowed.get(origin[1]):
                raise RuntimeError("unclassified Telegram protocol state")
            self._remember(
                state, self._authority.register_protocol(state.request, state.data, origin[1]), origin=origin[1]
            )
            owned = self._owned_states[state]
        if state.data != owned.body or type(state.request) is not owned.request_type:
            raise RuntimeError("owned Telegram request payload changed")
        return owned

    def _result_observed(self, state: _RequestState, future: asyncio.Future[object]) -> None:
        owned = self._owned_states.get(state)
        if owned is None:
            return
        task = asyncio.create_task(self._finish_result(owned, future))
        self._observing.add(task)
        task.add_done_callback(self._observation_done)

    async def _finish_result(self, owned: _OwnedState, future: asyncio.Future[object]) -> None:
        error = asyncio.CancelledError() if future.cancelled() else future.exception()
        self._retire_unsent_ping(owned, error)
        try:
            rejected = await self._authority.observe_result(
                owned.owner, error, None if error is not None else future.result()
            )
            if rejected is not None:
                error = rejected
        except Exception as persistence_error:
            self._authority.protect_transport(persistence_error)
            self._publish_owned_result(owned, future, error if error is not None else persistence_error)
            raise
        else:
            self._publish_owned_result(owned, future, error)
        finally:
            self._retire_completed_state(owned)

    def _retire_completed_state(self, owned: _OwnedState) -> None:
        if self._pending_state.get(owned.state.msg_id) is owned.state:
            self._pending_state.pop(owned.state.msg_id, None)
        if owned.origin != "ack" or owned.retired or owned.state not in self._last_acks:
            self._owned_states.pop(owned.state, None)

    def _retire_unsent_ping(self, owned: _OwnedState, error: BaseException | None) -> None:
        """An unwritten failed ping cannot keep the vendor keepalive pending."""
        if error is None or owned.origin != "ping" or owned.claims:
            return
        request = owned.state.request
        if isinstance(request, PingRequest) and self._ping == request.ping_id:
            self._ping = None

    @staticmethod
    def _publish_owned_result(owned: _OwnedState, future: asyncio.Future[object], error: BaseException | None) -> None:
        if owned.result is None or owned.result.done():
            return
        if isinstance(error, asyncio.CancelledError):
            owned.result.cancel()
        elif error is not None:
            owned.result.set_exception(error)
        else:
            owned.result.set_result(future.result())

    def _observation_done(self, task: asyncio.Task[None]) -> None:
        self._observing.discard(task)
        if not task.cancelled():
            task.exception()

    def _preparation_done(self, task: asyncio.Task[None]) -> None:
        self._preparing.discard(task)
        if not task.cancelled():
            task.exception()

    async def _prepare_state(self, state: _RequestState) -> None:
        owned = self.require_owned(state)
        packet: _OwnedPacket | None = None
        transferred = False
        connection = self._connection
        if not isinstance(connection, GuardedConnectionTcpFull):
            raise ConnectionError("owned Telegram connection is unavailable")
        try:
            permit = await self._authority.prepare_write(owned.owner)
            packet = _OwnedPacket(self, owned, permit)
            await connection.send(packet)
            transferred = True
        except ConnectionError:
            owned.queued = False
            if owned.origin == "auth":
                self._fail_preparation(owned, ConnectionError("owned authentication connection closed"))
                raise
        except asyncio.CancelledError as error:
            self._fail_preparation(owned, error)
            raise
        except Exception as error:
            self._fail_preparation(owned, error)
            raise
        finally:
            if packet is not None and not transferred:
                self.defer_packet(packet)

    @staticmethod
    def _fail_preparation(owned: _OwnedState, error: BaseException) -> None:
        owned.queued = False
        if isinstance(error, asyncio.CancelledError):
            if owned.claims == 0 and not owned.state.future.done():
                owned.state.future.cancel()
        elif not owned.state.future.done():
            owned.state.future.set_exception(error)

    def _offer_state(self, state: _RequestState) -> None:
        owned = self.require_owned(state)
        if owned.queued or state.future.done():
            return
        owned.queued = True
        task = asyncio.create_task(self._prepare_state(state))
        self._preparing.add(task)
        task.add_done_callback(self._preparation_done)

    def defer_packet(self, packet: _OwnedPacket) -> None:
        if packet.released:
            return
        packet.released = True
        packet.state.queued = False
        self._authority.defer_write(packet.state.owner, packet.permit)
        if packet.state.retired:
            self._owned_states.pop(packet.state.state, None)
            if not packet.state.state.future.done():
                packet.state.state.future.cancel()
            return
        if packet.state.result is not None and packet.state.result.cancelled():
            if packet.state.claims == 0 and not packet.state.state.future.done():
                packet.state.state.future.cancel()
            return
        if not packet.state.state.future.done():
            if packet.state.origin == "auth":
                self._offer_state(packet.state.state)
            else:
                self._send_queue.append(packet.state.state)

    def write_packet(self, packet: _OwnedPacket, write: Callable[[bytes], None]) -> bool:
        owned = packet.state
        if owned.retired or owned.state.future.done() or (owned.result is not None and owned.result.cancelled()):
            self.defer_packet(packet)
            return False
        if self.require_owned(owned.state) is not owned:
            raise RuntimeError("owned Telegram packet identity changed")
        if len(owned.body) + TLMessage.SIZE_OVERHEAD > MessageContainer.MAXIMUM_SIZE:
            self.defer_packet(packet)
            owned.state.future.set_exception(ValueError("Telegram request payload is too large"))
            return False
        if not self._claim_packet(packet):
            return False
        packet.released = True
        owned.claims += 1
        owned.queued = False
        try:
            self._write_owned_state(owned, write)
        except Exception as error:
            self._authority.protect_transport(error)
            if not owned.state.future.done():
                owned.state.future.set_exception(error)
            raise
        self._observe_write_return(owned.owner)
        return True

    def _observe_write_return(self, owner: object) -> None:
        try:
            self._authority.observe_write_return(owner)
        except Exception:  # noqa: BLE001 - passive telemetry must not change the completed write
            pass

    def _claim_packet(self, packet: _OwnedPacket) -> bool:
        owned = packet.state
        try:
            claimed = self._authority.claim_write(owned.owner, packet.permit)
        except Exception as error:  # noqa: BLE001 - reject the popped unclaimed packet and close authority safely.
            self._authority.protect_transport(error)
            if not owned.state.future.done():
                owned.state.future.set_exception(error)
            self.defer_packet(packet)
            return False
        if not claimed:
            self.defer_packet(packet)
            return False
        return True

    def _write_owned_state(self, owned: _OwnedState, write: Callable[[bytes], None]) -> None:
        state = owned.state
        if owned.origin == "auth":
            if not owned.plain_frame_validated or owned.plain_message_id is None:
                raise RuntimeError("owned authentication frame is missing")
            header = struct.pack("<qqi", 0, owned.plain_message_id(), len(owned.body))
            write(header + owned.body)
            return
        buffer = io.BytesIO()
        state.msg_id = self._state.write_data_as_message(
            buffer,
            owned.body,
            isinstance(state.request, TLRequest),
            after_id=state.after.msg_id if state.after else None,
        )
        state.container_id = None
        encrypted = self._state.encrypt_message_data(buffer.getvalue())
        if isinstance(state.request, TLRequest):
            self._pending_state[state.msg_id] = state
        write(encrypted)
        if owned.origin == "ack" and owned.claims == 1:
            # Vendor retention bounds replay of sent acknowledgements. Pending
            # obligations keep their existing queue ownership until first write.
            self._retain_ack(state)
        if owned.origin in {"ack", "state_info"}:
            result: asyncio.Future[object] = asyncio.get_running_loop().create_future()
            result.set_result(None)
            self._result_observed(state, result)

    def _retain_ack(self, state: _RequestState) -> None:
        if len(self._last_acks) == self._last_acks.maxlen:
            previous = self._owned_states.get(self._last_acks[0])
            if previous is not None:
                previous.retired = True
                if not previous.queued:
                    self._owned_states.pop(previous.state, None)
        self._last_acks.append(state)

    async def _send_loop(self) -> None:
        while self._user_connected and not self._reconnecting:
            if self._pending_ack:
                token = self._control_origin.set((asyncio.current_task(), "ack"))
                try:
                    ack = cast(_RequestState, VendorRequestState(MsgsAck(list(self._pending_ack))))
                    self._send_queue.append(ack)
                    self._pending_ack.clear()
                finally:
                    self._control_origin.reset(token)
            self._offer_state(await self._send_queue.next_state())

    def _keepalive_ping(self, rnd_id: int) -> None:
        if self._ping is not None:
            self._start_reconnect(None)
            return
        self._ping = rnd_id
        token = self._control_origin.set((asyncio.current_task(), "ping"))
        try:
            self._send_queue.append(cast(_RequestState, VendorRequestState(PingRequest(rnd_id))))
        finally:
            self._control_origin.reset(token)

    async def _try_gen_auth_key(self, attempt: int) -> bool:
        plain = GuardedMTProtoPlainSender(self)
        try:
            self.auth_key.key, self._state.time_offset = await authenticator.do_authentication(plain)
            if self._auth_key_callback is not None:
                await self._auth_key_callback(self.auth_key)
            return True
        except SecurityError, AssertionError:
            await asyncio.sleep(self._delay)
            return False

    async def _handle_rpc_result(self, message: object) -> None:
        token = self._control_origin.set((asyncio.current_task(), "ack"))
        try:
            await super()._handle_rpc_result(message)
        finally:
            self._control_origin.reset(token)

    async def _handle_state_forgotten(self, message: object) -> None:
        token = self._control_origin.set((asyncio.current_task(), "state_info"))
        try:
            await super()._handle_state_forgotten(message)
        finally:
            self._control_origin.reset(token)


class _AuthConnectionProxy:
    """Turn only the vendor authenticator's exact frozen plain request into a frame."""

    def __init__(self, sender: GuardedMTProtoSender, owned: _OwnedState, new_message_id: Callable[[], int]) -> None:
        self._sender = sender
        self._owned = owned
        self._new_message_id = new_message_id

    async def send(self, data: bytes) -> None:
        if len(data) != len(self._owned.body) + 20:
            raise RuntimeError("invalid owned authentication frame length")
        key_id, message_id, length = struct.unpack("<qqi", data[:20])
        if key_id != 0 or message_id <= 0 or length != len(self._owned.body) or data[20:] != self._owned.body:
            raise RuntimeError("authentication frame differs from owned request")
        self._owned.plain_frame_validated = True
        self._owned.plain_message_id = self._new_message_id
        await self._sender._prepare_state(self._owned.state)

    async def recv(self) -> bytes:
        connection = self._sender._connection
        if not isinstance(connection, GuardedConnectionTcpFull):
            raise ConnectionError("owned authentication connection unavailable")
        assert self._owned.result is not None
        receiving = asyncio.create_task(connection.recv())
        try:
            completed, _ = await asyncio.wait((receiving, self._owned.result), return_when=asyncio.FIRST_COMPLETED)
            if self._owned.result in completed:
                self._owned.result.result()
                raise RuntimeError("authentication completed without a response frame")
            return cast(bytes, receiving.result())
        finally:
            if not receiving.done():
                receiving.cancel()
            await asyncio.gather(receiving, return_exceptions=True)


class GuardedMTProtoPlainSender(MTProtoPlainSender):
    """Reuse the vendor authenticator and plain framing without a raw-send route."""

    def __init__(self, sender: GuardedMTProtoSender) -> None:
        self._sender = sender
        super().__init__(None, loggers=sender._loggers)

    async def send(self, request: object) -> object:
        if type(request) not in {ReqPqMultiRequest, ReqDHParamsRequest, SetClientDHParamsRequest}:
            raise RuntimeError("unclassified Telegram authentication request")
        state = cast(_RequestState, VendorRequestState(request))
        owner = self._sender._authority.register_protocol(request, state.data, "auth")
        owned = self._sender._remember(state, owner, application=True, origin="auth")
        self._connection = _AuthConnectionProxy(
            self._sender, owned, cast(Callable[[], int], self._state._get_new_msg_id)
        )
        assert owned.result is not None
        try:
            result = await super().send(request)
        except asyncio.CancelledError:
            if owned.claims:
                self._sender._authority.protect_transport(RuntimeError("authentication outcome is unknown"))
            state.future.cancel()
            raise
        except Exception as error:
            if not state.future.done():
                state.future.set_exception(error)
            await owned.result
            raise
        else:
            state.future.set_result(result)
            return await owned.result
