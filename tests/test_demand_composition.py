from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from mcp_telegram.activity_cold_backfill import (
    ColdBackfillHistoryPacing,
    ColdBackfillPacing,
    ColdPeerPageDemandAdapter,
)
from mcp_telegram.activity_hot_sweep import HotActivityDemandAdapter
from mcp_telegram.activity_sync import ArchiveBackfillDemandAdapter, ArchiveIncrementalDemandAdapter
from mcp_telegram.daemon import SQLiteSelfProfileCadence
from mcp_telegram.delta_sync import DeltaAccessProbeDemandAdapter, DeltaGapFillDemandAdapter
from mcp_telegram.demand_composition import (
    DemandCompositionClient,
    DemandCompositionDependencies,
    build_durable_adapter_map,
    build_durable_coordinator,
)
from mcp_telegram.dialog_sync import (
    DialogBootstrapDemandAdapter,
    DialogFullReconciliationDemandAdapter,
    DialogLightReconciliationDemandAdapter,
)
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, EntityRefreshCoordinator
from mcp_telegram.fact_hydration import FactHydrationDemandAdapter
from mcp_telegram.folders.worker import FolderProjectionDemandAdapter, FolderProjectionWorker
from mcp_telegram.message_fact_refresh import (
    MessageFactRefreshDemandAdapter,
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    ReadReceiptDemandAdapter,
)
from mcp_telegram.scheduled_messages import ScheduledDiscoveryDemandAdapter, ScheduledRepairDemandAdapter
from mcp_telegram.self_profile_maintenance import SelfProfileCadenceState, SelfProfileMaintenanceDemandAdapter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncDmEnrollmentDemandAdapter
from mcp_telegram.telegram_rpc_consumers import (
    DemandKind,
    demand_freshness_seconds,
)


def _dependencies(tmp_path: Path) -> tuple[DemandCompositionDependencies, dict[str, object]]:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    folder_repository = MagicMock()
    folder_repository.read_consecutive_failures.return_value = 0
    folder_repository.read_last_outcome.return_value = None
    folder_repository.read_next_retry_at.return_value = None
    folder_repository.read_last_success_at.return_value = None
    folder_repository.read_staging.return_value = None
    folder_policy = SimpleNamespace(
        refresh_interval_seconds=60.0,
        jitter_ratio=0.0,
        retry_delays_seconds=(1,),
        retry_cap_seconds=60,
        warning_failure_threshold=3,
        stale_threshold_seconds=300,
    )
    folder_worker = FolderProjectionWorker(MagicMock(), folder_repository, asyncio.Event(), folder_policy)
    message_fact_policy = MessageFactRefreshPolicy(
        interval_seconds=60.0,
        reaction_ttl_seconds=60,
        read_at_ttl_seconds=60,
        reaction_max_messages_per_cycle=1,
        read_at_max_messages_per_cycle=1,
        pause_seconds=0.0,
    )
    objects: dict[str, object] = {
        "client": MagicMock(),
        "conn": conn,
        "shutdown": asyncio.Event(),
        "full": MagicMock(),
        "delta": MagicMock(),
        "dm_gap_scanner": MagicMock(),
        "dialog": MagicMock(),
        "entity": EntityRefreshCoordinator(),
        "hydration": MagicMock(),
        "folder": folder_worker,
        "facts": MessageFactRefreshDeps(conn, MagicMock(), MagicMock()),
        "fact_policy": message_fact_policy,
        "scheduled": MagicMock(),
        "access_policy": MagicMock(),
        "hot_policy": SimpleNamespace(loop_interval_seconds=60.0),
        "cold_pacing": ColdBackfillPacing(
            idle_s=300.0,
            history=ColdBackfillHistoryPacing(batch_s=1.0, enroll_s=60.0, access_retry_s=60.0),
        ),
        "cadence": SQLiteSelfProfileCadence(conn, 60.0),
    }

    async def read_receipt_batch() -> object:
        return None

    update_profile: Callable[[object], None] = MagicMock()
    dependencies = DemandCompositionDependencies(
        client=cast(DemandCompositionClient, objects["client"]),
        conn=cast(sqlite3.Connection, objects["conn"]),
        db_path=db_path,
        shutdown_event=cast(asyncio.Event, objects["shutdown"]),
        full_sync_worker=cast(object, objects["full"]),  # type: ignore[arg-type]
        delta_sync_worker=cast(object, objects["delta"]),  # type: ignore[arg-type]
        dm_gap_scanner=cast(object, objects["dm_gap_scanner"]),  # type: ignore[arg-type]
        dialog_reconciliation_worker=cast(object, objects["dialog"]),  # type: ignore[arg-type]
        entity_refresh_coordinator=cast(EntityRefreshCoordinator, objects["entity"]),
        fact_hydration_worker=cast(object, objects["hydration"]),  # type: ignore[arg-type]
        folder_projection_worker=cast(FolderProjectionWorker, objects["folder"]),
        message_fact_refresh_deps=cast(MessageFactRefreshDeps, objects["facts"]),
        message_fact_refresh_policy=cast(MessageFactRefreshPolicy, objects["fact_policy"]),
        scheduled_reconciler=cast(object, objects["scheduled"]),  # type: ignore[arg-type]
        access_probe_policy=cast(object, objects["access_policy"]),  # type: ignore[arg-type]
        hot_sweep_policy=cast(object, objects["hot_policy"]),  # type: ignore[arg-type]
        cold_backfill_pacing=cast(object, objects["cold_pacing"]),  # type: ignore[arg-type]
        activity_rpc_timeout_seconds=30.0,
        read_receipt_batch=cast(Callable[[], Awaitable[object]], read_receipt_batch),
        self_profile_cadence=cast(SelfProfileCadenceState, objects["cadence"]),
        update_self_profile=update_profile,
    )
    objects["read_receipt_batch"] = read_receipt_batch
    objects["update_profile"] = update_profile
    return dependencies, objects


