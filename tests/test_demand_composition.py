from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
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
from mcp_telegram.delta_sync import DeltaAccessProbeDemandAdapter, DeltaGapFillDemandAdapter, DmGapScanPage
from mcp_telegram.demand_composition import (
    DemandCompositionClient,
    DemandCompositionDependencies,
    LinkedChatFactDemandAdapter,
    build_durable_adapter_map,
    build_durable_coordinator,
)
from mcp_telegram.dialog_directory import (
    CanonicalDialogDirectory,
    CanonicalDialogDirectoryDemandAdapter,
)
from mcp_telegram.dialog_sync import (
    DialogLightReconciliationDemandAdapter,
)
from mcp_telegram.drafts.owner import DraftMessageOwner
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, EntityRefreshCoordinator
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.fact_hydration import FactHydrationDemandAdapter
from mcp_telegram.folders.contracts import FolderRuleObservation
from mcp_telegram.folders.ports import FolderSnapshotRepository
from mcp_telegram.folders.worker import FolderProjectionDemandAdapter, FolderProjectionWorker
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
    ScheduledReconciliationPolicy,
    ScheduledRepairDemandAdapter,
)
from mcp_telegram.self_profile_maintenance import (
    SelfProfileCadenceState,
    SelfProfileMaintenanceDemandAdapter,
)
from mcp_telegram.startup_identity import StartupIdentityState
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncDmEnrollmentDemandAdapter
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    current_demand_token,
)
from mcp_telegram.telegram_rpc_consumers import (
    DemandKind,
    TelegramRpcSource,
    demand_freshness_seconds,
)
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from tests.helpers import LoudUserProfilePort


@dataclass(frozen=True, slots=True)
class _FolderPolicy:
    refresh_interval_seconds: float = 60.0
    jitter_ratio: float = 0.0
    retry_delays_seconds: tuple[int, ...] = (1,)
    retry_cap_seconds: int = 60
    warning_failure_threshold: int = 3
    stale_threshold_seconds: int = 300


class _IdleFolderRepository(FolderSnapshotRepository):
    def read_consecutive_failures(self) -> int:
        return 0

    def read_last_outcome(self) -> str | None:
        return None

    def read_last_success_at(self) -> int | None:
        return None

    def read_next_retry_at(self) -> int | None:
        return None

    def rules_are_fresh(self, *, now: int) -> bool:
        del now
        return False

    def project_observation(self, observation: FolderRuleObservation, *, completed_at: int) -> int | None:
        del observation, completed_at
        return None

    def reproject_current_rules(self, *, now: int) -> int | None:
        del now
        return None

    def next_mute_expiry(self) -> int | None:
        return None

    def record_attempt(
        self,
        *,
        attempted_at: int,
        outcome: str,
        next_retry_at: int | None,
        consecutive_failures: int,
    ) -> None:
        del attempted_at, outcome, next_retry_at, consecutive_failures


class _DmGapScanner(DmGapScanPage):
    async def run_dm_gap_scan_page(self, dialog_id: int, message_ids: Sequence[int]) -> int:
        del dialog_id, message_ids
        return 0


@dataclass(frozen=True, slots=True)
class _HotPolicy:
    loop_interval_seconds: float = 60.0
    max_peers_per_pass: int = 1
    base_due_seconds: float = 60.0
    max_due_seconds: float = 300.0
    jitter_max_seconds: float = 0.0
    initial_spread_seconds: float = 0.0


class _LinkedChatRefresher:
    def __init__(self) -> None:
        self.budgets: list[RpcAttemptBudget] = []
        self.contexts: list[tuple[DemandKind, str | None, str]] = []

    async def retry_one_pending_linked_chat_fact(self, budget: RpcAttemptBudget) -> bool:
        self.budgets.append(budget)
        token = current_demand_token()
        scope = current_rpc_scope()
        assert scope.attempt_budget is budget
        self.contexts.append(
            (
                token.kind,
                token.acquisition_kind.value if token.acquisition_kind else None,
                scope.source.value,
            )
        )
        return True


