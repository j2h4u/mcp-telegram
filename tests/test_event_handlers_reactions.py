"""Tests for Phase 39.2-01 reaction live-sync push path.

Covers:
- AC-1 (live add via MessageEdited reactions branch)
- AC-2 (live remove via MessageEdited)
- AC-2-RAW (raw update with empty results clears rows)
- AC-7 (idempotent through both paths)
- AC-8 (regression: text-unchanged + no reactions = no-op)
- AC-UPD-USER (UpdateMessageReactions DM peer)
- AC-UPD-CHANNEL (UpdateChannelMessageReactions)
- AC-REG-IDEMPOTENT (register/unregister symmetry)
- AC-6 (FloodWait on Raw path skips DB mutation)
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodWaitError  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    PeerChannel,
    PeerUser,
    UpdateMessageReactions,
)

from helpers import build_mock_message, build_mock_reactions
from mcp_telegram.event_handlers import EventHandlerManager
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def mock_client() -> MagicMock:
    c = MagicMock()
    c.add_event_handler = MagicMock()
    c.remove_event_handler = MagicMock()
    c.get_messages = AsyncMock()
    return c


@pytest.fixture()
def shutdown_event() -> asyncio.Event:
    return asyncio.Event()


def _enroll(conn: sqlite3.Connection, dialog_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    conn.commit()


def _insert_msg(conn: sqlite3.Connection, dialog_id: int, message_id: int, text: str = "hi") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(dialog_id, message_id, sent_at, text, sender_id, sender_first_name, "
        "media_description, reply_to_msg_id, forum_topic_id, is_deleted) "
        "VALUES (?, ?, 1704067200, ?, 42, 'Alice', NULL, NULL, NULL, 0)",
        (dialog_id, message_id, text),
    )
    conn.commit()


def _insert_reaction(conn: sqlite3.Connection, dialog_id: int, message_id: int, emoji: str, count: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (dialog_id, message_id, emoji, count),
    )
    conn.commit()


def _reactions(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> list[tuple]:
    return list(
        conn.execute(
            "SELECT emoji, count FROM message_reactions WHERE dialog_id=? AND message_id=? ORDER BY emoji",
            (dialog_id, message_id),
        )
    )


def _last_event_at(conn: sqlite3.Connection, dialog_id: int) -> int | None:
    row = conn.execute(
        "SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _make_manager(client, conn, ev) -> EventHandlerManager:
    m = EventHandlerManager(client, conn, ev)
    m.register()
    return m


# ---------------------------------------------------------------------------
# MessageEdited reactions branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_edited_reactions_only_applies_delta(
    mock_client, sync_db, shutdown_event
):
    """AC-1 via edited path: text unchanged, msg.reactions populated -> rows applied."""
    dialog_id = 12345
    _enroll(sync_db, dialog_id)
    _insert_msg(sync_db, dialog_id, 100, text="hi")

    msg = build_mock_message(id=100, text="hi", reactions=build_mock_reactions({"👍": 2}))
    event = SimpleNamespace(chat_id=dialog_id, message=msg)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_message_edited(event)

    assert _reactions(sync_db, dialog_id, 100) == [("👍", 2)]
    assert _last_event_at(sync_db, dialog_id) is not None


@pytest.mark.asyncio
async def test_on_message_edited_reactions_removed_clears_rows(
    mock_client, sync_db, shutdown_event
):
    """AC-2: reactions object present but empty results -> rows deleted."""
    dialog_id = 12345
    _enroll(sync_db, dialog_id)
    _insert_msg(sync_db, dialog_id, 100, text="hi")
    _insert_reaction(sync_db, dialog_id, 100, "👍", 3)

    # reactions has empty results -> delta should remove rows
    msg = build_mock_message(id=100, text="hi", reactions=SimpleNamespace(results=[]))
    event = SimpleNamespace(chat_id=dialog_id, message=msg)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_message_edited(event)

    assert _reactions(sync_db, dialog_id, 100) == []


@pytest.mark.asyncio
async def test_on_message_edited_no_text_change_no_reactions_is_noop(
    mock_client, sync_db, shutdown_event
):
    """AC-8: text unchanged AND reactions is None -> message_reactions untouched, last_event_at untouched."""
    dialog_id = 12345
    _enroll(sync_db, dialog_id)
    _insert_msg(sync_db, dialog_id, 100, text="hi")
    _insert_reaction(sync_db, dialog_id, 100, "❤", 5)
    before = _last_event_at(sync_db, dialog_id)

    msg = build_mock_message(id=100, text="hi", reactions=None)
    event = SimpleNamespace(chat_id=dialog_id, message=msg)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_message_edited(event)

    assert _reactions(sync_db, dialog_id, 100) == [("❤", 5)]
    assert _last_event_at(sync_db, dialog_id) == before


# ---------------------------------------------------------------------------
# Raw reaction update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_raw_reaction_update_user_peer(mock_client, sync_db, shutdown_event):
    """AC-UPD-USER: UpdateMessageReactions for DM, get_messages called with int dialog_id."""
    dialog_id = 268071163  # DM (positive int)
    _enroll(sync_db, dialog_id)
    _insert_msg(sync_db, dialog_id, 500, text="hi")

    fetched = build_mock_message(id=500, text="hi", reactions=build_mock_reactions({"🔥": 4}))
    mock_client.get_messages = AsyncMock(return_value=[fetched])

    update = UpdateMessageReactions(
        peer=PeerUser(user_id=dialog_id),
        msg_id=500,
        reactions=fetched.reactions,
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_raw_reaction_update(update)

    assert _reactions(sync_db, dialog_id, 500) == [("🔥", 4)]
    # Assert called with integer dialog_id (NOT entity)
    args, kwargs = mock_client.get_messages.call_args
    assert args[0] == dialog_id
    assert kwargs.get("ids") == [500]


@pytest.mark.asyncio
async def test_on_raw_reaction_update_channel_peer(mock_client, sync_db, shutdown_event):
    """AC-UPD-CHANNEL: UpdateMessageReactions(peer=PeerChannel), dialog_id derived via get_peer_id."""
    from telethon.utils import get_peer_id  # type: ignore[import-untyped]

    channel_id = 1234567890
    dialog_id = get_peer_id(PeerChannel(channel_id))  # negative -100... shape
    _enroll(sync_db, dialog_id)
    _insert_msg(sync_db, dialog_id, 700, text="hi")

    fetched = build_mock_message(id=700, text="hi", reactions=build_mock_reactions({"💯": 9}))
    mock_client.get_messages = AsyncMock(return_value=[fetched])

    update = UpdateMessageReactions(
        peer=PeerChannel(channel_id),
        msg_id=700,
        reactions=fetched.reactions,
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_raw_reaction_update(update)

    assert _reactions(sync_db, dialog_id, 700) == [("💯", 9)]
    args, _ = mock_client.get_messages.call_args
    assert args[0] == dialog_id


@pytest.mark.asyncio
async def test_on_raw_reaction_removal(mock_client, sync_db, shutdown_event):
    """AC-2-RAW: raw update with empty results removes existing rows."""
    dialog_id = 268071163
    _enroll(sync_db, dialog_id)
    _insert_msg(sync_db, dialog_id, 800, text="hi")
    _insert_reaction(sync_db, dialog_id, 800, "👍", 1)

    fetched = build_mock_message(id=800, text="hi", reactions=SimpleNamespace(results=[]))
    mock_client.get_messages = AsyncMock(return_value=[fetched])

    update = UpdateMessageReactions(
        peer=PeerUser(user_id=dialog_id),
        msg_id=800,
        reactions=fetched.reactions,
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_raw_reaction_update(update)

    assert _reactions(sync_db, dialog_id, 800) == []


@pytest.mark.asyncio
async def test_on_raw_reaction_update_drops_silently_for_unsynced_dialog(
    mock_client, sync_db, shutdown_event
):
    """Non-synced dialog -> no DB mutation, no API call."""
    dialog_id = 999999
    # Do NOT enroll
    update = UpdateMessageReactions(
        peer=PeerUser(user_id=dialog_id),
        msg_id=10,
        reactions=SimpleNamespace(results=[]),
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_raw_reaction_update(update)

    mock_client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_on_raw_reaction_update_floodwait_logs_and_skips(
    mock_client, sync_db, shutdown_event
):
    """AC-6 supporting: FloodWaitError -> no DB mutation, warning logged."""
    dialog_id = 268071163
    _enroll(sync_db, dialog_id)
    _insert_msg(sync_db, dialog_id, 900, text="hi")
    _insert_reaction(sync_db, dialog_id, 900, "👍", 2)

    mock_client.get_messages = AsyncMock(side_effect=FloodWaitError(request=None, capture=60))

    update = UpdateMessageReactions(
        peer=PeerUser(user_id=dialog_id),
        msg_id=900,
        reactions=SimpleNamespace(results=[]),
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_raw_reaction_update(update)

    # No mutation
    assert _reactions(sync_db, dialog_id, 900) == [("👍", 2)]


@pytest.mark.asyncio
async def test_on_raw_reaction_update_missing_message_no_op(
    mock_client, sync_db, shutdown_event
):
    """get_messages returns [None] -> no DB mutation."""
    dialog_id = 268071163
    _enroll(sync_db, dialog_id)
    _insert_msg(sync_db, dialog_id, 1000, text="hi")
    _insert_reaction(sync_db, dialog_id, 1000, "👍", 1)

    mock_client.get_messages = AsyncMock(return_value=[None])

    update = UpdateMessageReactions(
        peer=PeerUser(user_id=dialog_id),
        msg_id=1000,
        reactions=SimpleNamespace(results=[]),
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_raw_reaction_update(update)

    assert _reactions(sync_db, dialog_id, 1000) == [("👍", 1)]


@pytest.mark.asyncio
async def test_idempotency_edited_then_raw_same_state(
    mock_client, sync_db, shutdown_event
):
    """AC-7: same change via both paths converges to same DB state."""
    dialog_id = 268071163
    _enroll(sync_db, dialog_id)
    _insert_msg(sync_db, dialog_id, 1100, text="hi")

    reactions_obj = build_mock_reactions({"👍": 3})

    # Edited path
    edited_msg = build_mock_message(id=1100, text="hi", reactions=reactions_obj)
    edited_event = SimpleNamespace(chat_id=dialog_id, message=edited_msg)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_message_edited(edited_event)
    after_edited = _reactions(sync_db, dialog_id, 1100)

    # Raw path with same reactions
    fetched = build_mock_message(id=1100, text="hi", reactions=reactions_obj)
    mock_client.get_messages = AsyncMock(return_value=[fetched])
    update = UpdateMessageReactions(
        peer=PeerUser(user_id=dialog_id),
        msg_id=1100,
        reactions=reactions_obj,
    )
    await mgr.on_raw_reaction_update(update)
    after_raw = _reactions(sync_db, dialog_id, 1100)

    assert after_edited == after_raw == [("👍", 3)]


# ---------------------------------------------------------------------------
# Register/unregister symmetry
# ---------------------------------------------------------------------------


def test_register_unregister_register_no_double_handler(
    mock_client, sync_db, shutdown_event
):
    """AC-REG-IDEMPOTENT: register -> unregister -> register; raw handler net 1 active."""
    mgr = EventHandlerManager(mock_client, sync_db, shutdown_event)

    mgr.register()
    mgr.unregister()
    mgr.register()

    add_calls = mock_client.add_event_handler.call_args_list
    rm_calls = mock_client.remove_event_handler.call_args_list

    raw_adds = [c for c in add_calls if c.args and c.args[0] == mgr.on_raw_reaction_update]
    raw_rms = [c for c in rm_calls if c.args and c.args[0] == mgr.on_raw_reaction_update]
    assert len(raw_adds) == 2
    assert len(raw_rms) == 1
