"""Focused tests for transaction-neutral event message persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.fts import stem_text
from mcp_telegram.message_contracts import ExtractedMessage, StoredMessage
from mcp_telegram.messages.sqlite_repository import (
    insert_messages_with_fts,
    list_undeleted_message_ids,
    mark_message_deleted,
    persist_edited_message,
    persist_transcribed_text,
    read_message_text,
    upsert_message_transcription,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


def _message(
    message_id: int,
    *,
    text: str | None,
    sent_at: int = 100,
    media_kind: str | None = None,
    media_payload: str | None = None,
) -> ExtractedMessage:
    return ExtractedMessage(
        message=StoredMessage(
            dialog_id=42,
            message_id=message_id,
            sent_at=sent_at,
            text=text,
            sender_id=7,
            sender_first_name="Test",
            reply_to_msg_id=None,
            forum_topic_id=None,
            edit_date=None,
            grouped_id=None,
            reply_to_peer_id=None,
            out=0,
            is_service=0,
            post_author=None,
            media_kind=media_kind,
            media_payload=media_payload,
        ),
        reply_count=0,
    )


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    connection = _open_sync_db(path)
    try:
        yield connection
    finally:
        connection.close()


def test_read_message_text_distinguishes_missing_from_null(conn: sqlite3.Connection) -> None:
    missing = read_message_text(conn, 42, 1)
    assert missing.found is False
    assert missing.text is None

    with conn:
        insert_messages_with_fts(conn, [_message(1, text=None)])
    null_text = read_message_text(conn, 42, 1)
    assert null_text.found is True
    assert null_text.text is None


def test_persist_edited_message_versions_sequentially_and_refreshes_fts(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(10, text="first")])
    with conn:
        assert persist_edited_message(conn, _message(10, text="second"), old_text="first", edit_date=200) == 1
    with conn:
        assert persist_edited_message(conn, _message(10, text="third"), old_text="second", edit_date=300) == 2

    assert conn.execute(
        "SELECT version, old_text FROM message_versions WHERE dialog_id=42 AND message_id=10 ORDER BY version"
    ).fetchall() == [(1, "first"), (2, "second")]
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=10").fetchone() == ("third",)
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=42 AND message_id=10").fetchone() == (
        stem_text("third"),
    )


def test_persist_edited_message_unchanged_is_noop(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(11, text="same")])
    with conn:
        assert persist_edited_message(conn, _message(11, text="same"), old_text="same", edit_date=200) is None
    assert conn.execute("SELECT COUNT(*) FROM message_versions").fetchone() == (0,)


def _make_hydration_eligible(conn: sqlite3.Connection, status: str = "synced") -> None:
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (42, ?)", (status,))
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (42, 1, 'explicit', 1)"
    )


@pytest.mark.parametrize("media_kind", ["contact", "other"])
def test_message_persistence_enqueues_one_unresolved_job_and_preserves_attempts(
    conn: sqlite3.Connection, media_kind: str
) -> None:
    _make_hydration_eligible(conn)
    conn.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts) "
        "VALUES ('media_metadata', 42, 90, 100, 2)"
    )
    with conn:
        insert_messages_with_fts(conn, [_message(90, text=None, media_kind=media_kind, media_payload="{}")])
        insert_messages_with_fts(conn, [_message(90, text=None, media_kind=media_kind, media_payload="{}")])
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=42 AND message_id=90").fetchone() == (1,)
    assert conn.execute("SELECT kind, dialog_id, message_id, attempts FROM hydration_jobs").fetchall() == [
        ("media_metadata", 42, 90, 2)
    ]


@pytest.mark.parametrize(
    ("media_kind", "media_payload"),
    [(None, None), ("photo", "{}"), ("contact", '{"phone_number":"1"}')],
)
def test_message_persistence_removes_job_for_resolved_or_missing_media(
    conn: sqlite3.Connection, media_kind: str | None, media_payload: str | None
) -> None:
    _make_hydration_eligible(conn)
    conn.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts) "
        "VALUES ('media_metadata', 42, 91, 100, 2)"
    )
    with conn:
        insert_messages_with_fts(conn, [_message(91, text=None, media_kind=media_kind, media_payload=media_payload)])
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.parametrize("status", ["not_synced", "own_only", "fragment", "access_lost"])
def test_message_persistence_does_not_enqueue_inactive_dialogs(conn: sqlite3.Connection, status: str) -> None:
    _make_hydration_eligible(conn, status=status)
    with conn:
        insert_messages_with_fts(conn, [_message(92, text=None, media_kind="other", media_payload="{}")])
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


def test_persist_transcribed_text_versions_and_refreshes_fts(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(12, text=None)])
    with conn:
        assert (
            persist_transcribed_text(
                conn,
                42,
                12,
                old_text=None,
                transcribed_text="voice words",
                transcribed_at=400,
            )
            == 1
        )
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=12").fetchone() == ("voice words",)
    assert conn.execute(
        "SELECT old_text, version FROM message_versions WHERE dialog_id=42 AND message_id=12"
    ).fetchone() == (
        None,
        1,
    )
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=42 AND message_id=12").fetchone() == (
        stem_text("voice words"),
    )
    with conn:
        assert (
            persist_transcribed_text(
                conn,
                42,
                12,
                old_text="voice words",
                transcribed_text="voice words",
                transcribed_at=401,
            )
            is None
        )
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE dialog_id=42 AND message_id=12").fetchone() == (1,)


def test_message_transcription_is_applied_by_canonical_bundle_writer(conn: sqlite3.Connection) -> None:
    with conn:
        upsert_message_transcription(conn, 42, 14, transcribed_text="voice words", transcription_id=14, received_at=400)
        insert_messages_with_fts(conn, [_message(14, text="caption")])
        insert_messages_with_fts(conn, [_message(14, text=None)])

    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=14").fetchone() == ("voice words",)
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=42 AND message_id=14").fetchone() == (
        stem_text("voice words"),
    )
    assert conn.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == (1,)


def test_existing_transcription_survives_voice_reimport(conn: sqlite3.Connection) -> None:
    with conn:
        upsert_message_transcription(conn, 42, 15, transcribed_text="voice words", transcription_id=15, received_at=400)
        insert_messages_with_fts(conn, [_message(15, text="caption", media_kind="voice", media_payload="{}")])
        insert_messages_with_fts(conn, [_message(15, text=None, media_kind="voice", media_payload="{}")])
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=15").fetchone() == ("voice words",)


def test_unrelated_media_caption_can_be_removed_on_reimport(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(16, text="caption", media_kind="photo", media_payload="{}")])
        insert_messages_with_fts(conn, [_message(16, text=None, media_kind="photo", media_payload="{}")])
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=16").fetchone() == (None,)


def test_mark_message_deleted_is_idempotent_and_retains_text_and_fts(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(13, text="retain me")])
    with conn:
        assert mark_message_deleted(conn, 42, 13, 500) is True
    with conn:
        assert mark_message_deleted(conn, 42, 13, 600) is False
    assert conn.execute(
        "SELECT text, is_deleted, deleted_at FROM messages WHERE dialog_id=42 AND message_id=13"
    ).fetchone() == (
        "retain me",
        1,
        500,
    )
    assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE dialog_id=42 AND message_id=13").fetchone() == (1,)


def test_list_undeleted_message_ids_uses_strict_cutoff(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(
            conn,
            [
                _message(20, text="before", sent_at=99),
                _message(21, text="at cutoff", sent_at=100),
                _message(22, text="after", sent_at=101),
                _message(23, text="deleted", sent_at=98),
            ],
        )
        assert mark_message_deleted(conn, 42, 23, 600) is True
    assert list_undeleted_message_ids(conn, 42, 100) == (20,)


def test_repository_writes_rollback_with_caller_transaction(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(30, text="before")])
    with pytest.raises(RuntimeError, match="abort"):
        with conn:
            assert persist_edited_message(conn, _message(30, text="after"), old_text="before", edit_date=700) == 1
            raise RuntimeError("abort")
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=30").fetchone() == ("before",)
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE dialog_id=42 AND message_id=30").fetchone() == (0,)
