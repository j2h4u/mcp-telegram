from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_telegram import fact_hydration
from mcp_telegram.entity_profile.refresh import (
    EntityProfileDemandAdapter,
    EntityRefreshCoordinator,
)
from mcp_telegram.fact_hydration import (
    AppliedFacts,
    FactHydrationDemandAdapter,
    MessageFactHydrationWorker,
)
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.hydration_queue import HydrationJob, HydrationPriority, HydrationQueueRepository
from mcp_telegram.media_hydration import MediaFactHydrationHandler
from mcp_telegram.message_fact_refresh import (
    ReadReceiptDemandAdapter,
)
from mcp_telegram.messages import sqlite_hydration_jobs
from mcp_telegram.messages.sqlite_hydration_jobs import HydrationRepairCursor
from mcp_telegram.sync_db import _apply_migrations, _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import enable_runtime_writes
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
)
from mcp_telegram.telegram_demand_coordinator import CoordinatorState, TelegramDemandCoordinator
from mcp_telegram.telegram_read_receipts import TelethonTelegramReadReceiptGateway
from mcp_telegram.telegram_rpc_consumers import DURABLE_DEMAND_ORDER, DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcScope,
    TelegramRpcSource,
    current_rpc_scope,
    rpc_scope,
)


def _hydration_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


@pytest.mark.asyncio
async def test_hydration_adapters_partition_existing_queue_without_mutation(request: pytest.FixtureRequest) -> None:
    conn = _hydration_db()
    request.addfinalizer(conn.close)
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

    def apply(  # noqa: PLR0913
        self,
        conn: sqlite3.Connection,
        queue: HydrationQueueRepository,
        jobs: Sequence[HydrationJob],
        result: object,
        *,
        now: int,
        observation_order: int | None = None,
    ) -> AppliedFacts:
        del conn, result, now, observation_order
        for job in jobs:
            queue.remove(job)
        return AppliedFacts(completed=len(jobs))

    def is_terminal_error(self, exc: BaseException) -> bool:
        del exc
        return False


class _ThrottledHydrationHandler(_HydrationHandler):
    def __init__(self, error: TelegramRpcThrottled, *, dispatch: bool) -> None:
        super().__init__()
        self._error = error
        self._dispatch = dispatch
        self.calls = 0

    async def request(self, client: object, jobs: Sequence[HydrationJob]) -> object:
        del client, jobs
        self.calls += 1
        self.scope = current_rpc_scope()
        if self._dispatch:
            assert self.scope.attempt_budget is not None
            self.scope.attempt_budget.debit()
        raise self._error


class _IdleDemandAdapter:
    def status(self, now: float) -> DemandStatus | None:
        del now
        return None

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        del budget


class _DemandObserver:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def observe_demand(self, **event: object) -> None:
        self.events.append(event)


def _hydration_coordinator(
    adapter: DurableDemandAdapter,
    *,
    observer: _DemandObserver,
    shutdown_event: asyncio.Event | None = None,
) -> TelegramDemandCoordinator:
    adapters: dict[DemandKind, DurableDemandAdapter] = {kind: _IdleDemandAdapter() for kind in DURABLE_DEMAND_ORDER}
    adapters[DemandKind.LIVE_HYDRATION_BATCH] = adapter
    return TelegramDemandCoordinator(adapters, shutdown_event, clock=lambda: 100.0, observer=observer)


def _hydration_worker(
    conn: sqlite3.Connection,
    handler: _HydrationHandler,
    *,
    clock: Callable[[], float] = lambda: 1.0,
) -> MessageFactHydrationWorker:
    conn.commit()
    enable_runtime_writes(conn)
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


