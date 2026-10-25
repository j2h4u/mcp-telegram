"""Scheduled contact failures retain only safe, current-account RPC identity."""

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import cast

import pytest
from telethon.errors import RPCError
from telethon.errors.rpcbaseerrors import BadRequestError
from telethon.tl import types
from telethon.tl.functions.channels import GetParticipantsRequest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import (
    EntityProfileDemandAdapter,
    RefreshLimits,
    failure_retry_at,
    scope_changed_retry_at,
)
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_gateway import TelethonChannelProfileGateway
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from tests.helpers import in_memory_sync_db
from tests.test_entity_profile_progressive import _test_service

_PEER = -1_000_000_000_123


class _PrivateRequest:
    def __repr__(self) -> str:
        raise AssertionError("private request repr must not be inspected")


class _Session:
    def get_input_entity(self, _peer: int) -> types.InputPeerChannel:
        return types.InputPeerChannel(123, 456)


class _Client:
    session = _Session()

    def __init__(self) -> None:
        self.failure: BaseException = BadRequestError(_PrivateRequest(), "CONTACTS_TOO_MUCH")
        self.before_failure: Callable[[], None] = lambda: None
        self.requests: list[object] = []

    async def __call__(self, request: object) -> object:
        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        budget.debit()
        self.requests.append(request)
        self.before_failure()
        raise self.failure


type Scenario = tuple[sqlite3.Connection, DaemonEntityInfoService, _Client]


@pytest.fixture
def scenario() -> Iterator[Scenario]:
    conn = in_memory_sync_db()
    service = _test_service(conn, limits=RefreshLimits(), account_id=7)
    client = _Client()
    gateway = TelethonChannelProfileGateway(client, now_provider=lambda: 100.0)
    service._deps = replace(service._deps, channel_profile_port=gateway, channel_reference_provider=gateway)
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(?,'supergroup',10)", (_PEER,))
        conn.execute(
            "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,generation) "
            "VALUES(?,'pending',10,'contact_overlap',3)",
            (_PEER,),
        )
    try:
        yield conn, service, client
    finally:
        conn.close()


async def _run(service: DaemonEntityInfoService) -> None:
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    [("CONTACTS_TOO_MUCH", "badrequesterror:400:CONTACTS_TOO_MUCH"), ("BAD\nprivate-identity", "badrequesterror")],
)
async def test_scheduled_gateway_failure_persists_only_safe_identity(
    scenario: Scenario, message: str, expected: str
) -> None:
    conn, service, client = scenario
    client.failure = BadRequestError(_PrivateRequest(), message)
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,payload_json) "
            "VALUES(?,'contact_overlap','fresh',10,'[{\"id\":5}]')",
            (_PEER,),
        )
    try:
        await _run(service)
        assert len(client.requests) == 1
        request = client.requests[0]
        assert isinstance(request, GetParticipantsRequest)
        assert isinstance(request.filter, types.ChannelParticipantsContacts)
        assert (request.filter.q, request.offset, request.limit) == ("", 0, 200)
        assert conn.execute(
            "SELECT status,reason,generation,next_section FROM entity_profile_refresh_state"
        ).fetchone() == ("failed", expected, 3, "contact_overlap")
        assert conn.execute(
            "SELECT status,reason,observed_at,payload_json,retry_at FROM entity_detail_sections"
        ).fetchone() == ("stale", expected, 10, '[{"id":5}]', failure_retry_at(100))
        assert conn.execute("SELECT retry_at FROM entity_profile_refresh_state").fetchone() == (failure_retry_at(100),)
        assert "private-identity" not in "\n".join(conn.iterdump())
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["contact_overlap", "full_profile"])
async def test_other_failures_keep_existing_class_reason(scenario: Scenario, section: str) -> None:
    conn, service, client = scenario
    if section == "full_profile":
        # A non-contact operation traverses the same generic failure handler.
        with write_transaction(conn):
            conn.execute("UPDATE entity_profile_refresh_state SET next_section='full_profile'")
        client.failure = BadRequestError(_PrivateRequest(), "CONTACTS_TOO_MUCH")
        expected = "badrequesterror"
    else:
        client.failure = RuntimeError("private-identity")
        expected = "runtimeerror"
    try:
        await _run(service)
        assert conn.execute("SELECT reason FROM entity_profile_refresh_state").fetchone() == (expected,)
        assert "private-identity" not in "\n".join(conn.iterdump())
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_unusable_rpc_code_keeps_existing_class_reason(scenario: Scenario) -> None:
    conn, service, client = scenario
    client.failure = RPCError(_PrivateRequest(), "SAFE_SYMBOL", code=600)
    try:
        await _run(service)
        assert conn.execute("SELECT reason FROM entity_profile_refresh_state").fetchone() == ("rpcerror",)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["generation", "page_cursor"])
async def test_stale_cursor_cannot_publish_rpc_identity(scenario: Scenario, fence: str) -> None:
    conn, service, client = scenario

    def invalidate() -> None:
        with write_transaction(conn):
            if fence == "generation":
                conn.execute("UPDATE entity_profile_refresh_state SET generation=4,reason='new_cursor'")
            else:
                conn.execute("UPDATE entity_profile_refresh_state SET page_cursor_json='{}',reason='new_cursor'")

    client.before_failure = invalidate
    try:
        await _run(service)
        assert conn.execute("SELECT status,reason,generation FROM entity_profile_refresh_state").fetchone() == (
            "pending",
            "new_cursor",
            4 if fence == "generation" else 3,
        )
        assert conn.execute("SELECT count(*) FROM entity_detail_sections").fetchone() == (0,)
    finally:
        await service.shutdown()


class _QueuedWriter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.pause = False
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def run[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        if self.pause:
            self.waiting.set()
            await self.release.wait()
        return operation(self.conn)


@pytest.mark.asyncio
async def test_queued_failure_rechecks_account_before_publishing(scenario: Scenario) -> None:
    conn, service, client = scenario
    writer = _QueuedWriter(conn)
    service._deps = replace(service._deps, writer=cast(SQLiteWriter, writer))
    client.before_failure = lambda: setattr(writer, "pause", True)
    task = asyncio.create_task(_run(service))
    try:
        await asyncio.wait_for(writer.waiting.wait(), timeout=2)
        service._deps = replace(
            service._deps, full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 8, 2, 99)
        )
        writer.release.set()
        await task
        assert conn.execute("SELECT reason,retry_at,generation FROM entity_profile_refresh_state").fetchone() == (
            "auth_scope_changed",
            scope_changed_retry_at(100),
            3,
        )
        assert "CONTACTS_TOO_MUCH" not in "\n".join(conn.iterdump())
    finally:
        writer.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.shutdown()
