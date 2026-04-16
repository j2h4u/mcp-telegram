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

from helpers import MockTotalList, build_mock_message, build_mock_reactions
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

    mock_client.get_messages = AsyncMock(return_value=MockTotalList(msgs, total=len(msgs)))

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

    mock_client.get_messages = AsyncMock(return_value=MockTotalList([msg], total=1))

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
async def test_extract_reactions_rows(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """Reactions are stored as rows in message_reactions table (not JSON blob)."""
    from mcp_telegram.sync_worker import extract_message_row

    dialog_id = 1003
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()

    reactions = build_mock_reactions({"👍": 3, "❤": 1})
    msg = build_mock_message(id=600, text="liked", reactions=reactions)

    result = extract_message_row(dialog_id, msg)

    # result.reactions is list of tuples, not JSON
    assert isinstance(result.reactions, list)
    assert len(result.reactions) == 2
    emojis = {r[2] for r in result.reactions}
    assert emojis == {"👍", "❤"}

    # result.row does NOT contain any JSON string
    for item in result.row:
        assert not isinstance(item, str) or not item.startswith("{"), (
            f"row should not contain JSON reactions, got: {item!r}"
        )


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

    mock_client.get_messages = AsyncMock(return_value=MockTotalList([], total=0))

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()

    assert mock_client.get_messages.call_args.kwargs.get("offset_id") == 500


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

    mock_client.get_messages = AsyncMock(return_value=MockTotalList(msgs, total=len(msgs)))

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

    mock_client.get_messages = AsyncMock(side_effect=err)

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

    mock_client.get_messages = AsyncMock(side_effect=err)

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
    assert row[1] == "User"
    assert row[2] == "Ivan Zakazov"
    assert row[3] == "ivan_z"
    assert row[4] == "ivan zakazov"  # latinize("Ivan Zakazov")


@pytest.mark.asyncio
async def test_dm_bootstrap_populates_entities_bot(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() writes type='Bot' for a user entity with bot=True."""
    from telethon.tl import types  # type: ignore[import-untyped]

    user = MagicMock(spec=types.User)
    user.first_name = "BotFather"
    user.last_name = None
    user.username = "BotFather"
    user.bot = True
    dialog = SimpleNamespace(entity=user, id=40099)

    async def _iter_dialogs():  # noqa: ANN202
        yield dialog

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.bootstrap_dms()

    row = sync_db.execute(
        "SELECT type FROM entities WHERE id=?",
        (40099,),
    ).fetchone()
    assert row is not None
    assert row[0] == "Bot"


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
async def test_dm_bootstrap_writes_tombstone_for_nameless_user(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """bootstrap_dms() writes entity row with name=NULL when user has no display name.

    Invariant: every enrolled dialog must have an entity row after bootstrap so
    that absence of a row unambiguously means "never enrolled", not "enrolled
    but nameless".
    """
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

    row = sync_db.execute(
        "SELECT name, username FROM entities WHERE id=?", (40003,)
    ).fetchone()
    assert row is not None, "entity row must exist even when display name is empty"
    assert row[0] is None, "name must be NULL for nameless user"
    assert row[1] == "ghost", "username must be preserved"


@pytest.mark.asyncio
async def test_dm_bootstrap_invariant_every_enrolled_dialog_has_entity(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """After bootstrap_dms(), every enrolled dialog must have an entity row — named or not."""
    from telethon.tl import types  # type: ignore[import-untyped]

    def _make_user(first: str | None, last: str | None, username: str | None, dialog_id: int) -> SimpleNamespace:
        user = MagicMock(spec=types.User)
        user.first_name = first
        user.last_name = last
        user.username = username
        return SimpleNamespace(entity=user, id=dialog_id)

    dialogs = [
        _make_user("Ivan", "Zakazov", "ivan_z", 50001),
        _make_user(None, None, "ghost_bot", 50002),
        _make_user(None, None, None, 50003),
    ]

    async def _iter_dialogs():  # noqa: ANN202
        for d in dialogs:
            yield d

    mock_client.iter_dialogs = _iter_dialogs

    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.bootstrap_dms()

    enrolled = {row[0] for row in sync_db.execute("SELECT dialog_id FROM synced_dialogs").fetchall()}
    entities = {row[0] for row in sync_db.execute("SELECT id FROM entities").fetchall()}
    assert enrolled == entities, f"dialogs without entity rows: {enrolled - entities}"


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

    mock_client.get_messages = AsyncMock(return_value=MockTotalList([], total=100))

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

    mock_client.get_messages = AsyncMock(return_value=MockTotalList(msgs, total=50))

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

    mock_client.get_messages = AsyncMock(return_value=MockTotalList(msgs, total=1000))

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

    mock_client.get_messages = AsyncMock(side_effect=err)

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

    mock_client.get_messages = AsyncMock(side_effect=err)

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

    mock_client.get_messages = AsyncMock(side_effect=err)

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

    mock_client.get_messages = AsyncMock(side_effect=err)

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

    mock_client.get_messages = AsyncMock(side_effect=err)

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

    mock_client.get_messages = AsyncMock(return_value=MockTotalList(msgs, total=len(msgs)))

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

    mock_client.get_messages = AsyncMock(return_value=MockTotalList(msgs, total=len(msgs)))

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


# ---------------------------------------------------------------------------
# Phase 36-01: total_messages and last_synced_at writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_messages_written_on_batch(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """_fetch_batch writes total_messages from result.total to synced_dialogs."""
    dialog_id = 8001
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()
    msgs = [build_mock_message(id=300), build_mock_message(id=200), build_mock_message(id=100)]
    mock_client.get_messages = AsyncMock(return_value=MockTotalList(msgs, total=9999))
    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()
    row = sync_db.execute(
        "SELECT total_messages FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)
    ).fetchone()
    assert row[0] == 9999


@pytest.mark.asyncio
async def test_last_synced_at_set_on_empty_batch(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """When _fetch_batch gets empty batch (is_done=True), last_synced_at is set."""
    dialog_id = 8002
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 100)",
        (dialog_id,),
    )
    sync_db.commit()
    mock_client.get_messages = AsyncMock(return_value=MockTotalList([], total=200))
    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()
    row = sync_db.execute(
        "SELECT last_synced_at, status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)
    ).fetchone()
    assert row[0] is not None  # last_synced_at is set
    assert row[1] == "synced"


@pytest.mark.asyncio
async def test_last_synced_at_set_on_partial_batch(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """When _fetch_batch gets partial batch (< 100 msgs), last_synced_at is set."""
    dialog_id = 8003
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 0)",
        (dialog_id,),
    )
    sync_db.commit()
    msgs = [build_mock_message(id=50), build_mock_message(id=40)]  # < 100 = partial
    mock_client.get_messages = AsyncMock(return_value=MockTotalList(msgs, total=200))
    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()
    row = sync_db.execute(
        "SELECT last_synced_at, status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)
    ).fetchone()
    assert row[0] is not None  # last_synced_at is set
    assert row[1] == "synced"


@pytest.mark.asyncio
async def test_total_messages_written_on_completion(
    mock_client: MagicMock,
    sync_db: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    """total_messages is written even on the completion (empty) batch."""
    dialog_id = 8004
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, sync_progress) VALUES (?, 'syncing', 100)",
        (dialog_id,),
    )
    sync_db.commit()
    mock_client.get_messages = AsyncMock(return_value=MockTotalList([], total=5000))
    worker = make_worker(mock_client, sync_db, shutdown_event)
    await worker.process_one_batch()
    row = sync_db.execute(
        "SELECT total_messages FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)
    ).fetchone()
    assert row[0] == 5000


# ---------------------------------------------------------------------------
# ExtractedMessage and extraction helpers (v7 write path)
# ---------------------------------------------------------------------------


def test_extract_reactions_rows_returns_empty_for_none() -> None:
    """extract_reactions_rows(1, 1, None) returns empty list."""
    from mcp_telegram.sync_worker import extract_reactions_rows

    assert extract_reactions_rows(1, 1, None) == []


def test_extract_entity_rows_mention_and_bold(monkeypatch: Any) -> None:
    """Only mention is extracted; bold is skipped (not in analytics types)."""
    from mcp_telegram.sync_worker import extract_entity_rows, _ANALYTICS_ENTITY_TYPES

    class FakeMention:
        offset = 0
        length = 6

    class FakeBold:
        offset = 7
        length = 4

    # Monkeypatch _ANALYTICS_ENTITY_TYPES to use our fake classes
    monkeypatch.setitem(_ANALYTICS_ENTITY_TYPES, FakeMention, "mention")
    # FakeBold is NOT in the dict — simulates bold/italic being excluded

    msg = SimpleNamespace(
        entities=[FakeMention(), FakeBold()],
        message="@alice world",
    )
    rows = extract_entity_rows(1, 100, msg)

    # Only the mention should be extracted
    types_found = {r[4] for r in rows}
    assert "mention" in types_found, "mention entity must be extracted"
    # Bold is not in _ANALYTICS_ENTITY_TYPES so no 'bold' type should appear
    assert all(r[4] != "bold" for r in rows), "bold must not be extracted"


def test_extract_entity_rows_hashtag_populates_value(monkeypatch: Any) -> None:
    """Hashtag entity value is the text span (not None). Priority Action #1."""
    from mcp_telegram.sync_worker import extract_entity_rows, _ANALYTICS_ENTITY_TYPES

    class FakeHashtag:
        offset = 6
        length = 7  # len("#python") = 7

    monkeypatch.setitem(_ANALYTICS_ENTITY_TYPES, FakeHashtag, "hashtag")

    msg = SimpleNamespace(
        entities=[FakeHashtag()],
        message="Check #python for updates",
    )
    rows = extract_entity_rows(1, 100, msg)

    assert len(rows) == 1
    assert rows[0][4] == "hashtag"
    assert rows[0][5] == "#python", (
        f"hashtag value should be text span '#python', got {rows[0][5]!r}"
    )


def test_extract_entity_rows_url_populates_value(monkeypatch: Any) -> None:
    """URL entity value is the text span (not None). Priority Action #1."""
    from mcp_telegram.sync_worker import extract_entity_rows, _ANALYTICS_ENTITY_TYPES

    class FakeUrl:
        offset = 6
        length = 19  # len("https://example.com") = 19

    monkeypatch.setitem(_ANALYTICS_ENTITY_TYPES, FakeUrl, "url")

    msg = SimpleNamespace(
        entities=[FakeUrl()],
        message="Visit https://example.com today",
    )
    rows = extract_entity_rows(1, 100, msg)

    assert len(rows) == 1
    assert rows[0][4] == "url"
    assert rows[0][5] == "https://example.com", (
        f"url value should be text span, got {rows[0][5]!r}"
    )


def test_extract_entity_rows_text_url(monkeypatch: Any) -> None:
    """text_url entity value is from entity.url attribute (not text span)."""
    from mcp_telegram.sync_worker import extract_entity_rows, _ANALYTICS_ENTITY_TYPES

    class FakeTextUrl:
        offset = 0
        length = 4
        url = "https://real-url.example.com"

    monkeypatch.setitem(_ANALYTICS_ENTITY_TYPES, FakeTextUrl, "text_url")

    msg = SimpleNamespace(
        entities=[FakeTextUrl()],
        message="Click here",
    )
    rows = extract_entity_rows(1, 100, msg)

    assert len(rows) == 1
    assert rows[0][4] == "text_url"
    assert rows[0][5] == "https://real-url.example.com"


def test_extract_entity_rows_mention_name(monkeypatch: Any) -> None:
    """mention_name entity value is str(user_id)."""
    from mcp_telegram.sync_worker import extract_entity_rows, _ANALYTICS_ENTITY_TYPES

    class FakeMentionName:
        offset = 0
        length = 10
        user_id = 12345

    monkeypatch.setitem(_ANALYTICS_ENTITY_TYPES, FakeMentionName, "mention_name")

    msg = SimpleNamespace(
        entities=[FakeMentionName()],
        message="Hello friend!",
    )
    rows = extract_entity_rows(1, 100, msg)

    assert len(rows) == 1
    assert rows[0][4] == "mention_name"
    assert rows[0][5] == "12345"


def test_extract_entity_rows_mention_stores_username_text_span(monkeypatch: Any) -> None:
    """Mention entity value is @username text span.

    CONTEXT.md specified value=peer_id for mention, but MessageEntityMention
    has no peer_id. @username text span is the correct value -- enables
    'who is mentioned most' analytics via GROUP BY value.
    """
    from mcp_telegram.sync_worker import extract_entity_rows, _ANALYTICS_ENTITY_TYPES

    class FakeMention:
        offset = 6   # "@alice" starts at index 6 in "Hello @alice how are you"
        length = 6   # len("@alice") == 6

    monkeypatch.setitem(_ANALYTICS_ENTITY_TYPES, FakeMention, "mention")

    msg = SimpleNamespace(
        entities=[FakeMention()],
        message="Hello @alice how are you",
    )
    rows = extract_entity_rows(1, 100, msg)

    assert len(rows) == 1
    assert rows[0][4] == "mention"
    assert rows[0][5] == "@alice", (
        f"mention value should be '@alice' text span, got {rows[0][5]!r}"
    )


def test_extract_entity_rows_uses_isinstance(monkeypatch: Any) -> None:
    """isinstance() dispatch matches subclasses of tracked entity types."""
    from mcp_telegram.sync_worker import extract_entity_rows, _ANALYTICS_ENTITY_TYPES

    class BaseHashtag:
        offset = 0
        length = 5

    class SubHashtag(BaseHashtag):
        """Subclass -- isinstance(sub, BaseHashtag) is True."""

    monkeypatch.setitem(_ANALYTICS_ENTITY_TYPES, BaseHashtag, "hashtag")

    msg = SimpleNamespace(
        entities=[SubHashtag()],
        message="#test",
    )
    rows = extract_entity_rows(1, 100, msg)

    assert len(rows) == 1, (
        "SubHashtag must be matched via isinstance() dispatch"
    )
    assert rows[0][4] == "hashtag"


def test_utf16_slice_with_emoji() -> None:
    """_utf16_slice correctly handles non-BMP emoji (2 UTF-16 code units)."""
    from mcp_telegram.sync_worker import _utf16_slice

    # Flag emoji "\U0001f1fa\U0001f1f8" (US flag) is 4 UTF-16 code units (2 surrogates each).
    # Text: "\U0001f1fa\U0001f1f8 #usa"
    # UTF-16 layout: [U+1F1FA surrogate pair = 2 units] [U+1F1F8 surrogate pair = 2 units] [space = 1] [#=1] [u=1] [s=1] [a=1]
    # offset=5 (after 4 flag units + 1 space), length=4 ("#usa")
    text = "\U0001f1fa\U0001f1f8 #usa"
    result = _utf16_slice(text, 5, 4)
    assert result == "#usa", f"Expected '#usa', got {result!r}"


def test_utf16_slice_returns_none_on_decode_error() -> None:
    """_utf16_slice returns None (not a fallback string) on decode error."""
    from mcp_telegram.sync_worker import _utf16_slice

    # Odd byte length will cause decode error (UTF-16-LE needs even number of bytes)
    # Use an offset that causes an uneven slice
    text = "ab"
    # offset=0, length=999 -- goes way past end, slice is empty bytes which decode fine
    # Use an approach: monkeypatching decode to raise -- but simpler:
    # A surrogate character in a UTF-16 slice that can't be decoded back
    # We test the IndexError path: negative offset would give empty slice which decodes OK.
    # Test with a contrived case: text with 1 char, offset=0, length=2 (requests 2 UTF-16
    # code units but the surrogate pair for a BMP char is only 1)
    # Actually the safest test: provide text="" with offset=5 -- encoded is 0 bytes,
    # byte_offset=10 > len(encoded), slice is b"" which decodes fine.
    # Let's test the UnicodeDecodeError path with actual bad data by using a raw string
    # that when re-sliced at UTF-16 byte level produces an odd surrogate half.
    # Simplest: use the function on a text containing a supplementary char at a position
    # that if offset is wrong, the decode half-splits a surrogate pair.
    # \U0001f600 encodes as 2 UTF-16 code units (surrogates).
    # Slicing at offset=1, length=1 would extract just one surrogate (half a pair) => UnicodeDecodeError
    text_with_emoji = "\U0001f600"  # 1 Python codepoint but 2 UTF-16 code units
    result = _utf16_slice(text_with_emoji, 1, 1)
    # Extracting just the second surrogate half should fail to decode
    assert result is None, f"Expected None for half-surrogate slice, got {result!r}"


def test_extract_entity_rows_skips_on_utf16_decode_error(monkeypatch: Any) -> None:
    """Entity row is SKIPPED (not stored) when _utf16_slice returns None. Priority Action #4."""
    from mcp_telegram import sync_worker
    from mcp_telegram.sync_worker import extract_entity_rows, _ANALYTICS_ENTITY_TYPES

    class FakeMention:
        offset = 0
        length = 5

    monkeypatch.setitem(_ANALYTICS_ENTITY_TYPES, FakeMention, "mention")
    # Force _utf16_slice to return None (simulates decode error)
    monkeypatch.setattr(sync_worker, "_utf16_slice", lambda text, offset, length: None)

    msg = SimpleNamespace(
        entities=[FakeMention()],
        message="@hello",
    )
    rows = extract_entity_rows(1, 100, msg)

    assert rows == [], (
        "Entity row must be SKIPPED (not stored) when _utf16_slice returns None"
    )


def test_extract_fwd_row() -> None:
    """extract_fwd_row extracts forward metadata from a message."""
    from mcp_telegram.sync_worker import extract_fwd_row
    from datetime import datetime, timezone

    fwd_date = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    fwd_from = SimpleNamespace(
        from_id=SimpleNamespace(channel_id=99999, user_id=None, chat_id=None),
        from_name="Test Channel",
        date=fwd_date,
        channel_post=42,
    )
    msg = SimpleNamespace(fwd_from=fwd_from)

    result = extract_fwd_row(1, 100, msg)

    assert result is not None
    assert result[0] == 1    # dialog_id
    assert result[1] == 100  # message_id
    assert result[2] == 99999  # fwd_from_peer_id (channel_id)
    assert result[3] == "Test Channel"
    assert result[4] == int(fwd_date.timestamp())
    assert result[5] == 42  # fwd_channel_post

    # None when no fwd_from
    msg_no_fwd = SimpleNamespace(fwd_from=None)
    assert extract_fwd_row(1, 100, msg_no_fwd) is None


def test_extract_message_row_returns_dataclass() -> None:
    """extract_message_row returns an ExtractedMessage with expected fields."""
    from mcp_telegram.sync_worker import extract_message_row, ExtractedMessage

    msg = build_mock_message(id=123, text="hello")
    result = extract_message_row(1, msg)

    assert isinstance(result, ExtractedMessage)
    assert isinstance(result.row, tuple)
    assert isinstance(result.reactions, list)
    assert isinstance(result.entities, list)
    assert result.forward is None  # build_mock_message has no fwd_from


def test_extract_message_row_populates_v7_columns() -> None:
    """extract_message_row populates edit_date, grouped_id, reply_to_peer_id."""
    from mcp_telegram.sync_worker import extract_message_row
    from datetime import datetime, timezone

    edit_dt = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)

    class FakeReplyPeer:
        channel_id = 12345
        user_id = None
        chat_id = None

    reply_to = SimpleNamespace(
        reply_to_msg_id=10,
        forum_topic=False,
        reply_to_reply_top_id=None,
        reply_to_peer_id=FakeReplyPeer(),
    )
    msg = SimpleNamespace(
        id=500,
        date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        message="test",
        sender_id=1,
        sender=SimpleNamespace(first_name="Alice"),
        media=None,
        reply_to=reply_to,
        reactions=None,
        edit_date=edit_dt,
        grouped_id=9999,
        fwd_from=None,
        entities=None,
    )
    result = extract_message_row(1, msg)

    assert result.row[9] == int(edit_dt.timestamp()), "edit_date mismatch"
    assert result.row[10] == 9999, "grouped_id mismatch"
    assert result.row[11] == 12345, "reply_to_peer_id mismatch"

    # All three as None
    msg_none = SimpleNamespace(
        id=501,
        date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        message="test",
        sender_id=1,
        sender=None,
        media=None,
        reply_to=None,
        reactions=None,
        edit_date=None,
        grouped_id=None,
        fwd_from=None,
        entities=None,
    )
    result_none = extract_message_row(1, msg_none)
    assert result_none.row[9:12] == (None, None, None)


def test_insert_messages_with_fts_writes_reactions(sync_db: sqlite3.Connection) -> None:
    """insert_messages_with_fts writes reaction rows to message_reactions table."""
    from mcp_telegram.sync_worker import insert_messages_with_fts, ExtractedMessage

    dialog_id = 9001
    message_id = 1
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (dialog_id,)
    )
    sync_db.commit()

    em = ExtractedMessage(
        row=(dialog_id, message_id, 1700000000, "hello", 1, "Alice", None, None, None, None, None, None),
        reactions=[(dialog_id, message_id, "👍", 5), (dialog_id, message_id, "❤", 2)],
    )
    with sync_db:
        insert_messages_with_fts(sync_db, [em])

    rows = sync_db.execute(
        "SELECT emoji, count FROM message_reactions WHERE dialog_id=? AND message_id=? ORDER BY emoji",
        (dialog_id, message_id),
    ).fetchall()
    reaction_dict = {r[0]: r[1] for r in rows}
    assert reaction_dict == {"👍": 5, "❤": 2}


