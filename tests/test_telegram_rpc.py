# pyright: reportAny=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import telethon
from aiolimiter import AsyncLimiter
from telethon import TelegramClient, functions, types
from telethon._updates import ChannelState, SessionState
from telethon._updates.messagebox import ENTRY_ACCOUNT, MessageBox
from telethon.crypto import AuthKey
from telethon.errors import (
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    NetworkMigrateError,
    PhoneMigrateError,
    ServerError,
    SlowModeWaitError,
    UserMigrateError,
)
from telethon.network.connection.tcpfull import FullPacketCodec
from telethon.requestiter import RequestIter
from telethon.sessions import MemorySession, StringSession
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.tlobject import TLRequest

from mcp_telegram.config import (
    FloodWaitConfig,
    McpTelegramConfig,
    RuntimeObservationConfig,
    StateConfig,
    TelegramRpcConfig,
    TelegramRpcSchedulerConfig,
)
from mcp_telegram.daemon import _connect_telegram, _SyncMainContext
from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.delta_sync import (
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
    DmDeletionReconciliationDemandAdapter,
    prepare_dm_deletion_reconciliation,
)
from mcp_telegram.event_handlers import EventHandlerManager
from mcp_telegram.flood import (
    FloodWaitAccumulator,
    FloodWaitKillSwitchPolicy,
    FloodWaitObservation,
    TelegramRpcThrottled,
)
from mcp_telegram.message_history.contracts import ForwardGapPage
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from mcp_telegram.sync_db import (
    _apply_migrations,
    ensure_sync_schema,
    load_account_cooldown_until_utc,
    load_rpc_safety_owners,
    load_rpc_safety_record,
    save_rpc_safety_record,
)
from mcp_telegram.telegram import create_client
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    current_demand_token,
    demand_context,
    demand_contract,
    transferred_demand_context,
)
from mcp_telegram.telegram_rpc import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcBudget,
    TelegramRpcGate,
    TelegramRpcSafetyStore,
    TelegramRpcSource,
    UnclassifiedTelegramRpcError,
    _MainSenderAdapter,
    _TransportBoundaryState,
    account_cooldown_deadline,
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind, ExecutionMode
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmission,
    RpcAdmissionClosedError,
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    RpcServiceClass,
    RpcTransportReadiness,
    TelegramRpcAdmissionScheduler,
    TelegramRpcScope,
)
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from sqlite_writer_helpers import event_manager, inline_writer
from tests.daemon_api_policy import make_daemon_api_policy
from tests.helpers import (
    LoudChannelProfilePort,
    LoudChatAvatarHistoryPort,
    LoudCommonChatsPort,
    LoudGroupProfilePort,
    LoudUserAvatarHistoryPort,
    LoudUserProfilePort,
    reset_rpc_process_policy,
)
from tests.history_enrollment_helpers import seed_full_history_enrollment
from tests.test_rpc_admission_observations import _Recorder


@dataclass(frozen=True, slots=True)
class _CircuitStatus:
    open: bool

    def detail(self) -> str:
        return "open-for-test"


def _TestRequest(value: object) -> TLRequest:
    """Build an actual vendor request with test-only, non-wire metadata."""
    request = GetConfigRequest()
    request._test_value = value  # type: ignore[attr-defined]
    return request


def _DelayedResolveRequest(value: int, resolved: asyncio.Event, release: asyncio.Event) -> TLRequest:
    request = _TestRequest(value)

    async def resolve(client: object, utils: object) -> None:
        del client, utils
        if value == 2:
            resolved.set()
            await release.wait()

    request.resolve = resolve  # type: ignore[method-assign]
    return request


def _is_test_request(request: object) -> bool:
    return isinstance(request, TLRequest) and hasattr(request, "_test_value")


class _Sender:
    def __init__(self, callback: Callable[[object], object | Awaitable[object]]) -> None:
        self._callback = callback
        self.calls = 0
        self.sent: list[object] = []
        self.scopes: list[TelegramRpcScope] = []

    def send(self, request: object, *, ordered: bool = False) -> Awaitable[object]:
        del ordered
        return self.send_with_scope(request, current_rpc_scope())

    def send_with_scope(self, request: object, scope: TelegramRpcScope) -> Awaitable[object]:
        self.calls += 1
        self.sent.append(request)
        self.scopes.append(scope)

        async def execute() -> object:
            result = self._callback(request)
            if inspect.isawaitable(result):
                return await result
            return result

        return execute()


class _RawFutureSender:
    """Controllable scalar Telethon sender seam with real response Futures."""

    def __init__(self) -> None:
        self.calls = 0
        self.futures: list[asyncio.Future[object]] = []
        self.requests: list[object] = []
        self.scopes: list[TelegramRpcScope] = []
        self.disconnect_calls = 0
        self._connected = True

    def send(self, request: object, *, ordered: bool = False) -> asyncio.Future[object]:
        del ordered
        return self.send_with_scope(request, current_rpc_scope())

    def send_with_scope(self, request: object, scope: TelegramRpcScope) -> asyncio.Future[object]:
        self.calls += 1
        self.requests.append(request)
        self.scopes.append(scope)
        future = asyncio.get_running_loop().create_future()
        self.futures.append(future)
        return future

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False


async def _cleanup_raw_future_gate(gate: TelegramRpcGate, sender: _RawFutureSender) -> None:
    """Release scripted response waiters before draining the real transport owner."""
    for future in sender.futures:
        if not future.done():
            future.cancel()
    await gate._disconnect_main_sender()
    await gate.close_rpc_scheduler()


class _BootstrapSender:
    def __init__(self) -> None:
        self.auth_key = AuthKey(bytes(range(256)))
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.sent: list[object] = []
        self.scopes: list[TelegramRpcScope] = []
        self._connected = False
        self._disconnected = asyncio.get_running_loop().create_future()
        self.connect_started: asyncio.Event | None = None
        self.connect_release: asyncio.Event | None = None
        self.disconnect_started: asyncio.Event | None = None
        self.disconnect_release: asyncio.Event | None = None
        self.disconnect_error: BaseException | None = None
        self.response: Callable[[object], object | Awaitable[object]] | None = None

    async def connect(self, _connection: object) -> bool:
        self.connect_calls += 1
        self._connected = True
        if self.connect_started is not None and self.connect_release is not None:
            self.connect_started.set()
            await self.connect_release.wait()
        return True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_started is not None and self.disconnect_release is not None:
            self.disconnect_started.set()
            await self.disconnect_release.wait()
        if self.disconnect_error is not None:
            raise self.disconnect_error
        self._connected = False
        if not self._disconnected.done():
            self._disconnected.set_result(None)

    def send(self, request: object, *, ordered: bool = False) -> Awaitable[object]:
        del ordered
        return self.send_with_scope(request, current_rpc_scope())

    def send_with_scope(self, request: object, scope: TelegramRpcScope) -> Awaitable[object]:
        self.sent.append(request)
        self.scopes.append(scope)
        if self.response is not None:
            result = self.response(request)
            if inspect.isawaitable(result):
                return result
            if isinstance(result, BaseException):
                future = asyncio.get_running_loop().create_future()
                future.set_exception(result)
                return future
            future = asyncio.get_running_loop().create_future()
            future.set_result(result)
            return future
        result = asyncio.get_running_loop().create_future()
        result.set_result(object())
        return result

    def is_connected(self) -> bool:
        return self._connected

    @property
    def disconnected(self) -> asyncio.Future[object]:
        return self._disconnected

    def _transport_connected(self) -> bool:
        return self._connected

    def _keepalive_ping(self, _random_id: int) -> None:
        return None


def _request_value(request: object) -> object:
    if hasattr(request, "_test_value"):
        return request._test_value
    return request


def _set_sender(
    gate: TelegramRpcGate,
    callback: Callable[[object], object | Awaitable[object]] | object,
) -> object:
    """Replace only the scripted response provider; keep the owned wire transport."""
    provider = (
        callback
        if hasattr(callback, "send")
        else _Sender(cast(Callable[[object], object | Awaitable[object]], callback))
    )
    gate._test_scripted_provider = provider
    gate._test_server.provider = provider
    return provider


def _pending_owned_states(gate: TelegramRpcGate) -> dict[object, object]:
    """Return real sender RequestStates still awaiting terminal delivery."""
    return {state: owned for state, owned in gate._main_sender._owned_states.items() if not state.future.done()}


@pytest.fixture(autouse=True)
def _reset_process_policy() -> None:
    reset_rpc_process_policy()


class _PagedRequestIter(RequestIter):
    def __init__(self, client: object, pages: list[list[int]]) -> None:
        super().__init__(client, limit=None)
        self._pages = pages
        self._page = 0

    async def _load_next_chunk(self) -> bool:
        page = await self.client(_TestRequest(self._page))
        self._page += 1
        assert self.buffer is not None
        self.buffer.extend(page)
        return self._page >= len(self._pages)


class _MemorySafetyStore:
    def __init__(self, auth_key_id: int | None) -> None:
        assert type(auth_key_id) is int
        self.owners: tuple[int | None, int | None] = (None, None)
        self.legacy_floor: float | None = None
        self.fail_next = False
        self.fail_floor_next = False
        self.block_floor_saves = False
        self.floor_save_started: asyncio.Event | None = None
        self.floor_save_release: asyncio.Event | None = None
        self.writes: list[str] = []
        self.raw = json.dumps(
            {
                "version": 1,
                "generation": "test-generation",
                "scope": {"stage": "verified", "dc_id": 2, "key_id": auth_key_id, "account_id": 1},
                "floor": {"boot_id": "test-boot", "until_monotonic": 0.0, "until_utc": 0.0, "remaining_seconds": 0.0},
                "intents": {},
                "protection": None,
                "recovery": None,
                "transition": None,
            },
            separators=(",", ":"),
        )

    async def save(self, expected: str | None, value: str) -> None:
        old_floor = float(json.loads(expected)["floor"]["until_utc"]) if expected is not None else 0.0
        new_floor = float(json.loads(value)["floor"]["until_utc"])
        if (
            self.block_floor_saves
            and new_floor > old_floor
            and self.floor_save_started is not None
            and self.floor_save_release is not None
        ):
            self.floor_save_started.set()
            await self.floor_save_release.wait()
        if self.fail_floor_next and new_floor > old_floor:
            self.fail_floor_next = False
            raise sqlite3.OperationalError("synthetic safety floor failure")
        if self.fail_next:
            self.fail_next = False
            raise sqlite3.OperationalError("synthetic safety storage failure")
        if self.raw != expected:
            raise sqlite3.OperationalError("synthetic safety CAS conflict")
        self.raw = value
        self.writes.append(value)

    def api(self) -> TelegramRpcSafetyStore:
        return TelegramRpcSafetyStore(
            load_record=lambda: self.raw,
            save_record=self.save,
            load_legacy_until_utc=lambda: self.legacy_floor,
            canonical_owners=lambda: self.owners,
        )


class _FastFiniteLimiter(AsyncLimiter):
    """The real finite token bucket with a fast test rate and acquisition count."""

    def __init__(self) -> None:
        super().__init__(100_000, 1.0)
        self.acquisitions = 0

    async def acquire(self, amount: float = 1) -> None:
        self.acquisitions += 1
        await super().acquire(amount)


class _FakeReader:
    async def readexactly(self, n: int) -> bytes:
        await asyncio.Future()
        raise AssertionError(f"unexpected read of {n} bytes")


class _OwnedWireWriter:
    def __init__(self, gate: TelegramRpcGate) -> None:
        self.gate = gate
        self.frames: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.frames.append(data)
        self.gate._test_wire_written()

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _ScriptedServer:
    """Feed scripted server outcomes into the actual owned RequestState future."""

    def __init__(self, gate: TelegramRpcGate) -> None:
        self.gate = gate
        self.provider: object = _Sender(_request_value)
        self.seen: set[int] = set()
        self.tasks: set[asyncio.Task[None]] = set()

    def written(self) -> None:
        sender = self.gate._main_sender
        candidates = [
            (state.msg_id, state, owned)
            for state, owned in sender._owned_states.items()
            if state.msg_id is not None
            and state.msg_id not in self.seen
            and owned.result is not None
            and owned.claims > 0
            and not state.future.done()
        ]
        if not candidates:
            return
        msg_id, state, owned = min(candidates, key=lambda row: cast(int, row[0]))
        self.seen.add(cast(int, msg_id))
        scope = cast(object, owned.owner).scope
        provider = self.provider
        try:
            send_with_scope = getattr(provider, "send_with_scope", None)
            response = (
                send_with_scope(state.request, scope) if callable(send_with_scope) else provider.send(state.request)
            )
        except BaseException as exc:  # noqa: BLE001 - preserve cancellation as the owned request outcome.
            state.future.set_exception(exc)
            return
        if inspect.isawaitable(response):
            task = asyncio.create_task(self._transfer(response, state.future))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        else:
            state.future.set_result(response)

    @staticmethod
    async def _transfer(response: Awaitable[object], target: asyncio.Future[object]) -> None:
        try:
            value = await response
        except BaseException as exc:  # noqa: BLE001 - transfer cancellation/error to the actual RequestState.
            if not target.done():
                target.set_exception(exc)
        else:
            if not target.done():
                target.set_result(value)


