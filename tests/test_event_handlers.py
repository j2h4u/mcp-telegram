"""Tests for EventHandlerManager — TDD RED phase.

Covers DAEMON-07 (NewMessage), DAEMON-08 (MessageEdited),
DAEMON-09 (channel/supergroup MessageDeleted), and DAEMON-10
(DM gap scan) behaviors.
"""

# pyright: reportAny=false, reportArgumentType=false, reportOptionalSubscript=false, reportOperatorIssue=false, reportUndefinedVariable=false, reportMissingParameterType=false, reportReturnType=false, reportInvalidTypeForm=false, reportGeneralTypeIssues=false

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers import build_mock_message
from mcp_telegram.event_handlers import (
    EventHandlerManager,
    UpdateProcessingBarrier,
    _DeletedMessagesEvent,
    _EditedMessageEvent,
    _NewMessageEvent,
)
from mcp_telegram.history_enrollment import disable_history
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    UnclassifiedTelegramDemandError,
    current_demand_token,
    demand_context,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource
from tests.history_enrollment_helpers import seed_full_history_enrollment

_SQLiteConnection = sqlite3.Connection


@pytest.mark.asyncio
async def test_startup_update_barrier_waits_then_cancels_cleanly() -> None:
    shutdown = asyncio.Event()
    barrier = UpdateProcessingBarrier(closed=True)
    waiting = asyncio.create_task(barrier.wait(shutdown))
    await asyncio.sleep(0)
    assert not waiting.done()
    barrier.open()
    await waiting

    cancelled = UpdateProcessingBarrier(closed=True)
    waiting = asyncio.create_task(cancelled.wait(shutdown))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting


def make_new_message_event(
    chat_id: int | None,
    message: SimpleNamespace,
    is_private: bool = False,
) -> _NewMessageEvent:
    """Build a minimal NewMessage.Event-like object."""
    return cast(_NewMessageEvent, SimpleNamespace(chat_id=chat_id, message=message, is_private=is_private))


def make_message_edited_event(chat_id: int | None, message: SimpleNamespace) -> _EditedMessageEvent:
    """Build a minimal MessageEdited.Event-like object.

    The message should have .edit_date set to a datetime to signal an edit.
    """
    return cast(_EditedMessageEvent, SimpleNamespace(chat_id=chat_id, message=message))


def make_message_deleted_event(chat_id: int | None, deleted_ids: list[int]) -> _DeletedMessagesEvent:
    """Build a minimal MessageDeleted.Event-like object."""
    return cast(_DeletedMessagesEvent, SimpleNamespace(chat_id=chat_id, deleted_ids=deleted_ids))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_db(tmp_path: Path) -> Iterator[_SQLiteConnection]:
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
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> EventHandlerManager:
    manager = EventHandlerManager(mock_client, sync_db, shutdown_event)
    manager.bind_demand_sink(MagicMock())
    return manager


