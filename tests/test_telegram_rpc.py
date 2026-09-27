# pyright: reportAny=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import telethon
from telethon import TelegramClient, functions, types
from telethon.errors import (
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    NetworkMigrateError,
    ServerError,
    SlowModeWaitError,
    UserMigrateError,
)
from telethon.network.mtprotosender import MTProtoSender
from telethon.requestiter import RequestIter
from telethon.sessions import StringSession
from telethon.tl.tlobject import TLRequest

from mcp_telegram.config import (
    FloodWaitConfig,
    McpTelegramConfig,
    StateConfig,
    TelegramRpcConfig,
    TelegramRpcSchedulerConfig,
)
from mcp_telegram.daemon import _connect_telegram, _SyncMainContext
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
from mcp_telegram.sync_db import (
    _apply_migrations,
    ensure_sync_schema,
    load_account_cooldown_until_utc,
    save_account_cooldown_until_utc,
)
from mcp_telegram.telegram import create_client
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    current_demand_token,
    demand_context,
    demand_contract,
    transferred_demand_context,
)
from mcp_telegram.telegram_rpc import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcBudget,
    TelegramRpcCooldownPersistence,
    TelegramRpcGate,
    TelegramRpcSource,
    UnclassifiedTelegramRpcError,
    _MainSenderAdapter,
    _TransportBoundaryState,
    account_cooldown_deadline,
    current_rpc_scope,
    reset_account_cooldown,
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
from tests.history_enrollment_helpers import seed_full_history_enrollment


@dataclass(frozen=True, slots=True)
class _CircuitStatus:
    open: bool

    def detail(self) -> str:
        return "open-for-test"


class _Limiter:
    def __init__(self) -> None:
        self.acquisitions = 0

    async def acquire(self) -> None:
        self.acquisitions += 1


class _TestRequest(TLRequest):
    CONSTRUCTOR_ID = 0x12345678

    def __init__(self, value: object) -> None:
        self.value = value


class _DelayedResolveRequest(TLRequest):
    CONSTRUCTOR_ID = 0x23456789

    def __init__(self, value: int, resolved: asyncio.Event, release: asyncio.Event) -> None:
        self.value = value
        self._resolved = resolved
        self._release = release

    async def resolve(self, client: object, utils: object) -> None:
        del client, utils
        if self.value == 2:
            self._resolved.set()
            await self._release.wait()


class _Sender:
    def __init__(self, callback: Callable[[object], object | Awaitable[object]]) -> None:
        self._callback = callback
        self.calls = 0

    def send(self, request: object, *, ordered: bool = False) -> Awaitable[object]:
        del ordered
        self.calls += 1

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
        self.disconnect_calls = 0
        self._connected = True

    def send(self, _request: object, *, ordered: bool = False) -> asyncio.Future[object]:
        del ordered
        self.calls += 1
        self.requests.append(_request)
        future = asyncio.get_running_loop().create_future()
        self.futures.append(future)
        return future

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _transport_connected(self) -> bool:
        return self._connected


class _BootstrapSender:
    def __init__(self) -> None:
        self.auth_key = SimpleNamespace(key=b"test")
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
        self.sent.append(request)
        self.scopes.append(current_rpc_scope())
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
    return request.value if isinstance(request, _TestRequest) else request


def _set_sender(
    gate: TelegramRpcGate,
    callback: Callable[[object], object | Awaitable[object]],
) -> _Sender:
    sender = _Sender(callback)
    gate._main_sender = sender
    gate._sender = sender
    gate._transport_state = _TransportBoundaryState.READY
    return sender


@pytest.fixture(autouse=True)
def _reset_process_policy() -> None:
    reset_account_cooldown()


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


def _gate(status: _CircuitStatus | None = None, *, retry_delays: tuple[float, ...] = ()) -> TelegramRpcGate:
    status = status or _CircuitStatus(open=False)
    gate = object.__new__(TelegramRpcGate)
    gate._rpc_circuit_status = lambda: status
    gate._fallback_wait_seconds = 60
    gate._cooldown_buffer_seconds = 1.0
    gate._transient_retry_delays = retry_delays
    gate._flood_observer = lambda **_kwargs: None
    gate._limiter = _Limiter()
    gate._scheduler_policy = TelegramRpcSchedulerConfig()
    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=gate._scheduler_policy,
        limiter=gate._limiter,
        readiness=RpcTransportReadiness(
            probe=gate._scheduler_transport_ready,
            wait=gate._wait_for_scheduler_transport,
        ),
    )
    _set_sender(gate, _request_value)
    gate._loop = None
    gate._request_retries = 0
    gate._raise_last_call_error = True
    gate._flood_waited_requests = {}
    gate._no_updates = False
    gate._reconnect_event = asyncio.Event()
    gate._connect_owner = None
    gate._connection_capability = None
    gate._connection_rpc_tasks = set()
    gate._pending_scalar_dispatches = {}
    gate._transport_state = _TransportBoundaryState.READY
    gate._log = {"telethon.client.users": logging.getLogger(__name__)}
    gate.flood_sleep_threshold = 0
    gate.session = SimpleNamespace(process_entities=lambda _result: None)
    return gate


def _bootstrap_gate(status: _CircuitStatus | None = None) -> tuple[TelegramRpcGate, _BootstrapSender]:
    gate = _gate(status)
    sender = _BootstrapSender()
    gate._main_sender = sender
    gate._sender = _MainSenderAdapter(gate)
    gate._connect_owner = None
    gate._connection_capability = None
    gate._connection_rpc_tasks = set()
    gate._transport_state = _TransportBoundaryState.DISCONNECTED
    gate._updates_handle = None
    gate._keepalive_handle = None
    gate._use_ipv6 = False
    gate._proxy = None
    gate._local_addr = None
    gate._connection = lambda *_args, **_kwargs: object()
    gate._catch_up = False
    gate._init_request = SimpleNamespace(query=None)
    gate._message_box = SimpleNamespace(is_empty=lambda: False)
    gate._update_loop = _idle_bootstrap_task
    gate._keepalive_loop = _idle_bootstrap_task
    gate._log["telethon.client.telegrambaseclient"] = logging.getLogger(__name__)
    session = SimpleNamespace(
        server_address="127.0.0.1",
        port=443,
        dc_id=2,
        auth_key=None,
        save=lambda: None,
        get_input_entity=lambda _entity: (_ for _ in ()).throw(ValueError()),
        process_entities=lambda _result: None,
    )

    def set_dc(dc_id: int, address: str, port: int) -> None:
        session.dc_id = dc_id
        session.server_address = address
        session.port = port

    session.set_dc = set_dc
    gate.session = session
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