def _dependencies(tmp_path: Path) -> tuple[DemandCompositionDependencies, dict[str, object]]:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    folder_repository = _IdleFolderRepository()
    folder_worker = FolderProjectionWorker(MagicMock(), folder_repository, asyncio.Event(), _FolderPolicy())
    message_fact_policy = MessageFactRefreshPolicy(
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
        "access_probe": MagicMock(),
        "dm_gap_scanner": _DmGapScanner(),
        "dialog": MagicMock(),
        "directory": CanonicalDialogDirectory(MagicMock(), db_path, asyncio.Event()),
        "entity": EntityRefreshCoordinator(),
        "hydration": MagicMock(),
        "folder": folder_worker,
        "facts": MessageFactRefreshDeps(conn, MagicMock(), MagicMock()),
        "fact_policy": message_fact_policy,
        "scheduled": MagicMock(),
        "access_policy": MagicMock(),
        "hot_policy": _HotPolicy(),
        "cold_pacing": ColdBackfillPacing(
            history=ColdBackfillHistoryPacing(batch_s=1.0, enroll_s=60.0, access_retry_s=60.0),
        ),
        "cadence": SQLiteSelfProfileCadence(conn, 60.0),
        "draft_owner": DraftMessageOwner(
            MagicMock(), MagicMock(), asyncio.Event(), UpdateProcessingBarrier(closed=True)
        ),
        "linked_chat_refresh": _LinkedChatRefresher(),
    }

    async def read_receipt_batch() -> object:
        return None

    async def get_self_input_entity(_account_id: int) -> object:
        return object()

    update_profile: Callable[[object], None] = MagicMock()
    publish_startup_identity: Callable[[object, OwnOnlyContext], None] = MagicMock()
    dependencies = DemandCompositionDependencies(
        client=cast(DemandCompositionClient, objects["client"]),
        conn=cast(sqlite3.Connection, objects["conn"]),
        db_path=db_path,
        shutdown_event=cast(asyncio.Event, objects["shutdown"]),
        full_sync_worker=cast(object, objects["full"]),  # type: ignore[arg-type]
        delta_sync_worker=cast(object, objects["delta"]),  # type: ignore[arg-type]
        access_probe=cast(object, objects["access_probe"]),  # type: ignore[arg-type]
        dm_gap_scanner=cast(DmGapScanPage, objects["dm_gap_scanner"]),
        dialog_directory=cast(CanonicalDialogDirectory, objects["directory"]),
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
        startup_identity=StartupIdentityState.begin(now=100.0),
        get_self_input_entity=get_self_input_entity,
        user_profile_port=LoudUserProfilePort(),
        publish_startup_identity=publish_startup_identity,
        draft_owner=cast(DraftMessageOwner, objects["draft_owner"]),
        linked_chat_fact_refresh_port=cast(object, objects["linked_chat_refresh"]),  # type: ignore[arg-type]
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


def test_adapter_map_is_exact_against_literal_21_kind_class_map(
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
        DemandKind.DIALOG_BOOTSTRAP: CanonicalDialogDirectoryDemandAdapter,
        DemandKind.DIALOG_LIGHT_RECONCILIATION: DialogLightReconciliationDemandAdapter,
        DemandKind.ARCHIVE_BACKFILL: ArchiveBackfillDemandAdapter,
        DemandKind.ARCHIVE_INCREMENTAL: ArchiveIncrementalDemandAdapter,
        DemandKind.COLD_PEER_PAGE: ColdPeerPageDemandAdapter,
        DemandKind.BACKFILL_HYDRATION_BATCH: FactHydrationDemandAdapter,
        DemandKind.FOLDER_SNAPSHOT: FolderProjectionDemandAdapter,
        DemandKind.MESSAGE_FACT_REFRESH: MessageFactRefreshDemandAdapter,
        DemandKind.READ_RECEIPT_BATCH: ReadReceiptDemandAdapter,
        DemandKind.SCHEDULED_REPAIR: ScheduledRepairDemandAdapter,
        DemandKind.SCHEDULED_DISCOVERY: ScheduledDiscoveryDemandAdapter,
        DemandKind.DRAFT_SNAPSHOT: DraftMessageOwner,
        DemandKind.SELF_PROFILE_MAINTENANCE: SelfProfileMaintenanceDemandAdapter,
        DemandKind.LINKED_CHAT_REFRESH: LinkedChatFactDemandAdapter,
    }
    assert len(expected) == 21
    assert set(adapters) == set(expected)
    assert {kind: type(adapter) for kind, adapter in adapters.items()} == expected
    assert all(getattr(adapter, "demand_kind", None) is kind for kind, adapter in adapters.items())
    assert adapters[DemandKind.FULL_SYNC_PAGE]._worker is objects["full"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.FULL_SYNC_DM_ENROLLMENT]._worker is objects["full"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_GAP_FILL]._worker is objects["delta"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_GAP_FILL]._dm_gap_scanner is objects["dm_gap_scanner"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_ACCESS_PROBE]._worker is objects["delta"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DIALOG_LIGHT_RECONCILIATION]._worker is objects["dialog"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DIALOG_BOOTSTRAP]._directory is objects["directory"]  # type: ignore[attr-defined]
    assert (
        adapters[DemandKind.ARCHIVE_INCREMENTAL].interval_s  # type: ignore[attr-defined]
        == demand_freshness_seconds(DemandKind.ARCHIVE_INCREMENTAL)
    )
    assert adapters[DemandKind.LIVE_HYDRATION_BATCH]._worker is objects["hydration"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.BACKFILL_HYDRATION_BATCH]._worker is objects["hydration"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.FOLDER_SNAPSHOT]._worker is objects["folder"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.SCHEDULED_REPAIR]._reconciler is objects["scheduled"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.SCHEDULED_DISCOVERY]._reconciler is objects["scheduled"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DRAFT_SNAPSHOT] is objects["draft_owner"]
    assert adapters[DemandKind.MESSAGE_FACT_REFRESH]._shutdown_event is objects["shutdown"]  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        adapters[DemandKind.SCHEDULED_REPAIR] = adapters[DemandKind.SCHEDULED_DISCOVERY]  # type: ignore[index]


def test_linked_chat_adapter_selects_due_demand_and_passes_one_attempt_budget(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, objects = composition_dependencies
    refresher = cast(_LinkedChatRefresher, objects["linked_chat_refresh"])
    adapter = build_durable_adapter_map(dependencies)[DemandKind.LINKED_CHAT_REFRESH]
    assert isinstance(adapter, LinkedChatFactDemandAdapter)
    assert adapter.status(10**12) is None
    with dependencies.conn:
        dependencies.conn.execute(
            "INSERT INTO linked_chat_fact_state "
            "(channel_id,generation,pending_generation,requested_at,retry_at) VALUES(?,?,?,?,?)",
            (123, 1, 1, 100, 400),
        )
    status = adapter.status(100)
    assert status is not None and status.release_at == 400

    budget = RpcAttemptBudget(limit=1)
    asyncio.run(adapter.run_slice(budget))

    assert refresher.budgets == [budget]
    assert refresher.contexts == [(DemandKind.LINKED_CHAT_REFRESH, "linked_chat_resolution", "linked_chat_refresh")]


def test_scheduled_repair_remains_due_during_day_long_local_link_wait(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    from mcp_telegram.linked_chat_fact import linked_chat_fact_owner

    dependencies, _objects = composition_dependencies
    now = 1_800_000_000
    channel_id = -1000000000123
    due_dialog_id = -1000000000456
    with dependencies.conn:
        linked_chat_fact_owner.ensure_cold_demand(dependencies.conn, channel_id, now)
        work = linked_chat_fact_owner.next_due(dependencies.conn, now)
        assert work is not None
        assert linked_chat_fact_owner.defer(dependencies.conn, work, now + 86_400)
        dependencies.conn.execute(
            "INSERT INTO scheduled_reconciliation_state "
            "(dialog_id,repair_due_at,discovery_due_at,updated_at) VALUES (?,?,?,?)",
            (due_dialog_id, now, now, now),
        )

    reconciler = ScheduledMessageReconciler(
        cast(object, dependencies.client),  # type: ignore[arg-type]
        dependencies.conn,
        dependencies.shutdown_event,
        OwnOnlyContext(account_id=1, personal_channel_id=channel_id),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=30.0),
    )
    repair = ScheduledRepairDemandAdapter(reconciler).status(now)
    discovery = ScheduledDiscoveryDemandAdapter(reconciler).status(now)

    assert repair is not None and repair.release_at == now
    assert discovery is not None and discovery.release_at == now + 86_400


@pytest.mark.asyncio
async def test_linked_chat_adapter_real_gate_cannot_retry_physical_send(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    from telethon.errors import ServerError  # type: ignore[import-untyped]
    from telethon.tl.types import InputPeerChannel, UpdateChannel  # type: ignore[import-untyped]

    from mcp_telegram.event_handlers import EventHandlerManager
    from tests.test_telegram_rpc import _gate, _set_sender

    dependencies, _objects = composition_dependencies
    gate = _gate(retry_delays=(0.0,))
    physical_scopes = []
    physical_attempts = 0
    gate.set_rpc_request_observer(
        lambda **kwargs: physical_scopes.append((kwargs["source"], kwargs["demand_kind"], kwargs["acquisition_kind"]))
    )

    def send(_request: object) -> object:
        nonlocal physical_attempts
        physical_attempts += 1
        raise ServerError(None, "transient")

    _set_sender(gate, send)

    dialog_id = -1000000000123
    dependencies.conn.execute(
        "INSERT INTO dialogs (dialog_id,type,linked_chat_id,linked_chat_resolved_at) VALUES (?,'channel',NULL,100)",
        (dialog_id,),
    )
    dependencies.conn.execute("INSERT INTO synced_dialogs (dialog_id,status) VALUES (?,'synced')", (dialog_id,))
    dependencies.conn.commit()

    class _GateClient:
        session = type(
            "Session",
            (),
            {"get_input_entity": lambda _self, _peer: InputPeerChannel(channel_id=123, access_hash=1)},
        )()

        async def __call__(self, request: object) -> object:
            return await gate(request)

    client = _GateClient()
    manager = EventHandlerManager(client, dependencies.conn, dependencies.shutdown_event)  # type: ignore[arg-type]
    manager._synced_dialog_ids.add(dialog_id)

    class _RecordingSink:
        def __init__(self) -> None:
            self.offered: list[DemandKind] = []

        def offer(self, kind: DemandKind) -> bool:
            self.offered.append(kind)
            return True

    sink = _RecordingSink()
    manager.bind_demand_sink(sink)
    await manager.on_raw_channel_chat_update(UpdateChannel(channel_id=123))
    assert DemandKind.LINKED_CHAT_REFRESH in sink.offered

    adapter = LinkedChatFactDemandAdapter(manager, dependencies.conn)
    budget = RpcAttemptBudget(limit=1)
    await adapter.run_slice(budget)

    assert physical_attempts == 1
    assert budget.attempts == 1
    assert physical_scopes == [
        (
            TelegramRpcSource.LINKED_CHAT_REFRESH,
            DemandKind.LINKED_CHAT_REFRESH,
            AcquisitionKind.LINKED_CHAT_RESOLUTION,
        )
    ]


@pytest.mark.asyncio
async def test_composition_builds_executable_coordinator(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, _objects = composition_dependencies
    coordinator = build_durable_coordinator(dependencies)

    assert coordinator.state.value == "new"


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
