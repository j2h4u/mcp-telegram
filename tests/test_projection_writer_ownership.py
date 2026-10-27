"""Projection orchestration must work with a genuinely read-only loop connection."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from telethon import types

from mcp_telegram.config import DraftRecoveryConfig, FolderProjectionConfig
from mcp_telegram.drafts.contracts import SnapshotCoverage
from mcp_telegram.drafts.owner import DraftMessageOwner
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.refresh import FolderRefresher
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.folders.worker import FolderProjectionWorker
from mcp_telegram.scheduled_messages import (
    ScheduledMessageReconciler,
    ScheduledReconciliationPolicy,
    _mark_scheduled_dialog_dirty,
    upsert_scheduled_message,
)
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.topics.contracts import TopicFact
from mcp_telegram.topics.refresh import TopicRefresher
from mcp_telegram.topics.sqlite_repository import SQLiteTopicSnapshotRepository
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_scheduled_messages import _message, _ScheduledSnapshotClient


@pytest_asyncio.fixture
async def storage(tmp_path: Path) -> AsyncIterator[tuple[sqlite3.Connection, SQLiteWriter]]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    reader.execute("PRAGMA query_only=ON")
    try:
        yield reader, writer
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        assert not reader.in_transaction
    finally:
        reader.close()
        await writer.aclose()


@pytest.mark.asyncio
async def test_topic_snapshot_uses_writer(storage: tuple[sqlite3.Connection, SQLiteWriter]) -> None:
    reader, writer = storage

    class Gateway:
        async def fetch_topics(self, entity: object) -> tuple[TopicFact, ...]:
            del entity
            return (TopicFact(topic_id=7, title="Topic"),)

    refresher = TopicRefresher(Gateway(), SQLiteTopicSnapshotRepository(reader), writer=writer)
    assert await refresher.refresh(42, SimpleNamespace(forum=True)) == 1
    assert reader.execute("SELECT title FROM topic_metadata WHERE dialog_id=42").fetchone()[0] == "Topic"


@pytest.mark.asyncio
async def test_folder_refresh_and_retry_use_writer(storage: tuple[sqlite3.Connection, SQLiteWriter]) -> None:
    reader, writer = storage

    class Gateway:
        fail = True

        async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
            if self.fail:
                raise OSError("source unavailable")
            return FolderRuleObservation((FolderRule(1, "One"),), "token", started_at)

    gateway = Gateway()
    repository = SQLiteFolderSnapshotRepository(reader)
    refresher = FolderRefresher(gateway, repository, writer=writer)
    policy = FolderProjectionConfig(retry_delays_seconds=(1,))
    worker = FolderProjectionWorker(refresher, repository, asyncio.Event(), policy, writer=writer, clock=lambda: 1000)
    await worker._attempt("test")
    assert repository.read_next_retry_at() is None
    assert repository.read_last_outcome() == "source_unavailable"
    assert repository.read_consecutive_failures() == 1
    gateway.fail = False
    await refresher.refresh(completed_at=1002)
    await refresher.reproject_current_rules(now=1003)
    assert repository.read_last_success_at() == 1002


@pytest.mark.asyncio
async def test_draft_realtime_and_recovery_use_writer(storage: tuple[sqlite3.Connection, SQLiteWriter]) -> None:
    reader, writer = storage

    class Gateway:
        async def fetch_all_drafts(self) -> tuple[SnapshotCoverage, tuple[()]]:
            return SnapshotCoverage(42, True, 0, source_order_at=datetime.now(UTC)), ()

    repository = SQLiteDraftProjection(reader, DraftRecoveryConfig(retry_delays_seconds=(1,)))
    owner = DraftMessageOwner(
        object(),
        repository,
        asyncio.Event(),
        UpdateProcessingBarrier(),
        writer=writer,
        snapshot_gateway_factory=lambda _client, _account: Gateway(),
    )
    await owner.bind_account(42)
    await owner.on_raw_draft_update(
        types.UpdateDraftMessage(types.PeerUser(91), types.DraftMessage("draft", datetime.now(UTC)))
    )
    assert reader.execute("SELECT COUNT(*) FROM draft_current").fetchone()[0] == 1
    await owner.request_recovery("test")
    await run_ready_slice(owner)
    assert repository.recovery_due_at() is None


@pytest.mark.asyncio
async def test_scheduled_snapshot_uses_writer(storage: tuple[sqlite3.Connection, SQLiteWriter]) -> None:
    reader, writer = storage

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            upsert_scheduled_message(conn, 42, _message(11), now=100)
            _mark_scheduled_dialog_dirty(conn, 42, 100)

    await writer.run(seed)
    reconciler = ScheduledMessageReconciler(
        _ScheduledSnapshotClient({42: []}),
        reader,
        asyncio.Event(),
        writer=writer,
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120),
    )
    with demand_context(DemandKind.SCHEDULED_REPAIR):
        assert await reconciler.run_demand_slice(DemandKind.SCHEDULED_REPAIR) == 1
    assert reader.execute("SELECT message_state FROM scheduled_messages").fetchone()[0] == "unknown_missing"
