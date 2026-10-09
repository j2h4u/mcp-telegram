"""Regression tests for raw topic service events and complete pin sets."""

# pyright: reportAny=false, reportArgumentType=false, reportOptionalSubscript=false, reportOperatorIssue=false, reportUndefinedVariable=false, reportMissingParameterType=false, reportReturnType=false, reportInvalidTypeForm=false, reportGeneralTypeIssues=false

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
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
)

from mcp_telegram.event_handlers import EventHandlerManager
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.topics.contracts import TopicFact
from mcp_telegram.topics.sqlite_repository import SQLiteTopicSnapshotRepository
from tests.history_enrollment_helpers import seed_full_history_enrollment


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
    manager = EventHandlerManager(client, db, asyncio.Event())
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
    assert rows[10][1:] == ("old", 4, 0, 0)
    assert 999 not in rows


@pytest.mark.asyncio
async def test_raw_topic_events_ignore_nonservice_unknown_and_coverage_denied(
    client: MagicMock, db: sqlite3.Connection
) -> None:
    manager = EventHandlerManager(client, db, asyncio.Event())
    manager.register()
    peer = PeerChannel(123)
    regular = Message(id=1, peer_id=peer, message="hello", date=datetime.now(UTC), out=False)
    await manager.on_raw_topic_message(UpdateNewMessage(message=regular, pts=1, pts_count=1))
    await manager.on_raw_topic_message(
        UpdateNewMessage(message=_service(2, peer, MessageActionTopicCreate("ignored", 0)), pts=2, pts_count=1)
    )
    assert _topic_rows(db, -1000000000123) == {}


def test_raw_topic_handler_registration_is_symmetric(client: MagicMock, db: sqlite3.Connection) -> None:
    manager = EventHandlerManager(client, db, asyncio.Event())
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
    assert 999 not in _topic_rows(db)


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
        async def fetch_topics(self, entity: object) -> tuple[TopicFact, ...]:
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
            return (TopicFact(topic_id=1, title="stale snapshot", icon_emoji_id=5),)

    await TopicRefresher(Gateway(), repository).refresh(-1000000000123, SimpleNamespace(forum=True))
    row = _topic_rows(db)[1]
    assert row[1] == ("realtime" if mutation in {"create", "edit", "hide"} else "topic")
    if mutation in {"pin", "pins"}:
        assert row[3] == 1
    if mutation == "hide":
        assert row[4] == 1


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