def _install_scripted_authenticator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub crypto only for this server; transport auth framing has its own suite."""
    from telethon.network import authenticator

    original = authenticator.do_authentication
    auth_key_generation = 0

    async def authenticate(plain: object) -> tuple[AuthKey, int]:
        nonlocal auth_key_generation
        sender = getattr(plain, "_sender", None)
        connection = getattr(sender, "_connection", None)
        server = getattr(connection, "_test_scripted_server", None)
        if not isinstance(server, _ScriptedServer):
            return await original(plain)
        assert sender is server.gate._main_sender
        assert connection in server.gate._test_connections
        assert isinstance(connection, GuardedConnectionTcpFull)
        auth_key_generation += 1
        return AuthKey(bytes((value + auth_key_generation) % 256 for value in range(256))), 0

    monkeypatch.setattr(authenticator, "do_authentication", authenticate)


@pytest.fixture(autouse=True)
def _scripted_authenticator(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scripted_authenticator(monkeypatch)


def _attach_fake_connection(gate: TelegramRpcGate, *, initially_ready: bool) -> None:  # noqa: PLR0915
    sender = gate._main_sender
    connections: list[GuardedConnectionTcpFull] = []

    def factory(*args: object, **kwargs: object) -> GuardedConnectionTcpFull:
        connection = GuardedConnectionTcpFull(*args, **kwargs)
        connection.bind_owner(cast(GuardedMTProtoSender, sender))
        connection._test_scripted_server = gate._test_server
        writer = _OwnedWireWriter(gate)
        connection._reader = cast(object, _FakeReader())
        connection._writer = cast(object, writer)
        connection._codec = FullPacketCodec(connection)
        original_disconnect = connection.disconnect
        connect_attempted = False
        provider_disconnect_attempted = False

        async def connect_transport(*, timeout: float | None = None, ssl: object = None) -> None:
            nonlocal connect_attempted
            del timeout, ssl
            provider = gate._test_scripted_provider
            connect = getattr(provider, "connect", None)
            if callable(connect):
                connect_attempted = True
                await cast(Awaitable[object], connect(connection))
            connection._connected = True
            loop = asyncio.get_running_loop()
            connection._send_task = loop.create_task(connection._send_loop())
            connection._recv_task = loop.create_task(connection._recv_loop())
            gate._test_tasks.extend((connection._send_task, connection._recv_task))

        async def disconnect_transport() -> None:
            nonlocal provider_disconnect_attempted
            provider = gate._test_scripted_provider
            disconnect = getattr(provider, "disconnect", None)
            was_connected = connection._connected
            failure: BaseException | None = None
            if (was_connected or connect_attempted) and not provider_disconnect_attempted and callable(disconnect):
                provider_disconnect_attempted = True
                try:
                    await cast(Awaitable[object], disconnect())
                except BaseException as exc:  # noqa: BLE001 - close the fake socket before re-raising.
                    failure = exc
            try:
                await original_disconnect()
            except BaseException as exc:  # noqa: BLE001 - retain disconnect failure after fake cleanup.
                failure = failure or exc
            if failure is not None:
                raise failure

        connection.connect = connect_transport  # type: ignore[method-assign]
        connection.disconnect = disconnect_transport  # type: ignore[method-assign]
        connections.append(connection)
        return connection

    gate._connection = factory
    gate._test_connections = connections
    if initially_ready:
        connection = factory(gate.session.server_address, gate.session.port, gate.session.dc_id, loggers=gate._log)
        connection._connected = True
        sender._connection = connection
        sender._user_connected = True
        gate._transport_state = _TransportBoundaryState.READY
        sender._send_loop_handle = asyncio.get_running_loop().create_task(sender._send_loop())
        connection._send_task = asyncio.get_running_loop().create_task(connection._send_loop())
        connection._recv_task = asyncio.get_running_loop().create_task(connection._recv_loop())
        gate._test_tasks.extend((sender._send_loop_handle, connection._send_task, connection._recv_task))
    else:
        sender._connection = None
        sender._user_connected = False
        gate._transport_state = _TransportBoundaryState.DISCONNECTED


def _gate(  # noqa: PLR0915
    status: _CircuitStatus | None = None,
    *,
    retry_delays: tuple[float, ...] = (),
    initially_ready: bool = True,
    safety_store: TelegramRpcSafetyStore | None = None,
) -> TelegramRpcGate:
    status = status or _CircuitStatus(open=False)
    session = MemorySession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    safety = _MemorySafetyStore(session.auth_key.key_id)
    gate = TelegramRpcGate(
        session,
        12345,
        "test-api-hash",
        rpc_budget=TelegramRpcBudget(100_000, 1.0),
        circuit_status=lambda: status,
        fallback_wait_seconds=60,
        cooldown_buffer_seconds=1.0,
        transient_retry_delays_seconds=retry_delays,
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=safety_store or safety.api(),
        flood_observer=lambda **_kwargs: None,
    )
    limiter = _FastFiniteLimiter()
    gate._limiter = limiter
    gate._admission_scheduler._limiter = limiter
    gate._test_safety_store = safety
    gate._test_minimum_spacing_seconds = 1 / 100_000
    gate._test_server = _ScriptedServer(gate)
    gate._test_scripted_provider = gate._test_server.provider
    gate._test_connections = []
    gate._test_tasks = []
    gate._test_admitted = []
    gate._test_server.original_written = gate._test_server.written

    def track_written() -> None:
        gate._test_server.original_written()

    gate._test_wire_written = track_written
    _attach_fake_connection(gate, initially_ready=initially_ready)
    _set_sender(gate, _request_value)
    gate._loop = None
    gate._request_retries = 0
    gate._raise_last_call_error = True
    gate._flood_waited_requests = {}
    gate._reconnect_event = asyncio.Event()
    gate._delivery_repository = None
    gate._delivery_callbacks = {}
    gate._active_delivery = None
    gate._connect_owner = None
    gate._connection_capability = None
    gate._connection_rpc_tasks = set()
    gate._transport_state = _TransportBoundaryState.READY if initially_ready else _TransportBoundaryState.DISCONNECTED
    gate._test_old_close_scheduler = gate.close_rpc_scheduler

    async def cleanup_fake_transport() -> None:
        tasks = list(gate._test_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        for connection in gate._test_connections:
            for task in (connection._send_task, connection._recv_task):
                if task is not None and not task.done():
                    task.cancel()
        sender_loop = gate._main_sender._send_loop_handle
        if sender_loop is not None and not sender_loop.done():
            sender_loop.cancel()
            tasks.append(sender_loop)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close_scheduler() -> None:
        try:
            await gate._test_old_close_scheduler()
        finally:
            cleanup = asyncio.create_task(cleanup_fake_transport())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            await cleanup

    gate.close_rpc_scheduler = close_scheduler  # type: ignore[method-assign]
    return gate


def _bootstrap_gate(status: _CircuitStatus | None = None) -> tuple[TelegramRpcGate, _BootstrapSender]:
    gate = _gate(status, initially_ready=False)
    sender = _BootstrapSender()
    _set_sender(gate, sender)
    gate._connect_owner = None
    gate._connection_capability = None
    gate._connection_rpc_tasks = set()
    gate._transport_state = _TransportBoundaryState.DISCONNECTED
    gate._updates_handle = None
    gate._keepalive_handle = None
    gate._use_ipv6 = False
    gate._proxy = None
    gate._local_addr = None
    gate._catch_up = False
    gate._message_box = SimpleNamespace(is_empty=lambda: False)
    gate._update_loop = _idle_bootstrap_task
    gate._keepalive_loop = _idle_bootstrap_task
    gate._log["telethon.client.telegrambaseclient"] = logging.getLogger(__name__)
    gate.session.process_entities = lambda _result: None
    gate.session.get_input_entity = lambda _entity: (_ for _ in ()).throw(ValueError())
    return gate, sender


async def _idle_bootstrap_task() -> None:
    await asyncio.Future()


async def _close_bootstrap_gate(gate: TelegramRpcGate) -> None:
    for task in (getattr(gate, "_updates_handle", None), getattr(gate, "_keepalive_handle", None)):
        if task is not None:
            task.cancel()
    await asyncio.gather(
        *(task for task in (getattr(gate, "_updates_handle", None), getattr(gate, "_keepalive_handle", None)) if task),
        return_exceptions=True,
    )
    await gate._sender.disconnect()
    await gate.close_rpc_scheduler()


async def _normal_migration_gate(
    monkeypatch: pytest.MonkeyPatch, delays: tuple[float, ...]
) -> tuple[TelegramRpcGate, _BootstrapSender]:
    gate, sender = _bootstrap_gate()
    await gate.connect()
    gate._transient_retry_delays = delays
    gate._authorized = True
    monkeypatch.setattr(
        TelegramRpcGate,
        "_config",
        SimpleNamespace(
            dc_options=[
                SimpleNamespace(id=dc, ip_address=f"127.0.0.{dc}", port=443, ipv6=False, cdn=False) for dc in (2, 3, 4)
            ]
        ),
    )
    sender.sent.clear()
    sender.scopes.clear()
    sender.connect_calls = sender.disconnect_calls = 0
    return gate, sender


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "delays",
        "outcomes",
        "bootstrap",
        "expected_dc",
        "expected_sends",
        "expected_business_attempts",
        "expected_child_attempts",
        "exhausted",
    ),
    [
        ((0.0,), [3, "ok"], [], 3, 4, 2, 2, False),
        ((0.0,), [2, "ok"], [], 2, 4, 2, 2, False),
        ((), [3], [], 2, 1, 1, 0, True),
        ((0.0,), ["transient", 3], [], 2, 2, 2, 0, True),
        ((0.0, 0.0), ["transient", 3, "ok"], [], 3, 5, 3, 2, False),
        ((0.0, 0.0), [3, "ok"], [4], 4, 5, 2, 3, False),
        ((0.0,), [3], [4], 3, 2, 1, 1, True),
        ((0.0,), [3], [3], 3, 2, 1, 1, True),
        ((0.0, 0.0), [3, "ok"], ["transient"], 3, 5, 2, 3, False),
        ((0.0, 0.0), [3], ["transient", 4], 3, 3, 1, 2, True),
    ],
)
async def test_normal_vendor_migration_uses_only_the_original_retry_remainder(  # noqa: PLR0913
    monkeypatch: pytest.MonkeyPatch,
    delays: tuple[float, ...],
    outcomes: list[int | str],
    bootstrap: list[int | str],
    expected_dc: int,
    expected_sends: int,
    expected_business_attempts: int,
    expected_child_attempts: int,
    exhausted: bool,
) -> None:
    gate, sender = await _normal_migration_gate(monkeypatch, delays)
    original_auth_key = sender.auth_key.key
    normal_results, bootstrap_results = iter(outcomes), iter(bootstrap)
    budget = RpcAttemptBudget(12)
    deadline = asyncio.get_running_loop().time() + 10

    def respond(request: object) -> object:
        # Telethon performs a real GetMe after a same-account redirect; keep
        # that vendor-owned bootstrap response valid while the scripted
        # outcomes below describe only the application and redirect attempts.
        if isinstance(request, functions.users.GetUsersRequest):
            return _bootstrap_login_response(request)
        outcome = next(normal_results) if _is_test_request(request) else next(bootstrap_results, "ok")
        if isinstance(outcome, int):
            return UserMigrateError(request, capture=outcome)
        if outcome == "transient":
            return ServerError(request, "fixture")
        return "ok"

    sender.response = respond
    try:
        with demand_context(DemandKind.MCP_REMOTE_ACQUISITION, deadline=deadline) as token:
            with rpc_attempt_budget(budget), rpc_scope(TelegramRpcSource.MCP_INTERACTIVE, deadline=deadline):
                if exhausted:
                    with pytest.raises(TelegramRpcAdmissionDeferred, match="redirect exhausted"):
                        await gate(_TestRequest("normal"))
                else:
                    assert await gate(_TestRequest("normal")) == "ok"
        assert gate.session.dc_id == expected_dc
        assert len(sender.sent) == expected_sends
        assert budget.attempts == expected_business_attempts
        assert token.attempt_evidence.actual_attempts == expected_business_attempts + expected_child_attempts
        assert len(sender.scopes) == expected_sends
        business_scopes = [scope for scope in sender.scopes if scope.source is TelegramRpcSource.MCP_INTERACTIVE]
        child_scopes = [
            scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP
        ]
        assert len(business_scopes) == expected_business_attempts
        assert len(child_scopes) == expected_child_attempts
        assert all(scope.attempt_budget is budget and scope.deadline == deadline for scope in business_scopes)
        assert all(scope.attempt_evidence is token.attempt_evidence for scope in sender.scopes)
        assert all(scope.deadline == deadline for scope in child_scopes)
        if child_scopes:
            child_budget = child_scopes[0].attempt_budget
            assert child_budget is not None and child_budget.limit == 16
            assert all(scope.attempt_budget is child_budget for scope in child_scopes)
        if expected_dc == 2 and exhausted:
            assert sender.connect_calls == sender.disconnect_calls == 0
            assert gate.session.auth_key.key == original_auth_key
        assert gate._connect_owner is None
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_normal_migration_business_budget_does_not_reset_for_connection_bootstrap(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    gate, sender = await _normal_migration_gate(monkeypatch, (0.0,))
    original_auth_key = sender.auth_key.key
    config = gate._config
    monkeypatch.setattr(TelegramRpcGate, "_config", None)
    sender.response = lambda request: (
        UserMigrateError(request, capture=3)
        if _is_test_request(request)
        else (_bootstrap_login_response(request) if isinstance(request, functions.users.GetUsersRequest) else config)
    )
    budget = RpcAttemptBudget(limit)
    deadline = asyncio.get_running_loop().time() + 10
    try:
        with demand_context(DemandKind.MCP_REMOTE_ACQUISITION, deadline=deadline) as token:
            with rpc_attempt_budget(budget), rpc_scope(TelegramRpcSource.MCP_INTERACTIVE, deadline=deadline):
                expected_error = RpcAttemptBudgetExhaustedError if limit == 1 else TelegramRpcAdmissionDeferred
                with pytest.raises(expected_error) as caught:
                    await gate(_TestRequest("normal"))
        if limit == 2:
            assert "redirect exhausted" in str(caught.value)
        assert gate.session.dc_id == 3
        assert gate.session.auth_key.key != original_auth_key
        assert len(sender.sent) == limit + 3
        assert budget.attempts == limit
        assert token.attempt_evidence.actual_attempts == limit + 3
        business_scopes = [scope for scope in sender.scopes if scope.source is TelegramRpcSource.MCP_INTERACTIVE]
        child_scopes = [
            scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP
        ]
        assert len(business_scopes) == limit
        assert len(child_scopes) == 3
        assert all(scope.attempt_budget is budget and scope.deadline == deadline for scope in business_scopes)
        assert all(scope.attempt_evidence is token.attempt_evidence for scope in sender.scopes)
        assert all(scope.deadline == deadline for scope in child_scopes)
        child_budget = child_scopes[0].attempt_budget
        assert child_budget is not None and child_budget.limit == 16
        assert all(scope.attempt_budget is child_budget for scope in child_scopes)
        assert gate._connect_owner is None
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PhoneMigrateError, NetworkMigrateError])
async def test_normal_authorized_terminal_migration_does_not_switch(
    monkeypatch: pytest.MonkeyPatch, error: type[PhoneMigrateError] | type[NetworkMigrateError]
) -> None:
    gate, sender = await _normal_migration_gate(monkeypatch, (0.0, 0.0))
    sender.response = lambda request: error(request, capture=3)
    try:
        with pytest.raises(error):
            await _call(gate, _TestRequest("normal"))
        assert len(sender.sent) == 1
        assert gate.session.dc_id == 2
        assert sender.connect_calls == sender.disconnect_calls == 0
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_normal_migration_lookup_failure_preserves_old_transport_and_reservation_blocks_foreign_switch(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    gate, sender = await _normal_migration_gate(monkeypatch, (0.0,))
    original_auth_key = sender.auth_key.key
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(_client: TelegramClient, _dc: int, *, cdn: bool = False) -> object:
        del cdn
        entered.set()
        await release.wait()
        raise ValueError("lookup failed")

    monkeypatch.setattr(TelegramClient, "_get_dc", lookup)
    sender.response = lambda request: (
        UserMigrateError(request, capture=3) if _is_test_request(request) else _bootstrap_login_response(request)
    )
    caller = asyncio.create_task(_call(gate, _TestRequest("normal")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(TelegramRpcAdmissionDeferred, match="already running"):
            await _call(gate, _TestRequest("foreign migration"))
        if cancel:
            caller.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await caller
        assert sender.connect_calls == sender.disconnect_calls == 0
        assert len(sender.sent) == 2
        assert gate.session.dc_id == 2 and gate.session.auth_key.key == original_auth_key
        assert gate._transport_state is _TransportBoundaryState.READY
        assert gate._connect_owner is None
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain_cleanup", [False, True])
@pytest.mark.parametrize("mutation", ["set_dc", "save"])
async def test_normal_migration_postlookup_mutation_failure_uses_existing_cleanup(
    monkeypatch: pytest.MonkeyPatch, uncertain_cleanup: bool, mutation: str
) -> None:
    gate, sender = await _normal_migration_gate(monkeypatch, (0.0,))
    sender.response = lambda request: (
        UserMigrateError(request, capture=3) if _is_test_request(request) else _bootstrap_login_response(request)
    )
    original_set_dc = gate.session.set_dc

    def mutate_then_fail(dc: int, address: str, port: int) -> None:
        original_set_dc(dc, address, port)
        raise ValueError("session mutation failed")

    def fail_save() -> None:
        raise ValueError("session save failed")

    if mutation == "set_dc":
        gate.session.set_dc = mutate_then_fail
    else:
        gate.session.save = fail_save
    if uncertain_cleanup:
        sender.disconnect_error = OSError("unconfirmed cleanup")
    try:
        with pytest.raises(TelegramRpcAdmissionDeferred if uncertain_cleanup else ValueError):
            await _call(gate, _TestRequest("normal"))
        assert gate.session.dc_id == 3
        assert gate._connection_mutation_started
        assert sender.disconnect_calls == 1
        assert len(sender.sent) == 1
        assert (gate._connect_owner is not None) == uncertain_cleanup
        assert gate._transport_state is (
            _TransportBoundaryState.FAILED if uncertain_cleanup else _TransportBoundaryState.DISCONNECTED
        )
    finally:
        sender.disconnect_error = None
        if uncertain_cleanup:
            with pytest.raises(OSError, match="unconfirmed cleanup"):
                await _close_bootstrap_gate(gate)
            with pytest.raises(OSError, match="unconfirmed cleanup"):
                await gate.close_rpc_scheduler()
        else:
            await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["retry_delay", "cooldown"])
async def test_normal_migration_reserves_before_delay_or_cooldown_and_cancels_without_mutation(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import mcp_telegram.telegram_rpc as rpc

    gate, sender = await _normal_migration_gate(monkeypatch, (1.0 if phase == "retry_delay" else 0.0,))
    waiting = asyncio.Event()

    async def wait(_delay: float) -> None:
        waiting.set()
        await asyncio.Future()

    def respond(request: object) -> object:
        if phase == "cooldown":
            rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 30
        return UserMigrateError(request, capture=3)

    sender.response = respond
    monkeypatch.setattr(rpc.asyncio, "sleep", wait)
    caller = asyncio.create_task(_call(gate, _TestRequest("normal")))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert gate._connect_owner is not None
        with pytest.raises(TelegramRpcAdmissionDeferred, match="already running"):
            await gate._switch_dc(4)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert gate.session.dc_id == 2
        assert sender.connect_calls == sender.disconnect_calls == 0
        assert len(sender.sent) == 1
        assert gate._connect_owner is None
    finally:
        reset_rpc_process_policy()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["deadline", "circuit", "cancel"])
async def test_normal_migration_rechecks_admission_after_lookup(monkeypatch: pytest.MonkeyPatch, blocked: str) -> None:
    gate, sender = await _normal_migration_gate(monkeypatch, (0.0,))
    original_lookup = TelegramClient._get_dc
    entered, release = asyncio.Event(), asyncio.Event()
    deadline = asyncio.get_running_loop().time() + 0.25

    async def lookup(client: TelegramClient, dc: int, *, cdn: bool = False) -> object:
        result = await original_lookup(client, dc, cdn=cdn)
        entered.set()
        await release.wait()
        if blocked == "circuit":
            gate._rpc_circuit_status = lambda: _CircuitStatus(open=True)
        elif blocked == "cancel":
            cast(asyncio.Task[object], asyncio.current_task()).cancel()
        return result

    async def call() -> object:
        with demand_context(DemandKind.MCP_REMOTE_ACQUISITION, deadline=deadline):
            with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE, deadline=deadline):
                return await gate(_TestRequest("normal"))

    monkeypatch.setattr(TelegramClient, "_get_dc", lookup)
    sender.response = lambda request: UserMigrateError(request, capture=3)
    caller = asyncio.create_task(call())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if blocked == "deadline":
            await asyncio.sleep(max(0.0, deadline - asyncio.get_running_loop().time()) + 0.001)
        release.set()
        expected = {
            "deadline": RpcAdmissionExpiredError,
            "circuit": TelegramRpcThrottled,
            "cancel": asyncio.CancelledError,
        }[blocked]
        with pytest.raises(expected):
            await caller
        assert gate.session.dc_id == 2
        assert sender.connect_calls == sender.disconnect_calls == 0
        assert gate._connect_owner is None
        assert not gate._connection_mutation_started
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_normal_migration_rechecks_mutable_budget_after_postlookup_cooldown_wait(  # noqa: PLR0914, PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_telegram.telegram_rpc as rpc

    gate, sender = await _normal_migration_gate(monkeypatch, (0.0,))
    original_lookup = TelegramClient._get_dc
    waiting, release = asyncio.Event(), asyncio.Event()
    budget = RpcAttemptBudget(2)
    deadline = asyncio.get_running_loop().time() + 60
    config = gate._config
    original_auth_key = gate.session.auth_key.key
    token: object | None = None

    async def lookup(client: TelegramClient, dc: int, *, cdn: bool = False) -> object:
        result = await original_lookup(client, dc, cdn=cdn)
        rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 30
        return result

    async def wait(_delay: float) -> None:
        waiting.set()
        await release.wait()

    async def call() -> object:
        nonlocal token
        with demand_context(DemandKind.MCP_REMOTE_ACQUISITION, deadline=deadline) as demand:
            token = demand
            with rpc_attempt_budget(budget), rpc_scope(TelegramRpcSource.MCP_INTERACTIVE, deadline=deadline):
                return await gate(_TestRequest("normal"))

    monkeypatch.setattr(TelegramClient, "_get_dc", lookup)
    monkeypatch.setattr(rpc.asyncio, "sleep", wait)
    sender.response = lambda request: (
        UserMigrateError(request, capture=3)
        if _is_test_request(request)
        else (_bootstrap_login_response(request) if isinstance(request, functions.users.GetUsersRequest) else config)
    )
    caller = asyncio.create_task(call())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        budget.debit()
        reset_rpc_process_policy()
        release.set()
        with pytest.raises(RpcAttemptBudgetExhaustedError):
            await caller
        assert len(sender.sent) == 3
        assert budget.attempts == 2
        assert gate.session.dc_id == 3 and gate.session.auth_key.key != original_auth_key
        assert gate._connection_mutation_started
        assert token is not None
        evidence = token.attempt_evidence
        assert evidence.actual_attempts == 3
        business_scopes = [scope for scope in sender.scopes if scope.source is TelegramRpcSource.MCP_INTERACTIVE]
        child_scopes = [
            scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP
        ]
        assert len(business_scopes) == 1
        assert len(child_scopes) == 2
        assert all(scope.attempt_budget is budget for scope in business_scopes)
        effective_deadline = token.admission_deadline
        assert all(scope.deadline == effective_deadline for scope in sender.scopes)
        assert all(scope.attempt_evidence is evidence for scope in sender.scopes)
        child_budget = child_scopes[0].attempt_budget
        assert child_budget is not None and child_budget.limit == 16
        assert all(scope.attempt_budget is child_budget for scope in child_scopes)
        assert gate._connect_owner is None
    finally:
        reset_rpc_process_policy()
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await _close_bootstrap_gate(gate)


async def _ignore_cooldown(_deadline: float) -> None:
    pass


@dataclass
class _SelfOriginDifference:
    gate: TelegramRpcGate
    sender: _BootstrapSender
    request: object
    mode: str
    box: MessageBox
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    replacement_entered: asyncio.Event = field(default_factory=asyncio.Event)
    reply: object = field(default_factory=object)
    first_rpc: bool = True
    budget: RpcAttemptBudget = field(default_factory=lambda: RpcAttemptBudget(8))
    deadline: float | None = None

    def respond(self, request: object) -> object:  # noqa: PLR0911
        if type(request) is type(self.request):
            if self.first_rpc:
                self.first_rpc = False
                return UserMigrateError(request, capture=3)
            return self.reply
        if self.mode in ("bootstrap_error", "cleanup_error"):
            if self.mode == "cleanup_error":
                self.sender.disconnect_error = OSError("fixture unconfirmed cleanup")
            return ServerError(request, "fixture bootstrap failure")
        if self.mode in ("cancel", "shutdown"):
            return self.hold_bootstrap()
        if isinstance(request, functions.help.GetConfigRequest):
            return self.gate._config
        if isinstance(request, functions.users.GetUsersRequest):
            return _bootstrap_login_response(request)
        return object()

    async def hold_bootstrap(self) -> object:
        self.entered.set()
        await self.release.wait()
        return object()

    async def replacement_loop(self) -> None:
        self.replacement_entered.set()
        await asyncio.Future()

    async def call(self) -> object:
        deadline = asyncio.get_running_loop().time() + 10
        self.deadline = deadline
        with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
            with (
                rpc_attempt_budget(self.budget),
                rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline),
            ):
                return await self.gate(self.request)


async def _prepare_self_origin_difference(
    patch: pytest.MonkeyPatch, method: str, mode: str
) -> tuple[_SelfOriginDifference, asyncio.Task[object]]:
    gate, sender = await _normal_migration_gate(patch, (0.0,))
    idle = gate._updates_handle
    assert idle is not None
    idle.cancel()
    await asyncio.gather(idle, return_exceptions=True)
    box = MessageBox(logging.getLogger(__name__))
    box.load(SessionState(99, 2, False, 10, 3, 1_700_000_000, 1, None), [ChannelState(123, 21)])
    box.try_begin_get_diff(ENTRY_ACCOUNT if method == "account" else 123, "fixture in-flight difference")
    gate._message_box, gate._catch_up = box, True
    request = (
        functions.updates.GetDifferenceRequest(pts=10, date=datetime.now(UTC), qts=3)
        if method == "account"
        else functions.updates.GetChannelDifferenceRequest(
            channel=types.InputChannel(123, 456), filter=types.ChannelMessagesFilterEmpty(), pts=21, limit=10
        )
    )
    probe = _SelfOriginDifference(gate, sender, request, mode, box)
    sender.response, gate._update_loop = probe.respond, probe.replacement_loop
    if mode == "keepalive_error":
        gate._keepalive_handle.cancel()
        await asyncio.gather(gate._keepalive_handle, return_exceptions=True)
        gate._keepalive_handle = asyncio.create_task(_failing_keepalive())
        await asyncio.sleep(0)
    origin = asyncio.create_task(probe.call())
    gate._updates_handle = origin
    return probe, origin


async def _failing_keepalive() -> None:
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        raise ValueError("fixture keepalive failure") from None


async def _exercise_self_origin_difference(method: str, mode: str) -> None:  # noqa: PLR0915
    patch = pytest.MonkeyPatch()
    _install_scripted_authenticator(patch)
    probe, origin = await _prepare_self_origin_difference(patch, method, mode)
    gate, sender = probe.gate, probe.sender
    snapshot, in_flight = probe.box.session_state(), set(probe.box.getting_diff_for)
    try:
        if mode in ("cancel", "shutdown"):
            await probe.entered.wait()
            assert gate._updates_handle is origin and not origin.done()
            if mode == "cancel":
                origin.cancel()
            else:
                await gate._disconnect()
        if mode in ("ok", "keepalive_error"):
            assert await origin is probe.reply
            assert len(sender.sent) == 4
            assert probe.budget.attempts == 2
            business_scopes = [
                scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE
            ]
            child_scopes = [
                scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP
            ]
            assert len(business_scopes) == 2
            assert len(child_scopes) == 2
            assert all(scope.attempt_budget is probe.budget for scope in business_scopes)
            assert all(scope.deadline == probe.deadline for scope in sender.scopes)
            assert all(scope.attempt_evidence is sender.scopes[0].attempt_evidence for scope in sender.scopes)
            assert sender.scopes[0].attempt_evidence.actual_attempts == 4
            child_budget = child_scopes[0].attempt_budget
            assert child_budget is not None and child_budget.limit == 16
            assert all(scope.attempt_budget is child_budget for scope in child_scopes)
        else:
            expected = ServerError if mode == "bootstrap_error" else TelegramRpcAdmissionDeferred
            with pytest.raises(asyncio.CancelledError if mode in ("cancel", "shutdown") else expected):
                await origin
            assert len(sender.sent) == 2
        assert gate._updates_handle is origin and origin.done()
        assert gate._message_box is probe.box and probe.box.session_state() == snapshot
        assert probe.box.getting_diff_for == in_flight
        assert gate._catch_up and not probe.replacement_entered.is_set()
        assert (gate._connect_owner is not None) == (mode == "cleanup_error")
    finally:
        probe.release.set()
        sender.disconnect_error = None
        try:
            await _close_bootstrap_gate(gate)
        except OSError as exc:
            assert mode == "cleanup_error" and gate._transport_state is _TransportBoundaryState.FAILED
            assert str(exc) == "fixture unconfirmed cleanup"
            with pytest.raises(OSError, match="fixture unconfirmed cleanup"):
                await gate.close_rpc_scheduler()
        patch.undo()


@pytest.mark.parametrize("method", ["account", "channel"])
@pytest.mark.parametrize("mode", ["ok", "bootstrap_error", "cancel", "shutdown", "cleanup_error", "keepalive_error"])
def test_self_origin_difference_migration_is_bounded_without_cancel_cycle(method: str, mode: str) -> None:
    repo = Path(__file__).resolve().parents[1]
    script = (
        "import asyncio,os,sys,threading\n"
        "sys.path[:0]=sys.argv[1:4]\n"
        "from test_telegram_rpc import _exercise_self_origin_difference\n"
        "watchdog=threading.Timer(6,lambda:os._exit(124));watchdog.daemon=True;watchdog.start()\n"
        "asyncio.run(_exercise_self_origin_difference(sys.argv[4],sys.argv[5]))\n"
        "watchdog.cancel()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo), str(repo / "src"), str(repo / "tests"), method, mode],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, f"self-origin {method}/{mode}: {result.returncode}\n{result.stderr[-3000:]}"


async def _call(gate: TelegramRpcGate, request: object) -> object:
    request = request if isinstance(request, TLRequest) else _TestRequest(request)
    with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE):
        return await gate(request)


async def _wait_for(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


async def _assert_failed_gate_blocks_api_migration_and_ping(
    gate: TelegramRpcGate,
    forwarded_pings: list[int],
) -> None:
    with pytest.raises(TelegramRpcAdmissionDeferred):
        await _call(gate, _TestRequest("blocked"))
    with pytest.raises(TelegramRpcAdmissionDeferred):
        await gate._switch_dc(3)
    _MainSenderAdapter(gate)._keepalive_ping(1)
    assert forwarded_pings == []


@pytest.mark.asyncio
async def test_gate_is_real_telethon_subclass_and_direct_call_crosses_sender_proxy() -> None:
    gate = _gate()
    called: list[object] = []

    def send(request: object) -> object:
        called.append(_request_value(request))
        return "called"

    _set_sender(gate, send)
    assert isinstance(gate, TelegramClient)
    assert await _call(gate, "request") == "called"
    assert called == ["request"]
    assert gate._limiter.acquisitions == 1


@pytest.mark.asyncio
async def test_inherited_connect_admits_one_bootstrap_send_without_leaking_its_context() -> None:
    gate, sender = _bootstrap_gate()
    child_contexts: list[str] = []

    async def record_clean_context() -> None:
        with pytest.raises(Exception, match="no demand context"):
            current_demand_token()
        child_contexts.append("clean")

    gate._update_loop = record_clean_context
    gate._keepalive_loop = record_clean_context
    try:
        await gate.connect()
        await _wait_for(lambda: len(child_contexts) == 2)
        assert sender.connect_calls == 1
        assert len(sender.sent) == 1
        assert sender.scopes[0].source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP
        assert sender.scopes[0].demand_kind is DemandKind.TELETHON_CONNECTION_BOOTSTRAP
        assert sender.scopes[0].acquisition_kind is AcquisitionKind.CONNECTION_BOOTSTRAP
        assert gate._limiter.acquisitions == 1
        assert gate._connect_owner is None
        assert gate._connection_capability is None
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_inherited_connect_waits_for_a_restored_cooldown_before_its_first_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender = _bootstrap_gate()
    import mcp_telegram.telegram_rpc as rpc

    record = json.loads(gate._test_safety_store.raw)
    record["floor"].update(
        boot_id="test-boot",
        until_monotonic=rpc.time.monotonic() + 30,
        until_utc=rpc.time.time() + 30,
        remaining_seconds=30.0,
    )
    gate._test_safety_store.raw = json.dumps(record, separators=(",", ":"))
    gate._safety = None
    gate._ensure_safety_ledger()
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_cooldown(_delay: float) -> None:
        waiting.set()
        await release.wait()

    monkeypatch.setattr(rpc.asyncio, "sleep", wait_for_cooldown)
    caller = asyncio.create_task(gate.connect())
    await waiting.wait()
    assert sender.connect_calls == 0
    assert sender.sent == []
    record = json.loads(gate._test_safety_store.raw)
    record["floor"].update(until_monotonic=0.0, until_utc=0.0, remaining_seconds=0.0)
    gate._test_safety_store.raw = json.dumps(record, separators=(",", ":"))
    gate._safety = None
    gate._ensure_safety_ledger()
    rpc._COOLDOWN_DEADLINE = 0.0
    release.set()
    try:
        await caller
        assert len(sender.sent) == 1
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_inherited_connect_transfers_the_caller_deadline_and_attempt_budget() -> None:
    gate, sender = _bootstrap_gate()
    deadline = asyncio.get_running_loop().time() + 5
    budget = RpcAttemptBudget(2)
    try:
        with demand_context(DemandKind.MCP_REMOTE_ACQUISITION, deadline=deadline):
            with rpc_attempt_budget(budget):
                with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE, deadline=deadline) as caller_scope:
                    await gate.connect()
        assert sender.scopes[0].source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP
        assert sender.scopes[0].deadline == caller_scope.deadline
        assert sender.scopes[0].attempt_budget is not budget
        assert sender.scopes[0].attempt_budget.limit == 16
        assert sender.scopes[0].attempt_evidence is caller_scope.attempt_evidence
        assert caller_scope.attempt_budget is budget
        assert budget.attempts == 0
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_inherited_connect_admits_optional_get_me_and_get_state_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender = _bootstrap_gate()

    async def get_me(client: TelegramClient) -> object:
        await client(functions.users.GetUsersRequest([types.InputUserSelf()]))
        return types.User(1, bot=False, access_hash=11)

    async def on_login(client: TelegramClient, _me: object) -> None:
        await client(functions.updates.GetStateRequest())

    gate._message_box = SimpleNamespace(is_empty=lambda: True)
    sender.response = _bootstrap_login_response
    monkeypatch.setattr(TelegramClient, "get_me", get_me)
    monkeypatch.setattr(TelegramClient, "_on_login", on_login)
    try:
        await gate.connect()
        assert [type(request).__name__ for request in sender.sent] == [
            "InvokeWithLayerRequest",
            "GetUsersRequest",
            "GetStateRequest",
        ]
        assert all(scope.source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP for scope in sender.scopes)
        assert gate._limiter.acquisitions == 3
    finally:
        await _close_bootstrap_gate(gate)


def _bootstrap_login_response(request: object) -> object:
    if isinstance(request, functions.users.GetUsersRequest):
        return [types.User(1, bot=False, access_hash=11)]
    if isinstance(request, functions.updates.GetStateRequest):
        return types.updates.State(pts=1, qts=0, date=datetime.now(UTC), seq=1, unread_count=0)
    if isinstance(request, functions.updates.GetDifferenceRequest):
        return types.updates.DifferenceEmpty(date=datetime.now(UTC), seq=1)
    return object()


def _configure_bootstrap_dc_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_dc(_client: TelegramClient, dc_id: int, *, cdn: bool = False) -> object:
        del cdn
        return SimpleNamespace(id=dc_id, ip_address="127.0.0.2", port=443)

    monkeypatch.setattr(TelegramClient, "_get_dc", get_dc)


@pytest.mark.asyncio
async def test_real_telethon_login_bootstrap_admits_get_difference_with_inherited_update_root() -> None:
    gate, sender = _bootstrap_gate()
    loaded: list[object] = []
    budget = RpcAttemptBudget(4)
    deadline = asyncio.get_running_loop().time() + 5
    sender.response = _bootstrap_login_response
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda state, _channels: loaded.append(state))
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    try:
        with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline) as token:
            with rpc_attempt_budget(budget):
                with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
                    await gate.connect()
        assert [type(request).__name__ for request in sender.sent] == [
            "InvokeWithLayerRequest",
            "GetUsersRequest",
            "GetStateRequest",
            "GetDifferenceRequest",
        ]
        business_scopes = [
            scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE
        ]
        child_scopes = [
            scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP
        ]
        assert business_scopes == []
        assert len(child_scopes) == 4
        assert all(scope.deadline == token.admission_deadline for scope in sender.scopes)
        assert all(scope.attempt_evidence is token.attempt_evidence for scope in sender.scopes)
        child_budget = child_scopes[0].attempt_budget
        assert child_budget is not None and child_budget.limit == 16
        assert all(scope.attempt_budget is child_budget for scope in child_scopes)
        assert budget.attempts == 0
        assert token.attempt_evidence.actual_attempts == 4
        assert gate._limiter.acquisitions == 4
        assert len(loaded) == 1
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_nested_bootstrap_migration_flood_stops_with_original_update_scope() -> None:
    gate, sender = _bootstrap_gate()
    budget = RpcAttemptBudget(2)
    deadline = asyncio.get_running_loop().time() + 5
    gate._authorized = None
    sender.response = lambda request: (
        NetworkMigrateError(request, capture=3)
        if type(request).__name__ == "InvokeWithLayerRequest"
        else FloodWaitError(request, capture=20)
        if isinstance(request, functions.updates.GetStateRequest)
        else _bootstrap_login_response(request)
    )
    try:
        with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline) as token:
            with rpc_attempt_budget(budget):
                with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
                    with pytest.raises(TelegramRpcThrottled):
                        await gate.connect()
        assert [type(request).__name__ for request in sender.sent] == [
            "InvokeWithLayerRequest",
            "GetStateRequest",
        ]
        business_scopes = [
            scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE
        ]
        child_scopes = [
            scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP
        ]
        assert business_scopes == []
        assert len(child_scopes) == 2
        assert all(scope.deadline == token.admission_deadline for scope in sender.scopes)
        assert all(scope.attempt_evidence is token.attempt_evidence for scope in sender.scopes)
        child_budget = child_scopes[0].attempt_budget
        assert child_budget is not None and child_budget.limit == 16
        assert all(scope.attempt_budget is child_budget for scope in child_scopes)
        assert budget.attempts == 0
        assert token.attempt_evidence.actual_attempts == 2
        assert sender.disconnect_calls == 1
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_real_telethon_login_migration_unwinds_to_one_owner_redirect(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender = _bootstrap_gate()
    _configure_bootstrap_dc_lookup(monkeypatch)
    gate._transient_retry_delays = (0.0,)
    started: list[str] = []
    migrate_once = True
    budget = RpcAttemptBudget(7)
    deadline = asyncio.get_running_loop().time() + 5

    async def update_loop() -> None:
        started.append("update")
        await asyncio.Future()

    async def keepalive_loop() -> None:
        started.append("keepalive")
        await asyncio.Future()

    def respond(request: object) -> object:
        nonlocal migrate_once
        if isinstance(request, functions.updates.GetStateRequest) and migrate_once:
            migrate_once = False
            return UserMigrateError(request, capture=3)
        return _bootstrap_login_response(request)

    sender.response = respond
    gate._update_loop = update_loop
    gate._keepalive_loop = keepalive_loop
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda *_args: None)
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    try:
        with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline) as token:
            with rpc_attempt_budget(budget):
                with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
                    await gate.connect()
        await _wait_for(lambda: len(started) == 2)
        assert [type(request).__name__ for request in sender.sent] == [
            "InvokeWithLayerRequest",
            "GetUsersRequest",
            "GetStateRequest",
            "InvokeWithLayerRequest",
            "GetUsersRequest",
            "GetStateRequest",
            "GetDifferenceRequest",
        ]
        assert sender.connect_calls == 2
        assert sender.disconnect_calls == 1
        assert set(started) == {"update", "keepalive"}
        business_scopes = [
            scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE
        ]
        child_scopes = [
            scope for scope in sender.scopes if scope.source is TelegramRpcSource.TELETHON_CONNECTION_BOOTSTRAP
        ]
        assert business_scopes == []
        assert len(child_scopes) == 7
        assert all(scope.deadline == token.admission_deadline for scope in sender.scopes)
        assert all(scope.attempt_evidence is token.attempt_evidence for scope in sender.scopes)
        child_budget = child_scopes[0].attempt_budget
        assert child_budget is not None and child_budget.limit == 16
        assert all(scope.attempt_budget is child_budget for scope in child_scopes)
        assert budget.attempts == 0
        assert token.attempt_evidence.actual_attempts == len(sender.sent)
        assert gate._connection_rpc_tasks == set()
        assert gate._connect_owner is None
        assert gate._connection_capability is None
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_connection_migration_exhausts_existing_redirect_slots_without_extra_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender = _bootstrap_gate()
    _configure_bootstrap_dc_lookup(monkeypatch)
    gate._transient_retry_delays = (0.0,)
    sender.response = lambda request: (
        UserMigrateError(request, capture=3)
        if isinstance(request, functions.updates.GetStateRequest)
        else _bootstrap_login_response(request)
    )
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda *_args: None)
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    try:
        with pytest.raises(TelegramRpcAdmissionDeferred, match="redirect exhausted"):
            await gate.connect()
        assert [type(request).__name__ for request in sender.sent] == [
            "InvokeWithLayerRequest",
            "GetUsersRequest",
            "GetStateRequest",
            "InvokeWithLayerRequest",
            "GetUsersRequest",
            "GetStateRequest",
        ]
        assert sender.connect_calls == 2
        assert sender.disconnect_calls == 2
        assert gate._connection_rpc_tasks == set()
        assert gate._connect_owner is None
        assert gate._connection_capability is None
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_connection_migration_stops_before_redirect_when_circuit_opens() -> None:
    gate, sender = _bootstrap_gate()
    gate._transient_retry_delays = (0.0,)
    status = {"open": False}
    gate._rpc_circuit_status = lambda: _CircuitStatus(open=status["open"])

    def respond(request: object) -> object:
        if isinstance(request, functions.updates.GetStateRequest):
            status["open"] = True
            return UserMigrateError(request, capture=3)
        return _bootstrap_login_response(request)

    sender.response = respond
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda *_args: None)
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    try:
        with pytest.raises(TelegramRpcThrottled, match="open-for-test"):
            await gate.connect()
        assert [type(request).__name__ for request in sender.sent] == [
            "InvokeWithLayerRequest",
            "GetUsersRequest",
            "GetStateRequest",
        ]
        assert sender.connect_calls == 1
        assert sender.disconnect_calls == 1
        assert gate._connection_rpc_tasks == set()
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_connection_migration_cancellation_during_cooldown_cleans_owned_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender = _bootstrap_gate()
    gate._transient_retry_delays = (0.0,)
    import mcp_telegram.telegram_rpc as rpc

    waiting = asyncio.Event()

    async def wait_for_cooldown(_delay: float) -> None:
        waiting.set()
        await asyncio.Future()

    def respond(request: object) -> object:
        if isinstance(request, functions.updates.GetStateRequest):
            rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 30
            return UserMigrateError(request, capture=3)
        return _bootstrap_login_response(request)

    sender.response = respond
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda *_args: None)
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    monkeypatch.setattr(rpc.asyncio, "sleep", wait_for_cooldown)
    caller = asyncio.create_task(gate.connect())
    try:
        await waiting.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert len(sender.sent) == 3
        assert sender.disconnect_calls == 1
        assert gate._connection_rpc_tasks == set()
        assert gate._connect_owner is None
        assert gate._connection_capability is None
    finally:
        if not caller.done():
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_connection_migration_expiry_stops_before_redirect_with_original_budget() -> None:
    gate, sender = _bootstrap_gate()
    import mcp_telegram.telegram_rpc as rpc

    gate._transient_retry_delays = (0.0,)
    deadline = asyncio.get_running_loop().time() + 0.03
    budget = RpcAttemptBudget(3)

    def respond(request: object) -> object:
        if isinstance(request, functions.updates.GetStateRequest):
            rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 1
            return UserMigrateError(request, capture=3)
        return _bootstrap_login_response(request)

    sender.response = respond
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda *_args: None)
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    try:
        with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline) as token:
            with rpc_attempt_budget(budget):
                with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
                    with pytest.raises(RpcAdmissionExpiredError):
                        await gate.connect()
        assert len(sender.sent) == 3
        assert sender.connect_calls == 1
        assert sender.disconnect_calls == 1
        assert budget.attempts == 0
        assert token.attempt_evidence.actual_attempts == 3
        assert gate._connection_rpc_tasks == set()
        assert gate._connect_owner is None
        assert gate._connection_capability is None
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_real_telethon_login_get_difference_flood_stops_bootstrap_without_retry() -> None:
    gate, sender = _bootstrap_gate()
    sender.response = lambda request: (
        FloodWaitError(request=None, capture=30)
        if isinstance(request, functions.updates.GetDifferenceRequest)
        else _bootstrap_login_response(request)
    )
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda *_args: None)
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    try:
        with pytest.raises(TelegramRpcThrottled):
            await gate.connect()
        assert [type(request).__name__ for request in sender.sent] == [
            "InvokeWithLayerRequest",
            "GetUsersRequest",
            "GetStateRequest",
            "GetDifferenceRequest",
        ]
        assert sender.disconnect_calls == 1
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_real_telethon_login_get_difference_expiry_preserves_original_budget() -> None:
    gate, sender = _bootstrap_gate()
    import mcp_telegram.telegram_rpc as rpc

    budget = RpcAttemptBudget(4)
    deadline = asyncio.get_running_loop().time() + 0.03

    def expire_before_difference(request: object) -> object:
        if isinstance(request, functions.updates.GetStateRequest):
            rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 1
        return _bootstrap_login_response(request)

    sender.response = expire_before_difference
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda *_args: None)
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    try:
        with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline) as token:
            with rpc_attempt_budget(budget):
                with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
                    with pytest.raises(TelegramRpcAdmissionDeferred):
                        await gate.connect()
        assert [type(request).__name__ for request in sender.sent] == [
            "InvokeWithLayerRequest",
            "GetUsersRequest",
            "GetStateRequest",
        ]
        assert budget.attempts == 0
        assert token.attempt_evidence.actual_attempts == 3
        assert sender.disconnect_calls == 1
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_real_telethon_login_get_difference_cancellation_cleans_up_once() -> None:
    gate, sender = _bootstrap_gate()
    pending = asyncio.get_running_loop().create_future()
    sender.response = lambda request: (
        pending if isinstance(request, functions.updates.GetDifferenceRequest) else _bootstrap_login_response(request)
    )
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda *_args: None)
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    caller = asyncio.create_task(gate.connect())
    try:
        await _wait_for(lambda: len(sender.sent) == 4)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert sender.disconnect_calls == 1
        assert len(sender.sent) == 4
    finally:
        if not caller.done():
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_inherited_connect_stops_before_transport_when_circuit_is_open() -> None:
    gate, sender = _bootstrap_gate(_CircuitStatus(open=True))
    try:
        with pytest.raises(TelegramRpcThrottled):
            await gate.connect()
        assert sender.connect_calls == 0
        assert sender.sent == []
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_inherited_connect_cancellation_before_cooldown_expiry_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender = _bootstrap_gate()
    import mcp_telegram.telegram_rpc as rpc

    rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 30
    waiting = asyncio.Event()

    async def wait_for_cooldown(_delay: float) -> None:
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(rpc.asyncio, "sleep", wait_for_cooldown)
    caller = asyncio.create_task(gate.connect())
    await waiting.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert sender.connect_calls == 0
    assert sender.sent == []
    assert gate._connect_owner is None
    assert gate._connection_capability is None
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_inherited_connect_reserves_owner_while_waiting_for_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender = _bootstrap_gate()
    import mcp_telegram.telegram_rpc as rpc

    rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 30
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_cooldown(_delay: float) -> None:
        waiting.set()
        await release.wait()

    monkeypatch.setattr(rpc.asyncio, "sleep", wait_for_cooldown)
    caller = asyncio.create_task(gate.connect())
    await waiting.wait()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await gate.connect()
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
    rpc._COOLDOWN_DEADLINE = 0
    release.set()
    try:
        await caller
        assert sender.connect_calls == 1
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_inherited_connect_cancellation_cleans_unconfirmed_partial_transport() -> None:
    gate, sender = _bootstrap_gate()
    sender.connect_started = asyncio.Event()
    sender.connect_release = asyncio.Event()
    caller = asyncio.create_task(gate.connect())
    await sender.connect_started.wait()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await gate.connect()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert sender.connect_calls == 1
    assert sender.disconnect_calls == 1
    assert sender.sent == []
    assert gate._transport_state is _TransportBoundaryState.DISCONNECTED
    assert gate._connect_owner is None
    assert gate._connection_capability is None
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_foreign_dc_switch_defers_before_transport_or_session_mutation() -> None:
    gate, sender = _bootstrap_gate()
    sender.connect_started = asyncio.Event()
    sender.connect_release = asyncio.Event()
    caller = asyncio.create_task(gate.connect())
    await sender.connect_started.wait()
    session_before = (gate.session.dc_id, gate.session.server_address, gate.session.port, gate.session.auth_key)

    async def switch_from_equivalent_token() -> None:
        capability = gate._connection_capability
        assert capability is not None
        with transferred_demand_context(capability.token):
            await gate._switch_dc(3)

    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await asyncio.create_task(switch_from_equivalent_token())
    assert (gate.session.dc_id, gate.session.server_address, gate.session.port, gate.session.auth_key) == session_before
    assert sender.disconnect_calls == 0
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert gate._transport_state is _TransportBoundaryState.DISCONNECTED
    assert gate._connect_owner is None
    assert gate._connection_capability is None
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_inherited_connect_reservation_survives_failed_transport_cleanup() -> None:
    gate, sender = _bootstrap_gate()
    import mcp_telegram.telegram_rpc as rpc

    sender.response = lambda _request: FloodWaitError(request=None, capture=20)
    sender.disconnect_started = asyncio.Event()
    sender.disconnect_release = asyncio.Event()
    caller = asyncio.create_task(gate.connect())
    await sender.disconnect_started.wait()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await gate.connect()
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
    sender.disconnect_release.set()
    with pytest.raises(TelegramRpcThrottled):
        await caller
    rpc._COOLDOWN_DEADLINE = 0
    gate._flood_waited_requests.clear()
    sender.response = lambda _request: object()
    try:
        with pytest.raises(TelegramRpcAdmissionDeferred):
            await gate.connect()
        assert sender.connect_calls == 2
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_connection_owner_through_blocked_cleanup() -> None:
    gate, sender = _bootstrap_gate()
    sender.response = lambda _request: FloodWaitError(request=None, capture=20)
    sender.disconnect_started = asyncio.Event()
    sender.disconnect_release = asyncio.Event()
    caller = asyncio.create_task(gate.connect())
    await sender.disconnect_started.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await gate.connect()
    sender.disconnect_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert gate._connect_owner is None
    assert gate._connection_capability is None
    assert gate._connection_rpc_tasks == set()
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_failed_bootstrap_cleanup_keeps_reservation_and_chains_original_failure() -> None:
    gate, sender = _bootstrap_gate()
    sender.response = lambda _request: FloodWaitError(request=None, capture=20)
    sender.disconnect_error = OSError("transport teardown failed")
    with pytest.raises(TelegramRpcAdmissionDeferred, match="termination is unconfirmed") as caught:
        await gate.connect()
    assert isinstance(caught.value.__cause__, TelegramRpcThrottled)
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await gate.connect()
    with pytest.raises(OSError, match="transport teardown failed"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_retained_bootstrap_flood_is_recorded_during_cancelled_cleanup() -> None:
    gate, sender = _bootstrap_gate()
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    pending = asyncio.get_running_loop().create_future()
    sender.response = lambda request: (
        pending if isinstance(request, functions.updates.GetDifferenceRequest) else _bootstrap_login_response(request)
    )
    sender.disconnect_started = asyncio.Event()
    sender.disconnect_release = asyncio.Event()
    gate._message_box = SimpleNamespace(is_empty=lambda: True, load=lambda *_args: None)
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)
    caller = asyncio.create_task(gate.connect())
    try:
        await _wait_for(lambda: len(sender.sent) == 4)
        caller.cancel()
        await sender.disconnect_started.wait()
        caller.cancel()
        assert not pending.cancelled()
        assert gate._connect_owner is not None
        assert gate._connection_capability is not None
        pending.set_exception(FloodWaitError(request=None, capture=20))
        await _wait_for(lambda: accumulator.kill_switch_status().events_in_window == 1)
        assert account_cooldown_deadline() > asyncio.get_running_loop().time()
        sender.disconnect_release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert _pending_owned_states(gate) == {}
        assert gate._connection_rpc_tasks == set()
        assert gate._connect_owner is None
        assert gate._connection_capability is None
    finally:
        sender.disconnect_release.set()
        if not caller.done():
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        lambda gate: gate._call(object(), _TestRequest("private")),
        lambda gate: gate._borrow_exported_sender(2),
        lambda gate: gate._create_exported_sender(2),
        lambda gate: gate._get_cdn_client(object()),
    ],
)
async def test_unsupported_inherited_transport_paths_cannot_reach_the_raw_sender(
    operation: Callable[[TelegramRpcGate], Awaitable[object]],
) -> None:
    gate = _gate()
    sender = _set_sender(gate, _request_value)
    with pytest.raises(RuntimeError, match="unsupported|bypasses"):
        await operation(gate)
    assert sender.calls == 0


@pytest.mark.asyncio
async def test_helper_and_request_iter_pages_use_the_same_public_call_seam() -> None:
    gate = _gate()
    calls: list[object] = []

    def send(request: object) -> object:
        value = _request_value(request)
        calls.append(value)
        if isinstance(value, int):
            return [[1, 2], [3]][value]
        return [types.User(1, is_self=True, first_name="Test")]

    _set_sender(gate, send)
    gate._mb_entity_cache = SimpleNamespace(self_id=1)
    with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE):
        assert getattr(await gate.get_me(), "id", None) == 1
        iterator = _PagedRequestIter(gate, [[1, 2], [3]])
        assert [item async for item in iterator] == [1, 2, 3]
    assert calls[0].__class__.__name__ == "GetUsersRequest"
    assert calls[1:] == [0, 1]
    assert gate._limiter.acquisitions == 3


@pytest.mark.asyncio
async def test_get_entity_username_resolution_uses_the_same_public_call_seam() -> None:
    gate = _gate()
    calls: list[object] = []
    user = types.User(1, access_hash=2, username="alice")

    def send(request: object) -> object:
        calls.append(request)
        assert isinstance(request, functions.contacts.ResolveUsernameRequest)
        return types.contacts.ResolvedPeer(types.PeerUser(1), [], [user])

    _set_sender(gate, send)
    with rpc_scope(TelegramRpcSource.DIALOG_RESOLUTION):
        resolved = await gate.get_entity("alice")

    assert resolved is user
    assert len(calls) == 1
    assert gate._limiter.acquisitions == 1


def test_factory_uses_supplied_snapshot_without_loading_config_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = McpTelegramConfig(
        state=StateConfig(dir=tmp_path),
        flood_wait=FloodWaitConfig(fallback_wait_seconds=17, cooldown_buffer_seconds=2.5),
        telegram_rpc=TelegramRpcConfig(
            max_calls_per_period=7,
            period_seconds=13.0,
            transient_retry_delays_seconds=(0.0,),
        ),
    )
    monkeypatch.setattr(
        "mcp_telegram.telegram._load_settings", lambda: (_ for _ in ()).throw(AssertionError("reloaded"))
    )

    safety_store = _MemorySafetyStore(AuthKey(bytes(range(256))).key_id).api()
    gate = create_client.__wrapped__("1", "hash", session_name="snapshot", config=config, safety_store=safety_store)
    try:
        assert gate._limiter.max_rate == 7
        assert gate._limiter.time_period == 13.0
        assert gate._fallback_wait_seconds == 17
        assert gate._cooldown_buffer_seconds == 2.5
        assert gate._transient_retry_delays == (0.0,)
        assert gate._safety_store is safety_store
    finally:
        gate.session.close()


def test_factory_forwards_required_safety_store(tmp_path: Path) -> None:
    safety_store = _MemorySafetyStore(AuthKey(bytes(range(256))).key_id).api()
    gate = create_client.__wrapped__(
        "1",
        "hash",
        session_name="persistent-cooldown",
        config=McpTelegramConfig(state=StateConfig(dir=tmp_path)),
        safety_store=safety_store,
    )
    try:
        assert gate._safety_store is safety_store
    finally:
        gate.session.close()


def test_telethon_public_helper_and_update_loop_contract_is_pinned() -> None:
    from telethon.client.updates import UpdateMethods
    from telethon.client.users import UserMethods
    from telethon.tl.custom.message import Message

    assert telethon.__version__ == "1.45.0"
    assert "await self(" in inspect.getsource(TelegramClient.get_me)
    sender_source = inspect.getsource(Message.get_sender)
    assert "await self._client.get_entity" in sender_source
    update_source = inspect.getsource(UpdateMethods._update_loop)
    assert "diff = await self(get_diff)" in update_source
    assert "await self(get_diff)" in update_source
    call_source = inspect.getsource(UserMethods._call)
    assert tuple(inspect.signature(UserMethods._call).parameters) == (
        "self",
        "sender",
        "request",
        "ordered",
        "flood_sleep_threshold",
    )
    assert call_source.index("await r.resolve(self, utils)") < call_source.index(
        "future = sender.send(request, ordered=ordered)"
    )
    assert call_source.index("future = sender.send(request, ordered=ordered)") < call_source.index(
        "result = await future"
    )
    assert call_source.index("result = await future") < call_source.rindex("self.session.process_entities(result)")
    assert call_source.index("except (errors.ServerError") < call_source.index("await asyncio.sleep(2)")


@pytest.mark.asyncio
async def test_gate_blocks_when_circuit_is_open() -> None:
    gate = _gate(_CircuitStatus(open=True))
    with pytest.raises(TelegramRpcThrottled, match="open-for-test") as caught:
        await _call(gate, "request")
    assert caught.value.latched
    assert caught.value.retry_after_seconds is None
    assert gate._limiter.acquisitions == 0


@pytest.mark.asyncio
async def test_latched_circuit_precedes_disconnected_transport_deferral() -> None:
    gate = _gate(_CircuitStatus(open=True))
    sender = _set_sender(gate, _request_value)
    gate._transport_state = _TransportBoundaryState.DISCONNECTED

    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, "request")

    assert caught.value.latched is True
    assert sender.calls == 0
    assert gate._limiter.acquisitions == 0


@pytest.mark.asyncio
async def test_daemon_unobserved_topic_catalog_does_not_cross_latched_gate(tmp_path: Path) -> None:
    """Local catalog absence is readable without admission or a raw sender call."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    gate = _gate(_CircuitStatus(open=True))
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    check_calls = 0
    original_check = gate.check_circuit

    def check_circuit() -> None:
        nonlocal check_calls
        check_calls += 1
        original_check()

    gate.check_circuit = check_circuit  # type: ignore[method-assign]

    class GateClient:
        async def get_entity(self, dialog_id: int) -> object:
            return await gate(_TestRequest(dialog_id))

    server = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, GateClient()),
        asyncio.Event(),
        writer=inline_writer(conn),
        channel_profile_port=LoudChannelProfilePort(),
        group_profile_port=LoudGroupProfilePort(),
        user_profile_port=LoudUserProfilePort(),
        common_chats_port=LoudCommonChatsPort(),
        user_avatar_history_port=LoudUserAvatarHistoryPort(),
        chat_avatar_history_port=LoudChatAvatarHistoryPort(),
        policy=make_daemon_api_policy(),
    )
    server._ready = True
    try:
        response, _, _ = await server._handle_client_line(b'{"method":"list_topics","dialog_id":123}', "", None)
    finally:
        conn.close()
        await gate.close_rpc_scheduler()

    assert response["ok"] is True
    assert response["data"]["topics"] == []
    assert response["data"]["catalog"]["coverage"] == "not_observed"
    assert response["data"]["empty_reason"] == "topic_catalog_not_observed"
    assert check_calls == 0
    assert sender.calls == 0


