"""Command profile observations respect authentication and observation boundaries."""

# pyright: reportAny=false
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import types
from telethon.tl.functions.users import GetFullUserRequest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.contact_notes.intake import capture_contact_note_intake
from mcp_telegram.contact_notes.state import read_note
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService, _AuthScopeUnavailableError
from mcp_telegram.entity_profile.contracts import UserReference
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient
from tests.test_profile_collection_pages import _Client


class _ScopedClient(_PairClient):
    capture_scope: Callable[[], ScopeToken | None]
    claim_scope: Callable[[ScopeToken], bool]

    def capture_image_scope(self) -> ScopeToken | None:
        return self.capture_scope()

    def claim_image_scope(self, token: ScopeToken) -> bool:
        return self.claim_scope(token)


class _CommandClient(_Client):
    async def __call__(self, request: object) -> object:
        assert isinstance(request, GetFullUserRequest)
        assert request.id == types.InputUser(7, 123)
        return await super().__call__(request)


class _CommandGateway(TelethonUserProfileGateway):
    def get_user_reference(self, user_id: int, *, is_self: bool = False) -> UserReference | None:
        assert user_id == 7 and not is_self
        return UserReference(7, 123)


@pytest.mark.parametrize(
    "changed_scope",
    [
        TelegramAuthScope(AUTH_SCOPE_VERSION, 43, 2, 99),
        TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100),
    ],
)
async def test_command_observation_rejects_scope_switch_and_preserves_facts(  # noqa: PLR0915
    tmp_path: Path, changed_scope: TelegramAuthScope
) -> None:
    path = tmp_path / "profile.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    now = 100
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="name", contact=True)],
        chats=[],
        full_user=types.UserFull(
            id=7,
            settings=types.PeerSettings(),
            notify_settings=types.PeerNotifySettings(),
            common_chats_count=0,
            note=types.TextWithEntities("original", []),
        ),
    )
    cell = ScopeCell()
    assert cell.publish(scope)
    client = _ScopedClient()
    client.capture_scope = cell.capture
    client.claim_scope = cell.claim
    service = cast(DaemonEntityInfoService, _pair_service(reader, client, enabled=True))
    service._deps = replace(
        service._deps,
        writer=writer,
        full_user_auth_scope=lambda: scope,
        user_profile_port=_CommandGateway(_CommandClient(response), now_provider=lambda: now),
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
        assert (await service.acquire_command_profile(7)).accepted
        original = read_note(reader, 42, 7)
        before = reader.execute(
            "SELECT fields_json,provenance_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=42"
        ).fetchone()

        response.full_user.note = types.TextWithEntities("stale", [])

        def revoke_during_claim(token: ScopeToken) -> bool:
            cell.begin_change()
            return cell.claim(token)

        client.claim_scope = revoke_during_claim
        service._deps = replace(
            service._deps,
            user_profile_port=_CommandGateway(_CommandClient(response), now_provider=lambda: now),
        )
        assert not (await service.acquire_command_profile(7)).accepted
        assert read_note(reader, 42, 7) == original

        cell = ScopeCell()
        assert cell.publish(scope)
        client.capture_scope = cell.capture

        def deliver_during_claim(token: ScopeToken) -> bool:
            claimed = cell.claim(token)
            capture_contact_note_intake([types.UpdateUser(7)])
            return claimed

        client.claim_scope = deliver_during_claim
        service._deps = replace(
            service._deps,
            user_profile_port=_CommandGateway(_CommandClient(response), now_provider=lambda: now),
        )
        assert not (await service.acquire_command_profile(7)).accepted
        assert read_note(reader, 42, 7) == original
        client.claim_scope = cell.claim

        class SwitchingClient(_CommandClient):
            async def __call__(self, request: object) -> object:
                nonlocal scope
                scope = changed_scope
                return await super().__call__(request)

        response.full_user.note = types.TextWithEntities("stale", [])
        service._deps = replace(
            service._deps,
            user_profile_port=_CommandGateway(SwitchingClient(response), now_provider=lambda: now),
        )
        with pytest.raises(_AuthScopeUnavailableError, match="scope changed"):
            await service.acquire_command_profile(7)
        assert read_note(reader, 42, 7) == original
        assert tuple(
            reader.execute(
                "SELECT fields_json,provenance_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=42"
            ).fetchone()
        ) == tuple(before)
        assert read_note(reader, 43, 7)["state"] == "unknown"

        scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
        now = 200
        service._deps = replace(
            service._deps,
            user_profile_port=_CommandGateway(_CommandClient(response), now_provider=lambda: now),
        )
        assert (await service.acquire_command_profile(7)).accepted
        assert read_note(reader, 42, 7) != original
        after = reader.execute(
            "SELECT provenance_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=42"
        ).fetchone()[0]
        assert json.loads(after)["fields"]["full_user.note"]["observed_at"] == 200
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()
