from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram.entity_profile.refresh import (
    EntityProfileDemandAdapter,
    EntityRefreshCoordinator,
)
from mcp_telegram.fact_hydration import (
    AppliedFacts,
    FactHydrationDemandAdapter,
    MessageFactHydrationWorker,
)
from mcp_telegram.hydration_queue import HydrationJob, HydrationPriority, HydrationQueueRepository
from mcp_telegram.message_fact_refresh import (
    MessageFactRefreshDemandAdapter,
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    ReadReceiptDemandAdapter,
)
from mcp_telegram.reactions.contracts import ReactionFetchResult, ReactionFreshness, ReactionSnapshot
from mcp_telegram.reactions.refresh import ReactionFreshener
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.telegram_demand import AcquisitionKind, DemandStatus, RpcAttemptBudget
from mcp_telegram.telegram_read_receipts import TelethonTelegramReadReceiptGateway
from mcp_telegram.telegram_reading import TelegramReadReceiptGateway
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcScope,
    TelegramRpcSource,
    current_rpc_scope,
    rpc_scope,
)


def _hydration_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE hydration_jobs (
            kind TEXT NOT NULL,
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            due_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            message_sent_at INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 1,
            terminal INTEGER NOT NULL DEFAULT 0,
            last_outcome TEXT,
            last_error_code TEXT,
            PRIMARY KEY (kind, dialog_id, message_id)
        ) WITHOUT ROWID"""
    )
    return conn


@pytest.mark.asyncio
async def test_hydration_adapters_partition_existing_queue_without_mutation() -> None:
    conn = _hydration_db()
    conn.executemany(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, priority, terminal) "
        "VALUES ('test', 1, ?, ?, ?, ?)",
        ((1, 50, 1, 0), (2, 75, 0, 0), (3, 10, 1, 1)),
    )
    handler = _HydrationHandler()
    worker = _hydration_worker(conn, handler, clock=lambda: 100.0)
    live = FactHydrationDemandAdapter(worker, HydrationPriority.FOREGROUND)
    backfill = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL)
    before = conn.total_changes

    assert live.demand_kind is DemandKind.LIVE_HYDRATION_BATCH
    assert backfill.demand_kind is DemandKind.BACKFILL_HYDRATION_BATCH
    assert live.status(100.0).release_at == 50.0  # type: ignore[union-attr]
    assert backfill.status(100.0).release_at == 75.0  # type: ignore[union-attr]
    assert conn.total_changes == before

    budget = RpcAttemptBudget(limit=1)
    await live.run_slice(budget)

    assert live.status(100.0) is None
    assert backfill.status(100.0) is not None
    assert budget.attempts == 1
    assert handler.scope is not None
    assert handler.scope.demand_kind is DemandKind.LIVE_HYDRATION_BATCH
    assert handler.scope.acquisition_kind is AcquisitionKind.MESSAGE_LOOKUP
    assert handler.scope.attempt_budget is budget
    conn.close()


class _HydrationHandler:
    kind = "test"
    batch_size = 10
    request_cost = 1
    pending_delay_seconds = 30

    def __init__(self) -> None:
        self.scope: TelegramRpcScope | None = None

    def eligible(self, conn: sqlite3.Connection, job: HydrationJob) -> bool:
        del conn, job
        return True

    async def request(self, client: object, jobs: Sequence[HydrationJob]) -> object:
        del client, jobs
        self.scope = current_rpc_scope()
        if self.scope.attempt_budget is not None:
            self.scope.attempt_budget.debit()
        return object()

    def apply(
        self,
        conn: sqlite3.Connection,
        queue: HydrationQueueRepository,
        jobs: Sequence[HydrationJob],
        result: object,
        *,
        now: int,
    ) -> AppliedFacts:
        del conn, result, now
        for job in jobs:
            queue.remove(job)
        return AppliedFacts(completed=len(jobs))

    def is_terminal_error(self, exc: BaseException) -> bool:
        del exc
        return False


def _hydration_worker(
    conn: sqlite3.Connection,
    handler: _HydrationHandler,
    *,
    clock: Callable[[], float] = lambda: 1.0,
) -> MessageFactHydrationWorker:
    return MessageFactHydrationWorker(
        object(),
        conn,
        asyncio.Event(),
        handlers=(handler,),
        interval_seconds=60,
        max_requests_per_cycle=2,
        max_jobs_per_cycle=1,
        retry_delay_seconds=30,
        circuit_retry_seconds=30,
        max_attempts=3,
        pause_between_requests_seconds=0.01,
        backfill_debt_limit=1,
        clock=clock,
    )


def _repair_hydration_worker(conn: sqlite3.Connection, handler: _HydrationHandler) -> MessageFactHydrationWorker:
    return MessageFactHydrationWorker(
        object(),
        conn,
        asyncio.Event(),
        handlers=(handler,),
        interval_seconds=60,
        max_requests_per_cycle=2,
        max_jobs_per_cycle=1,
        retry_delay_seconds=30,
        circuit_retry_seconds=30,
        max_attempts=3,
        pause_between_requests_seconds=0.01,
        backfill_debt_limit=1,
    )


def test_backfill_status_reports_repair_candidates_without_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (1, 1, 'explicit', 1)"
    )
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, media_kind, media_payload) "
        "VALUES (1, 1, 1, 'other', '{}')"
    )
    conn.commit()
    handler = _HydrationHandler()
    handler.kind = "media_metadata"
    worker = _repair_hydration_worker(conn, handler)
    adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL)
    before = conn.total_changes

    status = adapter.status(100.0)

    assert status == DemandStatus(release_at=100.0)
    assert conn.total_changes == before
    conn.close()


@pytest.mark.asyncio
async def test_backfill_slice_seeds_and_processes_repair_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (1, 1, 'explicit', 1)"
    )
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, media_kind, media_payload) "
        "VALUES (1, 1, 1, 'other', '{}')"
    )
    conn.commit()
    handler = _HydrationHandler()
    handler.kind = "media_metadata"
    worker = _hydration_worker(conn, handler, clock=lambda: 100.0)
    adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL)

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    conn.close()


@pytest.mark.asyncio
async def test_entity_profile_adapter_observes_queue_and_entity_lookup_context() -> None:
    observed: list[tuple[DemandKind | None, AcquisitionKind | None]] = []
    pending = True

    def status(_now: float) -> DemandStatus | None:
        return DemandStatus(release_at=0.0) if pending else None

    async def run_slice(_budget: RpcAttemptBudget) -> None:
        nonlocal pending
        scope = current_rpc_scope()
        observed.append((scope.demand_kind, scope.acquisition_kind))
        pending = False

    coordinator = EntityRefreshCoordinator()
    coordinator.bind_durable_executor(status, run_slice)
    adapter = EntityProfileDemandAdapter(coordinator)
    budget = RpcAttemptBudget(limit=1)

    initial_status = adapter.status(100.0)
    await adapter.run_slice(budget)

    assert initial_status is not None and initial_status.release_at == 0.0
    assert adapter.demand_kind is DemandKind.ENTITY_PROFILE_REFRESH
    assert budget.attempts == 0
    assert observed == [(DemandKind.ENTITY_PROFILE_REFRESH, AcquisitionKind.ENTITY_LOOKUP)]
    assert adapter.status(101.0) is None
    await coordinator.shutdown()


def _message_fact_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE synced_dialogs (dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE full_history_enrollment (dialog_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL);
        CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL);
        CREATE TABLE messages (
            dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            sent_at INTEGER NOT NULL, out INTEGER NOT NULL
        );
        CREATE TABLE message_reactions (
            dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            emoji TEXT NOT NULL, count INTEGER NOT NULL
        );
        CREATE TABLE message_reactions_freshness (
            dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            checked_at INTEGER NOT NULL, PRIMARY KEY (dialog_id, message_id)
        );
        CREATE TABLE message_read_facts (
            dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            read_at INTEGER, checked_at INTEGER NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY (dialog_id, message_id)
        );
        """
    )
    return conn