def _installed_sender(
    connection: object,
    *,
    retries: int | None = None,
    connected: bool = True,
) -> MTProtoSender:
    logger = logging.getLogger(__name__)
    loggers = {
        "telethon.network.mtprotosender": logger,
        "telethon.network.mtprotostate": logger,
        "telethon.extensions.messagepacker": logger,
    }
    if retries is None:
        sender = MTProtoSender(None, loggers=loggers)
    else:
        sender = MTProtoSender(None, loggers=loggers, retries=retries, delay=0)
    sender._connection = connection
    sender._user_connected = connected
    return sender


def _assert_confirmed_scalar_outcome_consumed(
    gate: TelegramRpcGate,
    raw_future: asyncio.Future[object],
    accumulator: FloodWaitAccumulator,
    outcome: str,
) -> None:
    assert raw_future.done()
    assert raw_future.cancelled() is (outcome == "cancelled")
    assert gate._transport_state is _TransportBoundaryState.DISCONNECTED
    assert gate._pending_scalar_dispatches == {}
    assert sum(gate._admission_scheduler.active_depths().values()) == 0
    if outcome == "flood_wait":
        assert accumulator.kill_switch_status().events_in_window == 1
    gate._finalize_scalar_dispatch(raw_future)
    gate._finalize_scalar_dispatch(raw_future)
    assert gate._pending_scalar_dispatches == {}
    assert sum(gate._admission_scheduler.active_depths().values()) == 0
    if outcome == "flood_wait":
        assert accumulator.kill_switch_status().events_in_window == 1


def _assert_frozen_flood_observation(
    observed_floods: list[FloodWaitObservation],
    request_method: str,
    admission_sequence: int,
    dispatch_at_monotonic: float,
) -> None:
    assert len(observed_floods) == 1
    observation = observed_floods[0]
    assert observation.request_method == request_method
    assert observation.admission_sequence == admission_sequence
    assert observation.dispatch_at_monotonic == dispatch_at_monotonic
    assert observation.actual_dispatch is True


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

    gate._cooldown_persistence = TelegramRpcCooldownPersistence(lambda: rpc.time.time() + 30, lambda _until: None)
    gate._restore_account_cooldown()
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
    rpc._COOLDOWN_DEADLINE = 0
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
        assert sender.scopes[0].source is caller_scope.source
        assert sender.scopes[0].deadline == caller_scope.deadline
        assert sender.scopes[0].attempt_budget is budget
        assert budget.attempts == 1
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_inherited_connect_admits_optional_get_me_and_get_state_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, sender = _bootstrap_gate()

    async def get_me(client: TelegramClient) -> object:
        await client(functions.users.GetUsersRequest([types.InputUserSelf()]))
        return object()

    async def on_login(client: TelegramClient, _me: object) -> None:
        await client(functions.updates.GetStateRequest())

    gate._message_box = SimpleNamespace(is_empty=lambda: True)
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


def _configure_bootstrap_dc_lookup(gate: TelegramRpcGate) -> None:
    async def get_dc(dc_id: int) -> object:
        return SimpleNamespace(id=dc_id, ip_address="127.0.0.2", port=443)

    gate._get_dc = get_dc


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
        assert all(scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE for scope in sender.scopes)
        assert all(scope.deadline == deadline for scope in sender.scopes)
        assert all(scope.attempt_budget is budget for scope in sender.scopes)
        assert budget.attempts == token.attempt_evidence.actual_attempts == 4
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
        assert all(scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE for scope in sender.scopes)
        assert all(scope.deadline == deadline for scope in sender.scopes)
        assert all(scope.attempt_budget is budget for scope in sender.scopes)
        assert budget.attempts == token.attempt_evidence.actual_attempts == 2
        assert sender.disconnect_calls == 1
    finally:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_real_telethon_login_migration_unwinds_to_one_owner_redirect() -> None:
    gate, sender = _bootstrap_gate()
    _configure_bootstrap_dc_lookup(gate)
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
        assert all(scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE for scope in sender.scopes)
        assert all(scope.deadline == deadline for scope in sender.scopes)
        assert all(scope.attempt_budget is budget for scope in sender.scopes)
        assert budget.attempts == token.attempt_evidence.actual_attempts == len(sender.sent)
        assert gate._connection_rpc_tasks == set()
        assert gate._connect_owner is None
        assert gate._connection_capability is None
    finally:
        await _close_bootstrap_gate(gate)


@pytest.mark.asyncio
async def test_connection_migration_exhausts_existing_redirect_slots_without_extra_send() -> None:
    gate, sender = _bootstrap_gate()
    _configure_bootstrap_dc_lookup(gate)
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
        assert budget.attempts == token.attempt_evidence.actual_attempts == 3
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
        assert budget.attempts == token.attempt_evidence.actual_attempts == 3
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
async def test_inherited_connect_cancellation_retains_unconfirmed_partial_transport() -> None:
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
    assert sender.disconnect_calls == 0
    assert sender.sent == []
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
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
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
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
        assert gate._pending_scalar_dispatches == {}
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
        return [SimpleNamespace(id=1, is_self=True)]

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
    monkeypatch.setattr("mcp_telegram.telegram.load_config", lambda: (_ for _ in ()).throw(AssertionError("reloaded")))

    gate = create_client.__wrapped__("1", "hash", session_name="snapshot", config=config)
    try:
        assert gate._limiter.max_rate == 7
        assert gate._limiter.time_period == 13.0
        assert gate._fallback_wait_seconds == 17
        assert gate._cooldown_buffer_seconds == 2.5
        assert gate._transient_retry_delays == (0.0,)
        assert gate._cooldown_persistence is None
    finally:
        gate.session.close()


def test_factory_forwards_optional_cooldown_persistence(tmp_path: Path) -> None:
    persistence = TelegramRpcCooldownPersistence(lambda: None, lambda _deadline: None)
    gate = create_client.__wrapped__(
        "1",
        "hash",
        session_name="persistent-cooldown",
        config=McpTelegramConfig(state=StateConfig(dir=tmp_path)),
        cooldown_persistence=persistence,
    )
    try:
        assert gate._cooldown_persistence is persistence
    finally:
        gate.session.close()


