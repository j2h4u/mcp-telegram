from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import cast

import pytest
from telethon.requestiter import RequestIter

from mcp_telegram.telegram_rpc import (
    GovernedTelegramClient,
    TelegramRpcBudget,
    TelegramRpcCircuitOpenError,
    TelegramRpcGovernor,
)


@dataclass(frozen=True, slots=True)
class _CircuitStatus:
    open: bool

    def detail(self) -> str:
        return "open-for-test"


class _Client:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, *_args: object, **_kwargs: object) -> object:
        self.calls.append("call")
        return "called"

    async def get_messages(self, *_args: object, **_kwargs: object) -> object:
        self.calls.append("get_messages")
        return "messages"

    async def get_input_entity(self, *_args: object, **_kwargs: object) -> object:
        self.calls.append("get_input_entity")
        return "input-entity"

    async def iter_messages(self, *_args: object, **_kwargs: object) -> AsyncIterator[int]:
        self.calls.append("iter_messages")
        yield 1

    async def iter_dialogs(self, *_args: object, **_kwargs: object) -> AsyncIterator[int]:
        self.calls.append("iter_dialogs")
        yield 2

    async def iter_participants(self, *_args: object, **_kwargs: object) -> AsyncIterator[int]:
        self.calls.append("iter_participants")
        yield 3


class _CountingGovernor:
    def __init__(self) -> None:
        self.sources: list[str] = []

    async def acquire(self, *, source: str) -> None:
        self.sources.append(source)


class _PagedRequestIter(RequestIter):
    def __init__(self, client: object, pages: list[list[int]]) -> None:
        super().__init__(client, limit=None)
        self._pages = pages
        self._page_index = 0

    async def _load_next_chunk(self) -> bool:
        client = cast(Callable[[object], Awaitable[object]], self.client)
        page = cast(list[int], await client(self._page_index))
        self._page_index += 1
        assert self.buffer is not None
        self.buffer.extend(page)
        return self._page_index >= len(self._pages)


class _PagedClient(_Client):
    def __init__(self, pages: list[list[int]]) -> None:
        super().__init__()
        self.pages = pages
        self.page_requests: list[int] = []

    async def __call__(self, *args: object, **_kwargs: object) -> object:
        request = cast(int, args[0])
        self.page_requests.append(request)
        return self.pages[request]

    def iter_dialogs(self, *_args: object, **_kwargs: object) -> _PagedRequestIter:
        return _PagedRequestIter(self, self.pages)


def _governed_client(status: _CircuitStatus) -> tuple[_Client, GovernedTelegramClient]:
    client = _Client()
    governor = TelegramRpcGovernor(
        TelegramRpcBudget(max_calls_per_period=100, period_seconds=60.0),
        circuit_status=lambda: status,
    )
    return client, GovernedTelegramClient(client, governor)


@pytest.mark.asyncio
async def test_governed_client_delegates_telegram_methods() -> None:
    raw_client, client = _governed_client(_CircuitStatus(open=False))

    assert await client("request") == "called"
    assert await client.get_messages(entity=1) == "messages"
    assert await client.get_input_entity(1) == "input-entity"
    assert [item async for item in client.iter_messages(entity=1)] == [1]

    assert raw_client.calls == ["call", "get_messages", "get_input_entity", "iter_messages"]


@pytest.mark.asyncio
async def test_governed_client_blocks_when_circuit_is_open() -> None:
    raw_client, client = _governed_client(_CircuitStatus(open=True))

    with pytest.raises(TelegramRpcCircuitOpenError, match="open-for-test"):
        await client.get_messages(entity=1)

    assert raw_client.calls == []


@pytest.mark.asyncio
async def test_governed_client_blocks_peer_resolution_when_circuit_is_open() -> None:
    raw_client, client = _governed_client(_CircuitStatus(open=True))

    with pytest.raises(TelegramRpcCircuitOpenError, match="open-for-test"):
        await client.get_input_entity(1)

    assert raw_client.calls == []


@pytest.mark.asyncio
async def test_governed_request_iter_acquires_once_per_underlying_page() -> None:
    raw_client = _PagedClient([[1, 2], [3]])
    governor = _CountingGovernor()
    client = GovernedTelegramClient(raw_client, governor)  # type: ignore[arg-type]

    assert [item async for item in client.iter_dialogs()] == [1, 2, 3]
    assert raw_client.page_requests == [0, 1]
    assert governor.sources == ["client_call", "client_call"]
