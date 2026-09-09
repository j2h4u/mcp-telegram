"""Daemon composition for the PR1 shadow Telegram demand coordinator."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from mcp_telegram.activity_cold_backfill import ColdBackfillPacing, ColdPeerPageDemandAdapter
from mcp_telegram.activity_hot_sweep import HotActivityDemandAdapter, HotSweepPolicy
from mcp_telegram.activity_substrate import ActivityClient
from mcp_telegram.activity_sync import ArchiveBackfillDemandAdapter, ArchiveIncrementalDemandAdapter
from mcp_telegram.delta_sync import (
    AccessProbePolicy,
    DeltaAccessProbeDemandAdapter,
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
)
from mcp_telegram.dialog_sync import (
    DialogBootstrapDemandAdapter,
    DialogFullReconciliationDemandAdapter,
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
from mcp_telegram.rpc_admission_observations import DemandEvidenceOutcome
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
from mcp_telegram.sync_db import SyncDatabaseConnection
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncDmEnrollmentDemandAdapter, FullSyncWorker
from mcp_telegram.telegram_demand import DemandStatus, DurableDemandAdapter, RpcAttemptBudget
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator, validate_durable_adapters
from mcp_telegram.telegram_rpc_consumers import DemandKind

logger = logging.getLogger(__name__)

ACTIVITY_ARCHIVE_INTERVAL_SECONDS = 3_600.0
DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS = 86_400.0
SHADOW_SAFETY_SCAN_SECONDS = 60.0


class DemandCompositionClient(ActivityClient, Protocol):
    async def get_me(self) -> object: ...


class DemandEvidenceObserver(Protocol):
    """Narrow telemetry callback used by shadow composition."""

    def observe_demand(  # noqa: PLR0913 - mirrors the telemetry callback boundary
        self,
        *,
        outcome: DemandEvidenceOutcome | str,
        demand_kind: DemandKind,
        demand_units: int = 1,
        actual_attempts: int = 0,
        oldest_overdue_seconds: float | None = None,
        reason: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _ShadowStatusAdapter:
    """Make status observation incapable of disrupting legacy execution."""

    demand_kind: DemandKind
    adapter: DurableDemandAdapter
    on_status_error: Callable[[DemandKind, Exception], None]

    def status(self, now: float) -> DemandStatus | None:
        try:
            status = self.adapter.status(now)
            if status is not None and not isinstance(status, DemandStatus):
                raise TypeError("adapter returned an invalid status")
            return status
        except Exception as exc:  # noqa: BLE001 - PR1 shadow cannot affect production work
            self.on_status_error(self.demand_kind, exc)
            return None

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        del budget
        raise RuntimeError("PR1 shadow adapters cannot execute durable slices")


@dataclass(frozen=True, slots=True)
class DemandCompositionDependencies:
    """Existing daemon objects shared by legacy launchers and shadow adapters."""

    client: DemandCompositionClient
    conn: SyncDatabaseConnection
    db_path: Path
    shutdown_event: asyncio.Event
    full_sync_worker: FullSyncWorker
    delta_sync_worker: DeltaSyncWorker
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
    startup_detail_setter: Callable[[str], None] | None = None


def build_durable_adapter_map(
    dependencies: DemandCompositionDependencies,
) -> Mapping[DemandKind, DurableDemandAdapter]:
    """Build and validate the exhaustive PR1 durable adapter map."""
    adapters: dict[DemandKind, DurableDemandAdapter] = {
        DemandKind.ENTITY_PROFILE_REFRESH: EntityProfileDemandAdapter(dependencies.entity_refresh_coordinator),
        DemandKind.DELTA_GAP_FILL: DeltaGapFillDemandAdapter(dependencies.delta_sync_worker),
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
        DemandKind.DIALOG_BOOTSTRAP: DialogBootstrapDemandAdapter(
            dependencies.client,
            dependencies.conn,
            dependencies.db_path,
            dependencies.shutdown_event,
            startup_detail_setter=dependencies.startup_detail_setter,
        ),
        DemandKind.DIALOG_LIGHT_RECONCILIATION: DialogLightReconciliationDemandAdapter(
            dependencies.dialog_reconciliation_worker
        ),
        DemandKind.DIALOG_FULL_RECONCILIATION: DialogFullReconciliationDemandAdapter(
            dependencies.dialog_reconciliation_worker,
            interval_seconds=DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS,
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
            ACTIVITY_ARCHIVE_INTERVAL_SECONDS,
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
            )
        ),
    }
    validate_durable_adapters(adapters)
    for kind, adapter in adapters.items():
        if getattr(adapter, "demand_kind", None) is not kind:
            raise RuntimeError(f"durable adapter {kind.value} declares the wrong demand kind")
    return MappingProxyType(adapters)


class TelegramDemandShadow:
    """Drive authoritative PR1 scans while never executing an adapter slice."""

    def __init__(
        self,
        adapters: Mapping[DemandKind, DurableDemandAdapter],
        shutdown_event: asyncio.Event,
        *,
        observer: DemandEvidenceObserver | None = None,
        clock: Callable[[], float] = time.time,
        safety_scan_seconds: float = SHADOW_SAFETY_SCAN_SECONDS,
    ) -> None:
        if not math.isfinite(safety_scan_seconds) or safety_scan_seconds <= 0:
            raise ValueError("safety_scan_seconds must be finite and positive")
        self._shutdown_event = shutdown_event
        self._observer = observer
        self._clock = clock
        self._safety_scan_seconds = safety_scan_seconds
        self._wakeup = asyncio.Event()
        self._ready: set[DemandKind] = set()
        shadow_adapters = {
            kind: _ShadowStatusAdapter(kind, adapter, self._status_failed) for kind, adapter in adapters.items()
        }
        self.coordinator = TelegramDemandCoordinator(shadow_adapters, clock=clock)
        self._observe_transitions(self.coordinator.ready_kinds, now=self._clock())

    def offer(self, kind: DemandKind) -> bool:
        """Forward one wakeup hint, record coalescing, and wake the scan loop."""
        accepted = self.coordinator.offer(kind)
        self._observe(
            DemandEvidenceOutcome.OFFERED if accepted else DemandEvidenceOutcome.COALESCED_WAKEUP,
            kind,
        )
        self._observe_transitions(self.coordinator.ready_kinds, now=self._clock())
        self._wakeup.set()
        return accepted

    def after_cycle_scan(self) -> tuple[DemandKind, ...]:
        """Refresh shadow state after a legacy executor completes a cycle."""
        now = self._clock()
        ready = self.coordinator.after_cycle_scan(now=now)
        self._observe_transitions(ready, now=now)
        self._wakeup.set()
        return ready

    async def run(self) -> None:
        """Scan at the nearest release, on offers, and on a bounded safety cadence."""
        while not self._shutdown_event.is_set():
            now = self._clock()
            ready = self.coordinator.timer_scan(now=now)
            self._observe_transitions(ready, now=now)
            self._wakeup.clear()
            delay = self._next_scan_delay(now)
            if await self._wait_for_signal(delay):
                return

    def _next_scan_delay(self, now: float) -> float:
        release_at = self.coordinator.next_release_at
        if release_at is None:
            return self._safety_scan_seconds
        return min(self._safety_scan_seconds, max(0.0, release_at - now))

    async def _wait_for_signal(self, timeout: float) -> bool:
        shutdown_wait = asyncio.create_task(self._shutdown_event.wait())
        wakeup_wait = asyncio.create_task(self._wakeup.wait())
        pending: set[asyncio.Task[Literal[True]]] = set()
        try:
            done, pending = await asyncio.wait(
                (shutdown_wait, wakeup_wait),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            return shutdown_wait in done and shutdown_wait.result()
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def _observe_transitions(self, ready: tuple[DemandKind, ...], *, now: float) -> None:
        current = set(ready)
        for kind in sorted(current - self._ready, key=lambda item: item.value):
            status = self.coordinator.statuses[kind]
            overdue = status.overdue_seconds(now)
            self._observe(
                DemandEvidenceOutcome.READY,
                kind,
                oldest_overdue_seconds=overdue if overdue > 0 else None,
            )
        for kind in sorted(self._ready - current, key=lambda item: item.value):
            self._observe(DemandEvidenceOutcome.LOCALLY_SATISFIED, kind)
        self._ready = current

    def _observe(
        self,
        outcome: DemandEvidenceOutcome,
        kind: DemandKind,
        *,
        oldest_overdue_seconds: float | None = None,
        reason: str | None = None,
    ) -> None:
        if self._observer is None:
            return
        try:
            self._observer.observe_demand(
                outcome=outcome,
                demand_kind=kind,
                oldest_overdue_seconds=oldest_overdue_seconds,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - telemetry cannot affect shadow selection
            logger.warning("telegram_demand_shadow_observation_failed kind=%s", kind.value)

    def _status_failed(self, kind: DemandKind, exc: Exception) -> None:
        logger.warning(
            "telegram_demand_shadow_status_failed kind=%s error_type=%s",
            kind.value,
            type(exc).__name__,
        )
        self._observe(DemandEvidenceOutcome.FAILED, kind, reason="status_error")


__all__ = [
    "DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS",
    "DemandCompositionClient",
    "DemandCompositionDependencies",
    "TelegramDemandShadow",
    "build_durable_adapter_map",
]
