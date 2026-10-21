"""Realtime transactions use the real owner with a read-only loop connection."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import PeerUser, UpdateReadHistoryInbox

from helpers import build_mock_message
from mcp_telegram.event_handlers import EventHandlerManager, _EditedMessageEvent, _InboxReadUpdateLike, _NewMessageEvent
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema, open_sync_db_reader
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.history_enrollment_helpers import seed_full_history_enrollment


def _seed(path: Path) -> sqlite3.Connection:
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("INSERT INTO dialogs(dialog_id,type,unread_count) VALUES(42,'user',1)")
    conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(42,'user',1)")
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(42,'synced')")
    seed_full_history_enrollment(conn, 42, enabled=True)
    conn.commit()
    return conn


@pytest.mark.asyncio
async def test_realtime_commits_with_readonly_loop_connection_and_postcommit_offers(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    seed = _seed(path)
    seed.close()
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)
    loop_thread = threading.get_ident()
    offers: list[DemandKind] = []

    def offer(kind: DemandKind) -> bool:
        assert threading.get_ident() == loop_thread
        assert reader.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=1").fetchone() is not None
        offers.append(kind)
        return True

    class Sink:
        def offer(self, kind: DemandKind) -> bool:
            return offer(kind)

    manager = EventHandlerManager(MagicMock(), reader, asyncio.Event(), writer=writer)
    manager.bind_demand_sink(Sink())
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("UPDATE dialogs SET unread_count=2")
        reader.rollback()
        event = SimpleNamespace(chat_id=42, is_private=True, message=build_mock_message(1, text="before"))
        await manager.on_new_message(cast(_NewMessageEvent, event))
        assert reader.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=1").fetchone() == ("before",)
        assert offers

        event.message = build_mock_message(1, text="after")
        await manager.on_message_edited(cast(_EditedMessageEvent, event))
        assert reader.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=1").fetchone() == ("after",)
        assert reader.execute("SELECT old_text FROM message_versions WHERE dialog_id=42").fetchone() == ("before",)

        update = UpdateReadHistoryInbox(peer=PeerUser(42), max_id=1, still_unread_count=0, pts=1, pts_count=1)
        await manager.on_raw_inbox_read(cast(_InboxReadUpdateLike, update))
        assert reader.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id=42").fetchone() == (1,)
        assert reader.execute("SELECT unread_count FROM dialogs WHERE dialog_id=42").fetchone() == (0,)
    finally:
        await writer.aclose()
        reader.close()


@pytest.mark.asyncio
async def test_realtime_lock_wait_keeps_loop_responsive_and_rollback_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    lock = _seed(path)
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)
    manager = EventHandlerManager(MagicMock(), reader, asyncio.Event(), writer=writer)
    manager.bind_demand_sink(MagicMock())
    message = build_mock_message(1, text="pending")
    event = cast(_NewMessageEvent, SimpleNamespace(chat_id=42, is_private=True, message=message))
    pending: asyncio.Task[None] | None = None

    async def deliver() -> None:
        await manager.on_new_message(event)

    try:
        lock.execute("BEGIN IMMEDIATE")
        pending = asyncio.create_task(deliver())
        await asyncio.sleep(0.02)
        assert not pending.done()
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
        lock.rollback()
        await asyncio.wait_for(pending, timeout=2)
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)

        def fail_metadata(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected metadata failure")

        manager._project_new_message_metadata = fail_metadata  # type: ignore[method-assign]
        failed_event = cast(
            _NewMessageEvent,
            SimpleNamespace(chat_id=42, is_private=True, message=build_mock_message(2, text="rolled back")),
        )
        await manager.on_new_message(failed_event)
        assert reader.execute("SELECT message_id FROM messages ORDER BY message_id").fetchall() == [(1,)]
        assert reader.execute("SELECT message_id FROM messages_fts ORDER BY message_id").fetchall() == [(1,)]
    finally:
        lock.rollback()
        if pending is not None and not pending.done():
            await pending
        await writer.aclose()
        reader.close()
        lock.close()
