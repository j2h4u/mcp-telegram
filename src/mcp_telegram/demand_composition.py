"""Daemon composition for the process-wide Telegram demand coordinator."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

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
    DmDeletionReconciliationDemandAdapter,
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
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, EntityRefreshCoordinator
from mcp_telegram.fact_hydration import FactHydrationDemandAdapter, MessageFactHydrationWorker
from mcp_telegram.folders.worker import FolderProjectionDemandAdapter, FolderProjectionWorker
from mcp_telegram.hydration_queue import HydrationPriority
from mcp_telegram.linked_chat_fact import LinkedChatWork, linked_chat_fact_owner
from mcp_telegram.message_fact_refresh import (
    MessageFactRefreshDemandAdapter,
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    ReadReceiptDemandAdapter,
)
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
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.startup_identity import StartupIdentityState
from mcp_telegram.sync_db import SyncDatabaseConnection
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncDmEnrollmentDemandAdapter, FullSyncWorker
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    DeltaGapFillObservationHook,
    DemandObservationHook,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcRetryAuthorization,
    acquisition_context,
    durable_demand_context,
)
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator, validate_durable_adapters
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from mcp_telegram.telegram_rpc_consumers import DemandKind, demand_freshness_seconds
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcMethodRecoveryPending,
    TelegramRpcThrottled,
    rpc_attempt_budget,
)
from mcp_telegram.topics.demand import TopicSnapshotDemandAdapter
from mcp_telegram.topics.telegram_adapter import TelethonTelegramTopicGateway, TopicClient


class DemandCompositionClient(ActivityClient, Protocol):
    async def get_me(self) -> object: ...

    def rpc_eligibility(
        self, method_key: str | None = None
    ) -> TelegramRpcThrottled | TelegramRpcMethodRecoveryPending | None: ...

    def prepare_durable_retry(
        self, budget: RpcAttemptBudget, method_key: str | None
    ) -> RpcRetryAuthorization | None: ...

    def set_rpc_eligibility_observer(self, observer: Callable[[str | None], None] | None) -> None: ...


class DemandCompositionObserver(DemandObservationHook, DeltaGapFillObservationHook, Protocol):
    """Observer hooks consumed by coordinator and delta gap-fill adapters."""


class LinkedChatFactRefreshPort(Protocol):
    async def retry_one_pending_linked_chat_fact(self, budget: RpcAttemptBudget) -> SliceOutcome: ...


@dataclass(frozen=True, slots=True)
class DemandCompositionDependencies:
    """Daemon-owned objects required to construct every durable adapter."""

    client: DemandCompositionClient
    conn: SyncDatabaseConnection
    account_id: int
    writer: SQLiteWriter
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
    read_receipt_batch: Callable[[WorkRevision], Awaitable[SliceOutcome]]
    self_profile_cadence: SelfProfileCadenceState
    update_self_profile: Callable[[object], Awaitable[None]]
    startup_identity: StartupIdentityState
    publish_startup_identity: Callable[[object, int], Awaitable[None]]
    draft_owner: DraftMessageOwner
    linked_chat_fact_refresh_port: LinkedChatFactRefreshPort
    startup_detail_setter: Callable[[str], None] | None = None


class LinkedChatFactDemandAdapter(DurableDemandAdapter):
    """Retry one durable linked-chat fact demand with the coordinator's RPC budget."""

    demand_kind = DemandKind.LINKED_CHAT_REFRESH

    def __init__(self, port: LinkedChatFactRefreshPort, conn: SyncDatabaseConnection, *, account_id: int) -> None:
        self._port = port
        self._conn = conn
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        self._account_id = account_id

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        del now
        for work in linked_chat_fact_owner.pending_work(self._conn):
            selected = self._selected(work)
            if selected not in excluded:
                return DemandStatus(float(work.retry_at), selected=selected)
        return None

    def _selected(self, work: LinkedChatWork) -> WorkRevision:
        return WorkRevision(
            WorkKey(self.demand_kind, self._account_id, str(work.channel_id)),
            repr((work.generation, work.generation)),
        )

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise ValueError("linked-chat demand requires a selected revision")
        status = next(
            (
                DemandStatus(float(work.retry_at), selected=selected)
                for work in linked_chat_fact_owner.pending_work(self._conn)
                if self._selected(work) == selected
            ),
            None,
        )
        if status is None:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if budget.exhausted and not budget.can_prepare_retry:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        if not status.is_ready(time.time()):
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        with durable_demand_context(self.demand_kind):
            with acquisition_context(AcquisitionKind.LINKED_CHAT_RESOLUTION):
                with rpc_attempt_budget(budget):
                    return await self._port.retry_one_pending_linked_chat_fact(budget)


