"""Daemon composition for the process-wide Telegram demand coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from mcp_telegram.activity_cold_backfill import ColdBackfillPacing, ColdPeerPageDemandAdapter
from mcp_telegram.activity_hot_sweep import HotActivityDemandAdapter, HotSweepPolicy
from mcp_telegram.activity_substrate import ActivityClient
from mcp_telegram.activity_sync import ArchiveBackfillDemandAdapter, ArchiveIncrementalDemandAdapter
from mcp_telegram.delta_sync import (
    AccessProbePolicy,
    DeltaAccessProbeDemandAdapter,
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
    DmGapScanPage,
)
from mcp_telegram.dialog_directory import (
    CanonicalDialogDirectory,
    CanonicalDialogDirectoryDemandAdapter,
    CanonicalDirectoryFullDemandAdapter,
)
from mcp_telegram.dialog_sync import (
    DialogLightReconciliationDemandAdapter,
    DialogReconciliationWorker,
)
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, EntityRefreshCoordinator
from mcp_telegram.fact_hydration import FactHydrationDemandAdapter, MessageFactHydrationWorker
from mcp_telegram.folders.worker import FolderProjectionDemandAdapter, FolderProjectionWorker
from mcp_telegram.hydration_queue import HydrationPriority
from mcp_telegram.message_fact_refresh import (
    MessageFactRefreshDemandAdapter,
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    ReadReceiptDemandAdapter,
)
from mcp_telegram.own_only_contracts import OwnOnlyContext
from mcp_telegram.scheduled_messages import (
    ScheduledDiscoveryDemandAdapter,
    ScheduledMessageReconciler,
    ScheduledRepairDemandAdapter,
)
from mcp_telegram.self_profile_maintenance import (
    SelfProfileCadenceState,
    SelfProfileMaintenanceDemandAdapter,
    SelfProfileMaintenanceDependencies,
)
from mcp_telegram.startup_identity import StartupIdentityState
from mcp_telegram.sync_db import SyncDatabaseConnection
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncDmEnrollmentDemandAdapter, FullSyncWorker
from mcp_telegram.telegram_demand import (
    DurableDemandAdapter,
)
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator, validate_durable_adapters
from mcp_telegram.telegram_rpc_consumers import DemandKind, demand_freshness_seconds

# Compatibility constant retained for callers that read the contract-derived
# reconciliation cadence during the daemon cutover.
DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS = demand_freshness_seconds(DemandKind.DIALOG_FULL_RECONCILIATION)


class DemandCompositionClient(ActivityClient, Protocol):
    async def get_me(self) -> object: ...


@dataclass(frozen=True, slots=True)
class DemandCompositionDependencies:
    """Daemon-owned objects required to construct every durable adapter."""

    client: DemandCompositionClient
    conn: SyncDatabaseConnection
    db_path: Path
    shutdown_event: asyncio.Event
    full_sync_worker: FullSyncWorker
    delta_sync_worker: DeltaSyncWorker
    dm_gap_scanner: DmGapScanPage
    dialog_directory: CanonicalDialogDirectory
    dialog_reconciliation_worker: DialogReconciliationWorker
    entity_refresh_coordinator: EntityRefreshCoordinator
    fact_hydration_worker: MessageFactHydrationWorker
    folder_projection_worker: FolderProjectionWorker
    message_fact_refresh_deps: MessageFactRefreshDeps
    message_fact_refresh_policy: MessageFactRefreshPolicy
    scheduled_reconciler: ScheduledMessageReconciler
    access_probe_policy: AccessProbePolicy
    hot_sweep_policy: HotSweepPolicy
    cold_backfill_pacing: ColdBackfillPacing
    activity_rpc_timeout_seconds: float
    read_receipt_batch: Callable[[], Awaitable[object]]
    self_profile_cadence: SelfProfileCadenceState
    update_self_profile: Callable[[object], None]
    startup_identity: StartupIdentityState
    get_self_input_entity: Callable[[int], Awaitable[object]]
    get_full_self_user: Callable[[object], Awaitable[object]]
    publish_startup_identity: Callable[[object, OwnOnlyContext], None]
    startup_detail_setter: Callable[[str], None] | None = None


def build_durable_adapter_map(dependencies: DemandCompositionDependencies) -> Mapping[DemandKind, DurableDemandAdapter]:
    """Build and validate the exhaustive durable adapter map."""
    adapters: dict[DemandKind, DurableDemandAdapter] = {
        DemandKind.ENTITY_PROFILE_REFRESH: EntityProfileDemandAdapter(dependencies.entity_refresh_coordinator),
        DemandKind.DELTA_GAP_FILL: DeltaGapFillDemandAdapter(
            dependencies.delta_sync_worker,
            dependencies.dm_gap_scanner,
        ),
        DemandKind.DELTA_ACCESS_PROBE: DeltaAccessProbeDemandAdapter(
            dependencies.delta_sync_worker,
            dependencies.access_probe_policy,
        ),
        DemandKind.HOT_ACTIVITY_PAGE: HotActivityDemandAdapter(
            dependencies.client,
            dependencies.conn,
            dependencies.shutdown_event,
            dependencies.hot_sweep_policy,
            dependencies.activity_rpc_timeout_seconds,
        ),
        DemandKind.LIVE_HYDRATION_BATCH: FactHydrationDemandAdapter(
            dependencies.fact_hydration_worker,
            HydrationPriority.FOREGROUND,
        ),
        DemandKind.FULL_SYNC_DM_ENROLLMENT: FullSyncDmEnrollmentDemandAdapter(dependencies.full_sync_worker),
        DemandKind.FULL_SYNC_PAGE: FullSyncDemandAdapter(dependencies.full_sync_worker),
        DemandKind.DIALOG_BOOTSTRAP: CanonicalDialogDirectoryDemandAdapter(
            dependencies.dialog_directory,
            dependencies.conn,
        ),
        DemandKind.DIALOG_LIGHT_RECONCILIATION: DialogLightReconciliationDemandAdapter(
            dependencies.dialog_reconciliation_worker
        ),
        DemandKind.DIALOG_FULL_RECONCILIATION: CanonicalDirectoryFullDemandAdapter(
            dependencies.dialog_directory,
            dependencies.conn,
        ),
        DemandKind.ARCHIVE_BACKFILL: ArchiveBackfillDemandAdapter(
            dependencies.client,
            dependencies.conn,
            dependencies.shutdown_event,
            dependencies.activity_rpc_timeout_seconds,
        ),
        DemandKind.ARCHIVE_INCREMENTAL: ArchiveIncrementalDemandAdapter(
            dependencies.client,
            dependencies.conn,
            dependencies.shutdown_event,
            demand_freshness_seconds(DemandKind.ARCHIVE_INCREMENTAL),
            dependencies.activity_rpc_timeout_seconds,
        ),
        DemandKind.COLD_PEER_PAGE: ColdPeerPageDemandAdapter(
            dependencies.client,
            dependencies.conn,
            dependencies.shutdown_event,
            dependencies.cold_backfill_pacing,
            dependencies.activity_rpc_timeout_seconds,
        ),
        DemandKind.BACKFILL_HYDRATION_BATCH: FactHydrationDemandAdapter(
            dependencies.fact_hydration_worker,
            HydrationPriority.BACKFILL,
        ),
        DemandKind.FOLDER_SNAPSHOT: FolderProjectionDemandAdapter(dependencies.folder_projection_worker),
        DemandKind.MESSAGE_FACT_REFRESH: MessageFactRefreshDemandAdapter(
            dependencies.message_fact_refresh_deps,
            dependencies.message_fact_refresh_policy,
        ),
        DemandKind.READ_RECEIPT_BATCH: ReadReceiptDemandAdapter(
            dependencies.conn,
            dependencies.read_receipt_batch,
        ),
        DemandKind.SCHEDULED_REPAIR: ScheduledRepairDemandAdapter(dependencies.scheduled_reconciler),
        DemandKind.SCHEDULED_DISCOVERY: ScheduledDiscoveryDemandAdapter(dependencies.scheduled_reconciler),
        DemandKind.SELF_PROFILE_MAINTENANCE: SelfProfileMaintenanceDemandAdapter(
            SelfProfileMaintenanceDependencies(
                cadence=dependencies.self_profile_cadence,
                get_me=dependencies.client.get_me,
                update_profile=dependencies.update_self_profile,
                startup=dependencies.startup_identity,
                get_input_entity=dependencies.get_self_input_entity,
                get_full_user=dependencies.get_full_self_user,
                publish_startup_identity=dependencies.publish_startup_identity,
            )
        ),
    }
    validate_durable_adapters(adapters)
    for kind, adapter in adapters.items():
        if getattr(adapter, "demand_kind", None) is not kind:
            raise RuntimeError(f"durable adapter {kind.value} declares the wrong demand kind")
    return MappingProxyType(adapters)


def build_durable_coordinator(
    dependencies: DemandCompositionDependencies,
    *,
    observer: object | None = None,
) -> TelegramDemandCoordinator:
    """Construct the sole durable executor over the exhaustive adapter map."""
    return TelegramDemandCoordinator(
        build_durable_adapter_map(dependencies),
        dependencies.shutdown_event,
        observer=observer,
    )


__all__ = [
    "DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS",
    "DemandCompositionClient",
    "DemandCompositionDependencies",
    "build_durable_adapter_map",
    "build_durable_coordinator",
]
