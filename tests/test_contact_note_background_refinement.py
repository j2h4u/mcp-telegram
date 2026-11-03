"""Uncertain note writes are reconciled by ordinary profile refreshes."""

# pyright: reportAny=false
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.contact_notes.intake import capture_contact_note_intake
from mcp_telegram.contact_notes.receipts import bind_attempt, record_wire_outcome, reserve_operation
from mcp_telegram.contact_notes.state import read_note
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.native_write_fence import NativeWriteReceiptBinding
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient
from tests.test_profile_collection_pages import _Client


class _ScopeClient(_PairClient):
    capture_image_scope: Callable[[], ScopeToken | None]
    claim_image_scope: Callable[[ScopeToken], bool]


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("race", ["none", "scope", "update"])
async def test_background_refresh_refines_unknown_only_when_current(  # noqa: PLR0915
    tmp_path: Path, enabled: bool, race: str
) -> None:
    path = tmp_path / "profile.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    cell = ScopeCell()
    assert cell.publish(scope)
    client = _ScopeClient()
    client.capture_image_scope = cell.capture

    race_active = False

    def claim(token: ScopeToken) -> bool:
        if race_active and race == "scope":
            cell.begin_change()
        claimed = cell.claim(token)
        if race_active and race == "update":
            capture_contact_note_intake([types.UpdateUser(7)])
        return claimed

    client.claim_image_scope = claim
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=enabled))
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="name", contact=True)],
        chats=[],
        full_user=types.UserFull(
            id=7,
            settings=types.PeerSettings(),
            notify_settings=types.PeerNotifySettings(),
            common_chats_count=0,
            note=types.TextWithEntities("prior", []),
        ),
    )
    service._deps = replace(
        service._deps,
        user_profile_port=TelethonUserProfileGateway(_Client(response), now_provider=lambda: 100),
    )
    try:
        service._profiles.save_core({"id": 7, "type": "user", "name": "target"}, now=100)
        assert await service.request_note_reconciliation(7)
        coordinator = service.refresh_coordinator
        assert coordinator is not None
        await run_ready_slice(EntityProfileDemandAdapter(coordinator))
        prior = read_note(conn, 42, 7)
        assert prior["state"] == "retained"
        conn.execute("UPDATE entity_profile_refresh_state SET status='complete' WHERE entity_id=7")
        conn.commit()
        response.full_user.note = types.TextWithEntities("new", [])
        service._deps = replace(
            service._deps,
            user_profile_port=TelethonUserProfileGateway(_Client(response), now_provider=lambda: 100),
        )
        for account in (42, 43):
            binding = NativeWriteReceiptBinding(
                "contact_note", account, 2, 99, f"key-{account}", "update", 7, "b" * 64, 1
            )
            reserve_operation(conn, binding, "a" * 64, 100, expected_version="v" * 64)
            bind_attempt(conn, binding, f"attempt-{account}")
            record_wire_outcome(conn, binding, f"attempt-{account}", "unknown")
        assert await service.request_note_reconciliation(7)
        race_active = True
        await run_ready_slice(EntityProfileDemandAdapter(coordinator))
        current = read_note(conn, 42, 7)
        assert current["state"] == "retained"
        assert (current != prior) == (race == "none")
        assert read_note(conn, 43, 7)["state"] == "unknown"
        rows = conn.execute(
            "SELECT account_id,outcome,verification FROM contact_note_operation_receipts ORDER BY account_id"
        ).fetchall()
        expected = "observed_after_unknown" if race == "none" else "pending"
        assert rows == [(42, "unknown", expected), (43, "unknown", "pending")]
        if race == "none":
            service._deps = replace(
                service._deps,
                self_id=42,
                full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 43, 2, 99),
            )
            assert service._local_profile_owner(7) is None
            result = await service.get_entity_info({"entity_id": 7})
            assert "new" not in json.dumps(result)
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.parametrize("case", ["match", "mismatch", "old", "scope", "update"])
async def test_background_refresh_refines_acknowledged_only_with_current_fresh_evidence(  # noqa: PLR0915
    tmp_path: Path, case: str
) -> None:
    from mcp_telegram.contact_notes.intake import plain_note_fingerprint

    path = tmp_path / "profile.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    client = _ScopeClient()
    client.capture_image_scope = cell.capture

    race_active = False

    def claim(token: ScopeToken) -> bool:
        if race_active and case == "scope":
            cell.begin_change()
        result = cell.claim(token)
        if race_active and case == "update":
            capture_contact_note_intake([types.UpdateUser(7)])
        return result

    client.claim_image_scope = claim
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="name", contact=True)],
        chats=[],
        full_user=types.UserFull(
            id=7,
            settings=types.PeerSettings(),
            notify_settings=types.PeerNotifySettings(),
            common_chats_count=0,
            note=types.TextWithEntities("actual", []),
        ),
    )
    service._deps = replace(
        service._deps,
        user_profile_port=TelethonUserProfileGateway(_Client(response), now_provider=lambda: 100),
    )
    try:
        service._profiles.save_core({"id": 7, "type": "user", "name": "target"}, now=100)
        fingerprint = plain_note_fingerprint("actual" if case != "mismatch" else "different")
        binding = NativeWriteReceiptBinding("contact_note", 42, 2, 99, "ack", "update", 7, fingerprint, 1)
        reserve_operation(conn, binding, "a" * 64, 100, expected_version="v" * 64)
        bind_attempt(conn, binding, "attempt")
        record_wire_outcome(conn, binding, "attempt", "acknowledged")
        if case == "old":
            conn.execute("UPDATE contact_note_operation_receipts SET terminal_observation_order=999999")
            conn.commit()
        assert await service.request_note_reconciliation(7)
        race_active = True
        coordinator = service.refresh_coordinator
        assert coordinator is not None
        await run_ready_slice(EntityProfileDemandAdapter(coordinator))
        outcome, verification, order, terminal = conn.execute(
            "SELECT outcome,verification,verification_order,terminal_observation_order "
            "FROM contact_note_operation_receipts WHERE operation_key='ack'"
        ).fetchone()
        assert outcome == "acknowledged"
        expected = {"match": "verified", "mismatch": "mismatch"}.get(case, "pending")
        assert verification == expected
        if case in {"match", "mismatch"}:
            assert order > terminal
        else:
            assert order is None
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.parametrize("status", ["pending", "failed"])
async def test_repeated_reconciliation_coalesces_and_eventually_observes_fresh_note(
    tmp_path: Path, status: str
) -> None:
    path = tmp_path / "profile.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    client = _ScopeClient()
    client.capture_image_scope = cell.capture
    client.claim_image_scope = cell.claim
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="name", contact=True)],
        chats=[],
        full_user=types.UserFull(
            id=7,
            settings=types.PeerSettings(),
            notify_settings=types.PeerNotifySettings(),
            common_chats_count=0,
            note=types.TextWithEntities("fresh", []),
        ),
    )
    service._deps = replace(
        service._deps,
        user_profile_port=TelethonUserProfileGateway(_Client(response), now_provider=lambda: 100),
    )
    try:
        service._profiles.save_core({"id": 7, "type": "user", "name": "target"}, now=100)
        service._profiles.mark_pending(7, now=100)
        conn.execute(
            "UPDATE entity_profile_refresh_state SET status=?,retry_at=?,next_section='pinned_stories',"
            "acquisition_cursor=5 WHERE entity_id=7",
            (status, 150 if status == "failed" else None),
        )
        conn.commit()
        binding = NativeWriteReceiptBinding("contact_note", 42, 2, 99, "key", "update", 7, "b" * 64, 1)
        reserve_operation(conn, binding, "a" * 64, 100, expected_version="v" * 64)
        bind_attempt(conn, binding, "attempt")
        record_wire_outcome(conn, binding, "attempt", "unknown")
        before = conn.execute(
            "SELECT status,retry_at,next_section,acquisition_cursor,generation FROM entity_profile_refresh_state"
        ).fetchone()
        assert await service.request_note_reconciliation(7)
        assert await service.request_note_reconciliation(7)
        after = conn.execute(
            "SELECT status,retry_at,next_section,acquisition_cursor,generation,follow_up_required "
            "FROM entity_profile_refresh_state"
        ).fetchone()
        assert after == (*before, 1)
        conn.execute(
            "UPDATE entity_profile_refresh_state SET status='pending',retry_at=NULL,acquisition_cursor=0 WHERE entity_id=7"
        )
        conn.execute(
            "UPDATE entity_detail_sections SET status='not_applicable',retry_at=NULL "
            "WHERE entity_id=7 AND section='pinned_stories'"
        )
        conn.commit()
        cursor = service._profiles.next_due_refresh(now=100, entity_id=7)
        assert cursor is not None
        assert service._profiles.complete_unchanged_section(cursor, now=100)
        coordinator = service.refresh_coordinator
        assert coordinator is not None
        await run_ready_slice(EntityProfileDemandAdapter(coordinator))
        outcome, verification, order, terminal = conn.execute(
            "SELECT outcome,verification,verification_order,terminal_observation_order "
            "FROM contact_note_operation_receipts WHERE operation_key='key'"
        ).fetchone()
        assert (outcome, verification) == ("unknown", "observed_after_unknown")
        assert order > terminal
    finally:
        await service.shutdown()
        conn.close()
