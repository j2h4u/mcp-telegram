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
    AccessProbe,
    AccessProbePolicy,
    DeltaAccessProbeDemandAdapter,
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
    DmGapScanPage,
)
from mcp_telegram.dialog_directory import (
    CanonicalDialogDirectory,
    CanonicalDialogDirectoryDemandAdapter,
)
from mcp_telegram.dialog_sync import (
    DialogLightReconciliationDemandAdapter,
    DialogReconciliationWorker,
)
from mcp_telegram.drafts.owner import DraftMessageOwner
from mcp_telegram.entity_profile.ports import UserProfilePort
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, EntityRefreshCoordinator
from mcp_telegram.fact_hydration import FactHydrationDemandAdapter, MessageFactHydrationWorker
from mcp_telegram.folders.worker import FolderProjectionDemandAdapter, FolderProjectionWorker
from mcp_telegram.hydration_queue import HydrationPriority
from mcp_telegram.linked_chat_fact import linked_chat_fact_owner
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
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    acquisition_context,
    demand_context,
)
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator, validate_durable_adapters
from mcp_telegram.telegram_rpc_consumers import DemandKind, demand_freshness_seconds
from mcp_telegram.telegram_rpc_scheduler import rpc_attempt_budget


class DemandCompositionClient(ActivityClient, Protocol):
    async def get_me(self) -> object: ...


class LinkedChatFactRefreshPort(Protocol):
    async def retry_one_pending_linked_chat_fact(self, budget: RpcAttemptBudget) -> bool: ...


@dataclass(frozen=True, slots=True)
class DemandCompositionDependencies:
    """Daemon-owned objects required to construct every durable adapter."""

    client: DemandCompositionClient
    conn: SyncDatabaseConnection
    db_path: Path
    shutdown_event: asyncio.Event
    full_sync_worker: FullSyncWorker
    delta_sync_worker: DeltaSyncWorker
    access_probe: AccessProbe
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
    user_profile_port: UserProfilePort
    publish_startup_identity: Callable[[object, OwnOnlyContext], None]
    draft_owner: DraftMessageOwner
    linked_chat_fact_refresh_port: LinkedChatFactRefreshPort
    startup_detail_setter: Callable[[str], None] | None = None


class LinkedChatFactDemandAdapter(DurableDemandAdapter):
    """Retry one durable linked-chat fact demand with the coordinator's RPC budget."""

    demand_kind = DemandKind.LINKED_CHAT_REFRESH

    def __init__(self, port: LinkedChatFactRefreshPort, conn: SyncDatabaseConnection) -> None:
        self._port = port
        self._conn = conn

    def status(self, now: float) -> DemandStatus | None:
        release_at = linked_chat_fact_owner.next_release_at(self._conn)
        if release_at is None:
            return None
        return DemandStatus(release_at=float(release_at))

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        with demand_context(self.demand_kind):
            with acquisition_context(AcquisitionKind.LINKED_CHAT_RESOLUTION):
                with rpc_attempt_budget(budget):
                    await self._port.retry_one_pending_linked_chat_fact(budget)


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
            dependencies.access_probe,
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
            shutdown_event=dependencies.shutdown_event,
        ),
        DemandKind.READ_RECEIPT_BATCH: ReadReceiptDemandAdapter(
            dependencies.conn,
            dependencies.read_receipt_batch,
        ),
        DemandKind.SCHEDULED_REPAIR: ScheduledRepairDemandAdapter(dependencies.scheduled_reconciler),
        DemandKind.SCHEDULED_DISCOVERY: ScheduledDiscoveryDemandAdapter(dependencies.scheduled_reconciler),
        DemandKind.DRAFT_SNAPSHOT: dependencies.draft_owner,
        DemandKind.SELF_PROFILE_MAINTENANCE: SelfProfileMaintenanceDemandAdapter(
            SelfProfileMaintenanceDependencies(
                cadence=dependencies.self_profile_cadence,
                get_me=dependencies.client.get_me,
                update_profile=dependencies.update_self_profile,
                startup=dependencies.startup_identity,
                get_input_entity=dependencies.get_self_input_entity,
                user_profile_port=dependencies.user_profile_port,
                publish_startup_identity=dependencies.publish_startup_identity,
            )
        ),
        DemandKind.LINKED_CHAT_REFRESH: LinkedChatFactDemandAdapter(
            dependencies.linked_chat_fact_refresh_port,
            dependencies.conn,
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
    "DemandCompositionClient",
    "DemandCompositionDependencies",
    "build_durable_adapter_map",
    "build_durable_coordinator",
]
