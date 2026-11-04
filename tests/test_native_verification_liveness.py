"""Native write verification uses the existing durable profile owner."""

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from telethon.tl import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.contact_notes import receipts as note_receipts
from mcp_telegram.contact_notes.intake import capture_contact_note_intake, plain_note_fingerprint
from mcp_telegram.contacts import receipts as contact_receipts
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import (
    ObservationBoundary,
    ProfileFieldObservation,
    ProfileSemanticSnapshot,
    SuppliedProfileUser,
    TargetKind,
)
from mcp_telegram.entity_profile.full_user_normalization import normalize_full_user_response
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.native_write_fence import NativeWriteReceiptBinding
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_contact_note_background_refinement import _ScopeClient
from tests.test_entity_profile_full_user_pair import _pair_service
from tests.test_profile_collection_pages import _Client


@pytest.mark.parametrize(
    ("domain", "post_terminal"),
    [("contact", True), ("contact", False), ("contact_note", True), ("contact_note", False), ("contact_note", "mixed")],
)
async def test_existing_profile_owner_settles_or_acquires_after_terminal(  # noqa: PLR0915 - exercise the coalesced owner path
    tmp_path: Path, domain: Literal["contact", "contact_note"], post_terminal: bool | Literal["mixed"]
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    cell = ScopeCell()
    assert cell.publish(scope)
    client = _ScopeClient()
    client.capture_image_scope, client.claim_image_scope = cell.capture, cell.claim
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))

    def response() -> SimpleNamespace:
        return SimpleNamespace(
            users=[types.User(id=7, first_name="Peer", contact=domain != "contact")],
            chats=[],
            full_user=types.UserFull(
                id=7,
                settings=types.PeerSettings(),
                notify_settings=types.PeerNotifySettings(),
                common_chats_count=0,
                note=types.TextWithEntities("new", []),
            ),
        )

    wire = _Client(response(), response())
    service._deps = replace(service._deps, user_profile_port=TelethonUserProfileGateway(wire, now_provider=lambda: 100))
    try:
        service._profiles.save_core({"id": 7, "type": "user", "name": "target"}, now=100)
        service._profiles.mark_pending(7, now=100)
        assert service.refresh_coordinator is not None
        adapter = EntityProfileDemandAdapter(service.refresh_coordinator)
        await run_ready_slice(adapter, now=100)
        assert len(wire.requests) == 1
        fingerprint = plain_note_fingerprint("new") if domain == "contact_note" else "b" * 64
        binding = NativeWriteReceiptBinding(
            domain, 42, 2, 99, "op", "update" if domain == "contact_note" else "delete", 7, fingerprint, 1
        )
        if domain == "contact_note":
            note_receipts.reserve_operation(conn, binding, "a" * 64, 100, expected_version="v" * 64)
            note_receipts.bind_attempt(conn, binding, "attempt")
            note_receipts.record_wire_outcome(conn, binding, "attempt", "acknowledged")
        else:
            contact_receipts.reserve_operation(
                conn, binding, "a" * 64, 100, expected_version="v" * 64, verification_fingerprint="f" * 64
            )
            contact_receipts.bind_attempt(conn, binding, "attempt")
            contact_receipts.record_wire_outcome(conn, binding, "attempt", "acknowledged")
        assert await service.request_note_reconciliation(7)
        if post_terminal is True:
            _publish_witness(service, conn, domain, response(), 101)
        elif post_terminal == "mixed":
            from mcp_telegram.entity_profile.updates import project_user_profile

            push = project_user_profile(types.User(id=7, contact=True, first_name="Peer"))
            assert push is not None
            with write_transaction(conn):
                order = allocate_observation_order(conn)
                service._profiles.apply_supplied_user(
                    SuppliedProfileUser(7, push.semantic_fields, push.semantic_coverage, {}, False, False),
                    observation=ProfileFieldObservation(42, order, 101, "update"),
                )
        # Finish the active generation, which must retain the newly queued follow-up.
        conn.execute(
            "UPDATE entity_profile_refresh_state SET next_section='pinned_stories',acquisition_cursor=0 WHERE entity_id=7"
        )
        conn.execute(
            "UPDATE entity_detail_sections SET status='not_applicable',retry_at=NULL WHERE entity_id=7 AND section='pinned_stories'"
        )
        conn.commit()
        await run_ready_slice(adapter, now=100)
        assert conn.execute("SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=7").fetchone() == (
            "full_profile",
        )
        await run_ready_slice(adapter, now=100)
        table = "contact_note_operation_receipts" if domain == "contact_note" else "contact_operation_receipts"
        row = cast(
            tuple[str, str, int, int] | None,
            conn.execute(
                f"SELECT outcome,verification,verification_order,terminal_observation_order FROM {table} WHERE operation_key='op'"
            ).fetchone(),
        )
        assert row is not None and row[0:2] == ("acknowledged", "verified") and row[2] > row[3]
        assert len(wire.requests) == (1 if post_terminal is True else 2)
    finally:
        await service.shutdown()
        conn.close()


