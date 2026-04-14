"""Tests for FullSyncWorker — TDD RED phase.

Covers DAEMON-03 (full fetch), DAEMON-04 (resume), DAEMON-05 (FloodWait),
and DAEMON-06 (DM bootstrap) behaviors.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import build_mock_message, build_mock_reactions
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_worker import FullSyncWorker


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
    """Return a mock TelegramClient with async iter_messages/iter_dialogs."""
    client = MagicMock()
    client.is_connected.return_value = True
    return client


@pytest.fixture()
def shutdown_event() -> asyncio.Event:
    """Return an unset asyncio.Event (worker should process normally)."""
    return asyncio.Event()


def make_worker(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> FullSyncWorker:
    return FullSyncWorker(mock_client, sync_db, shutdown_event)


# ---------------------------------------------------------------------------
# DAEMON-03: Full fetch — stores messages in messages table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_sync_stores_all_messages(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """process_one_batch() stores all 3 messages in sync.db messages table."""
    dialog_id = 1001
    # Enroll dialog in synced_dialogs
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    msgs = [
        build_mock_message(id=300, text="msg 300", sender_id=10),
        build_mock_message(id=200, text="msg 200", sender_id=10),
        build_mock_message(id=100, text="msg 100", sender_id=10),
    ]

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        for m in msgs:
            yield m

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()

    rows = sync_db.execute(
        "SELECT message_id, text, sender_id FROM messages WHERE dialog_id = ? ORDER BY message_id DESC",
        (dialog_id,),
    ).fetchall()
    assert len(rows) == 3
    assert rows[0] == (300, "msg 300", 10)
    assert rows[1] == (200, "msg 200", 10)
    assert rows[2] == (100, "msg 100", 10)


@pytest.mark.asyncio
async def test_message_fields_extracted_correctly(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Message with media, reply_to, forum_topic — all fields stored correctly."""
    dialog_id = 1002
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    MediaDocumentClass = type("MessageMediaDocument", (), {})
    media_obj = MediaDocumentClass()

    msg = build_mock_message(
        id=500,
        text="has media",
        sender_id=99,
        sender_first_name="Bob",
        media=media_obj,
        reply_to_msg_id=400,
        forum_topic=True,
        reply_to_top_id=1,
    )

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        yield msg

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()

    row = sync_db.execute(
        "SELECT message_id, sender_id, sender_first_name, media_description, "
        "reply_to_msg_id, forum_topic_id FROM messages WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == 500
    assert row[1] == 99
    assert row[2] == "Bob"
    assert row[3] is not None  # media_description populated
    assert row[4] == 400
    assert row[5] == 1


@pytest.mark.asyncio
async def test_reactions_serialized_as_json(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Reactions are stored as JSON dict {emoji: count}."""
    dialog_id = 1003
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    reactions = build_mock_reactions({"👍": 3, "❤": 1})
    msg = build_mock_message(id=600, text="liked", reactions=reactions)

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        yield msg

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()

    row = sync_db.execute(
        "SELECT reactions FROM messages WHERE dialog_id = ? AND message_id = 600",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    parsed = json.loads(row[0])
    assert parsed == {"👍": 3, "❤": 1}


# ---------------------------------------------------------------------------
# DAEMON-04: Resume — uses offset_id from sync_progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_from_checkpoint(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """process_one_batch() passes offset_id=sync_progress to iter_messages."""
    dialog_id = 2001
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 500)",
        (dialog_id,),
    )
    sync_db.commit()

    captured_kwargs: dict[str, Any] = {}

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        captured_kwargs.update(kwargs)
        # Return empty to mark complete
        return
        yield  # make it an async generator

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()

    assert captured_kwargs.get("offset_id") == 500


@pytest.mark.asyncio
async def test_progress_atomic_commit(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """After batch of ids [300, 200, 100], sync_progress equals 100 (min id)."""
    dialog_id = 2002
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    msgs = [
        build_mock_message(id=300),
        build_mock_message(id=200),
        build_mock_message(id=100),
    ]

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        for m in msgs:
            yield m

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()

    row = sync_db.execute(
        "SELECT sync_progress FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == 100


# ---------------------------------------------------------------------------
# DAEMON-05: FloodWait — sleep and return without advancing progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_floodwait_sleep_continues(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """FloodWaitError causes interruptible sleep and returns (progress, False)."""
    from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

    dialog_id = 3001
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    err = FloodWaitError(request=None)
    err.seconds = 5

    async def _iter_messages_flood(**kwargs: Any):  # noqa: ANN202
        raise err
        yield  # make it an async generator

    mock_client.iter_messages = _iter_messages_flood

    slept_for: list[float] = []

    async def _mock_wait_for(coro: Any, timeout: float) -> None:  # noqa: ANN401
        slept_for.append(timeout)
        raise asyncio.TimeoutError

    worker = make_worker(mock_client, sync_db, shutdown_event)

    with patch("mcp_telegram.sync_worker.asyncio.wait_for", side_effect=_mock_wait_for):
        result = await worker.process_one_batch()

    # Must return False (not done) after FloodWait
    assert result is False
    # asyncio.wait_for must have been called with the FloodWait duration
    assert slept_for, "asyncio.wait_for should have been called for FloodWait sleep"
    assert slept_for[0] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_floodwait_no_progress_loss(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """sync_progress in DB has NOT changed after a FloodWait."""
    from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

    dialog_id = 3002
    initial_progress = 750
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', ?)",
        (dialog_id, initial_progress),
    )
    sync_db.commit()

    err = FloodWaitError(request=None)
    err.seconds = 2

    async def _iter_messages_flood(**kwargs: Any):  # noqa: ANN202
        raise err
        yield

    mock_client.iter_messages = _iter_messages_flood

    async def _mock_wait_for(coro: Any, timeout: float) -> None:  # noqa: ANN401
        raise asyncio.TimeoutError

    worker = make_worker(mock_client, sync_db, shutdown_event)

    with patch("mcp_telegram.sync_worker.asyncio.wait_for", side_effect=_mock_wait_for):
        await worker.process_one_batch()

    row = sync_db.execute(
        "SELECT sync_progress FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == initial_progress, "sync_progress must not change after FloodWait"


# ---------------------------------------------------------------------------
# DAEMON-06: DM bootstrap — enrolls User dialogs only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_bootstrap_enrolls_users(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() enrolls exactly 2 User dialogs (not channels)."""
    from telethon.tl import types  # type: ignore[import-untyped]

    user1 = MagicMock(spec=types.User)
    user2 = MagicMock(spec=types.User)
    channel = MagicMock(spec=types.Channel)

    dialog1 = SimpleNamespace(entity=user1, id=10001)
    dialog2 = SimpleNamespace(entity=user2, id=10002)
    dialog3 = SimpleNamespace(entity=channel, id=10003)

    async def _iter_dialogs():  # noqa: ANN202
        for d in [dialog1, dialog2, dialog3]:
            yield d

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    count = await worker.bootstrap_dms()

    assert count == 2

    rows = sync_db.execute(
        "SELECT dialog_id, status FROM synced_dialogs ORDER BY dialog_id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0] == (10001, "syncing")
    assert rows[1] == (10002, "syncing")


@pytest.mark.asyncio
async def test_dm_bootstrap_skips_groups(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() skips Chat and Channel entities — 0 rows inserted."""
    from telethon.tl import types  # type: ignore[import-untyped]

    chat = MagicMock(spec=types.Chat)
    channel = MagicMock(spec=types.Channel)

    dialog1 = SimpleNamespace(entity=chat, id=20001)
    dialog2 = SimpleNamespace(entity=channel, id=20002)

    async def _iter_dialogs():  # noqa: ANN202
        for d in [dialog1, dialog2]:
            yield d

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    count = await worker.bootstrap_dms()

    assert count == 0
    rows = sync_db.execute("SELECT COUNT(*) FROM synced_dialogs").fetchone()
    assert rows[0] == 0


@pytest.mark.asyncio
async def test_dm_bootstrap_idempotent(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() does NOT overwrite existing synced_dialogs rows."""
    from telethon.tl import types  # type: ignore[import-untyped]

    dialog_id = 30001
    # Pre-insert with 'synced' status and real progress
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'synced', 999)",
        (dialog_id,),
    )
    sync_db.commit()

    user = MagicMock(spec=types.User)
    dialog = SimpleNamespace(entity=user, id=dialog_id)

    async def _iter_dialogs():  # noqa: ANN202
        yield dialog

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    count = await worker.bootstrap_dms()

    # count should be 0 (INSERT OR IGNORE — already exists)
    assert count == 0

    row = sync_db.execute(
        "SELECT status, sync_progress FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "synced", "status must not be overwritten"
    assert row[1] == 999, "sync_progress must not be overwritten"


# ---------------------------------------------------------------------------
# bootstrap_dms() — entity population
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_bootstrap_populates_entities(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() writes an entities row for each User dialog."""
    from telethon.tl import types  # type: ignore[import-untyped]

    user = MagicMock(spec=types.User)
    user.first_name = "Ivan"
    user.last_name = "Zakazov"
    user.username = "ivan_z"
    dialog = SimpleNamespace(entity=user, id=40001)

    async def _iter_dialogs():  # noqa: ANN202
        yield dialog

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.bootstrap_dms()

    row = sync_db.execute(
        "SELECT id, type, name, username, name_normalized FROM entities WHERE id=?",
        (40001,),
    ).fetchone()
    assert row is not None, "entities row must be written for the enrolled user"
    assert row[1] == "user"
    assert row[2] == "Ivan Zakazov"
    assert row[3] == "ivan_z"
    assert row[4] == "ivan zakazov"  # latinize("Ivan Zakazov")


@pytest.mark.asyncio
async def test_dm_bootstrap_entity_backfills_existing_enrollment(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() writes entity even for already-enrolled dialogs (fixes existing gap)."""
    from telethon.tl import types  # type: ignore[import-untyped]

    dialog_id = 40002
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'synced', 500)",
        (dialog_id,),
    )
    sync_db.commit()

    user = MagicMock(spec=types.User)
    user.first_name = "Anna"
    user.last_name = "Smith"
    user.username = None
    dialog = SimpleNamespace(entity=user, id=dialog_id)

    async def _iter_dialogs():  # noqa: ANN202
        yield dialog

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    count = await worker.bootstrap_dms()

    assert count == 0  # already enrolled — no new enrollment

    row = sync_db.execute(
        "SELECT name, name_normalized FROM entities WHERE id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None, "entity must be backfilled for already-enrolled dialog"
    assert row[0] == "Anna Smith"
    assert row[1] == "anna smith"


@pytest.mark.asyncio
async def test_dm_bootstrap_skips_entity_for_nameless_user(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() does not crash or write entity when user has no display name."""
    from telethon.tl import types  # type: ignore[import-untyped]

    user = MagicMock(spec=types.User)
    user.first_name = None
    user.last_name = None
    user.username = "ghost"
    dialog = SimpleNamespace(entity=user, id=40003)

    async def _iter_dialogs():  # noqa: ANN202
        yield dialog

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.bootstrap_dms()

    row = sync_db.execute("SELECT id FROM entities WHERE id=?", (40003,)).fetchone()
    assert row is None, "no entities row must be written when name is empty"


# ---------------------------------------------------------------------------
# Completion detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_batch_marks_synced(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Empty iter_messages result marks dialog as 'synced' and returns True."""
    dialog_id = 4001
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 100)",
        (dialog_id,),
    )
    sync_db.commit()

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        return
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.process_one_batch()

    assert result is True

    row = sync_db.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "synced"


@pytest.mark.asyncio
async def test_partial_batch_marks_synced(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Batch of 50 messages (< 100) marks dialog as 'synced' after commit."""
    dialog_id = 4002
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    msgs = [build_mock_message(id=i) for i in range(50, 0, -1)]  # 50 messages

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        for m in msgs:
            yield m

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.process_one_batch()

    assert result is True

    row = sync_db.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "synced"


@pytest.mark.asyncio
async def test_full_batch_not_marked_synced(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Batch of exactly 100 messages keeps dialog status as 'syncing'."""
    dialog_id = 4003
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    msgs = [build_mock_message(id=i) for i in range(100, 0, -1)]  # exactly 100

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        for m in msgs:
            yield m

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.process_one_batch()

    assert result is False

    row = sync_db.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "syncing"


@pytest.mark.asyncio
async def test_no_pending_dialogs_returns_true(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """process_one_batch() returns True when no dialogs have pending status."""
    # All dialogs already synced
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (5001, 'synced')",
    )
    sync_db.commit()

    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.process_one_batch()

    assert result is True


# ---------------------------------------------------------------------------
# DAEMON-11: Access-loss classification in _fetch_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_lost_channel_private(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """ChannelPrivateError sets status='access_lost' and access_lost_at != NULL, returns True."""
    from telethon.errors import ChannelPrivateError  # type: ignore[import-untyped]

    dialog_id = 6001
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    err = ChannelPrivateError(request=None)

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        raise err
        yield  # make it an async generator

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.process_one_batch()

    assert result is True
    row = sync_db.execute(
        "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "access_lost"
    assert row[1] is not None


@pytest.mark.asyncio
async def test_access_lost_chat_forbidden(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """ChatForbiddenError sets status='access_lost' and access_lost_at != NULL."""
    from telethon.errors import ChatForbiddenError  # type: ignore[import-untyped]

    dialog_id = 6002
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    err = ChatForbiddenError(request=None)

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.process_one_batch()

    assert result is True
    row = sync_db.execute(
        "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "access_lost"
    assert row[1] is not None


@pytest.mark.asyncio
async def test_access_lost_user_banned(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """UserBannedInChannelError sets status='access_lost' and access_lost_at != NULL."""
    from telethon.errors import UserBannedInChannelError  # type: ignore[import-untyped]

    dialog_id = 6003
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    err = UserBannedInChannelError(request=None)

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.process_one_batch()

    assert result is True
    row = sync_db.execute(
        "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "access_lost"
    assert row[1] is not None


@pytest.mark.asyncio
async def test_access_lost_preserves_messages(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """After access_lost, messages table rows for dialog still exist (not deleted)."""
    from telethon.errors import ChannelPrivateError  # type: ignore[import-untyped]

    dialog_id = 6004
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    # Pre-insert messages
    for msg_id in [10, 20, 30]:
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, ?, 1704067200)",
            (dialog_id, msg_id),
        )
    sync_db.commit()

    err = ChannelPrivateError(request=None)

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()

    count = sync_db.execute(
        "SELECT COUNT(*) FROM messages WHERE dialog_id=?", (dialog_id,)
    ).fetchone()[0]
    assert count == 3, f"Expected 3 messages preserved, got {count}"


@pytest.mark.asyncio
async def test_generic_rpc_error_still_skips(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Non-access-loss RPCError leaves dialog in-progress (is_done=False) without setting access_lost status."""
    from telethon.errors import RPCError  # type: ignore[import-untyped]

    dialog_id = 6005
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    err = RPCError(request=None, message="SOME_GENERIC_ERROR", code=400)

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        raise err
        yield

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    result = await worker.process_one_batch()

    assert result is False, "Generic RPCError should leave dialog in-progress (is_done=False)"
    row = sync_db.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] != "access_lost", f"Generic RPCError must not set access_lost, got {row[0]}"
    assert row[0] == "syncing", f"Generic RPCError must leave dialog as syncing for retry, got {row[0]}"


# ---------------------------------------------------------------------------
# Phase 29-02: FTS population in FullSyncWorker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_batch_populates_fts(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """After process_one_batch(), messages_fts has matching rows with non-empty stemmed_text."""
    dialog_id = 7001
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    msgs = [
        build_mock_message(id=101, text="написал сообщение"),
        build_mock_message(id=102, text="hello world"),
        build_mock_message(id=103, text="third message"),
    ]

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        for m in msgs:
            yield m

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()

    fts_rows = sync_db.execute(
        "SELECT dialog_id, message_id, stemmed_text FROM messages_fts "
        "WHERE dialog_id = ? ORDER BY message_id",
        (dialog_id,),
    ).fetchall()
    assert len(fts_rows) == 3, f"Expected 3 FTS rows, got {len(fts_rows)}"
    for row in fts_rows:
        assert row[0] == dialog_id
        assert row[2] != "", "stemmed_text must be non-empty for messages with text"


@pytest.mark.asyncio
async def test_process_one_batch_fts_matches_message_ids(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """FTS rows have message_ids matching those in the messages table."""
    dialog_id = 7002
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    msgs = [build_mock_message(id=200, text="test"), build_mock_message(id=201, text="data")]

    async def _iter_messages(**kwargs: Any):  # noqa: ANN202
        for m in msgs:
            yield m

    mock_client.iter_messages = _iter_messages

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()

    fts_ids = {
        row[0]
        for row in sync_db.execute(
            "SELECT message_id FROM messages_fts WHERE dialog_id = ?", (dialog_id,)
        ).fetchall()
    }
    assert fts_ids == {200, 201}


# ---------------------------------------------------------------------------
# bootstrap_dms error handling (H-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_bootstrap_handles_flood_wait(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() catches FloodWaitError and commits partial progress."""
    from telethon.tl import types  # type: ignore[import-untyped]
    from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

    user = MagicMock(spec=types.User)
    dialog = SimpleNamespace(entity=user, id=40001)

    call_count = 0

    async def _iter_dialogs():
        nonlocal call_count
        yield dialog
        call_count += 1
        raise FloodWaitError(request=None, capture=42)

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    count = await worker.bootstrap_dms()

    assert count == 1, "should have enrolled the dialog yielded before the error"
    row = sync_db.execute(
        "SELECT dialog_id FROM synced_dialogs WHERE dialog_id = ?", (40001,)
    ).fetchone()
    assert row is not None, "partial progress should be committed"


@pytest.mark.asyncio
async def test_dm_bootstrap_handles_rpc_error(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() catches RPCError and commits partial progress."""
    from telethon.errors import RPCError  # type: ignore[import-untyped]

    async def _iter_dialogs():  # noqa: ANN202
        raise RPCError(request=None, message="TEST_ERROR", code=400)
        yield  # make it an async generator  # noqa: unreachable

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    count = await worker.bootstrap_dms()

    assert count == 0, "no dialogs enrolled on immediate error"


@pytest.mark.asyncio
async def test_dm_bootstrap_handles_network_error(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() catches OSError and doesn't crash."""
    async def _iter_dialogs():  # noqa: ANN202
        raise OSError("Connection reset")
        yield  # make it an async generator  # noqa: unreachable

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    count = await worker.bootstrap_dms()
    assert count == 0


# ---------------------------------------------------------------------------
# extract_reply_and_topic shared helper (M-9)
# ---------------------------------------------------------------------------


def test_extract_reply_and_topic_no_reply():
    from mcp_telegram.sync_worker import extract_reply_and_topic

    msg = SimpleNamespace(reply_to=None)
    reply_id, topic_id = extract_reply_and_topic(msg)
    assert reply_id is None
    assert topic_id is None


def test_extract_reply_and_topic_simple_reply():
    from mcp_telegram.sync_worker import extract_reply_and_topic

    reply_to = SimpleNamespace(
        reply_to_msg_id=42, forum_topic=False, reply_to_reply_top_id=None,
    )
    msg = SimpleNamespace(reply_to=reply_to)
    reply_id, topic_id = extract_reply_and_topic(msg)
    assert reply_id == 42
    assert topic_id is None


def test_extract_reply_and_topic_forum_with_top_id():
    from mcp_telegram.sync_worker import extract_reply_and_topic

    reply_to = SimpleNamespace(
        reply_to_msg_id=100, forum_topic=True, reply_to_reply_top_id=7,
    )
    msg = SimpleNamespace(reply_to=reply_to)
    reply_id, topic_id = extract_reply_and_topic(msg)
    assert reply_id == 100
    assert topic_id == 7


def test_extract_reply_and_topic_forum_general():
    """forum_topic=True with no reply_to_reply_top_id → General topic (id=1)."""
    from mcp_telegram.sync_worker import extract_reply_and_topic

    reply_to = SimpleNamespace(
        reply_to_msg_id=200, forum_topic=True, reply_to_reply_top_id=None,
    )
    msg = SimpleNamespace(reply_to=reply_to)
    reply_id, topic_id = extract_reply_and_topic(msg)
    assert reply_id == 200
    assert topic_id == 1
