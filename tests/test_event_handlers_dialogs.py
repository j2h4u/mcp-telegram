"""Tests for Phase 42 dialog-level event handlers.

Covers EVENTS-01 (pin handlers), EVENTS-02 (inbox-read logging), EVENTS-03
(dirty-flag), EVENTS-04 (last_message_at writeback), registration symmetry,
and the UPDATE-only contract (no INSERT into dialogs).

All handlers gate on _synced_dialog_ids membership before writing — same
invariant as the five existing handlers (event_handlers.py:214, 249, 337,
376, 441, 502).
"""

# pyright: reportAny=false, reportArgumentType=false, reportOptionalSubscript=false, reportOperatorIssue=false, reportUndefinedVariable=false, reportMissingParameterType=false, reportReturnType=false, reportInvalidTypeForm=false, reportGeneralTypeIssues=false

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from helpers import build_mock_message
from telethon.tl.types import (  # type: ignore[import-untyped]
    DialogPeer,
    PeerChannel,
    PeerChat,
    PeerUser,
    UpdateChannel,
    UpdateChat,
    UpdateDialogPinned,
    UpdateDialogUnreadMark,
    UpdatePinnedDialogs,
    UpdateReadChannelInbox,
    UpdateReadHistoryInbox,
)
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from mcp_telegram.event_handlers import (
    EventHandlerManager,
    _ChannelChatUpdateLike,
    _ChannelInboxReadUpdateLike,
    _ChatUpdateLike,
    _InboxReadUpdateLike,
    _NewMessageEvent,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema

_SQLiteConnection = sqlite3.Connection


class _DialogRow(TypedDict):
    pinned: int
    needs_refresh: int
    last_message_at: int | None
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
class _DialogRowOptions:
    pinned: int = 0
    needs_refresh: int = 0
    last_message_at: int | None = None
    snapshot_at: int = 1


def _insert_dialog(
    conn: _SQLiteConnection,
    dialog_id: int,
    *,
    opts: _DialogRowOptions | None = None,
    **kwargs: object,
) -> None:
    if opts is None:
        opts = _DialogRowOptions()
    if kwargs:
        opts = replace(opts, **kwargs)
    conn.execute(
        "INSERT OR REPLACE INTO dialogs "
        "(dialog_id, name, type, archived, pinned, members, created, "
        " last_message_at, snapshot_at, hidden, needs_refresh, "
        " unread_mentions_count, unread_reactions_count, draft_text) "
        "VALUES (?, 'X', 'channel', 0, ?, NULL, NULL, ?, ?, 0, ?, 0, 0, NULL)",
        (dialog_id, opts.pinned, opts.last_message_at, opts.snapshot_at, opts.needs_refresh),
    )
    conn.commit()


def _dialog_row(conn: _SQLiteConnection, dialog_id: int) -> _DialogRow | None:
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT pinned, needs_refresh, last_message_at, snapshot_at FROM dialogs WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    if row is None:
        return None
    return cast(
        _DialogRow,
        {
            "pinned": row[0],
            "needs_refresh": row[1],
            "last_message_at": row[2],
            "snapshot_at": row[3],
        },
    )


def _dialogs_count(conn: _SQLiteConnection) -> int:
    row = cast(tuple[object, ...] | None, conn.execute("SELECT COUNT(*) FROM dialogs").fetchone())
    assert row is not None
    return int(tuple(row)[0])


def _last_event_at(conn: _SQLiteConnection, dialog_id: int) -> int | None:
    row = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone(),
    )
    return None if row is None or row[0] is None else int(row[0])


def _make_manager(client: MagicMock, conn: _SQLiteConnection, ev: asyncio.Event) -> EventHandlerManager:
    m = EventHandlerManager(client, conn, ev)
    m.register()
    return m


# ---------------------------------------------------------------------------
# EVENTS-01: pin handlers (UpdateDialogPinned, UpdatePinnedDialogs,
#            UpdateDialogUnreadMark) — UPDATE-only, gated on _synced_dialog_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_dialog_pinned_sets_pinned_true(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-01: single dialog pin sets dialogs.pinned=1 and advances snapshot_at."""
    channel_id = 12345
    dialog_id = get_peer_id(PeerChannel(channel_id))  # -1000000012345
    _enroll_synced(sync_db, dialog_id)
    _insert_dialog(sync_db, dialog_id, pinned=0, snapshot_at=1)

    upd = UpdateDialogPinned(
        peer=DialogPeer(peer=PeerChannel(channel_id=channel_id)),
        pinned=True,
        folder_id=None,
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)
    await mgr.on_raw_dialog_pinned(upd)

    row = _dialog_row(sync_db, dialog_id)
    assert row is not None
    assert row["pinned"] == 1
    assert row["snapshot_at"] is not None and row["snapshot_at"] > 1