def test_telethon_public_helper_and_update_loop_contract_is_pinned() -> None:
    from telethon.client.updates import UpdateMethods
    from telethon.client.users import UserMethods
    from telethon.tl.custom.message import Message

    assert telethon.__version__ == "1.44.0"
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
async def test_disconnected_transport_keeps_finite_admission_deferral_when_circuit_is_closed() -> None:
    gate = _gate()
    sender = _set_sender(gate, _request_value)
    gate._transport_state = _TransportBoundaryState.DISCONNECTED

    with pytest.raises(TelegramRpcAdmissionDeferred) as caught:
        await _call(gate, "request")

    assert caught.value.latched is False
    assert caught.value.retry_after_seconds > 0
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

    gate._main_sender = gate._sender = _Sender()
    gate._loop = None
    gate._request_retries = 0
    gate._raise_last_call_error = True
    gate._flood_waited_requests = {}
    gate._no_updates = False
    gate._log = {"telethon.client.users": logging.getLogger(__name__)}
    gate.flood_sleep_threshold = 0
    gate.session = SimpleNamespace(process_entities=lambda _result: None)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", fake_sleep)
    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, functions.PingRequest(1))

    assert caught.value.__cause__ is final
    assert caught.value.retry_after_seconds == 7
    assert gate._sender.calls == 2
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
    gate.session = SimpleNamespace(process_entities=process_entities)

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
    import mcp_telegram.telegram_rpc as telegram_rpc_module

    monkeypatch.setattr(telegram_rpc_module, "GetFullChannelRequest", _TestRequest)
    gate = _gate(retry_delays=(0,))
    observed: list[dict[str, object]] = []
    gate.set_rpc_request_observer(lambda **values: observed.append(values))
    send_attempts = 0

    def send(_request: object) -> object:
        nonlocal send_attempts
        send_attempts += 1
        if send_attempts == 1:
            raise ServerError(None, "temporary")
        return "ok"

    _set_sender(gate, send)

    assert await _call(gate, _TestRequest("private channel identity")) == "ok"

    assert send_attempts == 2
    assert len(observed) == 2
    assert all(item["request_class"] == "get_full_channel" for item in observed)
    assert all(item["source"] is TelegramRpcSource.MCP_INTERACTIVE for item in observed)
    assert all("private channel identity" not in repr(item) for item in observed)


@pytest.mark.asyncio
async def test_sender_proxy_rejects_unexpected_future_batch_and_releases_slot() -> None:
    gate = _gate()
    returned_future: asyncio.Future[object] | None = None

    class _BatchSender:
        def send(self, _request: object, *, ordered: bool = False) -> list[asyncio.Future[object]]:
            nonlocal returned_future
            del ordered
            returned_future = asyncio.get_running_loop().create_future()
            return [returned_future]

    gate._main_sender = gate._sender = _BatchSender()
    with pytest.raises(RuntimeError, match="future batch for a scalar request"):
        await _call(gate, "request")

    assert returned_future is not None and returned_future.cancelled()
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)


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
async def test_gate_rechecks_cooldown_after_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_account_cooldown()
    gate = _gate()
    sleeps: list[float] = []

    acquisitions = 0

    async def acquire_and_open() -> None:
        nonlocal acquisitions
        acquisitions += 1
        gate._limiter.acquisitions += 1
        if acquisitions == 1:
            import mcp_telegram.telegram_rpc as rpc

            rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 3

    gate._limiter.acquire = acquire_and_open

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        import mcp_telegram.telegram_rpc as rpc

        rpc._COOLDOWN_DEADLINE = 0

    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", fake_sleep)
    assert await _call(gate, "request") == "request"
    assert len(sleeps) == 1
    assert gate._limiter.acquisitions == 2


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
    gate._admission_scheduler.set_observer(events.append)
    monkeypatch.setattr(gate._admission_scheduler, "admit", admit_then_change_readiness)

    assert await _call(gate, "request") == "request"

    assert gate._limiter.acquisitions == 2
    assert [event.kind for event in events].count(RpcAdmissionEventKind.DISPATCHED) == 1
    assert [event.reason for event in events if event.kind is RpcAdmissionEventKind.RESUBMITTED] == [
        "transport_readiness_changed"
    ]
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)


@pytest.mark.asyncio
async def test_gate_flood_is_immediate_cooldown_and_observed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_account_cooldown()
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

    gate = _gate()
    clock = iter((100.0, 101.0))
    monkeypatch.setattr(rpc.time, "monotonic", lambda: next(clock, 101.0))

    await gate._observe_flood(FloodWaitError(request=None, capture=7))
    first_deadline = account_cooldown_deadline()
    await gate._observe_flood(FloodWaitError(request=None, capture=2))

    assert first_deadline == 108.0
    assert account_cooldown_deadline() == first_deadline


@pytest.mark.asyncio
async def test_gate_restores_persisted_cooldown_and_blocks_transport_until_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monotonic_now = [200.0]
    loaded = 0
    monkeypatch.setattr("mcp_telegram.telegram_rpc.time.monotonic", lambda: monotonic_now[0])
    monkeypatch.setattr("mcp_telegram.telegram_rpc.time.time", lambda: 1_000.0)

    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    seed_conn = sqlite3.connect(str(db_path))
    save_account_cooldown_until_utc(seed_conn, 1_005.0)
    seed_conn.close()
    reopened_conn = sqlite3.connect(str(db_path))

    def load_until_utc() -> float | None:
        nonlocal loaded
        loaded += 1
        return load_account_cooldown_until_utc(reopened_conn)

    gate = TelegramRpcGate(
        StringSession(),
        1,
        "hash",
        rpc_budget=TelegramRpcBudget(max_calls_per_period=0, period_seconds=60),
        circuit_status=lambda: _CircuitStatus(open=False),
        fallback_wait_seconds=60,
        cooldown_buffer_seconds=1.0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        cooldown_persistence=TelegramRpcCooldownPersistence(
            load_until_utc,
            lambda deadline: save_account_cooldown_until_utc(reopened_conn, deadline),
        ),
    )
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_cooldown(_delay: float) -> None:
        waiting.set()
        await release.wait()

    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", wait_for_cooldown)

    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=gate._scheduler_policy,
        limiter=gate._limiter,
        clock=lambda: monotonic_now[0],
        readiness=RpcTransportReadiness(probe=gate._scheduler_transport_ready),
    )
    sender = _set_sender(gate, _request_value)
    try:
        caller = asyncio.create_task(_call(gate, "request"))
        await waiting.wait()

        assert loaded == 1
        assert account_cooldown_deadline() == 205.0
        assert sender.calls == 0
        assert gate._limiter is None
        assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)
        assert load_account_cooldown_until_utc(reopened_conn) == 1_005.0

        monotonic_now[0] = 206.0
        release.set()
        assert await caller == "request"
        assert sender.calls == 1
    finally:
        await gate.close_rpc_scheduler()
        gate.session.close()
        reopened_conn.close()


