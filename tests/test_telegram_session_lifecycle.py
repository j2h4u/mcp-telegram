"""Exclusive Session ownership survives startup and teardown failures."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from mcp_telegram import daemon
from mcp_telegram.config import TelegramRpcSchedulerConfig, load_config
from mcp_telegram.state import telegram_session_lease
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_rpc import (
    TelegramRpcBudget,
    TelegramRpcGate,
    TelegramRpcSafetyStore,
    _TransportBoundaryState,
)
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from tests.helpers import reset_rpc_process_policy


async def test_cancelled_bootstrap_retains_lease_until_its_thread_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = load_config().state.dir
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def bootstrap(*_args: object) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=10), "test did not release bootstrap thread"

    open_writer = AsyncMock(side_effect=AssertionError("cancelled startup must not create a writer"))
    monkeypatch.setattr(daemon, "_bootstrap_sync_database", bootstrap)
    monkeypatch.setattr(daemon.SQLiteWriter, "open", open_writer)
    build = asyncio.create_task(daemon._build_sync_main_context())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        build.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not build.done()
        with pytest.raises(RuntimeError, match="already owned"), telegram_session_lease(state_dir):
            pytest.fail("bootstrap released its exclusive lease before the thread stopped")
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await build
    open_writer.assert_not_called()
    with telegram_session_lease(state_dir):
        pass


class _FailingConnection(GuardedConnectionTcpFull):
    disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        await super().disconnect()
        raise OSError("synthetic transport teardown failure")


class _OpenCircuit:
    open = False

    def detail(self) -> str:
        return "test"


async def test_failed_transport_teardown_closes_session_and_retains_failure(tmp_path: Path) -> None:
    reset_rpc_process_policy()
    session = SQLiteSession(str(tmp_path / "telegram"))
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    session.save()

    async def save(_expected: str | None, _encoded: str) -> None:
        pytest.fail("empty teardown must not need a safety mutation")

    gate = TelegramRpcGate(
        session,
        1,
        "test",
        rpc_budget=TelegramRpcBudget(30, 60),
        circuit_status=_OpenCircuit,
        fallback_wait_seconds=1,
        cooldown_buffer_seconds=0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=TelegramRpcSafetyStore(lambda: None, save, lambda: None, lambda: (None, None)),
    )
    sender = cast(GuardedMTProtoSender, gate._main_sender)
    connection = _FailingConnection("127.0.0.1", 443, 2, loggers=gate._log)
    connection.bind_owner(sender)
    connection._connected = True
    sender._connection = connection
    sender._user_connected = True
    gate._transport_state = _TransportBoundaryState.READY
    assert session._conn is not None
    try:
        with pytest.raises(OSError, match="synthetic transport teardown"):
            await gate.disconnect()
        assert session._conn is None
        assert gate._transport_state is _TransportBoundaryState.FAILED
        with pytest.raises(OSError, match="synthetic transport teardown"):
            await gate.disconnect()
        assert connection.disconnect_calls == 1
        assert gate._transport_state is _TransportBoundaryState.FAILED
        with pytest.raises(OSError, match="synthetic transport teardown"):
            await gate.close_rpc_scheduler()
        assert session._conn is None
    finally:
        await gate._admission_scheduler.close()
        session.close()
        reset_rpc_process_policy()


async def test_composition_failure_closes_client_before_releasing_session_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = load_config().state.dir
    events: list[str] = []

    async def disconnect() -> None:
        with pytest.raises(RuntimeError, match="already owned"), telegram_session_lease(state_dir):
            pytest.fail("client teardown ran after session lease release")
        events.append("disconnect")

    async def close_scheduler() -> None:
        with pytest.raises(RuntimeError, match="already owned"), telegram_session_lease(state_dir):
            pytest.fail("scheduler teardown ran after session lease release")
        events.append("scheduler")

    client = SimpleNamespace(disconnect=disconnect, close_rpc_scheduler=close_scheduler)

    def bootstrap(path: Path, *_args: object) -> None:
        ensure_sync_schema(path)

    def fail_topic_constructor(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic composition failure")

    monkeypatch.setattr(daemon, "_bootstrap_sync_database", bootstrap)
    monkeypatch.setattr(daemon, "_create_telegram_client", lambda *_args: client)
    monkeypatch.setattr(daemon.daemon_shutdown, "register_shutdown_handler", lambda _loop: asyncio.Event())
    monkeypatch.setattr(daemon, "_install_flood_wait_kill_switch", lambda *_args: None)
    monkeypatch.setattr(daemon, "TopicRefresher", fail_topic_constructor)
    with pytest.raises(RuntimeError, match="synthetic composition failure"):
        await daemon._build_sync_main_context()
    assert events == ["disconnect", "scheduler"]
    with telegram_session_lease(state_dir):
        pass
