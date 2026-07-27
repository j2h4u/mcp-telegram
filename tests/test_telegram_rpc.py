from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

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

    async def __call__(self, *_args: object, **_kwargs: object) -> str:
        self.calls.append("call")
        return "called"

    async def get_messages(self, *_args: object, **_kwargs: object) -> str:
        self.calls.append("get_messages")
        return "messages"

    async def iter_messages(self, *_args: object, **_kwargs: object) -> AsyncIterator[int]:
        self.calls.append("iter_messages")
        yield 1

    async def iter_dialogs(self, *_args: object, **_kwargs: object) -> AsyncIterator[int]:
        self.calls.append("iter_dialogs")
        yield 2


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
    assert [item async for item in client.iter_messages(entity=1)] == [1]

    assert raw_client.calls == ["call", "get_messages", "iter_messages"]


@pytest.mark.asyncio
async def test_governed_client_blocks_when_circuit_is_open() -> None:
    raw_client, client = _governed_client(_CircuitStatus(open=True))

    with pytest.raises(TelegramRpcCircuitOpenError, match="open-for-test"):
        await client.get_messages(entity=1)

    assert raw_client.calls == []
