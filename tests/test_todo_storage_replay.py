from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import (
    MessageActionTodoAppendTasks,
    MessageActionTodoCompletions,
    MessageReplyHeader,
    MessageService,
    PeerUser,
    TextWithEntities,
    TodoItem,
    UpdateNewMessage,
)

from mcp_telegram.hydration_queue import HydrationQueueRepository
from mcp_telegram.observation_order import allocate_observation_order, hydration_observation_current
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from sqlite_writer_helpers import event_manager
from tests.history_enrollment_helpers import seed_full_history_enrollment


@pytest.mark.asyncio
async def test_raw_todo_service_fences_inflight_snapshot_and_preserves_parent(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(7,'synced')")
    seed_full_history_enrollment(conn, 7, enabled=True)
    payload = '{"_":"MessageMediaToDo","todo":{"items":[]}}'
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,10,1,'todo',?)",
        (payload,),
    )
    conn.commit()
    request_order = allocate_observation_order(conn)
    conn.execute(
        "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,attempts,priority,message_sent_at,terminal,last_outcome) VALUES('media_metadata',7,10,1,1,1,1,0,'processing')"
    )
    conn.commit()
    manager = event_manager(MagicMock(), conn, asyncio.Event())
    manager.bind_demand_sink(MagicMock())
    message = MessageService(
        id=11,
        peer_id=PeerUser(7),
        from_id=PeerUser(8),
        date=datetime(2024, 1, 1, tzinfo=UTC),
        action=MessageActionTodoCompletions([1], []),
        reply_to=MessageReplyHeader(reply_to_msg_id=10, reply_to_top_id=99),
    )
    await manager.on_raw_topic_message(UpdateNewMessage(message, 1, 1))
    row = cast(
        tuple[int, int, str] | None,
        conn.execute(
            "SELECT sender_id,reply_to_msg_id,service_action FROM messages WHERE dialog_id=7 AND message_id=11"
        ).fetchone(),
    )
    assert row is not None and row[0:2] == (8, 10)
    assert cast(dict[str, list[int]], json.loads(row[2]))["completed"] == [1]
    assert (
        conn.execute("SELECT media_payload FROM messages WHERE dialog_id=7 AND message_id=10").fetchone()[0] == payload
    )
    assert not hydration_observation_current(conn, 7, 10, request_order)
    assert conn.execute(
        "SELECT attempts,last_outcome FROM hydration_jobs WHERE dialog_id=7 AND message_id=10"
    ).fetchone() == (0, "queued")
    assert HydrationQueueRepository(conn).due_jobs(2**31, 10)[0].message_id == 10
    conn.close()


def test_migration_retains_rich_facts_and_only_normalizes_recognizable_todo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcp_telegram.sync_db as schema

    path = tmp_path / "old.db"
    with monkeypatch.context() as old:
        old.setattr(schema, "_CURRENT_SCHEMA_VERSION", 102)
        ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,rich_message,formatting_text,media_kind,media_payload) VALUES(7,1,1,'retained','{\"_\":\"RichMessage\"}','','other','{\"type\":\"MessageMediaToDo\"}')"
    )
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,2,2,'other','{\"type\":\"Unrelated\"}')"
    )
    conn.execute(
        "INSERT INTO scheduled_messages(dialog_id,message_id,text,rich_message,formatting_text,media_kind,media_payload,first_seen_at,updated_at) "
        "VALUES(7,3,'scheduled','{\"_\":\"RichMessage\"}','','other','{\"type\":\"MessageMediaToDo\"}',1,1)"
    )
    conn.commit()
    before = {
        row[0]
        for row in cast(
            list[tuple[str]],
            conn.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name IN ('messages','scheduled_messages') AND type IN ('index','trigger') AND sql IS NOT NULL"
            ).fetchall(),
        )
    }
    conn.close()
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    assert conn.execute(
        "SELECT text,rich_message,formatting_text,media_kind,media_payload FROM messages WHERE message_id=1"
    ).fetchone() == ("retained", '{"_":"RichMessage"}', "", "todo", "{}")
    assert conn.execute("SELECT media_kind,media_payload FROM messages WHERE message_id=2").fetchone() == (
        "other",
        '{"type":"Unrelated"}',
    )
    assert conn.execute(
        "SELECT text,rich_message,formatting_text,media_kind,media_payload FROM scheduled_messages WHERE message_id=3"
    ).fetchone() == ("scheduled", '{"_":"RichMessage"}', "", "todo", "{}")
    after = {
        row[0]
        for row in cast(
            list[tuple[str]],
            conn.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name IN ('messages','scheduled_messages') AND type IN ('index','trigger') AND sql IS NOT NULL"
            ).fetchall(),
        )
    }
    assert before == after
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        MessageReplyHeader(reply_to_top_id=10, forum_topic=True),
        MessageReplyHeader(reply_to_msg_id=10, reply_to_peer_id=PeerUser(7)),
        MessageReplyHeader(reply_to_msg_id=10, reply_to_scheduled=True),
        MessageReplyHeader(reply_to_msg_id=99),
    ],
)
async def test_ambiguous_parent_keeps_event_without_enqueuing(tmp_path: Path, reply: MessageReplyHeader) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(7,'synced')")
    seed_full_history_enrollment(conn, 7, enabled=True)
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,10,1,'todo','{}')"
    )
    conn.commit()
    manager = event_manager(MagicMock(), conn, asyncio.Event())
    manager.bind_demand_sink(MagicMock())
    message = MessageService(
        id=11,
        peer_id=PeerUser(7),
        from_id=PeerUser(8),
        date=datetime(2024, 1, 1, tzinfo=UTC),
        action=MessageActionTodoCompletions([1], []),
        reply_to=reply,
    )
    await manager.on_raw_topic_message(UpdateNewMessage(message, 1, 1))
    assert conn.execute("SELECT service_action FROM messages WHERE dialog_id=7 AND message_id=11").fetchone()
    assert conn.execute("SELECT 1 FROM hydration_jobs WHERE message_id=10").fetchone() is None
    conn.close()


