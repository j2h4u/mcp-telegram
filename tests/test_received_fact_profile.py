"""Full profile responses publish supplied local dialog facts."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_entity_info import _AuthScopeUnavailableError
from mcp_telegram.dialog_fact_observations import claim_dialog_fact
from mcp_telegram.dialog_identity_contracts import DialogIdentityObservation
from mcp_telegram.entity_profile.contracts import ProfileFieldObservation, SuppliedDialogFacts
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, RefreshLimits
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import TelethonChannelProfileGateway, TelethonGroupProfileGateway
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_channel_profile_telegram_gateway import _Client, _full_channel, _reference
from tests.test_entity_profile_group_full_chat_pair import _full_chat, _GroupClient
from tests.test_entity_profile_progressive import _test_service


@pytest.mark.parametrize("kind", ["group", "channel"])
@pytest.mark.parametrize("identity_mode", ["full", "min", "clear"])
@pytest.mark.parametrize(("race", "revoke"), [(False, False), (True, False), (False, True)])
async def test_profile_response_updates_supplied_facts(  # noqa: PLR0913, PLR0915
    tmp_path: Path, kind: str, race: bool, revoke: bool, identity_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "profile.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    dialog_id = -123 if kind == "group" else -1000000000123

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (?,?,'Target',1)", (dialog_id, kind))
            conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES (77,'user','Old','old',1)")
            conn.execute(
                "INSERT INTO dialogs(dialog_id,name,type,unread_count) VALUES (?,'Target',?,1)", (dialog_id, kind)
            )
            conn.execute(
                "INSERT INTO synced_dialogs(dialog_id,status,read_inbox_max_id,read_outbox_max_id) VALUES (?,'synced',10,9)",
                (dialog_id,),
            )
            conn.execute(
                "INSERT INTO dialog_directory_facts(dialog_id,mute_until,observed_at) VALUES (?,44,1)", (dialog_id,)
            )
            repo = EntityProfileRepository(conn, section_ttl_seconds=300)
            repo.mark_pending(dialog_id, now=100)
            conn.execute("UPDATE entity_profile_refresh_state SET acquisition_cursor=1 WHERE entity_id=?", (dialog_id,))

    await writer.run(seed)
    reader = sqlite3.connect(path)
    service = _test_service(reader, limits=RefreshLimits(), account_id=42)
    auth_client = _GroupClient(_full_chat())
    response = _full_chat() if kind == "group" else _full_channel()
    response.users.append(
        types.User(
            77, first_name="New", username="new" if identity_mode == "full" else None, min=identity_mode == "min"
        )
    )
    response.users.append(types.User(88, first_name="Unknown"))
    expected_sidecar = (
        ("Later", "later") if race else ("New", {"full": "new", "min": "old", "clear": None}[identity_mode])
    )
    full = cast(types.ChatFull | types.ChannelFull, response.full_chat)
    full.notify_settings = types.PeerNotifySettings(
        mute_until=datetime.fromtimestamp(0, tz=UTC) if identity_mode == "clear" else datetime(2030, 1, 1, tzinfo=UTC)
    )
    if isinstance(full, types.ChannelFull):
        full.read_inbox_max_id, full.read_outbox_max_id, full.unread_count = 41, 39, 7

    class GatewayClient(_Client):
        async def __call__(self, request: object) -> object:
            if race:

                def newer(conn: sqlite3.Connection) -> None:
                    with write_transaction(conn):
                        order = allocate_observation_order(conn)
                        for peer, field in (
                            (dialog_id, "name"),
                            (dialog_id, "mute_until"),
                            (dialog_id, "unread_count"),
                            (77, "name"),
                            (77, "username"),
                        ):
                            assert claim_dialog_fact(conn, peer, field, order)
                        conn.execute("UPDATE entities SET name='Newer' WHERE id=?", (dialog_id,))
                        conn.execute("UPDATE entities SET name='Later',username='later' WHERE id=77")
                        conn.execute("UPDATE dialogs SET name='Newer',unread_count=3 WHERE dialog_id=?", (dialog_id,))
                        conn.execute("UPDATE dialog_directory_facts SET mute_until=123 WHERE dialog_id=?", (dialog_id,))

                await writer.run(newer)
            return await super().__call__(request)

    class Reference:
        def get_channel_reference(self, entity_id: int):
            assert entity_id == dialog_id
            return _reference()

    service._deps = replace(
        service._deps,
        writer=writer,
        client=auth_client,
        group_profile_port=TelethonGroupProfileGateway(GatewayClient(response), now_provider=lambda: 100),
        channel_profile_port=TelethonChannelProfileGateway(GatewayClient(response), now_provider=lambda: 100),
        channel_reference_provider=Reference(),
    )
    if revoke:
        original_publish = EntityProfileRepository.publish_supplied_identity

        def revoke_during_publish(
            repo: EntityProfileRepository, identity: DialogIdentityObservation, observation: ProfileFieldObservation
        ) -> None:
            original_publish(repo, identity, observation)
            change = auth_client.scope_cell.begin_change()
            auth_client.scope_cell.end_change(change)
            assert auth_client.scope_cell.publish(auth_client_scope)

        token = auth_client.scope_cell.capture()
        assert token is not None
        auth_client_scope = token.scope
        monkeypatch.setattr(EntityProfileRepository, "publish_supplied_identity", revoke_during_publish)

    try:
        assert service.refresh_coordinator is not None
        if revoke:
            with pytest.raises(_AuthScopeUnavailableError):
                await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
            assert reader.execute("SELECT name,username FROM entities WHERE id=77").fetchone() == ("Old", "old")
            assert reader.execute(
                "SELECT mute_until FROM dialog_directory_facts WHERE dialog_id=?", (dialog_id,)
            ).fetchone() == (44,)
            return
        await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
        assert reader.execute("SELECT name,username FROM entities WHERE id=77").fetchone() == (expected_sidecar)
        assert reader.execute(
            "SELECT mute_until FROM dialog_directory_facts WHERE dialog_id=?", (dialog_id,)
        ).fetchone() == ((123,) if race else (0 if identity_mode == "clear" else 1893456000,))
        assert reader.execute("SELECT 1 FROM entities WHERE id=88").fetchone() is None
        if race:
            assert reader.execute("SELECT name FROM entities WHERE id=?", (dialog_id,)).fetchone() == ("Newer",)
        if kind == "channel":
            assert reader.execute(
                "SELECT read_inbox_max_id,read_outbox_max_id FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
            ).fetchone() == (41, 39)
            assert reader.execute("SELECT unread_count FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
                (3,) if race else (7,)
            )
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT name,username FROM entities WHERE id=77").fetchone() == (expected_sidecar)


async def test_rejected_profile_commit_rolls_back_field_fences(tmp_path: Path) -> None:
    from mcp_telegram.daemon_entity_info import _ProfileSectionContext
    from mcp_telegram.entity_profile.repository import EntitySectionCommit

    path = tmp_path / "rejected.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute("INSERT INTO dialog_directory_facts(dialog_id,mute_until,observed_at) VALUES (77,0,1)")
            conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (77,'user','Existing',1)")
            EntityProfileRepository(conn, section_ttl_seconds=300).mark_pending(77, now=100)

    await writer.run(seed)
    reader = sqlite3.connect(path)
    service = _test_service(reader, limits=RefreshLimits(), account_id=42)
    client = _GroupClient(_full_chat())
    service._deps = replace(service._deps, writer=writer, client=client)
    token = client.scope_cell.capture()
    assert token is not None
    context = _ProfileSectionContext(token.scope, 0, False, "disabled", False)
    context.received_scope = token
    context.received_observation = ProfileFieldObservation(42, 1, 100, "rpc")
    cursor = service._profiles.next_due_refresh(now=100)
    assert cursor is not None
    stale_cursor = replace(cursor, generation=cursor.generation + 1)

    def reject(profiles: EntityProfileRepository) -> bool:
        assert claim_dialog_fact(profiles._conn, 77, "name", 1)
        return profiles.commit_section(stale_cursor, EntitySectionCommit({}), now=100)

    try:
        assert not await service._write_profiles(
            reject,
            received_intake=(
                77,
                EntitySectionCommit({}, supplied_dialog_facts=SuppliedDialogFacts(mute_until=123)),
                context,
            ),
        )
        assert reader.execute(
            "SELECT COUNT(*) FROM dialog_fact_observations WHERE dialog_id=77 AND field='name'"
        ).fetchone() == (0,)
        assert reader.execute("SELECT mute_until FROM dialog_directory_facts WHERE dialog_id=77").fetchone() == (123,)
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()


@pytest.mark.parametrize("kind", ["group", "channel"])
@pytest.mark.parametrize("outcome", ["error", "scope_changed_success"])
async def test_same_account_lease_revocation_does_not_publish_old_profile_failure(
    tmp_path: Path, outcome: str, kind: str
) -> None:
    path = tmp_path / "failed-profile.db"
    dialog_id = -123 if kind == "group" else -1000000000123
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (?,?,'Existing',1)", (dialog_id, kind))
            EntityProfileRepository(conn, section_ttl_seconds=300).mark_pending(dialog_id, now=100)
            conn.execute("UPDATE entity_profile_refresh_state SET acquisition_cursor=1 WHERE entity_id=?", (dialog_id,))

    await writer.run(seed)
    reader = sqlite3.connect(path)
    service = _test_service(reader, limits=RefreshLimits(), account_id=42)
    client = _GroupClient(_full_chat())
    token = client.scope_cell.capture()
    assert token is not None

    class FailingGateway:
        async def __call__(self, request: object) -> object:
            del request
            change = client.scope_cell.begin_change()
            client.scope_cell.end_change(change)
            if outcome == "error":
                assert client.scope_cell.publish(token.scope)
                raise OSError("fixture failure")
            next_scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100)
            assert client.scope_cell.publish(next_scope)
            service._deps = replace(service._deps, full_user_auth_scope=lambda: next_scope)
            return _full_chat() if kind == "group" else _full_channel()

    class Reference:
        def get_channel_reference(self, entity_id: int):
            assert entity_id == dialog_id
            return _reference()

    service._deps = replace(
        service._deps,
        writer=writer,
        client=client,
        group_profile_port=TelethonGroupProfileGateway(FailingGateway(), now_provider=lambda: 100),
        channel_profile_port=TelethonChannelProfileGateway(FailingGateway(), now_provider=lambda: 100),
        channel_reference_provider=Reference(),
    )
    try:
        assert service.refresh_coordinator is not None
        if outcome == "error" and kind == "group":
            with pytest.raises(OSError):
                await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
        else:
            await run_ready_slice(EntityProfileDemandAdapter(service.refresh_coordinator))
        assert reader.execute(
            "SELECT status,reason FROM entity_profile_refresh_state WHERE entity_id=?", (dialog_id,)
        ).fetchone() == ("pending", "refresh_queued")
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()