@pytest.mark.asyncio
async def test_disconnected_transport_keeps_finite_admission_deferral_when_circuit_is_closed() -> None:
    gate = _gate()
    sender = _set_sender(gate, _request_value)
    gate._transport_state = _TransportBoundaryState.DISCONNECTED

    with pytest.raises(TelegramRpcAdmissionDeferred) as caught:
        await _call(gate, "request")

    assert caught.value.latched is False
    retry_after = caught.value.retry_after_seconds
    assert retry_after is not None
    assert retry_after > 0
    assert sender.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "batch_request",
    [[], (), set(), {}, range(2), (item for item in range(2))],
    ids=["list", "tuple", "set", "dict", "range", "generator"],
)
async def test_gate_rejects_transport_batches_before_admission(batch_request: object) -> None:
    gate = _gate()
    with pytest.raises(ValueError, match="transport batching.*sequential scalar calls"):
        await gate(batch_request)
    assert gate._limiter.acquisitions == 0


@pytest.mark.asyncio
async def test_gate_retries_transient_once_and_acquires_each_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate(retry_delays=(2.0,))
    attempts = 0
    sleeps: list[float] = []

    def send(_request: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ServerError(None, "temporary")
        return "ok"

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    _set_sender(gate, send)
    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", fake_sleep)
    assert await _call(gate, "request") == "ok"
    assert attempts == 2
    assert gate._limiter.acquisitions == 2
    assert sleeps == [2.0, 2.0]


@pytest.mark.asyncio
async def test_gate_default_retry_adds_no_sleep_beyond_telethon_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient retry has only Telethon's built-in 2s sleep by default."""
    gate = _gate(retry_delays=(0.0,))
    transient = ServerError(None, "temporary")
    final = FloodWaitError(request=None, capture=7)

    class _Sender:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, _request: object, *, ordered: bool = False) -> object:
            del ordered
            self.calls += 1

            async def _fail() -> None:
                raise transient if self.calls == 1 else final

            return _fail()

    sender = _set_sender(gate, _Sender())
    gate._loop = None
    gate._request_retries = 0
    gate._raise_last_call_error = True
    gate._flood_waited_requests = {}
    gate._no_updates = False
    gate._log = {"telethon.client.users": logging.getLogger(__name__)}
    gate.flood_sleep_threshold = 0
    gate.session.process_entities = lambda _result: None
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", fake_sleep)
    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, functions.PingRequest(1))

    assert caught.value.__cause__ is final
    assert caught.value.retry_after_seconds == 7
    assert sender.calls == 2
    assert gate._limiter.acquisitions == 2
    assert sleeps == [2]


