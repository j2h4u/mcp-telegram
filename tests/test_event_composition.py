"""Realtime composition edits preserve ordinary message and inbox state."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl import types

from helpers import build_mock_message
from mcp_telegram.event_handlers import EventHandlerManager, _EditedMessageEvent
from mcp_telegram.message_composition import decode_formatting_entities
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_formatting_only_edit_persists_without_lookup_or_history(tmp_path: Path, enabled: bool) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    client = MagicMock()
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES(42,'user')")
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(42,'user',1)")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status,last_event_at) VALUES(42,'synced',17)")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES(42,?,'explicit',1)",
            (int(enabled),),
        )
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,text,formatting_entities) VALUES(42,1,1,'same','[]')"
        )
        conn.execute("INSERT INTO messages_fts(dialog_id,message_id,stemmed_text) VALUES(42,1,'unchanged fts')")
        conn.commit()
        message = build_mock_message(1, text="same", edit_date=datetime(2026, 1, 1, tzinfo=UTC))
        message.entities = [types.MessageEntityBold(offset=0, length=4)]
        manager = EventHandlerManager(client, conn, asyncio.Event())
        await manager.on_message_edited(cast(_EditedMessageEvent, SimpleNamespace(chat_id=42, message=message)))
        facts = decode_formatting_entities(
            cast(tuple[str | None], conn.execute("SELECT formatting_entities FROM messages").fetchone())[0]
        )
        assert facts == ([{"_": "MessageEntityBold", "offset": 0, "length": 4}] if enabled else [])
        assert conn.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM conversation_history_events").fetchone()[0] == 0
        assert conn.execute("SELECT stemmed_text FROM messages_fts").fetchone()[0] == "unchanged fts"
        assert conn.execute("SELECT last_event_at FROM synced_dialogs").fetchone()[0] == 17
        assert client.mock_calls == []
    finally:
        conn.close()