def _message_fact_policy(*, read_limit: int = 0) -> MessageFactRefreshPolicy:
    return MessageFactRefreshPolicy(
        interval_seconds=600,
        reaction_max_messages_per_cycle=10,
        read_at_max_messages_per_cycle=read_limit,
        pause_seconds=0.01,
        reaction_ttl_seconds=60,
        read_at_ttl_seconds=120,
    )


@pytest.mark.asyncio
async def test_message_fact_status_owns_reaction_candidates_without_mutation() -> None:
    conn = _message_fact_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (10, 'synced');
        INSERT INTO full_history_enrollment VALUES (10, 1);
        INSERT INTO entities VALUES (10, 'user');
        INSERT INTO messages VALUES (10, 1, 100, 0);
        INSERT INTO message_reactions VALUES (10, 1, 'x', 1);
        INSERT INTO message_reactions_freshness VALUES (10, 1, 100);
        """
    )
    deps = MessageFactRefreshDeps(
        conn,
        cast(ReactionFreshener, _MessageFactReactionFreshener()),
        cast(TelegramReadReceiptGateway, object()),
    )
    adapter = MessageFactRefreshDemandAdapter(deps, _message_fact_policy())
    before = conn.total_changes

    status = adapter.status(120.0)
    assert status is not None and status.release_at == 160.0
    assert adapter.demand_kind is DemandKind.MESSAGE_FACT_REFRESH
    assert conn.total_changes == before
    conn.close()


class _MessageFactReactionFreshener:
    def __init__(self) -> None:
        self.scopes: list[TelegramRpcScope] = []

    async def refresh(self, dialog_id: int, entity: object, message_ids: list[int]) -> ReactionFreshness:
        del dialog_id, entity
        scope = current_rpc_scope()
        self.scopes.append(scope)
        assert scope.attempt_budget is not None
        scope.attempt_budget.debit()
        return ReactionFreshness(len(message_ids), 0, len(message_ids), len(message_ids), "refreshed")


@pytest.mark.asyncio
async def test_message_fact_slice_runs_existing_cycle_under_budget() -> None:
    conn = _message_fact_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (10, 'synced');
        INSERT INTO full_history_enrollment VALUES (10, 1);
        INSERT INTO entities VALUES (10, 'user');
        INSERT INTO messages VALUES (10, 1, 100, 0);
        INSERT INTO message_reactions VALUES (10, 1, 'x', 1);
        """
    )
    freshener = _MessageFactReactionFreshener()
    adapter = MessageFactRefreshDemandAdapter(
        MessageFactRefreshDeps(
            conn,
            cast(ReactionFreshener, freshener),
            cast(TelegramReadReceiptGateway, object()),
        ),
        _message_fact_policy(),
    )
    budget = RpcAttemptBudget(limit=1)

    await adapter.run_slice(budget)

    assert budget.attempts == 1
    assert len(freshener.scopes) == 1
    assert freshener.scopes[0].demand_kind is DemandKind.MESSAGE_FACT_REFRESH
    conn.close()


