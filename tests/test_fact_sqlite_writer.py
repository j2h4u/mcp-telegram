"""File-backed owner coverage for asynchronous fact orchestration."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from mcp_telegram.fact_hydration import MessageFactHydrationWorker
from mcp_telegram.hydration_queue import HydrationPriority
from mcp_telegram.message_fact_refresh import (
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    refresh_message_facts_once,
)
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.models import ReadMessage
from mcp_telegram.reactions.contracts import (
    ReactionAggregate,
    ReactionDetailFetchResult,
    ReactionDetailPage,
    ReactionEvent,
)
from mcp_telegram.reactions.detail import ReactionDetailRefresher
from mcp_telegram.reactions.persistence import apply_aggregate_observation
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_fact_queries import enrich_read_at
from mcp_telegram.telegram_fragments import FragmentContextService
from mcp_telegram.telegram_reading import FragmentFetchResult, ReadDateFetchResult
from tests.test_fact_demand_adapters import _HydrationHandler
from tests.test_telegram_reading import _message


def _seed(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (1,'synced')")
        conn.execute("INSERT INTO full_history_enrollment VALUES (1,1,'explicit',1)")
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (1,'user','peer',1)")
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at,out) VALUES (1,2,1,1)")
        conn.execute("INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,priority) VALUES ('test',1,2,1,1)")
        apply_aggregate_observation(
            conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
        )


@pytest.mark.asyncio
async def test_fact_flows_use_owner_with_readonly_loop_connection(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    loop_thread = threading.get_ident()
    observations: list[tuple[str, str]] = []

    class Gateway:
        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            assert threading.get_ident() == loop_thread
            assert not conn.in_transaction
            return ReactionDetailFetchResult(page=ReactionDetailPage((ReactionEvent(7, "👍", None),), None))

        async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
            assert threading.get_ident() == loop_thread
            return ReadDateFetchResult(read_at=5, status="complete")

        async def fetch_context(self, dialog_id: int, anchor_message_id: int, window_size: int) -> FragmentFetchResult:
            assert threading.get_ident() == loop_thread
            return FragmentFetchResult(messages=(extract_message_row(dialog_id, _message(3)),))

    def observe(kind: str, outcome: str) -> None:
        assert threading.get_ident() == loop_thread
        observations.append((kind, outcome))

    try:
        await writer.run(_seed)
        gateway = Gateway()
        refresher = ReactionDetailRefresher(conn, gateway, writer=writer, observation_sink=observe)
        result = await refresh_message_facts_once(
            MessageFactRefreshDeps(conn, refresher, gateway, writer=writer),
            MessageFactRefreshPolicy(1, 0, 0, 60),
            now=100,
        )
        assert result.reaction_refreshed == 1
        assert conn.execute("SELECT status FROM message_reaction_event_status").fetchone() == ("complete",)
        assert observations == [("reaction.detail", "attempt"), ("reaction.detail", "complete")]

        enriched = await enrich_read_at(
            conn,
            gateway,
            1,
            [ReadMessage(dialog_id=1, message_id=2, sent_at=1, out=1)],
            writer=writer,
            dialog_type="user",
            read_at_ttl_seconds=60,
            checked_at=100,
        )
        assert enriched[0].read_at == 5
        fragment = await FragmentContextService(conn, gateway, writer=writer).fetch(1, 3, 1)
        assert fragment.ok
        assert conn.execute("SELECT message_id FROM messages WHERE message_id=3").fetchone() == (3,)

        worker = MessageFactHydrationWorker(
            object(),
            conn,
            asyncio.Event(),
            writer=writer,
            handlers=(_HydrationHandler(),),
            interval_seconds=60,
            max_requests_per_cycle=1,
            max_jobs_per_cycle=1,
            retry_delay_seconds=30,
            circuit_retry_seconds=30,
            max_attempts=3,
            pause_between_requests_seconds=0,
            backfill_debt_limit=1,
            clock=lambda: 100,
        )
        await worker.run_priority_slice(HydrationPriority.FOREGROUND, RpcAttemptBudget(limit=1))
        await worker._run_due_repairs(100, 100)
        assert conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind='test'").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM messages")
    finally:
        conn.close()
        await writer.aclose()
