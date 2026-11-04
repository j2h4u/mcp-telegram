"""Physical contact commands use the registered native Gateway and durable receipt."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon.errors import RPCError
from telethon.tl import functions, types

from mcp_telegram.contacts.commands import ContactCommandOwner
from mcp_telegram.contacts.contracts import ContactProfileAcquisition
from mcp_telegram.contacts.domain import ContactWriteAuthority
from mcp_telegram.contacts.guard import ContactPeerGuard
from mcp_telegram.contacts.state import read_contact_command_state
from mcp_telegram.native_write_fence import ContactWriteRejected, NoteWriteRejected
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc import TelegramRpcGate, _AdmissionAwareSender
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from tests.test_native_write_physical import physical_note as physical_note
from tests.test_telegram_transport import _Writer


@pytest.mark.parametrize("action", ["create", "update", "delete"])
@pytest.mark.parametrize("terminal", ["acknowledged", "unknown", "rejected"])
async def test_contact_command_dispatches_one_bound_native_write(  # noqa: PLR0914, PLR0915 - physical composition
    physical_note: tuple[object, ...], action: str, terminal: str
) -> None:
    gate = cast(TelegramRpcGate, physical_note[0])
    sender = cast(GuardedMTProtoSender, physical_note[1])
    connection = cast(GuardedConnectionTcpFull, physical_note[2])
    sink = cast(_Writer, physical_note[3])
    writer = cast(SQLiteWriter, physical_note[4])
    reader = cast(sqlite3.Connection, physical_note[5])
    domain = gate._native_write_domains["contact"]
    authority = cast(ContactWriteAuthority, getattr(domain.preclaim, "__self__"))  # noqa: B009 - bound protocol callback

    async def reconcile(_entity: int) -> bool:
        return True

    authority.bind_reconciliation(reconcile)
    membership = action != "create"
    fields = {
        "user.contact": membership,
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
    peer = types.InputUser(43, 55)
    pumps: list[asyncio.Task[None]] = []

    async def pump() -> None:
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        if isinstance(state.request, functions.users.GetFullUserRequest):
            state.future.set_result(True)
        elif terminal == "unknown":
            state.future.set_exception(ConnectionError("simulated lost response"))
        elif terminal == "rejected":
            state.future.set_exception(RPCError(state.request, "simulated Telegram refusal", code=400))
        else:
            state.future.set_result(types.Updates([], [], [], datetime.now(UTC), 1))

    async def call(request: object) -> object:
        pumps.append(asyncio.create_task(pump()))
        return await _AdmissionAwareSender(gate, current_rpc_scope()).send(request)

    async def acquire(_entity: int) -> ContactProfileAcquisition:
        await call(functions.users.GetFullUserRequest(peer))
        return ContactProfileAcquisition(True, 1)

    owner = ContactCommandOwner(
        call,
        reader,
        writer=writer,
        authority=authority,
        guard=ContactPeerGuard(),
        identity=gate.retained_auth_scope,
        resolve=lambda _entity: peer,
        acquire=acquire,
        reconcile=reconcile,
    )
    request = {
        "entity_id": 43,
        "action": action,
        "expected_version": read_contact_command_state(reader, 42, 43)["expected_version"],
        "operation_key": "key",
    }
    if action != "delete":
        request["first_name"] = "New"
    result = await owner.execute(request)
    await asyncio.gather(*pumps)
    await asyncio.gather(*sender._observing)
    assert result["outcome"] == terminal
    if terminal == "rejected":
        assert result["telegram_error"] == {"type": "RPCError", "code": 400}
        assert result["required_action"]
    expected_frames = 3 if terminal == "acknowledged" else 2
    assert len(sink.frames) == expected_frames
    assert (
        reader.execute(
            "SELECT outcome,attempt_id FROM contact_operation_receipts WHERE operation_key='key'"
        ).fetchone()[0]
        == terminal
    )
    again = await owner.execute(request)
    assert again["outcome"] == terminal and len(sink.frames) == expected_frames


@pytest.mark.parametrize(
    ("action", "member", "first_name", "error", "preflight_frames", "stale"),
    [
        ("create", True, "New", "contact_already_exists", 1, False),
        ("update", False, "New", "contact_required", 1, False),
        ("create", False, "  ", None, 0, False),
        ("update", True, "New", "version_conflict", 1, True),
    ],
)
async def test_contact_refusals_do_not_dispatch_mutation(  # noqa: PLR0913
    physical_note: tuple[object, ...],
    action: str,
    member: bool,
    first_name: str,
    error: str | None,
    preflight_frames: int,
    stale: bool,
) -> None:
    gate = cast(TelegramRpcGate, physical_note[0])
    sender = cast(GuardedMTProtoSender, physical_note[1])
    connection = cast(GuardedConnectionTcpFull, physical_note[2])
    sink = cast(_Writer, physical_note[3])
    writer = cast(SQLiteWriter, physical_note[4])
    reader = cast(sqlite3.Connection, physical_note[5])
    authority = cast(ContactWriteAuthority, getattr(gate._native_write_domains["contact"].preclaim, "__self__"))  # noqa: B009
    fields = {
        "user.contact": member,
        "owner.saved_contact_name": {"first_name": "Before", "last_name": ""},
        "owner.saved_contact_phone_access": False,
    }

    def seed(db: sqlite3.Connection) -> None:
        with write_transaction(db):
            db.execute(
                "UPDATE entity_profile_semantic_snapshots SET fields_json=?,coverage_json=?,provenance_json=? "
                "WHERE owner_account_id=42 AND entity_id=43",
                (
                    json.dumps(fields),
                    json.dumps({k: {"state": "present", "authoritative": True} for k in fields}),
                    json.dumps({"fields": {k: {"order": 1} for k in fields}}),
                ),
            )

    await writer.run(seed)
    peer = types.InputUser(43, 55)

    async def acquire(_entity: int) -> ContactProfileAcquisition:
        future = sender.send_owned(functions.users.GetFullUserRequest(peer), current_rpc_scope())
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        state.future.set_result(True)
        await future
        return ContactProfileAcquisition(True, 1)

    async def no_write(_request: object) -> object:
        raise AssertionError("contact mutation was dispatched")

    async def reconcile(_entity: int) -> bool:
        return True

    owner = ContactCommandOwner(
        no_write,
        reader,
        writer=writer,
        authority=authority,
        guard=ContactPeerGuard(),
        identity=gate.retained_auth_scope,
        resolve=lambda _entity: peer,
        acquire=acquire,
        reconcile=reconcile,
    )
    result = await owner.execute(
        {
            "entity_id": 43,
            "action": action,
            "first_name": first_name,
            "operation_key": "refused",
            "expected_version": "0" * 64 if stale else read_contact_command_state(reader, 42, 43)["expected_version"],
        }
    )
    assert result["ok"] is False
    if error is not None:
        assert result["error"] == error
    assert len(sink.frames) == preflight_frames
    assert reader.execute("SELECT count(*) FROM contact_operation_receipts").fetchone() == (0,)


@pytest.mark.parametrize("tamper", ["peer", "body", "phone", "note", "privacy", "vector", "method"])
async def test_contact_wire_rejects_substituted_native_request(  # noqa: PLR0915
    physical_note: tuple[object, ...], tamper: str
) -> None:
    gate = cast(TelegramRpcGate, physical_note[0])
    sender = cast(GuardedMTProtoSender, physical_note[1])
    connection = cast(GuardedConnectionTcpFull, physical_note[2])
    sink = cast(_Writer, physical_note[3])
    writer = cast(SQLiteWriter, physical_note[4])
    reader = cast(sqlite3.Connection, physical_note[5])
    authority = cast(ContactWriteAuthority, getattr(gate._native_write_domains["contact"].preclaim, "__self__"))  # noqa: B009
    fields = {
        "user.contact": True,
        "owner.saved_contact_name": {"first_name": "Before", "last_name": ""},
        "owner.saved_contact_phone_access": False,
    }

    def seed(db: sqlite3.Connection) -> None:
        with write_transaction(db):
            db.execute(
                "UPDATE entity_profile_semantic_snapshots SET fields_json=?,coverage_json=?,provenance_json=? "
                "WHERE owner_account_id=42 AND entity_id=43",
                (
                    json.dumps(fields),
                    json.dumps({k: {"state": "present", "authoritative": True} for k in fields}),
                    json.dumps({"fields": {k: {"order": 1} for k in fields}}),
                ),
            )

    await writer.run(seed)
    peer = types.InputUser(43, 55)

    async def call(request: object) -> object:
        if tamper == "method" and isinstance(request, functions.contacts.AddContactRequest):
            request = functions.contacts.UpdateContactNoteRequest(peer, types.TextWithEntities("unexpected", []))
        future = sender.send_owned(request, current_rpc_scope())
        state = await sender._send_queue.next_state()
        if isinstance(request, functions.contacts.AddContactRequest):
            if tamper == "peer":
                request.id = types.InputUser(44, 55)
            elif tamper == "body":
                request.first_name = "Substituted"
            elif tamper == "phone":
                request.phone = "999"
            elif tamper == "note":
                request.note = types.TextWithEntities("unexpected", [])
            elif tamper == "privacy":
                request.add_phone_privacy_exception = True
        elif isinstance(request, functions.contacts.DeleteContactsRequest) and tamper == "vector":
            request.id.append(types.InputUser(44, 55))
        try:
            await sender._prepare_state(state)
        except ContactWriteRejected:
            return await future
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        assert isinstance(request, functions.users.GetFullUserRequest)
        state.future.set_result(True)
        return await future

    async def acquire(_entity: int) -> ContactProfileAcquisition:
        await call(functions.users.GetFullUserRequest(peer))
        return ContactProfileAcquisition(True, 1)

    async def reconcile(_entity: int) -> bool:
        return True

    owner = ContactCommandOwner(
        call,
        reader,
        writer=writer,
        authority=authority,
        guard=ContactPeerGuard(),
        identity=gate.retained_auth_scope,
        resolve=lambda _entity: peer,
        acquire=acquire,
        reconcile=reconcile,
    )
    request = {
        "entity_id": 43,
        "action": "delete" if tamper == "vector" else "update",
        "operation_key": "tampered",
        "expected_version": read_contact_command_state(reader, 42, 43)["expected_version"],
    }
    if tamper != "vector":
        request["first_name"] = "New"
    if tamper == "method":
        with pytest.raises(NoteWriteRejected, match="native_write_context_invalid"):
            await owner.execute(request)
    else:
        result = await owner.execute(request)
        assert result["ok"] is False
    assert len(sink.frames) == 1
    assert reader.execute(
        "SELECT count(*) FROM contact_operation_receipts WHERE attempt_id IS NOT NULL"
    ).fetchone() == (0,)
