"""Command validation against the real local note snapshot and receipt store."""

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.contact_notes.commands import ContactNoteCommandOwner
from mcp_telegram.contact_notes.domain import NoteWriteAuthority
from mcp_telegram.contact_notes.state import note_observation_order, read_note
from mcp_telegram.contacts.guard import ContactPeerGuard
from mcp_telegram.entity_profile.contracts import CommandProfileAcquisition, UserReference
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, rpc_scope


async def request_reconciliation(_entity: int) -> bool:
    return True


async def test_stale_contact_note_never_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: PLR0914, PLR0915
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    scope = TelegramAuthScope(1, 1, 2, 3)
    cell = ScopeCell()
    assert cell.publish(scope)
    authority = NoteWriteAuthority(
        writer,
        lambda: scope,
        lambda entity: UserReference(entity, 42),
        capture_scope=cell.capture,
        claim_scope=cell.claim,
    )
    fields = {"full_user.note": None, "user.contact": True}
    coverage = {
        "full_user.note": {"state": "cleared", "authoritative": True},
        "user.contact": {"state": "present", "authoritative": True},
    }

    def populate(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (2,'user',0)")
            conn.execute(
                "INSERT INTO entity_profile_semantic_snapshots (owner_account_id,entity_id,normalization_version,fields_json,coverage_json,source,provenance_json) VALUES (?,?,?,?,?,?,?)",
                (
                    1,
                    2,
                    "v1",
                    json.dumps(fields),
                    json.dumps(coverage),
                    "rpc",
                    json.dumps({"fields": {"full_user.note": {"order": 1}, "user.contact": {"order": 1}}}),
                ),
            )

    await writer.run(populate)

    induce_intake = False
    rpc_calls: list[str] = []

    async def acquire(_entity: int) -> CommandProfileAcquisition:
        rpc_calls.append("profile")
        if induce_intake:
            from mcp_telegram.contact_notes.intake import capture_contact_note_intake

            class UpdateUser:
                user_id = 2

            capture_contact_note_intake([UpdateUser()])
        return CommandProfileAcquisition(True, True, note_observation_order(reader, 1, 2))

    async def config(_hash: int) -> tuple[int, int | None]:
        rpc_calls.append("config")
        return 1, 128

    async def client(_request: object) -> object:
        raise AssertionError("stale request dispatched")

    peer_guard = ContactPeerGuard()
    owner = ContactNoteCommandOwner(
        client,
        reader,
        writer=writer,
        authority=authority,
        identity=lambda: scope,
        resolve_input_user=lambda entity: types.InputUser(entity, 42),
        acquire_command_profile=acquire,
        acquire_config=config,
        request_reconciliation=request_reconciliation,
        peer_guard=peer_guard,
    )
    version = read_note(reader, 1, 2)["expected_version"]
    assert version is not None
    from mcp_telegram.contact_notes import commands

    lock = peer_guard.lock(scope.account_id, 2)
    await lock.acquire()
    with rpc_scope(TelegramRpcSource.NOTE_COMMAND, timeout_seconds=0.01):
        assert await owner.execute(
            {"dialog_id": 2, "action": "create", "expected_version": version, "operation_key": "queued", "text": "x"}
        ) == {"error": "command_timeout"}
    lock.release()
    assert rpc_calls == []
    result = await owner.execute(
        {"dialog_id": 2, "action": "create", "expected_version": "0" * 64, "operation_key": "stale", "text": "example"}
    )
    assert result == {"error": "version_conflict"}
    assert reader.execute("SELECT count(*) FROM contact_note_operation_receipts").fetchone()[0] == 0
    induce_intake = True
    stale_clear = {"dialog_id": 2, "action": "clear", "expected_version": version, "operation_key": "noop_race"}
    assert await owner.execute(stale_clear) == {"error": "note_intake_changed"}
    induce_intake = False
    clear = {"dialog_id": 2, "action": "clear", "expected_version": version, "operation_key": "noop"}
    assert await owner.execute(clear) == {"outcome": "unchanged", "verification": "verified", "verification_order": 1}
    assert await owner.execute(clear) == {
        "outcome": "unchanged",
        "verification": "verified",
        "verification_order": 1,
        "current_applicability": "unknown",
    }
    await lock.acquire()
    with rpc_scope(TelegramRpcSource.NOTE_COMMAND, timeout_seconds=0.01):
        conflict = {
            "dialog_id": 2,
            "action": "create",
            "expected_version": version,
            "operation_key": "noop",
            "text": "x",
        }
        assert await owner.execute(conflict) == {"error": "operation_conflict"}
    lock.release()
    original_run = writer.run

    async def timeout_after_commit[T](callback: Callable[[sqlite3.Connection], T]) -> T:
        result = await original_run(callback)
        if callback.__name__ == "reserve":
            await asyncio.Event().wait()
        return result

    timed = {"dialog_id": 2, "action": "clear", "expected_version": version, "operation_key": "timed"}
    with monkeypatch.context() as patch, rpc_scope(TelegramRpcSource.NOTE_COMMAND, timeout_seconds=0.2):
        patch.setattr(writer, "run", timeout_after_commit)
        assert await owner.execute(timed) == {
            "outcome": "unchanged",
            "verification": "verified",
            "verification_order": 1,
            "current_applicability": "unknown",
        }

    original_read = commands.read_note_command_state
    changes: list[int] = []

    def change_during_noop(conn: sqlite3.Connection, account_id: int, entity_id: int) -> dict[str, object]:
        state = original_read(conn, account_id, entity_id)
        if threading.current_thread().name == "sync-db-writer":
            changes.append(cell.begin_change())
        return state

    with monkeypatch.context() as patch:
        patch.setattr(commands, "read_note_command_state", change_during_noop)
        denied = {"dialog_id": 2, "action": "clear", "expected_version": version, "operation_key": "scope_race"}
        assert await owner.execute(denied) == {"error": "note_state_changed"}
    assert len(changes) == 1
    cell.end_change(changes.pop())
    assert cell.publish(scope)
    assert (
        reader.execute(
            "SELECT count(*) FROM contact_note_operation_receipts WHERE operation_key='scope_race'"
        ).fetchone()[0]
        == 0
    )

    def advance_order_in_writer(conn: sqlite3.Connection, account_id: int, entity_id: int) -> dict[str, object]:
        if threading.current_thread().name == "sync-db-writer":
            provenance = {"fields": {"full_user.note": {"order": 2}, "user.contact": {"order": 1}}}
            conn.execute(
                "UPDATE entity_profile_semantic_snapshots SET provenance_json=? WHERE owner_account_id=? AND entity_id=?",
                (json.dumps(provenance), account_id, entity_id),
            )
        return original_read(conn, account_id, entity_id)

    with monkeypatch.context() as patch:
        patch.setattr(commands, "read_note_command_state", advance_order_in_writer)
        denied = {"dialog_id": 2, "action": "clear", "expected_version": version, "operation_key": "order_race"}
        assert await owner.execute(denied) == {"error": "note_state_changed"}
    assert note_observation_order(reader, 1, 2) == 1

    async def interrupt_after_commit[T](callback: Callable[[sqlite3.Connection], T]) -> T:
        result = await original_run(callback)
        if callback.__name__ == "reserve":
            from mcp_telegram.contact_notes.intake import capture_contact_note_intake

            class UpdateUser:
                user_id = 2

            capture_contact_note_intake([UpdateUser()])
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(writer, "run", interrupt_after_commit)
    cancelled = {"dialog_id": 2, "action": "clear", "expected_version": version, "operation_key": "cancelled"}
    with pytest.raises(asyncio.CancelledError):
        await owner.execute(cancelled)
    monkeypatch.setattr(writer, "run", original_run)
    assert await owner.execute(cancelled) == {
        "outcome": "unchanged",
        "verification": "verified",
        "verification_order": 1,
        "current_applicability": "unknown",
    }

    async def deliver_after_commit[T](callback: Callable[[sqlite3.Connection], T]) -> T:
        result = await original_run(callback)
        if callback.__name__ == "reserve":
            from mcp_telegram.contact_notes.intake import capture_contact_note_intake

            class UpdateUser:
                user_id = 2

            capture_contact_note_intake([UpdateUser()])
        return result

    monkeypatch.setattr(writer, "run", deliver_after_commit)
    late = {"dialog_id": 2, "action": "clear", "expected_version": version, "operation_key": "late"}
    assert await owner.execute(late) == {
        "outcome": "unchanged",
        "verification": "verified",
        "verification_order": 1,
        "current_applicability": "unknown",
    }
    monkeypatch.setattr(writer, "run", original_run)
    assert await owner.execute(late) == {
        "outcome": "unchanged",
        "verification": "verified",
        "verification_order": 1,
        "current_applicability": "unknown",
    }
    assert (
        reader.execute(
            "SELECT verification_order FROM contact_note_operation_receipts WHERE operation_key='late'"
        ).fetchone()[0]
        == 1
    )
    induce_intake = True
    request = {"dialog_id": 2, "action": "create", "expected_version": version, "operation_key": "intake", "text": "x"}
    assert await owner.execute(request) == {"error": "note_intake_changed"}
    assert await owner.execute(request) == {"outcome": "rejected", "verification": "pending"}
    reader.close()
    await writer.aclose()


async def test_acknowledged_write_needs_fresh_canonical_readback(  # noqa: PLR0914, PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.contact_notes import receipts
    from mcp_telegram.native_write_fence import current_native_write_check

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    scope = TelegramAuthScope(1, 1, 2, 3)
    cell = ScopeCell()
    assert cell.publish(scope)
    authority = NoteWriteAuthority(
        writer,
        lambda: scope,
        lambda entity: UserReference(entity, 42),
        capture_scope=cell.capture,
        claim_scope=cell.claim,
    )
    coverage = {"full_user.note": {"state": "cleared", "authoritative": True}, "user.contact": {"authoritative": True}}
    fields = {"full_user.note": None, "user.contact": True}

    def populate(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (2,'user',0)")
            conn.execute(
                "INSERT INTO entity_profile_semantic_snapshots (owner_account_id,entity_id,normalization_version,fields_json,coverage_json,source,provenance_json) VALUES (?,?,?,?,?,?,?)",
                (
                    1,
                    2,
                    "v1",
                    json.dumps(fields),
                    json.dumps(coverage),
                    "rpc",
                    json.dumps({"fields": {"full_user.note": {"order": 1}, "user.contact": {"order": 1}}}),
                ),
            )

    await writer.run(populate)

    acquisition_count = 0

    async def acquire(_entity: int) -> CommandProfileAcquisition:
        nonlocal acquisition_count
        acquisition_count += 1
        if acquisition_count == 4:

            def publish(conn: sqlite3.Connection) -> None:
                value = {
                    "full_user.note": {"constructor": "TextWithEntities", "text": "new", "entities": []},
                    "user.contact": True,
                }
                updated_coverage = {
                    "full_user.note": {"state": "present", "authoritative": True},
                    "user.contact": {"authoritative": True},
                }
                with write_transaction(conn):
                    conn.execute(
                        "UPDATE entity_profile_semantic_snapshots SET fields_json=?,coverage_json=?,provenance_json=? WHERE owner_account_id=1 AND entity_id=2",
                        (
                            json.dumps(value),
                            json.dumps(updated_coverage),
                            json.dumps({"fields": {"full_user.note": {"order": 2}}}),
                        ),
                    )

            await writer.run(publish)
        if acquisition_count == 6:
            from mcp_telegram.contact_notes.intake import capture_contact_note_intake

            class UpdateUser:
                user_id = 2

            capture_contact_note_intake([UpdateUser()])
        if acquisition_count == 8:
            return CommandProfileAcquisition(True, True, 1)
        return CommandProfileAcquisition(True, True, note_observation_order(reader, 1, 2))

    reconciliation_offers: list[int] = []

    async def request_reconciliation(entity: int) -> bool:
        reconciliation_offers.append(entity)
        return True

    async def config(_hash: int) -> tuple[int, int | None]:
        return 1, 128

    async def client(request: object) -> object:
        check = current_native_write_check()
        assert check is not None
        assert (await check.revalidate())()
        assert (await authority.preclaim(check.binding))()
        from telethon.tl.functions.contacts import UpdateContactNoteRequest

        assert isinstance(request, UpdateContactNoteRequest)
        assert request.note.text in {"new", "changed"}
        await writer.run(lambda conn: receipts.bind_attempt(conn, check.binding, "attempt"))
        await writer.run(lambda conn: receipts.record_wire_outcome(conn, check.binding, "attempt", "acknowledged"))
        return True

    owner = ContactNoteCommandOwner(
        client,
        reader,
        writer=writer,
        authority=authority,
        identity=lambda: scope,
        resolve_input_user=lambda entity: types.InputUser(entity, 42),
        acquire_command_profile=acquire,
        acquire_config=config,
        request_reconciliation=request_reconciliation,
    )
    version = read_note(reader, 1, 2)["expected_version"]
    result = await owner.execute(
        {"dialog_id": 2, "action": "create", "expected_version": version, "operation_key": "op", "text": "new"}
    )
    assert result == {"outcome": "acknowledged", "verification": "mismatch", "verification_order": 1}
    assert await owner.execute(
        {"dialog_id": 2, "action": "create", "expected_version": version, "operation_key": "op", "text": "new"}
    ) == {**result, "current_applicability": "unknown"}
    assert await owner.execute(
        {"dialog_id": 2, "action": "create", "expected_version": version, "operation_key": "op2", "text": "new"}
    ) == {"outcome": "acknowledged", "verification": "verified", "verification_order": 2}
    assert acquisition_count == 4
    updated_version = read_note(reader, 1, 2)["expected_version"]
    assert await owner.execute(
        {
            "dialog_id": 2,
            "action": "update",
            "expected_version": updated_version,
            "operation_key": "op3",
            "text": "changed",
        }
    ) == {"outcome": "acknowledged", "verification": "unavailable"}
    assert await owner.execute(
        {
            "dialog_id": 2,
            "action": "update",
            "expected_version": updated_version,
            "operation_key": "op4",
            "text": "changed",
        }
    ) == {"outcome": "acknowledged", "verification": "unavailable"}
    original_run = writer.run

    async def deliver_after_verification[T](callback: Callable[[sqlite3.Connection], T]) -> T:
        result = await original_run(callback)
        if callback.__name__ == "update":
            from mcp_telegram.contact_notes.intake import capture_contact_note_intake

            class UpdateUser:
                user_id = 2

            capture_contact_note_intake([UpdateUser()])
        return result

    monkeypatch.setattr(writer, "run", deliver_after_verification)
    late = {
        "dialog_id": 2,
        "action": "update",
        "expected_version": updated_version,
        "operation_key": "op5",
        "text": "changed",
    }
    assert await owner.execute(late) == {
        "outcome": "acknowledged",
        "verification": "mismatch",
        "verification_order": 2,
        "current_applicability": "unknown",
    }
    monkeypatch.setattr(writer, "run", original_run)
    assert await owner.execute(late) == {
        "outcome": "acknowledged",
        "verification": "mismatch",
        "verification_order": 2,
        "current_applicability": "unknown",
    }
    assert (
        reader.execute(
            "SELECT verification_order FROM contact_note_operation_receipts WHERE operation_key='op5'"
        ).fetchone()[0]
        == 2
    )
    from mcp_telegram.contact_notes import commands

    original_order = commands.note_observation_order
    changes: list[int] = []

    def change_during_verification(conn: sqlite3.Connection, account_id: int, entity_id: int) -> int | None:
        order = original_order(conn, account_id, entity_id)
        if threading.current_thread().name == "sync-db-writer":
            changes.append(cell.begin_change())
        return order

    with monkeypatch.context() as patch:
        patch.setattr(commands, "note_observation_order", change_during_verification)
        contested = {
            "dialog_id": 2,
            "action": "update",
            "expected_version": updated_version,
            "operation_key": "op6",
            "text": "changed",
        }
        assert await owner.execute(contested) == {"outcome": "acknowledged", "verification": "unavailable"}
    assert len(changes) == 1
    cell.end_change(changes.pop())
    assert cell.publish(scope)
    assert (
        reader.execute(
            "SELECT verification_order FROM contact_note_operation_receipts WHERE operation_key='op6'"
        ).fetchone()[0]
        is None
    )
    assert reconciliation_offers == [2, 2, 2]

    def publish_background(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute(
                "UPDATE contact_note_operation_receipts SET verification='verified',verification_order=777 "
                "WHERE operation_key='op7' AND verification='pending'"
            )

    async def race_background[T](callback: Callable[[sqlite3.Connection], T]) -> T:
        if callback.__name__ == "update":
            await original_run(publish_background)
        return await original_run(callback)

    with monkeypatch.context() as patch:
        patch.setattr(writer, "run", race_background)
        racing = {
            "dialog_id": 2,
            "action": "update",
            "expected_version": updated_version,
            "operation_key": "op7",
            "text": "changed",
        }
        assert await owner.execute(racing) == {
            "outcome": "acknowledged",
            "verification": "verified",
            "verification_order": 777,
            "current_applicability": "unknown",
        }
    row = cast(
        tuple[str, int] | None,
        reader.execute(
            "SELECT verification,verification_order FROM contact_note_operation_receipts WHERE operation_key='op7'"
        ).fetchone(),
    )
    assert row is not None and tuple(row) == ("verified", 777)
    reader.close()
    await writer.aclose()


async def test_phase_budget_prevents_preflight_retry_and_restores_inherited_limit(tmp_path: Path) -> None:
    import pytest

    from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptBudgetExhaustedError, demand_context
    from mcp_telegram.telegram_rpc_consumers import DemandKind
    from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope, rpc_attempt_budget

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    scope = TelegramAuthScope(1, 1, 2, 3)
    cell = ScopeCell()
    assert cell.publish(scope)
    authority = NoteWriteAuthority(
        writer,
        lambda: scope,
        lambda entity: UserReference(entity, 42),
        capture_scope=cell.capture,
        claim_scope=cell.claim,
    )

    async def config(_hash: int) -> tuple[int, int | None]:
        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        budget.debit()
        return 1, 128

    async def acquire(_entity: int) -> CommandProfileAcquisition:
        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        budget.debit()
        budget.debit()
        return CommandProfileAcquisition(True, True, note_observation_order(reader, 1, 2))

    async def client(_request: object) -> object:
        raise AssertionError("unexpected mutation")

    owner = ContactNoteCommandOwner(
        client,
        reader,
        writer=writer,
        authority=authority,
        identity=lambda: scope,
        resolve_input_user=lambda entity: types.InputUser(entity, 42),
        acquire_command_profile=acquire,
        acquire_config=config,
        request_reconciliation=request_reconciliation,
    )
    budget = RpcAttemptBudget(6, attempts=1)
    with demand_context(DemandKind.NOTE_COMMAND), rpc_attempt_budget(budget):
        with pytest.raises(RpcAttemptBudgetExhaustedError):
            await owner.execute(
                {
                    "dialog_id": 2,
                    "action": "create",
                    "expected_version": "0" * 64,
                    "operation_key": "phase",
                    "text": "x",
                }
            )
    assert budget.attempts == 3
    assert budget.limit == 6
    assert reader.execute("SELECT count(*) FROM contact_note_operation_receipts").fetchone()[0] == 0
    reader.close()
    await writer.aclose()


async def test_unclaimed_rejection_and_unknown_quarantine_never_replay(tmp_path: Path) -> None:  # noqa: PLR0915
    from mcp_telegram.contact_notes import receipts
    from mcp_telegram.native_write_fence import NoteWriteRejected, current_native_write_check

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    scope = TelegramAuthScope(1, 1, 2, 3)
    cell = ScopeCell()
    assert cell.publish(scope)
    authority = NoteWriteAuthority(
        writer,
        lambda: scope,
        lambda entity: UserReference(entity, 42),
        capture_scope=cell.capture,
        claim_scope=cell.claim,
    )
    fields = {"full_user.note": None, "user.contact": True}
    coverage = {"full_user.note": {"state": "cleared", "authoritative": True}, "user.contact": {"authoritative": True}}

    def populate(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (2,'user',0)")
            conn.execute(
                "INSERT INTO entity_profile_semantic_snapshots (owner_account_id,entity_id,normalization_version,fields_json,coverage_json,source,provenance_json) VALUES (?,?,?,?,?,?,?)",
                (
                    1,
                    2,
                    "v1",
                    json.dumps(fields),
                    json.dumps(coverage),
                    "rpc",
                    json.dumps({"fields": {"full_user.note": {"order": 1}, "user.contact": {"order": 1}}}),
                ),
            )

    await writer.run(populate)
    calls = []

    async def acquire(_entity: int) -> CommandProfileAcquisition:
        calls.append("acquire")
        return CommandProfileAcquisition(True, True, note_observation_order(reader, 1, 2))

    async def request_reconciliation(_entity: int) -> bool:
        calls.append("reconcile")
        return True

    async def config(_hash: int) -> tuple[int, int | None]:
        return 1, 128

    async def client(_request: object) -> object:
        check = current_native_write_check()
        assert check is not None
        calls.append("mutation")
        if calls.count("mutation") == 1:
            from mcp_telegram.contact_notes.intake import invalidate_contact_note

            invalidate_contact_note(2)
            assert not (await check.revalidate())()
            assert not (await authority.preclaim(check.binding))()
            raise NoteWriteRejected("stale")
        await writer.run(lambda conn: receipts.bind_attempt(conn, check.binding, "attempt"))
        await writer.run(lambda conn: receipts.record_wire_outcome(conn, check.binding, "attempt", "unknown"))
        raise asyncio.CancelledError

    owner = ContactNoteCommandOwner(
        client,
        reader,
        writer=writer,
        authority=authority,
        identity=lambda: scope,
        resolve_input_user=lambda entity: types.InputUser(entity, 42),
        acquire_command_profile=acquire,
        acquire_config=config,
        request_reconciliation=request_reconciliation,
    )
    version = read_note(reader, 1, 2)["expected_version"]
    request = {"dialog_id": 2, "action": "create", "expected_version": version, "operation_key": "one", "text": "x"}
    assert await owner.execute(request) == {"error": "stale"}
    assert await owner.execute(request) == {"outcome": "rejected", "verification": "pending"}
    request["operation_key"] = "two"
    with pytest.raises(asyncio.CancelledError):
        await owner.execute(request)
    assert calls.count("reconcile") == 1
    request["operation_key"] = "three"
    assert await owner.execute(request) == {"error": "note_outcome_unresolved"}
    assert calls.count("mutation") == 2
    request["operation_key"] = "two"
    scope = TelegramAuthScope(1, 1, 4, 5)
    before = calls.count("acquire")
    assert await owner.execute(request) == {"outcome": "unknown", "verification": "pending"}
    assert calls.count("acquire") == before
    reader.close()
    await writer.aclose()
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    cell = ScopeCell()
    assert cell.publish(scope)
    authority = NoteWriteAuthority(
        writer,
        lambda: scope,
        lambda entity: UserReference(entity, 42),
        capture_scope=cell.capture,
        claim_scope=cell.claim,
    )
    restarted = ContactNoteCommandOwner(
        client,
        reader,
        writer=writer,
        authority=authority,
        identity=lambda: scope,
        resolve_input_user=lambda entity: types.InputUser(entity, 42),
        acquire_command_profile=acquire,
        acquire_config=config,
        request_reconciliation=request_reconciliation,
    )
    assert await restarted.execute(request) == {"outcome": "unknown", "verification": "pending"}
    assert await restarted.execute(request) == {"outcome": "unknown", "verification": "pending"}
    assert calls.count("acquire") == before
    reader.close()
    await writer.aclose()
