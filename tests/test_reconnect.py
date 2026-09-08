"""Tests for public Telethon reconnect update recovery."""

from __future__ import annotations

import asyncio

import pytest

from mcp_telegram.reconnect import run_reconnect_catch_up_loop
from mcp_telegram.telegram_rpc_scheduler import RpcAdmissionClosedError, current_rpc_scope


class _Client:
    def __init__(self, states: list[bool], shutdown: asyncio.Event) -> None:
        self._states = iter(states)
        self._shutdown = shutdown
        self.catch_up_calls = 0

    def is_connected(self) -> bool:
        return next(self._states)

    async def catch_up(self) -> None:
        self.catch_up_calls += 1
        if self.catch_up_calls == 2:
            self._shutdown.set()


@pytest.mark.asyncio
async def test_reconnect_loop_catches_up_once_per_observed_transition() -> None:
    shutdown = asyncio.Event()
    client = _Client([False, False, True, True, False, True], shutdown)

    await run_reconnect_catch_up_loop(client, shutdown, interval_seconds=0.001)

    assert client.catch_up_calls == 2


@pytest.mark.asyncio
async def test_reconnect_loop_does_not_catch_up_while_steady_connected() -> None:
    shutdown = asyncio.Event()

    class ConnectedClient:
        def __init__(self) -> None:
            self.catch_up_calls = 0

        def is_connected(self) -> bool:
            return True

        async def catch_up(self) -> None:
            self.catch_up_calls += 1

    client = ConnectedClient()
    stop = asyncio.create_task(_stop_after(shutdown, 0.01))
    await run_reconnect_catch_up_loop(client, shutdown, interval_seconds=0.001)
    await stop

    assert client.catch_up_calls == 0


async def _stop_after(shutdown: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    shutdown.set()


@pytest.mark.asyncio
async def test_reconnect_loop_retries_failure_while_connected(caplog: pytest.LogCaptureFixture) -> None:
    shutdown = asyncio.Event()

    class FailingClient:
        def __init__(self) -> None:
            self._states = iter((False, True, True, True))
            self.catch_up_calls = 0

        def is_connected(self) -> bool:
            return next(self._states)

        async def catch_up(self) -> None:
            self.catch_up_calls += 1
            if self.catch_up_calls < 3:
                raise RuntimeError("catch-up failed")
            shutdown.set()

    client = FailingClient()
    with caplog.at_level("WARNING"):
        await run_reconnect_catch_up_loop(client, shutdown, interval_seconds=0.001)

    assert client.catch_up_calls == 3
    assert any("telegram reconnect catch_up failed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_reconnect_scheduler_closure_propagates() -> None:
    shutdown = asyncio.Event()

    class ClosedClient:
        def is_connected(self) -> bool:
            return False

        async def catch_up(self) -> None:
            raise RpcAdmissionClosedError(current_rpc_scope(), "scheduler closed")

    client = ClosedClient()
    # A reconnect transition is observed on the second poll.
    states = iter((False, True))
    client.is_connected = lambda: next(states)  # type: ignore[method-assign]

    with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
        await run_reconnect_catch_up_loop(client, shutdown, interval_seconds=0.001)
