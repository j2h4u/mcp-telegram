"""Recovery boundaries for native contact commands through durable SQLite."""

# pyright: reportAny=false
import asyncio
import json
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.contact_notes.domain import NoteWriteAuthority
from mcp_telegram.contacts import receipts
from mcp_telegram.contacts.commands import ContactCommandOwner
from mcp_telegram.contacts.contracts import ContactProfileAcquisition
from mcp_telegram.contacts.domain import ContactWriteAuthority
from mcp_telegram.contacts.guard import ContactPeerGuard
from mcp_telegram.contacts.state import read_contact_command_state
from mcp_telegram.entity_profile.contracts import UserReference
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.native_write_fence import current_native_write_check
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction


@dataclass
class _Fixture:
    writer: SQLiteWriter
    reader: sqlite3.Connection
    scope: TelegramAuthScope
    cell: ScopeCell
    authority: ContactWriteAuthority
    note_authority: NoteWriteAuthority


@asynccontextmanager
async def _fixture(path: Path) -> AsyncIterator[_Fixture]:
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    scope = TelegramAuthScope(1, 1, 2, 3)
    cell = ScopeCell()
    assert cell.publish(scope)

    def ref(entity: int) -> UserReference:
        return UserReference(entity, 42)

    authority = ContactWriteAuthority(writer, lambda: scope, ref, capture_scope=cell.capture, claim_scope=cell.claim)
    note_authority = NoteWriteAuthority(writer, lambda: scope, ref, capture_scope=cell.capture, claim_scope=cell.claim)
    fields = {
        "user.contact": True,
        "owner.saved_contact_name": {"first_name": "Saved", "last_name": ""},
        "owner.saved_contact_phone_access": True,
        "user.phone": "123",
    }
    evidence = {key: {"state": "present", "authoritative": True} for key in fields}
    provenance = {"fields": {key: {"order": 1} for key in fields}}

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(2,'user',0)")
            conn.execute(
                "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,fields_json,coverage_json,provenance_json,source) VALUES(1,2,?,?,?,?,?)",
                ("v1", json.dumps(fields), json.dumps(evidence), json.dumps(provenance), "rpc"),
            )

    await writer.run(seed)
    try:
        yield _Fixture(writer, reader, scope, cell, authority, note_authority)
    finally:
        reader.close()
        await writer.aclose()


def _request(reader: sqlite3.Connection, key: str) -> dict[str, object]:
    version = read_contact_command_state(reader, 1, 2)["expected_version"]
    assert version is not None
    return {"entity_id": 2, "action": "delete", "operation_key": key, "expected_version": version}


def _owner(
    fixture: _Fixture,
    client: Callable[[object], Awaitable[object]],
    acquire: Callable[[int], Awaitable[ContactProfileAcquisition]],
    reconcile: Callable[[int], Awaitable[bool]],
) -> ContactCommandOwner:
    return ContactCommandOwner(
        client,
        fixture.reader,
        writer=fixture.writer,
        authority=fixture.authority,
        guard=ContactPeerGuard(),
        identity=lambda: fixture.scope,
        resolve=lambda entity: types.InputUser(entity, 42),
        acquire=acquire,
        reconcile=reconcile,
    )


async def test_cancel_during_durable_reservation_never_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _fixture(tmp_path / "cancel.db") as f:
        entered, release = threading.Event(), threading.Event()
        original = receipts.reserve_operation

        def blocked(*args: object, **kwargs: object) -> tuple[bool, str]:
            entered.set()
            assert release.wait(5)
            return cast(Callable[..., tuple[bool, str]], original)(*args, **kwargs)

        monkeypatch.setattr(receipts, "reserve_operation", blocked)
        calls: list[str] = []

        async def acquire(_entity: int) -> ContactProfileAcquisition:
            return ContactProfileAcquisition(True, 1)

        async def reconcile(_entity: int) -> bool:
            return True

        async def client(_request: object) -> object:
            calls.append("sent")
            return types.UpdatesTooLong()

        task = asyncio.create_task(_owner(f, client, acquire, reconcile).execute(_request(f.reader, "cancel")))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not calls
            assert (
                f.reader.execute("SELECT COUNT(*) FROM contact_operation_receipts WHERE outcome='pending'").fetchone()[
                    0
                ]
                == 0
            )
        finally:
            release.set()


