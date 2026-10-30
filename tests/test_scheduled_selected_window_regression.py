import asyncio
import sqlite3
import time

import pytest

from mcp_telegram.scheduled_messages import (
    ScheduledDiscoveryDemandAdapter,
    ScheduledMessageReconciler,
    ScheduledReconciliationPolicy,
    ScheduledRepairDemandAdapter,
)
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
from tests.sqlite_writer_helpers import inline_writer
from tests.test_scheduled_messages import _ScheduledSnapshotClient
from tests.test_scheduled_messages import conn as conn


@pytest.mark.parametrize("repair", [False, True])
async def test_scheduled_runs_selected_revision_outside_bounded_head(
    conn: sqlite3.Connection,
    repair: bool,
) -> None:
    now = int(time.time())
    conn.executemany(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,discovery_due_at,dirty_generation,updated_at) "
        "VALUES (?, ?, 1, ?)",
        [(41, now - 2, now), (42, now - 1, now)],
    )
    conn.executemany("INSERT INTO dialogs(dialog_id,type,hidden) VALUES (?, 'user', 0)", [(41,), (42,)])
    if repair:
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=discovery_due_at")
    conn.commit()
    client = _ScheduledSnapshotClient()
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10, max_dialogs_per_slice=1),
        writer=inline_writer(conn),
    )
    adapter = (ScheduledRepairDemandAdapter if repair else ScheduledDiscoveryDemandAdapter)(worker, account_id=42)
    # Model an already parked head item while another due dialog is selected.
    first = adapter.status(now)
    assert first is not None and first.selected is not None
    second = adapter.status(now, excluded=frozenset({first.selected}))
    assert second is not None and second.selected is not None and second.selected.key.target == "42"
    outcome = await adapter.run_slice(RpcAttemptBudget(limit=16, selected=second.selected))
    assert outcome.kind is SliceOutcomeKind.PROGRESSED
    assert client.input_entity_calls == [42]


@pytest.mark.parametrize("change", ["removed", "future", "generation"])
async def test_selected_revision_must_remain_current_and_due(conn: sqlite3.Connection, change: str) -> None:
    now = int(time.time())
    conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES (42, 'user', 0)")
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,discovery_due_at,dirty_generation,updated_at) "
        "VALUES (42, ?, 1, ?)",
        (now - 1, now),
    )
    conn.commit()
    client = _ScheduledSnapshotClient()
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )
    adapter = ScheduledDiscoveryDemandAdapter(worker, account_id=42)
    selected = adapter.status(now)
    assert selected is not None and selected.selected is not None
    if change == "generation":
        conn.execute("UPDATE scheduled_reconciliation_state SET dirty_generation=2 WHERE dialog_id=42")
    else:
        due = None if change == "removed" else now + 3600
        conn.execute("UPDATE scheduled_reconciliation_state SET discovery_due_at=? WHERE dialog_id=42", (due,))
    conn.commit()
    outcome = await adapter.run_slice(RpcAttemptBudget(limit=16, selected=selected.selected))
    assert outcome.kind is (SliceOutcomeKind.DEFERRED if change == "future" else SliceOutcomeKind.STALE)
    assert client.requests == []
