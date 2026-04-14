"""Tests for EventHandlerManager — TDD RED phase.

Covers DAEMON-07 (NewMessage), DAEMON-08 (MessageEdited),
DAEMON-09 (channel/supergroup MessageDeleted), and DAEMON-10
(DM gap scan) behaviors.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import build_mock_message, build_mock_reactions
from mcp_telegram.event_handlers import EventHandlerManager
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


def make_new_message_event(
    chat_id: int | None,
    message: SimpleNamespace,
    is_private: bool = False,
) -> SimpleNamespace:
    """Build a minimal NewMessage.Event-like object."""
    return SimpleNamespace(chat_id=chat_id, message=message, is_private=is_private)


def make_message_edited_event(
    chat_id: int | None, message: SimpleNamespace
) -> SimpleNamespace:
    """Build a minimal MessageEdited.Event-like object.

    The message should have .edit_date set to a datetime to signal an edit.
    """
    return SimpleNamespace(chat_id=chat_id, message=message)


def make_message_deleted_event(
    chat_id: int | None, deleted_ids: list[int]
) -> SimpleNamespace:
    """Build a minimal MessageDeleted.Event-like object."""
    return SimpleNamespace(chat_id=chat_id, deleted_ids=deleted_ids)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_db(tmp_path: Any) -> sqlite3.Connection:
    """Create a real sync.db in tmp_path and return an open connection."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def mock_client() -> MagicMock:
    """Return a mock TelegramClient."""
    client = MagicMock()
    client.add_event_handler = MagicMock()
    client.remove_event_handler = MagicMock()
    return client


@pytest.fixture()
def shutdown_event() -> asyncio.Event:
    """Return an unset asyncio.Event."""
    return asyncio.Event()


