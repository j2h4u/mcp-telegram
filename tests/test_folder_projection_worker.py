"""Scheduler-facing local folder freshness tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.refresh import FolderRefresher
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.folders.worker import FolderAttemptResult, FolderProjectionDemandAdapter, FolderProjectionWorker
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import enable_runtime_writes, write_savepoint
from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred
from tests.sqlite_writer_helpers import inline_writer


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
async def test_forced_observation_starts_after_inflight_and_has_new_order(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Gateway:
        calls = 0

        async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
            return FolderRuleObservation((FolderRule(2, "One"),), "same", started_at)

    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=1,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        enable_runtime_writes(conn)
        repo = SQLiteFolderSnapshotRepository(conn)
        gateway = Gateway()
        worker = FolderProjectionWorker(
            FolderRefresher(gateway, repo, writer=inline_writer(conn)),
            repo,
            asyncio.Event(),
            _Policy(),
            writer=inline_writer(conn),
            clock=lambda: 1000,
        )
        first = asyncio.create_task(worker._attempt("scheduled"))
        await entered.wait()
        second = asyncio.create_task(worker.force_refresh())
        await asyncio.sleep(0)
        assert await second is None
        assert gateway.calls == 1
        release.set()
        old = await first
        fresh = await worker.force_refresh()
        assert old is not None and fresh is not None and old.observation_order is not None
        assert (
            fresh.accepted and fresh.observation_order is not None and fresh.observation_order > old.observation_order
        )
        assert gateway.calls == 2
    finally:
        conn.close()


def _budget(adapter: FolderProjectionDemandAdapter, now: float) -> RpcAttemptBudget:
    status = adapter.status(now)
    assert status is not None and status.selected is not None
    return RpcAttemptBudget(limit=1, selected=status.selected)


@pytest.mark.asyncio
async def test_receipt_verification_shares_folder_demand_adapter(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    selected = WorkRevision(WorkKey(DemandKind.FOLDER_SNAPSHOT, 1, "receipt:operation"), "version")

    class Verification:
        def verification_status(self, now: float, excluded: frozenset[WorkRevision]) -> DemandStatus | None:
            return None if selected in excluded else DemandStatus(release_at=now, selected=selected)

        async def verification_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
            assert budget.selected == selected
            return SliceOutcome(selected, SliceOutcomeKind.TERMINAL, "completed")

    try:
        repo = SQLiteFolderSnapshotRepository(conn)
        writer = inline_writer(conn)
        worker = FolderProjectionWorker(
            FolderRefresher(_Gateway(), repo, writer=writer),
            repo,
            asyncio.Event(),
            _Policy(),
            writer=writer,
            clock=lambda: 1000,
        )
        adapter = FolderProjectionDemandAdapter(worker, account_id=1, verification=Verification())
        status = adapter.status(1000)
        assert status is not None and status.selected == selected
        outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=selected))
        assert outcome.kind is SliceOutcomeKind.TERMINAL
        status = adapter.status(1000, excluded=frozenset({selected}))
        assert status is not None and status.selected != selected
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_worker_does_not_observe_rules_before_exact_900_second_deadline(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=1,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        enable_runtime_writes(conn)
        repository = SQLiteFolderSnapshotRepository(conn)
        repository.project_observation(FolderRuleObservation((FolderRule(1, "One"),), "token", 100), completed_at=100)
        gateway = _Gateway()
        worker = FolderProjectionWorker(
            FolderRefresher(gateway, repository, writer=inline_writer(conn)),
            repository,
            asyncio.Event(),
            _Policy(),
            writer=inline_writer(conn),
            clock=lambda: 999,
        )
        adapter = FolderProjectionDemandAdapter(worker, account_id=1)
        await adapter.run_slice(_budget(adapter, 999))
        assert gateway.calls == 0
        worker = FolderProjectionWorker(
            FolderRefresher(gateway, repository, writer=inline_writer(conn)),
            repository,
            asyncio.Event(),
            _Policy(),
            writer=inline_writer(conn),
            clock=lambda: 1000,
        )
        adapter = FolderProjectionDemandAdapter(worker, account_id=1)
        await adapter.run_slice(_budget(adapter, 1000))
        assert gateway.calls == 1
        assert not conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_unexpected_folder_failure_keeps_durable_demand_due(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    now = [1000.0]

    class Gateway:
        calls = 0

        async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
            self.calls += 1
            if self.calls == 1:
                raise ValueError("transient normalization failure")
            return FolderRuleObservation((FolderRule(2, "Two"),), "token", started_at)

    try:
        repository = SQLiteFolderSnapshotRepository(conn)
        gateway = Gateway()
        worker = FolderProjectionWorker(
            FolderRefresher(gateway, repository, writer=inline_writer(conn)),
            repository,
            asyncio.Event(),
            _Policy(),
            writer=inline_writer(conn),
            clock=lambda: now[0],
        )
        adapter = FolderProjectionDemandAdapter(worker, account_id=1)
        with pytest.raises(ValueError, match="transient normalization"):
            await adapter.run_slice(_budget(adapter, now[0]))
        status = adapter.status(now[0])
        assert status is not None and status.release_at == 1000.0

        now[0] = 1001.0
        assert status.selected is not None
        await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        assert gateway.calls == 2
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_latched_folder_failure_propagates_without_private_retry(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:

        class Gateway:
            async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
                del started_at
                raise TelegramRpcThrottled(latched=True, detail="circuit open")

        repository = SQLiteFolderSnapshotRepository(conn)
        worker = FolderProjectionWorker(
            FolderRefresher(Gateway(), repository, writer=inline_writer(conn)),
            repository,
            asyncio.Event(),
            _Policy(),
            writer=inline_writer(conn),
            clock=lambda: 1000.0,
        )
        adapter = FolderProjectionDemandAdapter(worker, account_id=1)
        with pytest.raises(TelegramRpcThrottled, match="circuit open"):
            await adapter.run_slice(_budget(adapter, 1000))
        assert repository.read_next_retry_at() is None
        assert adapter.status(1000.0) is not None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_zero_balance_retry_candidate_runs_folder_preflight_without_spending_or_publishing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:

        class Gateway:
            calls = 0

            async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
                self.calls += 1
                raise TelegramRpcAdmissionDeferred(
                    retry_after_seconds=5,
                    method_key="messages.GetDialogFiltersRequest",
                    generation="retry-generation",
                    eligible_at=1100.0,
                )

        repository = SQLiteFolderSnapshotRepository(conn)
        gateway = Gateway()
        worker = FolderProjectionWorker(
            FolderRefresher(gateway, repository, writer=inline_writer(conn)),
            repository,
            asyncio.Event(),
            _Policy(),
            writer=inline_writer(conn),
            clock=lambda: 1000.0,
        )
        adapter = FolderProjectionDemandAdapter(worker, account_id=1)
        budget = _budget(adapter, 1000.0)
        budget.debit()
        budget._retry_candidate = object()

        with pytest.raises(TelegramRpcAdmissionDeferred):
            await adapter.run_slice(budget)

        assert gateway.calls == 1
        assert budget.attempts == 1
        assert repository.read_snapshot_revision() == (None, None, None, None)
        assert repository.read_next_retry_at() is None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_exhausted_folder_budget_without_retry_candidate_skips_gateway(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        repository = SQLiteFolderSnapshotRepository(conn)
        gateway = _Gateway()
        worker = FolderProjectionWorker(
            FolderRefresher(gateway, repository, writer=inline_writer(conn)),
            repository,
            asyncio.Event(),
            _Policy(),
            writer=inline_writer(conn),
            clock=lambda: 1000.0,
        )
        adapter = FolderProjectionDemandAdapter(worker, account_id=1)
        budget = _budget(adapter, 1000.0)
        budget.debit()

        outcome = await adapter.run_slice(budget)

        assert outcome.kind.value == "deferred"
        assert gateway.calls == 0
        assert budget.attempts == 1
        assert repository.read_snapshot_revision() == (None, None, None, None)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_pending_folder_observation_without_canonical_generation_is_not_progress(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        repository = SQLiteFolderSnapshotRepository(conn)
        gateway = _Gateway()
        worker = FolderProjectionWorker(
            FolderRefresher(gateway, repository, writer=inline_writer(conn)),
            repository,
            asyncio.Event(),
            _Policy(),
            writer=inline_writer(conn),
            clock=lambda: 1000.0,
        )
        adapter = FolderProjectionDemandAdapter(worker, account_id=1)

        outcome = await adapter.run_slice(_budget(adapter, 1000.0))

        assert gateway.calls == 1
        assert outcome.kind is SliceOutcomeKind.NO_PROGRESS
        assert repository.read_snapshot_revision() == (None, None, None, None)
        assert conn.execute("SELECT COUNT(*) FROM telegram_folder_rules").fetchone() == (0,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_folder_refresh_rejects_publication_after_selected_projection_changes(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=1,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        enable_runtime_writes(conn)
        repository = SQLiteFolderSnapshotRepository(conn)
        repository.project_observation(
            FolderRuleObservation((FolderRule(1, "Seed"),), "seed-token", 100), completed_at=100
        )
        newer_observation = FolderRuleObservation((FolderRule(3, "Newer"),), "newer-token", 1001)

        class RacingGateway:
            calls = 0

            async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
                from mcp_telegram.observation_order import allocate_observation_order

                self.calls += 1
                assert (
                    repository.project_observation(
                        newer_observation, completed_at=1001, observation_order=allocate_observation_order(conn)
                    )
                    == 1
                )
                return FolderRuleObservation((FolderRule(2, "Older"),), "older-token", started_at)

        gateway = RacingGateway()
        worker = FolderProjectionWorker(
            FolderRefresher(gateway, repository, writer=inline_writer(conn)),
            repository,
            asyncio.Event(),
            _Policy(),
            writer=inline_writer(conn),
            clock=lambda: 1000.0,
        )
        adapter = FolderProjectionDemandAdapter(worker, account_id=1)
        budget = _budget(adapter, 1000.0)

        outcome = await adapter.run_slice(budget)

        assert gateway.calls == 1
        assert outcome.kind is SliceOutcomeKind.STALE
        assert set(
            conn.execute("SELECT title FROM telegram_folder_rules WHERE catalog_member=1 AND deleted=0").fetchall()
        ) == {("Newer",), ("Older",)}
        assert repository.read_catalog_version()[3] is False
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["intake", "account", "auth"])
async def test_folder_refresh_rejects_stale_intake_or_account(tmp_path: Path, change: str) -> None:
    from telethon.tl.types import UpdateDialogFilters

    from mcp_telegram.auth_scope import TelegramAuthScope
    from mcp_telegram.folders.intake import capture_folder_intake

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    auth = [TelegramAuthScope(1, 1, 2, 3)]
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=1,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        enable_runtime_writes(conn)
        repository = SQLiteFolderSnapshotRepository(conn)
        repository.project_observation(FolderRuleObservation((FolderRule(2, "Retained"),), "old", 1), completed_at=1)

        class Gateway:
            async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
                if change == "intake":
                    capture_folder_intake([UpdateDialogFilters()])
                elif change == "account":
                    with write_savepoint(conn):
                        conn.execute("UPDATE dialog_directory_publication SET account_id=2 WHERE singleton=1")
                else:
                    auth[0] = TelegramAuthScope(1, 1, 2, 4)
                return FolderRuleObservation((FolderRule(3, "Stale"),), "stale", started_at)

        result = await FolderRefresher(
            Gateway(), repository, writer=inline_writer(conn), auth_scope=lambda: auth[0]
        ).refresh(completed_at=10)
        assert not result.accepted
        assert conn.execute("SELECT title FROM telegram_folder_rules").fetchone() == ("Retained",)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_scope_claim_rejects_authorization_transition_and_post_commit_result(tmp_path: Path) -> None:
    from mcp_telegram.auth_scope import TelegramAuthScope
    from mcp_telegram.image_authorization import ScopeCell

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=1,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        enable_runtime_writes(conn)
        repo = SQLiteFolderSnapshotRepository(conn)
        repo.project_observation(FolderRuleObservation((FolderRule(2, "Retained"),), "old", 1), completed_at=1)
        scope = TelegramAuthScope(1, 1, 2, 3)
        cell = ScopeCell()
        cell.publish(scope)

        class TransitionGateway:
            async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
                change = cell.begin_change()
                cell.end_change(change)
                cell.publish(scope)
                return FolderRuleObservation((FolderRule(3, "Stale"),), "stale", started_at)

        refresher = FolderRefresher(
            TransitionGateway(),
            repo,
            writer=inline_writer(conn),
            auth_scope=lambda: scope,
            capture_scope=cell.capture,
            claim_scope=cell.claim,
        )
        assert not (await refresher.refresh(completed_at=10)).accepted
        assert conn.execute("SELECT title FROM telegram_folder_rules").fetchone() == ("Retained",)

        calls = 0

        def claim_then_revoke(token: object) -> bool:
            nonlocal calls
            calls += 1
            return calls == 1

        refresher = FolderRefresher(
            _Gateway(),
            repo,
            writer=inline_writer(conn),
            auth_scope=lambda: scope,
            capture_scope=cell.capture,
            claim_scope=claim_then_revoke,
        )
        result = await refresher.refresh(completed_at=11)
        assert not result.accepted and result.observation_order is None and result.committed_revision is None
    finally:
        conn.close()


@pytest.mark.parametrize("outcome", [FolderAttemptResult.CIRCUIT_OPEN, FolderAttemptResult.UNEXPECTED])
def test_legacy_failure_without_retry_is_immediately_due(tmp_path: Path, outcome: FolderAttemptResult) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        repository = SQLiteFolderSnapshotRepository(conn)
        repository.record_attempt(
            attempted_at=999,
            outcome=outcome,
            next_retry_at=None,
            consecutive_failures=1,
        )
        adapter = FolderProjectionDemandAdapter(
            FolderProjectionWorker(
                FolderRefresher(_Gateway(), repository, writer=inline_writer(conn)),
                repository,
                asyncio.Event(),
                _Policy(),
                writer=inline_writer(conn),
                clock=lambda: 1000.0,
            ),
            account_id=1,
        )
        status = adapter.status(1000.0)
        assert status is not None and status.release_at == 1000.0
    finally:
        conn.close()
