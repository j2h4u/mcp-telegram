"""Retain response messages when another topic page owns the cursor."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl.types import Message, PeerUser

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
from mcp_telegram.topics.catalog import TopicCatalogRepository
from mcp_telegram.topics.contracts import TopicCursor, TopicFact, TopicPage
from mcp_telegram.topics.demand import TopicSnapshotDemandAdapter
from mcp_telegram.topics.telegram_adapter import TelethonTelegramTopicGateway
from tests.sqlite_writer_helpers import inline_writer
from tests.test_topic_demand import _CountingClient, _selected_budget, _topic
from tests.topic_test_helpers import seed_topic_account, topic_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["cursor", "revision", "scope"])
async def test_stale_page_retains_authorized_messages_without_changing_cursor(tmp_path: Path, race: str) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    seed_topic_account(conn, 42)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (42,'synced')")
    conn.commit()
    committed: list[tuple[object, ...]] = []
    scope = topic_scope()

    class CompetingPage(_CountingClient):
        async def __call__(self, request: object) -> object:
            await super().__call__(request)
            repository = TopicCatalogRepository(conn)
            if race == "cursor":
                newer = repository.begin_page(42)
                assert newer is not None
                assert repository.apply_page_outcome(
                    newer, TopicPage((TopicFact(1, "Winner"),), 2, True, TopicCursor(101, 10, 1))
                )[0]
            elif race == "revision":
                repository.request(42, "newer_event")
            else:
                change = scope.begin_change()
                scope.end_change(change)
                assert scope.publish(TelegramAuthScope(1, 1, 1, 1))
            conn.commit()
            committed.append(
                cast(
                    tuple[object, ...], conn.execute("SELECT * FROM topic_catalog_state WHERE dialog_id=42").fetchone()
                )
            )
            return SimpleNamespace(
                topics=[_topic(1)],
                count=2,
                order_by_create_date=True,
                messages=[
                    Message(id=10, peer_id=PeerUser(42), date=datetime.fromtimestamp(101, UTC), message="retained")
                ],
            )

    try:
        adapter = TopicSnapshotDemandAdapter(
            TelethonTelegramTopicGateway(CompetingPage()),
            conn,
            writer=inline_writer(conn),
            account_id=1,
            capture_scope=scope.capture,
            claim_scope=scope.claim,
        )
        result = await adapter.run_slice(_selected_budget(adapter, 4))
        assert result.kind is not SliceOutcomeKind.PROGRESSED
        assert conn.execute("SELECT * FROM topic_catalog_state WHERE dialog_id=42").fetchone() == committed[0]
        if race == "scope":
            assert conn.execute("SELECT COUNT(*) FROM topic_metadata").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
        else:
            assert conn.execute("SELECT title FROM topic_metadata WHERE dialog_id=42 AND topic_id=1").fetchone() == (
                "Winner" if race == "cursor" else "Topic 1",
            )
            assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=10").fetchone() == (
                "retained",
            )
        if race == "revision":
            resumed = TopicCatalogRepository(conn).begin_page(42)
            assert resumed is not None and resumed.cursor == TopicCursor()
            assert resumed.revision == committed[0][1]
            assert resumed.generation != committed[0][3]
    finally:
        conn.close()