@pytest.mark.asyncio
async def test_telethon_retry_sleep_holds_no_slot_and_each_sender_attempt_readmits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telethon.client import users as telethon_users

    gate = _gate()
    gate._request_retries = 1
    gate._scheduler_policy = TelegramRpcSchedulerConfig(interactive_queue_capacity=1)
    events: list[RpcAdmissionEvent] = []
    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=gate._scheduler_policy,
        limiter=gate._limiter,
        minimum_spacing_seconds=gate._test_minimum_spacing_seconds,
        observer=events.append,
        readiness=RpcTransportReadiness(
            probe=gate._scheduler_transport_ready,
            wait=gate._wait_for_scheduler_transport,
        ),
    )
    first_sleep_started, release_first_sleep = asyncio.Event(), asyncio.Event()
    sequence: list[str] = []
    admission_sequences: list[int] = []
    sender_attempts = 0
    original_admit = gate._admission_scheduler.admit

    async def tracked_admit(scope: TelegramRpcScope) -> RpcAdmission:
        admission = await original_admit(scope)
        admission_sequences.append(admission.sequence)
        return admission

    def send(request: object) -> object:
        nonlocal sender_attempts
        sender_attempts += 1
        value = _request_value(request)
        if sender_attempts == 1:
            sequence.append(f"send:{value}:error")
            raise ServerError(None, "temporary")
        sequence.append(f"send:{value}:ok")
        return f"{value}-ok"

    async def telethon_sleep(delay: float) -> None:
        assert delay == 2
        sequence.append("telethon-sleep")
        assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
        assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)
        first_sleep_started.set()
        await release_first_sleep.wait()

    def process_entities(result: object) -> None:
        assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
        sequence.append(f"process:{result}")

    monkeypatch.setattr(gate._admission_scheduler, "admit", tracked_admit)
    monkeypatch.setattr(telethon_users, "asyncio", SimpleNamespace(sleep=telethon_sleep))
    _set_sender(gate, send)
    gate.session.process_entities = process_entities

    first = asyncio.create_task(_call(gate, "first"))
    await first_sleep_started.wait()
    assert await _call(gate, "second") == "second-ok"
    release_first_sleep.set()
    assert await first == "first-ok"

    assert sequence == [
        "send:first:error",
        "telethon-sleep",
        "send:second:ok",
        "process:second-ok",
        "send:first:ok",
        "process:first-ok",
    ]
    assert (len(admission_sequences), len(set(admission_sequences)), gate._limiter.acquisitions) == (3, 3, 3)
    assert [event.kind for event in events].count(RpcAdmissionEventKind.DISPATCHED) == 3
    assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)