@pytest.mark.asyncio
async def test_hydration_finite_throttle_reschedules_and_defers_through_coordinator() -> None:
    conn = _hydration_db()
    conn.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, priority) "
        "VALUES ('test', 1, 1, 1, 0, 1)"
    )
    handler = _ThrottledHydrationHandler(TelegramRpcThrottled(retry_after_seconds=17), dispatch=True)
    adapter = FactHydrationDemandAdapter(
        _hydration_worker(conn, handler, clock=lambda: 100.0), HydrationPriority.FOREGROUND
    )
    observer = _DemandObserver()
    coordinator = _hydration_coordinator(adapter, observer=observer)

    await coordinator._execute_slice(DemandKind.LIVE_HYDRATION_BATCH)

    assert conn.execute(
        "SELECT due_at, attempts, terminal, last_outcome FROM hydration_jobs WHERE message_id=1"
    ).fetchone() == (117, 1, 0, "rpc_paused")
    assert observer.events[-1] == {
        "outcome": "deferred",
        "demand_kind": DemandKind.LIVE_HYDRATION_BATCH,
        "actual_attempts": 1,
        "queue_age_seconds": None,
        "freshness_debt_seconds": None,
        "reason": "flood_wait",
    }
    conn.close()


@pytest.mark.asyncio
async def test_hydration_latched_throttle_restores_undispatched_job_and_stops_coordinator() -> None:
    conn = _hydration_db()
    try:
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, priority) "
            "VALUES ('test', 1, 1, 1, 2, 1)"
        )
        error = TelegramRpcThrottled(latched=True, detail="test circuit open")
        handler = _ThrottledHydrationHandler(error, dispatch=False)
        adapter = FactHydrationDemandAdapter(
            _hydration_worker(conn, handler, clock=lambda: 100.0), HydrationPriority.FOREGROUND
        )
        observer = _DemandObserver()
        shutdown_event = asyncio.Event()
        coordinator = _hydration_coordinator(adapter, observer=observer, shutdown_event=shutdown_event)

        await asyncio.wait_for(coordinator.run(), timeout=1.0)

        assert coordinator.state is CoordinatorState.STOPPED
        assert shutdown_event.is_set() is False
        assert handler.calls == 1
        assert conn.execute(
            "SELECT due_at, attempts, terminal, last_outcome, last_error_code FROM hydration_jobs WHERE message_id=1"
        ).fetchone() == (1, 2, 0, "rpc_paused", "TelegramRpcThrottled")
        assert [event["outcome"] for event in observer.events] == ["selected"]
    finally:
        conn.close()


def _repair_hydration_worker(
    conn: sqlite3.Connection,
    handler: _HydrationHandler,
    *,
    clock: Callable[[], float] = lambda: 100.0,
    interval_seconds: float = 60.0,
) -> MessageFactHydrationWorker:
    return MessageFactHydrationWorker(
        object(),
        conn,
        asyncio.Event(),
        handlers=(handler,),
        interval_seconds=interval_seconds,
        max_requests_per_cycle=2,
        max_jobs_per_cycle=1,
        retry_delay_seconds=30,
        circuit_retry_seconds=30,
        max_attempts=3,
        pause_between_requests_seconds=0.01,
        backfill_debt_limit=1,
        clock=clock,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_due", "expected_attempts"),
    [("transient", 430, 2), ("admission", 430, 1), ("flood_wait", 417, 2), ("pending", 480, 2)],
)
async def test_hydration_reschedules_from_slow_request_completion(
    outcome: str, expected_due: int, expected_attempts: int
) -> None:
    conn = _hydration_db()
    clock = [100.0]
    calls = 0

    class SlowHandler(_HydrationHandler):
        async def request(self, client: object, jobs: Sequence[HydrationJob]) -> object:
            nonlocal calls
            calls += 1
            clock[0] += 300
            if outcome == "admission":
                raise TelegramRpcAdmissionDeferred(retry_after_seconds=17)
            result = await super().request(client, jobs)
            if outcome == "transient":
                raise TimeoutError
            if outcome == "flood_wait":
                raise TelegramRpcThrottled(retry_after_seconds=17)
            return result

        def apply(  # noqa: PLR0913
            self,
            conn: sqlite3.Connection,
            queue: HydrationQueueRepository,
            jobs: Sequence[HydrationJob],
            result: object,
            *,
            now: int,
            observation_order: int | None = None,
        ) -> AppliedFacts:
            assert now == 400
            clock[0] += 50
            return AppliedFacts(pending=True)

    try:
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, message_sent_at, priority) "
            "VALUES ('test', 1, 1, 1, 1, 23, 1)"
        )
        worker = _hydration_worker(conn, SlowHandler(), clock=lambda: clock[0])
        if outcome == "flood_wait":
            with pytest.raises(TelegramRpcThrottled):
                await worker.run_priority_slice(HydrationPriority.FOREGROUND, RpcAttemptBudget(limit=1))
        else:
            await worker.run_priority_slice(HydrationPriority.FOREGROUND, RpcAttemptBudget(limit=1))
        assert conn.execute("SELECT due_at, attempts, terminal, message_sent_at FROM hydration_jobs").fetchone() == (
            expected_due,
            expected_attempts,
            0,
            23,
        )
        assert expected_due > clock[0]
        await worker.run_priority_slice(HydrationPriority.FOREGROUND, RpcAttemptBudget(limit=1))
        assert calls == 1
    finally:
        conn.close()


