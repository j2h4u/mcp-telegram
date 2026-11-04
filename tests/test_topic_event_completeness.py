"""Regression tests for raw topic service events and complete pin sets."""

# pyright: reportAny=false, reportArgumentType=false, reportOptionalSubscript=false, reportOperatorIssue=false, reportUndefinedVariable=false, reportMissingParameterType=false, reportReturnType=false, reportInvalidTypeForm=false, reportGeneralTypeIssues=false

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon import events
from telethon.tl.types import (
    Message,
    MessageActionTopicCreate,
    MessageActionTopicEdit,
    MessageReplyHeader,
    MessageService,
    PeerChannel,
    UpdateNewChannelMessage,
    UpdateNewMessage,
    UpdatePinnedForumTopic,
    UpdatePinnedForumTopics,
    User,
)

from mcp_telegram.event_handlers import EventHandlerManager
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.tools.discovery import _structured_topic
from mcp_telegram.topics.catalog import TopicCatalogRepository
from mcp_telegram.topics.contracts import TopicBatch, TopicFact, TopicPage
from mcp_telegram.topics.sqlite_repository import SQLiteTopicSnapshotRepository
from sqlite_writer_helpers import event_manager, inline_writer
from tests.history_enrollment_helpers import seed_full_history_enrollment
from tests.topic_test_helpers import seed_topic_account, topic_scope


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    ensure_sync_schema(tmp_path / "sync.db")
    conn = cast(sqlite3.Connection, _open_sync_db(tmp_path / "sync.db"))
    yield conn
    conn.close()


@pytest.fixture()
def client() -> MagicMock:
    return MagicMock()


def _manager(client: MagicMock, db: sqlite3.Connection) -> EventHandlerManager:
    dialog_id = -1000000000123
    db.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, 'synced')", (dialog_id,))
    seed_full_history_enrollment(db, dialog_id, enabled=True)
    db.commit()
    manager = event_manager(client, db, asyncio.Event())
    manager.register()
    return manager


def _service(message_id: int, peer_id: object, action: object, reply_to: object = None) -> MessageService:
    return MessageService(
        id=message_id,
        peer_id=peer_id,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        action=action,
        reply_to=reply_to,
    )


def _topic_rows(db: sqlite3.Connection, dialog_id: int = -1000000000123) -> dict[int, tuple[object, ...]]:
    return {
        int(row[0]): tuple(row)
        for row in db.execute(
            "SELECT topic_id, title, icon_emoji_id, pinned, hidden FROM topic_metadata WHERE dialog_id=?",
            (dialog_id,),
        ).fetchall()
    }


def _seed_topics(db: sqlite3.Connection, *topic_ids: int) -> None:
    db.executemany(
        "INSERT INTO topic_metadata(dialog_id, topic_id, title, is_general, is_deleted, updated_at, pinned, hidden) "
        "VALUES(-1000000000123, ?, 'topic', 0, 0, 1, ?, 0)",
        [(topic_id, int(topic_id % 2 == 0)) for topic_id in topic_ids],
    )
    db.commit()


def test_new_message_builder_drops_service_update() -> None:
    message = _service(7, PeerChannel(123), MessageActionTopicCreate("x", 0))
    update = UpdateNewMessage(message=message, pts=1, pts_count=1)

    assert events.NewMessage.build(update) is None


@pytest.mark.asyncio
async def test_raw_service_create_and_edit_use_peer_id_and_prefer_top_target(
    client: MagicMock, db: sqlite3.Connection
) -> None:
    manager = _manager(client, db)
    create = _service(10, PeerChannel(123), MessageActionTopicCreate("old", 0, icon_emoji_id=4))
    await manager.on_raw_topic_message(UpdateNewChannelMessage(message=create, pts=1, pts_count=1))

    reply = MessageReplyHeader(reply_to_msg_id=10, reply_to_top_id=999)
    edit = _service(20, PeerChannel(123), MessageActionTopicEdit(title="new"), reply)
    # Channel peer 123 is -1000000000123, so seed under Telethon's marked id.
    dialog_id = -1000000000123
    await manager.on_raw_topic_message(UpdateNewChannelMessage(message=edit, pts=2, pts_count=1))

    rows = _topic_rows(db, dialog_id)
    assert rows[10][1:] == ("old", 4, None, None)
    assert rows[999][1:] == ("new", None, None, None)


