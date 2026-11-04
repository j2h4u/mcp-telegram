"""Real MCP and IPC reach a physically claimed native contact write."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions, types
from telethon.tl.core import RpcResult, TLMessage

from mcp_telegram import server as mcp_server
from mcp_telegram.contacts.commands import ContactCommandOwner
from mcp_telegram.contacts.contracts import ContactProfileAcquisition
from mcp_telegram.contacts.domain import ContactWriteAuthority
from mcp_telegram.contacts.guard import ContactPeerGuard
from mcp_telegram.contacts.state import read_contact_command_state
from mcp_telegram.daemon_api import DraftCommands
from mcp_telegram.daemon_client import DaemonConnection
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc import TelegramRpcGate, _AdmissionAwareSender
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from mcp_telegram.tools import _base
from tests.test_daemon_api import make_server
from tests.test_native_write_physical import physical_note as physical_note
from tests.test_telegram_transport import _Writer


async def test_mcp_delete_reaches_claimed_gateway_and_durable_ack(  # noqa: PLR0914, PLR0915
    physical_note: tuple[object, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = cast(TelegramRpcGate, physical_note[0])
    sender = cast(GuardedMTProtoSender, physical_note[1])
    connection = cast(GuardedConnectionTcpFull, physical_note[2])
    sink = cast(_Writer, physical_note[3])
    writer = cast(SQLiteWriter, physical_note[4])
    reader = cast(sqlite3.Connection, physical_note[5])
    domain = gate._native_write_domains["contact"]
    authority = cast(ContactWriteAuthority, getattr(domain.preclaim, "__self__"))  # noqa: B009
    scope = gate.retained_auth_scope()
    assert scope is not None
    fields = {
        "user.contact": True,
        "owner.saved_contact_name": {"first_name": "Before", "last_name": ""},
        "owner.saved_contact_phone_access": False,
    }
    coverage = {name: {"state": "present", "authoritative": True} for name in fields}
    provenance = {"fields": {name: {"order": 1} for name in fields}}

    def seed(db: sqlite3.Connection) -> None:
        with write_transaction(db):
            db.execute(
                "UPDATE entity_profile_semantic_snapshots SET fields_json=?,coverage_json=?,provenance_json=? "
                "WHERE owner_account_id=42 AND entity_id=43",
                (json.dumps(fields), json.dumps(coverage), json.dumps(provenance)),
            )

    await writer.run(seed)
    offered: list[int] = []

    async def reconcile(entity: int) -> bool:
        offered.append(entity)
        return True

    authority.bind_reconciliation(reconcile)
    acquired = 0

    async def acquire(_entity: int) -> ContactProfileAcquisition:
        nonlocal acquired
        acquired += 1
        if acquired == 1:
            await call(functions.users.GetFullUserRequest(types.InputUser(43, 55)))
        return ContactProfileAcquisition(acquired == 1, 1 if acquired == 1 else None)

    async def call(request: object) -> object:
        return await _AdmissionAwareSender(gate, current_rpc_scope()).send(request)

    owner = ContactCommandOwner(
        call,
        reader,
        writer=writer,
        authority=authority,
        guard=ContactPeerGuard(),
        identity=gate.retained_auth_scope,
        resolve=lambda _: types.InputUser(43, 55),
        acquire=acquire,
        reconcile=reconcile,
    )
    api = make_server(reader, gate)
    api.self_id = 42
    await api._publish_auth_scope(scope)
    api.bind_contact_commands(cast(DraftCommands, owner))
    socket = tmp_path / "contact.sock"

    @asynccontextmanager
    async def ipc() -> AsyncIterator[DaemonConnection]:
        stream, output = await asyncio.open_unix_connection(str(socket))
        try:
            yield DaemonConnection(stream, output)
        finally:
            output.close()
            await output.wait_closed()

    monkeypatch.setattr(_base, "daemon_connection", ipc)
    monkeypatch.setattr(mcp_server, "daemon_connection", ipc)

    async def pump() -> None:
        preflight = await sender._send_queue.next_state()
        assert isinstance(preflight.request, functions.users.GetFullUserRequest)
        await sender._prepare_state(preflight)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        preflight.future.set_result(True)
        state = await sender._send_queue.next_state()
        assert isinstance(state.request, functions.contacts.DeleteContactsRequest)
        await sender._prepare_state(state)
        claimed = sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        assert claimed, state.future.exception() if state.future.done() else "deferred"
        response = types.Updates(
            [types.UpdateUser(43)], [types.User(id=43, first_name="Before", contact=False)], [], datetime.now(UTC), 0
        )
        await sender._handle_rpc_result(TLMessage(400, 0, RpcResult(state.msg_id, bytes(response), None)))

    task = asyncio.create_task(pump())
    args = {
        "entity_id": 43,
        "action": "delete",
        "operation_key": "mcp-delete",
        "expected_version": read_contact_command_state(reader, 42, 43)["expected_version"],
    }
    async with await asyncio.start_unix_server(api.handle_client, path=socket):
        result = await mcp_server.call_tool("manage_contact", args)
    await task
    await asyncio.gather(*sender._observing)
    assert result.is_error is not True
    assert cast(dict[str, object], result.structured_content)["outcome"] == "acknowledged"
    assert (
        reader.execute(
            "SELECT outcome,attempt_id FROM contact_operation_receipts WHERE operation_key='mcp-delete'"
        ).fetchone()[0]
        == "acknowledged"
    )
    assert len(sink.frames) == 2 and offered == [43]
    assert gate._updates_queue.qsize() == 1
