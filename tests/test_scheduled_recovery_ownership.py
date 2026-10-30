# pyright: reportAny=false

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.scheduled_messages import (
    ScheduledDiscoveryDemandAdapter,
    ScheduledMessageReconciler,
    ScheduledReconciliationPolicy,
    ScheduledRepairDemandAdapter,
    upsert_scheduled_message,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import DurableDemandAdapter, RpcAttemptBudget, RpcRetryAuthorization
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    current_rpc_scope,
)
from tests.sqlite_writer_helpers import inline_writer
from tests.test_scheduled_messages import _message, _ScheduledSnapshotClient
from tests.test_telegram_demand_coordinator import _adapters

_METHOD = "messages.GetScheduledHistoryRequest"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    db = _open_sync_db(path)
    with write_transaction(db):
        db.execute("UPDATE scheduled_sync_state SET account_id=42,last_snapshot_at=77,last_error='prior_failure'")
        upsert_scheduled_message(db, 42, _message(11), now=100)
        db.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=100,discovery_due_at=100")
    try:
        yield db
    finally:
        db.close()


def _coordinator(
    conn: sqlite3.Connection,
    client: _ScheduledSnapshotClient,
    now: list[float],
) -> tuple[TelegramDemandCoordinator, ScheduledMessageReconciler]:
    reconciler = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(10),
        writer=inline_writer(conn),
    )
    adapters = cast(dict[DemandKind, DurableDemandAdapter], cast(object, _adapters()))
    adapters[DemandKind.SCHEDULED_REPAIR] = ScheduledRepairDemandAdapter(reconciler, account_id=42)
    adapters[DemandKind.SCHEDULED_DISCOVERY] = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42)
    return TelegramDemandCoordinator(adapters, clock=lambda: now[0], monotonic_clock=lambda: now[0]), reconciler


async def test_first_use_hint_prioritizes_same_discovery_revision_without_writes(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=NULL,discovery_due_at=100")
        conn.executemany(
            "INSERT INTO scheduled_reconciliation_state(dialog_id,discovery_due_at,updated_at,coverage_state) "
            "VALUES(?,100,100,'unknown')",
            ((number,) for number in range(1000, 1850)),
        )
    coordinator, reconciler = _coordinator(conn, _ScheduledSnapshotClient(), [100.0])
    adapter = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42)
    target = 1849
    before = conn.execute("SELECT * FROM scheduled_reconciliation_state WHERE dialog_id=?", (target,)).fetchone()
    assert adapter.status(100).selected.key.target != str(target)  # type: ignore[union-attr]
    assert await reconciler.prioritize_baseline(42, target)
    selected = adapter.status(100)
    assert selected is not None and selected.selected is not None and selected.selected.key.target == str(target)
    assert await reconciler.prioritize_baseline(42, target)
    assert adapter.status(100) == selected
    assert (
        conn.execute("SELECT * FROM scheduled_reconciliation_state WHERE dialog_id=?", (target,)).fetchone() == before
    )
    _, restarted = _coordinator(conn, _ScheduledSnapshotClient(), [100.0])
    restarted_adapter = ScheduledDiscoveryDemandAdapter(restarted, account_id=42)
    assert restarted._priority_dialog_id is None and restarted_adapter.status(100) is not None
    assert (
        conn.execute("SELECT * FROM scheduled_reconciliation_state WHERE dialog_id=?", (target,)).fetchone() == before
    )
    coordinator.offer(DemandKind.SCHEDULED_DISCOVERY)
    coordinator.scan()
    assert coordinator.statuses[DemandKind.SCHEDULED_DISCOVERY].selected == selected.selected


async def test_first_use_hint_keeps_excluded_revision_parked_and_respects_account_floor(
    conn: sqlite3.Connection,
) -> None:
    with write_transaction(conn):
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=NULL,discovery_due_at=100")
        conn.execute(
            "INSERT INTO scheduled_reconciliation_state(dialog_id,discovery_due_at,updated_at,coverage_state) "
            "VALUES(77,100,100,'unknown')"
        )
    _, reconciler = _coordinator(conn, _ScheduledSnapshotClient(), [100.0])
    adapter = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42)
    assert await reconciler.prioritize_baseline(42, 77)
    selected = adapter.status(100)
    assert selected is not None and selected.selected is not None and selected.selected.key.target == "77"
    other = adapter.status(100, excluded=frozenset({selected.selected}))
    assert other is not None and other.selected is not None and other.selected.key.target != "77"
    with write_transaction(conn):
        conn.execute("UPDATE scheduled_sync_state SET next_retry_at=200 WHERE key='account'")
    assert adapter.status(100).release_at == 200  # type: ignore[union-attr]
    assert await reconciler.prioritize_baseline(42, 42)
    assert adapter.status(100).release_at == 200  # type: ignore[union-attr]