@pytest.mark.asyncio
async def test_request_specific_observation_counts_each_actual_retry_attempt_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate(retry_delays=(0,))
    observed: list[dict[str, object]] = []
    recorder = _Recorder()
    observer = RpcAdmissionObservationAggregator(
        recorder, method_names=TelegramRpcGate.rpc_observation_methods(), policy=RuntimeObservationConfig()
    )

    def observe(**values: object) -> None:
        observed.append(values)
        observer.observe_rpc_event(**values)  # type: ignore[arg-type]

    gate.set_rpc_request_observer(observe)
    gate.set_rpc_admission_observer(observer.observe)
    send_attempts = 0

    def send(_request: object) -> object:
        nonlocal send_attempts
        send_attempts += 1
        if send_attempts == 1:
            raise ServerError(None, "temporary")
        return "ok"

    _set_sender(gate, send)

    request = functions.channels.GetFullChannelRequest(channel=types.InputChannel(channel_id=1, access_hash=2))
    assert await _call(gate, request) == "ok"

    assert send_attempts == 2
    claims = [item for item in observed if item["phase"] == "write_claim"]
    assert len(claims) == 2
    assert all("channels.GetFullChannelRequest:" in str(item["request_method"]) for item in claims)
    assert all(item["scope"].source is TelegramRpcSource.MCP_INTERACTIVE for item in claims)
    assert all("InputChannel" not in repr(item) for item in observed)
    observer.flush()
    samples = [cast(dict[str, object], row["payload"]) for row in recorder.rows if row["outcome"] == "causal_sample"]
    retries = [payload for payload in samples if payload["phase"] == "retry"]
    assert len(retries) == 1
    assert retries[0]["method"] == claims[0]["request_method"]
    assert retries[0]["reason"] == "server_transient"
    assert retries[0]["root_attempts"] == 1 and cast(int, retries[0]["budget_remaining"]) >= 1


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError])
async def test_gate_normalizes_each_vendor_wait_to_owned_outcome(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type,
) -> None:
    gate = _gate()
    vendor_error = cast(Callable[..., BaseException], error_type)(request=None, capture=7)

    def send(_request: object) -> object:
        raise vendor_error

    _set_sender(gate, send)
    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, "request")

    assert caught.value.retry_after_seconds == 7
    assert caught.value.latched is False
    assert caught.value.__cause__ is vendor_error


@pytest.mark.asyncio
async def test_gate_rechecks_cooldown_after_limiter() -> None:
    reset_rpc_process_policy()
    gate = _gate()
    import mcp_telegram.telegram_rpc as rpc

    first_acquisition = asyncio.Event()
    acquisitions = 0

    async def acquire_and_open() -> None:
        nonlocal acquisitions
        acquisitions += 1
        gate._limiter.acquisitions += 1
        if acquisitions == 1:
            rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 3
            first_acquisition.set()

    gate._limiter.acquire = acquire_and_open

    operation = asyncio.create_task(_call(gate, "request"))
    try:
        await first_acquisition.wait()
        await asyncio.sleep(0)
        assert gate._test_server.seen == set()
        assert acquisitions == 1
        rpc._COOLDOWN_DEADLINE = 0
        gate._admission_scheduler.notify_eligibility_changed()
        assert await operation == "request"
        assert acquisitions == 2
        assert len(gate._test_server.seen) == 1
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        rpc._COOLDOWN_DEADLINE = 0
        gate._admission_scheduler.notify_eligibility_changed()


@pytest.mark.asyncio
async def test_gate_requeues_when_readiness_changes_after_scheduler_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()
    events: list[RpcAdmissionEvent] = []
    ready = True
    admission_count = 0
    original_admit = gate._admission_scheduler.admit

    def readiness() -> bool:
        nonlocal ready
        if ready:
            return True
        ready = True
        return False

    async def admit_then_change_readiness(scope: TelegramRpcScope) -> RpcAdmission:
        nonlocal admission_count, ready
        admission = await original_admit(scope)
        admission_count += 1
        if admission_count == 1:
            ready = False
        return admission

    gate._scheduler_transport_ready = readiness
    gate._admission_scheduler._readiness = RpcTransportReadiness(probe=readiness)
    gate._admission_scheduler.set_observer(events.append)
    monkeypatch.setattr(gate._admission_scheduler, "admit", admit_then_change_readiness)

    assert await _call(gate, "request") == "request"

    assert gate._limiter.acquisitions == 2
    assert [event.kind for event in events].count(RpcAdmissionEventKind.DISPATCHED) == 1
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)


@pytest.mark.asyncio
async def test_gate_flood_is_immediate_cooldown_and_observed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_rpc_process_policy()
    gate = _gate()
    observed: list[dict[str, object]] = []
    gate._flood_observer = lambda **kwargs: observed.append(kwargs)
    error = FloodWaitError(request=None, capture=7)
    attempts = 0

    def send(_request: object) -> object:
        nonlocal attempts
        attempts += 1
        raise error

    _set_sender(gate, send)
    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, "request")
    assert caught.value.__cause__ is error
    assert attempts == 1
    await gate._observe_flood(error)
    assert observed == [{"source": "telegram_rpc_gate", "seconds": 7}]
    assert account_cooldown_deadline() > 0


@pytest.mark.asyncio
async def test_gate_flood_cooldown_uses_buffer_and_extends_monotonically(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_telegram.telegram_rpc as rpc

    wall_clock = rpc.time.time
    clock = [100.0]
    monkeypatch.setattr(
        rpc,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=wall_clock),
    )
    gate = _gate()
    await gate._observe_flood(FloodWaitError(request=None, capture=7))
    first_deadline = account_cooldown_deadline()
    clock[0] = 101.0
    await gate._observe_flood(FloodWaitError(request=None, capture=2))

    assert first_deadline == 108.0
    assert account_cooldown_deadline() == first_deadline


@pytest.mark.asyncio
async def test_gate_restores_persisted_cooldown_and_blocks_transport_until_ready(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monotonic_now = [200.0]
    wall_now = [1_000.0]
    import mcp_telegram.telegram_rpc as rpc

    monkeypatch.setattr(rpc, "time", SimpleNamespace(monotonic=lambda: monotonic_now[0], time=lambda: wall_now[0]))

    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    seed_conn = sqlite3.connect(str(db_path))
    seed_conn.execute(
        "INSERT INTO daemon_state(key,value) VALUES (?,?)", ("telegram_account_cooldown_until_utc", "1005.0")
    )
    seed_conn.commit()
    verified_store = _MemorySafetyStore(AuthKey(bytes(range(256))).key_id)
    save_rpc_safety_record(seed_conn, None, verified_store.raw)
    seed_conn.close()
    reopened_conn = sqlite3.connect(str(db_path))

    async def save_record(expected: str | None, encoded: str) -> None:
        save_rpc_safety_record(reopened_conn, expected, encoded)

    safety_store = TelegramRpcSafetyStore(
        load_record=lambda: load_rpc_safety_record(reopened_conn),
        save_record=save_record,
        load_legacy_until_utc=lambda: load_account_cooldown_until_utc(reopened_conn),
        canonical_owners=lambda: load_rpc_safety_owners(reopened_conn),
    )
    gate = _gate(safety_store=safety_store)
    gate._ensure_safety_ledger()
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_cooldown(_delay: float) -> None:
        waiting.set()
        await release.wait()

    monkeypatch.setattr(rpc.asyncio, "sleep", wait_for_cooldown)
    gate._limiter = AsyncLimiter(30, 60)
    gate._test_minimum_spacing_seconds = 2.0

    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=gate._scheduler_policy,
        limiter=gate._limiter,
        minimum_spacing_seconds=gate._test_minimum_spacing_seconds,
        clock=lambda: monotonic_now[0],
        readiness=RpcTransportReadiness(probe=gate._scheduler_transport_ready),
    )
    sender = _set_sender(gate, _request_value)
    try:
        caller = asyncio.create_task(_call(gate, "request"))
        await waiting.wait()

        assert account_cooldown_deadline() == 205.0
        assert sender.calls == 0
        assert gate._limiter.max_rate == 30
        assert gate._limiter.time_period == 60
        assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)
        assert load_account_cooldown_until_utc(reopened_conn) == 1_005.0
        assert gate._main_sender._connection._writer.frames == []

        monotonic_now[0] = 206.0
        wall_now[0] = 1_006.0
        release.set()
        assert await caller == "request"
        assert sender.calls == 1
    finally:
        await gate.close_rpc_scheduler()
        gate.session.close()
        reopened_conn.close()


@pytest.mark.asyncio
async def test_latched_circuit_never_persists_a_cooldown() -> None:
    gate = _gate(_CircuitStatus(open=True))
    sender = _set_sender(gate, _request_value)

    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, "request")

    assert caught.value.latched
    assert sender.calls == 0
    import mcp_telegram.telegram_rpc as rpc

    assert account_cooldown_deadline() <= rpc.time.monotonic()
    assert gate._test_safety_store.writes == []


@pytest.mark.asyncio
async def test_gate_concurrent_observation_marks_one_exception_once() -> None:
    gate = _gate()
    observed: list[dict[str, object]] = []
    gate._flood_observer = lambda **kwargs: observed.append(kwargs)
    error = FloodWaitError(request=None, capture=7)
    barrier = asyncio.Barrier(3)

    async def observe() -> None:
        await barrier.wait()
        await gate._observe_flood(error)

    await asyncio.gather(observe(), observe(), barrier.wait())

    assert len(observed) == 1
    assert account_cooldown_deadline() >= asyncio.get_running_loop().time() + 7


@pytest.mark.asyncio
async def test_gate_flood_observation_opens_accumulator_and_rejects_next_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=1, max_wait_seconds=900)
    )
    gate = _gate()
    gate._rpc_circuit_status = accumulator.kill_switch_status
    gate._flood_observer = lambda **kwargs: accumulator.observe(**kwargs)
    error = FloodWaitError(request=None, capture=7)

    def send(_request: object) -> object:
        raise error

    _set_sender(gate, send)
    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, "request")
    assert caught.value.retry_after_seconds == 7
    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, "request")
    assert caught.value.latched
    assert caught.value.retry_after_seconds is None

    status = accumulator.kill_switch_status()
    assert status.open is True
    assert status.events_in_window == 1
    assert gate._limiter.acquisitions == 1


@pytest.mark.asyncio
async def test_one_update_difference_warning_cannot_amplify_during_cooldown() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate = _gate()
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    sender = _set_sender(gate, lambda _request: (_ for _ in ()).throw(FloodWaitError(request=None, capture=20)))

    try:
        with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE):
            with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
                with pytest.raises(TelegramRpcThrottled):
                    await gate(_TestRequest("difference"))
        assert sender.calls == 1
        assert accumulator.kill_switch_status().events_in_window == 1
        assert gate._limiter.acquisitions == 1
        assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_sequential_vendor_cache_error_is_not_another_flood_warning() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate = _gate()
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    sender = _set_sender(gate, lambda _request: (_ for _ in ()).throw(FloodWaitError(request=None, capture=20)))
    try:
        with pytest.raises(TelegramRpcThrottled):
            await _call(gate, _TestRequest("first"))
        import mcp_telegram.telegram_rpc as rpc

        rpc._COOLDOWN_DEADLINE = 0
        with pytest.raises(TelegramRpcThrottled):
            await _call(gate, _TestRequest("second"))
        assert sender.calls == 1
        assert gate._limiter.acquisitions == 1
        assert accumulator.kill_switch_status().events_in_window == 1
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_concurrent_vendor_cache_fold_is_a_local_deferral_not_a_second_warning() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate = _gate()
    phases: list[str] = []
    gate.set_rpc_request_observer(lambda **values: phases.append(values["phase"]))
    gate._rpc_circuit_status = accumulator.kill_switch_status
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    flood_events: list[dict[str, object]] = []
    gate._flood_event_observer = lambda observation: flood_events.append(
        {
            "origin": observation.origin,
            "actual_dispatch": observation.actual_dispatch,
            "admission_sequence": observation.admission_sequence,
        }
    )
    budget = RpcAttemptBudget(2)
    resolved = asyncio.Event()
    release = asyncio.Event()
    sender_calls = 0

    def send(_request: object) -> object:
        nonlocal sender_calls
        sender_calls += 1
        raise FloodWaitError(request=None, capture=20)

    sender = _set_sender(gate, send)
    delayed: asyncio.Task[object] | None = None
    try:
        with rpc_attempt_budget(budget):
            delayed = asyncio.create_task(_call(gate, _DelayedResolveRequest(2, resolved, release)))
            await resolved.wait()
            with pytest.raises(TelegramRpcThrottled):
                await _call(gate, _DelayedResolveRequest(1, resolved, release))
            release.set()
            with pytest.raises(TelegramRpcThrottled) as cached:
                await delayed

        assert cached.value.retry_after_seconds == 20
        assert sender_calls == sender.calls == 1
        assert gate._limiter.acquisitions == 1
        assert budget.attempts == 1
        assert accumulator.kill_switch_status().events_in_window == 1
        assert [event["origin"] for event in flood_events] == ["actual_send", "vendor_cache"]
        assert [event["actual_dispatch"] for event in flood_events] == [True, False]
        assert flood_events[0]["admission_sequence"] is not None
        assert flood_events[1]["admission_sequence"] is None
        assert phases.count("write_claim") == phases.count("vendor_cached") == 1
    finally:
        if delayed is not None and not delayed.done():
            release.set()
            delayed.cancel()
            await asyncio.gather(delayed, return_exceptions=True)
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_nested_resolution_does_not_turn_four_vendor_cache_deferrals_into_warnings(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telethon.client import users as vendor_users

    import mcp_telegram.telegram_rpc as rpc

    real_monotonic, real_wall_time = rpc.time.monotonic, rpc.time.time
    started_monotonic, started_wall_time = real_monotonic(), real_wall_time()
    clock_offsets = [0.0, 0.0]
    fake_time = SimpleNamespace(
        monotonic=lambda: started_monotonic + (real_monotonic() - started_monotonic) + clock_offsets[0],
        time=lambda: started_wall_time + (real_wall_time() - started_wall_time) + clock_offsets[1],
    )
    monkeypatch.setattr(rpc, "time", fake_time)
    monkeypatch.setattr(vendor_users, "time", SimpleNamespace(time=fake_time.time))

    def outer_request(*, nested: bool) -> TLRequest:
        request = functions.updates.GetStateRequest()

        async def resolve(client: TelegramClient, utils: object) -> None:
            del utils
            if nested:
                await client(_TestRequest("nested"))

        request.resolve = resolve  # type: ignore[method-assign]
        return request

    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate = _gate()
    gate._cooldown_buffer_seconds = 0
    gate._rpc_circuit_status = accumulator.kill_switch_status
    gate._admission_scheduler._clock = fake_time.monotonic
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    flood_events: list[str] = []
    gate._flood_event_observer = lambda observation: flood_events.append(observation.origin)
    sent: list[object] = []

    def send(request: object) -> object:
        sent.append(request)
        if isinstance(request, functions.updates.GetStateRequest):
            raise FloodWaitError(request=None, capture=5)
        return _request_value(request)

    sender = _set_sender(gate, send)
    try:
        deadline = asyncio.get_running_loop().time() + 300
        with demand_context(DemandKind.DIALOG_BOOTSTRAP, deadline=deadline):
            with rpc_scope(TelegramRpcSource.DIALOG_SYNC, deadline=deadline):
                with pytest.raises(TelegramRpcThrottled):
                    await gate(outer_request(nested=False))
                for _ in range(4):
                    clock_offsets[0] += 6
                    clock_offsets[1] += 6
                    assert gate._safety.floor_deadline <= fake_time.monotonic()
                    gate._flood_waited_requests[functions.updates.GetStateRequest.CONSTRUCTOR_ID] = fake_time.time() + 5
                    gate._admission_scheduler.notify_eligibility_changed()
                    with pytest.raises(TelegramRpcThrottled):
                        await asyncio.wait_for(gate(outer_request(nested=True)), timeout=2.0)

        assert sender.calls == 5
        assert [type(request) for request in sent] == [
            functions.updates.GetStateRequest,
            GetConfigRequest,
            GetConfigRequest,
            GetConfigRequest,
            GetConfigRequest,
        ]
        assert accumulator.kill_switch_status().events_in_window == 1
        assert accumulator.kill_switch_status().open is False
        assert flood_events == ["actual_send", "vendor_cache", "vendor_cache", "vendor_cache", "vendor_cache"]
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_unmatched_preflight_flood_wait_remains_an_unknown_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    origins: list[str] = []
    gate._flood_event_observer = lambda observation: origins.append(observation.origin)
    sender = _set_sender(gate, _request_value)

    async def preflight_flood(
        _client: TelegramClient,
        _sender: object,
        _request: object,
        ordered: bool = False,
        flood_sleep_threshold: int | None = None,
    ) -> object:
        del ordered, flood_sleep_threshold
        raise FloodWaitError(request=object(), capture=20)

    monkeypatch.setattr(TelegramClient, "_call", preflight_flood)
    try:
        with pytest.raises(TelegramRpcThrottled):
            await _call(gate, _TestRequest("unknown"))
        assert sender.calls == 0
        assert origins == ["unknown"]
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_five_independent_sender_warnings_latch_the_account_circuit() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    sender_calls = 0
    import mcp_telegram.telegram_rpc as rpc

    for index in range(5):
        gate = _gate()
        gate._rpc_circuit_status = accumulator.kill_switch_status
        gate._flood_observer = lambda **event: accumulator.observe(**event)
        gate._cooldown_buffer_seconds = 0

        def send(_request: object, *, _index: int = index) -> object:
            nonlocal sender_calls
            sender_calls += 1
            raise FloodWaitError(request=None, capture=1 + (_index % 2))

        _set_sender(gate, send)
        try:
            with pytest.raises(TelegramRpcThrottled) as caught:
                await _call(gate, f"request-{index}")
            assert caught.value.latched is False
        finally:
            await gate.close_rpc_scheduler()
        rpc._COOLDOWN_DEADLINE = 0

    assert sender_calls == 5
    assert accumulator.kill_switch_status().open is True
    blocked_gate = _gate()
    blocked_gate._rpc_circuit_status = accumulator.kill_switch_status
    blocked_sender = _set_sender(blocked_gate, lambda _request: pytest.fail("latched gate sent"))
    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(blocked_gate, "blocked")
    assert caught.value.latched is True
    assert sender_calls == 5
    assert blocked_sender.calls == 0
    assert blocked_gate._limiter.acquisitions == 0
    await blocked_gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_gate_cooldown_wait_is_cancellation_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    import mcp_telegram.telegram_rpc as rpc

    rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 30
    waiting = asyncio.Event()
    import mcp_telegram.telegram_rpc as telegram_rpc

    original_sleep = telegram_rpc.asyncio.sleep

    async def wait_and_signal(delay: float) -> None:
        waiting.set()
        await original_sleep(delay)

    monkeypatch.setattr(telegram_rpc.asyncio, "sleep", wait_and_signal)
    budget = RpcAttemptBudget(1)
    with rpc_attempt_budget(budget):
        caller = asyncio.create_task(_call(gate, "request"))
    await waiting.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert gate._limiter.acquisitions == 0
    assert budget.attempts == 0
    assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_cooldown_preflight_expires_on_original_deadline_without_admission() -> None:
    gate = _gate()
    sender = _set_sender(gate, _request_value)
    events: list[RpcAdmissionEvent] = []
    gate.set_rpc_admission_observer(events.append)
    import mcp_telegram.telegram_rpc as rpc

    rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 0.2
    budget = RpcAttemptBudget(1)
    with demand_context(DemandKind.MCP_REMOTE_ACQUISITION) as token:
        with rpc_attempt_budget(budget):
            with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE, timeout_seconds=0.03):
                with pytest.raises(TelegramRpcAdmissionDeferred):
                    await gate(_TestRequest("request"))

    assert sender.calls == 0
    assert gate._limiter.acquisitions == 0
    assert budget.attempts == 0
    assert token.attempt_evidence.actual_attempts == 0
    assert [event.kind for event in events] == [RpcAdmissionEventKind.EXPIRED]
    assert events[0].reason == "deadline_elapsed"
    await gate.close_rpc_scheduler()