class _ReactionRepository:
    def stale_reaction_ids(
        self, dialog_id: int, message_ids: Sequence[int], threshold: int
    ) -> tuple[str, set[int], list[int]]:
        del dialog_id, threshold
        return "active", set(), list(message_ids)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def history_enabled(self, dialog_id: int) -> bool:
        del dialog_id
        return True

    def persist_reaction_snapshots(
        self, dialog_id: int, snapshots: Sequence[ReactionSnapshot | None], checked_at: int
    ) -> int:
        del dialog_id, checked_at
        return len(snapshots)


class _ReactionGateway:
    def __init__(self) -> None:
        self.scope: TelegramRpcScope | None = None

    async def fetch_reactions(self, entity: object, message_ids: Sequence[int]) -> ReactionFetchResult:
        del entity
        self.scope = current_rpc_scope()
        return ReactionFetchResult(
            messages=tuple(ReactionSnapshot(message_id=message_id, aggregates=()) for message_id in message_ids)
        )


@pytest.mark.asyncio
async def test_reaction_acquisition_refines_message_fact_root() -> None:
    gateway = _ReactionGateway()
    freshener = ReactionFreshener(
        _ReactionRepository(),
        gateway,
        freshness_ttl_seconds=60,
        now=lambda: 100.0,
    )

    with rpc_scope(TelegramRpcSource.MESSAGE_FACT_REFRESH):
        await freshener.refresh(10, 10, [1])

    assert gateway.scope is not None
    assert gateway.scope.demand_kind is DemandKind.MESSAGE_FACT_REFRESH
    assert gateway.scope.acquisition_kind is AcquisitionKind.REACTION_SNAPSHOT