@pytest.mark.asyncio
async def test_raw_topic_events_ignore_nonservice_unknown_and_coverage_denied(
    client: MagicMock, db: sqlite3.Connection
) -> None:
    manager = event_manager(client, db, asyncio.Event())
    manager.register()
    peer = PeerChannel(123)
    regular = Message(id=1, peer_id=peer, message="hello", date=datetime.now(UTC), out=False)
    await manager.on_raw_topic_message(UpdateNewMessage(message=regular, pts=1, pts_count=1))
    await manager.on_raw_topic_message(
        UpdateNewMessage(message=_service(2, peer, MessageActionTopicCreate("ignored", 0)), pts=2, pts_count=1)
    )
    assert _topic_rows(db, -1000000000123) == {}


def test_raw_topic_handler_registration_is_symmetric(client: MagicMock, db: sqlite3.Connection) -> None:
    manager = event_manager(client, db, asyncio.Event())
    manager.register()
    raw_calls = [
        call for call in client.add_event_handler.call_args_list if call.args[0].__name__ == "on_raw_topic_message"
    ]
    assert len(raw_calls) == 1
    assert isinstance(raw_calls[0].args[1], events.Raw)
    raw_builder = raw_calls[0].args[1]
    assert set(raw_builder.types) == {UpdateNewMessage, UpdateNewChannelMessage}
    for update_type in (UpdateNewMessage, UpdateNewChannelMessage):
        update = update_type(
            message=_service(8, PeerChannel(123), MessageActionTopicCreate("x", 0)), pts=1, pts_count=1
        )
        assert raw_builder.build(update) is update
        assert raw_builder.filter(update) is update
    manager.unregister()
    assert any(call.args[0].__name__ == "on_raw_topic_message" for call in client.remove_event_handler.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [None, "bad", [1, None]])
async def test_plural_pin_malformed_or_none_is_noop(client: MagicMock, db: sqlite3.Connection, order: object) -> None:
    manager = _manager(client, db)
    _seed_topics(db, 1, 2)
    before = _topic_rows(db)
    update = UpdatePinnedForumTopics(peer=PeerChannel(123), order=cast(list[int] | None, order))
    await manager.on_raw_forum_topic_pinned(update)
    assert _topic_rows(db) == before


@pytest.mark.asyncio
async def test_plural_pin_membership_clear_and_unknown_do_not_create(client: MagicMock, db: sqlite3.Connection) -> None:
    manager = _manager(client, db)
    _seed_topics(db, 1, 2, 3)
    await manager.on_raw_forum_topic_pinned(UpdatePinnedForumTopics(peer=PeerChannel(123), order=[2, 999]))
    rows = _topic_rows(db)
    assert [rows[id][3] for id in (1, 2, 3)] == [0, 1, 0]
    await manager.on_raw_forum_topic_pinned(UpdatePinnedForumTopics(peer=PeerChannel(123), order=[]))
    assert all(row[3] == 0 for row in _topic_rows(db).values())
    assert _topic_rows(db)[999][3] == 0


@pytest.mark.asyncio
async def test_singular_pin_and_plural_missing_peer_are_safe(client: MagicMock, db: sqlite3.Connection) -> None:
    manager = _manager(client, db)
    _seed_topics(db, 1)
    await manager.on_raw_forum_topic_pinned(UpdatePinnedForumTopic(peer=PeerChannel(123), topic_id=1, pinned=True))
    assert _topic_rows(db)[1][3] == 1

    missing_peer = MagicMock(peer=None, order=[])
    await manager.on_raw_forum_topics_pinned(missing_peer)
    assert _topic_rows(db)[1][3] == 1


def test_snapshot_upsert_preserves_realtime_pin_state(db: sqlite3.Connection) -> None:
    _seed_topics(db, 1)
    db.execute("UPDATE topic_metadata SET pinned=1 WHERE dialog_id=-1000000000123 AND topic_id=1")
    db.commit()

    SQLiteTopicSnapshotRepository(db).upsert_topics(
        -1000000000123,
        (TopicFact(topic_id=1, title="refreshed"),),
    )

    assert _topic_rows(db)[1][1:] == ("refreshed", None, 1, 0)


