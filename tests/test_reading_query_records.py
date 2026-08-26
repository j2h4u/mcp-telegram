import sqlite3
from typing import cast

import pytest

from mcp_telegram.reading.query_records import read_message_from_row


def test_decoder_sqlite_row_defaults_null_content_kind() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE messages (message_id, sent_at, dialog_id, content_kind, media_kind)")
    conn.execute("INSERT INTO messages VALUES (7, 11, 22, NULL, NULL)")
    row = cast(object | None, conn.execute("SELECT * FROM messages").fetchone())
    assert row is not None
    message = read_message_from_row(cast(object, row))
    assert message.message_id == 7
    assert message.content_kind == "none"
    conn.close()


def test_decoder_rejects_malformed_non_null_integer() -> None:
    with pytest.raises(ValueError, match="invalid integer"):
        read_message_from_row({"message_id": "bad", "sent_at": 1, "dialog_id": 2})


@pytest.mark.parametrize(
    "field",
    [
        "sender_id",
        "reply_to_msg_id",
        "forum_topic_id",
        "deleted_at",
        "edit_date",
        "effective_sender_id",
        "read_at",
    ],
)
def test_decoder_rejects_malformed_nullable_integer_mapping(field: str) -> None:
    with pytest.raises(ValueError, match="invalid integer"):
        read_message_from_row({"message_id": 1, "sent_at": 1, "dialog_id": 2, field: "bad"})


def test_decoder_preserves_nullable_integer_values_from_mapping() -> None:
    message = read_message_from_row({"message_id": 1, "sent_at": 2, "dialog_id": 3, "sender_id": "4"})
    assert message.sender_id == 4
    assert message.reply_to_msg_id is None