async def test_first_use_hint_does_not_invalidate_inflight_discovery_snapshot(conn: sqlite3.Connection) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingClient(_ScheduledSnapshotClient):
        async def __call__(self, request: object, **kwargs: object) -> object:
            entered.set()
            await release.wait()
            return await super().__call__(request, **kwargs)

    with write_transaction(conn):
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=NULL,discovery_due_at=100")
        conn.execute(
            "UPDATE scheduled_sync_state SET directory_generation=(SELECT generation FROM dialog_directory_publication)"
        )
    client = BlockingClient({42: []})
    coordinator, reconciler = _coordinator(conn, client, [100.0])
    selected = coordinator.statuses[DemandKind.SCHEDULED_DISCOVERY].selected
    assert selected is not None and selected.key.target == "42"
    before = conn.execute("SELECT * FROM scheduled_reconciliation_state WHERE dialog_id=42").fetchone()
    task = asyncio.create_task(coordinator._execute_slice(DemandKind.SCHEDULED_DISCOVERY))
    await entered.wait()
    assert await reconciler.prioritize_baseline(42, 42)
    assert conn.execute("SELECT * FROM scheduled_reconciliation_state WHERE dialog_id=42").fetchone() == before
    release.set()
    await task
    assert len(client.requests) == 1
    assert conn.execute("SELECT coverage_state FROM scheduled_reconciliation_state WHERE dialog_id=42").fetchone() == (
        "complete",
    )
    ScheduledDiscoveryDemandAdapter(reconciler, account_id=42).status(100)
    assert reconciler._priority_dialog_id is None


async def test_first_use_hint_never_restarts_a_parked_discovery_budget(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=NULL,discovery_due_at=100")
        conn.execute(
            "INSERT INTO scheduled_reconciliation_state(dialog_id,discovery_due_at,updated_at,coverage_state) "
            "VALUES(77,100,100,'unknown')"
        )
        conn.execute(
            "UPDATE scheduled_sync_state SET directory_generation=(SELECT generation FROM dialog_directory_publication)"
        )
    client = _RecoveryClient()
    coordinator, reconciler = _coordinator(conn, client, [100.0])
    assert await reconciler.prioritize_baseline(42, 42)
    coordinator.scan()
    selected = coordinator.statuses[DemandKind.SCHEDULED_DISCOVERY].selected
    assert selected is not None and selected.key.target == "42"
    await coordinator._execute_slice(DemandKind.SCHEDULED_DISCOVERY)
    assert selected in coordinator._excluded
    budget = coordinator._invocations[selected].budget
    assert budget.attempts == 1
    assert await reconciler.prioritize_baseline(42, 42)
    coordinator._active_kind = None
    coordinator.offer(DemandKind.SCHEDULED_DISCOVERY)
    coordinator.scan()
    assert coordinator._invocations[selected].budget is budget and budget.attempts == 1
    assert selected in coordinator._excluded
    assert coordinator.statuses[DemandKind.SCHEDULED_DISCOVERY].selected != selected


@pytest.mark.parametrize("mode", ["wrong_account", "unbound", "missing", "complete", "inaccessible", "unsupported"])
async def test_first_use_hint_rejects_unavailable_or_unowned_target(conn: sqlite3.Connection, mode: str) -> None:
    with write_transaction(conn):
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=NULL,discovery_due_at=100")
        if mode == "unbound":
            conn.execute("UPDATE scheduled_sync_state SET account_id=NULL WHERE key='account'")
        if mode in {"complete", "inaccessible", "unsupported"}:
            conn.execute("UPDATE scheduled_reconciliation_state SET coverage_state=?", (mode,))
    _, reconciler = _coordinator(conn, _ScheduledSnapshotClient(), [100.0])
    account = 99 if mode == "wrong_account" else 42
    dialog = 99999 if mode == "missing" else 42
    assert not await reconciler.prioritize_baseline(account, dialog)
    assert reconciler._priority_dialog_id is None


class _RecoveryClient(_ScheduledSnapshotClient):
    def __init__(self) -> None:
        super().__init__({42: []})
        self.malformed = True
        self.budgets: list[RpcAttemptBudget] = []

    async def __call__(self, request: object, **kwargs: object) -> object:
        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        self.budgets.append(budget)
        budget.last_method = _METHOD
        budget.debit()
        if self.malformed:
            return types.messages.MessagesNotModified(count=0)
        return await super().__call__(request, **kwargs)


async def test_failed_revision_is_parked_without_timer_or_sibling_budget_and_new_event_recovers(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr("mcp_telegram.scheduled_messages.time.time", lambda: now[0])
    client = _RecoveryClient()
    coordinator, reconciler = _coordinator(conn, client, now)
    assert coordinator.ready_kinds == (DemandKind.SCHEDULED_REPAIR,)
    selected = coordinator.statuses[DemandKind.SCHEDULED_REPAIR].selected
    assert selected is not None
    await coordinator._execute_slice(DemandKind.SCHEDULED_REPAIR)
    assert selected in coordinator._excluded
    assert len(client.budgets) == 1 and client.budgets[0].attempts == 1
    assert conn.execute(
        "SELECT repair_due_at,discovery_due_at,coverage_reason FROM scheduled_reconciliation_state"
    ).fetchone() == (100, 100, "rpc_error")
    assert conn.execute("SELECT last_snapshot_at,last_error FROM scheduled_sync_state").fetchone() == (
        77,
        "prior_failure",
    )
    assert conn.execute("SELECT message_id,text FROM scheduled_messages").fetchone() == (11, "draft")
    for timestamp in (101.0, 401.0, 86_500.0):
        now[0] = timestamp
        coordinator._active_kind = None
        coordinator.scan()
        assert coordinator.ready_kinds == ()
        assert len(client.budgets) == 1
    # A genuinely accepted Telegram event changes the canonical revision.
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, "new event"), now=200)
    client.malformed = False
    coordinator.scan()
    assert coordinator.ready_kinds == (DemandKind.SCHEDULED_REPAIR,)
    await coordinator._execute_slice(DemandKind.SCHEDULED_REPAIR)
    assert len(client.budgets) == 2 and client.budgets[1] is not client.budgets[0]
    assert conn.execute(
        "SELECT repair_due_at,discovery_due_at,coverage_state FROM scheduled_reconciliation_state"
    ).fetchone() == (None, None, "complete")
    assert conn.execute("SELECT last_snapshot_at,last_error FROM scheduled_sync_state").fetchone() == (86_500, None)
    assert reconciler._due_rows(90_000) == []