def test_gate_factory_invariants_without_connecting() -> None:
    status = _CircuitStatus(open=False)
    gate = TelegramRpcGate(
        StringSession(),
        1,
        "hash",
        rpc_budget=TelegramRpcBudget(max_calls_per_period=3, period_seconds=60),
        circuit_status=lambda: status,
        fallback_wait_seconds=60,
        cooldown_buffer_seconds=1.0,
        transient_retry_delays_seconds=(2.0,),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=_MemorySafetyStore(AuthKey(bytes(range(256))).key_id).api(),
    )
    assert isinstance(gate, TelegramClient)
    assert gate._request_retries == 0
    assert gate._safety is None
    assert gate.flood_sleep_threshold == 0
    assert gate._raise_last_call_error is True
    assert gate._auto_reconnect is True


@pytest.mark.asyncio
async def test_internal_reconnect_probe_signals_before_vendor_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    observed: list[tuple[DemandKind, ExecutionMode, AcquisitionKind | None]] = []
    vendor_calls = 0

    async def vendor_handler(_client: TelegramClient) -> None:
        nonlocal vendor_calls
        vendor_calls += 1
        token = current_demand_token()
        observed.append((token.kind, demand_contract(token.kind).execution_mode, token.acquisition_kind))
        assert gate.reconnect_event.is_set()

    monkeypatch.setattr(TelegramClient, "_handle_auto_reconnect", vendor_handler)

    await gate._handle_auto_reconnect()

    assert vendor_calls == 1
    assert observed == [
        (DemandKind.TELETHON_RECONNECT_PROBE, ExecutionMode.PROTOCOL, AcquisitionKind.ACCOUNT_SELF_PROFILE)
    ]


@pytest.mark.asyncio
async def test_internal_reconnect_probe_runs_vendor_get_users_request_under_scope() -> None:
    assert "get_me" in inspect.getsource(TelegramClient._handle_auto_reconnect)
    gate = _gate()
    sent: list[object] = []
    gate._mb_entity_cache = SimpleNamespace(
        self_id=None,
        set_self_user=lambda *_args: None,
    )

    def send(request: object) -> list[object]:
        sent.append(request)
        return [SimpleNamespace(id=7, bot=False, access_hash=11)]

    _set_sender(gate, send)

    await gate._handle_auto_reconnect()

    assert len(sent) == 1
    assert isinstance(sent[0], functions.users.GetUsersRequest)
    assert sent[0].id[0].__class__ is types.InputUserSelf


def test_telethon_sender_keeps_internal_reconnect_callback_wiring() -> None:
    source = inspect.getsource(TelegramClient.__init__).replace(" ", "")

    assert "auto_reconnect_callback=self._handle_auto_reconnect" in source


@pytest.mark.asyncio
async def test_internal_reconnect_probe_rejects_same_task_nested_root() -> None:
    gate = _gate()

    with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE):
        with pytest.raises(RuntimeError, match="nested root demand"):
            await gate._handle_auto_reconnect()


@pytest.mark.asyncio
async def test_internal_reconnect_probe_refreshes_inherited_parent_scope() -> None:
    gate = _gate()
    sent: list[object] = []
    gate._mb_entity_cache = SimpleNamespace(self_id=None, set_self_user=lambda *_args: None)

    def send(request: object) -> list[object]:
        sent.append(request)
        return [SimpleNamespace(id=7, bot=False, access_hash=11)]

    _set_sender(gate, send)

    async def parent_rpc() -> None:
        with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE):
            await asyncio.create_task(gate._handle_auto_reconnect())

    await parent_rpc()

    assert len(sent) == 1
    assert isinstance(sent[0], functions.users.GetUsersRequest)


@pytest.mark.asyncio
async def test_gate_does_not_retry_slow_mode_or_nonretryable_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate(retry_delays=(2.0,))
    attempts = 0

    def send(_request: object) -> object:
        nonlocal attempts
        attempts += 1
        raise SlowModeWaitError(request=None, capture=2)

    _set_sender(gate, send)
    with pytest.raises(SlowModeWaitError):
        await _call(gate, "request")
    assert attempts == 1


@pytest.mark.asyncio
async def test_gate_does_not_retry_arbitrary_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate(retry_delays=(2.0,))
    attempts = 0

    def send(_request: object) -> object:
        nonlocal attempts
        attempts += 1
        raise ValueError("not a server transient")

    _set_sender(gate, send)
    with pytest.raises(ValueError, match="not a server transient"):
        await _call(gate, "request")
    assert attempts == 1


@pytest.mark.asyncio
async def test_gate_fails_closed_when_application_rpc_has_no_source(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    called = False

    def send(_request: object) -> object:
        nonlocal called
        called = True
        return "unexpected"

    _set_sender(gate, send)
    with pytest.raises(UnclassifiedTelegramRpcError, match="no explicit operation source"):
        await gate("request")
    assert called is False
    assert gate._limiter.acquisitions == 0


@pytest.mark.asyncio
async def test_gate_translates_recoverable_admission_failure_to_product_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()
    with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE):
        scope = current_rpc_scope()

    async def reject(_scope: object) -> object:
        raise RpcAdmissionSaturatedError(scope, "interactive outstanding capacity is full")

    monkeypatch.setattr(gate._admission_scheduler, "admit", reject)
    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, "request")

    assert caught.value.retry_after_seconds == gate._scheduler_policy.admission_retry_seconds
    assert "scheduler" not in str(caught.value).lower()
    assert "interactive" not in str(caught.value).lower()


@pytest.mark.asyncio
async def test_update_source_propagates_permanent_scheduler_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
        scope = current_rpc_scope()

    async def reject(_scope: object) -> object:
        raise RpcAdmissionClosedError(scope, "closed")

    monkeypatch.setattr(gate._admission_scheduler, "admit", reject)
    with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
        with pytest.raises(RpcAdmissionClosedError, match="closed"):
            await gate(_TestRequest("difference"))


@pytest.mark.asyncio
async def test_gate_releases_active_capacity_after_transport_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    gate._scheduler_policy = TelegramRpcSchedulerConfig(interactive_queue_capacity=1)
    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=gate._scheduler_policy,
        limiter=gate._limiter,
        minimum_spacing_seconds=gate._test_minimum_spacing_seconds,
        readiness=RpcTransportReadiness(
            probe=gate._scheduler_transport_ready,
            wait=gate._wait_for_scheduler_transport,
        ),
    )
    release = asyncio.Event()

    async def send(request: object) -> object:
        await release.wait()
        return _request_value(request)

    _set_sender(gate, send)
    first = asyncio.create_task(_call(gate, "first"))
    await _wait_for(lambda: gate._admission_scheduler.active_depths()[RpcServiceClass.INTERACTIVE] == 1)
    with pytest.raises(TelegramRpcAdmissionDeferred) as caught:
        await _call(gate, "second")
    assert isinstance(caught.value, TelegramRpcThrottled)
    assert "admission" not in str(caught.value).lower()
    assert gate._limiter.acquisitions == 1

    release.set()
    assert await first == "first"
    assert gate._admission_scheduler.active_depths()[RpcServiceClass.INTERACTIVE] == 0


@pytest.mark.asyncio
async def test_actual_future_flood_is_recorded_when_caller_cancels_after_response() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate = _gate()
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    observations: list[FloodWaitObservation] = []
    gate.set_flood_event_observer(observations.append)
    caller = asyncio.create_task(_call(gate, _TestRequest("actual")))
    await _wait_for(lambda: sender.calls == 1)
    raw_future = sender.futures[0]
    raw_future.set_exception(FloodWaitError(request=None, capture=20))
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await _wait_for(lambda: not _pending_owned_states(gate))
    await _wait_for(lambda: gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0))
    assert account_cooldown_deadline() > asyncio.get_running_loop().time()
    assert accumulator.kill_switch_status().events_in_window == 1
    assert len(observations) == 1
    observation = observations[0]
    assert observation.origin == "actual_send"
    assert observation.actual_dispatch is True
    assert observation.request_method.endswith(f"GetConfigRequest:{GetConfigRequest.CONSTRUCTOR_ID:08x}")
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    await gate.close_rpc_scheduler()


def _dm_reconciliation_test_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (211, 'synced')")
    seed_full_history_enrollment(conn, 211, enabled=True)
    conn.execute("INSERT INTO entities(id, type, updated_at) VALUES (211, 'user', 1)")
    conn.execute("INSERT INTO messages(dialog_id, message_id, sent_at, text) VALUES (211, 1, 1, 'local')")
    conn.commit()
    return conn


def _dm_reconciliation_state_for_rpc_test(conn: sqlite3.Connection) -> dict[str, object]:
    row = cast(
        tuple[str], conn.execute("SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'").fetchone()
    )
    import json

    return cast(dict[str, object], json.loads(row[0]))


class _DmPageFutureSender(_RawFutureSender):
    """Record the real Telethon page request and its inherited gate scope."""

    def __init__(self) -> None:
        super().__init__()
        self.scopes: list[TelegramRpcScope] = []

    def send(self, request: object, *, ordered: bool = False) -> asyncio.Future[object]:
        self.scopes.append(current_rpc_scope())
        return super().send(request, ordered=ordered)


class _DmEventHandlerGateClient:
    """Typed test bridge from the production page scanner to the real gate."""

    def __init__(self, gate: TelegramRpcGate) -> None:
        self._gate = gate

    def add_event_handler(self, _callback: object, _event: object) -> None:
        return

    def remove_event_handler(self, _callback: object) -> None:
        return

    async def get_messages(self, *args: object, **kwargs: object) -> object:
        return await self._gate.get_messages(*args, **kwargs)

    async def get_me(self) -> object:
        return await self._gate.get_me()

    async def get_entity(self, _dialog_id: object) -> object:
        return await self._gate.get_entity(cast(types.TypePeer, _dialog_id))

    def __call__(self, _request: object) -> Coroutine[object, object, object]:
        async def call_gate() -> object:
            return await self._gate(_request)

        return call_gate()


class _RecordingDmPageScanner:
    """Preserve the production scanner while exposing its collaborator boundary."""

    def __init__(self, manager: EventHandlerManager) -> None:
        self._manager = manager
        self.calls = 0

    async def run_dm_gap_scan_page(self, dialog_id: int, message_ids: Sequence[int]) -> int:
        self.calls += 1
        return await self._manager.run_dm_gap_scan_page(dialog_id, message_ids)


def _real_dm_page_scanner(gate: TelegramRpcGate, conn: sqlite3.Connection) -> _RecordingDmPageScanner:
    manager = event_manager(_DmEventHandlerGateClient(gate), conn, asyncio.Event())
    return _RecordingDmPageScanner(manager)


def _real_dm_page_adapter(
    conn: sqlite3.Connection,
) -> tuple[TelegramRpcGate, _DmPageFutureSender, DmDeletionReconciliationDemandAdapter, _RecordingDmPageScanner]:
    gate = _gate()
    sender = _DmPageFutureSender()
    _set_sender(gate, sender)
    gate.session.process_entities = lambda _result: None
    gate.session.get_input_entity = lambda entity: types.InputPeerUser(int(entity), 0)
    scanner = _real_dm_page_scanner(gate, conn)
    prepare_dm_deletion_reconciliation(conn, now=100)
    return (
        gate,
        sender,
        DmDeletionReconciliationDemandAdapter(conn, scanner, writer=inline_writer(conn), account_id=123),
        scanner,
    )


def _selected_dm_page_budget(adapter: DmDeletionReconciliationDemandAdapter) -> RpcAttemptBudget:
    status = adapter.status(100)
    assert status is not None and status.selected is not None
    return RpcAttemptBudget(limit=1, selected=status.selected)


async def _dispatch_forward_history_sibling(
    gate: TelegramRpcGate,
    sender: _DmPageFutureSender,
    conn: sqlite3.Connection,
    *,
    expected_cursor: int,
) -> None:
    """Run one normal durable sibling after the shared cooldown has expired."""

    class GateForwardPort:
        async def fetch_page(
            self,
            dialog_id: int,
            *,
            after_message_id: int,
            should_stop: Callable[[], bool],
        ) -> ForwardGapPage:
            assert dialog_id == 211
            assert after_message_id == expected_cursor
            assert not should_stop()
            await gate(
                functions.messages.GetHistoryRequest(
                    peer=types.InputPeerUser(user_id=dialog_id, access_hash=0),
                    offset_id=0,
                    offset_date=None,
                    add_offset=0,
                    limit=100,
                    max_id=0,
                    min_id=after_message_id,
                    hash=0,
                )
            )
            return ForwardGapPage(messages=(), complete=True)

    forward_adapter = DeltaGapFillDemandAdapter(
        DeltaSyncWorker(GateForwardPort(), conn, asyncio.Event(), writer=inline_writer(conn)), account_id=123
    )
    status = forward_adapter.status(time.time())
    assert status is not None and status.selected is not None
    sibling = asyncio.create_task(forward_adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected)))
    await _wait_for(lambda: sender.calls == 2)
    assert isinstance(sender.requests[1], functions.messages.GetHistoryRequest)
    assert sender.requests[1].min_id == expected_cursor
    assert sender.scopes[1].demand_kind is DemandKind.DELTA_GAP_FILL
    assert sender.scopes[1].acquisition_kind is AcquisitionKind.MESSAGE_HISTORY_PAGE
    sender.futures[1].set_result(types.messages.Messages(messages=[], topics=[], chats=[], users=[]))
    await sibling
    assert conn.execute("SELECT delta_message_id FROM synced_dialogs WHERE dialog_id=211").fetchone() == (
        expected_cursor,
    )


@pytest.mark.asyncio
async def test_dm_page_success_uses_real_get_messages_and_commits_tombstone_cursor() -> None:
    conn = _dm_reconciliation_test_db()
    gate, sender, adapter, scanner = _real_dm_page_adapter(conn)
    try:
        budget = _selected_dm_page_budget(adapter)
        task = asyncio.create_task(adapter.run_slice(budget))
        await _wait_for(lambda: sender.calls == 1)

        request = sender.requests[0]
        assert isinstance(request, functions.messages.GetMessagesRequest)
        assert len(request.id) == 1
        assert request.id[0].id == 1
        scope = sender.scopes[0]
        assert scope.demand_kind is DemandKind.DM_DELETION_RECONCILIATION
        assert scope.acquisition_kind is AcquisitionKind.MESSAGE_LOOKUP
        assert scope.attempt_budget is budget
        sender.futures[0].set_result(types.messages.MessagesNotModified(count=1))
        await task

        assert scanner.calls == 1
        assert budget.attempts == 1
        assert conn.execute("SELECT is_deleted FROM messages WHERE dialog_id=211 AND message_id=1").fetchone() == (1,)
        state = _dm_reconciliation_state_for_rpc_test(conn)
        assert state["status"] == "running"
        assert state["dialog_id_cursor"] == 211
        assert state["message_cursor"] == 1
    finally:
        await gate.close_rpc_scheduler()
        conn.close()