async def test_acknowledged_command_survives_failed_postflight(tmp_path: Path) -> None:
    async with _fixture(tmp_path / "ack.db") as f:
        acquisitions = 0
        reconciled: list[int] = []

        async def acquire(_entity: int) -> ContactProfileAcquisition:
            nonlocal acquisitions
            acquisitions += 1
            if acquisitions > 1:
                raise ConnectionError("controlled postflight failure")
            return ContactProfileAcquisition(True, 1)

        async def reconcile(entity: int) -> bool:
            reconciled.append(entity)
            return True

        async def client(_request: object) -> object:
            check = current_native_write_check()
            assert check is not None
            await f.authority.bind_attempt(check.binding, "physical")
            await f.authority.record_wire_outcome(check.binding, "physical", "acknowledged")
            return types.UpdatesTooLong()

        result = await _owner(f, client, acquire, reconcile).execute(_request(f.reader, "ack"))
        assert result["outcome"] == "acknowledged" and result["verification"] == "unavailable"
        assert f.reader.execute(
            "SELECT outcome FROM contact_operation_receipts WHERE operation_key='ack'"
        ).fetchone() == ("acknowledged",)
        assert reconciled == [2]


async def test_startup_unknown_delete_reconciles_with_background_full_user(tmp_path: Path) -> None:
    from dataclasses import replace
    from unittest.mock import MagicMock

    from mcp_telegram import daemon
    from mcp_telegram.daemon import _SyncMainContext
    from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
    from mcp_telegram.entity_profile.contracts import ObservationBoundary, TargetKind, UserProfileObservation
    from mcp_telegram.entity_profile.full_user_normalization import normalize_full_user_response
    from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
    from mcp_telegram.image_authorization import ScopeToken
    from mcp_telegram.native_write_fence import NativeWriteReceiptBinding
    from tests.demand_adapter_helpers import run_ready_slice
    from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient

    async with _fixture(tmp_path / "restart.db") as f:
        binding = NativeWriteReceiptBinding("contact", 1, 2, 3, "unknown-delete", "delete", 2, "b" * 64, 1)
        version = cast(str, read_contact_command_state(f.reader, 1, 2)["expected_version"])
        await f.writer.run(
            lambda db: receipts.reserve_operation(
                db, binding, "a" * 64, 100, expected_version=version, verification_fingerprint="f" * 64
            )
        )
        await f.writer.run(lambda db: receipts.bind_attempt(db, binding, "claimed"))
        await f.writer.run(lambda db: daemon._recover_native_management_receipts(db, f.scope))
        assert receipts.peer_quarantined(f.reader, 1, 2)
        from mcp_telegram.contact_notes import receipts as note_receipts

        note_binding = NativeWriteReceiptBinding("contact_note", 1, 2, 3, "note-blocked", "update", 2, "c" * 64, 1)
        with f.note_authority.watch(2) as watch:
            assert watch.intake_current()
            with pytest.raises(ValueError, match="contact_outcome_unresolved"):
                await f.writer.run(
                    lambda db: note_receipts.reserve_operation(
                        db, note_binding, "d" * 64, 100, expected_version="e" * 64
                    )
                )
        assert f.reader.execute(
            "SELECT outcome FROM contact_operation_receipts WHERE operation_key='unknown-delete'"
        ).fetchone() == ("unknown",)

        class ProfileClient(_PairClient):
            calls = 0
            capture_image_scope: Callable[[], ScopeToken | None]
            claim_image_scope: Callable[[ScopeToken], bool]

            async def fetch_user_profile(
                self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
            ) -> UserProfileObservation:
                self.calls += 1
                response = SimpleNamespace(
                    users=[types.User(id=2, first_name="Person", contact=False)],
                    chats=[],
                    full_user=types.UserFull(
                        id=2,
                        settings=types.PeerSettings(),
                        notify_settings=types.PeerNotifySettings(),
                        common_chats_count=0,
                    ),
                )
                return normalize_full_user_response(
                    response,
                    target_id=user_id,
                    target_kind=target_kind,
                    owner_account_id=owner_account_id,
                    observation=ObservationBoundary(100.0, 101.0),
                )

        client = ProfileClient()
        client.capture_image_scope = f.cell.capture
        client.claim_image_scope = f.cell.claim
        service = cast(DaemonEntityInfoService, _pair_service(f.reader, client, enabled=True))
        service._deps = replace(
            service._deps,
            writer=f.writer,
            full_user_auth_scope=lambda: f.scope,
            user_profile_port=client,
            now_provider=lambda: 100,
        )
        service.bind_demand_sink(MagicMock())
        token = f.cell.capture()
        assert token is not None
        context = SimpleNamespace(
            client=SimpleNamespace(claim_image_scope=f.cell.claim),
            shutdown_event=asyncio.Event(),
            writer=f.writer,
            api_server=SimpleNamespace(_get_entity_info_service=lambda: service),
        )
        await daemon._reconcile_note_receipts(cast(_SyncMainContext, context), token)
        assert f.reader.execute(
            "SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=2"
        ).fetchone() == ("full_profile",)
        assert service.refresh_coordinator is not None
        await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
        assert client.calls == 1
        assert read_contact_command_state(f.reader, 1, 2)["membership"] is False
        assert not receipts.peer_quarantined(f.reader, 1, 2)
        assert f.reader.execute(
            "SELECT outcome,verification FROM contact_operation_receipts WHERE operation_key='unknown-delete'"
        ).fetchone() == ("unknown", "observed_after_unknown")