def test_backfill_status_uses_repair_deadline_without_candidate_scans(tmp_path: Path) -> None:
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
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    assert adapter.status(100.0) == DemandStatus(release_at=100.0)
    assert adapter.status(100.0) == DemandStatus(release_at=100.0)

    assert conn.total_changes == before
    assert not any("LEFT JOIN hydration_jobs" in statement for statement in statements)
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
    worker = _repair_hydration_worker(conn, handler, clock=lambda: 100.75, interval_seconds=0.5)
    adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL)
    assert adapter.status(100.75) == DemandStatus(release_at=100.75)

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    assert worker.next_repair_at == 101.25
    assert adapter.status(100.75) == DemandStatus(release_at=101.25)
    conn.close()


@pytest.mark.asyncio
async def test_backfill_queued_work_runs_during_repair_cooldown(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (1, 1, 'explicit', 1)"
    )
    conn.executemany(
        "INSERT INTO messages(dialog_id, message_id, sent_at, media_kind, media_payload) "
        "VALUES (1, ?, ?, 'other', '{}')",
        ((1, 1),),
    )
    conn.commit()
    handler = _HydrationHandler()
    handler.kind = "media_metadata"
    worker = _repair_hydration_worker(conn, handler)
    adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL)

    await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert worker.next_repair_at == 160

    conn.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, priority) "
        "VALUES ('media_metadata', 1, 3, 101, 0)"
    )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    worker._clock = lambda: 101.0
    handler.scope = None
    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert handler.scope is not None
    assert not any("LEFT JOIN hydration_jobs" in statement for statement in statements)
    assert worker.next_repair_at == 160
    conn.close()


@pytest.mark.asyncio
async def test_backfill_more_repair_candidates_keep_repair_due(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (1, 1, 'explicit', 1)"
    )
    conn.executemany(
        "INSERT INTO messages(dialog_id, message_id, sent_at, media_kind, media_payload) "
        "VALUES (1, ?, ?, 'other', '{}')",
        ((1, 1), (2, 2)),
    )
    conn.commit()
    handler = _HydrationHandler()
    handler.kind = "media_metadata"
    worker = _repair_hydration_worker(conn, handler)
    adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL)

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert worker.next_repair_at == 100
    assert adapter.status(100.0) == DemandStatus(release_at=100.0)
    conn.close()


