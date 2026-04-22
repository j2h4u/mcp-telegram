"""Tests for Phase 39.3-02 outbox read-cursor live push path.

Covers AC-3 (live handler advances read_outbox_max_id) and related invariants:
  - PeerUser-only filter (PeerChat / PeerChannel drops).
  - Monotonic guard (smaller max_id absorbed by apply_read_cursor's MAX).
  - Unsynced dialog → silent drop.
  - last_event_at bumped on happy path.
  - Unexpected exception logged and not propagated.
  - Register / unregister symmetric, idempotent, and preserves callback identity.
  - Outbox handler does not touch read_inbox_max_id.

Telethon dispatch path LOCKED to Path A:
  ``events.MessageRead(inbox=False)`` — verified against the installed Telethon
  source at ``.venv/lib/python3.14/site-packages/telethon/events/messageread.py``
  lines 37-48 (``build()`` returns ``cls.Event(update.peer, update.max_id, True)``
  when the update is an ``UpdateReadHistoryOutbox``; filter at lines 57-61
  requires ``event.outbox == True`` when ``inbox=False``). This maximises
  symmetry with the Phase 38 inbox handler (``on_message_read``).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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


def _read_cursors(conn: sqlite3.Connection, dialog_id: int) -> tuple[Any, Any, Any]:
    row = conn.execute(
        "SELECT read_inbox_max_id, read_outbox_max_id, last_event_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    return (None, None, None) if row is None else (row[0], row[1], row[2])


def _make_event(chat_id: int | None, max_id: int) -> SimpleNamespace:
    """Build a minimal MessageRead.Event-like object for Path A dispatch.

    events.MessageRead(inbox=False).Event has: chat_id, max_id, outbox=True.
    """
    return SimpleNamespace(chat_id=chat_id, max_id=max_id, outbox=True)


def _make_manager(client, conn, ev) -> EventHandlerManager:
    return EventHandlerManager(client, conn, ev)


# ---------------------------------------------------------------------------
# Happy path + monotonic + last_event_at + inbox non-interference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_read_user_peer_advances_cursor(mock_client, sync_db, shutdown_event):
    """AC-3 happy path: a synced DM + max_id=42 writes read_outbox_max_id=42."""
    dialog_id = 1001
    _enroll(sync_db, dialog_id)
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    await mgr.on_outbox_read(_make_event(chat_id=dialog_id, max_id=42))

    inbox, outbox, last_ev = _read_cursors(sync_db, dialog_id)
    assert outbox == 42
    assert last_ev is not None  # bumped


@pytest.mark.asyncio
async def test_outbox_read_monotonic_no_regression(mock_client, sync_db, shutdown_event):
    """AC-3 monotonic guard: stored 100; incoming 50 → cursor stays 100."""
    dialog_id = 1001
    _enroll(sync_db, dialog_id)
    sync_db.execute(
        "UPDATE synced_dialogs SET read_outbox_max_id=? WHERE dialog_id=?",
        (100, dialog_id),
    )
    sync_db.commit()

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    await mgr.on_outbox_read(_make_event(chat_id=dialog_id, max_id=50))

    _, outbox, _ = _read_cursors(sync_db, dialog_id)
    assert outbox == 100, f"Expected 100 (monotonic), got {outbox} (regressed!)"


@pytest.mark.asyncio
async def test_outbox_read_unsynced_dialog_dropped(mock_client, sync_db, shutdown_event, caplog):
    """dialog_id not in _synced_dialog_ids → no DB mutation, no exception."""
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.event_handlers"):
        await mgr.on_outbox_read(_make_event(chat_id=9999, max_id=42))

    # no row should be created
    row = sync_db.execute(
        "SELECT COUNT(*) FROM synced_dialogs WHERE dialog_id=?",
        (9999,),
    ).fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_outbox_read_null_chat_id_silently_dropped(mock_client, sync_db, shutdown_event):
    """PeerChat / PeerChannel would land here as chat_id for non-DM peers.

    Our filter drops non-DM peers. Use chat_id=None as the "untrackable" sentinel
    — must not raise, must not mutate DB.
    """
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    await mgr.on_outbox_read(_make_event(chat_id=None, max_id=42))  # must not raise

    row = sync_db.execute("SELECT COUNT(*) FROM synced_dialogs").fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_outbox_read_chat_peer_dropped(mock_client, sync_db, shutdown_event):
    """PeerChat event (positive chat_id but not a DM User) → no DB mutation.

    We implement this by enrolling a non-DM-shaped dialog_id in synced_dialogs
    and asserting the handler's PeerUser-only filter drops it before any write.
    The Path A filter uses event.is_private (injected via SimpleNamespace).
    """
    dialog_id = 5001  # small groups have positive but non-user-shaped IDs
    _enroll(sync_db, dialog_id)
    sync_db.execute(
        "UPDATE synced_dialogs SET read_outbox_max_id=NULL WHERE dialog_id=?",
        (dialog_id,),
    )
    sync_db.commit()

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    event = SimpleNamespace(chat_id=dialog_id, max_id=42, outbox=True, is_private=False)
    await mgr.on_outbox_read(event)

    _, outbox, _ = _read_cursors(sync_db, dialog_id)
    assert outbox is None, "outbox cursor must not be written for non-DM peers"


@pytest.mark.asyncio
async def test_outbox_read_channel_peer_dropped(mock_client, sync_db, shutdown_event):
    """PeerChannel event → handler's PeerUser-only filter drops it."""
    dialog_id = -1001234567890  # channel-shaped id
    _enroll(sync_db, dialog_id)
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    event = SimpleNamespace(chat_id=dialog_id, max_id=42, outbox=True, is_private=False)
    await mgr.on_outbox_read(event)

    _, outbox, _ = _read_cursors(sync_db, dialog_id)
    assert outbox is None


