"""Tests for public Telethon reconnect update recovery."""

from __future__ import annotations

import asyncio
import time

import pytest

from mcp_telegram.reconnect import run_reconnect_catch_up_loop
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    UnclassifiedTelegramDemandError,
    current_demand_token,
    demand_context,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import RpcAdmissionClosedError, current_rpc_scope


class _Client:
    def __init__(self, shutdown: asyncio.Event) -> None:
        self._shutdown = shutdown
        self.reconnect_event = asyncio.Event()
        self.catch_up_calls = 0

    async def catch_up(self) -> None:
        self.catch_up_calls += 1
        if self.catch_up_calls == 1:
            self.reconnect_event.set()
        else:
            self._shutdown.set()


@pytest.mark.asyncio
async def test_reconnect_loop_catches_up_once_per_signal() -> None:
    shutdown = asyncio.Event()
    client = _Client(shutdown)
    client.reconnect_event.set()
    assert client.reconnect_event.is_set()
    recoveries: list[str] = []
    observations: list[tuple[str, str]] = []

    await run_reconnect_catch_up_loop(
        client,
        shutdown,
        interval_seconds=0.001,
        observe=lambda kind, outcome, _reason: observations.append((kind, outcome)),
        on_reconnect=lambda: recoveries.append("reconnect"),
    )

    assert client.catch_up_calls == 2
    assert recoveries == ["reconnect", "reconnect"]
    assert observations == [
        ("runtime.connection_observed", "reconnected"),
        ("runtime.catch_up_requested", "requested"),
        ("runtime.connection_observed", "reconnected"),
        ("runtime.catch_up_requested", "requested"),
    ]


@pytest.mark.asyncio
async def test_reconnect_signal_is_consumed_without_poll_interval() -> None:
    shutdown = asyncio.Event()
    client = _Client(shutdown)
    client.catch_up = lambda: _stop_after(shutdown, 0)  # type: ignore[method-assign]
    loop = asyncio.create_task(run_reconnect_catch_up_loop(client, shutdown, interval_seconds=5.0))
    await asyncio.sleep(0)

    started = time.monotonic()
    client.reconnect_event.set()
    await asyncio.wait_for(loop, timeout=0.1)

    assert time.monotonic() - started < 0.1


@pytest.mark.asyncio
async def test_reconnect_event_coalesces_repeated_pending_signals() -> None:
    shutdown = asyncio.Event()

    class Client:
        reconnect_event = asyncio.Event()
        catch_up_calls = 0

        async def catch_up(self) -> None:
            self.catch_up_calls += 1
            shutdown.set()

    client = Client()
    client.reconnect_event.set()
    client.reconnect_event.set()

    await run_reconnect_catch_up_loop(client, shutdown, interval_seconds=0.001)

    assert client.catch_up_calls == 1


@pytest.mark.asyncio
async def test_reconnect_loop_does_not_catch_up_while_steady_connected() -> None:
    shutdown = asyncio.Event()

    class ConnectedClient:
        def __init__(self) -> None:
            self.catch_up_calls = 0
            self.reconnect_event = asyncio.Event()

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
            self.reconnect_event = asyncio.Event()
            self.reconnect_event.set()
            self.catch_up_calls = 0

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
        reconnect_event = asyncio.Event()

        async def catch_up(self) -> None:
            raise RpcAdmissionClosedError(current_rpc_scope(), "scheduler closed")

    client = ClosedClient()
    client.reconnect_event.set()

    with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
        await run_reconnect_catch_up_loop(client, shutdown, interval_seconds=0.001)


@pytest.mark.asyncio
async def test_reconnect_catch_up_creates_inline_root_and_refines_acquisition() -> None:
    shutdown = asyncio.Event()
    observed: list[tuple[DemandKind, AcquisitionKind | None]] = []

    class InspectingClient:
        def __init__(self) -> None:
            self.reconnect_event = asyncio.Event()
            self.reconnect_event.set()

        async def catch_up(self) -> None:
            token = current_demand_token()
            observed.append((token.kind, token.acquisition_kind))
            shutdown.set()

    await run_reconnect_catch_up_loop(InspectingClient(), shutdown, interval_seconds=0.001)

    assert observed == [(DemandKind.RECONNECT_DIFFERENCE, AcquisitionKind.UPDATE_DIFFERENCE)]
    with pytest.raises(UnclassifiedTelegramDemandError):
        current_demand_token()


@pytest.mark.asyncio
async def test_reconnect_attempt_rejects_wrong_outer_root() -> None:
    shutdown = asyncio.Event()
    client = _Client(shutdown)
    client.reconnect_event.set()

    with demand_context(DemandKind.REALTIME_EVENT_ACQUISITION):
        with pytest.raises(RuntimeError, match="requires reconnect_difference demand"):
            await run_reconnect_catch_up_loop(client, shutdown, interval_seconds=0.001)

    assert client.catch_up_calls == 0


@pytest.mark.asyncio
async def test_reconnect_catch_up_cancellation_propagates() -> None:
    shutdown = asyncio.Event()

    class CancelledClient:
        def __init__(self) -> None:
            self.reconnect_event = asyncio.Event()
            self.reconnect_event.set()

        async def catch_up(self) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_reconnect_catch_up_loop(CancelledClient(), shutdown, interval_seconds=0.001)


@pytest.mark.asyncio
async def test_reconnect_retries_use_fresh_root_contexts() -> None:
    shutdown = asyncio.Event()
    observed_tokens: list[object] = []

    class RetryClient:
        def __init__(self) -> None:
            self.reconnect_event = asyncio.Event()
            self.reconnect_event.set()

        async def catch_up(self) -> None:
            observed_tokens.append(current_demand_token())
            if len(observed_tokens) == 1:
                raise RuntimeError("retry")
            shutdown.set()

    await run_reconnect_catch_up_loop(RetryClient(), shutdown, interval_seconds=0.001)

    assert len(observed_tokens) == 2
    assert observed_tokens[0] is not observed_tokens[1]


@pytest.mark.asyncio
async def test_reconnect_attempt_preserves_valid_outer_reconnect_root() -> None:
    shutdown = asyncio.Event()
    observed_deadlines: list[float] = []

    class InspectingClient:
        def __init__(self) -> None:
            self.reconnect_event = asyncio.Event()
            self.reconnect_event.set()

        async def catch_up(self) -> None:
            observed_deadlines.append(current_demand_token().admission_deadline)
            shutdown.set()

    with demand_context(DemandKind.RECONNECT_DIFFERENCE) as outer:
        await run_reconnect_catch_up_loop(InspectingClient(), shutdown, interval_seconds=0.001)

    assert observed_deadlines == [outer.admission_deadline]
