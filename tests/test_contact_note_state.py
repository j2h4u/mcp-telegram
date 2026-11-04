"""Account-bound note versions use exact retained semantic facts."""

# pyright: reportAny=false
import json
import sqlite3
from pathlib import Path

from mcp_telegram.contact_notes.state import read_note
from mcp_telegram.contacts.schema import CONTACT_RECEIPT_DDL


def _refine_unknown(conn: sqlite3.Connection, binding: object) -> bool:
    from mcp_telegram.contact_notes.receipts import refine_unknown_for_observation
    from mcp_telegram.contact_notes.state import note_observation_order
    from mcp_telegram.native_write_fence import NativeWriteReceiptBinding
    from mcp_telegram.sync_transactions import write_transaction

    assert isinstance(binding, NativeWriteReceiptBinding)
    order = note_observation_order(conn, binding.account_id, binding.target)
    if order is None:
        return False
    with write_transaction(conn):
        return bool(refine_unknown_for_observation(conn, binding.account_id, binding.target, order, lambda: True))


def test_note_version_distinguishes_unknown_absent_and_exact_rich_value() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entity_profile_semantic_snapshots (owner_account_id,entity_id,fields_json,coverage_json)"
    )
    assert read_note(conn, 1, 2) == {"state": "unknown", "expected_version": None}
    coverage = {"full_user.note": {"state": "cleared", "authoritative": True}}
    conn.execute(
        "INSERT INTO entity_profile_semantic_snapshots VALUES (?,?,?,?)",
        (1, 2, json.dumps({"full_user.note": None}), json.dumps(coverage)),
    )
    absent = read_note(conn, 1, 2)
    assert absent["state"] == "absent" and isinstance(absent["expected_version"], str)
    note = {
        "constructor": "TextWithEntities",
        "text": " 💬 note\n",
        "entities": [{"constructor": "MessageEntityBold", "offset": 1, "length": 2}],
    }
    coverage["full_user.note"]["state"] = "present"
    conn.execute(
        "UPDATE entity_profile_semantic_snapshots SET fields_json=?,coverage_json=?",
        (json.dumps({"full_user.note": note, "user.phone": "1"}), json.dumps(coverage)),
    )
    retained = read_note(conn, 1, 2)
    assert retained["state"] == "retained" and retained["expected_version"] != absent["expected_version"]
    conn.execute(
        "UPDATE entity_profile_semantic_snapshots SET fields_json=?",
        (json.dumps({"full_user.note": note, "user.phone": "2"}),),
    )
    assert read_note(conn, 1, 2) == retained
    assert read_note(conn, 3, 2)["state"] == "unknown"
    coverage["full_user.note"]["authoritative"] = False
    conn.execute("UPDATE entity_profile_semantic_snapshots SET coverage_json=?", (json.dumps(coverage),))
    assert read_note(conn, 1, 2)["state"] == "unknown"
    conn.close()


def test_intake_fences_only_matching_active_peer_and_scope() -> None:
    from types import SimpleNamespace

    from mcp_telegram.contact_notes.intake import (
        capture_contact_note_intake,
        invalidate_contact_note_scope,
        is_current,
        track_contact_note,
    )

    class UpdateUser:
        user_id: int

    with track_contact_note(2) as first, track_contact_note(3) as second:
        capture_contact_note_intake([SimpleNamespace(user_id=2)])
        assert is_current(2, first)
        update = UpdateUser()
        update.user_id = 2
        capture_contact_note_intake([update])
        assert not is_current(2, first) and is_current(3, second)
        invalidate_contact_note_scope()
        assert not is_current(3, second)
    assert not is_current(2, first)