@pytest.mark.asyncio
async def test_dm_page_cursor_commit_failure_preserves_tombstone_and_blocks_reconstruction() -> None:
    conn = _dm_reconciliation_test_db()
    gate, sender, adapter, scanner = _real_dm_page_adapter(conn)
    try:
        conn.execute(
            """CREATE TRIGGER fail_dm_cursor_commit
               BEFORE INSERT ON daemon_state
               WHEN NEW.key='delta_dm_gap_scan_state' AND NEW.value LIKE '%\"message_cursor\": 1%'
               BEGIN SELECT RAISE(ABORT, 'injected cursor commit failure'); END"""
        )
        conn.commit()

        budget = _selected_dm_page_budget(adapter)
        task = asyncio.create_task(adapter.run_slice(budget))
        await _wait_for(lambda: sender.calls == 1)
        assert isinstance(sender.requests[0], functions.messages.GetMessagesRequest)
        sender.futures[0].set_result(types.messages.MessagesNotModified(count=1))
        with pytest.raises(sqlite3.IntegrityError, match="injected cursor commit failure"):
            await task

        assert scanner.calls == 1
        assert budget.attempts == 1
        assert conn.execute("SELECT is_deleted FROM messages WHERE dialog_id=211 AND message_id=1").fetchone() == (1,)
        state = _dm_reconciliation_state_for_rpc_test(conn)
        assert state["status"] == "verifying"
        assert state["message_cursor"] == 0
        assert adapter.status(10_000_000) is None

        reconstructed_scanner = _real_dm_page_scanner(gate, conn)
        reconstructed = DmDeletionReconciliationDemandAdapter(
            conn, reconstructed_scanner, writer=inline_writer(conn), account_id=123
        )
        assert reconstructed.status(10_000_000) is None
        await reconstructed.run_slice(RpcAttemptBudget(limit=1, selected=budget.selected))
        assert reconstructed_scanner.calls == 0
        assert sender.calls == 1
    finally:
        await gate.close_rpc_scheduler()
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("delta_cursor", [None, 1])
async def test_dm_page_flood_crosses_gate_with_durable_demand_and_acquisition(  # noqa: PLR0915
    delta_cursor: int | None,
) -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    conn = _dm_reconciliation_test_db()
    conn.execute("UPDATE synced_dialogs SET delta_message_id=? WHERE dialog_id=211", (delta_cursor,))
    conn.commit()
    gate, sender, adapter, scanner = _real_dm_page_adapter(conn)
    try:
        gate._rpc_circuit_status = accumulator.kill_switch_status
        gate._flood_observer = lambda **event: accumulator.observe(**event)
        observations: list[FloodWaitObservation] = []
        gate.set_flood_event_observer(observations.append)

        budget = _selected_dm_page_budget(adapter)
        task = asyncio.create_task(adapter.run_slice(budget))
        await _wait_for(lambda: sender.calls == 1)
        assert isinstance(sender.requests[0], functions.messages.GetMessagesRequest)
        assert sender.scopes[0].demand_kind is DemandKind.DM_DELETION_RECONCILIATION
        assert sender.scopes[0].acquisition_kind is AcquisitionKind.MESSAGE_LOOKUP
        sender.futures[0].set_exception(FloodWaitError(request=None, capture=20))
        with pytest.raises(TelegramRpcThrottled):
            await task

        assert scanner.calls == 1
        assert len(observations) == 1
        assert observations[0].actual_dispatch is True
        token_kind = DemandKind.DM_DELETION_RECONCILIATION
        assert observations[0].demand_kind == token_kind.value
        assert observations[0].acquisition_kind == AcquisitionKind.MESSAGE_LOOKUP.value
        assert accumulator.kill_switch_status().events_in_window == 1
        assert _dm_reconciliation_state_for_rpc_test(conn)["status"] == "suspended"
        assert conn.execute("SELECT is_deleted FROM messages WHERE dialog_id=211 AND message_id=1").fetchone() == (0,)
        assert sender.calls == 1
        assert adapter.status(10_000_000) is None

        import mcp_telegram.telegram_rpc as rpc

        record = json.loads(gate._test_safety_store.raw)
        expired_monotonic = rpc.time.monotonic() - 1
        expired_utc = rpc.time.time() - 1
        record["floor"].update(
            boot_id="test-boot",
            until_monotonic=expired_monotonic,
            until_utc=expired_utc,
            remaining_seconds=0.0,
        )
        gate._test_safety_store.raw = json.dumps(record, separators=(",", ":"))
        gate._safety = None
        gate._ensure_safety_ledger()
        rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() - 1
        await _dispatch_forward_history_sibling(gate, sender, conn, expected_cursor=delta_cursor or 0)

        await adapter.run_slice(RpcAttemptBudget(limit=1, selected=budget.selected))
        assert scanner.calls == 1
        assert sender.calls == 2
        assert len(observations) == 1
    finally:
        await _cleanup_raw_future_gate(gate, sender)
        conn.close()


@pytest.mark.asyncio
async def test_cancelled_dm_page_late_gate_flood_stays_claimed_across_restart() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    conn = _dm_reconciliation_test_db()
    gate, sender, adapter, scanner = _real_dm_page_adapter(conn)
    try:
        gate._rpc_circuit_status = accumulator.kill_switch_status
        gate._flood_observer = lambda **event: accumulator.observe(**event)
        observations: list[FloodWaitObservation] = []
        gate.set_flood_event_observer(observations.append)

        budget = _selected_dm_page_budget(adapter)
        task = asyncio.create_task(adapter.run_slice(budget))
        await _wait_for(lambda: sender.calls == 1)
        assert isinstance(sender.requests[0], functions.messages.GetMessagesRequest)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _dm_reconciliation_state_for_rpc_test(conn)["status"] == "verifying"
        assert not sender.futures[0].cancelled()

        sender.futures[0].set_exception(FloodWaitError(request=None, capture=20))
        await _wait_for(lambda: not _pending_owned_states(gate))
        assert len(observations) == 1
        assert observations[0].actual_dispatch is True
        assert observations[0].demand_kind == DemandKind.DM_DELETION_RECONCILIATION.value
        assert observations[0].acquisition_kind == AcquisitionKind.MESSAGE_LOOKUP.value
        assert accumulator.kill_switch_status().events_in_window == 1
        restarted_scanner = _real_dm_page_scanner(gate, conn)
        restarted = DmDeletionReconciliationDemandAdapter(
            conn, restarted_scanner, writer=inline_writer(conn), account_id=123
        )
        assert restarted.status(10_000_000) is None
        prepare_dm_deletion_reconciliation(conn, now=200)
        assert _dm_reconciliation_state_for_rpc_test(conn)["status"] == "suspended"
        assert _dm_reconciliation_state_for_rpc_test(conn)["reason"] == "interrupted"
        await restarted.run_slice(RpcAttemptBudget(limit=1, selected=budget.selected))
        assert scanner.calls == 1
        assert restarted_scanner.calls == 0
        assert sender.calls == 1
    finally:
        await _cleanup_raw_future_gate(gate, sender)
        conn.close()


@pytest.mark.asyncio
async def test_dm_local_deferral_after_nested_dispatch_keeps_page_claimed() -> None:
    gate = _gate()
    sender = _set_sender(gate, _request_value)

    class Scanner:
        async def run_dm_gap_scan_page(self, dialog_id: int, message_ids: Sequence[int]) -> int:
            del dialog_id, message_ids
            with rpc_scope(TelegramRpcSource.DELTA_SYNC):
                await gate(_TestRequest("nested-dispatch"))
            raise TelegramRpcAdmissionDeferred(retry_after_seconds=3)

    conn = _dm_reconciliation_test_db()
    prepare_dm_deletion_reconciliation(conn, now=100)
    adapter = DmDeletionReconciliationDemandAdapter(conn, Scanner(), writer=inline_writer(conn), account_id=123)
    budget = _selected_dm_page_budget(adapter)
    with pytest.raises(TelegramRpcAdmissionDeferred):
        await adapter.run_slice(budget)

    state = _dm_reconciliation_state_for_rpc_test(conn)
    assert state["status"] == "verifying"
    assert state["message_cursor"] == 0
    assert budget.attempts == 1
    assert sender.calls == 1
    assert adapter.status(10_000_000) is None
    prepare_dm_deletion_reconciliation(conn, now=200)
    assert _dm_reconciliation_state_for_rpc_test(conn)["reason"] == "interrupted"
    assert sender.calls == 1
    await gate.close_rpc_scheduler()
    conn.close()


@pytest.mark.asyncio
async def test_abandoned_actual_future_keeps_capacity_and_records_later_flood() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate = _gate()
    gate._scheduler_policy = TelegramRpcSchedulerConfig(interactive_queue_capacity=1)
    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=gate._scheduler_policy,
        limiter=gate._limiter,
        minimum_spacing_seconds=gate._test_minimum_spacing_seconds,
        readiness=RpcTransportReadiness(
            probe=gate._scheduler_transport_ready,
            wait=gate._wait_for_scheduler_transport,
        ),
    )
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    caller = asyncio.create_task(_call(gate, _TestRequest("actual")))
    await _wait_for(lambda: sender.calls == 1)
    raw_future = sender.futures[0]
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not raw_future.cancelled()
    assert len(_pending_owned_states(gate)) == 1
    assert gate._admission_scheduler.active_depths()[RpcServiceClass.INTERACTIVE] == 1
    with pytest.raises(TelegramRpcAdmissionDeferred):
        await _call(gate, _TestRequest("blocked"))
    raw_future.set_exception(FloodWaitError(request=None, capture=20))
    await _wait_for(lambda: not _pending_owned_states(gate))
    assert accumulator.kill_switch_status().events_in_window == 1
    await _wait_for(lambda: gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0))
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_five_abandoned_actual_futures_still_latch_the_account() -> None:
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate = _gate()
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    gate._rpc_circuit_status = accumulator.kill_switch_status
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    callers = [asyncio.create_task(_call(gate, _TestRequest(index))) for index in range(5)]
    await _wait_for(lambda: sender.calls == len(callers))
    for caller in callers:
        caller.cancel()
    assert all(
        isinstance(result, asyncio.CancelledError) for result in await asyncio.gather(*callers, return_exceptions=True)
    )
    for future in sender.futures:
        future.set_exception(FloodWaitError(request=None, capture=1))
    await _wait_for(lambda: not _pending_owned_states(gate))
    await _wait_for(lambda: gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0))

    assert accumulator.kill_switch_status().open is True
    assert accumulator.kill_switch_status().events_in_window == 5
    assert gate._limiter.acquisitions == 5
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_already_completed_actual_futures_finalize_once_for_success_and_flood() -> None:
    gate = _gate()
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate._flood_observer = lambda **event: accumulator.observe(**event)

    class _DoneSender:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, _request: object, *, ordered: bool = False) -> asyncio.Future[object]:
            del ordered
            self.calls += 1
            future = asyncio.get_running_loop().create_future()
            if self.calls == 1:
                future.set_result("ok")
            else:
                future.set_exception(FloodWaitError(request=None, capture=20))
            return future

    sender = _DoneSender()
    _set_sender(gate, sender)
    assert await _call(gate, _TestRequest("success")) == "ok"
    with pytest.raises(TelegramRpcThrottled):
        await _call(gate, _TestRequest("flood"))
    assert sender.calls == 2
    assert accumulator.kill_switch_status().events_in_window == 1
    assert _pending_owned_states(gate) == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_cancelled_actual_flood_keeps_durable_receipt_and_lease_until_shutdown_drain() -> None:  # noqa: PLR0915
    gate = _gate()
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    commit = asyncio.Event()
    try:
        saving = asyncio.Event()
        caller = asyncio.create_task(_call(gate, _TestRequest("actual")))
        await _wait_for(lambda: sender.calls == 1)
        writes_before_flood = len(gate._test_safety_store.writes)
        gate._test_safety_store.block_floor_saves = True
        gate._test_safety_store.floor_save_started = saving
        gate._test_safety_store.floor_save_release = commit
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        error = FloodWaitError(request=None, capture=20)
        sender.futures[0].set_exception(error)
        await saving.wait()
        assert account_cooldown_deadline() > asyncio.get_running_loop().time()
        assert gate._admission_scheduler.active_depths()[RpcServiceClass.INTERACTIVE] == 1

        observer = asyncio.create_task(gate._observe_flood(error))
        await asyncio.sleep(0)
        assert not observer.done()
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        closing = asyncio.create_task(gate.close_rpc_scheduler())
        await asyncio.sleep(0)
        assert not closing.done()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        assert len(gate._test_safety_store.writes) == writes_before_flood
        commit.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert gate._test_safety_store.writes
        assert len(gate._test_safety_store.writes) > writes_before_flood
        assert float(json.loads(gate._test_safety_store.raw)["floor"]["until_utc"]) > 0
        assert _pending_owned_states(gate) == {}
        assert gate._cooldown_save_tasks == {}
        assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    finally:
        commit.set()
        await _cleanup_raw_future_gate(gate, sender)