@pytest.mark.asyncio
async def test_backfill_raw_pages_advance_past_existing_jobs_without_erasing_backoff(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (1, 1, 'explicit', 1)"
        )
        conn.executemany(
            "INSERT INTO messages(dialog_id, message_id, sent_at, media_kind, media_payload) VALUES (1, ?, ?, ?, '{}')",
            [(1, 10, "other"), (2, 9, "video"), (3, 8, "contact")],
        )
        conn.executemany(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, priority, terminal) "
            "VALUES ('media_metadata', 1, ?, ?, ?, 0, ?)",
            [(1, 4, 3, 1), (2, 999, 2, 0)],
        )
        conn.commit()
        handler = _HydrationHandler()
        handler.kind = "media_metadata"
        worker = _repair_hydration_worker(conn, handler)
        budget = RpcAttemptBudget(limit=1)
        budget.debit()
        await worker.run_priority_slice(HydrationPriority.BACKFILL, budget)
        assert worker._repair_cursors == (None, HydrationRepairCursor(10, 1, 1), None)
        assert worker.next_repair_at == 100
        await worker.run_priority_slice(HydrationPriority.BACKFILL, budget)
        assert worker._repair_cursors == (None, HydrationRepairCursor(10, 1, 1), HydrationRepairCursor(9, 1, 2))
        assert worker.next_repair_at == 100
        await worker.run_priority_slice(HydrationPriority.BACKFILL, budget)
        assert worker._repair_cursors == (None, None, None)
        assert worker.next_repair_at == 160
        assert conn.execute(
            "SELECT message_id, due_at, attempts, terminal FROM hydration_jobs ORDER BY message_id"
        ).fetchall() == [(1, 4, 3, 1), (2, 999, 2, 0), (3, 100, 0, 0)]
        assert handler.scope is None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_backfill_repair_failure_keeps_deadline_due() -> None:
    conn = _hydration_db()
    handler = _HydrationHandler()
    worker = _repair_hydration_worker(conn, handler)

    def fail(_now: int) -> bool:
        raise RuntimeError("producer failed")

    worker._run_repair_producers = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="producer failed"):
        await worker.run_priority_slice(HydrationPriority.BACKFILL, RpcAttemptBudget(limit=1))

    assert worker.next_repair_at == 100
    conn.close()


@pytest.mark.asyncio
async def test_slow_backfill_repair_cooldown_starts_after_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _hydration_db()
    clock = [100.0]
    worker = _repair_hydration_worker(conn, _HydrationHandler(), clock=lambda: clock[0])
    calls = 0

    def repair(_now: int) -> tuple[bool, tuple[None, None, None]]:
        nonlocal calls
        calls += 1
        clock[0] += 300
        return False, (None, None, None)

    monkeypatch.setattr(worker, "_run_repair_producers", repair)
    try:
        await worker.run_priority_slice(HydrationPriority.BACKFILL, RpcAttemptBudget(limit=1))
        assert worker.next_repair_at == 460
        await worker.run_priority_slice(HydrationPriority.BACKFILL, RpcAttemptBudget(limit=1))
        assert calls == 1
    finally:
        conn.close()


