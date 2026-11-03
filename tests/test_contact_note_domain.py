"""Gateway ownership binds current capability and durable terminal evidence."""

import asyncio
from pathlib import Path

import pytest
from telethon.tl import types
from telethon.tl.functions.contacts import UpdateContactNoteRequest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.contact_notes.domain import NoteWriteAuthority
from mcp_telegram.contact_notes.intake import note_semantic_fingerprint
from mcp_telegram.contact_notes.receipts import reserve_operation
from mcp_telegram.entity_profile.contracts import UserReference
from mcp_telegram.native_write_fence import NativeWriteReceiptBinding
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema


async def test_domain_owns_current_capability_and_drains_terminal_receipt(tmp_path: Path) -> None:  # noqa: PLR0915
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path, capacity=1)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 2**64 - 1)
    reference = UserReference(7, 123)
    authority = NoteWriteAuthority(writer, lambda: scope, lambda _entity_id: reference)
    offered: list[int] = []

    async def reconcile(entity_id: int) -> bool:
        offered.append(entity_id)
        return True

    authority.bind_reconciliation(reconcile)
    domain = authority.domain
    note = types.TextWithEntities("note", [])
    binding = NativeWriteReceiptBinding(
        "contact_note", 42, 2, scope.auth_key_id, "key", "update", 7, note_semantic_fingerprint(note), 1
    )
    request = UpdateContactNoteRequest(types.InputUser(7, 123), note)
    try:
        assert await domain.verify_request(binding, request)
        assert not await domain.verify_request(binding, UpdateContactNoteRequest(types.InputUser(7, 124), note))
        await writer.run(lambda conn: reserve_operation(conn, binding, "a" * 64, 1, expected_version="v" * 64))
        await domain.bind_attempt(binding, "attempt")
        await writer._slots.acquire()
        task = asyncio.ensure_future(domain.record_wire_outcome(binding, "attempt", "unknown"))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        writer._slots.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await domain.validate_binding(binding, "attempt")
        await domain.record_wire_outcome(binding, "attempt", "acknowledged")
        assert await domain.validate_binding(binding, "attempt")

        await writer._slots.acquire()
        failed = asyncio.ensure_future(domain.record_wire_outcome(binding, "wrong-attempt", "unknown"))
        await asyncio.sleep(0)
        failed.cancel()
        await asyncio.sleep(0)
        writer._slots.release()
        with pytest.raises(ValueError, match="outcome mismatch"):
            await failed

        from dataclasses import replace

        other = replace(binding, operation_key="recover", target=8)
        await writer.run(lambda conn: reserve_operation(conn, other, "c" * 64, 2, expected_version="v" * 64))
        await domain.bind_attempt(other, "old-attempt")
        assert not await domain.recover_binding(other, "forged-attempt")
        assert await domain.recover_binding(other, "old-attempt")
        assert await domain.validate_binding(other, "old-attempt")
        await authority.notify_terminal(binding, "unknown")
        await authority.notify_terminal(binding, "rejected")
        assert offered == [7]
        scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 43, 2, 100)
        await authority.notify_terminal(binding, "acknowledged")
        assert offered == [7]
    finally:
        await writer.aclose()


async def test_authoritative_permit_fences_intake_versions_and_capability(tmp_path: Path) -> None:  # noqa: PLR0915
    import json
    import sqlite3
    from dataclasses import replace

    from mcp_telegram.contact_notes.domain import NoteWriteAuthority
    from mcp_telegram.contact_notes.intake import capture_contact_note_intake, invalidate_contact_note
    from mcp_telegram.contact_notes.state import read_note
    from mcp_telegram.image_authorization import ScopeCell
    from mcp_telegram.native_write_fence import NoteWriteRejected
    from mcp_telegram.sync_transactions import write_transaction

    path = tmp_path / "permit.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    reference = UserReference(7, 123)
    cell = ScopeCell()
    assert cell.publish(scope)
    authority = NoteWriteAuthority(
        writer, lambda: scope, lambda _entity_id: reference, capture_scope=cell.capture, claim_scope=cell.claim
    )
    binding = NativeWriteReceiptBinding("contact_note", 42, 2, 99, "key", "update", 7, "b" * 64, 1)

    def setup(conn: sqlite3.Connection) -> str:
        fields = {"full_user.note": None, "user.contact": True}
        coverage = {
            "full_user.note": {"state": "cleared", "authoritative": True},
            "user.contact": {"state": "present", "authoritative": True},
        }
        with write_transaction(conn):
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(7,'user',1)")
            conn.execute(
                "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,fields_json,coverage_json,source) VALUES(42,7,'test',?,?,'rpc')",
                (json.dumps(fields), json.dumps(coverage)),
            )
        version = read_note(conn, 42, 7)["expected_version"]
        assert isinstance(version, str)
        return version

    try:
        version = await writer.run(setup)
        await writer.run(lambda conn: reserve_operation(conn, binding, "a" * 64, 1, expected_version=version))
        with pytest.raises(NoteWriteRejected, match="permit"):
            await authority.domain.preclaim(binding)
        update = types.UpdateUser(user_id=7)
        with authority.watch(7) as watch:
            capture_contact_note_intake([update])
            with pytest.raises(NoteWriteRejected, match="intake"):
                watch.arm(binding)
        with authority.watch(7) as watch:
            invalidate_contact_note(7)  # the preflight's own accepted canonical publication
            watch.arm(binding)
            token = await authority.domain.preclaim(binding)
            assert token()
            capture_contact_note_intake([update])
            assert not token()
        with authority.watch(7) as watch:
            watch.arm(binding)

            def lose_contact(conn: sqlite3.Connection) -> None:
                with write_transaction(conn):
                    conn.execute(
                        "UPDATE entity_profile_semantic_snapshots SET fields_json=? WHERE owner_account_id=42 AND entity_id=7",
                        (json.dumps({"full_user.note": None, "user.contact": False}),),
                    )

            await writer.run(lose_contact)
            with pytest.raises(NoteWriteRejected, match="state_changed"):
                await authority.domain.preclaim(binding)

            def restore_contact(conn: sqlite3.Connection) -> None:
                with write_transaction(conn):
                    conn.execute(
                        "UPDATE entity_profile_semantic_snapshots SET fields_json=? WHERE owner_account_id=42 AND entity_id=7",
                        (json.dumps({"full_user.note": None, "user.contact": True}),),
                    )

            await writer.run(restore_contact)
            token = await authority.domain.preclaim(binding)
            assert token()
            reference = replace(reference, access_hash=124)
            assert not token()
        with authority.watch(7) as watch:
            watch.arm(binding)
            scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100)
            with pytest.raises(NoteWriteRejected, match="permit"):
                await authority.domain.preclaim(binding)
    finally:
        await writer.aclose()
