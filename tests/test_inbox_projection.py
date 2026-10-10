import dataclasses
import sqlite3
from typing import cast

import pytest

from mcp_telegram.inbox_projection import _structured_messages
from mcp_telegram.reading.query_records import read_message_from_row
from mcp_telegram.reading.sqlite_projection import _FETCH_UNREAD_MESSAGES_SQL, _LIST_MESSAGES_BASE_SQL
from tests.test_daemon_api import _insert_message, _insert_message_version, _make_db_with_dialogs


@pytest.mark.parametrize("has_versions", [False, True])
def test_inbox_preserves_ordinary_reply_and_latest_edit_facts(has_versions: bool) -> None:
    conn = _make_db_with_dialogs()
    conn.row_factory = sqlite3.Row
    try:
        _insert_message(conn, 1, 12)
        conn.execute("UPDATE messages SET reply_to_msg_id=11, edit_date=200 WHERE message_id=12")
        if has_versions:
            _insert_message_version(conn, 1, 12, 1, edit_date=250)
            _insert_message_version(conn, 1, 12, 2, edit_date=225)
        params = {
            "dialog_id": 1,
            "after_msg_id": 0,
            "limit": 5,
            "self_id": None,
            "since_utc": None,
            "deleted_since_utc": 0,
        }
        row = cast(sqlite3.Row | None, conn.execute(_FETCH_UNREAD_MESSAGES_SQL, params).fetchone())
        ordinary_row = cast(sqlite3.Row | None, conn.execute(_LIST_MESSAGES_BASE_SQL, params).fetchone())
        assert row is not None and ordinary_row is not None
        ordinary = read_message_from_row(ordinary_row)
        message = read_message_from_row(row)
        assert message.reply_to_msg_id == ordinary.reply_to_msg_id == 11
        expected_edit = 250 if has_versions else 200
        assert message.edit_date == ordinary.edit_date == expected_edit
        projected = _structured_messages([dataclasses.asdict(message)], read_state=None, dialog_type="user")[0]
        assert cast(dict[str, object], projected["reply_context_ref"])["msg_id"] == 11
        assert projected["edit_date"] == expected_edit
    finally:
        conn.close()
