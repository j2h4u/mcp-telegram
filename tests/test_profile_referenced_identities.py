"""Bounded TG-220 characterization, non-production fixtures only."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.users import GetFullUserRequest

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService, _AuthScopeUnavailableError
from mcp_telegram.dialog_fact_observations import claim_dialog_fact
from mcp_telegram.dialog_identity import read_dialog_identities
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, RefreshLimits
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import (
    TelethonChannelProfileGateway,
    TelethonGroupProfileGateway,
    TelethonUserProfileGateway,
)
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_channel_profile_telegram_gateway import _full_channel, _reference
from tests.test_entity_profile_group_full_chat_pair import _full_chat, _GroupClient
from tests.test_entity_profile_progressive import _test_service


def _debit_current_attempt() -> None:
    budget = current_rpc_scope().attempt_budget
    assert budget is not None
    budget.debit()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["normal", "stale", "revoked", "later", "wrong_account"])
async def test_full_group_participant_sidecar_unknown(tmp_path: Path, scenario: str) -> None:
    path = tmp_path / "group.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES(-123,'group','Target',1)")
            conn.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES(-123,'Target','group')")
            EntityProfileRepository(conn, section_ttl_seconds=300).mark_pending(-123, now=100)
            conn.execute("UPDATE entity_profile_refresh_state SET acquisition_cursor=1 WHERE entity_id=-123")

    await writer.run(seed)
    response = _full_chat(
        participants=types.ChatParticipants(123, [types.ChatParticipant(88, 42, datetime(2026, 1, 1, tzinfo=UTC))], 1)
    )
    response.users.append(types.User(88, first_name="Participant", username="member"))
    response.users.append(types.User(99, first_name="Unreferenced"))
    auth = _GroupClient(response)
    original_token = auth.scope_cell.capture()
    assert original_token is not None

    class Gateway:
        async def __call__(self, request: object) -> object:
            assert isinstance(request, GetFullChatRequest)
            if scenario == "revoked":
                change = auth.scope_cell.begin_change()
                auth.scope_cell.end_change(change)
                assert auth.scope_cell.publish(original_token.scope)
            elif scenario in {"stale", "later", "wrong_account"}:

                def update(conn: sqlite3.Connection) -> None:
                    with write_transaction(conn):
                        if scenario == "stale":
                            conn.execute(
                                "UPDATE entity_profile_refresh_state SET generation=generation+1 WHERE entity_id=-123"
                            )
                        elif scenario == "wrong_account":
                            conn.execute("UPDATE dialog_directory_state SET account_id=43 WHERE singleton=1")
                        else:
                            order = allocate_observation_order(conn)
                            assert claim_dialog_fact(conn, 88, "name", order)
                            assert claim_dialog_fact(conn, 88, "username", order)
                            conn.execute(
                                "INSERT INTO entities(id,type,name,username,updated_at) VALUES (88,'user','Later','later',200)"
                            )

                await writer.run(update)
            return response

    reader = sqlite3.connect(path)
    service = _test_service(reader, limits=RefreshLimits(), account_id=42)
    service._deps = replace(
        service._deps,
        writer=writer,
        client=auth,
        group_profile_port=TelethonGroupProfileGateway(Gateway(), now_provider=lambda: 100),
    )
    try:
        assert service.refresh_coordinator is not None
        await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
        expected = (
            None
            if scenario in {"revoked", "wrong_account"}
            else (("Later", "later") if scenario == "later" else ("Participant", "member"))
        )
        assert reader.execute("SELECT name,username FROM entities WHERE id=88").fetchone() == expected
        assert reader.execute("SELECT 1 FROM entities WHERE id=99").fetchone() is None
        assert reader.execute("SELECT 1 FROM synced_dialogs WHERE dialog_id=88").fetchone() is None
        if scenario == "stale":
            assert reader.execute(
                "SELECT acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=-123"
            ).fetchone() == (1,)
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT name,username FROM entities WHERE id=88").fetchone() == expected
        assert read_dialog_identities(reopened, [88])[88].name == (expected[0] if expected is not None else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_full_channel_linked_chat_sidecar(tmp_path: Path, known: bool):
    path = tmp_path / "channel.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    target = -1000000000123
    linked = -1000000000200

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES(?,'channel','Target',1)", (target,))
            conn.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES(?,'Target','channel')", (target,))
            if known:
                conn.execute(
                    "INSERT INTO entities(id,type,name,username,updated_at) VALUES(?,'channel','Before','before',1)",
                    (linked,),
                )
            EntityProfileRepository(conn, section_ttl_seconds=300).mark_pending(target, now=100)
            conn.execute("UPDATE entity_profile_refresh_state SET acquisition_cursor=1 WHERE entity_id=?", (target,))

    await writer.run(seed)
    response = _full_channel()
    response.chats.append(
        types.Channel(200, "Linked", types.ChatPhotoEmpty(), None, access_hash=222, username="linked")
    )
    auth = _GroupClient(response)

    class Gateway:
        async def __call__(self, request: object) -> object:
            assert isinstance(request, GetFullChannelRequest)
            return response

    class Reference:
        def get_channel_reference(self, entity_id: int):
            assert entity_id == target
            return _reference()

    reader = sqlite3.connect(path)
    service = _test_service(reader, limits=RefreshLimits(), account_id=42)
    service._deps = replace(
        service._deps,
        writer=writer,
        client=auth,
        channel_profile_port=TelethonChannelProfileGateway(Gateway(), now_provider=lambda: 100),
        channel_reference_provider=Reference(),
    )
    try:
        assert service.refresh_coordinator is not None
        await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
        expected = ("Linked", "linked")
        assert reader.execute("SELECT name,username FROM entities WHERE id=?", (linked,)).fetchone() == expected
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT name,username FROM entities WHERE id=?", (linked,)).fetchone() == expected
        assert read_dialog_identities(reopened, [linked])[linked].name == "Linked"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["manager", "personal_channel"])
@pytest.mark.parametrize("scenario", ["normal", "stale", "revoked", "error_aba", "mid_commit", "flood_mid_failure"])
@pytest.mark.parametrize("pair_mode", ["enabled", "disabled"])
async def test_full_user_manager_and_personal_channel_sidecars(  # noqa: PLR0914, PLR0915
    tmp_path: Path, kind: str, scenario: str, pair_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "user.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    linked = -1000000000200

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute(
                "INSERT INTO entities(id,type,name,updated_at) VALUES(55,?,'Target',1)",
                ("bot" if kind == "manager" else "user",),
            )
            conn.execute(
                "INSERT INTO entities(id,type,name,username,updated_at) VALUES(88,?,'Before','before',1)",
                ("bot" if kind == "manager" else "user",),
            )
            conn.execute(
                "INSERT INTO entities(id,type,name,username,updated_at) VALUES(?,'channel','Old Channel','oldchannel',1)",
                (linked,),
            )
            repo = EntityProfileRepository(conn, section_ttl_seconds=300)
            repo.save_core({"id": 55, "type": "bot" if kind == "manager" else "user", "name": "Target"}, now=100)
            repo.mark_pending(55, now=100, pair_eligible_override=True, pair_mode_override=pair_mode)
            conn.execute("UPDATE entity_profile_refresh_state SET acquisition_cursor=1 WHERE entity_id=55")

    await writer.run(seed)
    full = types.UserFull(
        55,
        types.PeerSettings(),
        types.PeerNotifySettings(),
        0,
        bot_manager_id=88 if kind == "manager" else None,
        personal_channel_id=200 if kind == "personal_channel" else None,
        personal_channel_message=1 if kind == "personal_channel" else None,
    )
    target = types.User(
        55, first_name="Target", bot=kind == "manager", bot_info_version=1 if kind == "manager" else None
    )
    manager = types.User(
        88, first_name="Manager", username="manager", bot=True, bot_info_version=1, bot_can_manage_bots=True
    )
    channel = types.Channel(200, "Channel New", types.ChatPhotoEmpty(), None, access_hash=222, username="channelnew")
    response = types.users.UserFull(
        full, [channel] if kind == "personal_channel" else [], [target, manager] if kind == "manager" else [target]
    )
    response.users.append(types.User(99, first_name="Unreferenced"))
    response.chats.append(types.Channel(201, "Unreferenced", types.ChatPhotoEmpty(), None, access_hash=223))
    auth = _GroupClient(response)
    token = auth.scope_cell.capture()
    assert token is not None

    class Gateway:
        async def __call__(self, request: object) -> object:
            assert isinstance(request, GetFullUserRequest)
            _debit_current_attempt()
            if scenario == "flood_mid_failure":
                raise TelegramRpcThrottled(retry_after_seconds=7)
            if scenario in {"revoked", "error_aba"}:
                change = auth.scope_cell.begin_change()
                auth.scope_cell.end_change(change)
                assert auth.scope_cell.publish(token.scope)
                if scenario == "error_aba":
                    raise OSError("fixture read failure")
            elif scenario == "stale":

                def advance(conn: sqlite3.Connection) -> None:
                    with write_transaction(conn):
                        conn.execute(
                            "UPDATE entity_profile_refresh_state SET generation=generation+1 WHERE entity_id=55"
                        )

                await writer.run(advance)
            return response

    reader = sqlite3.connect(path)
    service = _test_service(reader, limits=RefreshLimits(), account_id=42)
    service._deps = replace(
        service._deps,
        writer=writer,
        client=auth,
        user_profile_port=TelethonUserProfileGateway(Gateway(), now_provider=lambda: 100),
        enable_full_user_pair=pair_mode == "enabled",
    )
    if scenario == "mid_commit":
        method = "commit_full_user_pair" if pair_mode == "enabled" else "_commit_section_in_transaction"
        original = cast(Callable[..., bool], getattr(EntityProfileRepository, method))

        def revoke_after_write(repo: EntityProfileRepository, *args: object, **kwargs: object) -> bool:
            result = original(repo, *args, **kwargs)
            change = auth.scope_cell.begin_change()
            auth.scope_cell.end_change(change)
            assert auth.scope_cell.publish(token.scope)
            return result

        monkeypatch.setattr(EntityProfileRepository, method, revoke_after_write)
    if scenario == "flood_mid_failure":
        original_failure = cast(Callable[..., bool], EntityProfileRepository.mark_section_failure)

        def revoke_after_failure(repo: EntityProfileRepository, *args: object, **kwargs: object) -> bool:
            result = original_failure(repo, *args, **kwargs)
            change = auth.scope_cell.begin_change()
            auth.scope_cell.end_change(change)
            assert auth.scope_cell.publish(token.scope)
            return result

        monkeypatch.setattr(EntityProfileRepository, "mark_section_failure", revoke_after_failure)
    try:
        assert service.refresh_coordinator is not None
        if scenario in {"mid_commit", "flood_mid_failure"}:
            expected = _AuthScopeUnavailableError if scenario == "mid_commit" else TelegramRpcThrottled
            with pytest.raises(expected):
                await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
        else:
            await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
        assert reader.execute("SELECT name,username FROM entities WHERE id=88").fetchone() == (
            ("Manager", "manager")
            if kind == "manager" and scenario not in {"revoked", "error_aba", "mid_commit", "flood_mid_failure"}
            else ("Before", "before")
        )
        assert reader.execute("SELECT name,username FROM entities WHERE id=?", (linked,)).fetchone() == (
            ("Channel New", "channelnew")
            if kind == "personal_channel"
            and scenario not in {"revoked", "error_aba", "mid_commit", "flood_mid_failure"}
            else ("Old Channel", "oldchannel")
        )
        assert reader.execute("SELECT 1 FROM entities WHERE id IN (99, -1000000000201)").fetchone() is None
        detail = EntityProfileRepository(reader, section_ttl_seconds=300).read(55, now=100)
        assert detail is not None
        if kind == "personal_channel" and scenario == "normal" and pair_mode == "enabled":
            assert cast(dict[str, object], detail.detail["personal_channel"])["title"] == "Channel New"
        if scenario in {"error_aba", "mid_commit", "flood_mid_failure"}:
            assert reader.execute(
                "SELECT status,reason,retry_at,pair_attempts,pair_retries FROM entity_profile_refresh_state WHERE entity_id=55"
            ).fetchone() == ("pending", "refresh_queued", None, 0, 0)
        if scenario == "stale":
            assert reader.execute(
                "SELECT acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=55"
            ).fetchone() == (1,)
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT name,username FROM entities WHERE id=88").fetchone() == (
            ("Manager", "manager")
            if kind == "manager" and scenario not in {"revoked", "error_aba", "mid_commit", "flood_mid_failure"}
            else ("Before", "before")
        )
        assert reopened.execute("SELECT name,username FROM entities WHERE id=?", (linked,)).fetchone() == (
            ("Channel New", "channelnew")
            if kind == "personal_channel"
            and scenario not in {"revoked", "error_aba", "mid_commit", "flood_mid_failure"}
            else ("Old Channel", "oldchannel")
        )
        assert read_dialog_identities(reopened, [88, linked])[88].name == (
            "Manager"
            if kind == "manager" and scenario not in {"revoked", "error_aba", "mid_commit", "flood_mid_failure"}
            else "Before"
        )
        assert read_dialog_identities(reopened, [linked])[linked].name == (
            "Channel New"
            if kind == "personal_channel"
            and scenario not in {"revoked", "error_aba", "mid_commit", "flood_mid_failure"}
            else "Old Channel"
        )


@pytest.mark.parametrize("scenario", ["normal", "marker_revoked", "token_revoked"])
async def test_command_full_user_retains_referenced_channel_without_enrollment(tmp_path: Path, scenario: str) -> None:
    from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
    from mcp_telegram.contact_notes.intake import capture_contact_note_intake
    from mcp_telegram.image_authorization import ScopeCell
    from tests.test_contact_note_profile_seam import _CommandClient, _CommandGateway, _ScopedClient
    from tests.test_entity_profile_full_user_pair import _pair_service

    path = tmp_path / "command-profile.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            EntityProfileRepository(conn, section_ttl_seconds=300).save_core(
                {"id": 7, "type": "user", "name": "Target"}, now=100
            )

    await writer.run(seed)
    full = types.UserFull(7, types.PeerSettings(), types.PeerNotifySettings(), 0, personal_channel_id=200)
    channel = types.Channel(200, "Personal", types.ChatPhotoEmpty(), None, access_hash=222, username="personal")
    response = types.users.UserFull(full, [channel], [types.User(7, first_name="Target", contact=True)])
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    cell = ScopeCell()
    assert cell.publish(scope)
    client = _ScopedClient()
    client.capture_image_scope, client.claim_image_scope = cell.capture, cell.claim

    class CommandClient(_CommandClient):
        async def __call__(self, request: object) -> object:
            result = await super().__call__(request)
            if scenario == "marker_revoked":
                capture_contact_note_intake([types.UpdateUser(7)])
            elif scenario == "token_revoked":
                change = cell.begin_change()
                cell.end_change(change)
                assert cell.publish(scope)
            return result

    reader = sqlite3.connect(path)
    service = cast(DaemonEntityInfoService, _pair_service(reader, client, enabled=True))
    service._deps = replace(
        service._deps,
        writer=writer,
        full_user_auth_scope=lambda: scope,
        user_profile_port=_CommandGateway(CommandClient(response), now_provider=lambda: 100),
    )
    linked = -1000000000200
    try:
        await service.acquire_command_profile(7)
        expected = ("Personal", "personal") if scenario == "normal" else None
        assert reader.execute("SELECT name,username FROM entities WHERE id=?", (linked,)).fetchone() == expected
        assert reader.execute("SELECT 1 FROM synced_dialogs WHERE dialog_id=?", (linked,)).fetchone() is None
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert read_dialog_identities(reopened, [linked])[linked].name == ("Personal" if scenario == "normal" else None)