def test_insert_messages_with_fts_writes_forwards(sync_db: sqlite3.Connection) -> None:
    """insert_messages_with_fts writes forward metadata to message_forwards table."""
    from mcp_telegram.sync_worker import insert_messages_with_fts, ExtractedMessage

    dialog_id = 9002
    message_id = 2
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (dialog_id,)
    )
    sync_db.commit()

    em = ExtractedMessage(
        row=(dialog_id, message_id, 1700000000, "forwarded", 1, "Alice", None, None, None, None, None, None),
        forward=(dialog_id, message_id, 55555, "Original Author", 1700000000, None),
    )
    with sync_db:
        insert_messages_with_fts(sync_db, [em])

    row = sync_db.execute(
        "SELECT fwd_from_peer_id, fwd_from_name FROM message_forwards WHERE dialog_id=? AND message_id=?",
        (dialog_id, message_id),
    ).fetchone()
    assert row is not None
    assert row[0] == 55555
    assert row[1] == "Original Author"


def test_insert_messages_with_fts_edit_idempotency_reactions(sync_db: sqlite3.Connection) -> None:
    """Inserting same message_id twice replaces reactions (DELETE-before-INSERT)."""
    from mcp_telegram.sync_worker import insert_messages_with_fts, ExtractedMessage

    dialog_id = 9003
    message_id = 3
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (dialog_id,)
    )
    sync_db.commit()

    # First insert: 2 reactions
    em1 = ExtractedMessage(
        row=(dialog_id, message_id, 1700000000, "v1", 1, "Alice", None, None, None, None, None, None),
        reactions=[(dialog_id, message_id, "👍", 3), (dialog_id, message_id, "❤", 1)],
    )
    with sync_db:
        insert_messages_with_fts(sync_db, [em1])

    # Second insert: 1 different reaction
    em2 = ExtractedMessage(
        row=(dialog_id, message_id, 1700000000, "v2", 1, "Alice", None, None, None, None, None, None),
        reactions=[(dialog_id, message_id, "🔥", 7)],
    )
    with sync_db:
        insert_messages_with_fts(sync_db, [em2])

    rows = sync_db.execute(
        "SELECT emoji, count FROM message_reactions WHERE dialog_id=? AND message_id=?",
        (dialog_id, message_id),
    ).fetchall()
    assert len(rows) == 1, f"Only 1 reaction row should remain after re-insert. Got: {rows}"
    assert rows[0] == ("🔥", 7)


