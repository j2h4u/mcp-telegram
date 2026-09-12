"""Scheduler-facing local folder freshness tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.refresh import FolderRefresher
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.folders.worker import FolderProjectionDemandAdapter, FolderProjectionWorker
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget


class _Policy:
    refresh_interval_seconds = 900
    retry_delays_seconds = (1,)
    retry_cap_seconds = 10
    warning_failure_threshold = 1


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
        self.calls += 1
        return FolderRuleObservation((FolderRule(1, "One"),), "token", started_at)


@pytest.mark.asyncio
async def test_worker_does_not_observe_rules_before_exact_900_second_deadline(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE dialog_directory_publication SET account_id=1,generation=1,observation_started_at=1 WHERE singleton=1")
        repository = SQLiteFolderSnapshotRepository(conn)
        repository.project_observation(FolderRuleObservation((FolderRule(1, "One"),), "token", 100), completed_at=100)
        gateway = _Gateway()
        worker = FolderProjectionWorker(FolderRefresher(gateway, repository), repository, asyncio.Event(), _Policy(), clock=lambda: 999)
        await FolderProjectionDemandAdapter(worker).run_slice(RpcAttemptBudget(limit=1))
        assert gateway.calls == 0
        worker = FolderProjectionWorker(FolderRefresher(gateway, repository), repository, asyncio.Event(), _Policy(), clock=lambda: 1000)
        await FolderProjectionDemandAdapter(worker).run_slice(RpcAttemptBudget(limit=1))
        assert gateway.calls == 1
    finally:
        conn.close()