def _publish_witness(
    service: DaemonEntityInfoService, conn: sqlite3.Connection, domain: str, response: object, now: int
) -> None:
    if domain == "contact":
        from mcp_telegram.contacts.profile import project_contact_profile
        from mcp_telegram.entity_profile.updates import project_user_profile

        user = cast(types.User, cast(SimpleNamespace, response).users[0])
        push = project_user_profile(user)
        assert push is not None
        snapshot = project_contact_profile(
            ProfileSemanticSnapshot(7, 42, push.semantic_fields, push.semantic_coverage, ObservationBoundary(now, now)),
            user,
        )
    else:
        observation = normalize_full_user_response(
            response,
            target_id=7,
            target_kind=TargetKind.USER,
            owner_account_id=42,
            observation=ObservationBoundary(now, now),
        )
        snapshot = observation.semantic_snapshot
        assert snapshot is not None
    with write_transaction(conn):
        order = allocate_observation_order(conn)
        service._profiles.apply_supplied_user(
            SuppliedProfileUser(7, snapshot.fields, snapshot.coverage, {}, False, False),
            observation=ProfileFieldObservation(42, order, now, "update" if domain == "contact" else "rpc"),
            boundary=snapshot.observation,
        )


@pytest.mark.parametrize("interrupt", ["deferred", "cancelled"])
async def test_unfinished_acquisition_keeps_native_obligation(tmp_path: Path, interrupt: str) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    client = _ScopeClient()
    client.capture_image_scope, client.claim_image_scope = cell.capture, cell.claim
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    started = asyncio.Event()

    class InterruptedClient:
        async def __call__(self, request: object) -> object:
            del request
            started.set()
            if interrupt == "deferred":
                raise TelegramRpcAdmissionDeferred(retry_after_seconds=3)
            await asyncio.Event().wait()
            raise AssertionError("cancelled request resumed")

    service._deps = replace(
        service._deps, user_profile_port=TelethonUserProfileGateway(InterruptedClient(), now_provider=lambda: 100)
    )
    try:
        service._profiles.save_core({"id": 7, "type": "user", "name": "target"}, now=100)
        binding = NativeWriteReceiptBinding("contact_note", 42, 2, 99, "op", "update", 7, "b" * 64, 1)
        note_receipts.reserve_operation(conn, binding, "a" * 64, 100, expected_version="v" * 64)
        note_receipts.bind_attempt(conn, binding, "attempt")
        note_receipts.record_wire_outcome(conn, binding, "attempt", "acknowledged")
        assert await service.request_note_reconciliation(7)
        assert service.refresh_coordinator is not None
        task = asyncio.create_task(run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator), now=100))
        await started.wait()
        if interrupt == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TelegramRpcAdmissionDeferred):
                await task
        assert conn.execute("SELECT outcome,verification FROM contact_note_operation_receipts").fetchone() == (
            "acknowledged",
            "pending",
        )
        assert conn.execute("SELECT next_section FROM entity_profile_refresh_state").fetchone() == ("full_profile",)
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.parametrize("outcome", ["acknowledged", "unknown"])
@pytest.mark.parametrize("domain", ["contact", "contact_note"])
async def test_insufficient_full_user_finishes_once_then_later_witness_refines(
    tmp_path: Path, outcome: str, domain: Literal["contact", "contact_note"]
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    client = _ScopeClient()
    client.capture_image_scope, client.claim_image_scope = cell.capture, cell.claim
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    full_user = types.UserFull(
        id=7, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(), common_chats_count=0
    )
    incomplete = SimpleNamespace(users=[types.UserEmpty(id=7)], chats=[], full_user=full_user)
    complete = SimpleNamespace(
        users=[types.User(id=7, first_name="Peer", contact=domain == "contact_note")], chats=[], full_user=full_user
    )
    wire = _Client(incomplete, complete)
    service._deps = replace(service._deps, user_profile_port=TelethonUserProfileGateway(wire, now_provider=lambda: 100))
    try:
        service._profiles.save_core({"id": 7, "type": "user", "name": "target"}, now=100)
        binding = NativeWriteReceiptBinding(
            domain,
            42,
            2,
            99,
            "op",
            "delete" if domain == "contact" else "clear",
            7,
            "b" * 64 if domain == "contact" else plain_note_fingerprint(""),
            1,
        )
        if domain == "contact":
            contact_receipts.reserve_operation(
                conn, binding, "a" * 64, 100, expected_version="v" * 64, verification_fingerprint="f" * 64
            )
            contact_receipts.bind_attempt(conn, binding, "attempt")
            contact_receipts.record_wire_outcome(conn, binding, "attempt", outcome)
        else:
            note_receipts.reserve_operation(conn, binding, "a" * 64, 100, expected_version="v" * 64)
            note_receipts.bind_attempt(conn, binding, "attempt")
            note_receipts.record_wire_outcome(conn, binding, "attempt", outcome)
        assert await service.request_note_reconciliation(7)
        assert service.refresh_coordinator is not None
        adapter = EntityProfileDemandAdapter(service.refresh_coordinator)
        await run_ready_slice(adapter, now=100)
        table = "contact_operation_receipts" if domain == "contact" else "contact_note_operation_receipts"
        row = cast(
            tuple[str, str, str, str | None] | None,
            conn.execute(
                "SELECT outcome,verification,json_extract(result_json,'$.verification_reason'),"
                f"json_extract(result_json,'$.verification_fingerprint') FROM {table}"
            ).fetchone(),
        )
        assert row == (outcome, "inconclusive", "insufficient_evidence", "f" * 64 if domain == "contact" else None)
        assert contact_receipts.peer_quarantined(conn, 42, 7) is (outcome == "unknown")
        assert len(wire.requests) == 1
        _publish_witness(service, conn, domain, complete, 101)
        # The next normal profile pass consumes this witness without another network request.
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='full_profile' WHERE entity_id=7")
        conn.commit()
        await run_ready_slice(adapter, now=100)
        assert conn.execute(f"SELECT outcome,verification FROM {table}").fetchone() == (
            outcome,
            "verified" if outcome == "acknowledged" else "observed_after_unknown",
        )
        assert len(wire.requests) == 1
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.parametrize("new_revision", [False, True])
async def test_intake_race_retries_only_current_revision(tmp_path: Path, new_revision: bool) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    client = _ScopeClient()
    client.capture_image_scope, client.claim_image_scope = cell.capture, cell.claim
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="Peer", contact=False)],
        chats=[],
        full_user=types.UserFull(
            id=7, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(), common_chats_count=0
        ),
    )

    class RacingClient:
        async def __call__(self, request: object) -> object:
            del request
            capture_contact_note_intake([types.UpdateUser(7)])
            if new_revision:
                conn.execute("UPDATE entity_profile_refresh_state SET generation=generation+1 WHERE entity_id=7")
                conn.commit()
            return response

    service._deps = replace(
        service._deps, user_profile_port=TelethonUserProfileGateway(RacingClient(), now_provider=lambda: 100)
    )
    try:
        service._profiles.save_core({"id": 7, "type": "user", "name": "target"}, now=100)
        binding = NativeWriteReceiptBinding("contact", 42, 2, 99, "op", "delete", 7, "b" * 64, 1)
        contact_receipts.reserve_operation(
            conn, binding, "a" * 64, 100, expected_version="v" * 64, verification_fingerprint="f" * 64
        )
        contact_receipts.bind_attempt(conn, binding, "attempt")
        contact_receipts.record_wire_outcome(conn, binding, "attempt", "acknowledged")
        assert await service.request_note_reconciliation(7)
        assert service.refresh_coordinator is not None
        result = await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator), now=100)
        assert result is not None
        assert result.kind is (SliceOutcomeKind.STALE if new_revision else SliceOutcomeKind.RETRYABLE_FAILURE)
        assert conn.execute("SELECT outcome,verification FROM contact_operation_receipts").fetchone() == (
            "acknowledged",
            "pending",
        )
    finally:
        await service.shutdown()
        conn.close()


