from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from mcp_telegram.demand_composition import (
    DemandCompositionClient,
    DemandCompositionDependencies,
    SQLiteSelfProfileCadence,
    TelegramDemandShadow,
    build_durable_adapter_map,
)
from mcp_telegram.rpc_admission_observations import DemandEvidenceOutcome
from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import TELEGRAM_DEMAND_CONTRACTS, DemandKind, ExecutionMode


class _Adapter:
    def __init__(self, status: DemandStatus | None = None, *, fail_status: bool = False) -> None:
        self.current_status = status
        self.fail_status = fail_status
        self.status_calls: list[float] = []
        self.run_calls: list[RpcAttemptBudget] = []

    def status(self, now: float) -> DemandStatus | None:
        self.status_calls.append(now)
        if self.fail_status:
            raise RuntimeError("sensitive domain failure")
        return self.current_status

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        self.run_calls.append(budget)


class _Observer:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def observe_demand(self, **event: object) -> None:
        self.events.append(event)


def _shadow_adapters() -> dict[DemandKind, _Adapter]:
    return {
        kind: _Adapter()
        for kind, contract in TELEGRAM_DEMAND_CONTRACTS.items()
        if contract.execution_mode is ExecutionMode.DURABLE
    }


def _dependencies() -> tuple[DemandCompositionDependencies, dict[str, object]]:
    objects: dict[str, object] = {
        "client": MagicMock(),
        "conn": MagicMock(spec=sqlite3.Connection),
        "shutdown": asyncio.Event(),
        "full": MagicMock(),
        "delta": MagicMock(),
        "dialog": MagicMock(),
        "entity": MagicMock(),
        "hydration": MagicMock(),
        "folder": MagicMock(),
        "facts": MagicMock(),
        "fact_policy": MagicMock(),
        "scheduled": MagicMock(),
        "access_policy": MagicMock(),
        "hot_policy": MagicMock(),
        "cold_pacing": MagicMock(),
        "cadence": MagicMock(),
    }

    async def read_receipt_batch() -> object:
        return None

    update_profile: Callable[[object], None] = MagicMock()
    dependencies = DemandCompositionDependencies(
        client=cast(DemandCompositionClient, objects["client"]),
        conn=cast(sqlite3.Connection, objects["conn"]),
        db_path=Path("/state/sync.db"),
        shutdown_event=cast(asyncio.Event, objects["shutdown"]),
        full_sync_worker=cast(object, objects["full"]),  # type: ignore[arg-type]
        delta_sync_worker=cast(object, objects["delta"]),  # type: ignore[arg-type]
        dialog_reconciliation_worker=cast(object, objects["dialog"]),  # type: ignore[arg-type]
        entity_refresh_coordinator=cast(object, objects["entity"]),  # type: ignore[arg-type]
        fact_hydration_worker=cast(object, objects["hydration"]),  # type: ignore[arg-type]
        folder_projection_worker=cast(object, objects["folder"]),  # type: ignore[arg-type]
        message_fact_refresh_deps=cast(object, objects["facts"]),  # type: ignore[arg-type]
        message_fact_refresh_policy=cast(object, objects["fact_policy"]),  # type: ignore[arg-type]
        scheduled_reconciler=cast(object, objects["scheduled"]),  # type: ignore[arg-type]
        access_probe_policy=cast(object, objects["access_policy"]),  # type: ignore[arg-type]
        hot_sweep_policy=cast(object, objects["hot_policy"]),  # type: ignore[arg-type]
        cold_backfill_pacing=cast(object, objects["cold_pacing"]),  # type: ignore[arg-type]
        activity_rpc_timeout_seconds=30.0,
        read_receipt_batch=cast(Callable[[], Awaitable[object]], read_receipt_batch),
        self_profile_cadence=cast(SQLiteSelfProfileCadence, objects["cadence"]),
        update_self_profile=update_profile,
    )
    objects["read_receipt_batch"] = read_receipt_batch
    objects["update_profile"] = update_profile
    return dependencies, objects