async def test_cancel_during_unknown_cleanup_preserves_cancellation(tmp_path: Path) -> None:
    async with _fixture(tmp_path / "cancel-cleanup.db") as f:
        started, release = asyncio.Event(), asyncio.Event()

        async def acquire(_entity: int) -> ContactProfileAcquisition:
            return ContactProfileAcquisition(True, 1)

        async def reconcile(_entity: int) -> bool:
            started.set()
            await release.wait()
            return True

        async def client(_request: object) -> object:
            check = current_native_write_check()
            assert check is not None
            await f.authority.bind_attempt(check.binding, "physical")
            await f.authority.record_wire_outcome(check.binding, "physical", "unknown")
            raise ConnectionError("controlled transport error")

        task = asyncio.create_task(_owner(f, client, acquire, reconcile).execute(_request(f.reader, "cancel-cleanup")))
        try:
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert f.reader.execute(
                "SELECT outcome FROM contact_operation_receipts WHERE operation_key='cancel-cleanup'"
            ).fetchone() == ("unknown",)
        finally:
            release.set()


async def test_cleanup_failure_cannot_be_reported_as_successful_unknown(tmp_path: Path) -> None:
    async with _fixture(tmp_path / "cleanup-failure.db") as f:

        async def acquire(_entity: int) -> ContactProfileAcquisition:
            return ContactProfileAcquisition(True, 1)

        async def reconcile(_entity: int) -> bool:
            raise OSError("controlled reconciliation failure")

        async def client(_request: object) -> object:
            check = current_native_write_check()
            assert check is not None
            await f.authority.bind_attempt(check.binding, "physical")
            await f.authority.record_wire_outcome(check.binding, "physical", "unknown")
            raise ConnectionError("controlled transport error")

        with pytest.raises(ConnectionError):
            await _owner(f, client, acquire, reconcile).execute(_request(f.reader, "cleanup-failure"))
        assert f.reader.execute(
            "SELECT outcome FROM contact_operation_receipts WHERE operation_key='cleanup-failure'"
        ).fetchone() == ("unknown",)