def test_todo_repair_paginates_raw_candidates_and_respects_disabled_enrollment(tmp_path: Path) -> None:
    from mcp_telegram.messages.sqlite_hydration_jobs import repair_media_metadata_hydration_jobs
    from mcp_telegram.sync_transactions import write_transaction

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.executemany("INSERT INTO synced_dialogs(dialog_id,status) VALUES(?,'synced')", [(7,), (8,)])
    seed_full_history_enrollment(conn, 7, enabled=True)
    seed_full_history_enrollment(conn, 8, enabled=False)
    conn.executemany(
        "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(?,?,?,'todo','{}')",
        [(7, i, i) for i in range(1, 302)] + [(8, 400, 400)],
    )
    conn.commit()
    with write_transaction(conn):
        first = repair_media_metadata_hydration_jobs(conn, due_at=1, max_jobs=300)
    assert first.has_more
    with write_transaction(conn):
        second = repair_media_metadata_hydration_jobs(
            conn,
            due_at=1,
            max_jobs=300,
            contact_other_cursor=first.next_contact_other_cursor,
            video_cursor=first.next_video_cursor,
        )
    assert not second.has_more
    assert (
        conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind='media_metadata' AND dialog_id=7").fetchone()[0]
        == 301
    )
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE dialog_id=8").fetchone()[0] == 0
    conn.close()


