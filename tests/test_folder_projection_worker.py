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
from mcp_telegram.folders.worker import FolderProjectionWorker
from mcp_telegram.sync_db import ensure_sync_schema


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
        (TimeoutError("network"), "source_unavailable", 160),
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
    worker = _worker(gateway, repository, now=[100.0])
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