def test_repair_cursor_advances_only_after_commit_and_resets_at_completed_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _hydration_db()
    worker = _repair_hydration_worker(conn, _HydrationHandler())
    cursor = HydrationRepairCursor(23, 1, 5)
    next_cursors = (cursor, cursor, cursor)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE repair_parent(id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE repair_child(parent_id INTEGER REFERENCES repair_parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )

    def fail_commit(
        _now: int,
    ) -> tuple[bool, tuple[HydrationRepairCursor, HydrationRepairCursor, HydrationRepairCursor]]:
        conn.execute("INSERT INTO repair_child VALUES (1)")
        return True, next_cursors

    try:
        monkeypatch.setattr(worker, "_run_repair_producers", fail_commit)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            worker._run_due_repairs(100, 100)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM repair_child").fetchone() == (0,)
        assert worker._repair_cursors == (None, None, None)
        assert worker.next_repair_at == 100
        monkeypatch.setattr(worker, "_run_repair_producers", lambda _now: (True, next_cursors))
        worker._run_due_repairs(100, 100)
        assert worker._repair_cursors == next_cursors
        assert worker.next_repair_at == 100
        monkeypatch.setattr(worker, "_run_repair_producers", lambda _now: (False, next_cursors))
        worker._run_due_repairs(100, 100)
        assert worker._repair_cursors == (None, None, None)
        assert worker.next_repair_at == 160
    finally:
        conn.close()


def test_backfill_repair_rejects_foreign_transaction_without_rolling_it_back(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    reader = _open_sync_db(db_path)
    try:
        worker = _repair_hydration_worker(conn, _HydrationHandler())
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
        with pytest.raises(RuntimeError, match="requires no open transaction"):
            worker._run_due_repairs(100, 100)
        assert conn.in_transaction
        assert conn.execute("SELECT dialog_id FROM synced_dialogs").fetchall() == [(1,)]
        assert reader.execute("SELECT dialog_id FROM synced_dialogs").fetchall() == []
        assert worker._repair_cursors == (None, None, None)
        assert worker.next_repair_at == 100
    finally:
        reader.close()
        conn.close()


def test_backfill_repair_rolls_back_all_producers_and_retries_same_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    reader = _open_sync_db(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (1, 1, 'explicit', 1)"
        )
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, media_kind, media_payload) "
            "VALUES (1, 1, 1, 'voice', '{}')"
        )
        conn.commit()
        handler = _HydrationHandler()
        handler.kind = "transcription"
        worker = MessageFactHydrationWorker(
            object(),
            conn,
            asyncio.Event(),
            handlers=(handler, MediaFactHydrationHandler(batch_size=1)),
            interval_seconds=60,
            max_requests_per_cycle=2,
            max_jobs_per_cycle=2,
            retry_delay_seconds=30,
            circuit_retry_seconds=30,
            max_attempts=3,
            pause_between_requests_seconds=0.01,
            backfill_debt_limit=1,
            clock=lambda: 100.0,
        )
        media_repair = fact_hydration.repair_media_metadata_hydration_jobs

        def fail_second_producer(*_args: object, **_kwargs: object) -> None:
            assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (1,)
            raise sqlite3.OperationalError("producer failed")

        monkeypatch.setattr(fact_hydration, "repair_media_metadata_hydration_jobs", fail_second_producer)
        with pytest.raises(sqlite3.OperationalError, match="producer failed"):
            worker._run_due_repairs(100, 100)
        assert not conn.in_transaction
        assert reader.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
        assert worker._repair_cursors == (None, None, None)
        assert worker.next_repair_at == 100

        monkeypatch.setattr(fact_hydration, "repair_media_metadata_hydration_jobs", media_repair)
        worker._run_due_repairs(100, 100)
        assert not conn.in_transaction
        assert reader.execute("SELECT kind, message_id FROM hydration_jobs").fetchall() == [("transcription", 1)]
        assert worker.next_repair_at == 160
    finally:
        reader.close()
        conn.close()


def test_backfill_repair_owns_wal_writer_before_candidate_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    writer = sqlite3.connect(db_path, timeout=0)
    try:
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
        raw_page = sqlite_hydration_jobs._repair_raw_page
        interleaved = False

        def attempt_competing_write(*args: object, **kwargs: object) -> object:
            nonlocal interleaved
            rows = raw_page(*args, **kwargs)  # type: ignore[arg-type]
            if not interleaved:
                interleaved = True
                with pytest.raises(sqlite3.OperationalError) as error:
                    writer.execute("UPDATE synced_dialogs SET last_synced_at=2 WHERE dialog_id=1")
                assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
                writer.rollback()
            return rows

        monkeypatch.setattr(sqlite_hydration_jobs, "_repair_raw_page", attempt_competing_write)
        worker._run_due_repairs(100, 100)
        assert interleaved
        assert not conn.in_transaction
        assert conn.execute("SELECT kind, message_id FROM hydration_jobs").fetchall() == [("media_metadata", 1)]
        with writer:
            writer.execute("UPDATE synced_dialogs SET last_synced_at=2 WHERE dialog_id=1")
    finally:
        writer.close()
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
