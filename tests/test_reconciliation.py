"""Tests for the entity-only dialog reconciliation worker."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import ChannelPrivateError, PeerIdInvalidError

from mcp_telegram.dialog_sync import DialogReconciliationWorker
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.topics.refresh import TopicRefresher
from mcp_telegram.topics.sqlite_repository import SQLiteTopicSnapshotRepository
from mcp_telegram.topics.telegram_adapter import TelethonTelegramTopicGateway


@pytest.fixture
def sync_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def shutdown_event() -> asyncio.Event:
    return asyncio.Event()


class _MockClient:
    def __init__(self) -> None:
        self.get_entity = AsyncMock()
        self.iter_dialogs = MagicMock()


@pytest.fixture
def mock_client() -> _MockClient:
    return _MockClient()


def _seed_dialog(conn: sqlite3.Connection, dialog_id: int, *, needs_refresh: int = 0, hidden: int = 0) -> None:
    with conn:
        conn.execute(
            "INSERT INTO dialogs (dialog_id, name, type, archived, pinned, snapshot_at, hidden, needs_refresh) "
            "VALUES (?, 'Old', 'user', 0, 0, 1700000000, ?, ?)",
            (dialog_id, hidden, needs_refresh),
        )


def _user(dialog_id: int, name: str = "Alice") -> object:
    return SimpleNamespace(id=dialog_id, access_hash=1, first_name=name, forum=False)


@pytest.mark.asyncio
async def test_recon_light_pass_resets_needs_refresh(
    sync_db: sqlite3.Connection, mock_client: _MockClient, shutdown_event: asyncio.Event
) -> None:
    _seed_dialog(sync_db, 100, needs_refresh=1)
    mock_client.get_entity.return_value = _user(100, "NewName")

    count = await DialogReconciliationWorker(mock_client, sync_db, shutdown_event).run_light_pass()

    assert count == 1
    assert sync_db.execute("SELECT name, needs_refresh FROM dialogs WHERE dialog_id=100").fetchone() == ("NewName", 0)


@pytest.mark.asyncio
async def test_recon_light_pass_skips_hidden_and_clean_rows(
    sync_db: sqlite3.Connection, mock_client: _MockClient, shutdown_event: asyncio.Event
) -> None:
    _seed_dialog(sync_db, 100, needs_refresh=1, hidden=1)
    _seed_dialog(sync_db, 200, needs_refresh=0)

    assert await DialogReconciliationWorker(mock_client, sync_db, shutdown_event).run_light_pass() == 0
    mock_client.get_entity.assert_not_called()


@pytest.mark.asyncio
async def test_recon_light_pass_never_calls_iter_dialogs(
    sync_db: sqlite3.Connection, mock_client: _MockClient, shutdown_event: asyncio.Event
) -> None:
    _seed_dialog(sync_db, 100, needs_refresh=1)
    mock_client.get_entity.return_value = _user(100)
    mock_client.iter_dialogs.side_effect = AssertionError("light pass must not enumerate dialogs")

    await DialogReconciliationWorker(mock_client, sync_db, shutdown_event).run_light_pass()

    mock_client.iter_dialogs.assert_not_called()


@pytest.mark.asyncio
async def test_recon_light_pass_flood_wait_advances_to_next_dialog(
    sync_db: sqlite3.Connection, mock_client: _MockClient, shutdown_event: asyncio.Event
) -> None:
    _seed_dialog(sync_db, 100, needs_refresh=1)
    _seed_dialog(sync_db, 200, needs_refresh=1)
    mock_client.get_entity.side_effect = [TelegramRpcThrottled(retry_after_seconds=1), _user(200, "B")]

    count = await DialogReconciliationWorker(mock_client, sync_db, shutdown_event).run_light_pass()

    assert count == 1
    assert mock_client.get_entity.call_count == 2
    assert sync_db.execute("SELECT needs_refresh FROM dialogs WHERE dialog_id=100").fetchone() == (1,)
    assert sync_db.execute("SELECT needs_refresh FROM dialogs WHERE dialog_id=200").fetchone() == (0,)


@pytest.mark.asyncio
async def test_recon_light_pass_flood_wait_returns_on_shutdown(
    sync_db: sqlite3.Connection, mock_client: _MockClient, shutdown_event: asyncio.Event
) -> None:
    _seed_dialog(sync_db, 100, needs_refresh=1)
    mock_client.get_entity.side_effect = TelegramRpcThrottled(retry_after_seconds=3600)
    shutdown_event.set()

    assert await DialogReconciliationWorker(mock_client, sync_db, shutdown_event).run_light_pass() == 0
    assert sync_db.execute("SELECT needs_refresh FROM dialogs WHERE dialog_id=100").fetchone() == (1,)


@pytest.mark.asyncio
async def test_recon_light_pass_access_lost_sets_hidden(
    sync_db: sqlite3.Connection, mock_client: _MockClient, shutdown_event: asyncio.Event
) -> None:
    _seed_dialog(sync_db, 100, needs_refresh=1)
    sync_db.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (100, 'syncing')")
    sync_db.commit()
    mock_client.get_entity.side_effect = ChannelPrivateError(request=None)

    await DialogReconciliationWorker(mock_client, sync_db, shutdown_event).run_light_pass()

    assert sync_db.execute("SELECT status FROM synced_dialogs WHERE dialog_id=100").fetchone() == ("access_lost",)
    assert sync_db.execute("SELECT hidden FROM dialogs WHERE dialog_id=100").fetchone() == (1,)


@pytest.mark.asyncio
async def test_recon_light_pass_peer_invalid_leaves_dirty(
    sync_db: sqlite3.Connection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_dialog(sync_db, 100, needs_refresh=1)
    mock_client.get_entity.side_effect = PeerIdInvalidError(request=None)

    with caplog.at_level(logging.WARNING):
        assert await DialogReconciliationWorker(mock_client, sync_db, shutdown_event).run_light_pass() == 0

    assert sync_db.execute("SELECT needs_refresh, hidden FROM dialogs WHERE dialog_id=100").fetchone() == (1, 0)
    assert any("recon_light_pass_peer_invalid" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_recon_light_pass_emits_complete_log(
    sync_db: sqlite3.Connection,
    mock_client: _MockClient,
    shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_dialog(sync_db, 100, needs_refresh=1)
    mock_client.get_entity.return_value = _user(100)

    with caplog.at_level(logging.INFO):
        await DialogReconciliationWorker(mock_client, sync_db, shutdown_event).run_light_pass()

    assert any("recon_light_pass_complete count=1" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_refresh_forum_topics_upserts(sync_db: sqlite3.Connection, shutdown_event: asyncio.Event) -> None:
    entity = MagicMock(forum=True)
    topic = SimpleNamespace(id=1, title="General", icon_emoji_id=None, date=None)
    topic_client = AsyncMock(return_value=SimpleNamespace(topics=[topic]))
    worker = DialogReconciliationWorker(
        topic_client,
        sync_db,
        shutdown_event,
        TopicRefresher(TelethonTelegramTopicGateway(topic_client), SQLiteTopicSnapshotRepository(sync_db)),
    )

    assert await worker._refresh_forum_topics(999, entity) == 1
    assert sync_db.execute("SELECT title FROM topic_metadata WHERE dialog_id=999 AND topic_id=1").fetchone() == (
        "General",
    )


@pytest.mark.asyncio
async def test_light_pass_refreshes_forum_topics(
    sync_db: sqlite3.Connection, mock_client: _MockClient, shutdown_event: asyncio.Event
) -> None:
    _seed_dialog(sync_db, 777, needs_refresh=1)
    entity = MagicMock(forum=True, first_name=None, last_name=None, title="Forum Group")
    entity.username = None
    entity.participants_count = None
    entity.date = None
    mock_client.get_entity.return_value = entity
    topic_client = AsyncMock(return_value=SimpleNamespace(topics=[]))
    worker = DialogReconciliationWorker(
        mock_client,
        sync_db,
        shutdown_event,
        TopicRefresher(TelethonTelegramTopicGateway(topic_client), SQLiteTopicSnapshotRepository(sync_db)),
    )
    with patch.object(worker, "_refresh_forum_topics", wraps=worker._refresh_forum_topics) as refresh:
        await worker.run_light_pass()

    refresh.assert_called_once_with(777, entity)
