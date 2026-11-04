"""A deletion scan also publishes the surviving message it already received."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from telethon.tl import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from tests.sqlite_writer_helpers import event_manager


@pytest.mark.parametrize("case", ["normal", "revoke", "syncing"])
async def test_gap_scan_retains_edited_survivor_without_extra_request(tmp_path: Path, case: str) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    client = MagicMock()
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    client.capture_image_scope, client.claim_image_scope = cell.capture, cell.claim
    manager = event_manager(client, conn, asyncio.Event())
    manager.set_self_id(42)
    old = types.Message(
        id=7,
        peer_id=types.PeerUser(55),
        from_id=types.PeerUser(55),
        date=datetime.fromtimestamp(10, UTC),
        message="old body",
    )
    edited = types.Message(
        id=7,
        peer_id=types.PeerUser(55),
        from_id=types.PeerUser(55),
        date=datetime.fromtimestamp(10, UTC),
        edit_date=datetime.fromtimestamp(20, UTC),
        message="updated survivor",
    )
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (55,'user')")
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (55,'user',0)")
        insert_messages_with_fts(conn, [extract_message_row(55, old)])

    async def get_messages(dialog_id: int, *, ids: list[int]) -> object:
        assert dialog_id == 55 and ids == [7]
        if case == "revoke":
            change = cell.begin_change()
            cell.end_change(change)
            assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 43, 2, 99))
            assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
        if case == "syncing":
            with write_transaction(conn):
                conn.execute("UPDATE synced_dialogs SET status='syncing' WHERE dialog_id=55")
        return [edited]

    client.get_messages = get_messages
    assert await manager.run_dm_gap_scan_page(55, [7]) == 0
    if case == "revoke":
        assert conn.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == ("old body",)
        assert conn.execute("SELECT COUNT(*) FROM message_versions").fetchone() == (0,)
    else:
        assert conn.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (
            "updated survivor",
        )
        assert conn.execute("SELECT old_text FROM message_versions WHERE dialog_id=55 AND message_id=7").fetchone() == (
            "old body",
        )
        assert conn.execute(
            "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'updated' AND dialog_id=55"
        ).fetchone() == (7,)
    conn.close()
