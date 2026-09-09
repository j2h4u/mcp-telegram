# pyright: reportAny=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
    ServerError,
    SlowModeWaitError,
)
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
from mcp_telegram.flood import FloodWaitAccumulator, FloodWaitKillSwitchPolicy, TelegramRpcThrottled
from mcp_telegram.sync_db import ensure_sync_schema, load_account_cooldown_until_utc, save_account_cooldown_until_utc
from mcp_telegram.telegram import create_client
from mcp_telegram.telegram_rpc import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcBudget,
    TelegramRpcCooldownPersistence,
    TelegramRpcGate,
    TelegramRpcSource,
    UnclassifiedTelegramRpcError,
    account_cooldown_deadline,
    current_rpc_scope,
    reset_account_cooldown,
    rpc_scope,
)
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmission,
    RpcAdmissionClosedError,
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    RpcAdmissionSaturatedError,
    RpcServiceClass,
    RpcTransportReadiness,
    TelegramRpcAdmissionScheduler,
    TelegramRpcScope,
)


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


def _request_value(request: object) -> object:
    return request.value if isinstance(request, _TestRequest) else request


def _set_sender(
    gate: TelegramRpcGate,
    callback: Callable[[object], object | Awaitable[object]],
) -> _Sender:
    sender = _Sender(callback)
    gate._sender = sender
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
    gate._log = {"telethon.client.users": logging.getLogger(__name__)}
    gate.flood_sleep_threshold = 0
    gate.session = SimpleNamespace(process_entities=lambda _result: None)
    return gate


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

    gate._sender = _Sender()
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
async def test_sender_proxy_rejects_unexpected_future_batch_and_releases_slot() -> None:
    gate = _gate()
    returned_future: asyncio.Future[object] | None = None

    class _BatchSender:
        def send(self, _request: object, *, ordered: bool = False) -> list[asyncio.Future[object]]:
            nonlocal returned_future
            del ordered
            returned_future = asyncio.get_running_loop().create_future()
            return [returned_future]

    gate._sender = _BatchSender()
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

    async def wait_for_readiness() -> None:
        waiting.set()
        await release.wait()

    gate._admission_scheduler = TelegramRpcAdmissionScheduler(
        policy=gate._scheduler_policy,
        limiter=gate._limiter,
        clock=lambda: monotonic_now[0],
        readiness=RpcTransportReadiness(
            probe=gate._scheduler_transport_ready,
            wait=wait_for_readiness,
        ),
    )
    sender = _set_sender(gate, _request_value)
    try:
        caller = asyncio.create_task(_call(gate, "request"))
        await waiting.wait()

        assert loaded == 1
        assert account_cooldown_deadline() == 205.0
        assert sender.calls == 0
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
async def test_gate_cooldown_wait_is_cancellation_safe() -> None:
    gate = _gate()
    import mcp_telegram.telegram_rpc as rpc

    rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 30
    caller = asyncio.create_task(_call(gate, "request"))
    await _wait_for(lambda: gate._admission_scheduler.queue_depths()[RpcServiceClass.INTERACTIVE] == 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)
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

    async def send(request: object) -> object:
        await asyncio.sleep(0)
        return _request_value(request)

    monkeypatch.setattr(gate._admission_scheduler, "admit", admit_then_cancel)
    _set_sender(gate, send)

    caller = asyncio.create_task(_call(gate, "request"))
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    assert gate._admission_scheduler.outstanding_depths() == dict.fromkeys(RpcServiceClass, 0)


@pytest.mark.asyncio
async def test_scheduler_close_cancels_active_transport_caller() -> None:
    gate = _gate()
    transport_started = asyncio.Event()
    in_flight: asyncio.Future[object] | None = None

    class _PendingSender:
        def send(self, _request: object, *, ordered: bool = False) -> asyncio.Future[object]:
            nonlocal in_flight
            del ordered
            in_flight = asyncio.get_running_loop().create_future()
            transport_started.set()
            return in_flight

    gate._sender = _PendingSender()
    caller = asyncio.create_task(_call(gate, "request"))
    await transport_started.wait()

    await gate.close_rpc_scheduler()

    assert caller.cancelled()
    assert in_flight is not None and in_flight.cancelled()
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

    gate._sender = _Sender()
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
    gate = _gate()
    attempts = 0
    sleeps: list[float] = []

    def send(request: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FloodWaitError(request=None, capture=7)
        return _request_value(request)

    async def finish_cooldown(delay: float) -> None:
        sleeps.append(delay)
        import mcp_telegram.telegram_rpc as rpc

        rpc._COOLDOWN_DEADLINE = 0

    _set_sender(gate, send)
    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", finish_cooldown)
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
async def test_telethon_update_loop_binds_live_difference_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    seen: list[TelegramRpcSource] = []

    async def base_update_loop(_self: TelegramClient) -> None:
        seen.append(current_rpc_scope().source)

    monkeypatch.setattr(TelegramClient, "_update_loop", base_update_loop)
    await gate._update_loop()

    assert seen == [TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE]


@pytest.mark.asyncio
async def test_telethon_update_child_task_rebinds_live_scope_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    seen: list[tuple[TelegramRpcSource, asyncio.Task[object] | None, asyncio.Task[object] | None]] = []

    async def base_dispatch_update(_self: TelegramClient, _update: object) -> None:
        scope = current_rpc_scope()
        seen.append((scope.source, scope.owner_task, asyncio.current_task()))

    monkeypatch.setattr(TelegramClient, "_dispatch_update", base_dispatch_update)
    with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
        child = asyncio.create_task(gate._dispatch_update("update"))
    await child

    assert seen == [(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE, child, child)]
