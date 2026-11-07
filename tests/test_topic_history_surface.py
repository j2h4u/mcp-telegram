"""Topic attribution from Telegram message through SQLite to the MCP read."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl import types

from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.tools.reading import ListMessages, list_messages
from tests.test_daemon_api import _TestClient, make_server


async def test_telegram_topic_header_survives_local_storage_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO synced_dialogs(dialog_id,status,history_depth_complete,last_synced_at) VALUES (1,'synced',1,100)"
    )
    conn.commit()
    message = types.Message(
        id=201,
        peer_id=types.PeerChannel(1),
        date=datetime.fromtimestamp(90, UTC),
        message="topic reply",
        reply_to=types.MessageReplyHeader(reply_to_msg_id=100, forum_topic=True),
    )
    with write_transaction(conn):
        insert_messages_with_fts(conn, [extract_message_row(1, message)])
    client = _TestClient()
    client.iter_messages = MagicMock(side_effect=AssertionError("read acquired Telegram history"))
    client.get_entity = AsyncMock(side_effect=AssertionError("read resolved Telegram entity"))
    server = make_server(conn, client)
    offer = MagicMock(side_effect=AssertionError("read queued acquisition"))
    server.bind_demand_sink(MagicMock(offer=offer))

    class Connection:
        async def list_messages(self, **request: object) -> dict[str, object]:
            return await server._list_messages({key: value for key, value in request.items() if value is not None})

    @asynccontextmanager
    async def connection() -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr("mcp_telegram.tools.reading.daemon_connection", connection)
    try:
        result = await list_messages(ListMessages(exact_dialog_id=1, exact_topic_id=100, message_state="sent"))
        assert result.structured_content is not None
        payload = result.structured_content
        assert payload["messages"][0]["msg_id"] == 201
        assert payload["coverage"]["topic_attribution"]["unassigned_message_count"] == 0
        assert [w["kind"] for w in payload["warnings"]] == ["topic_history_unverified"]
        client.iter_messages.assert_not_called()
        client.get_entity.assert_not_called()
        offer.assert_not_called()
    finally:
        await server.shutdown()
        conn.close()
