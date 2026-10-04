"""Composition facts survive common extraction, migration and formatting-only edits."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.message_composition import (
    decode_formatting_entities,
    decode_service_action,
    extract_message_composition,
    normalize_telegram_fact,
)
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts, persist_edited_message
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.sync_db import _apply_migration_79, _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction


def _message(entities: list[object] | None = None, action: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1, date=datetime(2026, 1, 1, tzinfo=UTC), message="😀code", entities=entities, action=action
    )


def test_all_formatting_fields_and_utf16_offsets_are_preserved():
    message = _message(
        [
            types.MessageEntityBold(offset=2, length=4),
            types.MessageEntityItalic(offset=2, length=4),
            types.MessageEntityPre(offset=2, length=4, language="python"),
            types.MessageEntityCustomEmoji(offset=0, length=2, document_id=123),
        ]
    )
    extracted = extract_message_row(42, message)
    assert decode_formatting_entities(extracted.message.formatting_entities) == [
        {"_": "MessageEntityBold", "offset": 2, "length": 4},
        {"_": "MessageEntityItalic", "offset": 2, "length": 4},
        {"_": "MessageEntityPre", "offset": 2, "length": 4, "language": "python"},
        {"_": "MessageEntityCustomEmoji", "offset": 0, "length": 2, "document_id": 123},
    ]
    assert extracted.entities == []  # Existing analytics filtering is unchanged.
    assert extracted.message.service_action is None
    assert extract_message_composition(_message()) == ("[]", None)
    assert decode_formatting_entities(None) is None


def test_typed_service_action_and_nested_values_are_preserved():
    action = types.MessageActionChatAddUser(users=[123, 456])
    _, payload = extract_message_composition(_message(action=action))
    assert decode_service_action(payload) == {"_": "MessageActionChatAddUser", "users": [123, 456]}
    assert normalize_telegram_fact({"nested": [datetime(2026, 1, 1, tzinfo=UTC), b"\x00\xff"]}) == {
        "nested": ["2026-01-01T00:00:00+00:00", {"encoding": "base64", "data": "AP8="}]
    }
    with pytest.raises(TypeError, match="Unsupported"):
        normalize_telegram_fact(object())


def test_migration_leaves_historical_messages_and_scheduled_messages_unknown(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        for table in ("messages", "scheduled_messages"):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN formatting_entities")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN service_action")
        conn.execute("DELETE FROM schema_version WHERE version=79")
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES(42,1,1,'old')")
        conn.execute(
            "INSERT INTO scheduled_messages(dialog_id,message_id,scheduled_at,text,first_seen_at,updated_at) VALUES(42,1,1,'old',1,1)"
        )
        conn.commit()
        assert _apply_migration_79(conn, 78) == 79
        for table in ("messages", "scheduled_messages"):
            assert cast(
                tuple[object, ...],
                conn.execute(f"SELECT formatting_entities,service_action,text FROM {table}").fetchone(),
            ) == (
                None,
                None,
                "old",
            )
        assert _apply_migration_79(conn, 79) == 79
    finally:
        conn.close()


def test_identical_text_edit_updates_formatting_and_action_without_text_version(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        before = extract_message_row(42, _message())
        after = extract_message_row(
            42,
            _message(
                entities=[types.MessageEntityBold(offset=2, length=4)],
                action=types.MessageActionChatAddUser(users=[123]),
            ),
        )
        with write_transaction(conn):
            insert_messages_with_fts(conn, [before])
            assert persist_edited_message(conn, after, old_text="😀code", edit_date=20) is None
        row = cast(
            tuple[str | None, str | None],
            conn.execute(
                "SELECT formatting_entities,service_action FROM messages WHERE dialog_id=42 AND message_id=1"
            ).fetchone(),
        )
        assert row == (after.message.formatting_entities, after.message.service_action)
        assert conn.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0] == 0
    finally:
        conn.close()


def test_embedded_photo_bytes_are_omitted_but_opaque_metadata_survives():
    photo = types.Photo(
        id=1,
        access_hash=2,
        file_reference=b"reference",
        date=datetime(2026, 1, 1, tzinfo=UTC),
        sizes=[
            types.PhotoCachedSize(type="s", w=1, h=2, bytes=b"thumbnail"),
            types.PhotoStrippedSize(type="i", bytes=b"preview"),
        ],
        dc_id=4,
    )
    _, payload = extract_message_composition(_message(action=types.MessageActionChatEditPhoto(photo=photo)))
    action = decode_service_action(payload)
    assert action is not None
    normalized_photo = action["photo"]
    assert isinstance(normalized_photo, dict)
    assert normalized_photo["sizes"] == [
        {"_": "PhotoCachedSize", "type": "s", "w": 1, "h": 2, "bytes_omitted": True, "bytes_length": 9},
        {"_": "PhotoStrippedSize", "type": "i", "bytes_omitted": True, "bytes_length": 7},
    ]
    assert normalized_photo["file_reference"] == {"encoding": "base64", "data": "cmVmZXJlbmNl"}
    assert normalize_telegram_fact({"waveform": b"metadata"}) == {
        "waveform": {"encoding": "base64", "data": "bWV0YWRhdGE="}
    }