async def test_contact_update_with_authoritative_noncontact_witness_is_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    client = _ScopeClient()
    client.capture_image_scope, client.claim_image_scope = cell.capture, cell.claim
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="Peer", contact=False)],
        chats=[],
        full_user=types.UserFull(
            id=7, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(), common_chats_count=0
        ),
    )
    wire = _Client(response)
    service._deps = replace(service._deps, user_profile_port=TelethonUserProfileGateway(wire, now_provider=lambda: 100))
    try:
        service._profiles.save_core({"id": 7, "type": "user", "name": "target"}, now=100)
        binding = NativeWriteReceiptBinding("contact", 42, 2, 99, "op", "update", 7, "b" * 64, 1)
        contact_receipts.reserve_operation(
            conn, binding, "a" * 64, 100, expected_version="v" * 64, verification_fingerprint="f" * 64
        )
        contact_receipts.bind_attempt(conn, binding, "attempt")
        contact_receipts.record_wire_outcome(conn, binding, "attempt", "acknowledged")
        assert await service.request_note_reconciliation(7)
        assert service.refresh_coordinator is not None
        await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator), now=100)
        assert conn.execute("SELECT verification FROM contact_operation_receipts").fetchone() == ("mismatch",)
        assert len(wire.requests) == 1
    finally:
        await service.shutdown()
        conn.close()
