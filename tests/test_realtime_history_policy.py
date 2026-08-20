"""Realtime history coverage policy matrix and event integration checks."""

# pyright: reportAny=false, reportArgumentType=false, reportOptionalSubscript=false, reportOperatorIssue=false, reportUndefinedVariable=false, reportMissingParameterType=false, reportReturnType=false, reportInvalidTypeForm=false, reportGeneralTypeIssues=false

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.types import PeerUser  # type: ignore[import-untyped]

from helpers import build_mock_message, build_mock_reactions
from mcp_telegram.event_handlers import EventHandlerManager
from mcp_telegram.realtime_history_policy import (
    RealtimeBodyEvent,
    RealtimeHistoryCoverage,
    allows_existing_body_update,
    allows_missing_body_insert,
    allows_new_message,
    realtime_history_coverage,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("synced", RealtimeHistoryCoverage.FULL_HISTORY),
        ("syncing", RealtimeHistoryCoverage.FULL_HISTORY),
        ("own_only", RealtimeHistoryCoverage.OWN_OUTGOING),
        ("not_synced", RealtimeHistoryCoverage.NO_REALTIME_HISTORY),
        ("fragment", RealtimeHistoryCoverage.NO_REALTIME_HISTORY),
        ("access_lost", RealtimeHistoryCoverage.NO_REALTIME_HISTORY),
        (None, RealtimeHistoryCoverage.NO_REALTIME_HISTORY),
        ("future_status", RealtimeHistoryCoverage.NO_REALTIME_HISTORY),
    ],
)
def test_realtime_history_status_matrix(status: str | None, expected: RealtimeHistoryCoverage) -> None:
    assert realtime_history_coverage(status) is expected


def test_own_only_requires_canonical_outgoing_for_all_body_families() -> None:
    coverage = RealtimeHistoryCoverage.OWN_OUTGOING
    assert allows_new_message(coverage, outgoing=True)
    assert not allows_new_message(coverage, outgoing=False)
    for event in RealtimeBodyEvent:
        assert allows_existing_body_update(coverage, event, outgoing=True)
        assert not allows_existing_body_update(coverage, event, outgoing=False)
        assert allows_missing_body_insert(coverage, event, outgoing=True) is (
            event not in (RealtimeBodyEvent.REACTION, RealtimeBodyEvent.DELETE)
        )
        assert not allows_missing_body_insert(coverage, event, outgoing=False)


@pytest.fixture()
def sync_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    yield conn
    conn.close()


def _manager(conn: sqlite3.Connection, client: MagicMock) -> EventHandlerManager:
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (42, 'own_only')")
    conn.commit()
    manager = EventHandlerManager(client, conn, asyncio.Event())
    manager.register()
    return manager


def _insert_message(conn: sqlite3.Connection, *, out: int, message_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, sender_id, out, is_deleted) "
        "VALUES (42, ?, 1704067200, 'old', 7, ?, 0)",
        (message_id, out),
    )
    conn.commit()


@pytest.mark.asyncio
async def test_own_only_rejects_inbound_new_edit_reaction_transcription_delete(
    sync_db: sqlite3.Connection,
) -> None:
    client = MagicMock()
    fetched_inbound = build_mock_message(id=1, text="new")
    fetched_inbound.out = False
    client.get_messages = AsyncMock(return_value=[fetched_inbound])
    manager = _manager(sync_db, client)
    inbound = build_mock_message(id=1, text="new", reactions=build_mock_reactions({"👍": 1}))
    inbound.out = False
    await manager.on_new_message(SimpleNamespace(chat_id=42, is_private=False, message=inbound))
    await manager.on_message_edited(SimpleNamespace(chat_id=42, message=inbound))
    await manager.on_raw_reaction_update(SimpleNamespace(peer=PeerUser(user_id=42), msg_id=1))
    await manager.on_raw_transcribed_audio(SimpleNamespace(peer=PeerUser(user_id=42), msg_id=1, text="speech"))
    await manager.on_message_deleted(SimpleNamespace(chat_id=42, deleted_ids=[1]))
    assert sync_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert sync_db.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 0
    assert sync_db.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0] == 0
    assert sync_db.execute("SELECT last_event_at FROM synced_dialogs WHERE dialog_id=42").fetchone()[0] is None
    assert client.get_messages.await_count == 1