def test_topic_hidden_false_and_other_fields_apply_at_same_second(db: sqlite3.Connection) -> None:
    from mcp_telegram.sync_transactions import write_transaction

    repository = SQLiteTopicSnapshotRepository(db)
    _seed_topics(db, 1)
    with write_transaction(db):
        repository.apply_topic_edit(-1000000000123, 1, title="hidden", icon_emoji_id=7, hidden=True, observed_at=100)
        repository.apply_topic_edit(-1000000000123, 1, title="visible", icon_emoji_id=8, hidden=False, observed_at=100)
    assert _topic_rows(db)[1][1:] == ("visible", 8, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["create", "edit", "hide", "pin", "pins"])
async def test_topic_snapshot_captured_before_fetch_cannot_replace_realtime(
    db: sqlite3.Connection,
    mutation: str,
) -> None:
    from types import SimpleNamespace

    from mcp_telegram.sync_transactions import write_transaction
    from mcp_telegram.topics.refresh import TopicRefresher

    repository = SQLiteTopicSnapshotRepository(db)
    _seed_topics(db, 1)

    class Gateway:
        async def fetch_topics(self, entity: object) -> TopicBatch:
            with write_transaction(db):
                if mutation == "create":
                    repository.apply_topic_create(
                        -1000000000123, 1, title="realtime", icon_emoji_id=8, date=100, observed_at=100
                    )
                elif mutation in {"edit", "hide"}:
                    repository.apply_topic_edit(
                        -1000000000123, 1, title="realtime", icon_emoji_id=8, hidden=mutation == "hide", observed_at=100
                    )
                elif mutation == "pin":
                    repository.apply_topic_pin(-1000000000123, 1, pinned=True, observed_at=100)
                else:
                    repository.apply_topic_pins(-1000000000123, (1,), observed_at=100)
            return TopicBatch((TopicFact(topic_id=1, title="stale snapshot", icon_emoji_id=5),), ())

    seed_topic_account(db, -1000000000123)
    scope = topic_scope()
    await TopicRefresher(
        Gateway(), repository, writer=inline_writer(db), capture_scope=scope.capture, claim_scope=scope.claim
    ).refresh(-1000000000123, SimpleNamespace(forum=True))
    row = _topic_rows(db)[1]
    assert row[1] == ("realtime" if mutation in {"create", "edit", "hide"} else "stale snapshot")
    if mutation in {"pin", "pins"}:
        assert row[3] == 1
    if mutation == "hide":
        assert row[4] == 1


@pytest.mark.asyncio
async def test_full_topic_snapshot_persists_supplied_message_for_local_reads(db: sqlite3.Connection) -> None:
    from telethon.tl import types
    from telethon.tl.functions.messages import GetForumTopicsRequest

    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
    from mcp_telegram.topics.refresh import TopicRefresher
    from mcp_telegram.topics.telegram_adapter import TelethonTelegramTopicGateway
    from tests.test_daemon_api import _response_messages, make_server

    dialog_id = -1000000000123
    seed_topic_account(db, dialog_id)
    db.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (?,'synced')", (dialog_id,))
    db.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES (?,'Forum','supergroup')", (dialog_id,))
    db.commit()
    date = datetime(2026, 1, 1, tzinfo=UTC)
    topic = types.ForumTopic(
        id=7,
        date=date,
        peer=types.PeerChannel(123),
        title="Visible topic",
        icon_color=6,
        top_message=10,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=types.PeerUser(77),
        notify_settings=types.PeerNotifySettings(),
    )
    message = types.Message(
        id=10, peer_id=types.PeerChannel(123), date=date, message="snapshot needle", from_id=types.PeerUser(77)
    )
    response = types.messages.ForumTopics(1, [topic], [message], [], [types.User(77, first_name="Sender")], 1)

    class Client:
        calls = 0

        async def get_input_entity(self, _entity: object) -> object:
            return types.InputPeerChannel(123, 9)

        async def get_entity(self, _entity: object) -> object:
            raise AssertionError("unexpected entity acquisition")

        async def __call__(self, request: object) -> object:
            assert isinstance(request, GetForumTopicsRequest)
            self.calls += 1
            return response

    client = Client()
    scope = topic_scope()
    writer = await SQLiteWriter.open(Path(db.execute("PRAGMA database_list").fetchone()[2]))
    feedback = sqlite3.connect(":memory:")
    try:
        with rpc_scope(TelegramRpcSource.TOPIC_RECONCILIATION):
            count = await TopicRefresher(
                TelethonTelegramTopicGateway(client),
                SQLiteTopicSnapshotRepository(db),
                writer=writer,
                capture_scope=scope.capture,
                claim_scope=scope.claim,
            ).refresh(dialog_id, SimpleNamespace(forum=True))
        assert count == 1 and client.calls == 1
        assert db.execute(
            "SELECT title FROM topic_metadata WHERE dialog_id=? AND topic_id=7", (dialog_id,)
        ).fetchone() == ("Visible topic",)
        assert db.execute("SELECT text FROM messages WHERE dialog_id=? AND message_id=10", (dialog_id,)).fetchone() == (
            "snapshot needle",
        )
        assert db.execute(
            "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'needle' AND dialog_id=?", (dialog_id,)
        ).fetchone() == (10,)
        daemon = make_server(db, client, feedback_conn=feedback, writer=writer)
        listed = await daemon._list_messages({"dialog_id": dialog_id, "limit": 10})
        searched = await daemon._search_messages({"dialog_id": dialog_id, "query": "needle", "limit": 10})
        assert [item["text"] for item in _response_messages(listed)] == ["snapshot needle"]
        assert [item["text"] for item in _response_messages(searched)] == ["snapshot needle"]
        assert client.calls == 1
    finally:
        feedback.close()
        await writer.aclose()