@pytest.mark.parametrize("signal", ["admission", "flood", "method_recovery"])
async def test_existing_gate_signal_releases_same_exact_revision_with_same_budget(
    conn: sqlite3.Connection,
    signal: str,
) -> None:
    now = [100.0]
    hold = TelegramRpcAdmissionDeferred(30, method_key=_METHOD, eligible_at=130, reason="method")
    hint: list[TelegramRpcThrottled | TelegramRpcMethodRecoveryPending | None] = [None]

    class Client(_RecoveryClient):
        async def __call__(self, request: object, **kwargs: object) -> object:
            if not self.budgets:
                budget = current_rpc_scope().attempt_budget
                assert budget is not None
                self.budgets.append(budget)
                budget.last_method = _METHOD
                if signal == "method_recovery":
                    recovery = TelegramRpcMethodRecoveryPending(_METHOD, "generation")
                    hint[0] = recovery
                    raise recovery
                hint[0] = hold
                if signal == "flood":
                    budget.debit()
                    raise TelegramRpcThrottled(30)
                raise hold
            self.malformed = False
            return await super().__call__(request, **kwargs)

    client = Client()
    coordinator, _ = _coordinator(conn, client, now)
    coordinator._rpc_eligibility = lambda _method: hint[0]
    coordinator._prepare_durable_retry = lambda _budget, _method: (
        RpcRetryAuthorization(145) if hint[0] is None and now[0] >= 130 else None
    )
    selected = coordinator.statuses[DemandKind.SCHEDULED_REPAIR].selected
    assert selected is not None
    await coordinator._execute_slice(DemandKind.SCHEDULED_REPAIR)
    assert selected in coordinator._eligibility_waits
    budget = client.budgets[0]
    assert budget.attempts == (1 if signal == "flood" else 0)
    now[0] = 129
    coordinator._active_kind = None
    coordinator.scan()
    assert coordinator.ready_kinds == ()
    assert conn.execute("SELECT repair_due_at,discovery_due_at FROM scheduled_reconciliation_state").fetchone() == (
        100,
        100,
    )
    hint[0] = None
    now[0] = 130
    coordinator.eligibility_changed(_METHOD)
    coordinator.scan()
    assert coordinator.ready_kinds == (DemandKind.SCHEDULED_REPAIR,)
    assert coordinator.statuses[DemandKind.SCHEDULED_REPAIR].selected == selected
    await coordinator._execute_slice(DemandKind.SCHEDULED_REPAIR)
    assert client.budgets == [budget, budget] and budget.attempts == (2 if signal == "flood" else 1)
    assert conn.execute(
        "SELECT repair_due_at,discovery_due_at,coverage_state FROM scheduled_reconciliation_state"
    ).fetchone() == (None, None, "complete")


async def test_future_repair_blocks_direct_and_selected_discovery_of_same_obligation(conn: sqlite3.Connection) -> None:
    from mcp_telegram.telegram_demand_progress import SliceOutcomeKind, WorkKey, WorkRevision

    with write_transaction(conn):
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=300,discovery_due_at=100")
    client = _RecoveryClient()
    _, reconciler = _coordinator(conn, client, [100.0])
    discovery = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42)
    assert discovery.status(100) is None
    assert reconciler._due_rows(100) == []
    assert reconciler._due_rows(100, DemandKind.SCHEDULED_DISCOVERY) == []
    assert await reconciler.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY) == 0
    generation, snapshot_order = conn.execute(
        "SELECT dirty_generation,COALESCE(snapshot_observation_order,0) FROM scheduled_reconciliation_state"
    ).fetchone()
    selected = WorkRevision(
        WorkKey(DemandKind.SCHEDULED_DISCOVERY, 42, "42"), f"{generation}|discovery|{snapshot_order}"
    )
    result = await discovery.run_slice(RpcAttemptBudget(16, selected=selected))
    assert result.kind is SliceOutcomeKind.STALE and client.budgets == []
    assert reconciler._due_rows(300) == [(42, generation, False)]
