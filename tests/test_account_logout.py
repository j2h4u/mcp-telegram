"""Operator logout publishes no success or shutdown before durable confirmation."""

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import cast

import pytest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_demand import current_demand_token
from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource
from tests.daemon_api_policy import make_daemon_api_policy
from tests.helpers import (
    LoudChannelProfilePort,
    LoudChatAvatarHistoryPort,
    LoudCommonChatsPort,
    LoudGroupProfilePort,
    LoudUserAvatarHistoryPort,
    LoudUserProfilePort,
)
from tests.sqlite_writer_helpers import inline_writer


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


async def _server(conn: sqlite3.Connection, callback: Callable[[], Awaitable[None]] | None = None) -> DaemonAPIServer:
    server = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, object()),
        asyncio.Event(),
        writer=inline_writer(conn),
        account_logout=callback,
        channel_profile_port=LoudChannelProfilePort(),
        group_profile_port=LoudGroupProfilePort(),
        user_profile_port=LoudUserProfilePort(),
        common_chats_port=LoudCommonChatsPort(),
        user_avatar_history_port=LoudUserAvatarHistoryPort(),
        chat_avatar_history_port=LoudChatAvatarHistoryPort(),
        policy=make_daemon_api_policy(),
    )
    server._ready = True
    server.self_id = 42
    server.self_profile = {"id": 42, "first_name": "Retained identity"}
    await server._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    return server


@dataclass
class _Writer:
    shutdown: asyncio.Event
    written: list[bytes] = field(default_factory=list)
    drain_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_drain: asyncio.Event | None = None
    fail_write: bool = False
    closed: bool = False

    def write(self, data: bytes) -> None:
        assert not self.shutdown.is_set()
        if self.fail_write:
            raise BrokenPipeError
        self.written.append(data)

    async def drain(self) -> None:
        assert not self.shutdown.is_set()
        self.drain_started.set()
        if self.release_drain is not None:
            await self.release_drain.wait()

    def close(self) -> None:
        assert not self.shutdown.is_set()
        self.closed = True

    async def wait_closed(self) -> None:
        assert not self.shutdown.is_set()


def _reader(*, eof: bool) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"method":"logout_account"}\n')
    if eof:
        reader.feed_eof()
    return reader


def _response(writer: _Writer) -> dict[str, object]:
    return cast(dict[str, object], json.loads(b"".join(writer.written)))


def _assert_identity_retained(server: DaemonAPIServer, *, ready: bool = True) -> None:
    assert server._ready is ready
    assert server.self_id == 42
    assert server.self_profile == {"id": 42, "first_name": "Retained identity"}
    assert server._auth_scope == TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    assert not server._shutdown_event.is_set()


async def test_logout_without_backend_preserves_identity(conn: sqlite3.Connection) -> None:
    server = await _server(conn)
    writer = _Writer(server._shutdown_event)

    await server.handle_client(_reader(eof=True), cast(asyncio.StreamWriter, writer))

    assert _response(writer) == {"ok": False, "error": "logout_not_available"}
    _assert_identity_retained(server)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ValueError("private eligibility detail"), "logout_not_eligible"),
        (RuntimeError("private receipt detail"), "logout_not_confirmed"),
        (TelegramRpcThrottled(latched=True, detail="private safety detail"), "logout_not_confirmed"),
    ],
)
@pytest.mark.parametrize("ready", [False, True])
async def test_unconfirmed_logout_does_not_clear_or_restart(
    conn: sqlite3.Connection, failure: Exception, expected: str, ready: bool
) -> None:
    calls = 0

    async def reject() -> None:
        nonlocal calls
        calls += 1
        assert current_demand_token().kind is DemandKind.OPERATOR_LOGOUT
        assert current_demand_token().source is TelegramRpcSource.OPERATOR_LOGOUT
        raise failure

    server = await _server(conn, reject)
    server._ready = ready
    writer = _Writer(server._shutdown_event)

    await server.handle_client(_reader(eof=True), cast(asyncio.StreamWriter, writer))

    assert calls == 1
    assert _response(writer) == {"ok": False, "error": expected}
    assert b"private" not in b"".join(writer.written)
    _assert_identity_retained(server, ready=ready)


@pytest.mark.parametrize("ready", [False, True])
async def test_confirmed_logout_clears_identity_before_reply_and_shutdown(
    conn: sqlite3.Connection, ready: bool
) -> None:
    entered = asyncio.Event()
    confirmed = asyncio.Event()
    flushed = asyncio.Event()
    calls = 0

    async def logout() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await confirmed.wait()

    server = await _server(conn, logout)
    server._ready = ready
    writer = _Writer(server._shutdown_event, release_drain=flushed)
    task = asyncio.create_task(server.handle_client(_reader(eof=False), cast(asyncio.StreamWriter, writer)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        _assert_identity_retained(server, ready=ready)
        assert writer.written == []
        confirmed.set()
        await asyncio.wait_for(writer.drain_started.wait(), 1)
        assert calls == 1
        assert not server._ready
        assert server.self_id is None
        assert server.self_profile is None
        assert server._auth_scope is None
        assert not server._shutdown_event.is_set()
        assert _response(writer) == {"ok": True, "data": {"logout": "confirmed", "restart_scheduled": True}}
        unavailable, _, _ = await server._handle_client_line(b'{"method":"get_me"}', "", None)
        assert unavailable["error"] == "daemon_not_ready"
        flushed.set()
        await asyncio.wait_for(task, 1)
        assert server._shutdown_event.is_set()
        assert writer.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("method", ["recover_account", "logout_account"])
async def test_disconnected_operator_command_keeps_owned_completion(conn: sqlite3.Connection, method: str) -> None:
    entered = asyncio.Event()
    complete = asyncio.Event()
    completed: list[bool] = []

    async def confirm() -> None:
        entered.set()
        await complete.wait()
        completed.append(True)

    async def recover(_generation: str, _acknowledged: bool) -> None:
        await confirm()

    server = await _server(conn, confirm)
    server._account_recovery = recover
    reader = asyncio.StreamReader()
    reader.feed_data(
        json.dumps({"method": method, "expected_generation": "test", "acknowledge_unknown_outcome": True}).encode()
        + b"\n"
    )
    writer = _Writer(server._shutdown_event, fail_write=True)
    task = asyncio.create_task(server.handle_client(reader, cast(asyncio.StreamWriter, writer)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        reader.feed_eof()
        await asyncio.sleep(0)
        assert not task.done()
        assert not server._shutdown_event.is_set()
        complete.set()
        await asyncio.wait_for(task, 1)
        assert completed == [True]
        assert server._shutdown_event.is_set()
        assert writer.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