@pytest.mark.asyncio
async def test_update_dialog_pinned_with_pinned_false_unsets(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-01: UpdateDialogPinned(pinned=False) sets dialogs.pinned=0."""
    channel_id = 12345
    dialog_id = get_peer_id(PeerChannel(channel_id))
    _enroll_synced(sync_db, dialog_id)
    _insert_dialog(sync_db, dialog_id, pinned=1, snapshot_at=1)

    upd = UpdateDialogPinned(
        peer=DialogPeer(peer=PeerChannel(channel_id=channel_id)),
        pinned=False,
        folder_id=None,
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)
    await mgr.on_raw_dialog_pinned(upd)

    row = _dialog_row(sync_db, dialog_id)
    assert row is not None
    assert row["pinned"] == 0


@pytest.mark.asyncio
async def test_update_dialog_pinned_skips_unenrolled_dialog(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-01: unenrolled dialog — no UPDATE issued (snapshot_at stays at seeded=1)."""
    channel_id = 99999
    dialog_id = get_peer_id(PeerChannel(channel_id))
    # NOT enrolled in _synced_dialog_ids
    _insert_dialog(sync_db, dialog_id, pinned=0, snapshot_at=1)

    upd = UpdateDialogPinned(
        peer=DialogPeer(peer=PeerChannel(channel_id=channel_id)),
        pinned=True,
        folder_id=None,
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    # Explicitly do NOT add dialog_id to _synced_dialog_ids
    await mgr.on_raw_dialog_pinned(upd)

    row = _dialog_row(sync_db, dialog_id)
    assert row is not None
    assert row["pinned"] == 0
    assert row["snapshot_at"] == 1  # unchanged


@pytest.mark.asyncio
async def test_update_dialog_pinned_missing_dialogs_row_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-01: enrolled in _synced_dialog_ids but NO dialogs row — no exception, no INSERT."""
    channel_id = 77777
    dialog_id = get_peer_id(PeerChannel(channel_id))
    _enroll_synced(sync_db, dialog_id)
    # No dialogs row

    upd = UpdateDialogPinned(
        peer=DialogPeer(peer=PeerChannel(channel_id=channel_id)),
        pinned=True,
        folder_id=None,
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)
    await mgr.on_raw_dialog_pinned(upd)  # must not raise

    assert _dialogs_count(sync_db) == 0  # no row created


@pytest.mark.asyncio
async def test_update_pinned_dialogs_with_order_rewrites_full_set(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-01: non-empty order list pins listed dialogs and unpins others."""
    # Three dialogs: A and B pinned, C not pinned
    id_a = get_peer_id(PeerUser(user_id=111))
    id_b = get_peer_id(PeerChannel(channel_id=222))
    id_c = get_peer_id(PeerChannel(channel_id=333))

    for did in [id_a, id_b, id_c]:
        _enroll_synced(sync_db, did)
    _insert_dialog(sync_db, id_a, pinned=1, snapshot_at=1)
    _insert_dialog(sync_db, id_b, pinned=1, snapshot_at=1)
    _insert_dialog(sync_db, id_c, pinned=0, snapshot_at=1)

    # order: A and C are pinned; B is no longer pinned
    upd = UpdatePinnedDialogs(
        folder_id=None,
        order=[
            DialogPeer(peer=PeerUser(user_id=111)),
            DialogPeer(peer=PeerChannel(channel_id=333)),
        ],
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    for did in [id_a, id_b, id_c]:
        mgr._synced_dialog_ids.add(did)
    await mgr.on_raw_dialog_pinned(upd)

    assert _dialog_row(sync_db, id_a)["pinned"] == 1
    assert _dialog_row(sync_db, id_b)["pinned"] == 0
    assert _dialog_row(sync_db, id_c)["pinned"] == 1


@pytest.mark.asyncio
async def test_update_pinned_dialogs_with_empty_order_clears_all_pins(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-01: empty order list unpins all — uses _CLEAR_ALL_PINS_SQL (no NOT IN ())."""
    id_a = get_peer_id(PeerChannel(channel_id=444))
    id_b = get_peer_id(PeerChannel(channel_id=555))

    for did in [id_a, id_b]:
        _enroll_synced(sync_db, did)
        _insert_dialog(sync_db, did, pinned=1, snapshot_at=1)

    upd = UpdatePinnedDialogs(folder_id=None, order=[])
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    for did in [id_a, id_b]:
        mgr._synced_dialog_ids.add(did)
    await mgr.on_raw_dialog_pinned(upd)

    assert _dialog_row(sync_db, id_a)["pinned"] == 0
    assert _dialog_row(sync_db, id_b)["pinned"] == 0


@pytest.mark.asyncio
async def test_update_pinned_dialogs_with_order_none_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-01: order=None means no actionable data — row state preserved."""
    dialog_id = get_peer_id(PeerChannel(channel_id=666))
    _enroll_synced(sync_db, dialog_id)
    _insert_dialog(sync_db, dialog_id, pinned=1, snapshot_at=42)

    upd = UpdatePinnedDialogs(folder_id=None, order=None)
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)
    await mgr.on_raw_dialog_pinned(upd)

    row = _dialog_row(sync_db, dialog_id)
    assert row["pinned"] == 1
    assert row["snapshot_at"] == 42  # unchanged


@pytest.mark.asyncio
async def test_update_dialog_unread_mark_sets_needs_refresh(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-01: UpdateDialogUnreadMark sets needs_refresh=1."""
    user_id = 12345
    dialog_id = get_peer_id(PeerUser(user_id=user_id))  # positive int for DM
    _enroll_synced(sync_db, dialog_id)
    _insert_dialog(sync_db, dialog_id, needs_refresh=0, snapshot_at=1)

    upd = UpdateDialogUnreadMark(
        peer=DialogPeer(peer=PeerUser(user_id=user_id)),
        unread=True,
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)
    await mgr.on_raw_dialog_pinned(upd)

    row = _dialog_row(sync_db, dialog_id)
    assert row["needs_refresh"] == 1


# ---------------------------------------------------------------------------
# EVENTS-02: still_unread_count captured via structured log
#
# Satisfaction strategy: capture-via-log only. No dialogs.unread_count column
# added in this milestone. The requirement is that the field is not silently
# dropped — structured logging at the daemon's observability boundary satisfies
# this.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_read_history_inbox_logs_still_unread_count(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """EVENTS-02: UpdateReadHistoryInbox logs still_unread_count via structured log."""
    # UpdateReadHistoryInbox.peer is TypePeer directly (not DialogPeer wrapper).
    dialog_id = get_peer_id(PeerChannel(channel_id=9999))
    _enroll_synced(sync_db, dialog_id)

    upd = UpdateReadHistoryInbox(
        peer=PeerChannel(channel_id=9999),
        max_id=42,
        still_unread_count=7,
        pts=100,
        pts_count=1,
        folder_id=None,
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    with caplog.at_level(logging.INFO, logger="mcp_telegram.event_handlers"):
        await mgr.on_raw_inbox_read(cast(_InboxReadUpdateLike, upd))

    assert any("still_unread_count=7" in r.message for r in caplog.records)
    # last_event_at should be advanced
    assert _last_event_at(sync_db, dialog_id) is not None


@pytest.mark.asyncio
async def test_update_read_channel_inbox_extracts_dialog_id_via_peer_channel(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """EVENTS-02: UpdateReadChannelInbox derives dialog_id via PeerChannel.

    Effective dialog_id must match -100XXXXXXXXX form.
    """
    channel_id = 1234567890
    dialog_id = get_peer_id(PeerChannel(channel_id))  # negative -100... shape
    assert dialog_id < 0, f"Expected negative dialog_id for channel, got {dialog_id}"

    _enroll_synced(sync_db, dialog_id)

    upd = UpdateReadChannelInbox(
        channel_id=channel_id,
        max_id=55,
        still_unread_count=3,
        pts=200,
        folder_id=None,
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)

    with caplog.at_level(logging.INFO, logger="mcp_telegram.event_handlers"):
        await mgr.on_raw_inbox_read(cast(_ChannelInboxReadUpdateLike, upd))

    assert any("still_unread_count=3" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_update_read_history_inbox_skips_unenrolled_dialog(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """EVENTS-02: unenrolled dialog — handler returns early, no log line emitted."""
    dialog_id = get_peer_id(PeerChannel(channel_id=88888))
    # NOT enrolled

    upd = UpdateReadHistoryInbox(
        peer=PeerChannel(channel_id=88888),
        max_id=1,
        still_unread_count=0,
        pts=10,
        pts_count=1,
        folder_id=None,
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)

    with caplog.at_level(logging.INFO, logger="mcp_telegram.event_handlers"):
        await mgr.on_raw_inbox_read(cast(_InboxReadUpdateLike, upd))

    assert not any("still_unread_count" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# EVENTS-03: dirty flag (UpdateChannel / UpdateChat) — UPDATE-only, gated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_channel_sets_needs_refresh(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-03: UpdateChannel sets dialogs.needs_refresh=1 and advances snapshot_at."""
    channel_id = 123456789
    dialog_id = get_peer_id(PeerChannel(channel_id))
    _enroll_synced(sync_db, dialog_id)
    _insert_dialog(sync_db, dialog_id, needs_refresh=0, snapshot_at=1)

    upd = UpdateChannel(channel_id=channel_id)
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)
    await mgr.on_raw_channel_chat_update(cast(_ChannelChatUpdateLike, upd))

    row = _dialog_row(sync_db, dialog_id)
    assert row["needs_refresh"] == 1
    assert row["snapshot_at"] > 1


@pytest.mark.asyncio
async def test_update_chat_sets_needs_refresh(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-03: UpdateChat sets dialogs.needs_refresh=1 via PeerChat(chat_id)."""
    chat_id = 678
    dialog_id = get_peer_id(PeerChat(chat_id))
    assert dialog_id < 0, f"PeerChat should produce negative id, got {dialog_id}"

    _enroll_synced(sync_db, dialog_id)
    _insert_dialog(sync_db, dialog_id, needs_refresh=0, snapshot_at=1)

    upd = UpdateChat(chat_id=chat_id)
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)
    await mgr.on_raw_channel_chat_update(cast(_ChatUpdateLike, upd))

    row = _dialog_row(sync_db, dialog_id)
    assert row["needs_refresh"] == 1


@pytest.mark.asyncio
async def test_update_channel_unknown_dialog_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-03: unenrolled dialog — handler runs without exception, no SQL UPDATE."""
    channel_id = 55555
    dialog_id = get_peer_id(PeerChannel(channel_id))
    # NOT enrolled in _synced_dialog_ids

    upd = UpdateChannel(channel_id=channel_id)
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_raw_channel_chat_update(cast(_ChannelChatUpdateLike, upd))  # must not raise


@pytest.mark.asyncio
async def test_update_channel_missing_dialogs_row_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-03: enrolled but NO dialogs row — handler runs without exception, no INSERT."""
    channel_id = 66666
    dialog_id = get_peer_id(PeerChannel(channel_id))
    _enroll_synced(sync_db, dialog_id)
    # No dialogs row

    count_before = _dialogs_count(sync_db)
    upd = UpdateChannel(channel_id=channel_id)
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr._synced_dialog_ids.add(dialog_id)
    await mgr.on_raw_channel_chat_update(cast(_ChannelChatUpdateLike, upd))  # must not raise

    assert _dialogs_count(sync_db) == count_before  # no INSERT


# ---------------------------------------------------------------------------
# EVENTS-04: last_message_at monotonic writeback in on_new_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_new_message_advances_last_message_at(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-04: on_new_message sets dialogs.last_message_at from msg.date."""
    from datetime import UTC, datetime

    dialog_id = 268071163
    _enroll_synced(sync_db, dialog_id)
    _insert_dialog(sync_db, dialog_id, last_message_at=None, snapshot_at=1)

    # build_mock_message uses date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    # = 1704110400
    expected_ts = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
    msg = build_mock_message(id=1, text="hello")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=False,
            get_sender=AsyncMock(return_value=None),
        ),
    )

    # Mock _build_fwd_entity_map and extract_message_row helpers
    import unittest.mock as mock

    with (
        mock.patch("mcp_telegram.event_handlers._build_fwd_entity_map", return_value={}),
        mock.patch("mcp_telegram.event_handlers.insert_messages_with_fts"),
    ):
        mgr = _make_manager(mock_client, sync_db, shutdown_event)
        mgr._synced_dialog_ids.add(dialog_id)
        await mgr.on_new_message(event)

    row = _dialog_row(sync_db, dialog_id)
    assert row["last_message_at"] == expected_ts


@pytest.mark.asyncio
async def test_on_new_message_does_not_regress_last_message_at(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-04: older msg.date does NOT decrease dialogs.last_message_at."""
    dialog_id = 268071163
    future_ts = 2_000_000_000
    _enroll_synced(sync_db, dialog_id)
    _insert_dialog(sync_db, dialog_id, last_message_at=future_ts, snapshot_at=1)

    # build_mock_message has date=2024-01-01 which is < future_ts
    msg = build_mock_message(id=2, text="old")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(chat_id=dialog_id, message=msg, is_private=False, get_sender=AsyncMock(return_value=None)),
    )

    import unittest.mock as mock

    with (
        mock.patch("mcp_telegram.event_handlers._build_fwd_entity_map", return_value={}),
        mock.patch("mcp_telegram.event_handlers.insert_messages_with_fts"),
    ):
        mgr = _make_manager(mock_client, sync_db, shutdown_event)
        mgr._synced_dialog_ids.add(dialog_id)
        await mgr.on_new_message(event)

    row = _dialog_row(sync_db, dialog_id)
    assert row["last_message_at"] == future_ts  # unchanged


@pytest.mark.asyncio
async def test_on_new_message_missing_dialogs_row_is_noop(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """EVENTS-04: synced_dialogs row exists but NO dialogs row — no INSERT into dialogs."""
    dialog_id = 268071163
    _enroll_synced(sync_db, dialog_id)
    # No dialogs row

    count_before = _dialogs_count(sync_db)
    msg = build_mock_message(id=3, text="new")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(chat_id=dialog_id, message=msg, is_private=False, get_sender=AsyncMock(return_value=None)),
    )

    import unittest.mock as mock

    with (
        mock.patch("mcp_telegram.event_handlers._build_fwd_entity_map", return_value={}),
        mock.patch("mcp_telegram.event_handlers.insert_messages_with_fts"),
    ):
        mgr = _make_manager(mock_client, sync_db, shutdown_event)
        mgr._synced_dialog_ids.add(dialog_id)
        await mgr.on_new_message(event)  # must not raise

    assert _dialogs_count(sync_db) == count_before  # no INSERT


# ---------------------------------------------------------------------------
# Registration symmetry
# ---------------------------------------------------------------------------


def test_register_attaches_three_new_handlers(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Three new Phase 42 Plan 01 handlers (+ one Plan 02 handler) bring total to 10."""
    mgr = EventHandlerManager(mock_client, sync_db, shutdown_event)
    mgr.register()
    assert mock_client.add_event_handler.call_count == 10


def test_unregister_detaches_all_new_handlers(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Every handler attached by register() is detached by unregister()."""
    mgr = EventHandlerManager(mock_client, sync_db, shutdown_event)
    mgr.register()
    mgr.unregister()

    add_calls = {c.args[0] for c in mock_client.add_event_handler.call_args_list if c.args}
    rm_calls = {c.args[0] for c in mock_client.remove_event_handler.call_args_list if c.args}
    assert add_calls == rm_calls


# ---------------------------------------------------------------------------
# UPDATE-only invariant: no handler inserts into dialogs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_handler_inserts_into_dialogs_table(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Across all dialog-handler calls for non-bootstrapped dialog_ids, count never increases.

    This test covers:
    - UpdateDialogPinned on missing rows (unenrolled)
    - UpdateChannel on missing rows (unenrolled)
    - UpdateReadHistoryInbox on missing rows (unenrolled)
    - on_new_message on missing dialogs row (enrolled in synced_dialogs only)
    """
    assert _dialogs_count(sync_db) == 0

    channel_id = 11111
    dialog_id = get_peer_id(PeerChannel(channel_id))

    # Test 1: UpdateDialogPinned — unenrolled
    upd_pin = UpdateDialogPinned(
        peer=DialogPeer(peer=PeerChannel(channel_id=channel_id)),
        pinned=True,
        folder_id=None,
    )
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    await mgr.on_raw_dialog_pinned(upd_pin)
    assert _dialogs_count(sync_db) == 0

    # Test 2: UpdateChannel — unenrolled
    upd_ch = UpdateChannel(channel_id=channel_id)
    await mgr.on_raw_channel_chat_update(cast(_ChannelChatUpdateLike, upd_ch))
    assert _dialogs_count(sync_db) == 0

    # Test 3: UpdateReadHistoryInbox — unenrolled
    upd_read = UpdateReadHistoryInbox(
        peer=PeerChannel(channel_id=channel_id),
        max_id=1,
        still_unread_count=0,
        pts=10,
        pts_count=1,
        folder_id=None,
    )
    await mgr.on_raw_inbox_read(cast(_InboxReadUpdateLike, upd_read))
    assert _dialogs_count(sync_db) == 0

    # Test 4: on_new_message — enrolled in synced_dialogs but NO dialogs row
    _enroll_synced(sync_db, dialog_id)
    mgr._synced_dialog_ids.add(dialog_id)
    msg = build_mock_message(id=1, text="hello")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(chat_id=dialog_id, message=msg, is_private=False, get_sender=None),
    )

    import unittest.mock as mock

    with (
        mock.patch("mcp_telegram.event_handlers._build_fwd_entity_map", return_value={}),
        mock.patch("mcp_telegram.event_handlers.insert_messages_with_fts"),
    ):
        await mgr.on_new_message(event)

    assert _dialogs_count(sync_db) == 0  # bootstrap is the sole dialogs-row creator