@pytest.fixture()
def composition_dependencies(
    tmp_path: Path,
) -> Generator[tuple[DemandCompositionDependencies, dict[str, object]]]:
    dependencies, objects = _dependencies(tmp_path)
    try:
        yield dependencies, objects
    finally:
        cast(sqlite3.Connection, objects["conn"]).close()


def test_adapter_map_is_exact_against_literal_20_kind_class_map(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, objects = composition_dependencies

    adapters = build_durable_adapter_map(dependencies)

    expected = {
        DemandKind.ENTITY_PROFILE_REFRESH: EntityProfileDemandAdapter,
        DemandKind.DELTA_GAP_FILL: DeltaGapFillDemandAdapter,
        DemandKind.DELTA_ACCESS_PROBE: DeltaAccessProbeDemandAdapter,
        DemandKind.HOT_ACTIVITY_PAGE: HotActivityDemandAdapter,
        DemandKind.LIVE_HYDRATION_BATCH: FactHydrationDemandAdapter,
        DemandKind.FULL_SYNC_DM_ENROLLMENT: FullSyncDmEnrollmentDemandAdapter,
        DemandKind.FULL_SYNC_PAGE: FullSyncDemandAdapter,
        DemandKind.DIALOG_BOOTSTRAP: DialogBootstrapDemandAdapter,
        DemandKind.DIALOG_LIGHT_RECONCILIATION: DialogLightReconciliationDemandAdapter,
        DemandKind.DIALOG_FULL_RECONCILIATION: DialogFullReconciliationDemandAdapter,
        DemandKind.ARCHIVE_BACKFILL: ArchiveBackfillDemandAdapter,
        DemandKind.ARCHIVE_INCREMENTAL: ArchiveIncrementalDemandAdapter,
        DemandKind.COLD_PEER_PAGE: ColdPeerPageDemandAdapter,
        DemandKind.BACKFILL_HYDRATION_BATCH: FactHydrationDemandAdapter,
        DemandKind.FOLDER_SNAPSHOT: FolderProjectionDemandAdapter,
        DemandKind.MESSAGE_FACT_REFRESH: MessageFactRefreshDemandAdapter,
        DemandKind.READ_RECEIPT_BATCH: ReadReceiptDemandAdapter,
        DemandKind.SCHEDULED_REPAIR: ScheduledRepairDemandAdapter,
        DemandKind.SCHEDULED_DISCOVERY: ScheduledDiscoveryDemandAdapter,
        DemandKind.SELF_PROFILE_MAINTENANCE: SelfProfileMaintenanceDemandAdapter,
    }
    assert len(expected) == 20
    assert set(adapters) == set(expected)
    assert {kind: type(adapter) for kind, adapter in adapters.items()} == expected
    assert all(getattr(adapter, "demand_kind", None) is kind for kind, adapter in adapters.items())
    assert adapters[DemandKind.FULL_SYNC_PAGE]._worker is objects["full"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.FULL_SYNC_DM_ENROLLMENT]._worker is objects["full"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_GAP_FILL]._worker is objects["delta"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_GAP_FILL]._dm_gap_scanner is objects["dm_gap_scanner"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_ACCESS_PROBE]._worker is objects["delta"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DIALOG_LIGHT_RECONCILIATION]._worker is objects["dialog"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DIALOG_FULL_RECONCILIATION]._worker is objects["dialog"]  # type: ignore[attr-defined]
    assert (
        adapters[DemandKind.DIALOG_FULL_RECONCILIATION]._interval_seconds  # type: ignore[attr-defined]
        == demand_freshness_seconds(DemandKind.DIALOG_FULL_RECONCILIATION)
    )
    assert (
        adapters[DemandKind.ARCHIVE_INCREMENTAL].interval_s  # type: ignore[attr-defined]
        == demand_freshness_seconds(DemandKind.ARCHIVE_INCREMENTAL)
    )
    assert adapters[DemandKind.LIVE_HYDRATION_BATCH]._worker is objects["hydration"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.BACKFILL_HYDRATION_BATCH]._worker is objects["hydration"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.FOLDER_SNAPSHOT]._worker is objects["folder"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.SCHEDULED_REPAIR]._reconciler is objects["scheduled"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.SCHEDULED_DISCOVERY]._reconciler is objects["scheduled"]  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        adapters[DemandKind.SCHEDULED_REPAIR] = adapters[DemandKind.SCHEDULED_DISCOVERY]  # type: ignore[index]


@pytest.mark.asyncio
async def test_composition_builds_executable_coordinator(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, _objects = composition_dependencies
    coordinator = build_durable_coordinator(dependencies)

    assert coordinator.queued_kinds == coordinator.authoritative_ready_kinds
    await coordinator.run_one_slice()


def test_self_profile_cadence_survives_database_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    cadence = SQLiteSelfProfileCadence(conn, 60.0)

    assert cadence.status(100.0).release_at == 0.0
    cadence.mark_refreshed(100.0)
    conn.close()

    restarted_conn = sqlite3.connect(str(db_path))
    try:
        restarted_cadence = SQLiteSelfProfileCadence(restarted_conn, 60.0)
        assert restarted_cadence.status(110.0).release_at == 160.0
        assert not restarted_cadence.status(159.0).is_ready(159.0)
        assert restarted_cadence.status(160.0).is_ready(160.0)
    finally:
        restarted_conn.close()
