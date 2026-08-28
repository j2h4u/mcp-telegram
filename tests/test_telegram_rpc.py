# pyright: reportAny=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import telethon
from telethon import TelegramClient
from telethon.errors import FloodWaitError, ServerError, SlowModeWaitError
from telethon.requestiter import RequestIter
from telethon.sessions import StringSession

from mcp_telegram.telegram_rpc import (
    TelegramRpcBudget,
    TelegramRpcCircuitOpenError,
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
    with pytest.raises(TelegramRpcCircuitOpenError, match="open-for-test"):
        await gate("request")
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

    async def base_call(_self: TelegramClient, _request: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(TelegramClient, "__call__", base_call)
    with pytest.raises(FloodWaitError) as caught:
        await gate("request")
    assert caught.value is error
    assert observed == [{"source": "telegram_rpc_gate", "seconds": 7}]
    assert account_cooldown_deadline() > 0


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
