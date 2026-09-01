# pyright: reportAny=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
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

from mcp_telegram.config import FloodWaitConfig, McpTelegramConfig, StateConfig, TelegramRpcConfig
from mcp_telegram.flood import FloodWaitAccumulator, FloodWaitKillSwitchPolicy, TelegramRpcThrottled
from mcp_telegram.telegram import create_client
from mcp_telegram.telegram_rpc import (
    TelegramRpcBudget,
    TelegramRpcGate,
    account_cooldown_deadline,
    reset_account_cooldown,
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


@pytest.fixture(autouse=True)
def _reset_process_policy() -> None:
    reset_account_cooldown()


class _PagedRequestIter(RequestIter):
    def __init__(self, client: object, pages: list[list[int]]) -> None:
        super().__init__(client, limit=None)
        self._pages = pages
        self._page = 0

    async def _load_next_chunk(self) -> bool:
        page = await self.client(self._page)
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
    return gate


@pytest.mark.asyncio
async def test_gate_is_real_telethon_subclass_and_direct_call_crosses_override(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    called: list[object] = []

    async def base_call(_self: TelegramClient, request: object, **_kwargs: object) -> object:
        called.append(request)
        return "called"

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    assert isinstance(gate, TelegramClient)
    assert await gate("request") == "called"
    assert called == ["request"]
    assert gate._limiter.acquisitions == 1


@pytest.mark.asyncio
async def test_helper_and_request_iter_pages_use_the_same_public_call_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    calls: list[object] = []

    async def base_call(_self: TelegramClient, request: object, **_kwargs: object) -> object:
        calls.append(request)
        if isinstance(request, int):
            return [[1, 2], [3]][request]
        return [SimpleNamespace(id=1, is_self=True)]

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    gate._mb_entity_cache = SimpleNamespace(self_id=1)
    assert getattr(await gate.get_me(), "id", None) == 1
    iterator = _PagedRequestIter(gate, [[1, 2], [3]])
    assert [item async for item in iterator] == [1, 2, 3]
    assert calls[0].__class__.__name__ == "GetUsersRequest"
    assert calls[1:] == [0, 1]
    assert gate._limiter.acquisitions == 3


@pytest.mark.asyncio
async def test_get_entity_username_resolution_uses_the_same_public_call_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()
    calls: list[object] = []
    user = types.User(1, access_hash=2, username="alice")

    async def base_call(_self: TelegramClient, request: object, **_kwargs: object) -> object:
        calls.append(request)
        assert isinstance(request, functions.contacts.ResolveUsernameRequest)
        return types.contacts.ResolvedPeer(types.PeerUser(1), [], [user])

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
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
    finally:
        gate.session.close()


def test_telethon_public_helper_and_update_loop_contract_is_pinned() -> None:
    from telethon.client.updates import UpdateMethods
    from telethon.tl.custom.message import Message

    assert telethon.__version__ == "1.44.0"
    assert "await self(" in inspect.getsource(TelegramClient.get_me)
    sender_source = inspect.getsource(Message.get_sender)
    assert "await self._client.get_entity" in sender_source
    update_source = inspect.getsource(UpdateMethods._update_loop)
    assert "diff = await self(get_diff)" in update_source
    assert "await self(get_diff)" in update_source


@pytest.mark.asyncio
async def test_gate_blocks_when_circuit_is_open() -> None:
    gate = _gate(_CircuitStatus(open=True))
    with pytest.raises(TelegramRpcThrottled, match="open-for-test") as caught:
        await gate("request")
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

    async def base_call(_self: TelegramClient, _request: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ServerError(None, "temporary")
        return "ok"

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", fake_sleep)
    assert await gate("request") == "ok"
    assert attempts == 2
    assert gate._limiter.acquisitions == 2
    assert sleeps == [2.0]


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
        await gate(functions.PingRequest(1))

    assert caught.value.__cause__ is final
    assert caught.value.retry_after_seconds == 7
    assert gate._sender.calls == 2
    assert gate._limiter.acquisitions == 2
    assert sleeps == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError])
async def test_gate_normalizes_each_vendor_wait_to_owned_outcome(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type,
) -> None:
    gate = _gate()
    vendor_error = cast(Callable[..., BaseException], error_type)(request=None, capture=7)

    async def base_call(_self: TelegramClient, _request: object, **_kwargs: object) -> object:
        raise vendor_error

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    with pytest.raises(TelegramRpcThrottled) as caught:
        await gate("request")

    assert caught.value.retry_after_seconds == 7
    assert caught.value.latched is False
    assert caught.value.__cause__ is vendor_error


@pytest.mark.asyncio
async def test_gate_rechecks_cooldown_after_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_account_cooldown()
    gate = _gate()
    sleeps: list[float] = []

    async def acquire_and_open() -> None:
        gate._limiter.acquisitions += 1
        import mcp_telegram.telegram_rpc as rpc

        rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 3

    gate._limiter.acquire = acquire_and_open

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        import mcp_telegram.telegram_rpc as rpc

        rpc._COOLDOWN_DEADLINE = 0

    async def base_call(_self: TelegramClient, request: object, **_kwargs: object) -> object:
        return request

    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    assert await gate("request") == "request"
    assert len(sleeps) == 1


@pytest.mark.asyncio
async def test_gate_flood_is_immediate_cooldown_and_observed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_account_cooldown()
    gate = _gate()
    observed: list[dict[str, object]] = []
    gate._flood_observer = lambda **kwargs: observed.append(kwargs)
    error = FloodWaitError(request=None, capture=7)
    attempts = 0

    async def base_call(_self: TelegramClient, _request: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        raise error

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    with pytest.raises(TelegramRpcThrottled) as caught:
        await gate("request")
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

    async def base_call(_self: TelegramClient, _request: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    with pytest.raises(TelegramRpcThrottled) as caught:
        await gate("request")
    assert caught.value.retry_after_seconds == 7
    with pytest.raises(TelegramRpcThrottled) as caught:
        await gate("request")
    assert caught.value.latched
    assert caught.value.retry_after_seconds is None

    status = accumulator.kill_switch_status()
    assert status.open is True
    assert status.events_in_window == 1
    assert gate._limiter.acquisitions == 1


@pytest.mark.asyncio
async def test_gate_cooldown_wait_is_cancellation_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    import mcp_telegram.telegram_rpc as rpc

    rpc._COOLDOWN_DEADLINE = rpc.time.monotonic() + 30

    async def cancel_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("mcp_telegram.telegram_rpc.asyncio.sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await gate("request")


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
    )
    assert isinstance(gate, TelegramClient)
    assert gate._request_retries == 0
    assert gate.flood_sleep_threshold == 0
    assert gate._raise_last_call_error is True
    assert gate._auto_reconnect is True


@pytest.mark.asyncio
async def test_gate_does_not_retry_slow_mode_or_nonretryable_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate(retry_delays=(2.0,))
    attempts = 0

    async def base_call(_self: TelegramClient, _request: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        raise SlowModeWaitError(request=None, capture=2)

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    with pytest.raises(SlowModeWaitError):
        await gate("request")
    assert attempts == 1


@pytest.mark.asyncio
async def test_gate_does_not_retry_arbitrary_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate(retry_delays=(2.0,))
    attempts = 0

    async def base_call(_self: TelegramClient, _request: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        raise ValueError("not a server transient")

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    with pytest.raises(ValueError, match="not a server transient"):
        await gate("request")
    assert attempts == 1