def make_manager(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> EventHandlerManager:
    return EventHandlerManager(mock_client, sync_db, shutdown_event)


def insert_synced_dialog(conn: sqlite3.Connection, dialog_id: int) -> None:
    """Insert a dialog into synced_dialogs so the manager treats it as synced."""
    conn.execute(
        "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    conn.commit()


def insert_message(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    text: str | None = "some text",
    is_deleted: int = 0,
    deleted_at: int | None = None,
) -> None:
    """Insert a message row directly for test setup."""
    conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(dialog_id, message_id, sent_at, text, sender_id, sender_first_name, "
        "media_description, reply_to_msg_id, forum_topic_id, reactions, is_deleted, deleted_at) "
        "VALUES (?, ?, 1704067200, ?, 42, 'Alice', NULL, NULL, NULL, NULL, ?, ?)",
        (dialog_id, message_id, text, is_deleted, deleted_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# DAEMON-07: NewMessage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_new_message_inserts_row(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """New message in a synced dialog is inserted into messages table."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(id=500, text="hello")
    event = make_new_message_event(chat_id=dialog_id, message=msg)
    await manager.on_new_message(event)

    row = sync_db.execute(
        "SELECT dialog_id, message_id, text FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 500),
    ).fetchone()
    assert row is not None
    assert row[0] == dialog_id
    assert row[1] == 500
    assert row[2] == "hello"


@pytest.mark.asyncio
async def test_on_new_message_ignores_unsynced(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """New message for an unsynced dialog produces no DB row."""
    # dialog_id=9999 is NOT in synced_dialogs
    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(id=100, text="ignored")
    event = make_new_message_event(chat_id=9999, message=msg)
    await manager.on_new_message(event)

    count = sync_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_on_new_message_auto_enrolls_private_dialog(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Private message from an unknown dialog enrolls it into synced_dialogs."""
    dialog_id = 7001
    # dialog_id is NOT in synced_dialogs

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(id=1, text="hey")
    event = make_new_message_event(chat_id=dialog_id, message=msg, is_private=True)
    await manager.on_new_message(event)

    row = sync_db.execute(
        "SELECT dialog_id, status FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None, "auto-enroll must insert a synced_dialogs row"
    assert row[1] == "syncing"
    assert dialog_id in manager._synced_dialog_ids


@pytest.mark.asyncio
async def test_on_new_message_auto_enroll_idempotent(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Auto-enroll is idempotent — two private messages from the same new dialog don't duplicate."""
    dialog_id = 7002

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    for msg_id in [1, 2]:
        msg = build_mock_message(id=msg_id, text="hi")
        event = make_new_message_event(chat_id=dialog_id, message=msg, is_private=True)
        await manager.on_new_message(event)

    count = sync_db.execute(
        "SELECT COUNT(*) FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()[0]
    assert count == 1, "synced_dialogs must have exactly one row for the dialog"


@pytest.mark.asyncio
async def test_on_new_message_ignores_unsynced_non_private(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """New message from an unknown non-private (group) dialog is ignored — no enrollment."""
    dialog_id = 7003

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(id=1, text="group msg")
    event = make_new_message_event(chat_id=dialog_id, message=msg, is_private=False)
    await manager.on_new_message(event)

    count = sync_db.execute(
        "SELECT COUNT(*) FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()[0]
    assert count == 0, "non-private unknown dialog must not be enrolled"
    assert dialog_id not in manager._synced_dialog_ids


@pytest.mark.asyncio
async def test_on_new_message_updates_last_event_at(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Firing on_new_message sets synced_dialogs.last_event_at."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)

    # Verify last_event_at starts as None
    before = sync_db.execute(
        "SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone()[0]
    assert before is None

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(id=501)
    event = make_new_message_event(chat_id=dialog_id, message=msg)
    await manager.on_new_message(event)

    after = sync_db.execute(
        "SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone()[0]
    assert after is not None


@pytest.mark.asyncio
async def test_burst_50_messages(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Burst of 50 on_new_message events all insert without drops."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    for i in range(50):
        msg = build_mock_message(id=1000 + i, text=f"msg {i}")
        event = make_new_message_event(chat_id=dialog_id, message=msg)
        await manager.on_new_message(event)

    count = sync_db.execute(
        "SELECT COUNT(*) FROM messages WHERE dialog_id=?", (dialog_id,)
    ).fetchone()[0]
    assert count == 50


# ---------------------------------------------------------------------------
# DAEMON-08: MessageEdited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_edited_creates_version(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Editing a message with changed text creates a message_versions row."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=100, text="old text")

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    edit_dt = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    msg = build_mock_message(id=100, text="new text", edit_date=edit_dt)
    event = make_message_edited_event(chat_id=dialog_id, message=msg)
    await manager.on_message_edited(event)

    # version row created with old text
    ver_row = sync_db.execute(
        "SELECT old_text, version FROM message_versions WHERE dialog_id=? AND message_id=?",
        (dialog_id, 100),
    ).fetchone()
    assert ver_row is not None
    assert ver_row[0] == "old text"
    assert ver_row[1] == 1

    # messages row updated with new text
    new_text = sync_db.execute(
        "SELECT text FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 100),
    ).fetchone()[0]
    assert new_text == "new text"


@pytest.mark.asyncio
async def test_on_message_edited_no_version_if_same(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Editing a message with same text does NOT create a message_versions row."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=101, text="same")

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    edit_dt = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    msg = build_mock_message(id=101, text="same", edit_date=edit_dt)
    event = make_message_edited_event(chat_id=dialog_id, message=msg)
    await manager.on_message_edited(event)

    count = sync_db.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_on_message_edited_unknown_message(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Edit to a message not in sync.db inserts the row but creates no version history."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    # No pre-inserted message for message_id=999

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    edit_dt = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    msg = build_mock_message(id=999, text="current text", edit_date=edit_dt)
    event = make_message_edited_event(chat_id=dialog_id, message=msg)
    await manager.on_message_edited(event)

    # Message should be inserted
    row = sync_db.execute(
        "SELECT message_id FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 999),
    ).fetchone()
    assert row is not None

    # No version row (no old text to track)
    count = sync_db.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_on_message_edited_increments_version(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Two sequential edits with different text produce version=1 and version=2."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=200, text="v0 text")

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    edit_dt1 = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    msg1 = build_mock_message(id=200, text="v1 text", edit_date=edit_dt1)
    await manager.on_message_edited(make_message_edited_event(dialog_id, msg1))

    edit_dt2 = datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    msg2 = build_mock_message(id=200, text="v2 text", edit_date=edit_dt2)
    await manager.on_message_edited(make_message_edited_event(dialog_id, msg2))

    versions = sync_db.execute(
        "SELECT version, old_text FROM message_versions WHERE dialog_id=? AND message_id=? ORDER BY version",
        (dialog_id, 200),
    ).fetchall()
    assert len(versions) == 2
    assert versions[0] == (1, "v0 text")
    assert versions[1] == (2, "v1 text")


# ---------------------------------------------------------------------------
# DAEMON-09: MessageDeleted — channels/supergroups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_deleted_channel(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Deleted channel message gets is_deleted=1 with a deleted_at timestamp."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=200, text="will be deleted", is_deleted=0)

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    event = make_message_deleted_event(chat_id=dialog_id, deleted_ids=[200])
    await manager.on_message_deleted(event)

    row = sync_db.execute(
        "SELECT is_deleted, deleted_at FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 200),
    ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] is not None


@pytest.mark.asyncio
async def test_on_message_deleted_preserves_text(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Deleting a message preserves its text column (does not clear it)."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=201, text="will be deleted", is_deleted=0)

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    event = make_message_deleted_event(chat_id=dialog_id, deleted_ids=[201])
    await manager.on_message_deleted(event)

    text = sync_db.execute(
        "SELECT text FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 201),
    ).fetchone()[0]
    assert text == "will be deleted"


@pytest.mark.asyncio
async def test_on_message_deleted_already_deleted(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Firing delete again on an already-deleted message does not re-stamp deleted_at."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    original_deleted_at = 1000
    insert_message(
        sync_db, dialog_id, message_id=202, text="already gone",
        is_deleted=1, deleted_at=original_deleted_at,
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    event = make_message_deleted_event(chat_id=dialog_id, deleted_ids=[202])
    await manager.on_message_deleted(event)

    deleted_at = sync_db.execute(
        "SELECT deleted_at FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 202),
    ).fetchone()[0]
    assert deleted_at == original_deleted_at


@pytest.mark.asyncio
async def test_on_message_deleted_updates_last_event_at(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Deleting a message in a synced dialog updates synced_dialogs.last_event_at."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=203, text="msg")

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    before = sync_db.execute(
        "SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone()[0]
    assert before is None

    event = make_message_deleted_event(chat_id=dialog_id, deleted_ids=[203])
    await manager.on_message_deleted(event)

    after = sync_db.execute(
        "SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone()[0]
    assert after is not None


# ---------------------------------------------------------------------------
# DAEMON-10: DM deletes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_deleted_dm_logs_debug(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    caplog: Any,
) -> None:
    """MessageDeleted with chat_id=None logs DEBUG and makes no DB changes."""
    import logging

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    event = make_message_deleted_event(chat_id=None, deleted_ids=[555, 556])

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.event_handlers"):
        await manager.on_message_deleted(event)

    count = sync_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0

    # Log should mention the MTProto limitation
    assert any("MTProto limitation" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_gap_scan_marks_deleted(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Gap scan marks message absent in Telegram (None returned) as is_deleted=1."""
    dialog_id = 2001
    insert_synced_dialog(sync_db, dialog_id)
    # Pre-insert 3 messages
    for msg_id in [10, 20, 30]:
        insert_message(sync_db, dialog_id, message_id=msg_id, text=f"msg {msg_id}")

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    # client.get_messages returns: msg10 present, msg20 absent (None), msg30 present
    msg10 = build_mock_message(id=10)
    msg30 = build_mock_message(id=30)
    mock_client.get_messages = AsyncMock(return_value=[msg10, None, msg30])

    deleted_count = await manager.run_dm_gap_scan()

    assert deleted_count == 1
    row = sync_db.execute(
        "SELECT is_deleted FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 20),
    ).fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_gap_scan_skips_unsynced_messages(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Gap scan does not mark messages whose sent_at >= scan_start as deleted."""
    dialog_id = 2001
    insert_synced_dialog(sync_db, dialog_id)

    # Insert a message with a very large sent_at (in the future — arrived during scan)
    future_sent_at = 9_999_999_999  # far future UNIX timestamp
    sync_db.execute(
        "INSERT OR REPLACE INTO messages "
        "(dialog_id, message_id, sent_at, text, sender_id, sender_first_name, "
        "media_description, reply_to_msg_id, forum_topic_id, reactions, is_deleted) "
        "VALUES (?, ?, ?, 'future msg', 42, 'Alice', NULL, NULL, NULL, NULL, 0)",
        (dialog_id, 777, future_sent_at),
    )
    sync_db.commit()

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    # The future message is excluded from scan (sent_at > scan_start), so get_messages
    # is called with an empty list — return value doesn't matter
    mock_client.get_messages = AsyncMock(return_value=[])

    deleted_count = await manager.run_dm_gap_scan()

    assert deleted_count == 0
    row = sync_db.execute(
        "SELECT is_deleted FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 777),
    ).fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_adds_handlers(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """register() calls client.add_event_handler exactly 3 times."""
    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    assert mock_client.add_event_handler.call_count == 3


def test_unregister_removes_handlers(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """unregister() calls client.remove_event_handler exactly 3 times."""
    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()
    manager.unregister()

    assert mock_client.remove_event_handler.call_count == 3


def test_refresh_synced_dialogs(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """refresh_synced_dialogs() picks up dialogs inserted after manager creation."""
    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    # At registration time, no dialogs present
    assert 3001 not in manager._synced_dialog_ids

    # Insert a new dialog directly
    insert_synced_dialog(sync_db, 3001)

    # Before refresh, still not present
    assert 3001 not in manager._synced_dialog_ids

    # After refresh, now present
    manager.refresh_synced_dialogs()
    assert 3001 in manager._synced_dialog_ids


# ---------------------------------------------------------------------------
# DAEMON-11: access_lost filtering — refresh and gap scan
# ---------------------------------------------------------------------------


def test_refresh_excludes_access_lost(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='access_lost' is NOT in _synced_dialog_ids after refresh."""
    # Insert access_lost dialog
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9901, 'access_lost')",
    )
    # Insert synced dialog (should still appear)
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9902, 'synced')",
    )
    sync_db.commit()

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    assert 9901 not in manager._synced_dialog_ids, (
        "access_lost dialog must be excluded from _synced_dialog_ids"
    )
    assert 9902 in manager._synced_dialog_ids, (
        "synced dialog must remain in _synced_dialog_ids"
    )


def test_refresh_includes_syncing(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='syncing' IS in _synced_dialog_ids after refresh (only access_lost excluded)."""
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9903, 'syncing')",
    )
    sync_db.commit()

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    assert 9903 in manager._synced_dialog_ids, (
        "syncing dialog must be included in _synced_dialog_ids"
    )


@pytest.mark.asyncio
async def test_gap_scan_excludes_syncing_dialogs(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='syncing' is NOT scanned by run_dm_gap_scan."""
    dialog_id = 9910
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'syncing')",
        (dialog_id,),
    )
    for msg_id in [1, 2, 3]:
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, ?, 1000000000)",
            (dialog_id, msg_id),
        )
    sync_db.commit()

    get_messages_calls: list[Any] = []

    async def _get_messages(entity: Any, ids: Any) -> list[Any]:
        get_messages_calls.append(entity)
        return []

    mock_client.get_messages = _get_messages

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    await manager.run_dm_gap_scan()

    assert dialog_id not in get_messages_calls, (
        f"'syncing' dialog {dialog_id} must not be scanned"
    )


@pytest.mark.asyncio
async def test_gap_scan_excludes_access_lost_dialogs(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='access_lost' is NOT scanned by run_dm_gap_scan."""
    dialog_id = 9911
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'access_lost')",
        (dialog_id,),
    )
    for msg_id in [1, 2, 3]:
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, ?, 1000000000)",
            (dialog_id, msg_id),
        )
    sync_db.commit()

    get_messages_calls: list[Any] = []

    async def _get_messages(entity: Any, ids: Any) -> list[Any]:
        get_messages_calls.append(entity)
        return []

    mock_client.get_messages = _get_messages

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    await manager.run_dm_gap_scan()

    assert dialog_id not in get_messages_calls, (
        f"'access_lost' dialog {dialog_id} must not be scanned"
    )


@pytest.mark.asyncio
async def test_gap_scan_only_synced(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Only dialogs with status='synced' are scanned by run_dm_gap_scan."""
    # synced dialog — should be scanned
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9920, 'synced')",
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (9920, 1, 1000000000)",
    )
    # syncing dialog — must NOT be scanned
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9921, 'syncing')",
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (9921, 2, 1000000000)",
    )
    # access_lost dialog — must NOT be scanned
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9922, 'access_lost')",
    )
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (9922, 3, 1000000000)",
    )
    sync_db.commit()

    scanned: list[Any] = []

    async def _get_messages(entity: Any, ids: Any) -> list[Any]:
        scanned.append(entity)
        return [None] * len(ids)  # return Nones (all "deleted") — not relevant to test

    mock_client.get_messages = _get_messages

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    await manager.run_dm_gap_scan()

    assert 9920 in scanned, "synced dialog 9920 must be scanned"
    assert 9921 not in scanned, "syncing dialog 9921 must NOT be scanned"
    assert 9922 not in scanned, "access_lost dialog 9922 must NOT be scanned"


# ---------------------------------------------------------------------------
# Phase 29-02: FTS population in EventHandlerManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_new_message_populates_fts(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """on_new_message() inserts a corresponding row into messages_fts."""
    dialog_id = 8001
    insert_synced_dialog(sync_db, dialog_id)

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(id=500, text="написал сообщение")
    event = make_new_message_event(chat_id=dialog_id, message=msg)
    await manager.on_new_message(event)

    fts_row = sync_db.execute(
        "SELECT dialog_id, message_id, stemmed_text FROM messages_fts "
        "WHERE dialog_id=? AND message_id=?",
        (dialog_id, 500),
    ).fetchone()
    assert fts_row is not None, "messages_fts must have a row for the new message"
    assert fts_row[0] == dialog_id
    assert fts_row[1] == 500
    assert fts_row[2] != "", "stemmed_text must be non-empty"


@pytest.mark.asyncio
async def test_on_message_edited_updates_fts(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """on_message_edited() updates the FTS entry with the new stemmed text."""
    dialog_id = 8002
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=600, text="old text here")
    # Pre-populate FTS with old text
    sync_db.execute(
        "INSERT OR REPLACE INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
        (dialog_id, 600, "old text here"),
    )
    sync_db.commit()

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    edit_dt = datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    msg = build_mock_message(id=600, text="new edited content", edit_date=edit_dt)
    event = make_message_edited_event(chat_id=dialog_id, message=msg)
    await manager.on_message_edited(event)

    fts_row = sync_db.execute(
        "SELECT stemmed_text FROM messages_fts WHERE dialog_id=? AND message_id=?",
        (dialog_id, 600),
    ).fetchone()
    assert fts_row is not None, "messages_fts row must exist after edit"
    # stemmed text must differ from the original stub
    assert fts_row[0] != "old text here", (
        "FTS stemmed_text must be updated after message edit"
    )
