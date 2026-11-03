"""Real command receipt and guarded transport composition for native writes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError
from telethon.network.connection.tcpfull import FullPacketCodec
from telethon.sessions import MemorySession
from telethon.tl import functions, types

from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.contact_notes.commands import ContactNoteCommandOwner
from mcp_telegram.contact_notes.domain import NoteWriteAuthority
from mcp_telegram.contact_notes.intake import capture_contact_note_intake
from mcp_telegram.contact_notes.state import read_note
from mcp_telegram.entity_profile.contracts import CommandProfileAcquisition, UserReference
from mcp_telegram.native_write_fence import NativeWireOutcome, NativeWriteReceiptBinding, NoteWriteRejected
from mcp_telegram.peer_access import PeerAccess
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.state import telegram_session_lease
from mcp_telegram.sync_db import (
    ensure_sync_schema,
    load_account_cooldown_until_utc,
    load_rpc_safety_owners,
    load_rpc_safety_record,
    open_sync_db_reader,
    save_rpc_safety_record,
)
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc import (
    TelegramRpcBudget,
    TelegramRpcGate,
    TelegramRpcSafetyStore,
    _TransportBoundaryState,
    _WireAttempt,
)
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from tests.helpers import reset_rpc_process_policy
from tests.test_telegram_rpc_wire_integration import _Circuit
from tests.test_telegram_transport import _Writer


@pytest.fixture
async def physical_note(tmp_path: Path) -> AsyncIterator[tuple[object, ...]]:  # noqa: PLR0915
    reset_rpc_process_policy()
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)

    async def save(expected: str | None, encoded: str) -> None:
        await writer.run(lambda conn: save_rpc_safety_record(conn, expected, encoded))

    store = TelegramRpcSafetyStore(
        lambda: load_rpc_safety_record(reader),
        save,
        lambda: load_account_cooldown_until_utc(reader),
        lambda: load_rpc_safety_owners(reader),
    )
    session = MemorySession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    gate = TelegramRpcGate(
        session,
        1,
        "test",
        rpc_budget=TelegramRpcBudget(30, 60),
        circuit_status=_Circuit,
        fallback_wait_seconds=1,
        cooldown_buffer_seconds=0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=store,
    )
    lease_stack = ExitStack()
    gate.bind_session_lease(lease_stack.enter_context(telegram_session_lease(tmp_path)))
    authority = NoteWriteAuthority(
        writer,
        gate.retained_auth_scope,
        lambda user_id: UserReference(user_id, 55),
        capture_scope=gate.capture_image_scope,
        claim_scope=gate.claim_image_scope,
    )
    gate.register_native_write_domain("contact_note", authority.domain)
    await gate.verify_startup_account(42)
    auth = gate.retained_auth_scope()
    assert auth is not None
    access = PeerAccess(auth)
    await writer.run(access.bind)
    gate.bind_peer_access(access)
    sender = cast(GuardedMTProtoSender, gate._main_sender)
    connection = GuardedConnectionTcpFull("127.0.0.1", 443, 2, loggers=gate._log)
    connection.bind_owner(sender)
    connection._connected = True
    sink = _Writer()
    connection._writer = cast(asyncio.StreamWriter, sink)
    connection._codec = FullPacketCodec(connection)
    sender._connection = connection
    sender._user_connected = True
    gate._transport_state = _TransportBoundaryState.READY

    def populate(conn: sqlite3.Connection) -> None:
        fields = {"full_user.note": None, "user.contact": True}
        coverage = {
            "full_user.note": {"state": "cleared", "authoritative": True},
            "user.contact": {"authoritative": True},
        }
        with write_transaction(conn):
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (43,'user',0)")
            conn.execute(
                "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,"
                "fields_json,coverage_json,source,provenance_json) VALUES (42,43,'v1',?,?,'rpc',?)",
                (json.dumps(fields), json.dumps(coverage), json.dumps({"fields": {"full_user.note": {"order": 1}}})),
            )

    await writer.run(populate)
    try:
        yield gate, sender, connection, sink, writer, reader, authority
    finally:
        for state in tuple(sender._owned_states):
            if not state.future.done():
                state.future.cancel()
        await asyncio.sleep(0)
        await asyncio.gather(*sender._observing, return_exceptions=True)
        await gate._admission_scheduler.close()
        await connection.disconnect()
        await writer.run(access.unbind)
        await writer.aclose()
        reader.close()
        lease_stack.close()
        reset_rpc_process_policy()


@pytest.mark.parametrize(
    "terminal",
    [
        "acknowledged",
        "rejected",
        "unknown",
        "body_swap",
        "target_swap",
        "receipt_failure",
        "flood",
        "cancel",
        "intake_preflight",
        "intake_claim",
        "forged_token",
        "deadline",
        "cancel_unknown",
        "auth_transition",
        "notify_ack_active",
        "notify_unknown_active",
        "notify_late_ack",
        "notify_late_unknown",
        "notify_failure",
        "notify_cancel",
    ],
)
async def test_note_owner_records_exact_physical_outcome_without_replay(  # noqa: PLR0911, PLR0912, PLR0914, PLR0915
    physical_note: tuple[object, ...], terminal: str
) -> None:
    gate, sender, connection, sink, writer, reader, authority = physical_note
    gate = cast(TelegramRpcGate, gate)
    sender = cast(GuardedMTProtoSender, sender)
    connection = cast(GuardedConnectionTcpFull, connection)
    sink = cast(_Writer, sink)
    writer = cast(SQLiteWriter, writer)
    reader = cast(sqlite3.Connection, reader)
    authority = cast(NoteWriteAuthority, authority)
    peer = types.InputUser(43, 55)
    mutations: list[object] = []
    notices: list[tuple[str, str, object]] = []
    wire_attempts: list[_WireAttempt] = []
    recording = asyncio.Event()
    release = asyncio.Event()
    if terminal.startswith("notify_"):
        registered = gate._native_write_domains["contact_note"]

        async def notify(binding: NativeWriteReceiptBinding, outcome: NativeWireOutcome) -> None:
            row = cast(
                tuple[str] | None,
                reader.execute(
                    "SELECT outcome FROM contact_note_operation_receipts WHERE account_id=? AND operation_key=?",
                    (binding.account_id, binding.operation_key),
                ).fetchone(),
            )
            assert row is not None
            notices.append((outcome, row[0], gate.account_safety_status()))
            if terminal == "notify_failure":
                raise sqlite3.OperationalError("injected notification failure")
            if terminal == "notify_cancel":
                raise asyncio.CancelledError

        gate._native_write_domains["contact_note"] = replace(registered, notify_terminal=notify)
    if terminal in {"cancel", "cancel_unknown"}:
        registered = gate._native_write_domains["contact_note"]

        async def delay_receipt(binding: NativeWriteReceiptBinding, attempt: str, outcome: NativeWireOutcome) -> None:
            recording.set()
            await release.wait()
            await registered.record_wire_outcome(binding, attempt, outcome)

        gate._native_write_domains["contact_note"] = replace(registered, record_wire_outcome=delay_receipt)
    if terminal == "receipt_failure":

        async def fail_receipt(*_args: object) -> None:
            raise sqlite3.OperationalError("injected receipt failure")

        registered = gate._native_write_domains["contact_note"]
        gate._native_write_domains["contact_note"] = replace(registered, record_wire_outcome=fail_receipt)

    async def call(request: object) -> object:  # noqa: PLR0912, PLR0915
        response = sender.send_owned(request, current_rpc_scope())
        state = await sender._send_queue.next_state()
        if terminal == "forged_token" and isinstance(request, functions.contacts.UpdateContactNoteRequest):
            wire_owner = cast(_WireAttempt, sender.application_owner(response))
            assert wire_owner.native_check is not None

            async def forge() -> Callable[[], bool]:
                return lambda: True

            wire_owner.native_check = replace(wire_owner.native_check, revalidate=forge)
        if isinstance(request, functions.contacts.UpdateContactNoteRequest):
            if terminal == "body_swap":
                request.note = types.TextWithEntities("substituted", [])
            elif terminal == "target_swap":
                request.id = types.InputUser(44, 55)
        try:
            await sender._prepare_state(state)
        except NoteWriteRejected:
            return await response
        packet = connection._send_queue.get_nowait()
        if terminal == "auth_transition" and isinstance(request, functions.contacts.UpdateContactNoteRequest):
            ledger = gate._ensure_safety_ledger()
            original_store = ledger._store
            entered = asyncio.Event()
            finish = asyncio.Event()

            async def block_save(expected: str | None, encoded: str) -> None:
                entered.set()
                await finish.wait()
                await original_store.save_record(expected, encoded)

            ledger._store = replace(original_store, save_record=block_save)
            scope = gate.retained_auth_scope()
            assert scope is not None
            transition = asyncio.create_task(
                ledger.begin_authentication(scope.dc_id, scope.auth_key_id, acknowledged=True)
            )
            await entered.wait()
            assert gate.retained_auth_scope() == scope
            assert not sender.write_packet(packet, connection._send)
            finish.set()
            await transition
            ledger._store = original_store
            return await response
        if terminal in {"intake_claim", "forged_token", "deadline"} and isinstance(
            request, functions.contacts.UpdateContactNoteRequest
        ):
            if terminal == "deadline":
                wire_owner = cast(_WireAttempt, packet.state.owner)
                wire_owner.scope = replace(wire_owner.scope, deadline=time.monotonic() - 1)
            else:
                capture_contact_note_intake([types.UpdateUser(43)])
            assert not sender.write_packet(packet, connection._send)
            return await response
        assert sender.write_packet(packet, connection._send)
        if terminal == "intake_preflight" and isinstance(request, functions.users.GetFullUserRequest):
            capture_contact_note_intake([types.UpdateUser(43)])
        if isinstance(request, functions.contacts.UpdateContactNoteRequest):
            mutations.append(request)
            wire_attempts.append(cast(_WireAttempt, packet.state.owner))
            if terminal in {"notify_late_ack", "notify_late_unknown", "notify_failure", "notify_cancel"}:
                gate._revoke_wire_owner(wire_attempts[-1])
            if terminal in {"unknown", "cancel_unknown", "notify_unknown_active", "notify_late_unknown"}:
                state.future.set_exception(RuntimeError("lost response"))
            elif terminal == "flood":
                state.future.set_exception(FloodWaitError(request=None, capture=30))
            else:
                state.future.set_result(
                    terminal
                    in {
                        "acknowledged",
                        "receipt_failure",
                        "cancel",
                        "notify_ack_active",
                        "notify_late_ack",
                        "notify_failure",
                        "notify_cancel",
                    }
                )
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
        resolve_input_user=lambda _target: peer,
        acquire_command_profile=acquire,
        acquire_config=config,
        request_reconciliation=request_reconciliation,
    )
    version = read_note(reader, 42, 43)["expected_version"]
    request = {
        "dialog_id": 43,
        "action": "create",
        "expected_version": version,
        "operation_key": "operation",
        "text": "new",
    }
    result: object = None
    try:
        if terminal in {"cancel", "cancel_unknown"}:
            command = asyncio.create_task(owner.execute(request))
            await asyncio.wait_for(recording.wait(), 5)
            command.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await command
            await asyncio.gather(*sender._observing, return_exceptions=True)
        else:
            result = await owner.execute(request)
    except RuntimeError as error:
        assert terminal in {"unknown", "notify_unknown_active", "notify_late_unknown"} and str(error) == "lost response"
    except sqlite3.OperationalError as error:
        assert terminal == "receipt_failure" and "injected receipt failure" in str(error)
    except FloodWaitError:
        assert terminal == "flood"
    row = cast(
        tuple[str, str | None] | None,
        reader.execute(
            "SELECT outcome,attempt_id FROM contact_note_operation_receipts WHERE account_id=42 AND operation_key='operation'"
        ).fetchone(),
    )
    if terminal.startswith("notify_"):
        expected = "unknown" if terminal in {"notify_unknown_active", "notify_late_unknown"} else "acknowledged"
        assert row is not None and row[0] == expected and row[1]
        if terminal in {"notify_ack_active", "notify_unknown_active"}:
            assert notices == []
        else:
            assert notices == [(expected, expected, None)]
        assert gate._transport_state is _TransportBoundaryState.READY
        return
    if terminal == "auth_transition":
        assert row is not None and row[0] == "rejected"
        assert len(mutations) == 0 and len(sink.frames) == 1
        assert gate._transport_state is _TransportBoundaryState.READY
        return
    if terminal in {"intake_preflight", "intake_claim", "forged_token", "deadline"}:
        assert row is None or row[0] == "rejected"
        assert len(mutations) == 0 and len(sink.frames) == 1
        assert gate.account_safety_status() is None
        return
    if terminal in {"cancel", "cancel_unknown"}:
        expected = "unknown" if terminal == "cancel_unknown" else "acknowledged"
        assert row is not None and row[0] == expected and row[1]
        assert len(mutations) == 1 and gate.account_safety_status() is None
        if terminal == "cancel_unknown":
            await gate.observe_result(wire_attempts[0], None, True)
            after = cast(
                tuple[str] | None,
                reader.execute(
                    "SELECT outcome FROM contact_note_operation_receipts WHERE account_id=42 AND operation_key='operation'"
                ).fetchone(),
            )
            assert after is not None and after[0] == "unknown"
        return
    if terminal == "flood":
        assert row is not None and row[0] == "rejected" and row[1]
        assert len(mutations) == 1
        retry_until = gate._ensure_safety_ledger().status()["retry_until_utc"]
        assert isinstance(retry_until, float) and retry_until > time.time()
        return
    if terminal == "receipt_failure":
        assert row is not None and row[0] == "pending" and row[1]
        assert len(mutations) == 1 and gate.account_safety_status() is not None
        return
    if terminal in {"body_swap", "target_swap"}:
        assert row is not None and row[0] == "rejected" and row[1] is None
        assert len(mutations) == 0 and len(sink.frames) == 1
        assert gate.account_safety_status() is None
        return
    assert row is not None and row[0] == terminal and row[1], result
    assert len(mutations) == 1
    before = len(sink.frames)
    await owner.execute(request)
    assert len(mutations) == 1 and len(sink.frames) == before
    if terminal == "unknown":
        assert gate.account_safety_status() is None
        assert gate._ensure_safety_ledger()._record["recovery"] is None