@pytest.mark.asyncio
async def test_finite_flood_wait_persists_effective_max_utc_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_now = [100.0]
    utc_now = [1_000.0]
    saved: list[float] = []
    gate = _gate()
    gate._cooldown_persistence = TelegramRpcCooldownPersistence(lambda: None, saved.append)
    monkeypatch.setattr("mcp_telegram.telegram_rpc.time.monotonic", lambda: monotonic_now[0])
    monkeypatch.setattr("mcp_telegram.telegram_rpc.time.time", lambda: utc_now[0])

    await gate._observe_flood(FloodWaitError(request=None, capture=7))
    monotonic_now[0] = 101.0
    utc_now[0] = 1_001.0
    await gate._observe_flood(FloodWaitError(request=None, capture=2))
    monotonic_now[0] = 102.0
    utc_now[0] = 1_002.0
    await gate._observe_flood(FloodWaitError(request=None, capture=10))

    assert account_cooldown_deadline() == 113.0
    assert saved == [1_008.0, 1_008.0, 1_013.0]


@pytest.mark.asyncio
async def test_latched_circuit_never_persists_a_cooldown() -> None:
    saved: list[float] = []
    gate = _gate(_CircuitStatus(open=True))
    gate._cooldown_persistence = TelegramRpcCooldownPersistence(lambda: None, saved.append)
    sender = _set_sender(gate, _request_value)

    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, "request")

    assert caught.value.latched
    assert sender.calls == 0
    assert account_cooldown_deadline() == 0.0
    assert saved == []


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
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(gate(_TestRequest("difference")), timeout=0.05)
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
    finally:
        if delayed is not None and not delayed.done():
            release.set()
            delayed.cancel()
            await asyncio.gather(delayed, return_exceptions=True)
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_nested_resolution_does_not_turn_four_vendor_cache_deferrals_into_warnings() -> None:
    class _CachedOuterRequest(TLRequest):
        CONSTRUCTOR_ID = 0x34567890

        def __init__(self, resolve_nested: bool) -> None:
            self._resolve_nested = resolve_nested

        async def resolve(self, client: TelegramClient, utils: object) -> None:
            del utils
            if self._resolve_nested:
                await client(_TestRequest("nested"))

    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate = _gate()
    gate._cooldown_buffer_seconds = 0
    gate._rpc_circuit_status = accumulator.kill_switch_status
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    flood_events: list[str] = []
    gate._flood_event_observer = lambda observation: flood_events.append(observation.origin)
    sent: list[object] = []

    def send(request: object) -> object:
        sent.append(request)
        if isinstance(request, _CachedOuterRequest):
            raise FloodWaitError(request=None, capture=20)
        return _request_value(request)

    sender = _set_sender(gate, send)
    try:
        with pytest.raises(TelegramRpcThrottled):
            await _call(gate, _CachedOuterRequest(resolve_nested=False))
        import mcp_telegram.telegram_rpc as rpc

        for _ in range(4):
            rpc._COOLDOWN_DEADLINE = 0
            with pytest.raises(TelegramRpcThrottled):
                await _call(gate, _CachedOuterRequest(resolve_nested=True))

        assert sender.calls == 5
        assert [type(request) for request in sent] == [
            _CachedOuterRequest,
            _TestRequest,
            _TestRequest,
            _TestRequest,
            _TestRequest,
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
    gate = _gate()
    gate._rpc_circuit_status = accumulator.kill_switch_status
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    gate._cooldown_buffer_seconds = 0
    sender_calls = 0

    for index in range(5):

        def send(_request: object, *, _index: int = index) -> object:
            nonlocal sender_calls
            sender_calls += 1
            raise FloodWaitError(request=None, capture=1 + (_index % 2))

        _set_sender(gate, send)
        with pytest.raises(TelegramRpcThrottled) as caught:
            await _call(gate, f"request-{index}")
        assert caught.value.latched is False
        if index < 4:
            import mcp_telegram.telegram_rpc as rpc

            rpc._COOLDOWN_DEADLINE = 0
            gate._flood_waited_requests.clear()

    assert sender_calls == 5
    assert accumulator.kill_switch_status().open is True
    with pytest.raises(TelegramRpcThrottled) as caught:
        await _call(gate, "blocked")
    assert caught.value.latched is True
    assert sender_calls == 5
    assert gate._limiter.acquisitions == 5


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
    )
    assert isinstance(gate, TelegramClient)
    assert gate._request_retries == 0
    assert gate._cooldown_persistence is None
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
    gate._main_sender = sender
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
    await _wait_for(lambda: not gate._pending_scalar_dispatches)
    assert account_cooldown_deadline() > asyncio.get_running_loop().time()
    assert accumulator.kill_switch_status().events_in_window == 1
    assert len(observations) == 1
    observation = observations[0]
    assert observation.origin == "actual_send"
    assert observation.actual_dispatch is True
    assert observation.request_method == "_TestRequest"
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
    manager = EventHandlerManager(_DmEventHandlerGateClient(gate), conn, asyncio.Event())
    return _RecordingDmPageScanner(manager)


def _real_dm_page_adapter(
    conn: sqlite3.Connection,
) -> tuple[TelegramRpcGate, _DmPageFutureSender, DmDeletionReconciliationDemandAdapter, _RecordingDmPageScanner]:
    gate = _gate()
    sender = _DmPageFutureSender()
    gate._main_sender = sender
    gate.session = SimpleNamespace(
        process_entities=lambda _result: None,
        get_input_entity=lambda entity: types.InputPeerUser(int(entity), 0),
    )
    scanner = _real_dm_page_scanner(gate, conn)
    prepare_dm_deletion_reconciliation(conn, now=100)
    return gate, sender, DmDeletionReconciliationDemandAdapter(conn, scanner), scanner