async def test_ack_create_background_readback_allows_newly_visible_phone(tmp_path: Path) -> None:
    from dataclasses import replace
    from unittest.mock import MagicMock

    from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
    from mcp_telegram.entity_profile.contracts import ObservationBoundary, TargetKind, UserProfileObservation
    from mcp_telegram.entity_profile.full_user_normalization import normalize_full_user_response
    from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
    from mcp_telegram.image_authorization import ScopeToken
    from tests.demand_adapter_helpers import run_ready_slice
    from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient

    async with _fixture(tmp_path / "create-phone.db") as f:

        def seed_noncontact(db: sqlite3.Connection) -> None:
            with write_transaction(db):
                db.execute(
                    "UPDATE entity_profile_semantic_snapshots SET fields_json=json_set(fields_json, '$.\"user.contact\"', json('false'), '$.\"owner.saved_contact_name\"', NULL, '$.\"owner.saved_contact_phone_access\"', NULL), provenance_json=json_set(provenance_json, '$.fields.\"user.contact\".order', 1, '$.fields.\"owner.saved_contact_name\".order', 1, '$.fields.\"owner.saved_contact_phone_access\".order', 1) WHERE owner_account_id=1 AND entity_id=2"
                )

        await f.writer.run(seed_noncontact)
        state = read_contact_command_state(f.reader, 1, 2)
        assert state["membership"] is False and state["expected_version"] is not None
        acquired = 0

        async def acquire(_entity: int) -> ContactProfileAcquisition:
            nonlocal acquired
            acquired += 1
            if acquired > 1:
                raise ConnectionError("controlled postflight failure")
            return ContactProfileAcquisition(True, 1)

        async def reconcile(_entity: int) -> bool:
            return True

        async def send(request: object) -> object:
            assert isinstance(request, functions.contacts.AddContactRequest)
            assert request.phone == ""
            check = current_native_write_check()
            assert check is not None
            await f.authority.bind_attempt(check.binding, "physical")
            await f.authority.record_wire_outcome(check.binding, "physical", "acknowledged")
            return types.UpdatesTooLong()

        result = await _owner(f, send, acquire, reconcile).execute(
            {
                "entity_id": 2,
                "action": "create",
                "operation_key": "create-phone",
                "expected_version": state["expected_version"],
                "first_name": "New",
            }
        )
        assert result["outcome"] == "acknowledged" and result["verification"] == "unavailable"

        class ProfileClient(_PairClient):
            capture_image_scope: Callable[[], ScopeToken | None]
            claim_image_scope: Callable[[ScopeToken], bool]

            async def fetch_user_profile(
                self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
            ) -> UserProfileObservation:
                response = SimpleNamespace(
                    users=[types.User(id=2, first_name="New", last_name="", phone="123", contact=True)],
                    chats=[],
                    full_user=types.UserFull(
                        id=2,
                        settings=types.PeerSettings(),
                        notify_settings=types.PeerNotifySettings(),
                        common_chats_count=0,
                    ),
                )
                return normalize_full_user_response(
                    response,
                    target_id=user_id,
                    target_kind=target_kind,
                    owner_account_id=owner_account_id,
                    observation=ObservationBoundary(100.0, 101.0),
                )

        client = ProfileClient()
        client.capture_image_scope, client.claim_image_scope = f.cell.capture, f.cell.claim
        service = cast(DaemonEntityInfoService, _pair_service(f.reader, client, enabled=True))
        service._deps = replace(
            service._deps,
            writer=f.writer,
            full_user_auth_scope=lambda: f.scope,
            user_profile_port=client,
            now_provider=lambda: 100,
        )
        service.bind_demand_sink(MagicMock())
        assert await service.request_note_reconciliation(2)
        assert service.refresh_coordinator is not None
        await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
        assert f.reader.execute(
            "SELECT outcome,verification FROM contact_operation_receipts WHERE operation_key='create-phone'"
        ).fetchone() == ("acknowledged", "verified")
        assert read_contact_command_state(f.reader, 1, 2)["phone"] == "123"


async def test_unexpected_postflight_failure_is_not_hidden(tmp_path: Path) -> None:
    async with _fixture(tmp_path / "postflight-contract.db") as f:
        acquisitions = 0

        async def acquire(_entity: int) -> ContactProfileAcquisition:
            nonlocal acquisitions
            acquisitions += 1
            if acquisitions > 1:
                raise RuntimeError("controlled invariant failure")
            return ContactProfileAcquisition(True, 1)

        async def reconcile(_entity: int) -> bool:
            return True

        async def client(_request: object) -> object:
            check = current_native_write_check()
            assert check is not None
            await f.authority.bind_attempt(check.binding, "physical")
            await f.authority.record_wire_outcome(check.binding, "physical", "acknowledged")
            return types.UpdatesTooLong()

        with pytest.raises(RuntimeError, match="controlled invariant failure"):
            await _owner(f, client, acquire, reconcile).execute(_request(f.reader, "postflight-contract"))
        assert f.reader.execute(
            "SELECT outcome FROM contact_operation_receipts WHERE operation_key='postflight-contract'"
        ).fetchone() == ("acknowledged",)