async def test_command_observation_keeps_refresh_cursor_and_publishes_note(tmp_path: Path) -> None:  # noqa: PLR0914, PLR0915
    from dataclasses import replace
    from types import SimpleNamespace

    from telethon.tl import types

    from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
    from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
    from mcp_telegram.entity_profile.contracts import UserReference
    from mcp_telegram.entity_profile.repository import EntityProfileRepository
    from mcp_telegram.image_authorization import ScopeCell
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
    from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
    from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient
    from tests.test_profile_collection_pages import _Client

    class LocalGateway(TelethonUserProfileGateway):
        def get_user_reference(self, user_id: int, *, is_self: bool = False) -> UserReference | None:
            return UserReference(user_id, 123, is_self)

    path = tmp_path / "profile.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="name", contact=True)],
        chats=[],
        full_user=types.UserFull(
            id=7,
            settings=types.PeerSettings(),
            notify_settings=types.PeerNotifySettings(),
            common_chats_count=0,
            note=types.TextWithEntities(" exact ", []),
        ),
    )
    service = _pair_service(reader, _PairClient(), enabled=True)
    assert isinstance(service, DaemonEntityInfoService)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    service._deps = replace(
        service._deps,
        client=SimpleNamespace(capture_image_scope=cell.capture, claim_image_scope=cell.claim),
        writer=writer,
        user_profile_port=LocalGateway(_Client(response, response), now_provider=lambda: 100),
    )

    def prepare(conn: sqlite3.Connection) -> None:
        repo = EntityProfileRepository(conn, section_ttl_seconds=300)
        repo.save_core({"id": 7, "type": "user", "name": "old"}, now=100)
        repo.mark_pending(7, now=100, pair_eligible_override=True)

    try:
        await writer.run(prepare)
        before = reader.execute(
            "SELECT next_section,acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=7"
        ).fetchone()
        old_cursor = service._profiles.next_due_refresh(now=100, entity_id=7)
        assert old_cursor is not None
        from mcp_telegram.contact_notes.intake import is_current, track_contact_note

        with track_contact_note(7) as marker:
            acquisition = await service.acquire_command_profile(7)
            assert acquisition.accepted and acquisition.observation_order is not None
            assert not is_current(7, marker)
        from mcp_telegram.contact_notes.state import note_observation_order, read_note_command_state

        assert read_note_command_state(reader, 42, 7)["observation_order"] == acquisition.observation_order
        assert note_observation_order(reader, 42, 7) == acquisition.observation_order
        new_cursor = service._profiles.next_due_refresh(now=100, entity_id=7)
        assert new_cursor is not None and new_cursor.profile_revision == old_cursor.profile_revision + 1
        after = reader.execute(
            "SELECT next_section,acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=7"
        ).fetchone()
        assert tuple(after) == tuple(before)
        original_note = read_note(reader, 42, 7)
        assert original_note["state"] == "retained"

        from mcp_telegram.contact_notes.receipts import (
            bind_attempt,
            record_wire_outcome,
            reserve_operation,
        )
        from mcp_telegram.native_write_fence import NativeWriteReceiptBinding

        binding = NativeWriteReceiptBinding("contact_note", 42, 2, 99, "unknown-key", "update", 7, "b" * 64, 1)
        await writer.run(lambda conn: reserve_operation(conn, binding, "a" * 64, 100, expected_version="v" * 64))
        await writer.run(lambda conn: bind_attempt(conn, binding, "attempt"))
        await writer.run(lambda conn: record_wire_outcome(conn, binding, "attempt", "unknown"))
        assert not await writer.run(lambda conn: _refine_unknown(conn, binding))
        assert (await service.acquire_command_profile(7)).accepted
        assert await writer.run(lambda conn: _refine_unknown(conn, binding))
        assert read_note(reader, 42, 7) == original_note

        response.users[0].contact = False
        service._deps = replace(
            service._deps, user_profile_port=LocalGateway(_Client(response), now_provider=lambda: 100)
        )
        noncontact = await service.acquire_command_profile(7)
        assert noncontact.accepted and not noncontact.contact_current
        response.users[0].contact = True
        service._deps = replace(
            service._deps, user_profile_port=LocalGateway(_Client(response), now_provider=lambda: 100)
        )
        restored = await service.acquire_command_profile(7)
        assert restored.accepted and restored.contact_current

        class ContestedClient(_Client):
            async def __call__(self, request: object) -> object:
                await writer.run(
                    lambda conn: EntityProfileRepository(conn, section_ttl_seconds=300).save_core(
                        {"id": 7, "type": "user", "name": "newer"}, now=101
                    )
                )
                return await super().__call__(request)

        response.full_user.note = types.TextWithEntities("stale", [])
        service._deps = replace(
            service._deps,
            user_profile_port=LocalGateway(ContestedClient(response), now_provider=lambda: 100),
        )
        assert not (await service.acquire_command_profile(7)).accepted
        assert read_note(reader, 42, 7) == original_note

        from unittest.mock import patch

        import pytest

        service._deps = replace(
            service._deps,
            user_profile_port=LocalGateway(_Client(response), now_provider=lambda: 100),
        )
        revision = reader.execute("SELECT profile_revision FROM entity_details WHERE entity_id=7").fetchone()[0]
        with patch.object(EntityProfileRepository, "_store_semantic_commit", side_effect=RuntimeError("storage")):
            with pytest.raises(RuntimeError, match="storage"):
                await service.acquire_command_profile(7)
        assert reader.execute("SELECT profile_revision FROM entity_details WHERE entity_id=7").fetchone()[0] == revision
        assert read_note(reader, 42, 7) == original_note
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()