def test_reserved_topic_event_order_rejects_older_event(db: sqlite3.Connection) -> None:
    from mcp_telegram.sync_transactions import write_transaction

    repository = SQLiteTopicSnapshotRepository(db)
    _seed_topics(db, 1)
    older = repository.begin_snapshot()
    newer = repository.begin_snapshot()
    with write_transaction(db):
        repository.apply_topic_edit(
            -1000000000123, 1, title="newer", icon_emoji_id=None, hidden=None, observed_at=100, observation_order=newer
        )
        repository.apply_topic_edit(
            -1000000000123, 1, title="older", icon_emoji_id=None, hidden=None, observed_at=100, observation_order=older
        )
    assert _topic_rows(db)[1][1] == "newer"


@pytest.mark.asyncio
async def test_supplied_bot_capability_change_reopens_catalog_without_rpc(db: sqlite3.Connection) -> None:
    seed_full_history_enrollment(db, 42, enabled=True)
    db.commit()
    client = MagicMock()
    manager = event_manager(client, db, asyncio.Event())
    manager.set_self_id(1)
    sink = MagicMock()
    manager.bind_demand_sink(sink)
    ordinary = SimpleNamespace(_entities={42: User(42, first_name="Bot", bot=True, bot_forum_view=False)})

    await manager.on_raw_identity_or_notify(ordinary)
    assert TopicCatalogRepository(db).coverage(42)["coverage"] == "not_applicable"
    enabled = SimpleNamespace(_entities={42: User(42, first_name="Bot", bot=True, bot_forum_view=True)})
    await manager.on_raw_identity_or_notify(enabled)

    assert TopicCatalogRepository(db).coverage(42)["pending"] is True
    assert any(call.args == (DemandKind.TOPIC_SNAPSHOT,) for call in sink.offer.call_args_list)
    previous = list(sink.offer.call_args_list)
    await manager.on_raw_identity_or_notify(enabled)
    assert sink.offer.call_args_list == previous
    client.assert_not_called()