def test_insert_messages_with_fts_edit_idempotency_entities(sync_db: sqlite3.Connection) -> None:
    """Inserting same message_id twice replaces entities (DELETE-before-INSERT)."""
    from mcp_telegram.sync_worker import insert_messages_with_fts, ExtractedMessage

    dialog_id = 9004
    message_id = 4
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (dialog_id,)
    )
    sync_db.commit()

    em1 = ExtractedMessage(
        row=(dialog_id, message_id, 1700000000, "old", 1, "Alice", None, None, None, None, None, None),
        entities=[
            (dialog_id, message_id, 0, 5, "mention", "@old1"),
            (dialog_id, message_id, 6, 5, "mention", "@old2"),
        ],
    )
    with sync_db:
        insert_messages_with_fts(sync_db, [em1])

    em2 = ExtractedMessage(
        row=(dialog_id, message_id, 1700000000, "new", 1, "Alice", None, None, None, None, None, None),
        entities=[(dialog_id, message_id, 0, 4, "hashtag", "#new")],
    )
    with sync_db:
        insert_messages_with_fts(sync_db, [em2])

    rows = sync_db.execute(
        "SELECT type, value FROM message_entities WHERE dialog_id=? AND message_id=?",
        (dialog_id, message_id),
    ).fetchall()
    assert len(rows) == 1, f"Only 1 entity row should remain. Got: {rows}"
    assert rows[0] == ("hashtag", "#new")