async def test_config_limit_hash_and_not_modified() -> None:
    from telethon.tl import types
    from telethon.tl.functions.help import GetAppConfigRequest

    from mcp_telegram.telegram_gateway import TelethonUserProfileGateway

    class Client:
        async def __call__(self, request: object) -> object:
            assert isinstance(request, GetAppConfigRequest)
            if request.hash:
                return types.help.AppConfigNotModified()
            return types.help.AppConfig(
                99, types.JsonObject([types.JsonObjectValue("contact_note_length_limit", types.JsonNumber(256.0))])
            )

    gateway = TelethonUserProfileGateway(Client())
    assert await gateway.fetch_contact_note_limit() == (99, 256)
    assert await gateway.fetch_contact_note_limit(99) == (99, None)


def test_note_wire_binding_rejects_changed_peer_body_and_domain() -> None:
    from telethon.tl import types
    from telethon.tl.functions.contacts import UpdateContactNoteRequest

    from mcp_telegram.contact_notes.intake import note_semantic_fingerprint, validate_note_wire_request
    from mcp_telegram.native_write_fence import NativeWriteReceiptBinding

    note = types.TextWithEntities("💬", [])
    request = UpdateContactNoteRequest(types.InputUser(7, 123), note)
    binding = NativeWriteReceiptBinding(
        "contact_note", 42, 2, 99, "key", "update", 7, note_semantic_fingerprint(note), 1
    )
    assert validate_note_wire_request(binding, request)
    assert not validate_note_wire_request(binding, UpdateContactNoteRequest(types.InputUser(8, 123), note))
    assert not validate_note_wire_request(
        binding, UpdateContactNoteRequest(types.InputUser(7, 123), types.TextWithEntities("other", []))
    )
    from dataclasses import replace

    assert not validate_note_wire_request(replace(binding, domain="folder"), request)
    rich_note = types.TextWithEntities("💬", [types.MessageEntityBold(0, 2)])
    assert not validate_note_wire_request(binding, UpdateContactNoteRequest(types.InputUser(7, 123), rich_note))


def test_receipt_terminal_binding_is_immutable() -> None:
    from dataclasses import replace

    import pytest

    from mcp_telegram.contact_notes.receipts import (
        CONTACT_NOTE_RECEIPT_DDL,
        bind_attempt,
        record_wire_outcome,
        reserve_operation,
        validate_binding,
    )
    from mcp_telegram.native_write_fence import NativeWriteReceiptBinding

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(CONTACT_NOTE_RECEIPT_DDL)
        conn.execute(CONTACT_RECEIPT_DDL)
        conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE entity_profile_semantic_snapshots (owner_account_id,entity_id,provenance_json,coverage_json)"
        )
        conn.execute(
            "INSERT INTO contact_note_operation_receipts "
            "(account_id,dc_id,key_id,operation_key,request_fingerprint,semantic_fingerprint,expected_version,action,entity_id,outcome,created_at,updated_at) "
            "VALUES (42,2,99,'key',?,?,?,'update',7,'pending',1,1)",
            ("a" * 64, "b" * 64, "v" * 64),
        )
        conn.commit()
        binding = NativeWriteReceiptBinding("contact_note", 42, 2, 99, "key", "update", 7, "b" * 64, 1)
        bind_attempt(conn, binding, "attempt")
        assert not validate_binding(conn, binding, "attempt")
        record_wire_outcome(conn, binding, "attempt", "unknown")
        record_wire_outcome(conn, binding, "attempt", "acknowledged")
        assert validate_binding(conn, binding, "attempt")
        assert not validate_binding(conn, replace(binding, target=8), "attempt")
        assert conn.execute(
            "SELECT outcome,binding_revision,terminal_revision FROM contact_note_operation_receipts"
        ).fetchone() == ("unknown", 1, 1)
        assert not _refine_unknown(conn, binding)
        coverage = {"full_user.note": {"authoritative": True}, "user.contact": {"authoritative": True}}
        provenance = {"fields": {"full_user.note": {"order": 1}, "user.contact": {"order": 1}}}
        conn.execute(
            "INSERT INTO entity_profile_semantic_snapshots VALUES (42,7,?,?)",
            (json.dumps(provenance), json.dumps(coverage)),
        )
        conn.commit()
        assert not _refine_unknown(conn, binding)
        provenance["fields"]["full_user.note"]["order"] = 2
        provenance["fields"]["user.contact"]["order"] = 2
        conn.execute("UPDATE entity_profile_semantic_snapshots SET provenance_json=?", (json.dumps(provenance),))
        conn.commit()
        assert _refine_unknown(conn, binding)
        assert conn.execute("SELECT outcome,verification FROM contact_note_operation_receipts").fetchone() == (
            "unknown",
            "observed_after_unknown",
        )
        assert reserve_operation(
            conn, replace(binding, operation_key="new"), "c" * 64, 2, expected_version="v" * 64
        ) == (True, "pending")
        with pytest.raises(ValueError):
            bind_attempt(conn, binding, "other")
        conn.execute(
            "UPDATE contact_note_operation_receipts SET outcome='pending',attempt_id=NULL,terminal_revision=0 WHERE operation_key='key'"
        )
        conn.commit()
        bind_attempt(conn, binding, "rejected")
        record_wire_outcome(conn, binding, "rejected", "rejected")
        assert validate_binding(conn, binding, "rejected")
        assert conn.execute(
            "SELECT outcome FROM contact_note_operation_receipts WHERE operation_key='key'"
        ).fetchone() == ("rejected",)
    finally:
        conn.close()