async def _dispatch_forward_history_sibling(
    gate: TelegramRpcGate,
    sender: _DmPageFutureSender,
    conn: sqlite3.Connection,
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
            assert after_message_id == 1
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

    forward_adapter = DeltaGapFillDemandAdapter(DeltaSyncWorker(GateForwardPort(), conn, asyncio.Event()))
    sibling = asyncio.create_task(forward_adapter.run_slice(RpcAttemptBudget(limit=1)))
    await _wait_for(lambda: sender.calls == 2)
    assert isinstance(sender.requests[1], functions.messages.GetHistoryRequest)
    assert sender.scopes[1].demand_kind is DemandKind.DELTA_GAP_FILL
    assert sender.scopes[1].acquisition_kind is AcquisitionKind.MESSAGE_HISTORY_PAGE
    sender.futures[1].set_result(types.messages.Messages(messages=[], topics=[], chats=[], users=[]))
    await sibling


@pytest.mark.asyncio
async def test_dm_page_success_uses_real_get_messages_and_commits_tombstone_cursor() -> None:
    conn = _dm_reconciliation_test_db()
    gate, sender, adapter, scanner = _real_dm_page_adapter(conn)
    try:
        budget = RpcAttemptBudget(limit=1)
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

        budget = RpcAttemptBudget(limit=1)
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
        reconstructed = DmDeletionReconciliationDemandAdapter(conn, reconstructed_scanner)
        assert reconstructed.status(10_000_000) is None
        await reconstructed.run_slice(RpcAttemptBudget(limit=1))
        assert reconstructed_scanner.calls == 0
        assert sender.calls == 1
    finally:
        await gate.close_rpc_scheduler()
        conn.close()


@pytest.mark.asyncio
async def test_dm_page_flood_crosses_gate_with_durable_demand_and_acquisition() -> None:
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

        task = asyncio.create_task(adapter.run_slice(RpcAttemptBudget(limit=1)))
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

        rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() - 1
        await _dispatch_forward_history_sibling(gate, sender, conn)

        await adapter.run_slice(RpcAttemptBudget(limit=1))
        assert scanner.calls == 1
        assert sender.calls == 2
        assert len(observations) == 1
    finally:
        await gate.close_rpc_scheduler()
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

        task = asyncio.create_task(adapter.run_slice(RpcAttemptBudget(limit=1)))
        await _wait_for(lambda: sender.calls == 1)
        assert isinstance(sender.requests[0], functions.messages.GetMessagesRequest)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _dm_reconciliation_state_for_rpc_test(conn)["status"] == "verifying"
        assert not sender.futures[0].cancelled()

        sender.futures[0].set_exception(FloodWaitError(request=None, capture=20))
        await _wait_for(lambda: not gate._pending_scalar_dispatches)
        assert len(observations) == 1
        assert observations[0].actual_dispatch is True
        assert observations[0].demand_kind == DemandKind.DM_DELETION_RECONCILIATION.value
        assert observations[0].acquisition_kind == AcquisitionKind.MESSAGE_LOOKUP.value
        assert accumulator.kill_switch_status().events_in_window == 1
        restarted_scanner = _real_dm_page_scanner(gate, conn)
        restarted = DmDeletionReconciliationDemandAdapter(conn, restarted_scanner)
        assert restarted.status(10_000_000) is None
        prepare_dm_deletion_reconciliation(conn, now=200)
        assert _dm_reconciliation_state_for_rpc_test(conn)["status"] == "suspended"
        assert _dm_reconciliation_state_for_rpc_test(conn)["reason"] == "interrupted"
        await restarted.run_slice(RpcAttemptBudget(limit=1))
        assert scanner.calls == 1
        assert restarted_scanner.calls == 0
        assert sender.calls == 1
    finally:
        await gate.close_rpc_scheduler()
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
    adapter = DmDeletionReconciliationDemandAdapter(conn, Scanner())
    budget = RpcAttemptBudget(limit=1)
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
        readiness=RpcTransportReadiness(
            probe=gate._scheduler_transport_ready,
            wait=gate._wait_for_scheduler_transport,
        ),
    )
    sender = _RawFutureSender()
    gate._main_sender = sender
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    caller = asyncio.create_task(_call(gate, _TestRequest("actual")))
    await _wait_for(lambda: sender.calls == 1)
    raw_future = sender.futures[0]
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not raw_future.cancelled()
    assert set(gate._pending_scalar_dispatches) == {raw_future}
    assert gate._admission_scheduler.active_depths()[RpcServiceClass.INTERACTIVE] == 1
    with pytest.raises(TelegramRpcAdmissionDeferred):
        await _call(gate, _TestRequest("blocked"))
    raw_future.set_exception(FloodWaitError(request=None, capture=20))
    await _wait_for(lambda: not gate._pending_scalar_dispatches)
    assert accumulator.kill_switch_status().events_in_window == 1
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
    gate._main_sender = sender
    gate._rpc_circuit_status = accumulator.kill_switch_status
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    import mcp_telegram.telegram_rpc as rpc

    for index in range(5):
        caller = asyncio.create_task(_call(gate, _TestRequest(index)))
        expected_calls = index + 1
        await _wait_for(lambda expected_calls=expected_calls: sender.calls == expected_calls)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        sender.futures[index].set_exception(FloodWaitError(request=None, capture=1))
        await _wait_for(lambda: not gate._pending_scalar_dispatches)
        if index < 4:
            rpc._COOLDOWN_DEADLINE = 0
            gate._flood_waited_requests.clear()

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
    gate._main_sender = sender
    assert await _call(gate, _TestRequest("success")) == "ok"
    with pytest.raises(TelegramRpcThrottled):
        await _call(gate, _TestRequest("flood"))
    assert sender.calls == 2
    assert accumulator.kill_switch_status().events_in_window == 1
    assert gate._pending_scalar_dispatches == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_actual_future_flood_protects_when_persistence_and_event_sinks_fail() -> None:
    gate = _gate()
    sender = _RawFutureSender()
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate._main_sender = sender
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    gate._cooldown_persistence = TelegramRpcCooldownPersistence(
        lambda: None, lambda _deadline: (_ for _ in ()).throw(OSError())
    )
    gate.set_flood_event_observer(lambda _event: (_ for _ in ()).throw(OSError()))
    caller = asyncio.create_task(_call(gate, _TestRequest("actual")))
    await _wait_for(lambda: sender.calls == 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    sender.futures[0].set_exception(FloodWaitError(request=None, capture=20))
    await _wait_for(lambda: not gate._pending_scalar_dispatches)
    assert account_cooldown_deadline() > asyncio.get_running_loop().time()
    assert accumulator.kill_switch_status().events_in_window == 1
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_disconnect_drains_completed_actual_flood_after_caller_cancellation() -> None:
    gate = _gate()
    sender = _RawFutureSender()
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate._main_sender = sender
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
    assert gate._pending_scalar_dispatches == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_failed_raw_disconnect_retains_response_lease_and_blocks_transport_reuse() -> None:
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
    gate._main_sender = sender
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
    assert set(gate._pending_scalar_dispatches) == {raw_future}
    assert gate._admission_scheduler.active_depths()[RpcServiceClass.INTERACTIVE] == 1
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await _call(gate, _TestRequest("blocked"))
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate.connect()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._switch_dc(3)
    _MainSenderAdapter(gate)._keepalive_ping(1)
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._disconnect_main_sender()
    assert sender.disconnect_calls == 1
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor_failure", ["cancelled_future", "local_exception"])
async def test_installed_vendor_sender_failure_retains_gate_response_lease(vendor_failure: str) -> None:
    gate = _gate()
    raw_future = asyncio.get_running_loop().create_future()

    class _FailingConnection:
        def __init__(self) -> None:
            self.disconnect_calls = 0
            self._connected = True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            raise OSError("fake transport teardown failed")

    connection = _FailingConnection()
    sender = _installed_sender(connection)
    sender._pending_state[101] = SimpleNamespace(future=raw_future)
    forwarded_pings: list[int] = []
    sender._keepalive_ping = forwarded_pings.append
    gate._main_sender = sender
    gate._sender = _MainSenderAdapter(gate)

    with demand_context(DemandKind.FULL_SYNC_PAGE):
        with rpc_scope(TelegramRpcSource.FULL_SYNC):
            scope = current_rpc_scope()
            admission = await gate._admission_scheduler.admit(scope)
    gate._register_scalar_dispatch_completion(
        raw_future,
        scope=scope,
        request_method="GetHistoryRequest",
        admission=admission,
        dispatch_at_monotonic=asyncio.get_running_loop().time(),
    )

    if vendor_failure == "cancelled_future":
        with pytest.raises(OSError, match="fake transport teardown failed"):
            await gate._disconnect_main_sender()
        assert raw_future.cancelled()
    else:
        with pytest.raises(OSError, match="fake transport teardown failed"):
            await sender._disconnect(error=OSError("local sender failure"))
        with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
            await gate._disconnect_main_sender()
        assert raw_future.done() and not raw_future.cancelled()
        assert set(gate._pending_scalar_dispatches) == {raw_future}
        assert sum(gate._admission_scheduler.active_depths().values()) == 1

    await asyncio.sleep(0)
    assert connection.disconnect_calls == 1
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert set(gate._pending_scalar_dispatches) == {raw_future}
    assert sum(gate._admission_scheduler.active_depths().values()) == 1
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await _call(gate, _TestRequest("blocked"))
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate.connect()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._switch_dc(3)
    _MainSenderAdapter(gate)._keepalive_ping(1)
    assert forwarded_pings == []
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._disconnect_main_sender()
    assert connection.disconnect_calls == 1
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_installed_vendor_internal_failure_with_zero_pending_cannot_confirm_teardown() -> None:
    gate = _gate()

    class _FailingConnection:
        def __init__(self) -> None:
            self.disconnect_calls = 0
            self._connected = True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            raise OSError("internal transport teardown failed")

    connection = _FailingConnection()
    sender = _installed_sender(connection)
    forwarded_pings: list[int] = []
    sender._keepalive_ping = forwarded_pings.append
    gate._main_sender = sender
    gate._sender = _MainSenderAdapter(gate)

    with pytest.raises(OSError, match="internal transport teardown failed"):
        await sender._disconnect(error=OSError("internal sender failure"))
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._disconnect_main_sender()

    assert connection.disconnect_calls == 1
    assert sender._connection is None
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert gate._pending_scalar_dispatches == {}
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await _call(gate, _TestRequest("blocked"))
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate.connect()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._switch_dc(3)
    _MainSenderAdapter(gate)._keepalive_ping(1)
    assert forwarded_pings == []
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_installed_vendor_internal_teardown_in_progress_cannot_be_taken_over() -> None:
    gate = _gate()
    disconnect_started = asyncio.Event()
    disconnect_release = asyncio.Event()

    class _BlockedConnection:
        def __init__(self) -> None:
            self.disconnect_calls = 0
            self._connected = True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            disconnect_started.set()
            await disconnect_release.wait()
            raise OSError("internal transport teardown failed")

    connection = _BlockedConnection()
    sender = _installed_sender(connection)
    gate._main_sender = sender
    gate._sender = _MainSenderAdapter(gate)

    internal = asyncio.create_task(sender._disconnect(error=OSError("internal sender failure")))
    await disconnect_started.wait()
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._disconnect_main_sender()
    assert connection.disconnect_calls == 1
    assert gate._transport_state is _TransportBoundaryState.FAILED

    disconnect_release.set()
    with pytest.raises(OSError, match="internal transport teardown failed"):
        await internal
    assert sender._connection is None
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "raise_user_probe", "raise_transport_probe"),
    [
        (_TransportBoundaryState.READY, False, False),
        (_TransportBoundaryState.CONNECTING, False, False),
        (_TransportBoundaryState.READY, True, False),
        (_TransportBoundaryState.CONNECTING, False, True),
    ],
)
async def test_teardown_preflight_rejects_missing_or_unreadable_raw_transport(
    state: _TransportBoundaryState,
    raise_user_probe: bool,
    raise_transport_probe: bool,
) -> None:
    gate = _gate()
    raw_future = asyncio.get_running_loop().create_future()
    owner = object()

    class _ProbeSender:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        def is_connected(self) -> bool:
            if raise_user_probe:
                raise RuntimeError("user-connected probe failed")
            return True

        def _transport_connected(self) -> bool:
            if raise_transport_probe:
                raise RuntimeError("transport-connected probe failed")
            return False

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    sender = _ProbeSender()
    gate._main_sender = sender
    gate._connect_owner = owner
    with demand_context(DemandKind.FULL_SYNC_PAGE):
        with rpc_scope(TelegramRpcSource.FULL_SYNC):
            scope = current_rpc_scope()
            admission = await gate._admission_scheduler.admit(scope)
    gate._register_scalar_dispatch_completion(
        raw_future,
        scope=scope,
        request_method="GetHistoryRequest",
        admission=admission,
        dispatch_at_monotonic=asyncio.get_running_loop().time(),
    )
    gate._transport_state = state

    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is failed"):
        await gate._disconnect_main_sender()
    assert sender.disconnect_calls == 0
    assert gate._transport_state is _TransportBoundaryState.FAILED
    assert gate._connect_owner is owner
    assert set(gate._pending_scalar_dispatches) == {raw_future}
    assert sum(gate._admission_scheduler.active_depths().values()) == 1
    with pytest.raises(TelegramRpcAdmissionDeferred):
        await _call(gate, _TestRequest("blocked"))
    with pytest.raises(TelegramRpcAdmissionDeferred):
        await gate.connect()

    raw_future.set_result("terminal cleanup")
    await _wait_for(lambda: gate._pending_scalar_dispatches == {})
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_teardown_fails", [False, True])
async def test_overlapping_installed_sender_disconnect_cannot_confirm_first_teardown(
    first_teardown_fails: bool,
) -> None:
    """Only the transport owner may establish disconnect completion."""
    gate = _gate()
    raw_future = asyncio.get_running_loop().create_future()
    disconnect_started = asyncio.Event()
    disconnect_release = asyncio.Event()

    class _BlockedFailingConnection:
        def __init__(self) -> None:
            self.disconnect_calls = 0
            self._connected = True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            disconnect_started.set()
            await disconnect_release.wait()
            if first_teardown_fails:
                raise OSError("first teardown failed")

    connection = _BlockedFailingConnection()
    sender = _installed_sender(connection)
    sender._pending_state[101] = SimpleNamespace(future=raw_future)
    gate._main_sender = sender
    gate._sender = _MainSenderAdapter(gate)

    with demand_context(DemandKind.FULL_SYNC_PAGE):
        with rpc_scope(TelegramRpcSource.FULL_SYNC):
            scope = current_rpc_scope()
            admission = await gate._admission_scheduler.admit(scope)
    gate._register_scalar_dispatch_completion(
        raw_future,
        scope=scope,
        request_method="GetHistoryRequest",
        admission=admission,
        dispatch_at_monotonic=asyncio.get_running_loop().time(),
    )

    first = asyncio.create_task(gate._disconnect_main_sender())
    await disconnect_started.wait()
    assert gate._transport_state is _TransportBoundaryState.DISCONNECTING
    with pytest.raises(TelegramRpcAdmissionDeferred, match="transport is disconnecting"):
        await gate._disconnect_main_sender()
    assert connection.disconnect_calls == 1
    assert set(gate._pending_scalar_dispatches) == {raw_future}

    disconnect_release.set()
    if first_teardown_fails:
        with pytest.raises(OSError, match="first teardown failed"):
            await first
        assert gate._transport_state is _TransportBoundaryState.FAILED
        assert set(gate._pending_scalar_dispatches) == {raw_future}
        assert sum(gate._admission_scheduler.active_depths().values()) == 1
    else:
        await first
        assert gate._transport_state is _TransportBoundaryState.DISCONNECTED
        assert gate._pending_scalar_dispatches == {}
        assert sum(gate._admission_scheduler.active_depths().values()) == 0
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["local_error", "cancelled", "result", "flood_wait"])
async def test_confirmed_installed_sender_outcomes_release_once_after_owned_teardown(outcome: str) -> None:
    """Only a successful owned teardown may consume residual scalar outcomes."""
    gate = _gate()
    raw_future = asyncio.get_running_loop().create_future()
    flood_error = FloodWaitError(request=None, capture=20)
    accumulator = FloodWaitAccumulator()
    accumulator.configure_kill_switch(
        FloodWaitKillSwitchPolicy(enabled=True, window_seconds=600, max_events=5, max_wait_seconds=900)
    )
    gate._flood_observer = lambda **event: accumulator.observe(**event)
    observed_floods: list[FloodWaitObservation] = []
    gate.set_flood_event_observer(observed_floods.append)

    class _SuccessfulConnection:
        def __init__(self) -> None:
            self.disconnect_calls = 0
            self._connected = True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            if outcome == "result":
                raw_future.set_result("response")
            elif outcome == "flood_wait":
                raw_future.set_exception(flood_error)

    connection = _SuccessfulConnection()
    sender = _installed_sender(connection)
    sender._pending_state[101] = SimpleNamespace(future=raw_future)

    if outcome == "local_error":

        async def disconnect_with_local_error() -> None:
            await sender._disconnect(error=OSError("local sender failure"))

        sender.disconnect = disconnect_with_local_error

    gate._main_sender = sender
    gate._sender = _MainSenderAdapter(gate)
    with demand_context(DemandKind.FULL_SYNC_PAGE):
        with rpc_scope(TelegramRpcSource.FULL_SYNC):
            scope = current_rpc_scope()
            admission = await gate._admission_scheduler.admit(scope)
    gate._register_scalar_dispatch_completion(
        raw_future,
        scope=scope,
        request_method="GetHistoryRequest",
        admission=admission,
        dispatch_at_monotonic=asyncio.get_running_loop().time(),
    )
    pending = gate._pending_scalar_dispatches[raw_future]
    assert pending.scope is scope
    assert pending.request_method == "GetHistoryRequest"
    assert pending.admission is admission

    await gate._disconnect_main_sender()
    assert connection.disconnect_calls == 1
    _assert_confirmed_scalar_outcome_consumed(gate, raw_future, accumulator, outcome)
    if outcome == "flood_wait":
        _assert_frozen_flood_observation(
            observed_floods,
            pending.request_method,
            pending.admission.sequence,
            pending.dispatch_at_monotonic,
        )

    reconnect_sender = _BootstrapSender()
    gate._main_sender = reconnect_sender
    gate._sender = _MainSenderAdapter(gate)
    await gate._sender.connect(object())
    assert gate._transport_state is _TransportBoundaryState.READY
    assert reconnect_sender.connect_calls == 1
    await gate._sender.disconnect()
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_installed_vendor_auth_init_disconnect_failure_parks_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auth-init failure cannot turn vendor reference clearing into recovery."""
    from telethon.errors import SecurityError
    from telethon.network import authenticator

    gate, _ = _bootstrap_gate()

    class _AuthFailureConnection:
        def __init__(self) -> None:
            self.connect_calls = 0
            self.disconnect_calls = 0

        async def connect(self, *, timeout: object) -> None:
            del timeout
            self.connect_calls += 1

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            raise OSError("internal transport teardown failed")

    connection = _AuthFailureConnection()
    sender = _installed_sender(connection, retries=1, connected=False)
    gate._main_sender = sender
    gate._sender = _MainSenderAdapter(gate)
    gate._connection = lambda *_args, **_kwargs: connection
    forwarded_pings: list[int] = []
    sender._keepalive_ping = forwarded_pings.append

    async def fail_authentication(_plain: object) -> tuple[bytes, int]:
        raise SecurityError("authentication failed")

    monkeypatch.setattr(authenticator, "do_authentication", fail_authentication)
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
    context = cast(
        _SyncMainContext,
        SimpleNamespace(
            client=gate,
            api_server=SimpleNamespace(startup_detail="", _ready=False),
            shutdown_event=shutdown,
        ),
    )
    parked = asyncio.create_task(_connect_telegram(context))
    await _wait_for(lambda: bool(observed_errors))

    assert sender._connection is None
    assert connection.connect_calls == 1
    assert connection.disconnect_calls == 1
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None
    assert isinstance(observed_errors[0].__cause__, OSError)
    assert context.api_server._ready is False
    assert not parked.done()
    await _assert_failed_gate_blocks_api_migration_and_ping(gate, forwarded_pings)
    with pytest.raises(TelegramRpcAdmissionDeferred, match="connection bootstrap is already running"):
        await gate.connect()
    assert connection.connect_calls == 1
    assert connection.disconnect_calls == 1

    shutdown.set()
    assert await parked is False
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_installed_vendor_connect_cancellation_retains_failed_bootstrap_owner() -> None:
    gate, _ = _bootstrap_gate()
    connect_started = asyncio.Event()

    class _BlockingConnection:
        async def connect(self, *, timeout: object) -> None:
            del timeout
            connect_started.set()
            await asyncio.Future()

        async def disconnect(self) -> None:
            raise AssertionError("failed initialization must not manufacture confirmation")

    connection = _BlockingConnection()
    sender = _installed_sender(connection, retries=1, connected=False)
    gate._main_sender = sender
    gate._sender = _MainSenderAdapter(gate)
    gate._connection = lambda *_args, **_kwargs: connection
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
    await gate.close_rpc_scheduler()


def test_transport_connect_failure_only_changes_connecting_state() -> None:
    gate = _gate()
    for state in (
        _TransportBoundaryState.FAILED,
        _TransportBoundaryState.READY,
        _TransportBoundaryState.DISCONNECTING,
        _TransportBoundaryState.DISCONNECTED,
    ):
        gate._transport_state = state
        gate._finish_transport_connect_failure()
        assert gate._transport_state is state

    gate._transport_state = _TransportBoundaryState.CONNECTING
    gate._finish_transport_connect_failure()
    assert gate._transport_state is _TransportBoundaryState.FAILED


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
    api_server = SimpleNamespace(startup_detail="", _ready=False)
    daemon_context = cast(
        _SyncMainContext,
        SimpleNamespace(client=gate, api_server=api_server, shutdown_event=shutdown),
    )
    parked = asyncio.create_task(_connect_telegram(daemon_context))
    await _wait_for(lambda: bool(observed_errors))
    assert isinstance(observed_errors[0].__cause__, TelegramRpcThrottled)
    assert "termination is unconfirmed" in api_server.startup_detail
    assert api_server._ready is False
    assert not parked.done()
    assert sender.connect_calls == 1
    assert sender.disconnect_calls == 1
    assert gate._connect_owner is not None
    assert gate._connection_capability is not None

    shutdown.set()
    assert await parked is False
    assert sender.connect_calls == 1
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
    assert gate._pending_scalar_dispatches == {}


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

    gate._main_sender = gate._sender = _PendingSender()
    caller = asyncio.create_task(_call(gate, "request"))
    await transport_started.wait()

    await gate.close_rpc_scheduler()

    assert caller.cancelled()
    assert in_flight is not None and not in_flight.cancelled()
    assert set(gate._pending_scalar_dispatches) == {in_flight}
    await gate._disconnect_main_sender()
    assert in_flight.cancelled()
    assert gate._pending_scalar_dispatches == {}
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)


@pytest.mark.asyncio
async def test_recursive_telethon_resolution_reenters_admission_without_deadlock() -> None:
    gate = _gate()
    sent: list[object] = []
    events: list[RpcAdmissionEvent] = []
    gate._scheduler_policy = TelegramRpcSchedulerConfig(interactive_queue_capacity=1)
    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=gate._scheduler_policy,
        limiter=gate._limiter,
        observer=events.append,
        readiness=RpcTransportReadiness(
            probe=gate._scheduler_transport_ready,
            wait=gate._wait_for_scheduler_transport,
        ),
    )

    class _RecursiveRequest(TLRequest):
        CONSTRUCTOR_ID = 123

        async def resolve(self, client: TelegramClient, utils: object) -> None:
            del utils
            await client(functions.PingRequest(99))

    class _Sender:
        def send(self, request: object, *, ordered: bool = False) -> object:
            del ordered
            sent.append(request)

            async def complete() -> object:
                return request

            return complete()

    gate._main_sender = gate._sender = _Sender()
    gate._loop = None
    gate._request_retries = 0
    gate._raise_last_call_error = True
    gate._flood_waited_requests = {}
    gate._no_updates = False
    gate._log = {"telethon.client.users": logging.getLogger(__name__)}
    gate.flood_sleep_threshold = 0
    gate.session = SimpleNamespace(process_entities=lambda _result: None)

    with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE):
        result = await asyncio.wait_for(gate(_RecursiveRequest()), timeout=1.0)

    assert isinstance(result, _RecursiveRequest)
    assert [type(request) for request in sent] == [functions.PingRequest, _RecursiveRequest]
    assert gate._limiter.acquisitions == 2
    dispatched = [event for event in events if event.kind is RpcAdmissionEventKind.DISPATCHED]
    assert len(dispatched) == 2
    assert all(event.active_depth == 1 and event.total_outstanding == 1 for event in dispatched)
    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)


@pytest.mark.asyncio
async def test_update_difference_flood_wait_retries_inside_supported_gate_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_telegram.telegram_rpc as rpc

    gate = _gate()
    attempts = 0
    sleeps: list[float] = []
    utc_now = [rpc.time.time()]

    def send(request: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FloodWaitError(request=None, capture=7)
        return _request_value(request)

    async def finish_cooldown(delay: float) -> None:
        sleeps.append(delay)
        utc_now[0] += delay
        rpc._COOLDOWN_DEADLINE = 0

    _set_sender(gate, send)
    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", finish_cooldown)
    monkeypatch.setattr(rpc.time, "time", lambda: utc_now[0])
    with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
        result = await gate(_TestRequest("difference"))

    assert result == "difference"
    assert attempts == 2
    assert gate._limiter.acquisitions == 2
    assert len(sleeps) == 1


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
    with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
        result = await gate(_TestRequest("difference"))

    assert result == "difference"
    assert sleeps == [gate._scheduler_policy.update_loop_retry_seconds]
    assert gate._limiter.acquisitions == 1


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