def insert_synced_dialog(conn: _SQLiteConnection, dialog_id: int) -> None:
    """Insert a dialog into synced_dialogs so the manager treats it as synced."""
    conn.execute(
        "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (dialog_id,),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=True)
    conn.commit()


class _RecordingDemandSink:
    def __init__(self) -> None:
        self.offered: list[DemandKind] = []

    def offer(self, kind: DemandKind) -> bool:
        self.offered.append(kind)
        return True


def test_auto_enrollment_offers_full_sync_only_after_commit(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    manager = make_manager(mock_client, sync_db, shutdown_event)
    offered: list[DemandKind] = []
    sink = MagicMock()

    def offer(kind: DemandKind) -> bool:
        assert not sync_db.in_transaction
        offered.append(kind)
        return True

    sink.offer.side_effect = offer
    manager.bind_demand_sink(sink)

    assert manager._auto_enroll_dm(42)
    assert offered == [DemandKind.FULL_SYNC_PAGE]


def confirm_human_dm(conn: _SQLiteConnection, dialog_id: int, message_id: int) -> None:
    conn.execute("INSERT OR REPLACE INTO dialogs(dialog_id, type) VALUES (?, 'user')", (dialog_id,))
    conn.execute("INSERT OR REPLACE INTO entities(id, type, updated_at) VALUES (?, 'user', 1)", (dialog_id,))
    conn.execute(
        "UPDATE messages SET sender_id=?, out=0, is_service=0 WHERE dialog_id=? AND message_id=?",
        (dialog_id, dialog_id, message_id),
    )
    conn.commit()


@dataclass(frozen=True)
class _MessageRowOptions:
    text: str | None = "some text"
    is_deleted: int = 0
    deleted_at: int | None = None


def insert_message(
    conn: _SQLiteConnection,
    dialog_id: int,
    message_id: int,
    *,
    opts: _MessageRowOptions | None = None,
    **kwargs: object,
) -> None:
    """Insert a message row directly for test setup."""
    if opts is None:
        opts = _MessageRowOptions()
    if kwargs:
        opts = replace(opts, **kwargs)
    conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(dialog_id, message_id, sent_at, text, sender_id, sender_first_name, "
        "media_kind, media_payload, reply_to_msg_id, forum_topic_id, is_deleted, deleted_at) "
        "VALUES (?, ?, 1704067200, ?, 42, 'Alice', NULL, NULL, NULL, NULL, ?, ?)",
        (dialog_id, message_id, opts.text, opts.is_deleted, opts.deleted_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# DAEMON-07: NewMessage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_new_message_inserts_row(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """New message in a synced dialog is inserted into messages table."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()
    offered: list[DemandKind] = []
    sink = MagicMock()

    def offer(kind: DemandKind) -> bool:
        assert not sync_db.in_transaction
        offered.append(kind)
        return True

    sink.offer.side_effect = offer
    manager.bind_demand_sink(sink)

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
    assert offered == [
        DemandKind.LIVE_HYDRATION_BATCH,
        DemandKind.BACKFILL_HYDRATION_BATCH,
        DemandKind.MESSAGE_FACT_REFRESH,
        DemandKind.READ_RECEIPT_BATCH,
    ]


@pytest.mark.asyncio
async def test_realtime_event_root_refines_nested_entity_acquisition(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog_id = 1003
    insert_synced_dialog(sync_db, dialog_id)
    observed: list[tuple[DemandKind, TelegramRpcSource, AcquisitionKind | None]] = []

    async def inspect_acquisition(_message: object, _client: object) -> dict[int, str]:
        token = current_demand_token()
        observed.append((token.kind, token.source, token.acquisition_kind))
        return {}

    monkeypatch.setattr("mcp_telegram.event_handlers._build_fwd_entity_map", inspect_acquisition)
    manager = make_manager(mock_client, sync_db, shutdown_event)
    await manager.on_new_message(make_new_message_event(dialog_id, build_mock_message(id=503, text="root")))

    assert observed == [
        (
            DemandKind.REALTIME_EVENT_ACQUISITION,
            TelegramRpcSource.REALTIME_EVENT,
            AcquisitionKind.ENTITY_LOOKUP,
        )
    ]
    with pytest.raises(UnclassifiedTelegramDemandError):
        current_demand_token()


@pytest.mark.asyncio
async def test_realtime_event_preserves_existing_protocol_root(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog_id = 1005
    insert_synced_dialog(sync_db, dialog_id)
    observed: list[tuple[DemandKind, TelegramRpcSource, AcquisitionKind | None]] = []

    async def inspect_acquisition(_message: object, _client: object) -> dict[int, str]:
        token = current_demand_token()
        observed.append((token.kind, token.source, token.acquisition_kind))
        return {}

    monkeypatch.setattr("mcp_telegram.event_handlers._build_fwd_entity_map", inspect_acquisition)
    manager = make_manager(mock_client, sync_db, shutdown_event)
    with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE):
        await manager.on_new_message(make_new_message_event(dialog_id, build_mock_message(id=505, text="catch-up")))

    assert observed == [
        (
            DemandKind.TELETHON_UPDATE_DIFFERENCE,
            TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE,
            AcquisitionKind.ENTITY_LOOKUP,
        )
    ]


@pytest.mark.asyncio
async def test_realtime_event_cancellation_propagates_and_clears_context(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog_id = 1004
    insert_synced_dialog(sync_db, dialog_id)

    async def cancel_acquisition(_message: object, _client: object) -> dict[int, str]:
        token = current_demand_token()
        assert token.kind is DemandKind.REALTIME_EVENT_ACQUISITION
        assert token.acquisition_kind is AcquisitionKind.ENTITY_LOOKUP
        raise asyncio.CancelledError

    monkeypatch.setattr("mcp_telegram.event_handlers._build_fwd_entity_map", cancel_acquisition)
    manager = make_manager(mock_client, sync_db, shutdown_event)

    with pytest.raises(asyncio.CancelledError):
        await manager.on_new_message(make_new_message_event(dialog_id, build_mock_message(id=504, text="cancel")))
    with pytest.raises(UnclassifiedTelegramDemandError):
        current_demand_token()


@pytest.mark.asyncio
async def test_on_new_message_preserves_message_thread_topic_id(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Topic metadata without reply_to should still persist forum_topic_id."""
    dialog_id = 1002
    insert_synced_dialog(sync_db, dialog_id)
    sync_db.execute(
        "INSERT OR REPLACE INTO topic_metadata "
        "(dialog_id, topic_id, title, is_general, is_deleted, updated_at) "
        "VALUES (?, ?, ?, 0, 0, 1704067200)",
        (dialog_id, 7, "Deployments"),
    )
    sync_db.commit()

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(
        id=501, text="topic hello", reply_to_msg_id=None, message_thread_id=7, is_topic_message=True
    )
    event = make_new_message_event(chat_id=dialog_id, message=msg)
    await manager.on_new_message(event)

    row = sync_db.execute(
        "SELECT m.forum_topic_id, tm.title FROM messages m "
        "LEFT JOIN topic_metadata tm ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
        "WHERE m.dialog_id=? AND m.message_id=?",
        (dialog_id, 501),
    ).fetchone()
    assert row == (7, "Deployments")


@pytest.mark.asyncio
async def test_on_new_message_ignores_unsynced(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """New message for an unsynced dialog produces no DB row."""
    # dialog_id=9999 is NOT in synced_dialogs
    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(id=100, text="ignored")
    event = make_new_message_event(chat_id=9999, message=msg)
    await manager.on_new_message(event)

    count_row = sync_db.execute("SELECT COUNT(*) FROM messages").fetchone()
    assert count_row is not None
    count = int(tuple(count_row)[0])
    assert count == 0


@pytest.mark.asyncio
async def test_on_new_message_auto_enrolls_private_dialog(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
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
    sync_db: _SQLiteConnection,
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

    count_row = sync_db.execute(
        "SELECT COUNT(*) FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert count_row is not None
    assert int(tuple(count_row)[0]) == 1, "synced_dialogs must have exactly one row for the dialog"


@pytest.mark.asyncio
async def test_first_seen_private_message_projects_visible_dialog_and_entity(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_id = 7016
    sender = SimpleNamespace(first_name="Алиса", last_name="Иванова", username="alice")
    msg = build_mock_message(id=17, text="hello")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_sender=AsyncMock(return_value=sender),
        ),
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    await manager.on_new_message(event)

    expected_sent_at = int(msg.date.timestamp())
    dialog_row = sync_db.execute(
        "SELECT name, type, last_message_at, snapshot_at, hidden, needs_refresh FROM dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert dialog_row is not None
    assert dialog_row[:3] == ("Алиса Иванова", "user", expected_sent_at)
    assert dialog_row[3] is not None and dialog_row[3] > 0
    assert dialog_row[4:] == (0, 1)
    assert sync_db.execute(
        "SELECT type, name, username, name_normalized FROM entities WHERE id=?", (dialog_id,)
    ).fetchone() == ("user", "Алиса Иванова", "alice", "alisa ivanova")
    assert sync_db.execute("SELECT message_id FROM messages WHERE dialog_id=?", (dialog_id,)).fetchone() == (17,)


@pytest.mark.asyncio
async def test_first_seen_outgoing_private_message_uses_chat_peer(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_id = 7017
    peer = SimpleNamespace(first_name="Target", last_name="Bot", username="target_bot", bot=True)
    msg = build_mock_message(id=18, text="outgoing")
    msg.out = True
    get_sender = AsyncMock(side_effect=AssertionError("operator sender must not be used"))
    get_chat = AsyncMock(return_value=peer)
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_sender=get_sender,
            get_chat=get_chat,
        ),
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    await manager.on_new_message(event)

    assert sync_db.execute("SELECT name, type FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        "Target Bot",
        "bot",
    )
    get_chat.assert_awaited_once_with()
    get_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_seen_outgoing_private_lookup_failure_keeps_message_discoverable(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_id = 7018
    msg = build_mock_message(id=19, text="lookup failed")
    msg.out = True
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_chat=AsyncMock(side_effect=RuntimeError("unavailable")),
        ),
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    await manager.on_new_message(event)

    assert sync_db.execute(
        "SELECT name, type, hidden, needs_refresh FROM dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == (None, None, 0, 1)
    assert sync_db.execute("SELECT identity_revision FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (0,)
    assert sync_db.execute("SELECT message_id, out FROM messages WHERE dialog_id=?", (dialog_id,)).fetchone() == (19, 1)


@pytest.mark.asyncio
async def test_first_seen_private_event_respects_explicit_disable_tombstone(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_id = 7020
    disable_history(sync_db, dialog_id, now=100)
    sender = SimpleNamespace(first_name="Disabled", last_name="Peer", username="disabled")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=build_mock_message(id=21, text="ignored"),
            is_private=True,
            get_sender=AsyncMock(return_value=sender),
        ),
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    await manager.on_new_message(event)

    assert sync_db.execute("SELECT dialog_id FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() is None
    assert sync_db.execute("SELECT dialog_id FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() is None
    assert sync_db.execute("SELECT message_id FROM messages WHERE dialog_id=?", (dialog_id,)).fetchone() is None


@pytest.mark.asyncio
async def test_first_seen_private_event_preserves_existing_dialog_facts(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_id = 7019
    sync_db.execute(
        "INSERT INTO dialogs(dialog_id, name, type, archived, pinned, members, hidden, unread_count) "
        "VALUES (?, 'Saved', 'user', 1, 1, 9, 1, 4)",
        (dialog_id,),
    )
    sync_db.commit()
    sender = SimpleNamespace(first_name="New", last_name="Name", username="new_name")
    msg = build_mock_message(id=20, text="new")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_sender=AsyncMock(return_value=sender),
        ),
    )
    manager = make_manager(mock_client, sync_db, shutdown_event)
    assert manager._auto_enroll_dm(dialog_id, sender=sender, message_date=msg.date, observed_at=100)

    # Realtime presence intentionally exposes an absent-from-snapshot row; other
    # durable dialog facts must survive the thin first-event projection.
    assert sync_db.execute(
        "SELECT archived, pinned, members, hidden, unread_count FROM dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone() == (1, 1, 9, 0, 4)
    assert sync_db.execute(
        "SELECT name,username,type,identity_complete,identity_source,identity_revision FROM dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone() == ("New Name", "new_name", "user", 0, "realtime", 1)


@pytest.mark.asyncio
async def test_on_new_message_ignores_unsynced_non_private(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """New message from an unknown non-private (group) dialog is ignored — no enrollment."""
    dialog_id = 7003

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(id=1, text="group msg")
    event = make_new_message_event(chat_id=dialog_id, message=msg, is_private=False)
    await manager.on_new_message(event)

    count_row = sync_db.execute(
        "SELECT COUNT(*) FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert count_row is not None
    assert int(tuple(count_row)[0]) == 0, "non-private unknown dialog must not be enrolled"
    assert dialog_id not in manager._synced_dialog_ids


@pytest.mark.asyncio
async def test_auto_enroll_writes_entity_when_sender_available(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Auto-enroll writes an entities row when get_sender() returns a User."""
    dialog_id = 7010
    sender = SimpleNamespace(first_name="Fixture", last_name="Person", username="fixture_person")

    msg = build_mock_message(id=1, text="hey")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_sender=AsyncMock(return_value=sender),
        ),
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()
    await manager.on_new_message(event)

    row = sync_db.execute(
        "SELECT id, type, name, username, name_normalized FROM entities WHERE id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None, "entity must be written when sender is available"
    assert row[1] == "user"
    assert row[2] == "Fixture Person"
    assert row[3] == "fixture_person"
    assert row[4] == "fixture person"


def test_dm_entity_collectible_username_and_partial_sender_preserve_canonical_facts(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_id = 7016
    sync_db.execute(
        "INSERT INTO entities(id,type,name,username,name_normalized,updated_at) VALUES (?,?,?,?,?,?)",
        (dialog_id, "user", "Known Person", "known", "known person", 1),
    )
    sync_db.commit()
    manager = make_manager(mock_client, sync_db, shutdown_event)

    assert manager._auto_enroll_dm(
        dialog_id,
        sender=SimpleNamespace(usernames=[SimpleNamespace(username="@collectible", active=True)]),
        observed_at=100,
    )
    assert sync_db.execute("SELECT name,username,type FROM entities WHERE id=?", (dialog_id,)).fetchone() == (
        "Known Person",
        "collectible",
        "user",
    )

    dialog_id += 1
    sync_db.execute(
        "INSERT INTO entities(id,type,name,username,name_normalized,updated_at) VALUES (?,?,?,?,?,?)",
        (dialog_id, "service", "Known Service", "service_name", "known service", 1),
    )
    sync_db.commit()
    assert manager._auto_enroll_dm(dialog_id, sender=SimpleNamespace(), observed_at=101)
    assert sync_db.execute("SELECT name,username,type FROM entities WHERE id=?", (dialog_id,)).fetchone() == (
        "Known Service",
        "service_name",
        "service",
    )

    for offset, stored_type in enumerate(("unknown", ""), start=2):
        unknown_id = dialog_id + offset
        sync_db.execute(
            "INSERT INTO entities(id,type,name,username,name_normalized,updated_at) VALUES (?,?,?,?,?,?)",
            (unknown_id, stored_type, None, None, None, 1),
        )
        identity = manager._dm_identity(SimpleNamespace(first_name=None, last_name=None, username="observed"))
        assert identity is not None
        manager._persist_dm_entity(unknown_id, identity, observed_at=102)
        assert sync_db.execute("SELECT type,username FROM entities WHERE id=?", (unknown_id,)).fetchone() == (
            "user",
            "observed",
        )


@pytest.mark.asyncio
async def test_auto_enroll_entity_write_fails_gracefully(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Enrollment succeeds even if entity write fails (independent failure domains)."""
    dialog_id = 7011
    sender = SimpleNamespace(first_name="Broken", last_name="User", username=None)

    msg = build_mock_message(id=1, text="hi")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_sender=AsyncMock(return_value=sender),
        ),
    )

    # Drop entities table to force entity write to fail
    sync_db.execute("DROP TABLE entities")
    sync_db.commit()

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()
    await manager.on_new_message(event)

    # Dialog must still be enrolled despite entity write failure
    row = sync_db.execute(
        "SELECT dialog_id, status FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None, "dialog must be enrolled even when entity write fails"
    assert row[1] == "syncing"
    assert dialog_id in manager._synced_dialog_ids


def test_auto_enroll_rolls_back_enrollment_when_dialog_projection_fails(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_id = 7021
    sync_db.execute(
        "CREATE TRIGGER fail_realtime_dialog BEFORE INSERT ON dialogs "
        "BEGIN SELECT RAISE(ABORT, 'dialog projection failed'); END"
    )
    sync_db.commit()
    manager = make_manager(mock_client, sync_db, shutdown_event)
    sink = MagicMock()
    manager.bind_demand_sink(sink)

    assert not manager._auto_enroll_dm(
        dialog_id,
        sender=SimpleNamespace(first_name="Atomic", last_name="Peer", username="atomic"),
        message_date=build_mock_message(id=22).date,
        observed_at=100,
    )
    assert sync_db.execute("SELECT dialog_id FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() is None
    assert (
        sync_db.execute("SELECT dialog_id FROM full_history_enrollment WHERE dialog_id=?", (dialog_id,)).fetchone()
        is None
    )
    assert sync_db.execute("SELECT dialog_id FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() is None
    sink.offer.assert_not_called()


def test_auto_enroll_entity_failure_does_not_rollback_committed_dialog_projection(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_id = 7022
    sync_db.execute(
        "CREATE TRIGGER fail_realtime_entity BEFORE INSERT ON entities "
        "BEGIN SELECT RAISE(ROLLBACK, 'entity projection failed'); END"
    )
    sync_db.commit()
    manager = make_manager(mock_client, sync_db, shutdown_event)
    sink = MagicMock()
    manager.bind_demand_sink(sink)

    assert manager._auto_enroll_dm(
        dialog_id,
        sender=SimpleNamespace(first_name="Committed", last_name="Peer", username="committed"),
        message_date=build_mock_message(id=23).date,
        observed_at=100,
    )
    assert sync_db.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        "syncing",
    )
    assert sync_db.execute(
        "SELECT name, type, needs_refresh FROM dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == ("Committed Peer", "user", 1)
    assert sync_db.execute("SELECT id FROM entities WHERE id=?", (dialog_id,)).fetchone() is None
    sink.offer.assert_called_once()


@pytest.mark.asyncio
async def test_auto_enroll_no_entity_when_sender_unavailable(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """When get_sender() fails, dialog is enrolled but no entity row is written."""
    dialog_id = 7012

    msg = build_mock_message(id=1, text="hello")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_sender=AsyncMock(side_effect=Exception("Telegram unreachable")),
        ),
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()
    await manager.on_new_message(event)

    # Dialog enrolled
    row = sync_db.execute(
        "SELECT status FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None, "dialog must be enrolled even when sender fetch fails"
    assert row[0] == "syncing"

    # No entity row
    entity = sync_db.execute("SELECT id FROM entities WHERE id=?", (dialog_id,)).fetchone()
    assert entity is None, "no entity row must be written when sender is unavailable"


@pytest.mark.asyncio
async def test_auto_enroll_writes_tombstone_for_nameless_sender(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Auto-enroll writes entity row with name=NULL when sender has no display name."""
    dialog_id = 7013
    sender = SimpleNamespace(first_name=None, last_name=None, username="ghost_bot")

    msg = build_mock_message(id=1, text="beep")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_sender=AsyncMock(return_value=sender),
        ),
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()
    await manager.on_new_message(event)

    row = sync_db.execute("SELECT name, username FROM entities WHERE id=?", (dialog_id,)).fetchone()
    assert row is not None, "entity row must exist even when sender has no display name"
    assert row[0] is None, "name must be NULL for nameless sender"
    assert row[1] == "ghost_bot", "username must be preserved"


@pytest.mark.asyncio
async def test_auto_enroll_handles_sender_without_name_attrs(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Auto-enroll tolerates sender objects missing first_name and last_name."""
    dialog_id = 7014
    sender = SimpleNamespace(username="channel_like")

    msg = build_mock_message(id=1, text="beep")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_sender=AsyncMock(return_value=sender),
        ),
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()
    await manager.on_new_message(event)

    row = sync_db.execute(
        "SELECT name, username FROM entities WHERE id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None, "entity row must be written even when sender lacks name attrs"
    assert row[0] is None
    assert row[1] == "channel_like"


@pytest.mark.asyncio
async def test_auto_enroll_handles_non_string_sender_name_fields(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Auto-enroll ignores non-string first_name/last_name/username values."""
    dialog_id = 7015
    sender = SimpleNamespace(first_name=123, last_name=[], username={"u": "bad"})

    msg = build_mock_message(id=1, text="beep")
    event = cast(
        _NewMessageEvent,
        SimpleNamespace(
            chat_id=dialog_id,
            message=msg,
            is_private=True,
            get_sender=AsyncMock(return_value=sender),
        ),
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()
    await manager.on_new_message(event)

    row = sync_db.execute(
        "SELECT name, username FROM entities WHERE id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None, "entity row must be written with non-string sender fields"
    assert row[0] is None
    assert row[1] is None


@pytest.mark.asyncio
async def test_on_new_message_updates_last_event_at(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Firing on_new_message sets synced_dialogs.last_event_at."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)

    # Verify last_event_at starts as None
    before_row = sync_db.execute("SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone()
    assert before_row is not None
    before = tuple(before_row)[0]
    assert before is None

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    msg = build_mock_message(id=501)
    event = make_new_message_event(chat_id=dialog_id, message=msg)
    await manager.on_new_message(event)

    after_row = sync_db.execute("SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone()
    assert after_row is not None
    after = tuple(after_row)[0]
    assert after is not None


@pytest.mark.asyncio
async def test_burst_50_messages(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
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

    count_row = sync_db.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=?", (dialog_id,)).fetchone()
    assert count_row is not None
    count = int(tuple(count_row)[0])
    assert count == 50


# ---------------------------------------------------------------------------
# DAEMON-08: MessageEdited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_edited_creates_version(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Editing a message with changed text creates a message_versions row."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=100, text="old text")
    confirm_human_dm(sync_db, dialog_id, 100)

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    edit_dt = datetime(2024, 1, 1, 13, 0, 0, tzinfo=UTC)
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
    new_text_row = sync_db.execute(
        "SELECT text FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 100),
    ).fetchone()
    assert new_text_row is not None
    assert new_text_row[0] == "new text"


@pytest.mark.asyncio
async def test_on_message_edited_no_version_if_same(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Editing a message with same text does NOT create a message_versions row."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=101, text="same")

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    edit_dt = datetime(2024, 1, 1, 13, 0, 0, tzinfo=UTC)
    msg = build_mock_message(id=101, text="same", edit_date=edit_dt)
    event = make_message_edited_event(chat_id=dialog_id, message=msg)
    await manager.on_message_edited(event)

    count_row = sync_db.execute("SELECT COUNT(*) FROM message_versions").fetchone()
    assert count_row is not None
    count = int(tuple(count_row)[0])
    assert count == 0


@pytest.mark.asyncio
async def test_on_message_edited_unknown_message(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Edit to a message not in sync.db inserts the row but creates no version history."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    # No pre-inserted message for message_id=999

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    edit_dt = datetime(2024, 1, 1, 13, 0, 0, tzinfo=UTC)
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
    count_row = sync_db.execute("SELECT COUNT(*) FROM message_versions").fetchone()
    assert count_row is not None
    count = int(tuple(count_row)[0])
    assert count == 0


@pytest.mark.asyncio
async def test_on_message_edited_increments_version(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Two sequential edits with different text produce version=1 and version=2."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=200, text="v0 text")
    confirm_human_dm(sync_db, dialog_id, 200)

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    edit_dt1 = datetime(2024, 1, 1, 13, 0, 0, tzinfo=UTC)
    msg1 = build_mock_message(id=200, text="v1 text", edit_date=edit_dt1)
    await manager.on_message_edited(make_message_edited_event(dialog_id, msg1))
    # The generic mock sender is not this DM peer; restore the persisted
    # human-DM identity before simulating the correspondent's second edit.
    confirm_human_dm(sync_db, dialog_id, 200)

    edit_dt2 = datetime(2024, 1, 1, 14, 0, 0, tzinfo=UTC)
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
    sync_db: _SQLiteConnection,
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
    sync_db: _SQLiteConnection,
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

    text_row = sync_db.execute(
        "SELECT text FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 201),
    ).fetchone()
    assert text_row is not None
    assert text_row[0] == "will be deleted"


@pytest.mark.asyncio
async def test_on_message_deleted_already_deleted(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Firing delete again on an already-deleted message does not re-stamp deleted_at."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    original_deleted_at = 1000
    insert_message(
        sync_db,
        dialog_id,
        message_id=202,
        text="already gone",
        is_deleted=1,
        deleted_at=original_deleted_at,
    )

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    event = make_message_deleted_event(chat_id=dialog_id, deleted_ids=[202])
    await manager.on_message_deleted(event)

    deleted_at_row = sync_db.execute(
        "SELECT deleted_at FROM messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, 202),
    ).fetchone()
    assert deleted_at_row is not None
    assert deleted_at_row[0] == original_deleted_at


@pytest.mark.asyncio
async def test_on_message_deleted_updates_last_event_at(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Deleting a message in a synced dialog updates synced_dialogs.last_event_at."""
    dialog_id = 1001
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id=203, text="msg")

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    before_row = sync_db.execute("SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone()
    assert before_row is not None
    before = tuple(before_row)[0]
    assert before is None

    event = make_message_deleted_event(chat_id=dialog_id, deleted_ids=[203])
    await manager.on_message_deleted(event)

    after_row = sync_db.execute("SELECT last_event_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone()
    assert after_row is not None
    after = tuple(after_row)[0]
    assert after is not None


# ---------------------------------------------------------------------------
# DAEMON-10: DM deletes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_deleted_peerless_without_candidate_is_ignored(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A peer-less deletion without one safe DM candidate makes no DB changes."""
    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    event = make_message_deleted_event(chat_id=None, deleted_ids=[555, 556])

    with caplog.at_level(logging.INFO, logger="mcp_telegram.event_handlers"):
        await manager.on_message_deleted(event)

    count_row = sync_db.execute("SELECT COUNT(*) FROM messages").fetchone()
    assert count_row is not None
    count = int(tuple(count_row)[0])
    assert count == 0

    assert any("resolved=0 unresolved=2" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_on_message_deleted_peerless_marks_unique_incoming_human_dm(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    dialog_id = 2001
    message_id = 555
    insert_synced_dialog(sync_db, dialog_id)
    insert_message(sync_db, dialog_id, message_id)
    confirm_human_dm(sync_db, dialog_id, message_id)
    manager = make_manager(mock_client, sync_db, shutdown_event)

    await manager.on_message_deleted(make_message_deleted_event(chat_id=None, deleted_ids=[message_id]))

    assert sync_db.execute(
        "SELECT is_deleted FROM messages WHERE dialog_id=? AND message_id=?", (dialog_id, message_id)
    ).fetchone() == (1,)
    assert sync_db.execute(
        "SELECT kind FROM conversation_history_events WHERE dialog_id=? AND message_id=?",
        (dialog_id, message_id),
    ).fetchone() == ("deleted_message",)


@pytest.mark.asyncio
async def test_on_message_deleted_peerless_rejects_ambiguous_dm_message_id(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    message_id = 555
    for dialog_id in (2001, 2002):
        insert_synced_dialog(sync_db, dialog_id)
        insert_message(sync_db, dialog_id, message_id)
        confirm_human_dm(sync_db, dialog_id, message_id)
    manager = make_manager(mock_client, sync_db, shutdown_event)

    await manager.on_message_deleted(make_message_deleted_event(chat_id=None, deleted_ids=[message_id]))

    assert sync_db.execute(
        "SELECT COUNT(*) FROM messages WHERE message_id=? AND is_deleted=1", (message_id,)
    ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_gap_scan_marks_deleted(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
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
    sync_db: _SQLiteConnection,
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
        "media_kind, media_payload, reply_to_msg_id, forum_topic_id, is_deleted) "
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
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """register() attaches every current real-time callback exactly once."""
    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    expected = [
        manager.on_new_message,
        manager.on_raw_topic_message,
        manager.on_message_edited,
        manager.on_message_deleted,
        manager.on_outbox_read,
        manager.on_raw_reaction_update,
        manager.on_raw_transcribed_audio,
        manager.on_raw_new_scheduled_message,
        manager.on_raw_delete_scheduled_messages,
        manager.on_raw_dialog_pinned,
        manager.on_raw_channel_chat_update,
        manager.on_raw_identity_or_notify,
        manager.on_raw_participant,
        manager.on_raw_inbox_read,
        manager.on_raw_forum_topic_pinned,
    ]
    registered = [call.args[0] for call in mock_client.add_event_handler.call_args_list if call.args]
    assert registered == expected


def test_unregister_removes_handlers(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """unregister() removes every callback attached by register()."""
    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()
    manager.unregister()

    expected = [
        manager.on_new_message,
        manager.on_raw_topic_message,
        manager.on_message_edited,
        manager.on_message_deleted,
        manager.on_outbox_read,
        manager.on_raw_reaction_update,
        manager.on_raw_transcribed_audio,
        manager.on_raw_new_scheduled_message,
        manager.on_raw_delete_scheduled_messages,
        manager.on_raw_dialog_pinned,
        manager.on_raw_channel_chat_update,
        manager.on_raw_identity_or_notify,
        manager.on_raw_participant,
        manager.on_raw_inbox_read,
        manager.on_raw_forum_topic_pinned,
    ]
    removed = [call.args[0] for call in mock_client.remove_event_handler.call_args_list if call.args]
    assert removed == expected


def test_refresh_synced_dialogs(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
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
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='access_lost' is NOT in _synced_dialog_ids after refresh."""
    # Insert access_lost dialog
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9901, 'access_lost')",
    )
    seed_full_history_enrollment(sync_db, 9901, enabled=False)
    # Insert synced dialog (should still appear)
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9902, 'synced')",
    )
    seed_full_history_enrollment(sync_db, 9902, enabled=True)
    sync_db.commit()

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    assert 9901 not in manager._synced_dialog_ids, "access_lost dialog must be excluded from _synced_dialog_ids"
    assert 9902 in manager._synced_dialog_ids, "synced dialog must remain in _synced_dialog_ids"


def test_refresh_includes_syncing(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='syncing' IS in _synced_dialog_ids after refresh (only access_lost excluded)."""
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9903, 'syncing')",
    )
    seed_full_history_enrollment(sync_db, 9903, enabled=True)
    sync_db.commit()

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    assert 9903 in manager._synced_dialog_ids, "syncing dialog must be included in _synced_dialog_ids"


@pytest.mark.asyncio
async def test_gap_scan_excludes_syncing_dialogs(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='syncing' is NOT scanned by run_dm_gap_scan."""
    dialog_id = 9910
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'syncing')",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=True)
    for msg_id in [1, 2, 3]:
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, ?, 1000000000)",
            (dialog_id, msg_id),
        )
    sync_db.commit()

    get_messages_calls: list[object] = []

    async def _get_messages(entity: object, ids: object) -> list[object]:
        get_messages_calls.append(entity)
        return []

    mock_client.get_messages = _get_messages

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    await manager.run_dm_gap_scan()

    assert dialog_id not in get_messages_calls, f"'syncing' dialog {dialog_id} must not be scanned"


@pytest.mark.asyncio
async def test_gap_scan_excludes_access_lost_dialogs(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Dialog with status='access_lost' is NOT scanned by run_dm_gap_scan."""
    dialog_id = 9911
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'access_lost')",
        (dialog_id,),
    )
    seed_full_history_enrollment(sync_db, dialog_id, enabled=False)
    for msg_id in [1, 2, 3]:
        sync_db.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (?, ?, 1000000000)",
            (dialog_id, msg_id),
        )
    sync_db.commit()

    get_messages_calls: list[object] = []

    async def _get_messages(entity: object, ids: object) -> list[object]:
        get_messages_calls.append(entity)
        return []

    mock_client.get_messages = _get_messages

    manager = make_manager(mock_client, sync_db, shutdown_event)
    manager.register()

    await manager.run_dm_gap_scan()

    assert dialog_id not in get_messages_calls, f"'access_lost' dialog {dialog_id} must not be scanned"


@pytest.mark.asyncio
async def test_gap_scan_only_synced(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    """Only dialogs with status='synced' are scanned by run_dm_gap_scan."""
    # synced dialog — should be scanned
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9920, 'synced')",
    )
    seed_full_history_enrollment(sync_db, 9920, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (9920, 1, 1000000000)",
    )
    # syncing dialog — must NOT be scanned
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9921, 'syncing')",
    )
    seed_full_history_enrollment(sync_db, 9921, enabled=True)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (9921, 2, 1000000000)",
    )
    # access_lost dialog — must NOT be scanned
    sync_db.execute(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (9922, 'access_lost')",
    )
    seed_full_history_enrollment(sync_db, 9922, enabled=False)
    sync_db.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at) VALUES (9922, 3, 1000000000)",
    )
    sync_db.commit()

    scanned: list[object] = []

    async def _get_messages(entity: object, ids: object) -> list[object]:
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
    sync_db: _SQLiteConnection,
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
        "SELECT dialog_id, message_id, stemmed_text FROM messages_fts WHERE dialog_id=? AND message_id=?",
        (dialog_id, 500),
    ).fetchone()
    assert fts_row is not None, "messages_fts must have a row for the new message"
    assert fts_row[0] == dialog_id
    assert fts_row[1] == 500
    assert fts_row[2] != "", "stemmed_text must be non-empty"


@pytest.mark.asyncio
async def test_on_message_edited_updates_fts(
    mock_client: MagicMock,
    sync_db: _SQLiteConnection,
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

    edit_dt = datetime(2024, 1, 1, 13, 0, 0, tzinfo=UTC)
    msg = build_mock_message(id=600, text="new edited content", edit_date=edit_dt)
    event = make_message_edited_event(chat_id=dialog_id, message=msg)
    await manager.on_message_edited(event)

    fts_row = sync_db.execute(
        "SELECT stemmed_text FROM messages_fts WHERE dialog_id=? AND message_id=?",
        (dialog_id, 600),
    ).fetchone()
    assert fts_row is not None, "messages_fts row must exist after edit"
    # stemmed text must differ from the original stub
    assert fts_row[0] != "old text here", "FTS stemmed_text must be updated after message edit"


# ---------------------------------------------------------------------------
# TG-4: realtime linked-chat updates only enqueue durable work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_updatechannel_offers_linked_chat_refresh_without_rpc(
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    from telethon.tl.types import UpdateChannel  # type: ignore[import-untyped]

    dialog_id = -1004444444444
    channel_id = 4444444444
    sync_db.execute(
        "INSERT INTO dialogs (dialog_id, type, linked_chat_id, linked_chat_resolved_at) "
        "VALUES (?, 'channel', -1005555555555, 1700000000)",
        (dialog_id,),
    )
    sync_db.commit()
    insert_synced_dialog(sync_db, dialog_id)

    client = MagicMock()
    manager = make_manager(client, sync_db, shutdown_event)
    sink = _RecordingDemandSink()
    manager.bind_demand_sink(sink)
    manager.register()

    await manager.on_raw_channel_chat_update(UpdateChannel(channel_id=channel_id))

    assert client.call_count == 0
    assert sink.offered == [
        DemandKind.DIALOG_LIGHT_RECONCILIATION,
        DemandKind.LINKED_CHAT_REFRESH,
    ]
    assert sync_db.execute(
        "SELECT linked_chat_id, linked_chat_resolved_at, needs_refresh FROM dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone() == (-1005555555555, 1700000000, 1)
    pending = sync_db.execute(
        "SELECT generation, pending_generation FROM linked_chat_fact_state WHERE channel_id=?", (dialog_id,)
    ).fetchone()
    assert pending is not None and pending[0] == pending[1]


@pytest.mark.asyncio
async def test_updatechannel_does_not_enqueue_never_resolved_channel(
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    from telethon.tl.types import UpdateChannel  # type: ignore[import-untyped]

    dialog_id = -1007777777777
    insert_synced_dialog(sync_db, dialog_id)
    sync_db.execute(
        "INSERT INTO dialogs (dialog_id, type, linked_chat_id, linked_chat_resolved_at) "
        "VALUES (?, 'channel', NULL, NULL)",
        (dialog_id,),
    )
    sync_db.commit()
    client = MagicMock()
    manager = make_manager(client, sync_db, shutdown_event)
    sink = _RecordingDemandSink()
    manager.bind_demand_sink(sink)
    manager.register()

    await manager.on_raw_channel_chat_update(UpdateChannel(channel_id=7777777777))

    assert client.call_count == 0
    assert sync_db.execute("SELECT 1 FROM linked_chat_fact_state WHERE channel_id=?", (dialog_id,)).fetchone() is None
    assert DemandKind.LINKED_CHAT_REFRESH not in sink.offered


@pytest.mark.parametrize(("flood_wait_seconds", "expected_delay"), [(20, 300), (200_000, 86_400)])
@pytest.mark.asyncio
async def test_linked_chat_retry_respects_owner_flood_wait_policy(
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    flood_wait_seconds: int,
    expected_delay: int,
) -> None:
    import time

    from telethon.tl.types import InputPeerChannel

    from mcp_telegram.flood import TelegramRpcThrottled
    from mcp_telegram.linked_chat_fact import linked_chat_fact_owner
    from mcp_telegram.telegram_demand import RpcAttemptBudget

    dialog_id = -1000000000123
    sync_db.execute(
        "INSERT INTO dialogs (dialog_id, type, linked_chat_id, linked_chat_resolved_at) "
        "VALUES (?, 'channel', NULL, 100)",
        (dialog_id,),
    )
    with sync_db:
        linked_chat_fact_owner.invalidate_from_update(sync_db, dialog_id, int(time.time()))

    class _Session:
        def get_input_entity(self, _peer: object) -> InputPeerChannel:
            return InputPeerChannel(channel_id=123, access_hash=1)

    class _FloodingClient:
        session = _Session()
        transaction_open_during_rpc: bool | None = None

        async def __call__(self, _request: object) -> object:
            self.transaction_open_during_rpc = sync_db.in_transaction
            raise TelegramRpcThrottled(retry_after_seconds=flood_wait_seconds)

    client = _FloodingClient()
    manager = EventHandlerManager(client, sync_db, shutdown_event)  # type: ignore[arg-type]
    before = int(time.time())
    await manager.retry_one_pending_linked_chat_fact(RpcAttemptBudget(limit=1))
    after = int(time.time())

    row = sync_db.execute(
        "SELECT retry_at, failure_count FROM linked_chat_fact_state WHERE channel_id=?", (dialog_id,)
    ).fetchone()
    assert row is not None
    assert before + expected_delay <= row[0] <= after + expected_delay
    assert row[1] == 1
    assert client.transaction_open_during_rpc is False


@pytest.mark.asyncio
async def test_linked_chat_no_due_self_heal_commits_before_return(
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    import time

    from mcp_telegram.linked_chat_fact import linked_chat_fact_owner
    from mcp_telegram.telegram_demand import RpcAttemptBudget

    dialog_id = -1000000000123
    sync_db.execute(
        "INSERT INTO dialogs (dialog_id, type, linked_chat_id, linked_chat_resolved_at) "
        "VALUES (?, 'channel', NULL, 100)",
        (dialog_id,),
    )
    with sync_db:
        linked_chat_fact_owner.invalidate_from_update(sync_db, dialog_id, int(time.time()))
        sync_db.execute("UPDATE linked_chat_fact_state SET retry_at=NULL WHERE channel_id=?", (dialog_id,))

    manager = EventHandlerManager(MagicMock(), sync_db, shutdown_event)  # type: ignore[arg-type]
    assert await manager.retry_one_pending_linked_chat_fact(RpcAttemptBudget(limit=1)) is False
    assert sync_db.in_transaction is False
    repaired_retry = sync_db.execute(
        "SELECT retry_at FROM linked_chat_fact_state WHERE channel_id=?", (dialog_id,)
    ).fetchone()
    assert repaired_retry is not None and repaired_retry[0] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("linked_chat_id", "expected_offers"),
    [
        (456, [DemandKind.SCHEDULED_DISCOVERY, DemandKind.COLD_PEER_PAGE]),
        (None, [DemandKind.SCHEDULED_DISCOVERY]),
    ],
)
async def test_accepted_linked_chat_publication_offers_scheduled_discovery_after_commit(
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
    linked_chat_id: int | None,
    expected_offers: list[DemandKind],
) -> None:
    import time
    from contextlib import closing
    from types import SimpleNamespace

    from telethon.tl.types import Channel, InputPeerChannel

    from mcp_telegram.linked_chat_fact import linked_chat_fact_owner
    from mcp_telegram.telegram_demand import RpcAttemptBudget

    dialog_id = -1000000000123
    channel_id = 123
    sync_db.execute(
        "INSERT INTO dialogs (dialog_id, type, linked_chat_id, linked_chat_resolved_at) "
        "VALUES (?, 'channel', NULL, 100)",
        (dialog_id,),
    )
    with sync_db:
        linked_chat_fact_owner.invalidate_from_update(sync_db, dialog_id, int(time.time()))

    full_result = SimpleNamespace(
        full_chat=SimpleNamespace(id=channel_id, linked_chat_id=linked_chat_id),
        chats=[Channel(id=channel_id, access_hash=0, title="channel", photo=None, date=None)],
    )

    class _Session:
        def get_input_entity(self, _peer: object) -> InputPeerChannel:
            return InputPeerChannel(channel_id=channel_id, access_hash=1)

    class _Client:
        session = _Session()

        async def __call__(self, _request: object) -> object:
            return full_result

    database_path = str(sync_db.execute("PRAGMA database_list").fetchone()[2])

    class _CommitObservingSink:
        def __init__(self) -> None:
            self.offered: list[DemandKind] = []
            self.published_at_offer: list[bool] = []

        def offer(self, kind: DemandKind) -> bool:
            self.offered.append(kind)
            with closing(sqlite3.connect(database_path)) as reader:
                fact = reader.execute(
                    "SELECT linked_chat_id, linked_chat_resolved_at FROM dialogs WHERE dialog_id=?", (dialog_id,)
                ).fetchone()
                pending = reader.execute(
                    "SELECT pending_generation FROM linked_chat_fact_state WHERE channel_id=?", (dialog_id,)
                ).fetchone()
            self.published_at_offer.append(
                fact is not None
                and fact[0] == (None if linked_chat_id is None else -1000000000456)
                and fact[1] is not None
                and pending == (None,)
            )
            return True

    manager = EventHandlerManager(_Client(), sync_db, shutdown_event)  # type: ignore[arg-type]
    sink = _CommitObservingSink()
    manager.bind_demand_sink(sink)

    await manager.retry_one_pending_linked_chat_fact(RpcAttemptBudget(limit=1))

    assert sink.offered == expected_offers
    assert sink.published_at_offer == [True] * len(expected_offers)


def test_stale_linked_chat_publication_offers_no_followup_work(
    sync_db: _SQLiteConnection,
    shutdown_event: asyncio.Event,
) -> None:
    import time
    from types import SimpleNamespace

    from telethon.tl.types import Channel

    from mcp_telegram.channel_full_siblings import capture_channel_full_siblings_token
    from mcp_telegram.linked_chat_fact import linked_chat_fact_owner

    dialog_id = -1000000000123
    channel_id = 123
    now = int(time.time())
    sync_db.execute(
        "INSERT INTO dialogs (dialog_id, type, linked_chat_id, linked_chat_resolved_at) "
        "VALUES (?, 'channel', NULL, 100)",
        (dialog_id,),
    )
    with sync_db:
        linked_chat_fact_owner.invalidate_from_update(sync_db, dialog_id, now)
        work = linked_chat_fact_owner.next_due(sync_db, now + 1000)
        assert work is not None
        siblings_token = capture_channel_full_siblings_token(sync_db, dialog_id)
        linked_chat_fact_owner.invalidate_from_update(sync_db, dialog_id, now + 1)

    full_result = SimpleNamespace(
        full_chat=SimpleNamespace(id=channel_id, linked_chat_id=456),
        chats=[Channel(id=channel_id, access_hash=0, title="channel", photo=None, date=None)],
    )
    manager = EventHandlerManager(MagicMock(), sync_db, shutdown_event)  # type: ignore[arg-type]
    sink = _RecordingDemandSink()
    manager.bind_demand_sink(sink)

    manager._publish_linked_chat_observation(work, work.generation, siblings_token, full_result, now)

    assert sink.offered == []
