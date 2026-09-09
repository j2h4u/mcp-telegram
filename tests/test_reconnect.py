"""Tests for public Telethon reconnect update recovery."""

from __future__ import annotations

import asyncio

import pytest

from mcp_telegram.reconnect import run_reconnect_catch_up_loop
from mcp_telegram.telegram_demand import AcquisitionKind, current_demand_token, demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind
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

    with demand_context(DemandKind.RECONNECT_DIFFERENCE):
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
    with demand_context(DemandKind.RECONNECT_DIFFERENCE):
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
        with demand_context(DemandKind.RECONNECT_DIFFERENCE):
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

    with demand_context(DemandKind.RECONNECT_DIFFERENCE):
        with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
            await run_reconnect_catch_up_loop(client, shutdown, interval_seconds=0.001)


@pytest.mark.asyncio
async def test_reconnect_catch_up_inherits_durable_root_and_refines_acquisition() -> None:
    shutdown = asyncio.Event()
    observed: list[tuple[DemandKind, AcquisitionKind | None]] = []

    class InspectingClient:
        def __init__(self) -> None:
            self._states = iter((False, True))

        def is_connected(self) -> bool:
            return next(self._states)

        async def catch_up(self) -> None:
            token = current_demand_token()
            observed.append((token.kind, token.acquisition_kind))
            shutdown.set()

    with demand_context(DemandKind.RECONNECT_DIFFERENCE):
        await run_reconnect_catch_up_loop(InspectingClient(), shutdown, interval_seconds=0.001)

    assert observed == [(DemandKind.RECONNECT_DIFFERENCE, AcquisitionKind.UPDATE_DIFFERENCE)]


@pytest.mark.asyncio
async def test_reconnect_loop_rejects_wrong_root_before_polling() -> None:
    shutdown = asyncio.Event()
    client = _Client([False], shutdown)

    with demand_context(DemandKind.REALTIME_EVENT_ACQUISITION):
        with pytest.raises(RuntimeError, match="requires reconnect_difference demand"):
            await run_reconnect_catch_up_loop(client, shutdown, interval_seconds=0.001)

    assert client.catch_up_calls == 0


@pytest.mark.asyncio
async def test_reconnect_catch_up_cancellation_propagates() -> None:
    shutdown = asyncio.Event()

    class CancelledClient:
        def __init__(self) -> None:
            self._states = iter((False, True))

        def is_connected(self) -> bool:
            return next(self._states)

        async def catch_up(self) -> None:
            raise asyncio.CancelledError

    with demand_context(DemandKind.RECONNECT_DIFFERENCE):
        with pytest.raises(asyncio.CancelledError):
            await run_reconnect_catch_up_loop(CancelledClient(), shutdown, interval_seconds=0.001)