def build_durable_adapter_map(
    dependencies: DemandCompositionDependencies,
    *,
    observer: DeltaGapFillObservationHook | None = None,
) -> Mapping[DemandKind, DurableDemandAdapter]:
    """Build and validate the exhaustive durable adapter map."""
    adapters: dict[DemandKind, DurableDemandAdapter] = {
        DemandKind.TOPIC_SNAPSHOT: TopicSnapshotDemandAdapter(
            TelethonTelegramTopicGateway(cast(TopicClient, dependencies.client)),
            dependencies.conn,
            writer=dependencies.writer,
            account_id=dependencies.account_id,
        ),
        DemandKind.ENTITY_PROFILE_REFRESH: EntityProfileDemandAdapter(dependencies.entity_refresh_coordinator),
        DemandKind.DELTA_GAP_FILL: DeltaGapFillDemandAdapter(
            dependencies.delta_sync_worker, account_id=dependencies.account_id, observer=observer
        ),
        DemandKind.DM_DELETION_RECONCILIATION: DmDeletionReconciliationDemandAdapter(
            dependencies.conn,
            dependencies.dm_gap_scanner,
            account_id=dependencies.account_id,
            writer=dependencies.writer,
        ),
        DemandKind.DELTA_ACCESS_PROBE: DeltaAccessProbeDemandAdapter(
            dependencies.delta_sync_worker,
            dependencies.access_probe_policy,
            dependencies.access_probe,
            account_id=dependencies.account_id,
            observer=observer,
        ),
        DemandKind.HOT_ACTIVITY_PAGE: HotActivityDemandAdapter(
            dependencies.client,
            dependencies.conn,
            dependencies.shutdown_event,
            dependencies.hot_sweep_policy,
            dependencies.activity_rpc_timeout_seconds,
            account_id=dependencies.account_id,
            writer=dependencies.writer,
        ),
        DemandKind.LIVE_HYDRATION_BATCH: FactHydrationDemandAdapter(
            dependencies.fact_hydration_worker,
            HydrationPriority.FOREGROUND,
            account_id=dependencies.account_id,
        ),
        DemandKind.FULL_SYNC_DM_ENROLLMENT: FullSyncDmEnrollmentDemandAdapter(
            dependencies.full_sync_worker, account_id=dependencies.account_id
        ),
        DemandKind.FULL_SYNC_PAGE: FullSyncDemandAdapter(
            dependencies.full_sync_worker, account_id=dependencies.account_id
        ),
        DemandKind.DIALOG_BOOTSTRAP: CanonicalDialogDirectoryDemandAdapter(
            dependencies.dialog_directory,
            dependencies.conn,
        ),
        DemandKind.DIALOG_LIGHT_RECONCILIATION: DialogLightReconciliationDemandAdapter(
            dependencies.dialog_reconciliation_worker, account_id=dependencies.account_id
        ),
        DemandKind.ARCHIVE_BACKFILL: ArchiveBackfillDemandAdapter(
            dependencies.client,
            dependencies.conn,
            dependencies.shutdown_event,
            dependencies.activity_rpc_timeout_seconds,
            account_id=dependencies.account_id,
            writer=dependencies.writer,
        ),
        DemandKind.ARCHIVE_INCREMENTAL: ArchiveIncrementalDemandAdapter(
            dependencies.client,
            dependencies.conn,
            dependencies.shutdown_event,
            demand_freshness_seconds(DemandKind.ARCHIVE_INCREMENTAL),
            dependencies.activity_rpc_timeout_seconds,
            account_id=dependencies.account_id,
            writer=dependencies.writer,
        ),
        DemandKind.COLD_PEER_PAGE: ColdPeerPageDemandAdapter(
            dependencies.client,
            dependencies.conn,
            dependencies.shutdown_event,
            dependencies.cold_backfill_pacing,
            dependencies.activity_rpc_timeout_seconds,
            account_id=dependencies.account_id,
            writer=dependencies.writer,
        ),
        DemandKind.BACKFILL_HYDRATION_BATCH: FactHydrationDemandAdapter(
            dependencies.fact_hydration_worker,
            HydrationPriority.BACKFILL,
            account_id=dependencies.account_id,
        ),
        DemandKind.FOLDER_SNAPSHOT: FolderProjectionDemandAdapter(
            dependencies.folder_projection_worker, account_id=dependencies.account_id
        ),
        DemandKind.MESSAGE_FACT_REFRESH: MessageFactRefreshDemandAdapter(
            dependencies.message_fact_refresh_deps,
            dependencies.message_fact_refresh_policy,
            account_id=dependencies.account_id,
            shutdown_event=dependencies.shutdown_event,
        ),
        DemandKind.READ_RECEIPT_BATCH: ReadReceiptDemandAdapter(
            dependencies.conn,
            dependencies.read_receipt_batch,
            account_id=dependencies.account_id,
        ),
        DemandKind.SCHEDULED_REPAIR: ScheduledRepairDemandAdapter(
            dependencies.scheduled_reconciler, account_id=dependencies.account_id
        ),
        DemandKind.SCHEDULED_DISCOVERY: ScheduledDiscoveryDemandAdapter(
            dependencies.scheduled_reconciler, account_id=dependencies.account_id
        ),
        DemandKind.DRAFT_SNAPSHOT: dependencies.draft_owner,
        DemandKind.SELF_PROFILE_MAINTENANCE: SelfProfileMaintenanceDemandAdapter(
            SelfProfileMaintenanceDependencies(
                cadence=dependencies.self_profile_cadence,
                get_me=dependencies.client.get_me,
                update_profile=dependencies.update_self_profile,
                startup=dependencies.startup_identity,
                publish_startup_identity=dependencies.publish_startup_identity,
            ),
            account_id=dependencies.account_id,
        ),
        DemandKind.LINKED_CHAT_REFRESH: LinkedChatFactDemandAdapter(
            dependencies.linked_chat_fact_refresh_port,
            dependencies.conn,
            account_id=dependencies.account_id,
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
    observer: DemandCompositionObserver | None = None,
) -> TelegramDemandCoordinator:
    """Construct the sole durable executor over the exhaustive adapter map."""
    coordinator = TelegramDemandCoordinator(
        build_durable_adapter_map(dependencies, observer=observer),
        dependencies.shutdown_event,
        observer=observer,
        rpc_eligibility=dependencies.client.rpc_eligibility,
        prepare_durable_retry=dependencies.client.prepare_durable_retry,
    )
    dependencies.client.set_rpc_eligibility_observer(coordinator.eligibility_changed)
    return coordinator


__all__ = [
    "DemandCompositionClient",
    "DemandCompositionDependencies",
    "build_durable_adapter_map",
    "build_durable_coordinator",
]
