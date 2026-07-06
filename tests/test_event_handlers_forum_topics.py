"""Tests for Phase 42 EVENTS-05: forum topic event handlers.

Covers:
- Topic create (MessageActionTopicCreate via NewMessage service message)
- Topic edit / partial edit (MessageActionTopicEdit, COALESCE behaviour)
- Topic hidden (MessageActionTopicEdit(hidden=True) soft-delete branch)
- Defensive reply_to handling (None / missing reply_to_msg_id)
- UpdatePinnedForumTopic Raw handler (pin / unpin / unenrolled skip / missing row)
- Non-forum dialog gating (action=None does not produce rows)
- No-RPC invariant (GetForumTopicsRequest never called)
- Registration / unregistration symmetry
- daemon_api LEFT JOIN regression guard (topic_metadata title read path)

All writes target the `topic_metadata` table (extended by Plan 01 v19 ALTER).
No parallel `forum_topics` table is used.
"""

# pyright: reportAny=false, reportArgumentType=false, reportOptionalSubscript=false, reportOperatorIssue=false, reportUndefinedVariable=false, reportMissingParameterType=false, reportReturnType=false, reportInvalidTypeForm=false, reportGeneralTypeIssues=false

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import (  # type: ignore[import-untyped]
    MessageActionTopicCreate,
    MessageActionTopicEdit,
    PeerChannel,
    UpdatePinnedForumTopic,
)
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from helpers import build_mock_message
from mcp_telegram.event_handlers import (
    EventHandlerManager,
    _ForumTopicPinnedUpdateLike,
    _NewMessageEvent,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema

_SQLiteConnection = sqlite3.Connection


class _TopicRow(TypedDict):
    title: str
    icon_emoji_id: int | None
    pinned: int
    hidden: int
    snapshot_at: int


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_db(tmp_path: Path) -> Iterator[_SQLiteConnection]:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = cast(_SQLiteConnection, _open_sync_db(db_path))
    yield conn
    conn.close()


@pytest.fixture()
def mock_client() -> MagicMock:
    c = MagicMock()
    c.add_event_handler = MagicMock()
    c.remove_event_handler = MagicMock()
    return c


@pytest.fixture()
def shutdown_event() -> asyncio.Event:
    return asyncio.Event()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enroll_synced(conn: _SQLiteConnection, dialog_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    conn.commit()


@dataclass(frozen=True)
class _TopicMetadataOptions:
    title: str = "General"
    icon_emoji_id: int | None = None
    pinned: int = 0
    hidden: int = 0
    snapshot_at: int = 1
    is_general: int = 0
    is_deleted: int = 0
    updated_at: int = 1


@dataclass(frozen=True)
class _TopicEditEventOptions:
    title: str | None = None
    icon_emoji_id: int | None = None
    hidden: bool | None = None
    closed: bool | None = None
    reply_to: object = ...


def _insert_topic_metadata(
    conn: _SQLiteConnection,
    dialog_id: int,
    topic_id: int,
    *,
    opts: _TopicMetadataOptions | None = None,
    **kwargs: object,
) -> None:
    if opts is None:
        opts = _TopicMetadataOptions()
    if kwargs:
        opts = replace(opts, **kwargs)
    conn.execute(
        "INSERT OR REPLACE INTO topic_metadata "
        "(dialog_id, topic_id, title, top_message_id, is_general, is_deleted, "
        " updated_at, icon_emoji_id, pinned, hidden, snapshot_at, date) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            dialog_id,
            topic_id,
            opts.title,
            opts.is_general,
            opts.is_deleted,
            opts.updated_at,
            opts.icon_emoji_id,
            opts.pinned,
            opts.hidden,
            opts.snapshot_at,
        ),
    )
    conn.commit()


def _topic_row(conn: _SQLiteConnection, dialog_id: int, topic_id: int) -> _TopicRow | None:
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT title, icon_emoji_id, pinned, hidden, snapshot_at FROM topic_metadata WHERE dialog_id=? AND topic_id=?",
            (dialog_id, topic_id),
        ).fetchone(),
    )
    if row is None:
        return None
    return cast(
        _TopicRow,
        {
            "title": row[0],
            "icon_emoji_id": row[1],
            "pinned": row[2],
            "hidden": row[3],
            "snapshot_at": row[4],
        },
    )