def test_adapter_map_is_exact_and_reuses_legacy_owned_objects() -> None:
    dependencies, objects = _dependencies()

    adapters = build_durable_adapter_map(dependencies)

    expected = {
        kind
        for kind, contract in TELEGRAM_DEMAND_CONTRACTS.items()
        if contract.execution_mode is ExecutionMode.DURABLE
    }
    assert set(adapters) == expected
    assert all(getattr(adapter, "demand_kind", None) is kind for kind, adapter in adapters.items())
    assert adapters[DemandKind.FULL_SYNC_PAGE]._worker is objects["full"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.FULL_SYNC_DM_ENROLLMENT]._worker is objects["full"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_GAP_FILL]._worker is objects["delta"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_ACCESS_PROBE]._worker is objects["delta"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DIALOG_LIGHT_RECONCILIATION]._worker is objects["dialog"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DIALOG_FULL_RECONCILIATION]._worker is objects["dialog"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.LIVE_HYDRATION_BATCH]._worker is objects["hydration"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.BACKFILL_HYDRATION_BATCH]._worker is objects["hydration"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.FOLDER_SNAPSHOT]._worker is objects["folder"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.SCHEDULED_REPAIR]._reconciler is objects["scheduled"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.SCHEDULED_DISCOVERY]._reconciler is objects["scheduled"]  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        adapters[DemandKind.SCHEDULED_REPAIR] = adapters[DemandKind.SCHEDULED_DISCOVERY]  # type: ignore[index]


def test_shadow_records_transitions_and_never_executes_adapters() -> None:
    adapters = _shadow_adapters()
    adapters[DemandKind.SCHEDULED_REPAIR].current_status = DemandStatus(
        release_at=50.0,
        freshness_deadline=75.0,
    )
    adapters[DemandKind.SCHEDULED_DISCOVERY].current_status = DemandStatus(release_at=150.0)
    observer = _Observer()
    shadow = TelegramDemandShadow(
        adapters,
        asyncio.Event(),
        observer=observer,
        clock=lambda: 100.0,
    )

    assert shadow.coordinator.ready_kinds == (DemandKind.SCHEDULED_REPAIR,)
    assert shadow.coordinator.next_release_at == 150.0
    assert shadow._next_scan_delay(100.0) == 50.0
    assert shadow.offer(DemandKind.SCHEDULED_DISCOVERY) is True
    assert shadow.offer(DemandKind.SCHEDULED_DISCOVERY) is False

    adapters[DemandKind.SCHEDULED_REPAIR].current_status = None
    adapters[DemandKind.SCHEDULED_DISCOVERY].current_status = DemandStatus(release_at=100.0)
    shadow.after_cycle_scan()

    outcomes = [event["outcome"] for event in observer.events]
    assert DemandEvidenceOutcome.READY in outcomes
    assert DemandEvidenceOutcome.OFFERED in outcomes
    assert DemandEvidenceOutcome.COALESCED_WAKEUP in outcomes
    assert DemandEvidenceOutcome.LOCALLY_SATISFIED in outcomes
    assert all(not adapter.run_calls for adapter in adapters.values())


def test_shadow_status_failures_are_bounded_and_do_not_block_scans() -> None:
    adapters = _shadow_adapters()
    adapters[DemandKind.SCHEDULED_REPAIR].fail_status = True
    adapters[DemandKind.SCHEDULED_DISCOVERY].current_status = DemandStatus(release_at=0.0)
    observer = _Observer()

    shadow = TelegramDemandShadow(adapters, asyncio.Event(), observer=observer, clock=lambda: 100.0)

    assert shadow.coordinator.ready_kinds == (DemandKind.SCHEDULED_DISCOVERY,)
    assert any(
        event["outcome"] is DemandEvidenceOutcome.FAILED
        and event["demand_kind"] is DemandKind.SCHEDULED_REPAIR
        and event["reason"] == "status_error"
        for event in observer.events
    )
    assert all("sensitive domain failure" not in str(value) for event in observer.events for value in event.values())


@pytest.mark.asyncio
async def test_shadow_timer_exits_cleanly_without_executing() -> None:
    adapters = _shadow_adapters()
    shutdown = asyncio.Event()
    shadow = TelegramDemandShadow(adapters, shutdown, safety_scan_seconds=0.01)

    task = asyncio.create_task(shadow.run())
    await asyncio.sleep(0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert all(not adapter.run_calls for adapter in adapters.values())


def test_self_profile_cadence_is_persisted_in_daemon_state() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    cadence = SQLiteSelfProfileCadence(conn, 60.0)

    assert cadence.status(100.0).release_at == 0.0
    cadence.mark_refreshed(100.0)
    assert cadence.status(110.0).release_at == 160.0
    conn.close()