@pytest.mark.asyncio
async def test_min_capability_bundle_does_not_turn_unknown_into_not_applicable(db: sqlite3.Connection) -> None:
    seed_full_history_enrollment(db, 42, enabled=True)
    db.commit()
    client = MagicMock()
    manager = event_manager(client, db, asyncio.Event())
    manager.set_self_id(1)
    manager.bind_demand_sink(MagicMock())

    await manager.on_raw_identity_or_notify(SimpleNamespace(_entities={42: User(42, min=True, first_name="Partial")}))

    assert TopicCatalogRepository(db).coverage(42)["coverage"] != "not_applicable"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_delivered_partial_topics_are_retained_and_locally_visible(db, client):
    dialog = -1000000000123
    db.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (?, 'synced')", (dialog,))
    seed_full_history_enrollment(db, dialog, enabled=True)
    db.commit()
    manager = event_manager(client, db, asyncio.Event())
    manager.register()
    peer = PeerChannel(123)
    create = MessageService(
        id=10, peer_id=peer, date=datetime(2026, 1, 1, tzinfo=UTC), action=MessageActionTopicCreate("topic", 0x6FB9F0)
    )
    await manager.on_raw_topic_message(UpdateNewChannelMessage(message=create, pts=1, pts_count=1))
    edit = MessageService(
        id=20,
        peer_id=peer,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        action=MessageActionTopicEdit(title="new", hidden=True),
        reply_to=MessageReplyHeader(reply_to_msg_id=99),
    )
    await manager.on_raw_topic_message(UpdateNewChannelMessage(message=edit, pts=2, pts_count=1))
    await manager.on_raw_forum_topic_pinned(UpdatePinnedForumTopic(peer=peer, topic_id=98, pinned=True))
    assert db.execute(
        "SELECT title,icon_color FROM topic_metadata WHERE dialog_id=? AND topic_id=10", (dialog,)
    ).fetchone() == ("topic", 0x6FB9F0)
    assert db.execute(
        "SELECT title,hidden FROM topic_metadata WHERE dialog_id=? AND topic_id=99", (dialog,)
    ).fetchone() == ("new", 1)
    assert db.execute(
        "SELECT title,pinned,hidden FROM topic_metadata WHERE dialog_id=? AND topic_id=98", (dialog,)
    ).fetchone() == (None, 1, None)
    assert _structured_topic({"id": 98, "title": None, "pinned": True}) == {
        "topic_id": 98,
        "title": None,
        "pinned": True,
    }
    assert TopicCatalogRepository(db).coverage(dialog)["coverage"] == "partial"


def test_new_pin_does_not_prevent_old_page_filling_unknown_fields(db):
    seed_full_history_enrollment(db, 42, enabled=True)
    db.commit()
    catalog = TopicCatalogRepository(db)
    catalog.request(42, "bootstrap")
    scan = catalog.begin_page(42)
    assert scan is not None
    with write_transaction(db):
        SQLiteTopicSnapshotRepository(db).apply_topic_pin(42, 7, pinned=True, observed_at=100)
    assert catalog.apply_page(scan, TopicPage((TopicFact(7, "title", icon_emoji_id=5),), 1, False, None))
    assert db.execute(
        "SELECT title,icon_emoji_id,pinned,hidden FROM topic_metadata WHERE dialog_id=42 AND topic_id=7"
    ).fetchone() == ("title", 5, 1, None)


@pytest.mark.asyncio
async def test_partial_pin_reopens_and_reaches_registered_mcp(db, client, monkeypatch):
    from contextlib import asynccontextmanager

    from mcp_telegram import server
    from tests.test_daemon_api import make_server

    dialog = -1000000000123
    manager = _manager(client, db)
    await manager.on_raw_forum_topic_pinned(UpdatePinnedForumTopic(peer=PeerChannel(123), topic_id=55, pinned=True))
    hidden = _service(
        99,
        PeerChannel(123),
        MessageActionTopicEdit(title="hidden", hidden=True),
        MessageReplyHeader(reply_to_msg_id=56),
    )
    await manager.on_raw_topic_message(UpdateNewChannelMessage(message=hidden, pts=2, pts_count=1))
    path = db.execute("PRAGMA database_list").fetchone()[2]
    reopened = sqlite3.connect(path)
    feedback = sqlite3.connect(":memory:")
    daemon = make_server(reopened, client, feedback_conn=feedback)

    class LocalConnection:
        async def list_topics(self, **request):
            return await daemon._list_topics(request)

    @asynccontextmanager
    async def connect():
        yield LocalConnection()

    monkeypatch.setattr("mcp_telegram.tools.discovery.daemon_connection", connect)
    try:
        result = await server.call_tool("list_topics", {"exact_dialog_id": dialog})
        assert result.is_error is False
        topic = result.structured_content["topics"][0]
        assert topic["topic_id"] == 55
        assert topic["title"] is None
        assert "title_content" not in topic
        assert topic["pinned"] is True
        assert "hidden" not in topic
        hidden_topic = result.structured_content["topics"][1]
        assert hidden_topic["topic_id"] == 56
        assert hidden_topic["hidden"] is True
        assert hidden_topic["title"] == "hidden"
        assert result.structured_content["catalog"]["coverage"] == "partial"
        client.get_entity.assert_not_called()
        client.get_messages.assert_not_called()
    finally:
        feedback.close()
        reopened.close()
