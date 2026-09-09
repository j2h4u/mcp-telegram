from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.folders.contracts import DialogCategory, DialogFacts, FolderRule, FolderSourceSnapshot
from mcp_telegram.folders.refresh import FolderRefresher
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.folders.worker import FolderProjectionDemandAdapter, FolderProjectionWorker
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget, current_demand_token
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcScope, current_rpc_scope


@dataclass(frozen=True, slots=True)
class _Policy:
    refresh_interval_seconds: float = 100.0
    jitter_ratio: float = 0.0
    retry_delays_seconds: tuple[int, ...] = (60, 120, 240, 480)
    retry_cap_seconds: int = 900
    warning_failure_threshold: int = 3
    stale_threshold_seconds: int = 1_800


def _snapshot() -> FolderSourceSnapshot:
    return FolderSourceSnapshot(
        folders=(FolderRule(1, "Work", categories=frozenset({DialogCategory.CONTACT})),),
        dialogs=(DialogFacts(10, DialogCategory.CONTACT),),
    )


class _Gateway:
    def __init__(self, value: object = None) -> None:
        self.value = value
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_snapshot(self) -> FolderSourceSnapshot:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        if self.value == "block":
            await self.release.wait()
        self.active -= 1
        if isinstance(self.value, BaseException):
            raise self.value
        return _snapshot()


def _db(tmp_path: Path) -> tuple[sqlite3.Connection, SQLiteFolderSnapshotRepository]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    return conn, SQLiteFolderSnapshotRepository(conn)


def _worker(
    gateway: _Gateway,
    repository: SQLiteFolderSnapshotRepository,
    *,
    now: list[float] | None = None,
) -> FolderProjectionWorker:
    clock = (lambda: now[0]) if now is not None else time.time
    return FolderProjectionWorker(
        FolderRefresher(gateway, repository),
        repository,
        asyncio.Event(),
        _Policy(),
        clock=clock,
        jitter=lambda interval, _ratio: interval,
    )


def _demand_adapter(
    repository: SQLiteFolderSnapshotRepository,
    gateway: _Gateway | None = None,
) -> FolderProjectionDemandAdapter:
    return FolderProjectionDemandAdapter(_worker(gateway or _Gateway(), repository))