def test_insert_messages_with_fts_edit_idempotency_forwards(sync_db: sqlite3.Connection) -> None:
    """Inserting same message_id with no forward clears forward row."""
    from mcp_telegram.sync_worker import insert_messages_with_fts, ExtractedMessage

    dialog_id = 9005
    message_id = 5
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (dialog_id,)
    )
    sync_db.commit()

    em1 = ExtractedMessage(
        row=(dialog_id, message_id, 1700000000, "fwd msg", 1, "Alice", None, None, None, None, None, None),
        forward=(dialog_id, message_id, 12345, "Src", 1700000000, None),
    )
    with sync_db:
        insert_messages_with_fts(sync_db, [em1])

    # Verify forward exists
    fwd = sync_db.execute(
        "SELECT COUNT(*) FROM message_forwards WHERE dialog_id=? AND message_id=?",
        (dialog_id, message_id),
    ).fetchone()[0]
    assert fwd == 1

    # Re-insert with no forward
    em2 = ExtractedMessage(
        row=(dialog_id, message_id, 1700000000, "edited msg", 1, "Alice", None, None, None, None, None, None),
        forward=None,
    )
    with sync_db:
        insert_messages_with_fts(sync_db, [em2])

    fwd_after = sync_db.execute(
        "SELECT COUNT(*) FROM message_forwards WHERE dialog_id=? AND message_id=?",
        (dialog_id, message_id),
    ).fetchone()[0]
    assert fwd_after == 0, "Forward row must be cleared when re-inserted without forward"
