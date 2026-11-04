"""Chat-photo search messages flow through the profile owner into canonical storage."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import types
from telethon.tl.functions.messages import SearchRequest

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.entity_profile.refresh import RefreshLimits
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.models import DialogType
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.reactions.contracts import ReactionAggregate, ReactionAggregateSource
from mcp_telegram.reactions.persistence import replace_reaction_aggregates
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import demand_context
from mcp_telegram.telegram_gateway import TelethonChatAvatarHistoryGateway
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
from tests.test_entity_profile_progressive import _test_service


@pytest.mark.parametrize(
    "scenario", ["accepted", "revoked", "disabled", "stale_cursor", "late_event", "deleted", "wrong_peer", "all_wrong"]
)
async def test_chat_avatar_response_publishes_messages_with_profile_cursor(  # noqa: PLR0912, PLR0914, PLR0915
    tmp_path: Path, scenario: str
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=77 WHERE singleton=1")
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES(-3,'group','Group',1)")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(-3,'synced')")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(-3,?,1)",
            (scenario != "disabled",),
        )
        conn.execute(
            "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,generation) VALUES(-3,'pending',100,'avatar_history',1)"
        )
        if scenario in {"wrong_peer", "all_wrong"}:
            previous = [{"photo_id": 88, "date": None}]
            conn.execute(
                "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES(-3,?,1,77)",
                (json.dumps({"avatar_history": previous, "avatar_count": 1}),),
            )
            conn.execute(
                "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,payload_json) VALUES(-3,'avatar_history','fresh',1,?)",
                (json.dumps(previous),),
            )
    writer = await SQLiteWriter.open(path)
    scope = TelegramAuthScope(1, 77, 2, 99)
    cell = ScopeCell()
    assert cell.publish(scope)
    date = datetime(2026, 1, 1, tzinfo=UTC)
    photo = types.Photo(12, 1, b"", date, [], 1)
    response = types.messages.Messages(
        [
            types.MessageService(
                1, types.PeerChat(3), date, action=types.MessageActionChatEditPhoto(photo), from_id=types.PeerUser(44)
            ),
            types.Message(2, types.PeerChat(3), date, "Avatar search body"),
        ],
        [],
        [],
        [],
    )
    if scenario in {"wrong_peer", "all_wrong"}:
        wrong = types.Message(3, types.PeerChat(4), date, "Foreign body")
        if scenario == "all_wrong":
            response.messages[:] = [wrong]
        else:
            response.messages.append(wrong)

    class Client:
        session = SimpleNamespace(get_input_entity=lambda _entity_id: types.InputPeerChat(3))
        capture_image_scope = staticmethod(cell.capture)
        claim_image_scope = staticmethod(cell.claim)

        async def __call__(self, request: object) -> object:
            assert isinstance(request, SearchRequest)
            if scenario == "revoked":
                change = cell.begin_change()
                cell.end_change(change)
                assert cell.publish(scope)
            if scenario in {"late_event", "deleted"}:

                def newer_event(db: sqlite3.Connection) -> None:
                    event = types.Message(2, types.PeerChat(3), date, "New event")
                    event.media = types.MessageMediaContact("123", "Name", "", "", 7)
                    with write_transaction(db):
                        insert_messages_with_fts(
                            db,
                            [replace(extract_message_row(-3, event), observation_order=allocate_observation_order(db))],
                        )
                        replace_reaction_aggregates(
                            db, -3, 2, [ReactionAggregate("👍", 3)], source=ReactionAggregateSource.RAW_UPDATE
                        )
                        if scenario == "deleted":
                            db.execute("UPDATE messages SET is_deleted=1 WHERE dialog_id=-3 AND message_id=2")

                await writer.run(newer_event)
            return response

    client = Client()
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(
        service._deps,
        writer=writer,
        client=client,
        self_id=77,
        full_user_auth_scope=lambda: scope,
        chat_avatar_history_port=TelethonChatAvatarHistoryGateway(client),
    )
    try:
        cursor = EntityProfileRepository(conn, section_ttl_seconds=300).next_due_refresh(now=100)
        assert cursor is not None
        with demand_context(DemandKind.ENTITY_PROFILE_REFRESH), rpc_scope(TelegramRpcSource.ENTITY_INFO_REFRESH):
            context = service._profile_section_context(cursor, DialogType.GROUP)
            result = await service._acquire_chat_avatar_history(-3)
        if scenario in {"wrong_peer", "all_wrong"}:
            assert result.reason == "inconsistent_peer"
        if scenario == "stale_cursor":

            def invalidate(db: sqlite3.Connection) -> None:
                with write_transaction(db):
                    db.execute("UPDATE entity_profile_refresh_state SET generation=2 WHERE entity_id=-3")

            await writer.run(invalidate)
        with demand_context(DemandKind.ENTITY_PROFILE_REFRESH), rpc_scope(TelegramRpcSource.ENTITY_INFO_REFRESH):
            committed = await service._commit_profile_section_result(cursor, DialogType.GROUP, result, context, now=100)
        rows = conn.execute(
            "SELECT message_id,text,is_service,service_action FROM messages WHERE dialog_id=-3 ORDER BY message_id"
        ).fetchall()
        if scenario in {"accepted", "stale_cursor", "late_event", "deleted", "wrong_peer"}:
            assert committed == (scenario != "stale_cursor")
            assert len(rows) == 2 and rows[0][0:3] == (1, None, 1)
            assert "MessageActionChatEditPhoto" in rows[0][3]
            if scenario in {"accepted", "stale_cursor", "wrong_peer"}:
                assert rows[1][0:3] == (2, "Avatar search body", 0)
                assert conn.execute(
                    "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'search'"
                ).fetchall() == [(2,)]
            else:
                assert rows[1][0:3] == (2, "New event", 0)
                assert conn.execute(
                    "SELECT media_kind,is_deleted FROM messages WHERE dialog_id=-3 AND message_id=2"
                ).fetchone() == ("contact", int(scenario == "deleted"))
                assert conn.execute(
                    "SELECT emoji,count FROM message_reactions WHERE dialog_id=-3 AND message_id=2"
                ).fetchall() == [("👍", 3)]
            assert conn.execute("SELECT sender_id FROM messages WHERE dialog_id=-3 AND message_id=1").fetchone() == (
                44,
            )
            if scenario == "stale_cursor":
                assert conn.execute(
                    "SELECT generation,next_section FROM entity_profile_refresh_state WHERE entity_id=-3"
                ).fetchone() == (2, "avatar_history")
        else:
            assert rows == []
            assert committed == (scenario in {"disabled", "all_wrong"})
        if scenario in {"wrong_peer", "all_wrong"}:
            assert conn.execute("SELECT count(*) FROM messages WHERE dialog_id=-4").fetchone() == (0,)
            assert conn.execute(
                "SELECT status,reason FROM entity_detail_sections WHERE entity_id=-3 AND section='avatar_history'"
            ).fetchone() == ("unavailable", "inconsistent_peer")
            row = cast(tuple[str], conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=-3").fetchone())
            detail = cast(dict[str, object], json.loads(row[0]))
            history = cast(list[dict[str, object]], detail["avatar_history"])
            assert {item["photo_id"] for item in history} == ({12, 88} if scenario == "wrong_peer" else {88})
            profile = EntityProfileRepository(conn, section_ttl_seconds=300).read(-3, now=100)
            assert profile is not None
            section = profile.sections["avatar_history"]
            assert section["status"] == "unavailable" and section["reason"] == "inconsistent_peer"
            assert section["data"] == history
            result = await service.get_entity_info({"entity_id": -3})
            data = cast(dict[str, object], result["data"])
            sections = cast(dict[str, dict[str, object]], data["sections"])
            assert data["avatar_history"] == history
            assert sections["avatar_history"]["reason"] == "inconsistent_peer"
    finally:
        await service.shutdown()
        await writer.aclose()
        conn.close()