@pytest.mark.asyncio
async def test_demand_adapter_reports_startup_demand_and_runs_one_worker_attempt(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    try:
        gateway = _Gateway()
        adapter = _demand_adapter(repository, gateway)
        status = adapter.status(100.0)
        budget = RpcAttemptBudget(limit=1)
        assert status is not None

        await adapter.run_slice(budget)

        assert adapter.demand_kind is DemandKind.FOLDER_SNAPSHOT
        assert status.release_at == 100.0
        assert status.freshness_deadline is None
        assert budget.attempts == 1
        assert gateway.calls == 1
        assert repository.read_last_outcome() == "success"
    finally:
        conn.close()


def test_demand_adapter_reconstructs_success_and_retry_cadence(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    try:
        repository.replace_snapshot(_snapshot(), ((1, 10),), completed_at=90)
        adapter = _demand_adapter(repository)

        status = adapter.status(100.0)
        assert status is not None

        assert status.release_at == 190.0
        assert status.freshness_deadline == 1890

        repository.record_attempt(
            attempted_at=100,
            outcome="source_unavailable",
            next_retry_at=200,
            consecutive_failures=1,
        )
        retry_status = adapter.status(150.0)
        assert retry_status is not None
        assert retry_status.release_at == 200.0
        assert retry_status.freshness_deadline == 1890
    finally:
        conn.close()


def test_demand_adapter_reports_freshness_debt_from_durable_state(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    try:
        repository.replace_snapshot(_snapshot(), ((1, 10),), completed_at=90)
        status = _demand_adapter(repository).status(2_000.0)
        assert status is not None
        assert status.overdue_seconds(2_000.0) == 110.0
    finally:
        conn.close()


def test_demand_adapter_suppresses_terminal_failure_until_legacy_restart(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    try:
        repository.record_attempt(
            attempted_at=100,
            outcome="circuit_open",
            next_retry_at=None,
            consecutive_failures=1,
        )
        assert _demand_adapter(repository).status(100.0) is None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_demand_adapter_runs_one_worker_attempt_with_precise_context(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    gateway = _Gateway()
    seen: list[object] = []
    scopes: list[TelegramRpcScope] = []
    original_fetch = gateway.fetch_snapshot

    async def fetch_with_context() -> FolderSourceSnapshot:
        seen.append(current_demand_token())
        scopes.append(current_rpc_scope())
        return await original_fetch()

    gateway.fetch_snapshot = fetch_with_context  # type: ignore[method-assign]
    try:
        adapter = _demand_adapter(repository, gateway)
        budget = RpcAttemptBudget(limit=1)

        await adapter.run_slice(budget)

        assert budget.attempts == 1
        assert gateway.calls == 1
        assert seen[0].kind is DemandKind.FOLDER_SNAPSHOT  # type: ignore[union-attr]
        assert scopes[0].demand_kind is DemandKind.FOLDER_SNAPSHOT
        assert scopes[0].attempt_budget is budget
        assert repository.read_last_outcome() == "success"
        assert repository.read_last_success_at() is not None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_startup_prime_is_single_and_run_does_not_duplicate(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    gateway = _Gateway()
    worker = _worker(gateway, repository)
    try:
        await worker.prime()
        worker._shutdown_event.set()  # type: ignore[attr-defined]
        await worker.run()
        assert gateway.calls == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_restart_honors_future_persisted_retry_without_rpc(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    now = [100.0]
    repository.record_attempt(
        attempted_at=100,
        outcome="source_unavailable",
        next_retry_at=200,
        consecutive_failures=1,
    )
    gateway = _Gateway()
    try:
        worker = _worker(gateway, repository, now=now)
        await worker.prime()
        assert gateway.calls == 0

        now[0] = 200
        restarted = _worker(gateway, repository, now=now)
        await restarted.prime()
        assert gateway.calls == 1
        assert repository.read_last_outcome() == "success"
        assert restarted._next_due_at == 300  # type: ignore[attr-defined]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_restart_honors_fresh_success_without_rpc(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    repository.replace_snapshot(_snapshot(), ((1, 10),), completed_at=90)
    gateway = _Gateway()
    try:
        worker = _worker(gateway, repository, now=[100.0])
        await worker.prime()
        assert gateway.calls == 0
        assert worker._next_due_at == 190  # type: ignore[attr-defined]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_success_completion_is_after_acquisition_and_drives_due_time(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    now = [100.0]
    gateway = _Gateway()
    original_fetch = gateway.fetch_snapshot

    async def delayed_fetch() -> FolderSourceSnapshot:
        result = await original_fetch()
        now[0] = 150.0
        return result

    gateway.fetch_snapshot = delayed_fetch  # type: ignore[method-assign]
    worker = _worker(gateway, repository, now=now)
    try:
        await worker.prime()
        assert repository.read_last_success_at() == 150
        assert worker._next_due_at == 250  # type: ignore[attr-defined]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_concurrent_attempts_are_single_flight(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    gateway = _Gateway("block")
    worker = _worker(gateway, repository)
    first = asyncio.create_task(worker._attempt("scheduled"))  # type: ignore[attr-defined]
    await gateway.started.wait()
    second = asyncio.create_task(worker._attempt("scheduled"))  # type: ignore[attr-defined]
    await asyncio.sleep(0)
    assert gateway.max_active == 1
    gateway.release.set()
    await asyncio.gather(first, second)
    try:
        assert gateway.max_active == 1
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "outcome", "expected_retry_at"),
    [
        (TimeoutError("network"), "source_unavailable", 260),
        (TelegramRpcThrottled(latched=True, detail="open"), "circuit_open", None),
    ],
)
async def test_expected_failures_preserve_snapshot_and_retry_state(
    tmp_path: Path,
    error: Exception,
    outcome: str,
    expected_retry_at: int | None,
) -> None:
    conn, repository = _db(tmp_path)
    repository.replace_snapshot(_snapshot(), ((1, 10),), completed_at=90)
    gateway = _Gateway(error)
    worker = _worker(gateway, repository, now=[200.0])
    try:
        await worker.prime()
        assert repository.read_last_outcome() == outcome
        assert repository.read_next_retry_at() == expected_retry_at
        assert repository.read_last_success_at() == 90
        assert worker._next_due_at == expected_retry_at  # type: ignore[attr-defined]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_flood_wait_uses_requested_delay_without_same_cycle_retry(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    gateway = _Gateway(TelegramRpcThrottled(retry_after_seconds=1_200))
    worker = _worker(gateway, repository, now=[100.0])
    try:
        await worker.prime()
        assert gateway.calls == 1
        assert repository.read_last_outcome() == "flood_wait"
        assert repository.read_next_retry_at() == 1_300
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_unexpected_failure_is_persisted_and_not_retried(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    gateway = _Gateway(RuntimeError("broken invariant"))
    worker = _worker(gateway, repository, now=[100.0])
    try:
        with pytest.raises(RuntimeError, match="broken invariant"):
            await worker.prime()
        assert repository.read_last_outcome() == "unexpected"
        assert repository.read_next_retry_at() is None
        assert gateway.calls == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_cancellation_propagates_without_recording_expected_failure(tmp_path: Path) -> None:
    conn, repository = _db(tmp_path)
    gateway = _Gateway("block")
    worker = _worker(gateway, repository)
    task = asyncio.create_task(worker.prime())
    await gateway.started.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert repository.read_last_outcome() is None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_restart_above_warning_threshold_emits_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn, repository = _db(tmp_path)
    repository.record_attempt(
        attempted_at=100,
        outcome="source_unavailable",
        next_retry_at=200,
        consecutive_failures=4,
    )
    gateway = _Gateway()
    worker = _worker(gateway, repository, now=[100.0])
    try:
        with caplog.at_level("WARNING", logger="mcp_telegram.folders.worker"):
            await worker.prime()
        warnings = [record for record in caplog.records if record.message.startswith("folder_projection_warning")]
        assert len(warnings) == 1
        assert gateway.calls == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_warning_rearms_after_successful_recovery(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    conn, repository = _db(tmp_path)
    gateway = _Gateway(TimeoutError("network"))
    worker = _worker(gateway, repository, now=[100.0])
    try:
        with caplog.at_level("WARNING", logger="mcp_telegram.folders.worker"):
            for _ in range(3):
                await worker._attempt("scheduled")  # type: ignore[attr-defined]

            gateway.value = None
            await worker._attempt("scheduled")  # type: ignore[attr-defined]

            gateway.value = TimeoutError("network again")
            for _ in range(3):
                await worker._attempt("scheduled")  # type: ignore[attr-defined]

        warnings = [record for record in caplog.records if record.message.startswith("folder_projection_warning")]
        assert len(warnings) == 2
    finally:
        conn.close()