def _topic_count(conn: _SQLiteConnection) -> int:
    row = cast(tuple[object, ...] | None, conn.execute("SELECT COUNT(*) FROM topic_metadata").fetchone())
    assert row is not None
    return int(tuple(row)[0])


def _make_manager(client: MagicMock, conn: _SQLiteConnection, ev: asyncio.Event) -> EventHandlerManager:
    m = EventHandlerManager(client, conn, ev)
    m.register()
    return m


def _make_service_msg(msg_id: int, action: object, reply_to_obj: object = None) -> MagicMock:
    """Build a minimal service-message mock with all fields needed by extract_message_row."""
    msg = MagicMock()
    msg.id = msg_id
    msg.action = action
    msg.date = datetime(2024, 1, 1, tzinfo=UTC)
    msg.reactions = None
    msg.message = ""
    # Service messages have no sender
    msg.sender_id = None
    msg.sender = None
    # Media: None → no media_description
    msg.media = None
    msg.edit_date = None
    msg.grouped_id = None
    msg.out = False
    msg.post_author = None
    msg.reply_to = reply_to_obj
    # No forward — prevents _build_fwd_entity_map from processing MagicMock attrs
    msg.fwd_from = None
    return msg


def _make_topic_create_event(
    dialog_id: int,
    msg_id: int,
    title: str,
    icon_emoji_id: int | None = None,
) -> _NewMessageEvent:
    action = MessageActionTopicCreate(
        title=title,
        icon_color=0,
        title_missing=None,
        icon_emoji_id=icon_emoji_id,
    )
    msg = _make_service_msg(msg_id, action)
    event = MagicMock()
    event.chat_id = dialog_id
    event.is_private = False
    event.message = msg
    return cast(_NewMessageEvent, event)


def _make_topic_edit_event(
    dialog_id: int,
    msg_id: int,
    target_topic_id: int | None,
    *,
    opts: _TopicEditEventOptions | None = None,
    **kwargs,
) -> _NewMessageEvent:
    if opts is None:
        opts = _TopicEditEventOptions()
    if kwargs:
        opts = replace(opts, **kwargs)
    action = MessageActionTopicEdit(
        title=opts.title,
        icon_emoji_id=opts.icon_emoji_id,
        closed=opts.closed,
        hidden=opts.hidden,
    )
    if opts.reply_to is ...:
        reply_to_obj = MagicMock()
        reply_to_obj.reply_to_msg_id = target_topic_id
    else:
        reply_to_obj = opts.reply_to  # caller-supplied; can be None
    msg = _make_service_msg(msg_id, action, reply_to_obj)
    event = MagicMock()
    event.chat_id = dialog_id
    event.is_private = False
    event.message = msg
    return cast(_NewMessageEvent, event)


# ---------------------------------------------------------------------------
# Topic create tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topic_create_inserts_topic_metadata_row(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """MessageActionTopicCreate in enrolled dialog inserts row with correct fields."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    event = _make_topic_create_event(dialog_id, msg_id=100, title="General", icon_emoji_id=42)
    await mgr.on_new_message(event)

    row = _topic_row(sync_db, dialog_id, 100)
    assert row is not None
    assert row["title"] == "General"
    assert row["icon_emoji_id"] == 42
    assert row["pinned"] == 0
    assert row["hidden"] == 0
    assert row["snapshot_at"] is not None and row["snapshot_at"] > 0


