"""Durable demand adapters for history, delta, and dialog synchronization."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers import MockTotalList, build_mock_message
from mcp_telegram.delta_sync import (
    AccessProbePolicy,
    DeltaAccessProbeDemandAdapter,
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
    DurableAccessRecoveryStateRequiredError,
    _DeltaSyncClient,
)
from mcp_telegram.dialog_sync import (
    DialogBootstrapDemandAdapter,
    DialogFullReconciliationDemandAdapter,
    DialogLightReconciliationDemandAdapter,
    DialogReconciliationWorker,
    DurableDialogSweepStateRequiredError,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncWorker
from mcp_telegram.telegram_demand import AcquisitionKind, RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcScope, current_rpc_scope
from tests.history_enrollment_helpers import seed_full_history_enrollment


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    return path


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = _open_sync_db(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _seed_history_dialog(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    status: str,
    last_synced_at: int | None = None,
    last_delta_checked_at: int | None = None,
    refresh_requested_at: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, last_synced_at, last_delta_checked_at, delta_refresh_requested_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (dialog_id, status, last_synced_at, last_delta_checked_at, refresh_requested_at),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=True)
    conn.commit()


def _delta_policy() -> AccessProbePolicy:
    return AccessProbePolicy(
        interval_seconds=86_400.0,
        max_dialogs_per_cycle=1,
        cooldown_seconds=100,
        probe_pause_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_full_sync_adapter_status_is_read_only_and_slice_has_precise_scope(conn: sqlite3.Connection) -> None:
    dialog_id = 101
    _seed_history_dialog(conn, dialog_id, status="syncing")
    observed_scopes: list[TelegramRpcScope] = []

    async def get_messages(**_kwargs: object) -> MockTotalList:
        observed_scopes.append(current_rpc_scope())
        return MockTotalList([], total=0)

    client = SimpleNamespace(get_messages=get_messages)
    worker = FullSyncWorker(client, conn, asyncio.Event())
    adapter = FullSyncDemandAdapter(worker)
    changes_before = conn.total_changes

    assert adapter.status(500.0) is not None
    assert conn.total_changes == changes_before

    budget = RpcAttemptBudget(limit=1)
    await adapter.run_slice(budget)
    scope = observed_scopes[0]
    assert scope.demand_kind is DemandKind.FULL_SYNC_PAGE
    assert scope.acquisition_kind is AcquisitionKind.MESSAGE_HISTORY_PAGE
    assert scope.attempt_budget is budget
    assert adapter.status(500.0) is None


def test_delta_gap_status_uses_refresh_or_recency_boundary_without_writes(conn: sqlite3.Connection) -> None:
    _seed_history_dialog(conn, 201, status="synced", last_delta_checked_at=100)
    worker = DeltaSyncWorker(cast(_DeltaSyncClient, SimpleNamespace()), conn, asyncio.Event())
    adapter = DeltaGapFillDemandAdapter(worker)
    changes_before = conn.total_changes

    assert adapter.status(50.0).release_at == 3700.0  # type: ignore[union-attr]
    assert conn.total_changes == changes_before

    conn.execute("UPDATE synced_dialogs SET delta_refresh_requested_at = 75 WHERE dialog_id = 201")
    conn.commit()
    assert adapter.status(50.0).release_at == 75.0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_delta_gap_slice_commits_one_page_and_keeps_durable_continuation(
    conn: sqlite3.Connection,
) -> None:
    dialog_id = 202
    _seed_history_dialog(conn, dialog_id, status="synced", refresh_requested_at=1)
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, 100, 1, 'baseline')",
        (dialog_id,),
    )
    conn.commit()
    pages = [[build_mock_message(id=message_id, text=str(message_id)) for message_id in range(101, 201)], []]
    observed_scopes: list[TelegramRpcScope] = []
    observed_limits: list[object] = []

    async def iter_messages(**kwargs: object) -> AsyncIterator[object]:
        observed_scopes.append(current_rpc_scope())
        observed_limits.append(kwargs["limit"])
        for message in pages.pop(0):
            yield message

    worker = DeltaSyncWorker(cast(_DeltaSyncClient, SimpleNamespace(iter_messages=iter_messages)), conn, asyncio.Event())
    adapter = DeltaGapFillDemandAdapter(worker)
    budget = RpcAttemptBudget(limit=1)

    await adapter.run_slice(budget)

    assert conn.execute("SELECT MAX(message_id) FROM messages WHERE dialog_id = ?", (dialog_id,)).fetchone() == (200,)
    assert conn.execute(
        "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()[0] is not None
    assert adapter.status(10_000.0).is_ready(10_000.0)  # type: ignore[union-attr]
    assert observed_limits == [100]
    assert observed_scopes[0].demand_kind is DemandKind.DELTA_GAP_FILL
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.MESSAGE_HISTORY_PAGE
    assert observed_scopes[0].attempt_budget is budget

    await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute(
        "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id = ?",
        (dialog_id,),
    ).fetchone()[0] is None


@pytest.mark.asyncio
async def test_access_probe_adapter_restores_non_enrolled_peer_with_precise_scope(conn: sqlite3.Connection) -> None:
    dialog_id = 301
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1)",
        (dialog_id,),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=False)
    conn.commit()
    observed_scopes: list[TelegramRpcScope] = []

    async def get_messages(**_kwargs: object) -> MockTotalList:
        observed_scopes.append(current_rpc_scope())
        return MockTotalList([], total=0)

    client = cast(_DeltaSyncClient, SimpleNamespace(get_messages=get_messages))
    worker = DeltaSyncWorker(client, conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy())
    changes_before = conn.total_changes

    assert adapter.status(500.0).release_at == 101.0  # type: ignore[union-attr]
    assert conn.total_changes == changes_before
    budget = RpcAttemptBudget(limit=1)
    await adapter.run_slice(budget)

    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone() != (
        "access_lost",
    )
    assert observed_scopes[0].demand_kind is DemandKind.DELTA_ACCESS_PROBE
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.MESSAGE_LOOKUP
    assert observed_scopes[0].attempt_budget is budget


@pytest.mark.asyncio
async def test_access_probe_adapter_refuses_enrolled_peer_before_telegram(conn: sqlite3.Connection) -> None:
    dialog_id = 302
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1)",
        (dialog_id,),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=True)
    conn.commit()
    get_messages = AsyncMock()
    worker = DeltaSyncWorker(cast(_DeltaSyncClient, SimpleNamespace(get_messages=get_messages)), conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy())

    with pytest.raises(DurableAccessRecoveryStateRequiredError, match="durable probe-success-to-gap-fill handoff"):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_dialog_bootstrap_adapter_status_and_slice_use_committed_state(
    conn: sqlite3.Connection,
    db_path: Path,
) -> None:
    observed_scopes: list[TelegramRpcScope] = []

    async def iter_dialogs(**_kwargs: object) -> AsyncIterator[object]:
        observed_scopes.append(current_rpc_scope())
        return
        yield  # pragma: no cover

    adapter = DialogBootstrapDemandAdapter(
        SimpleNamespace(iter_dialogs=iter_dialogs),
        conn,
        db_path,
        asyncio.Event(),
    )
    changes_before = conn.total_changes
    assert adapter.status(10.0) is not None
    assert conn.total_changes == changes_before

    budget = RpcAttemptBudget(limit=32)
    await adapter.run_slice(budget)

    assert adapter.status(10.0) is None
    assert observed_scopes[0].demand_kind is DemandKind.DIALOG_BOOTSTRAP
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.DIALOG_TRAVERSAL
    assert observed_scopes[0].attempt_budget is budget


@pytest.mark.asyncio
async def test_dialog_light_adapter_clears_one_durable_dirty_flag(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO dialogs (dialog_id, name, type, hidden, needs_refresh, snapshot_at) "
        "VALUES (401, 'old', 'user', 0, 1, 1)"
    )
    conn.commit()
    observed_scopes: list[TelegramRpcScope] = []

    async def get_entity(_dialog_id: int) -> object:
        observed_scopes.append(current_rpc_scope())
        return SimpleNamespace(
            id=401,
            first_name="new",
            last_name=None,
            title=None,
            username=None,
            access_hash=1,
            bot=False,
            broadcast=False,
            date=None,
            forum=False,
        )

    worker = DialogReconciliationWorker(SimpleNamespace(get_entity=get_entity), conn, asyncio.Event())
    adapter = DialogLightReconciliationDemandAdapter(worker)
    changes_before = conn.total_changes
    assert adapter.status(10.0) is not None
    assert conn.total_changes == changes_before
    budget = RpcAttemptBudget(limit=8)

    await adapter.run_slice(budget)

    assert adapter.status(10.0) is None
    assert observed_scopes[0].demand_kind is DemandKind.DIALOG_LIGHT_RECONCILIATION
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.ENTITY_LOOKUP
    assert observed_scopes[0].attempt_budget is budget


@pytest.mark.asyncio
async def test_dialog_full_adapter_reports_due_state_but_refuses_unsafe_slice(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO daemon_state (key, value) VALUES ('dialog_reconciliation_last_full_at', '100')"
    )
    conn.commit()
    iter_dialogs = MagicMock()
    worker = DialogReconciliationWorker(SimpleNamespace(iter_dialogs=iter_dialogs), conn, asyncio.Event())
    adapter = DialogFullReconciliationDemandAdapter(worker, interval_seconds=50.0)
    changes_before = conn.total_changes

    status = adapter.status(200.0)
    assert status.release_at == 150.0
    assert status.freshness_deadline == 150.0
    assert conn.total_changes == changes_before
    with pytest.raises(DurableDialogSweepStateRequiredError, match="generation, cursor, and seen-membership"):
        await adapter.run_slice(RpcAttemptBudget(limit=32))
    iter_dialogs.assert_not_called()
    assert conn.total_changes == changes_before