@pytest.mark.asyncio
async def test_direct_reaction_acquisition_uses_inline_root() -> None:
    gateway = _ReactionGateway()
    freshener = ReactionFreshener(
        _ReactionRepository(),
        gateway,
        freshness_ttl_seconds=60,
        now=lambda: 100.0,
    )

    await freshener.refresh(10, 10, [1])

    assert gateway.scope is not None
    assert gateway.scope.demand_kind is DemandKind.REACTION_REFRESH_BATCH
    assert gateway.scope.acquisition_kind is AcquisitionKind.REACTION_SNAPSHOT


def _read_position_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
            read_inbox_max_id INTEGER, read_outbox_max_id INTEGER,
            read_position_next_attempt_at INTEGER
        );
        CREATE TABLE full_history_enrollment (dialog_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL);
        CREATE TABLE dialogs (dialog_id INTEGER PRIMARY KEY, unread_count INTEGER);
        CREATE TABLE messages (
            dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            is_deleted INTEGER NOT NULL, is_service INTEGER NOT NULL, out INTEGER NOT NULL
        );
        """
    )
    return conn


@pytest.mark.asyncio
async def test_read_receipt_adapter_reports_only_read_position_queue() -> None:
    conn = _read_position_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (10, 'synced', 5, 6, 300);
        INSERT INTO full_history_enrollment VALUES (10, 1);
        INSERT INTO dialogs VALUES (10, 2);
        """
    )
    observed = []

    async def run_batch() -> object:
        scope = current_rpc_scope()
        observed.append(scope)
        assert scope.attempt_budget is not None
        scope.attempt_budget.debit()
        return object()

    adapter = ReadReceiptDemandAdapter(conn, run_batch)
    budget = RpcAttemptBudget(limit=1)
    before = conn.total_changes

    status = adapter.status(200.0)
    await adapter.run_slice(budget)

    assert adapter.demand_kind is DemandKind.READ_RECEIPT_BATCH
    assert status is not None and status.release_at == 300.0
    assert budget.attempts == 1
    assert observed[0].demand_kind is DemandKind.READ_RECEIPT_BATCH
    assert observed[0].acquisition_kind is AcquisitionKind.READ_RECEIPT_SNAPSHOT
    assert conn.total_changes == before
    conn.execute("UPDATE dialogs SET unread_count = 0")
    assert adapter.status(200.0) is None
    conn.close()


@pytest.mark.asyncio
async def test_exact_read_date_refines_message_fact_root() -> None:
    observed = []

    class Client:
        async def get_input_entity(self, entity: object) -> object:
            observed.append(current_rpc_scope())
            return entity

        async def __call__(self, request: object) -> object:
            del request
            observed.append(current_rpc_scope())
            return SimpleNamespace(date=datetime.fromtimestamp(1_700_000_000, tz=UTC))

    with rpc_scope(TelegramRpcSource.MESSAGE_FACT_REFRESH):
        result = await TelethonTelegramReadReceiptGateway(Client()).fetch_outbox_read_date(10, 1)

    assert result.status == "complete"
    assert [scope.demand_kind for scope in observed] == [
        DemandKind.MESSAGE_FACT_REFRESH,
        DemandKind.MESSAGE_FACT_REFRESH,
    ]
    assert {scope.acquisition_kind for scope in observed} == {AcquisitionKind.READ_RECEIPT_SNAPSHOT}