def test_note_composition_refuses_loss_and_counts_unicode_codepoints() -> None:
    import pytest

    from mcp_telegram.contact_notes.composition import NoteIntent, compose_note

    rich = {"text": "a", "entities": [{"constructor": "MessageEntityBold", "offset": 0, "length": 1}]}
    assert compose_note(NoteIntent("update", "a", False), "retained", rich, 1) is None
    with pytest.raises(ValueError, match="formatting"):
        compose_note(NoteIntent("update", "💬", False), "retained", rich, 1)
    value = compose_note(NoteIntent("update", "💬", True), "retained", rich, 1)
    assert value is not None and value.text == "💬" and value.entities == []
    assert compose_note(NoteIntent("clear", None, False), "absent", None, 1) is None
    with pytest.raises(ValueError, match="unknown"):
        compose_note(NoteIntent("create", "a", False), "unknown", None, 1)


def test_receipt_reservation_blocks_replay_and_unresolved_peer() -> None:
    from dataclasses import replace

    import pytest

    from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
    from mcp_telegram.contact_notes.receipts import (
        CONTACT_NOTE_RECEIPT_DDL,
        recover_interrupted_operations,
        reserve_operation,
    )
    from mcp_telegram.native_write_fence import NativeWriteReceiptBinding

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(CONTACT_NOTE_RECEIPT_DDL)
        conn.execute(CONTACT_RECEIPT_DDL)
        conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
        binding = NativeWriteReceiptBinding("contact_note", 42, 2, 2**64 - 1, "key", "update", 7, "b" * 64, 1)
        assert reserve_operation(conn, binding, "a" * 64, 1, expected_version="v" * 64) == (True, "pending")
        assert reserve_operation(conn, binding, "a" * 64, 2, expected_version="v" * 64) == (False, "pending")
        with pytest.raises(ValueError, match="operation_conflict"):
            reserve_operation(conn, binding, "c" * 64, 2, expected_version="v" * 64)
        with pytest.raises(ValueError, match="note_outcome_unresolved"):
            reserve_operation(conn, replace(binding, operation_key="other"), "a" * 64, 2, expected_version="v" * 64)
        with pytest.raises(ValueError, match="operation_conflict"):
            reserve_operation(conn, replace(binding, account_id=43), "a" * 64, 2, expected_version="v" * 64)
        assert reserve_operation(
            conn,
            replace(binding, operation_key="noop", target=8),
            "d" * 64,
            2,
            expected_version="v" * 64,
            unchanged=True,
            no_op_check=lambda _conn: True,
            no_op_order=1,
        ) == (True, "unchanged")
        assert conn.execute(
            "SELECT outcome,verification FROM contact_note_operation_receipts WHERE operation_key='noop'"
        ).fetchone() == ("unchanged", "verified")
        foreign = replace(binding, account_id=43, operation_key="foreign", target=9)
        assert reserve_operation(conn, foreign, "e" * 64, 2, expected_version="v" * 64) == (True, "pending")
        scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 2**64 - 1)
        assert recover_interrupted_operations(conn, scope) == 1
        assert conn.execute(
            "SELECT outcome FROM contact_note_operation_receipts WHERE operation_key='foreign'"
        ).fetchone() == ("pending",)
        assert reserve_operation(conn, binding, "a" * 64, 3, expected_version="v" * 64) == (False, "unknown")
        with pytest.raises(ValueError, match="note_outcome_unresolved"):
            reserve_operation(conn, replace(binding, operation_key="other"), "a" * 64, 3, expected_version="v" * 64)
    finally:
        conn.close()


