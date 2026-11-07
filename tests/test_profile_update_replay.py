"""A retained profile update replays through the real delivery and SQLite owners."""

from contextlib import closing
from pathlib import Path
from typing import cast

from telethon import types
from telethon.sessions import SQLiteSession

from mcp_telegram.dialog_identity import ensure_dialog_identity_baseline
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import _open_sync_db, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction
from tests.test_update_delivery import _client, _database, _message, _pending, _safe
from tests.test_update_delivery_handlers import _admit, _manager


async def test_retained_status_with_thin_dialog_replays_before_checkpoint(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with closing(_open_sync_db(path)) as conn, write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=99 WHERE singleton=1")
        ensure_dialog_identity_baseline(conn, 55)
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (55,'user',1)")
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (55,5,1)")
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = _client(tmp_path / "session", writer, channel=False)
    _manager(client, reader, writer)
    try:
        await client.start_update_delivery(99)
        previous = _safe(path)
        await _admit(
            client,
            [types.UpdateUserStatus(55, types.UserStatusRecently()), types.UpdateNewMessage(_message(11), 11, 1)],
            include_fixture_user=False,
        )
        assert _pending(path) is not None
        assert _safe(path) == previous
        assert reader.execute("SELECT type FROM dialogs WHERE dialog_id=55").fetchone() == (None,)

        cast(SQLiteSession, client.session).close()
        reader.close()
        await writer.aclose()
        writer = await SQLiteWriter.open(path)
        reader = open_sync_db_reader(path)
        client = _client(tmp_path / "session", writer, channel=False)
        _manager(client, reader, writer)
        await client.start_update_delivery(99)

        assert _pending(path) is None
        assert _safe(path).pts == 11
        assert reader.execute(
            "SELECT json_extract(detail_json,'$.status.type') FROM entity_details WHERE entity_id=55"
        ).fetchone() == ("recently",)
        assert reader.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=55 AND message_id=11").fetchone() == (1,)
    finally:
        cast(SQLiteSession, client.session).close()
        reader.close()
        await writer.aclose()