@pytest.mark.asyncio
async def test_opposite_duplicate_and_historical_actions_do_not_rewrite_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(7,'synced')")
    seed_full_history_enrollment(conn, 7, enabled=True)
    payload = '{"_":"MessageMediaToDo","todo":{"list":[]}}'
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,10,1,'todo',?)",
        (payload,),
    )
    conn.commit()
    manager = event_manager(MagicMock(), conn, asyncio.Event())
    manager.bind_demand_sink(MagicMock())
    for message_id, completed, incompleted in ((11, [1], []), (11, [1], []), (12, [], [1]), (9, [1], [])):
        event = MessageService(
            id=message_id,
            peer_id=PeerUser(7),
            from_id=PeerUser(8),
            date=datetime(2024, 1, 1, tzinfo=UTC),
            action=MessageActionTodoCompletions(completed, incompleted),
            reply_to=MessageReplyHeader(reply_to_msg_id=10),
        )
        await manager.on_raw_topic_message(UpdateNewMessage(event, 1, 1))
    assert conn.execute("SELECT media_payload FROM messages WHERE dialog_id=7 AND message_id=10").fetchone() == (
        payload,
    )
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=7 AND is_service=1").fetchone() == (3,)
    assert conn.execute(
        "SELECT COUNT(*) FROM hydration_jobs WHERE dialog_id=7 AND message_id=10 AND terminal=0"
    ).fetchone() == (1,)
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,terminal", [("temporary_failure", 0), ("exhausted", 1), ("processing", 0)])
async def test_duplicate_append_preserves_existing_queue_state(tmp_path: Path, outcome: str, terminal: int) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(7,'synced')")
    seed_full_history_enrollment(conn, 7, enabled=True)
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,10,1,'todo','{}')"
    )
    conn.commit()
    manager = event_manager(MagicMock(), conn, asyncio.Event())
    manager.bind_demand_sink(MagicMock())
    msg = MessageService(
        id=11,
        peer_id=PeerUser(7),
        from_id=PeerUser(8),
        date=datetime(2024, 1, 1, tzinfo=UTC),
        action=MessageActionTodoAppendTasks([TodoItem(17, TextWithEntities("item", []))]),
        reply_to=MessageReplyHeader(reply_to_msg_id=10),
    )
    await manager.on_raw_topic_message(UpdateNewMessage(msg, 1, 1))
    row = cast(
        tuple[str, int],
        conn.execute(
            "SELECT service_action,reply_to_msg_id FROM messages WHERE dialog_id=7 AND message_id=11"
        ).fetchone(),
    )
    assert cast(dict[str, list[dict[str, int]]], json.loads(row[0]))["list"][0]["id"] == 17 and row[1] == 10
    conn.execute(
        "UPDATE hydration_jobs SET attempts=3,due_at=999,terminal=?,last_outcome=?,last_error_code='FLOOD' WHERE dialog_id=7 AND message_id=10",
        (terminal, outcome),
    )
    conn.commit()
    await manager.on_raw_topic_message(UpdateNewMessage(msg, 1, 1))
    assert conn.execute(
        "SELECT attempts,due_at,terminal,last_outcome,last_error_code FROM hydration_jobs WHERE dialog_id=7 AND message_id=10"
    ).fetchone() == (3, 999, terminal, outcome, "FLOOD")
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,terminal", [("temporary_failure", 0), ("exhausted", 1)])
async def test_new_service_does_not_reset_retry_or_terminal(tmp_path: Path, outcome: str, terminal: int) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(7,'synced')")
    seed_full_history_enrollment(conn, 7, enabled=True)
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,10,1,'todo','{}')"
    )
    conn.execute(
        "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,attempts,priority,message_sent_at,terminal,last_outcome,last_error_code) VALUES('media_metadata',7,10,999,3,1,1,?,?, 'FLOOD')",
        (terminal, outcome),
    )
    conn.commit()
    manager = event_manager(MagicMock(), conn, asyncio.Event())
    manager.bind_demand_sink(MagicMock())
    msg = MessageService(
        id=11,
        peer_id=PeerUser(7),
        date=datetime(2024, 1, 1, tzinfo=UTC),
        action=MessageActionTodoCompletions([1], []),
        reply_to=MessageReplyHeader(reply_to_msg_id=10),
    )
    await manager.on_raw_topic_message(UpdateNewMessage(msg, 1, 1))
    assert conn.execute(
        "SELECT attempts,due_at,terminal,last_outcome,last_error_code FROM hydration_jobs WHERE dialog_id=7 AND message_id=10"
    ).fetchone() == (3, 999, terminal, outcome, "FLOOD")
    conn.close()


def test_stale_event_cannot_cancel_a_newer_processing_lease(tmp_path: Path) -> None:
    from mcp_telegram.messages.sqlite_hydration_jobs import enqueue_todo_refresh
    from mcp_telegram.sync_transactions import write_transaction

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(7,'synced')")
    seed_full_history_enrollment(conn, 7, enabled=True)
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,10,1,'todo','{}')"
    )
    conn.execute("INSERT INTO message_observations(dialog_id,message_id,observation_order) VALUES(7,10,10)")
    conn.execute(
        "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,attempts,priority,message_sent_at,terminal,last_outcome) VALUES('media_metadata',7,10,999,3,1,1,0,'processing')"
    )
    conn.commit()
    msg = MessageService(
        id=11,
        peer_id=PeerUser(7),
        date=datetime(2024, 1, 1, tzinfo=UTC),
        action=MessageActionTodoCompletions([1], []),
        reply_to=MessageReplyHeader(reply_to_msg_id=10),
    )
    with write_transaction(conn):
        enqueue_todo_refresh(conn, 7, msg, 1, 9)
    assert conn.execute(
        "SELECT attempts,due_at,last_outcome FROM hydration_jobs WHERE dialog_id=7 AND message_id=10"
    ).fetchone() == (3, 999, "processing")
    conn.close()
