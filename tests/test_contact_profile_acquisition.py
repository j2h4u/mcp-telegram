"""Contact command preflight publishes account-bound FullUser evidence."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.contacts.intake import capture_contact_intake
from mcp_telegram.contacts.state import read_contact_command_state
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from tests.test_contact_note_profile_seam import _CommandClient, _CommandGateway, _ScopedClient
from tests.test_entity_profile_full_user_pair import _pair_service


@pytest.mark.parametrize(("contact", "min_user"), [(False, False), (True, False), (True, True)])
async def test_contact_preflight_accepts_authoritative_user_and_local_read(
    tmp_path: Path, contact: bool, min_user: bool
) -> None:
    path = tmp_path / "profile.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    cell = ScopeCell()
    assert cell.publish(scope)
    client = _ScopedClient()
    client.capture_image_scope = cell.capture
    client.claim_image_scope = cell.claim
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="saved", contact=contact, min=min_user, phone="123" if contact else None)],
        chats=[],
        full_user=types.UserFull(
            id=7, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(), common_chats_count=0
        ),
    )
    service = cast(DaemonEntityInfoService, _pair_service(reader, client, enabled=True))
    service._deps = replace(
        service._deps,
        writer=writer,
        full_user_auth_scope=lambda: scope,
        user_profile_port=_CommandGateway(_CommandClient(response), now_provider=lambda: 100),
    )
    try:
        await writer.run(
            lambda conn: EntityProfileRepository(conn, section_ttl_seconds=300).save_core(
                {"id": 7, "type": "user", "name": "old"}, now=100
            )
        )
        await writer.run(
            lambda conn: EntityProfileRepository(conn, section_ttl_seconds=300).mark_pending(
                7, now=100, pair_eligible_override=True
            )
        )
        acquired = await service.acquire_contact_command_profile(7)
        state = read_contact_command_state(reader, 42, 7)
        if min_user:
            assert not acquired.accepted and state["membership"] is None
        else:
            assert acquired.accepted and acquired.observation_order == state["observation_order"]
            assert state["membership"] is contact
        if contact and not min_user:
            assert state["first_name"] == "saved" and state["phone"] == "123"
        else:
            assert state["phone_state"] == "unknown"
        network = AsyncMock(side_effect=AssertionError("ordinary reads must remain local"))
        service._deps = replace(service._deps, user_profile_port=network)
        result = await service.get_entity_info({"entity_id": 7})
        data = cast(dict[str, object], result["data"])
        assert data["contact_state"] == state
        assert network.await_count == 0
    finally:
        reader.close()
        await writer.aclose()


async def test_contact_preflight_rejects_concurrent_delivered_update(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    cell = ScopeCell()
    assert cell.publish(scope)
    client = _ScopedClient()
    client.capture_image_scope, client.claim_image_scope = cell.capture, cell.claim
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="name", contact=False)],
        chats=[],
        full_user=types.UserFull(
            id=7, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(), common_chats_count=0
        ),
    )

    class RacingClient(_CommandClient):
        async def __call__(self, request: object) -> object:
            capture_contact_intake([types.UpdateUser(7)])
            return await super().__call__(request)

    service = cast(DaemonEntityInfoService, _pair_service(reader, client, enabled=True))
    service._deps = replace(
        service._deps,
        writer=writer,
        full_user_auth_scope=lambda: scope,
        user_profile_port=_CommandGateway(RacingClient(response), now_provider=lambda: 100),
    )
    try:
        await writer.run(
            lambda conn: EntityProfileRepository(conn, section_ttl_seconds=300).save_core(
                {"id": 7, "type": "user", "name": "old"}, now=100
            )
        )
        await writer.run(
            lambda conn: EntityProfileRepository(conn, section_ttl_seconds=300).mark_pending(
                7, now=100, pair_eligible_override=True
            )
        )
        assert not (await service.acquire_contact_command_profile(7)).accepted
        assert read_contact_command_state(reader, 42, 7)["membership"] is None
    finally:
        reader.close()
        await writer.aclose()