@pytest.mark.asyncio
async def test_actual_future_flood_protects_when_persistence_and_event_sinks_fail() -> None:
    gate = _gate()
    sender = _RawFutureSender()
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    _set_sender(gate, sender)
    gate._flood_observer = lambda **event: accumulator.observe(**event)

    try:
        gate.set_flood_event_observer(lambda _event: (_ for _ in ()).throw(OSError()))
        caller = asyncio.create_task(_call(gate, _TestRequest("actual")))
        await _wait_for(lambda: sender.calls == 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        gate._test_safety_store.fail_floor_next = True
        sender.futures[0].set_exception(FloodWaitError(request=None, capture=20))
        await _wait_for(
            lambda: (
                not _pending_owned_states(gate)
                and not gate._cooldown_save_tasks
                and gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
            )
        )
        assert account_cooldown_deadline() > asyncio.get_running_loop().time()
        assert accumulator.kill_switch_status().events_in_window == 1
    finally:
        await _cleanup_raw_future_gate(gate, sender)


@pytest.mark.asyncio
async def test_disconnect_drains_completed_actual_flood_after_caller_cancellation() -> None:
    gate = _gate()
    sender = _RawFutureSender()
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    _set_sender(gate, sender)
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    caller = asyncio.create_task(_call(gate, _TestRequest("actual")))
    await _wait_for(lambda: sender.calls == 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    sender.futures[0].set_exception(FloodWaitError(request=None, capture=20))
    await gate._disconnect_main_sender()
    assert sender.disconnect_calls == 1
    assert accumulator.kill_switch_status().events_in_window == 1
    assert _pending_owned_states(gate) == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_failed_owned_disconnect_records_unknown_and_blocks_transport_reuse() -> None:
    gate = _gate()

    class _FailingDisconnectSender(_RawFutureSender):
        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            for future in self.futures:
                future.cancel()
            raise OSError("transport teardown failed")

        def _keepalive_ping(self, _random_id: int) -> None:
            raise AssertionError("failed transport must not forward keepalive ping")

    sender = _FailingDisconnectSender()
    _set_sender(gate, sender)
    caller = asyncio.create_task(_call(gate, _TestRequest("actual")))
    await _wait_for(lambda: sender.calls == 1)
    raw_future = sender.futures[0]
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    with pytest.raises(OSError, match="teardown failed"):
        await gate._disconnect_main_sender()
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert raw_future.cancelled()
    assert _pending_owned_states(gate) == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    record = json.loads(gate._test_safety_store.raw)
    assert record["protection"] is not None or record["intents"]
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await _call(gate, _TestRequest("blocked"))
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate.connect()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._switch_dc(3)
    _MainSenderAdapter(gate)._keepalive_ping(1)
    with pytest.raises(OSError, match="transport teardown failed"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor_failure", ["cancelled_future", "local_exception"])
async def test_vendor_transport_failure_settles_actual_owned_state_without_reuse(vendor_failure: str) -> None:
    gate = _gate()

    class _FailingDisconnectProvider(_RawFutureSender):
        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            if self.futures:
                if vendor_failure == "cancelled_future":
                    self.futures[0].cancel()
                else:
                    self.futures[0].set_exception(OSError("local sender failure"))
            raise OSError("fake transport teardown failed")

    sender = _FailingDisconnectProvider()
    _set_sender(gate, sender)
    caller = asyncio.create_task(_call(gate, _TestRequest("teardown failure")))
    await _wait_for(lambda: sender.calls == 1)
    state = next(iter(_pending_owned_states(gate)))
    owned_future = state.future
    caller.cancel()
    assert isinstance((await asyncio.gather(caller, return_exceptions=True))[0], asyncio.CancelledError)

    with pytest.raises(OSError, match="fake transport teardown failed"):
        await gate._disconnect_main_sender()
    assert owned_future.done()
    if vendor_failure == "cancelled_future":
        assert not owned_future.cancelled()
        assert isinstance(owned_future.exception(), asyncio.CancelledError)
    else:
        assert not owned_future.cancelled()
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert _pending_owned_states(gate) == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    record = json.loads(gate._test_safety_store.raw)
    assert record["protection"] is not None or record["intents"]
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await _call(gate, _TestRequest("blocked"))
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate.connect()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._switch_dc(3)
    _MainSenderAdapter(gate)._keepalive_ping(1)
    with pytest.raises(OSError, match="fake transport teardown failed"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_installed_vendor_internal_failure_with_zero_pending_cannot_confirm_teardown() -> None:
    gate = _gate()

    class _FailingDisconnectProvider(_RawFutureSender):
        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            raise OSError("internal transport teardown failed")

    sender = _FailingDisconnectProvider()
    _set_sender(gate, sender)

    with pytest.raises(OSError, match="internal transport teardown failed"):
        await gate._disconnect_main_sender()

    assert sender.disconnect_calls == 1
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert _pending_owned_states(gate) == {}
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await _call(gate, _TestRequest("blocked"))
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate.connect()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._switch_dc(3)
    _MainSenderAdapter(gate)._keepalive_ping(1)
    with pytest.raises(OSError, match="internal transport teardown failed"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_installed_vendor_internal_teardown_in_progress_cannot_be_taken_over() -> None:
    gate = _gate()
    disconnect_started = asyncio.Event()
    disconnect_release = asyncio.Event()

    class _BlockedDisconnectProvider(_RawFutureSender):
        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            disconnect_started.set()
            await disconnect_release.wait()
            raise OSError("internal transport teardown failed")

    sender = _BlockedDisconnectProvider()
    _set_sender(gate, sender)

    first = asyncio.create_task(gate._disconnect_main_sender())
    await disconnect_started.wait()
    second = asyncio.create_task(gate._disconnect_main_sender())
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    assert sender.disconnect_calls == 1
    assert gate._transport_state is _TransportBoundaryState.DISCONNECTING

    disconnect_release.set()
    with pytest.raises(OSError, match="internal transport teardown failed"):
        await first
    with pytest.raises(OSError, match="internal transport teardown failed"):
        await second
    assert sender.disconnect_calls == 1
    assert gate._transport_state is _TransportBoundaryState.FAILED
    with pytest.raises(OSError, match="internal transport teardown failed"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_teardown_fails", [False, True])
async def test_overlapping_owned_disconnects_share_one_teardown_and_settle_unknown(
    first_teardown_fails: bool,
) -> None:
    """Concurrent disconnect callers share the real sender's teardown owner."""
    gate = _gate()
    disconnect_started = asyncio.Event()
    disconnect_release = asyncio.Event()

    class _BlockedDisconnectProvider(_RawFutureSender):
        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            disconnect_started.set()
            await disconnect_release.wait()
            if first_teardown_fails:
                raise OSError("first teardown failed")

    sender = _BlockedDisconnectProvider()
    _set_sender(gate, sender)
    caller = asyncio.create_task(_call(gate, _TestRequest("pending during teardown")))
    await _wait_for(lambda: sender.calls == 1)
    state = next(iter(_pending_owned_states(gate)))
    response_future = state.future
    caller.cancel()
    assert isinstance((await asyncio.gather(caller, return_exceptions=True))[0], asyncio.CancelledError)
    first = asyncio.create_task(gate._disconnect_main_sender())
    await disconnect_started.wait()
    second = asyncio.create_task(gate._disconnect_main_sender())
    await asyncio.sleep(0)
    assert gate._transport_state is _TransportBoundaryState.DISCONNECTING
    assert not first.done() and not second.done()
    assert sender.disconnect_calls == 1

    disconnect_release.set()
    if first_teardown_fails:
        with pytest.raises(OSError, match="first teardown failed"):
            await first
        with pytest.raises(OSError, match="first teardown failed"):
            await second
        assert gate._transport_state is _TransportBoundaryState.FAILED
    else:
        await first
        await second
        assert gate._transport_state is _TransportBoundaryState.DISCONNECTED
    assert sender.disconnect_calls == 1
    assert response_future.done() and response_future.cancelled()
    assert _pending_owned_states(gate) == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    record = json.loads(gate._test_safety_store.raw)
    assert record["protection"] is not None or record["intents"]
    if first_teardown_fails:
        with pytest.raises(OSError, match="first teardown failed"):
            await gate.close_rpc_scheduler()
    else:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["local_error", "cancelled", "result", "flood_wait"])
async def test_confirmed_installed_sender_outcomes_release_once_after_owned_teardown(outcome: str) -> None:
    """Real queued outcomes are settled and recorded by the owned transport."""
    gate = _gate()
    flood_error = FloodWaitError(request=None, capture=20)
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    observed_floods: list[FloodWaitObservation] = []
    gate.set_flood_event_observer(observed_floods.append)

    class _DisconnectOutcomeProvider(_RawFutureSender):
        def __init__(self) -> None:
            super().__init__()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            if outcome == "local_error":
                raise OSError("local sender failure")
            if outcome == "result":
                self.futures[0].set_result("response")
            elif outcome == "flood_wait":
                self.futures[0].set_exception(flood_error)
            if outcome in {"result", "flood_wait"}:
                await _wait_for(lambda: not _pending_owned_states(gate))

    sender = _DisconnectOutcomeProvider()
    _set_sender(gate, sender)
    caller = asyncio.create_task(_call(gate, _TestRequest("disconnect outcome")))
    await _wait_for(lambda: sender.calls == 1)
    owned_states = _pending_owned_states(gate)
    assert len(owned_states) == 1
    raw_future = next(iter(owned_states)).future
    caller.cancel()
    assert isinstance((await asyncio.gather(caller, return_exceptions=True))[0], asyncio.CancelledError)

    if outcome == "local_error":
        with pytest.raises(OSError, match="local sender failure"):
            await gate._disconnect_main_sender()
    else:
        await gate._disconnect_main_sender()
    assert sender.disconnect_calls == 1
    assert raw_future.done()
    assert raw_future.cancelled() is (outcome in {"cancelled", "local_error"})
    assert _pending_owned_states(gate) == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    if outcome == "local_error":
        assert gate._transport_state is _TransportBoundaryState.FAILED
    else:
        assert gate._transport_state is _TransportBoundaryState.DISCONNECTED
    if outcome == "flood_wait":
        assert len(observed_floods) == 1
        assert observed_floods[0].actual_dispatch is True
        assert observed_floods[0].origin == "actual_send"
        assert accumulator.kill_switch_status().events_in_window == 1
    if outcome in {"cancelled", "local_error"}:
        record = json.loads(gate._test_safety_store.raw)
        assert record["protection"] is not None or record["intents"], record
    if outcome == "local_error":
        with pytest.raises(OSError, match="local sender failure"):
            await gate.close_rpc_scheduler()
    else:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_installed_vendor_auth_init_disconnect_failure_parks_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """An auth-init failure cannot turn vendor reference clearing into recovery."""
    from telethon.errors import SecurityError

    gate, _ = _bootstrap_gate()

    class _AuthFailureProvider(_BootstrapSender):
        def __init__(self) -> None:
            super().__init__()

        async def connect(self, _connection: object) -> bool:
            self.connect_calls += 1
            raise SecurityError("authentication failed")

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            raise OSError("internal transport teardown failed")

    sender = _AuthFailureProvider()
    _set_sender(gate, sender)
    forwarded_pings: list[int] = []
    observed_errors: list[TelegramRpcAdmissionDeferred] = []
    actual_connect = gate.connect

    async def capture_gate_failure() -> None:
        try:
            await actual_connect()
        except TelegramRpcAdmissionDeferred as exc:
            observed_errors.append(exc)
            raise

    monkeypatch.setattr(gate, "connect", capture_gate_failure)
    shutdown = asyncio.Event()
    published_scopes: list[object] = []

    async def publish_auth_scope(scope: object) -> None:
        published_scopes.append(scope)

    context = cast(
        _SyncMainContext,
        SimpleNamespace(
            client=gate,
            api_server=SimpleNamespace(startup_detail="", _ready=False, _publish_auth_scope=publish_auth_scope),
            shutdown_event=shutdown,
        ),
    )
    parked = asyncio.create_task(_connect_telegram(context))
    await _wait_for(lambda: bool(observed_errors))

    assert gate._main_sender._connection is None
    assert sender.connect_calls == 1
    assert sender.disconnect_calls == 1
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
    assert isinstance(observed_errors[0].__cause__, SecurityError)
    assert "termination is unconfirmed" in str(observed_errors[0])
    assert context.api_server._ready is True
    assert context.api_server.self_id == 1
    assert len(published_scopes) == 1
    assert not parked.done()
    await _assert_failed_gate_blocks_api_migration_and_ping(gate, forwarded_pings)
    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await gate.connect()
    assert sender.connect_calls == 1
    assert sender.disconnect_calls == 1

    shutdown.set()
    assert await parked is False
    with pytest.raises(OSError, match="internal transport teardown failed"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_installed_vendor_connect_cancellation_retains_failed_bootstrap_owner() -> None:
    gate, _ = _bootstrap_gate()
    connect_started = asyncio.Event()

    class _BlockingConnectProvider(_BootstrapSender):
        async def connect(self, _connection: object) -> bool:
            self.connect_calls += 1
            connect_started.set()
            await asyncio.Future()
            raise AssertionError("blocked connect unexpectedly resumed")

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            raise AssertionError("failed initialization must not manufacture confirmation")

    sender = _BlockingConnectProvider()
    _set_sender(gate, sender)
    shutdown = asyncio.Event()
    context = cast(
        _SyncMainContext,
        SimpleNamespace(
            client=gate,
            api_server=SimpleNamespace(startup_detail="", _ready=False),
            shutdown_event=shutdown,
        ),
    )
    caller = asyncio.create_task(_connect_telegram(context))
    await connect_started.wait()
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not shutdown.is_set()
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await gate.connect()
    with pytest.raises(AssertionError, match="failed initialization must not manufacture confirmation"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_actual_failed_bootstrap_parks_daemon_until_shutdown_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender = _bootstrap_gate()
    sender.response = lambda _request: FloodWaitError(request=None, capture=20)
    sender.disconnect_error = OSError("transport teardown failed")
    observed_errors: list[TelegramRpcAdmissionDeferred] = []
    actual_connect = gate.connect

    async def capture_gate_failure() -> None:
        try:
            await actual_connect()
        except TelegramRpcAdmissionDeferred as exc:
            observed_errors.append(exc)
            raise

    monkeypatch.setattr(gate, "connect", capture_gate_failure)
    shutdown = asyncio.Event()

    published_scopes: list[object] = []

    async def publish_auth_scope(scope: object) -> None:
        published_scopes.append(scope)

    api_server = SimpleNamespace(startup_detail="", _ready=False, _publish_auth_scope=publish_auth_scope)
    daemon_context = cast(
        _SyncMainContext,
        SimpleNamespace(client=gate, api_server=api_server, shutdown_event=shutdown),
    )
    parked = asyncio.create_task(_connect_telegram(daemon_context))
    await _wait_for(lambda: bool(observed_errors))
    assert isinstance(observed_errors[0].__cause__, TelegramRpcThrottled)
    assert "termination is unconfirmed" in api_server.startup_detail
    assert api_server._ready is True
    assert api_server.self_id == 1
    assert len(published_scopes) == 1
    assert not parked.done()
    assert sender.connect_calls == 1
    assert sender.disconnect_calls == 1
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None

    shutdown.set()
    assert await parked is False
    assert sender.connect_calls == 1
    with pytest.raises(OSError, match="transport teardown failed"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_cancelled_failed_bootstrap_cleanup_keeps_reservation_and_does_not_park() -> None:
    gate, sender = _bootstrap_gate()
    sender.response = lambda _request: FloodWaitError(request=None, capture=20)
    sender.disconnect_started = asyncio.Event()
    sender.disconnect_release = asyncio.Event()
    sender.disconnect_error = OSError("transport teardown failed")
    shutdown = asyncio.Event()
    context = cast(
        _SyncMainContext,
        SimpleNamespace(
            client=gate,
            api_server=SimpleNamespace(startup_detail="", _ready=False),
            shutdown_event=shutdown,
        ),
    )
    caller = asyncio.create_task(_connect_telegram(context))
    await sender.disconnect_started.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    sender.disconnect_release.set()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not shutdown.is_set()
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await gate.connect()
    with pytest.raises(OSError, match="transport teardown failed"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_ready_connected_connect_is_zero_work() -> None:
    gate, sender = _bootstrap_gate()
    try:
        await gate.connect()
        handles = (gate._updates_handle, gate._keepalive_handle)
        sent = tuple(sender.sent)
        acquisitions = gate._limiter.acquisitions
        await gate.connect()

        assert sender.connect_calls == 1
        assert sender.disconnect_calls == 0
        assert tuple(sender.sent) == sent
        assert gate._limiter.acquisitions == acquisitions
        assert (gate._updates_handle, gate._keepalive_handle) == handles
        assert gate._connect_owner is None
        assert gate._connection_capability is None
        assert gate._connection_rpc_tasks == set()
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_gate_cancellation_after_admission_cannot_leak_active_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()
    original_admit = gate._admission_scheduler.admit

    async def admit_then_cancel(scope: TelegramRpcScope) -> RpcAdmission:
        admission = await original_admit(scope)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return admission

    sends = 0

    async def send(request: object) -> object:
        nonlocal sends
        sends += 1
        await asyncio.sleep(0)
        return _request_value(request)

    monkeypatch.setattr(gate._admission_scheduler, "admit", admit_then_cancel)
    _set_sender(gate, send)

    caller = asyncio.create_task(_call(gate, "request"))
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert sends == 0
    assert _pending_owned_states(gate) == {}


@pytest.mark.asyncio
async def test_scheduler_close_keeps_dispatched_transport_until_disconnect() -> None:
    gate = _gate()
    transport_started = asyncio.Event()
    in_flight: asyncio.Future[object] | None = None

    class _PendingSender:
        def is_connected(self) -> bool:
            return True

        def _transport_connected(self) -> bool:
            return True

        def send(self, _request: object, *, ordered: bool = False) -> asyncio.Future[object]:
            nonlocal in_flight
            del ordered
            in_flight = asyncio.get_running_loop().create_future()
            transport_started.set()
            return in_flight

        async def disconnect(self) -> None:
            return None

    _set_sender(gate, _PendingSender())
    caller = asyncio.create_task(_call(gate, "request"))
    await transport_started.wait()

    await gate.close_rpc_scheduler()

    assert caller.cancelled()
    assert in_flight is not None and not in_flight.cancelled()
    assert _pending_owned_states(gate) == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    record = json.loads(gate._test_safety_store.raw)
    assert record["protection"] is not None or record["intents"]


@pytest.mark.asyncio
async def test_recursive_telethon_resolution_reenters_admission_without_deadlock() -> None:
    gate = _gate()
    sent: list[object] = []
    events: list[RpcAdmissionEvent] = []
    gate._scheduler_policy = TelegramRpcSchedulerConfig(interactive_queue_capacity=1)
    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=gate._scheduler_policy,
        limiter=gate._limiter,
        minimum_spacing_seconds=gate._test_minimum_spacing_seconds,
        observer=events.append,
        readiness=RpcTransportReadiness(
            probe=gate._scheduler_transport_ready,
            wait=gate._wait_for_scheduler_transport,
        ),
    )

    request = functions.help.GetConfigRequest()

    async def resolve(client: TelegramClient, utils: object) -> None:
        del utils
        await client(functions.PingRequest(99))

    request.resolve = resolve  # type: ignore[method-assign]

    class _Sender:
        def send(self, request: object, *, ordered: bool = False) -> object:
            del ordered
            sent.append(request)

            async def complete() -> object:
                return request

            return complete()

    _set_sender(gate, _Sender())
    gate._loop = None
    gate._request_retries = 0
    gate._raise_last_call_error = True
    gate._flood_waited_requests = {}
    gate._no_updates = False
    gate._log = {"telethon.client.users": logging.getLogger(__name__)}
    gate.flood_sleep_threshold = 0
    gate.session.process_entities = lambda _result: None

    with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE):
        result = await asyncio.wait_for(gate(request), timeout=1.0)

    assert result is request
    assert [type(sent_request) for sent_request in sent] == [
        functions.PingRequest,
        functions.help.GetConfigRequest,
    ]
    assert gate._limiter.acquisitions == 2
    dispatched = [event for event in events if event.kind is RpcAdmissionEventKind.DISPATCHED]
    assert len(dispatched) == 2
    assert all(event.active_depth == 1 and event.total_outstanding == 1 for event in dispatched)
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)


@pytest.mark.asyncio
async def test_update_difference_flood_wait_returns_to_durable_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    gate = _gate()
    attempts = 0

    def send(request: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FloodWaitError(request=None, capture=1)
        return _request_value(request)

    _set_sender(gate, send)
    deadline = asyncio.get_running_loop().time() + 4
    with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
            with pytest.raises(TelegramRpcThrottled) as caught:
                await gate(_TestRequest("difference"))

    assert caught.value.retry_after_seconds == 1
    assert attempts == 1
    assert gate._limiter.acquisitions == 1


@pytest.mark.asyncio
async def test_update_difference_open_circuit_waits_without_fatal_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()
    circuit_open = True
    sleeps: list[float] = []

    def status() -> _CircuitStatus:
        return _CircuitStatus(open=circuit_open)

    async def close_circuit(delay: float) -> None:
        nonlocal circuit_open
        sleeps.append(delay)
        circuit_open = False

    gate._rpc_circuit_status = status
    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", close_circuit)
    with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE):
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
            with pytest.raises(TelegramRpcThrottled) as caught:
                await gate(_TestRequest("difference"))

    assert caught.value.latched
    assert sleeps == []
    assert gate._limiter.acquisitions == 0


@pytest.mark.asyncio
async def test_update_difference_expired_scope_returns_without_sending() -> None:
    gate = _gate()
    events: list[RpcAdmissionEvent] = []
    gate.set_rpc_admission_observer(events.append)
    request = functions.updates.GetDifferenceRequest(pts=1, date=None, qts=0)
    deadline = asyncio.get_running_loop().time() - 1

    with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
            with pytest.raises(OSError, match="admission deadline elapsed"):
                await asyncio.wait_for(gate(request), timeout=0.1)

    assert gate._test_server.seen == set()
    assert gate._limiter.acquisitions == 0
    assert [event.kind for event in events] == [RpcAdmissionEventKind.EXPIRED]
    assert events[0].reason == "deadline_elapsed"


@pytest.mark.asyncio
async def test_update_difference_saturation_wait_is_bounded_by_original_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()
    request = functions.updates.GetDifferenceRequest(pts=1, date=None, qts=0)
    deadline = asyncio.get_running_loop().time() + 0.01
    calls = 0

    async def saturated(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        scope = current_rpc_scope()
        raise RpcAdmissionSaturatedError(scope, "saturated")

    monkeypatch.setattr(gate, "_dispatch_attempt", saturated)
    with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
            with pytest.raises(OSError, match="admission deadline elapsed"):
                await asyncio.wait_for(gate(request), timeout=0.2)

    assert calls == 1
    assert gate._test_server.seen == set()


@pytest.mark.asyncio
async def test_update_difference_open_circuit_is_reported_before_scope_deadline() -> None:
    gate = _gate(_CircuitStatus(open=True))
    request = functions.updates.GetDifferenceRequest(pts=1, date=None, qts=0)
    deadline = asyncio.get_running_loop().time() + 0.01

    with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
            with pytest.raises(TelegramRpcThrottled) as caught:
                await gate(request)

    assert caught.value.latched
    assert gate._test_server.seen == set()


@pytest.mark.asyncio
async def test_update_difference_flood_does_not_retry_inside_vendor_call() -> None:
    gate = _gate()
    request = functions.updates.GetDifferenceRequest(pts=1, date=None, qts=0)
    deadline = asyncio.get_running_loop().time() + 0.01

    def flood_once(_request: object) -> object:
        raise FloodWaitError(request=None, capture=30)

    _set_sender(gate, flood_once)
    with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
            with pytest.raises(TelegramRpcThrottled):
                await gate(request)

    assert len(gate._test_server.seen) == 1
    assert gate._limiter.acquisitions == 1


@pytest.mark.asyncio
async def test_update_difference_cancellation_during_saturation_wait_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()
    request = functions.updates.GetDifferenceRequest(pts=1, date=None, qts=0)
    deadline = asyncio.get_running_loop().time() + 5
    waiting = asyncio.Event()

    async def saturated(*_args: object, **_kwargs: object) -> object:
        scope = current_rpc_scope()
        raise RpcAdmissionSaturatedError(scope, "saturated")

    async def wait_forever(_delay: float) -> None:
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(gate, "_dispatch_attempt", saturated)
    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", wait_forever)
    caller = asyncio.create_task(gate(request))
    try:
        await waiting.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
    finally:
        if not caller.done():
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)

    assert gate._test_server.seen == set()


@pytest.mark.asyncio
async def test_update_difference_next_outer_iteration_gets_a_fresh_scope() -> None:
    gate = _gate()
    sender = _set_sender(gate, _request_value)
    request = functions.updates.GetDifferenceRequest(pts=1, date=None, qts=0)
    expired_deadline = asyncio.get_running_loop().time() - 1
    with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=expired_deadline):
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=expired_deadline):
            with pytest.raises(OSError):
                await gate(request)

    deadline = asyncio.get_running_loop().time() + 5
    with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, deadline=deadline):
            assert await gate(request) is request

    assert sender.calls == 1


@pytest.mark.asyncio
async def test_telethon_update_loop_scopes_difference_and_live_dispatch_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()
    seen: list[TelegramRpcScope] = []

    async def call_with_policy(
        _request: object,
        *,
        ordered: bool,
        scope: TelegramRpcScope,
        connection_owned: bool,
    ) -> object:
        del ordered, connection_owned
        seen.append(scope)
        return object()

    async def base_dispatch_update(_self: TelegramClient, _update: object) -> None:
        seen.append(current_rpc_scope())

    async def base_update_loop(client: TelegramClient) -> None:
        await client(functions.updates.GetDifferenceRequest(pts=1, date=None, qts=0))
        await client._dispatch_update("ordinary-live-update")

    monkeypatch.setattr(gate, "_call_with_source_policy", call_with_policy)
    monkeypatch.setattr(TelegramClient, "_dispatch_update", base_dispatch_update)
    monkeypatch.setattr(TelegramClient, "_update_loop", base_update_loop)
    await gate._update_loop()

    assert [(scope.demand_kind, scope.source, scope.acquisition_kind) for scope in seen] == [
        (
            DemandKind.TELETHON_UPDATE_DIFFERENCE,
            TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE,
            AcquisitionKind.UPDATE_DIFFERENCE,
        ),
        (
            DemandKind.REALTIME_EVENT_ACQUISITION,
            TelegramRpcSource.REALTIME_EVENT,
            None,
        ),
    ]
    with pytest.raises(UnclassifiedTelegramRpcError):
        current_rpc_scope()


@pytest.mark.asyncio
async def test_telethon_update_child_task_owns_realtime_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    seen: list[
        tuple[DemandKind | None, TelegramRpcSource, asyncio.Task[object] | None, asyncio.Task[object] | None]
    ] = []

    async def base_dispatch_update(_self: TelegramClient, _update: object) -> None:
        scope = current_rpc_scope()
        seen.append((scope.demand_kind, scope.source, scope.owner_task, asyncio.current_task()))

    monkeypatch.setattr(TelegramClient, "_dispatch_update", base_dispatch_update)
    child = asyncio.create_task(gate._dispatch_update("update"))
    await child

    assert seen == [(DemandKind.REALTIME_EVENT_ACQUISITION, TelegramRpcSource.REALTIME_EVENT, child, child)]


@pytest.mark.asyncio
async def test_execution_deadline_keeps_raw_response_lease_until_completion() -> None:
    gate = _gate()
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE, timeout_seconds=0.005):
        with pytest.raises(TimeoutError):
            await gate(_TestRequest("slow"))
    assert sender.calls == 1
    raw_future = sender.futures[0]
    assert not raw_future.done()
    assert gate._admission_scheduler.active_depths()[RpcServiceClass.INTERACTIVE] == 1
    await asyncio.sleep(0.05)
    assert not raw_future.done()
    raw_future.set_result("late success")
    await _wait_for(lambda: not _pending_owned_states(gate))
    await _wait_for(lambda: gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0))
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    await gate.close_rpc_scheduler()
