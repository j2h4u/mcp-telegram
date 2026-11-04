"""A delivered native write response can settle an existing verification demand."""

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from telethon.sessions import SQLiteSession
from telethon.tl import types

from mcp_telegram.contacts import receipts
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.native_write_fence import NativeWriteReceiptBinding
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.state import telegram_session_lease
from mcp_telegram.sync_db import open_sync_db_reader
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_contact_note_background_refinement import _ScopeClient
from tests.test_contact_response_delivery import _contact_response
from tests.test_entity_profile_full_user_pair import _pair_service
from tests.test_profile_collection_pages import _Client
from tests.test_telegram_transport import _Harness
from tests.test_update_delivery import _client, _database
from tests.test_update_delivery_handlers import _manager


async def test_delivered_delete_settles_receipt_without_another_rpc(tmp_path: Path) -> None:  # noqa: PLR0914, PLR0915
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    profile_conn = sqlite3.connect(path)
    gate = _client(tmp_path / "session", writer, channel=False)
    lease_context = telegram_session_lease(tmp_path)
    gate.bind_session_lease(lease_context.__enter__())
    wire = _Harness()
    service: DaemonEntityInfoService | None = None
    try:
        await gate.start_update_delivery(99)
        _manager(gate, reader, writer)
        scope = gate.retained_auth_scope()
        assert scope is not None and gate._image_scope_cell.publish(scope)
        client = _ScopeClient()
        client.capture_image_scope = gate._image_scope_cell.capture
        client.claim_image_scope = gate._image_scope_cell.claim
        service = cast(DaemonEntityInfoService, _pair_service(profile_conn, client, enabled=True))
        response = SimpleNamespace(
            users=[types.User(id=55, first_name="Fixture", contact=True)],
            chats=[],
            full_user=types.UserFull(
                id=55, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(), common_chats_count=0
            ),
        )
        profile_wire = _Client(response)
        service._deps = replace(
            service._deps,
            full_user_auth_scope=lambda: scope,
            user_profile_port=TelethonUserProfileGateway(profile_wire, now_provider=lambda: 100),
        )
        service._profiles.save_core({"id": 55, "type": "user", "name": "Fixture"}, now=100)
        service._profiles.mark_pending(55, now=100)
        assert service.refresh_coordinator is not None
        adapter = EntityProfileDemandAdapter(service.refresh_coordinator)
        await run_ready_slice(adapter, now=100)
        binding = NativeWriteReceiptBinding("contact", 99, 2, scope.auth_key_id, "key", "delete", 55, "a" * 64, 1)
        await writer.run(
            lambda conn: receipts.reserve_operation(
                conn, binding, "b" * 64, 100, expected_version="v" * 64, verification_fingerprint="f" * 64
            )
        )
        await writer.run(lambda conn: receipts.bind_attempt(conn, binding, "attempt"))
        await writer.run(lambda conn: receipts.record_wire_outcome(conn, binding, "attempt", "acknowledged"))
        assert await service.request_note_reconciliation(55)
        envelope = await _contact_response(gate, wire, True)
        processed: list[object] = []
        users, chats = gate._message_box.process_updates(envelope, gate._mb_entity_cache, processed)
        admitted = await gate._preprocess_updates(processed, users, chats)
        await asyncio.gather(*(gate._dispatch_update(update) for update in admitted))
        profile_conn.execute(
            "UPDATE entity_profile_refresh_state SET next_section='pinned_stories',acquisition_cursor=0 WHERE entity_id=55"
        )
        profile_conn.execute(
            "UPDATE entity_detail_sections SET status='not_applicable',retry_at=NULL WHERE entity_id=55 AND section='pinned_stories'"
        )
        profile_conn.commit()
        await run_ready_slice(adapter, now=100)
        await run_ready_slice(adapter, now=100)
        row = cast(
            tuple[str, str, int, int] | None,
            reader.execute(
                "SELECT outcome,verification,verification_order,terminal_observation_order FROM contact_operation_receipts"
            ).fetchone(),
        )
        assert row is not None and row[:2] == ("acknowledged", "verified") and row[2] > row[3]
        assert len(profile_wire.requests) == 1
    finally:
        if service is not None:
            await service.shutdown()
        await wire.close()
        cast(SQLiteSession, gate.session).close()
        assert gate._session_pin is not None
        gate._session_pin._release()
        lease_context.__exit__(None, None, None)
        profile_conn.close()
        reader.close()
        await writer.aclose()