@pytest.mark.asyncio
async def test_outbox_read_updates_last_event_at(mock_client, sync_db, shutdown_event):
    """Happy path bumps last_event_at (same convention as inbox)."""
    dialog_id = 1001
    _enroll(sync_db, dialog_id)
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    await mgr.on_outbox_read(_make_event(chat_id=dialog_id, max_id=7))

    _, _, last_ev = _read_cursors(sync_db, dialog_id)
    assert last_ev is not None and last_ev > 0


@pytest.mark.asyncio
async def test_outbox_read_unexpected_exception_logged_not_propagated(mock_client, sync_db, shutdown_event, caplog):
    """Force apply_read_cursor to raise; handler must log + swallow (not crash loop)."""
    dialog_id = 1001
    _enroll(sync_db, dialog_id)
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    with patch(
        "mcp_telegram.event_handlers.apply_read_cursor",
        side_effect=RuntimeError("boom"),
    ):
        with caplog.at_level(logging.ERROR, logger="mcp_telegram.event_handlers"):
            # Must not raise.
            await mgr.on_outbox_read(_make_event(chat_id=dialog_id, max_id=42))

    assert any("event_outbox_read_failed" in rec.message for rec in caplog.records), (
        f"Expected event_outbox_read_failed log; got {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_outbox_handler_does_not_touch_inbox_cursor(mock_client, sync_db, shutdown_event):
    """Happy path must leave read_inbox_max_id untouched."""
    dialog_id = 1001
    _enroll(sync_db, dialog_id)
    sync_db.execute(
        "UPDATE synced_dialogs SET read_inbox_max_id=? WHERE dialog_id=?",
        (500, dialog_id),
    )
    sync_db.commit()

    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    await mgr.on_outbox_read(_make_event(chat_id=dialog_id, max_id=42))

    inbox, outbox, _ = _read_cursors(sync_db, dialog_id)
    assert inbox == 500, "inbox cursor must be untouched by the outbox handler"
    assert outbox == 42


# ---------------------------------------------------------------------------
# Register / unregister symmetry + callback identity
# ---------------------------------------------------------------------------


def test_register_unregister_register_no_double_outbox_handler(mock_client, sync_db, shutdown_event):
    """AC-REG-IDEMPOTENT for outbox: register → unregister → register = net 1 active."""
    mgr = _make_manager(mock_client, sync_db, shutdown_event)

    mgr.register()
    add_count_after_first_register = sum(
        1 for call in mock_client.add_event_handler.call_args_list if call.args and call.args[0] == mgr.on_outbox_read
    )
    mgr.unregister()
    rm_count_after_unregister = sum(
        1
        for call in mock_client.remove_event_handler.call_args_list
        if call.args and call.args[0] == mgr.on_outbox_read
    )
    mgr.register()
    add_count_after_second_register = sum(
        1 for call in mock_client.add_event_handler.call_args_list if call.args and call.args[0] == mgr.on_outbox_read
    )

    assert add_count_after_first_register == 1
    assert rm_count_after_unregister == 1
    assert add_count_after_second_register == 2  # two adds, one remove = net 1
    # Net active = adds - removes
    net_active = add_count_after_second_register - rm_count_after_unregister
    assert net_active == 1


def test_unregister_uses_correct_callback_identity(mock_client, sync_db, shutdown_event):
    """Per codex MEDIUM: remove_event_handler must receive the SAME callback
    object that was passed to add_event_handler (identity equality, not just
    method name). Catches the footgun where Telethon decorators wrap the
    callback and `self.on_outbox_read` doesn't match the registered identity.
    """
    mgr = _make_manager(mock_client, sync_db, shutdown_event)
    mgr.register()

    # Capture the exact callback object registered with add_event_handler.
    registered = [
        call.args[0]
        for call in mock_client.add_event_handler.call_args_list
        if call.args and call.args[0] == mgr.on_outbox_read
    ]
    assert len(registered) == 1, "outbox handler must be registered exactly once"
    registered_cb = registered[0]

    mgr.unregister()

    removed = [
        call.args[0]
        for call in mock_client.remove_event_handler.call_args_list
        if call.args and call.args[0] == mgr.on_outbox_read
    ]
    assert len(removed) == 1, "outbox handler must be removed exactly once"
    removed_cb = removed[0]

    # Identity, not just equality — bound-method identity on same instance
    # holds because attribute access yields a fresh bound-method wrapper each
    # time but they compare equal; Telethon dispatches by equality under the
    # hood for bound methods. We assert equality PLUS that the underlying
    # function and `__self__` match exactly.
    assert removed_cb == registered_cb
    assert removed_cb.__func__ is registered_cb.__func__
    assert removed_cb.__self__ is registered_cb.__self__
