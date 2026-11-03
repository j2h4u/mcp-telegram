"""Two independently claimed Telegram packets retain both failure causes."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram.contact_notes.commands import ContactNoteCommandOwner
from mcp_telegram.contact_notes.domain import NoteWriteAuthority
from mcp_telegram.contact_notes.state import read_note
from mcp_telegram.entity_profile.contracts import CommandProfileAcquisition
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptEvidence
from mcp_telegram.telegram_rpc import TelegramRpcGate
from mcp_telegram.telegram_rpc_recovery import RecoveryCause
from mcp_telegram.telegram_rpc_scheduler import RpcServiceClass, TelegramRpcScope, TelegramRpcSource, current_rpc_scope
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from tests.test_native_write_physical import physical_note as physical_note
from tests.test_telegram_transport import _Writer


@pytest.mark.parametrize("native_first", [False, True])
async def test_claimed_native_transport_and_read_failures_preserve_both_holds(  # noqa: PLR0914, PLR0915
    physical_note: tuple[object, ...], native_first: bool
) -> None:
    raw_gate, raw_sender, raw_connection, raw_sink, raw_writer, raw_reader, raw_authority = physical_note
    gate = cast(TelegramRpcGate, raw_gate)
    sender = cast(GuardedMTProtoSender, raw_sender)
    connection = cast(GuardedConnectionTcpFull, raw_connection)
    sink = cast(_Writer, raw_sink)
    writer = cast(SQLiteWriter, raw_writer)
    reader = cast(sqlite3.Connection, raw_reader)
    authority = cast(NoteWriteAuthority, raw_authority)
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        time.monotonic() + 20,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(2),
        attempt_evidence=RpcAttemptEvidence(),
    )
    read_response = sender.send_owned(functions.updates.GetStateRequest(), scope)
    read_state = await sender._send_queue.next_state()
    await sender._prepare_state(read_state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    native_claimed = asyncio.Event()
    finish_native = asyncio.Event()
    peer = types.InputUser(43, 55)

    async def call(request: object) -> object:
        response = sender.send_owned(request, current_rpc_scope())
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        if isinstance(request, functions.contacts.UpdateContactNoteRequest):
            native_claimed.set()
            await finish_native.wait()
            state.future.set_exception(ConnectionError("native transport lost"))
        else:
            state.future.set_result(True)
        return await response

    async def acquire(_target: int) -> CommandProfileAcquisition:
        await call(functions.users.GetFullUserRequest(peer))
        return CommandProfileAcquisition(True, True, 1)

    async def config(_hash: int) -> tuple[int, int | None]:
        return 0, 128

    async def request_reconciliation(_target: int) -> bool:
        return True

    owner = ContactNoteCommandOwner(
        call,
        reader,
        writer=writer,
        authority=authority,
        identity=gate.retained_auth_scope,
        resolve_input_user=lambda _: peer,
        acquire_command_profile=acquire,
        acquire_config=config,
        request_reconciliation=request_reconciliation,
    )
    command = asyncio.create_task(
        owner.execute(
            {
                "dialog_id": 43,
                "action": "create",
                "expected_version": read_note(reader, 42, 43)["expected_version"],
                "operation_key": "two-fault-operation",
                "text": "new",
            }
        )
    )
    await asyncio.wait_for(native_claimed.wait(), 10)
    assert len(sink.frames) == 3
    if native_first:
        finish_native.set()
        with pytest.raises(ConnectionError):
            await command
        read_state.future.set_exception(RuntimeError("independent read lost"))
    else:
        read_state.future.set_exception(RuntimeError("independent read lost"))
        with pytest.raises(RuntimeError, match="independent read lost"):
            await read_response
        finish_native.set()
        with pytest.raises(ConnectionError):
            await command
    if native_first:
        with pytest.raises(RuntimeError, match="independent read lost"):
            await read_response
    await asyncio.gather(*sender._observing, return_exceptions=True)
    status = gate._ensure_safety_ledger().status()
    assert status["blocking_request_count"] == 1
    assert gate._wire_fault == "transport_unavailable"
    assert gate._recovery_cause is RecoveryCause.READ_UNCERTAINTY
    assert (
        reader.execute(
            "SELECT outcome FROM contact_note_operation_receipts WHERE operation_key='two-fault-operation'"
        ).fetchone()[0]
        == "unknown"
    )