@pytest.mark.asyncio
async def test_own_only_inbound_new_keeps_dialog_and_topic_metadata_only(
    sync_db: sqlite3.Connection,
) -> None:
    from telethon.tl.types import MessageActionTopicCreate  # type: ignore[import-untyped]

    sync_db.execute("INSERT INTO dialogs(dialog_id, snapshot_at) VALUES (42, 1)")
    sync_db.commit()
    manager = _manager(sync_db, MagicMock())
    message = build_mock_message(id=7, text="inbound")
    message.out = False
    message.action = MessageActionTopicCreate(
        title="Inbound topic",
        icon_color=0,
        title_missing=None,
        icon_emoji_id=None,
    )
    await manager.on_new_message(SimpleNamespace(chat_id=42, is_private=False, message=message))
    dialog = sync_db.execute("SELECT last_message_at FROM dialogs WHERE dialog_id=42").fetchone()
    assert dialog[0] == 1704110400
    assert sync_db.execute("SELECT title FROM topic_metadata WHERE dialog_id=42 AND topic_id=7").fetchone() == (
        "Inbound topic",
    )
    assert sync_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert sync_db.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 0
    assert sync_db.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0] == 0
    assert sync_db.execute("SELECT COUNT(*) FROM message_reactions").fetchone()[0] == 0
    assert sync_db.execute("SELECT last_event_at FROM synced_dialogs WHERE dialog_id=42").fetchone()[0] is None


@pytest.mark.asyncio
async def test_own_only_outgoing_new_is_persisted(sync_db: sqlite3.Connection) -> None:
    manager = _manager(sync_db, MagicMock())
    message = build_mock_message(id=8, text="outgoing")
    message.out = True
    await manager.on_new_message(SimpleNamespace(chat_id=42, is_private=False, message=message))
    assert sync_db.execute("SELECT text, out FROM messages WHERE dialog_id=42 AND message_id=8").fetchone() == (
        "outgoing",
        1,
    )
    assert sync_db.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_own_only_allows_existing_outgoing_updates(sync_db: sqlite3.Connection) -> None:
    _insert_message(sync_db, out=1)
    client = MagicMock()
    client.get_messages = AsyncMock(
        return_value=[build_mock_message(id=1, text="old", reactions=build_mock_reactions({"👍": 2}))]
    )
    manager = _manager(sync_db, client)
    edited = build_mock_message(id=1, text="new")
    await manager.on_message_edited(SimpleNamespace(chat_id=42, message=edited))
    await manager.on_raw_reaction_update(SimpleNamespace(peer=PeerUser(user_id=42), msg_id=1))
    await manager.on_raw_transcribed_audio(SimpleNamespace(peer=PeerUser(user_id=42), msg_id=1, text="speech"))
    await manager.on_message_deleted(SimpleNamespace(chat_id=42, deleted_ids=[1]))
    assert sync_db.execute("SELECT text, out, is_deleted FROM messages WHERE message_id=1").fetchone() == (
        "speech",
        1,
        1,
    )


@pytest.mark.asyncio
async def test_own_only_delete_filters_mixed_outgoing_and_inbound_rows(sync_db: sqlite3.Connection) -> None:
    _insert_message(sync_db, out=1, message_id=10)
    _insert_message(sync_db, out=0, message_id=11)
    manager = _manager(sync_db, MagicMock())
    await manager.on_message_deleted(SimpleNamespace(chat_id=42, deleted_ids=[10, 11]))
    assert sync_db.execute("SELECT message_id, is_deleted FROM messages ORDER BY message_id").fetchall() == [
        (10, 1),
        (11, 0),
    ]
    assert sync_db.execute("SELECT last_event_at FROM synced_dialogs WHERE dialog_id=42").fetchone()[0] is not None


@pytest.mark.asyncio
async def test_new_message_status_race_fails_closed_after_fetch(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_db.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (42, 'synced')")
    sync_db.commit()
    client = MagicMock()

    async def fetch_forward(*_args: object, **_kwargs: object) -> dict[int, str]:
        sync_db.execute("UPDATE synced_dialogs SET status='access_lost' WHERE dialog_id=42")
        sync_db.commit()
        return {}

    monkeypatch.setattr("mcp_telegram.event_handlers._build_fwd_entity_map", fetch_forward)
    manager = EventHandlerManager(client, sync_db, asyncio.Event())
    manager.register()
    await manager.on_new_message(SimpleNamespace(chat_id=42, is_private=False, message=build_mock_message(id=4)))
    assert sync_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
