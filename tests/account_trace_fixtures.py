from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


def open_trace_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a current sync.db and return an open writable connection."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    return _open_sync_db(db_path)


def seed_entity(
    conn: sqlite3.Connection,
    *,
    entity_id: int,
    entity_type: str = "User",
    name: str = "Alice Example",
    username: str | None = "alice",
    updated_at: int = 1_700_000_000,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO entities
            (id, type, name, username, name_normalized, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entity_id, entity_type, name, username, name.casefold(), updated_at),
    )


def seed_dialog(
    conn: sqlite3.Connection,
    *,
    dialog_id: int,
    name: str,
    dialog_type: str = "User",
    hidden: int = 0,
    snapshot_at: int = 1_700_000_000,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO dialogs
            (dialog_id, name, type, hidden, snapshot_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (dialog_id, name, dialog_type, hidden, snapshot_at),
    )


def seed_synced_dialog(
    conn: sqlite3.Connection,
    *,
    dialog_id: int,
    status: str = "synced",
    total_messages: int | None = 10,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO synced_dialogs
            (dialog_id, status, total_messages)
        VALUES (?, ?, ?)
        """,
        (dialog_id, status, total_messages),
    )


def seed_topic(
    conn: sqlite3.Connection,
    *,
    dialog_id: int,
    topic_id: int,
    title: str = "General",
    updated_at: int = 1_700_000_000,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO topic_metadata
            (dialog_id, topic_id, title, is_general, is_deleted, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (dialog_id, topic_id, title, 0, 0, updated_at),
    )


def seed_message(
    conn: sqlite3.Connection,
    *,
    dialog_id: int,
    message_id: int,
    sent_at: int,
    text: str | None = "hello",
    sender_id: int | None = None,
    out: int = 0,
    is_service: int = 0,
    forum_topic_id: int | None = None,
    post_author: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO messages
            (dialog_id, message_id, sent_at, text, sender_id, forum_topic_id,
             out, is_service, post_author, is_deleted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            dialog_id,
            message_id,
            sent_at,
            text,
            sender_id,
            forum_topic_id,
            out,
            is_service,
            post_author,
        ),
    )


def seed_channel_signature_message(
    conn: sqlite3.Connection,
    *,
    dialog_id: int,
    message_id: int,
    sent_at: int,
    signature: str,
    text: str = "signed channel post",
) -> None:
    seed_message(
        conn,
        dialog_id=dialog_id,
        message_id=message_id,
        sent_at=sent_at,
        text=text,
        sender_id=dialog_id,
        post_author=signature,
    )


def seed_trace_fragment(
    conn: sqlite3.Connection,
    *,
    target_user_id: int,
    dialog_id: int,
    topic_id: int = 0,
    coverage_kind: str = "authored_message",
    status: str = "pending",
    fetched_at: int | None = None,
    checkpoint: str | None = None,
    last_error: str | None = None,
    next_retry_at: int | None = None,
    created_at: int = 1_700_000_000,
    updated_at: int = 1_700_000_000,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO trace_coverage_fragments
            (target_user_id, dialog_id, topic_id, coverage_kind, status,
             fetched_at, checkpoint, last_error, next_retry_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target_user_id,
            dialog_id,
            topic_id,
            coverage_kind,
            status,
            fetched_at,
            checkpoint,
            last_error,
            next_retry_at,
            created_at,
            updated_at,
        ),
    )


def make_channel_signature_evidence() -> dict[str, object]:
    return {
        "source": "sync.db",
        "evidence_kind": "authored_message",
        "dialog_id": -100123,
        "dialog_title": "Channel",
        "dialog_type": "Channel",
        "topic_id": None,
        "topic_title": None,
        "message_id": 42,
        "sent_at": 1_700_000_000,
        "sender_id": -100123,
        "effective_sender_id": -100123,
        "authorship_basis": "post_author_signature",
        "author_signature": "Alice Example",
        "text": "signed channel post",
        "media_description": None,
    }