def test_restart_unknown_without_attempt_needs_fresh_authoritative_account_evidence() -> None:
    from dataclasses import replace

    from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
    from mcp_telegram.contact_notes.receipts import (
        CONTACT_NOTE_RECEIPT_DDL,
        recover_interrupted_operations,
        reserve_operation,
        validate_binding,
    )
    from mcp_telegram.native_write_fence import NativeWriteReceiptBinding

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(CONTACT_NOTE_RECEIPT_DDL)
        conn.execute(CONTACT_RECEIPT_DDL)
        conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE entity_profile_semantic_snapshots (owner_account_id,entity_id,provenance_json,coverage_json)"
        )
        binding = NativeWriteReceiptBinding("contact_note", 42, 2, 99, "key", "update", 7, "b" * 64, 1)
        assert reserve_operation(conn, binding, "a" * 64, 1, expected_version="v" * 64) == (True, "pending")
        scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
        assert recover_interrupted_operations(conn, scope) == 1
        coverage = {"full_user.note": {"authoritative": True}, "user.contact": {"authoritative": True}}
        provenance = {"fields": {"full_user.note": {"order": 1}, "user.contact": {"order": 1}}}
        conn.execute(
            "INSERT INTO entity_profile_semantic_snapshots VALUES (43,7,?,?)",
            (json.dumps(provenance), json.dumps(coverage)),
        )
        conn.commit()
        assert not _refine_unknown(conn, binding)
        provenance["fields"]["full_user.note"]["order"] = 2
        conn.execute(
            "UPDATE entity_profile_semantic_snapshots SET owner_account_id=42,provenance_json=?",
            (json.dumps(provenance),),
        )
        conn.commit()
        assert not _refine_unknown(conn, binding)
        provenance["fields"]["user.contact"]["order"] = 2
        coverage["user.contact"]["authoritative"] = False
        conn.execute(
            "UPDATE entity_profile_semantic_snapshots SET provenance_json=?,coverage_json=?",
            (json.dumps(provenance), json.dumps(coverage)),
        )
        conn.commit()
        assert not _refine_unknown(conn, binding)
        coverage["user.contact"]["authoritative"] = True
        conn.execute("UPDATE entity_profile_semantic_snapshots SET coverage_json=?", (json.dumps(coverage),))
        conn.commit()
        assert _refine_unknown(conn, binding)
        assert not validate_binding(conn, binding, "no-attempt")
        assert reserve_operation(conn, binding, "a" * 64, 2, expected_version="v" * 64) == (False, "unknown")
        assert reserve_operation(
            conn, replace(binding, operation_key="new"), "c" * 64, 2, expected_version="v" * 64
        ) == (True, "pending")
    finally:
        conn.close()


async def test_config_method_unavailable_uses_retained_hash_without_swallowing_holds() -> None:
    import asyncio

    import pytest
    from telethon.errors import FloodWaitError, MethodInvalidError, UnauthorizedError
    from telethon.tl.functions.help import GetAppConfigRequest

    from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
    from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred

    request = GetAppConfigRequest(hash=0)

    class FailingClient:
        def __init__(self, error: BaseException) -> None:
            self.error = error

        async def __call__(self, _request: object) -> object:
            raise self.error

    unavailable = TelethonUserProfileGateway(FailingClient(MethodInvalidError(request)))
    assert await unavailable.fetch_contact_note_limit(77) == (77, None)
    errors = [
        asyncio.CancelledError(),
        FloodWaitError(request, 10),
        UnauthorizedError(request, "auth"),
        TelegramRpcAdmissionDeferred(10),
        sqlite3.OperationalError("storage"),
    ]
    for error in errors:
        gateway = TelethonUserProfileGateway(FailingClient(error))
        with pytest.raises(type(error)):
            await gateway.fetch_contact_note_limit()
