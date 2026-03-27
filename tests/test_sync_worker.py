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

from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_worker import FullSyncWorker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_message(
    id: int,  # noqa: A002
    text: str | None = "hello",
    sender_id: int | None = 42,
    sender_first_name: str | None = "Alice",
    media: object | None = None,
    reply_to_msg_id: int | None = None,
    forum_topic: bool = False,
    reply_to_top_id: int | None = None,
    reactions: object | None = None,
) -> SimpleNamespace:
    """Build a minimal Telethon-like message object."""
    from datetime import datetime, timezone

    sender = SimpleNamespace(first_name=sender_first_name) if sender_first_name is not None else None

    reply_to: SimpleNamespace | None = None
    if reply_to_msg_id is not None or forum_topic:
        reply_to = SimpleNamespace(
            reply_to_msg_id=reply_to_msg_id,
            forum_topic=forum_topic,
            reply_to_top_id=reply_to_top_id,
        )

    return SimpleNamespace(
        id=id,
        date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        message=text,
        sender_id=sender_id,
        sender=sender,
        media=media,
        reply_to=reply_to,
        reactions=reactions,
    )


def make_reactions(counts: dict[str, int]) -> SimpleNamespace:
    """Build a mock MessageReactions object."""
    results = [
        SimpleNamespace(reaction=SimpleNamespace(emoticon=emoji), count=count)
        for emoji, count in counts.items()
    ]
    return SimpleNamespace(results=results)


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
        make_mock_message(id=300, text="msg 300", sender_id=10),
        make_mock_message(id=200, text="msg 200", sender_id=10),
        make_mock_message(id=100, text="msg 100", sender_id=10),
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

    msg = make_mock_message(
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

    reactions = make_reactions({"👍": 3, "❤": 1})
    msg = make_mock_message(id=600, text="liked", reactions=reactions)

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
        make_mock_message(id=300),
        make_mock_message(id=200),
        make_mock_message(id=100),
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

    msgs = [make_mock_message(id=i) for i in range(50, 0, -1)]  # 50 messages

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

    msgs = [make_mock_message(id=i) for i in range(100, 0, -1)]  # exactly 100

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
    """Non-access-loss RPCError returns (progress, True) without setting access_lost status."""
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

    assert result is True
    row = sync_db.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] != "access_lost", f"Generic RPCError must not set access_lost, got {row[0]}"