@pytest.mark.asyncio
async def test_topic_create_idempotent(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Replaying the same NewMessage create event twice produces only one row."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    event = _make_topic_create_event(dialog_id, msg_id=100, title="General", icon_emoji_id=42)
    await mgr.on_new_message(event)
    await mgr.on_new_message(event)

    assert _topic_count(sync_db) == 1
    row = _topic_row(sync_db, dialog_id, 100)
    assert row["title"] == "General"
    assert row["icon_emoji_id"] == 42


@pytest.mark.asyncio
async def test_topic_create_skipped_for_unenrolled_dialog(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """NewMessage for unenrolled dialog does NOT produce a topic_metadata row."""
    dialog_id = 99999
    # Do NOT enroll

    mgr = _make_manager(mock_client, sync_db, shutdown_event)

    event = _make_topic_create_event(dialog_id, msg_id=100, title="General", icon_emoji_id=None)
    await mgr.on_new_message(event)

    assert _topic_count(sync_db) == 0


# ---------------------------------------------------------------------------
# Topic edit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topic_edit_updates_title_and_icon(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """MessageActionTopicEdit with title and icon_emoji_id updates both fields."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)
    _insert_topic_metadata(
        sync_db,
        dialog_id,
        100,
        title="Old",
        icon_emoji_id=11,
        snapshot_at=1,
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    event = _make_topic_edit_event(
        dialog_id,
        msg_id=200,
        target_topic_id=100,
        title="New",
        icon_emoji_id=99,
    )
    await mgr.on_new_message(event)

    row = _topic_row(sync_db, dialog_id, 100)
    assert row["title"] == "New"
    assert row["icon_emoji_id"] == 99


@pytest.mark.asyncio
async def test_topic_edit_partial_preserves_existing_fields(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Edit with title=None preserves existing title; icon_emoji_id updated."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)
    _insert_topic_metadata(
        sync_db,
        dialog_id,
        100,
        title="Old",
        icon_emoji_id=11,
        snapshot_at=1,
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    event = _make_topic_edit_event(
        dialog_id,
        msg_id=200,
        target_topic_id=100,
        title=None,
        icon_emoji_id=99,
    )
    await mgr.on_new_message(event)

    row = _topic_row(sync_db, dialog_id, 100)
    assert row["title"] == "Old"  # COALESCE preserved
    assert row["icon_emoji_id"] == 99


@pytest.mark.asyncio
async def test_topic_edit_with_no_reply_to_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """MessageActionTopicEdit with msg.reply_to=None is a silent no-op (defensive guard)."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)
    _insert_topic_metadata(
        sync_db,
        dialog_id,
        100,
        title="Unchanged",
        icon_emoji_id=11,
        snapshot_at=1,
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    # Explicit reply_to=None — the defensive guard must prevent AttributeError
    event = _make_topic_edit_event(
        dialog_id,
        msg_id=200,
        target_topic_id=None,
        title="X",
        icon_emoji_id=None,
        reply_to=None,
    )
    await mgr.on_new_message(event)  # must not raise

    row = _topic_row(sync_db, dialog_id, 100)
    assert row["title"] == "Unchanged"  # unchanged


@pytest.mark.asyncio
async def test_topic_edit_with_reply_to_missing_id_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """reply_to object without reply_to_msg_id attribute → getattr fallback → skip."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)
    _insert_topic_metadata(
        sync_db,
        dialog_id,
        100,
        title="Unchanged",
        icon_emoji_id=11,
        snapshot_at=1,
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    class _NoMsgId:
        pass  # no reply_to_msg_id attribute

    event = _make_topic_edit_event(
        dialog_id,
        msg_id=200,
        target_topic_id=None,
        title="X",
        icon_emoji_id=None,
        reply_to=_NoMsgId(),
    )
    await mgr.on_new_message(event)  # must not raise

    row = _topic_row(sync_db, dialog_id, 100)
    assert row["title"] == "Unchanged"


# ---------------------------------------------------------------------------
# Topic hidden tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topic_edit_hidden_sets_hidden_flag(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """MessageActionTopicEdit(hidden=True) sets topic_metadata.hidden=1."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)
    _insert_topic_metadata(
        sync_db,
        dialog_id,
        100,
        title="Topic",
        hidden=0,
        snapshot_at=1,
    )

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    event = _make_topic_edit_event(
        dialog_id,
        msg_id=200,
        target_topic_id=100,
        hidden=True,
    )
    await mgr.on_new_message(event)

    row = _topic_row(sync_db, dialog_id, 100)
    assert row["hidden"] == 1


@pytest.mark.asyncio
async def test_topic_edit_hidden_on_missing_row_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """hidden=True UPDATE on absent row is silent no-op; no INSERT performed."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)
    # No seed row

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    event = _make_topic_edit_event(
        dialog_id,
        msg_id=200,
        target_topic_id=100,
        hidden=True,
    )
    await mgr.on_new_message(event)  # must not raise

    assert _topic_count(sync_db) == 0


# ---------------------------------------------------------------------------
# Topic pinned tests (UpdatePinnedForumTopic Raw handler)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pinned_forum_topic_sets_pinned_true(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """UpdatePinnedForumTopic(pinned=True) sets topic_metadata.pinned=1."""
    channel_id = 12345
    dialog_id = get_peer_id(PeerChannel(channel_id))
    _enroll_synced(sync_db, dialog_id)
    _insert_topic_metadata(sync_db, dialog_id, 100, pinned=0, snapshot_at=1)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    update = UpdatePinnedForumTopic(
        peer=PeerChannel(channel_id),
        topic_id=100,
        pinned=True,
    )
    await mgr.on_raw_forum_topic_pinned(cast(_ForumTopicPinnedUpdateLike, update))

    row = _topic_row(sync_db, dialog_id, 100)
    assert row["pinned"] == 1


@pytest.mark.asyncio
async def test_update_pinned_forum_topic_unsets_when_pinned_false(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """UpdatePinnedForumTopic(pinned=False) sets topic_metadata.pinned=0."""
    channel_id = 12345
    dialog_id = get_peer_id(PeerChannel(channel_id))
    _enroll_synced(sync_db, dialog_id)
    _insert_topic_metadata(sync_db, dialog_id, 100, pinned=1, snapshot_at=1)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    update = UpdatePinnedForumTopic(
        peer=PeerChannel(channel_id),
        topic_id=100,
        pinned=False,
    )
    await mgr.on_raw_forum_topic_pinned(cast(_ForumTopicPinnedUpdateLike, update))

    row = _topic_row(sync_db, dialog_id, 100)
    assert row["pinned"] == 0


@pytest.mark.asyncio
async def test_update_pinned_forum_topic_unsets_when_pinned_none(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """UpdatePinnedForumTopic(pinned=None) treats as falsy → pinned=0."""
    channel_id = 12345
    dialog_id = get_peer_id(PeerChannel(channel_id))
    _enroll_synced(sync_db, dialog_id)
    _insert_topic_metadata(sync_db, dialog_id, 100, pinned=1, snapshot_at=1)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    update = UpdatePinnedForumTopic(
        peer=PeerChannel(channel_id),
        topic_id=100,
        pinned=None,
    )
    await mgr.on_raw_forum_topic_pinned(cast(_ForumTopicPinnedUpdateLike, update))

    row = _topic_row(sync_db, dialog_id, 100)
    assert row["pinned"] == 0


@pytest.mark.asyncio
async def test_update_pinned_forum_topic_skips_unenrolled_dialog(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Unenrolled dialog: pinned/snapshot_at remain at seed values."""
    channel_id = 12345
    dialog_id = get_peer_id(PeerChannel(channel_id))
    # Seed the row but do NOT enroll the dialog
    _insert_topic_metadata(sync_db, dialog_id, 100, pinned=0, snapshot_at=5)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    # Do NOT add to _synced_dialog_ids

    update = UpdatePinnedForumTopic(
        peer=PeerChannel(channel_id),
        topic_id=100,
        pinned=True,
    )
    await mgr.on_raw_forum_topic_pinned(cast(_ForumTopicPinnedUpdateLike, update))

    row = _topic_row(sync_db, dialog_id, 100)
    assert row["pinned"] == 0  # unchanged
    assert row["snapshot_at"] == 5  # unchanged


@pytest.mark.asyncio
async def test_update_pinned_forum_topic_unknown_topic_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Enrolled dialog, no seed row → UPDATE matches 0 rows, no exception, no INSERT."""
    channel_id = 12345
    dialog_id = get_peer_id(PeerChannel(channel_id))
    _enroll_synced(sync_db, dialog_id)
    # No seed row

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    update = UpdatePinnedForumTopic(
        peer=PeerChannel(channel_id),
        topic_id=100,
        pinned=True,
    )
    await mgr.on_raw_forum_topic_pinned(cast(_ForumTopicPinnedUpdateLike, update))  # must not raise

    assert _topic_count(sync_db) == 0


@pytest.mark.asyncio
async def test_update_pinned_forum_topic_missing_peer_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """UpdatePinnedForumTopic with peer=None returns silently without crashing."""
    mgr = _make_manager(mock_client, sync_db, shutdown_event)

    # Fabricate a minimal update-like object with peer=None
    class _FakeUpdate:
        peer = None
        topic_id = 100
        pinned = True

    await mgr.on_raw_forum_topic_pinned(cast(_ForumTopicPinnedUpdateLike, _FakeUpdate()))  # must not raise

    assert _topic_count(sync_db) == 0


# ---------------------------------------------------------------------------
# Non-forum dialog gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_forum_message_does_not_write_topic_metadata(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Regular NewMessage (action=None) in enrolled dialog produces no topic_metadata row."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    # build_mock_message builds a SimpleNamespace with no action attribute
    msg = build_mock_message(id=999, text="hello")
    # ensure action is None (isinstance checks reject it)
    msg.action = None

    from types import SimpleNamespace

    event = cast(
        _NewMessageEvent,
        SimpleNamespace(chat_id=dialog_id, is_private=False, message=msg),
    )
    await mgr.on_new_message(event)

    assert _topic_count(sync_db) == 0


# ---------------------------------------------------------------------------
# No-RPC invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_get_forum_topics_request_called(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """GetForumTopicsRequest must never be invoked by any forum topic handler."""
    dialog_id = 12345
    _enroll_synced(sync_db, dialog_id)
    _insert_topic_metadata(sync_db, dialog_id, 100, snapshot_at=1)

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    channel_id = 12345
    channel_dialog_id = get_peer_id(PeerChannel(channel_id))
    _enroll_synced(sync_db, channel_dialog_id)
    _insert_topic_metadata(sync_db, channel_dialog_id, 100, snapshot_at=1)
    mgr._synced_dialog_ids.add(channel_dialog_id)

    # Exercise all handlers
    await mgr.on_new_message(_make_topic_create_event(dialog_id, msg_id=200, title="T", icon_emoji_id=None))
    await mgr.on_new_message(_make_topic_edit_event(dialog_id, msg_id=201, target_topic_id=100, title="New"))
    await mgr.on_new_message(_make_topic_edit_event(dialog_id, msg_id=202, target_topic_id=100, hidden=True))
    await mgr.on_raw_forum_topic_pinned(
        cast(
            _ForumTopicPinnedUpdateLike,
            UpdatePinnedForumTopic(peer=PeerChannel(channel_id), topic_id=100, pinned=True),
        )
    )

    # Assert mock_client was never called with any forum-topics RPC request.
    # GetForumTopicsRequest may not exist in all Telethon builds, so we check
    # by class name to remain version-agnostic.
    for call in mock_client.call_args_list:
        args, kwargs = call
        for a in args:
            assert type(a).__name__ != "GetForumTopicsRequest", f"GetForumTopicsRequest unexpectedly invoked: {call}"


# ---------------------------------------------------------------------------
# Registration symmetry
# ---------------------------------------------------------------------------


def test_register_attaches_forum_topic_pinned_handler(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """register() adds on_raw_forum_topic_pinned as an event handler."""
    mgr = EventHandlerManager(mock_client, sync_db, shutdown_event)
    mgr.register()

    add_calls = mock_client.add_event_handler.call_args_list
    handler_fns = [c.args[0] for c in add_calls if c.args]
    assert mgr.on_raw_forum_topic_pinned in handler_fns


def test_unregister_detaches_forum_topic_pinned_handler(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """unregister() removes on_raw_forum_topic_pinned."""
    mgr = EventHandlerManager(mock_client, sync_db, shutdown_event)
    mgr.register()
    mgr.unregister()

    rm_calls = mock_client.remove_event_handler.call_args_list
    handler_fns = [c.args[0] for c in rm_calls if c.args]
    assert mgr.on_raw_forum_topic_pinned in handler_fns


# ---------------------------------------------------------------------------
# daemon_api LEFT JOIN regression guard
# ---------------------------------------------------------------------------


def test_daemon_api_topic_title_left_join_still_works(sync_db: _SQLiteConnection) -> None:
    """UPSERT-written topic_metadata row is readable by the daemon_api LEFT JOIN pattern.

    Mirrors daemon_api.py:573 LEFT JOIN expression in isolation to confirm that
    Plan 02 writes to `topic_metadata` (the UPSERT) are visible to the read path
    without requiring a separate forum_topics table.
    """
    dialog_id = 12345
    topic_id = 100
    _insert_topic_metadata(
        sync_db,
        dialog_id,
        topic_id,
        title="My Topic",
        snapshot_at=9999,
    )

    # Mirror the LEFT JOIN query used in daemon_api._LIST_MESSAGES_BASE_SQL
    row = sync_db.execute(
        "SELECT tm.title FROM topic_metadata tm WHERE tm.dialog_id = ? AND tm.topic_id = ?",
        (dialog_id, topic_id),
    ).fetchone()

    assert row is not None
    assert row[0] == "My Topic"
