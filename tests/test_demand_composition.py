from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable, Generator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.activity_cold_backfill import (
    ColdBackfillHistoryPacing,
    ColdBackfillPacing,
    ColdPeerPageDemandAdapter,
)
from mcp_telegram.activity_hot_sweep import HotActivityDemandAdapter
from mcp_telegram.activity_sync import ArchiveBackfillDemandAdapter, ArchiveIncrementalDemandAdapter
from mcp_telegram.daemon import SQLiteSelfProfileCadence
from mcp_telegram.delta_sync import (
    DeltaAccessProbeDemandAdapter,
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
    DmDeletionReconciliationDemandAdapter,
    DmGapScanPage,
    prepare_dm_deletion_reconciliation,
)
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
from mcp_telegram.message_history.contracts import ForwardGapPage
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
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.startup_identity import StartupIdentityState
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncDmEnrollmentDemandAdapter
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    current_demand_token,
)
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkRevision
from mcp_telegram.telegram_rpc_consumers import (
    DemandKind,
    TelegramRpcSource,
    demand_freshness_seconds,
)
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope, rpc_scope
from mcp_telegram.topics.demand import TopicSnapshotDemandAdapter
from tests.helpers import LoudUserProfilePort, build_mock_message
from tests.sqlite_writer_helpers import inline_writer


@dataclass(frozen=True, slots=True)
class _FolderPolicy:
    refresh_interval_seconds: float = 60.0
    jitter_ratio: float = 0.0
    retry_delays_seconds: tuple[int, ...] = (1,)
    retry_cap_seconds: int = 60
    warning_failure_threshold: int = 3
    stale_threshold_seconds: int = 300


class _IdleFolderRepository(FolderSnapshotRepository):
    def for_connection(self, conn: sqlite3.Connection) -> FolderSnapshotRepository:
        return self

    def read_consecutive_failures(self) -> int:
        return 0

    def read_last_outcome(self) -> str | None:
        return None

    def read_last_success_at(self) -> int | None:
        return None

    def read_next_retry_at(self) -> int | None:
        return None

    def read_snapshot_revision(self) -> tuple[int | None, int | None, str | None, int | None] | None:
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

    async def retry_one_pending_linked_chat_fact(self, budget: RpcAttemptBudget) -> SliceOutcome:
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
        assert budget.selected is not None
        return SliceOutcome(budget.selected, SliceOutcomeKind.NO_PROGRESS)


def _dependencies(tmp_path: Path) -> tuple[DemandCompositionDependencies, dict[str, object]]:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    folder_repository = _IdleFolderRepository()
    folder_worker = FolderProjectionWorker(
        MagicMock(), folder_repository, asyncio.Event(), _FolderPolicy(), writer=inline_writer(conn)
    )
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
        "directory": CanonicalDialogDirectory(MagicMock(), db_path, asyncio.Event(), writer=inline_writer(conn)),
        "entity": EntityRefreshCoordinator(),
        "hydration": MagicMock(),
        "folder": folder_worker,
        "facts": MessageFactRefreshDeps(conn, MagicMock(), MagicMock(), writer=inline_writer(conn)),
        "fact_policy": message_fact_policy,
        "scheduled": MagicMock(),
        "access_policy": MagicMock(),
        "hot_policy": _HotPolicy(),
        "cold_pacing": ColdBackfillPacing(
            history=ColdBackfillHistoryPacing(batch_s=1.0, enroll_s=60.0, access_retry_s=60.0),
        ),
        "cadence": SQLiteSelfProfileCadence(conn, 60.0, writer=inline_writer(conn)),
        "draft_owner": DraftMessageOwner(
            MagicMock(), MagicMock(), asyncio.Event(), UpdateProcessingBarrier(closed=True), writer=inline_writer(conn)
        ),
        "linked_chat_refresh": _LinkedChatRefresher(),
    }

    async def read_receipt_batch(_selected: WorkRevision) -> SliceOutcome:
        raise AssertionError("read receipt batch is not called by adapter-map tests")

    async def get_self_input_entity(_account_id: int) -> object:
        return object()

    update_profile: Callable[[object], Awaitable[None]] = AsyncMock()
    publish_startup_identity: Callable[[object, OwnOnlyContext], Awaitable[None]] = AsyncMock()
    dependencies = DemandCompositionDependencies(
        client=cast(DemandCompositionClient, objects["client"]),
        conn=cast(sqlite3.Connection, objects["conn"]),
        account_id=1,
        writer=inline_writer(conn),
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
        read_receipt_batch=read_receipt_batch,
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


def test_adapter_map_is_exact_against_literal_23_kind_class_map(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, objects = composition_dependencies

    adapters = build_durable_adapter_map(dependencies)

    expected = {
        DemandKind.ENTITY_PROFILE_REFRESH: EntityProfileDemandAdapter,
        DemandKind.DELTA_GAP_FILL: DeltaGapFillDemandAdapter,
        DemandKind.DM_DELETION_RECONCILIATION: DmDeletionReconciliationDemandAdapter,
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
        DemandKind.TOPIC_SNAPSHOT: TopicSnapshotDemandAdapter,
        DemandKind.MESSAGE_FACT_REFRESH: MessageFactRefreshDemandAdapter,
        DemandKind.READ_RECEIPT_BATCH: ReadReceiptDemandAdapter,
        DemandKind.SCHEDULED_REPAIR: ScheduledRepairDemandAdapter,
        DemandKind.SCHEDULED_DISCOVERY: ScheduledDiscoveryDemandAdapter,
        DemandKind.DRAFT_SNAPSHOT: DraftMessageOwner,
        DemandKind.SELF_PROFILE_MAINTENANCE: SelfProfileMaintenanceDemandAdapter,
        DemandKind.LINKED_CHAT_REFRESH: LinkedChatFactDemandAdapter,
    }
    assert len(expected) == 23
    assert set(adapters) == set(expected)
    assert {kind: type(adapter) for kind, adapter in adapters.items()} == expected
    assert all(getattr(adapter, "demand_kind", None) is kind for kind, adapter in adapters.items())
    assert adapters[DemandKind.FULL_SYNC_PAGE]._worker is objects["full"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.FULL_SYNC_DM_ENROLLMENT]._worker is objects["full"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_GAP_FILL]._worker is objects["delta"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DM_DELETION_RECONCILIATION]._scanner is objects["dm_gap_scanner"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DELTA_ACCESS_PROBE]._worker is objects["delta"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DIALOG_LIGHT_RECONCILIATION]._worker is objects["dialog"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DIALOG_BOOTSTRAP]._directory is objects["directory"]  # type: ignore[attr-defined]
    assert (
        adapters[DemandKind.ARCHIVE_INCREMENTAL].interval_s  # type: ignore[attr-defined]
        == demand_freshness_seconds(DemandKind.ARCHIVE_INCREMENTAL)
    )
    assert adapters[DemandKind.LIVE_HYDRATION_BATCH]._worker is objects["hydration"]  # type: ignore[attr-defined]


def test_bootstrap_quarantines_legacy_dm_generation_before_registration(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, objects = composition_dependencies
    legacy_state = {
        "status": "running",
        "generation": 8,
        "scan_started_at": 100,
        "dialog_id_cursor": 7,
        "message_cursor": 44,
        "next_run_at": 0,
    }
    dependencies.conn.execute(
        "INSERT INTO daemon_state(key, value) VALUES ('delta_dm_gap_scan_state', ?)",
        (json.dumps(legacy_state),),
    )
    dependencies.conn.commit()
    prepare_dm_deletion_reconciliation(dependencies.conn)

    adapters = build_durable_adapter_map(dependencies)

    stored = cast(
        tuple[str],
        dependencies.conn.execute("SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'").fetchone(),
    )
    quarantined = cast(dict[str, object], json.loads(stored[0]))
    assert {key: quarantined[key] for key in legacy_state} == {**legacy_state, "status": "suspended"}
    assert quarantined["policy_version"] == 1
    assert quarantined["reason"] == "legacy_generation_review"
    assert isinstance(quarantined["state_changed_at"], int)
    assert adapters[DemandKind.DM_DELETION_RECONCILIATION].status(1_000_000.0) is None
    assert adapters[DemandKind.BACKFILL_HYDRATION_BATCH]._worker is objects["hydration"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.FOLDER_SNAPSHOT]._worker is objects["folder"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.SCHEDULED_REPAIR]._reconciler is objects["scheduled"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.SCHEDULED_DISCOVERY]._reconciler is objects["scheduled"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DRAFT_SNAPSHOT] is objects["draft_owner"]
    assert adapters[DemandKind.MESSAGE_FACT_REFRESH]._shutdown_event is objects["shutdown"]  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        adapters[DemandKind.SCHEDULED_REPAIR] = adapters[DemandKind.SCHEDULED_DISCOVERY]  # type: ignore[index]


def test_adapter_composition_contains_invalid_dm_checkpoint_to_its_domain(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, objects = composition_dependencies
    dependencies.conn.execute("INSERT INTO daemon_state(key, value) VALUES ('delta_dm_gap_scan_state', NULL)")
    dependencies.conn.commit()
    before = cast(
        tuple[object, ...] | None,
        dependencies.conn.execute("SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'").fetchone(),
    )

    adapters = build_durable_adapter_map(dependencies)

    assert adapters[DemandKind.DELTA_GAP_FILL]._worker is objects["delta"]  # type: ignore[attr-defined]
    assert adapters[DemandKind.DM_DELETION_RECONCILIATION].status(1_000_000.0) is None
    assert (
        dependencies.conn.execute("SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'").fetchone()
        == before
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delta_cursor", [None, 1])
async def test_invalid_dm_checkpoint_does_not_block_forward_delta_coordinator(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
    delta_cursor: int | None,
) -> None:
    dependencies, objects = composition_dependencies
    dialog_id = 7123
    dependencies.conn.execute("INSERT INTO daemon_state(key, value) VALUES ('delta_dm_gap_scan_state', NULL)")
    dependencies.conn.execute(
        "INSERT INTO synced_dialogs(dialog_id,status,delta_message_id) VALUES (?,'synced',?)",
        (dialog_id, delta_cursor),
    )
    dependencies.conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (?,1,'explicit',0)",
        (dialog_id,),
    )
    dependencies.conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (?,1,1)", (dialog_id,))
    dependencies.conn.commit()

    class EmptyForwardPage:
        async def fetch_page(
            self, dialog_id: int, *, after_message_id: int, should_stop: Callable[[], bool]
        ) -> ForwardGapPage:
            assert after_message_id == (delta_cursor or 0)
            assert not should_stop()
            return ForwardGapPage(messages=(), complete=True)

    worker = DeltaSyncWorker(
        EmptyForwardPage(), dependencies.conn, dependencies.shutdown_event, writer=inline_writer(dependencies.conn)
    )  # type: ignore[arg-type]
    coordinated_dependencies = replace(dependencies, delta_sync_worker=worker)
    coordinator = build_durable_coordinator(coordinated_dependencies)

    assert DemandKind.DELTA_GAP_FILL in coordinator.ready_kinds
    assert DemandKind.DM_DELETION_RECONCILIATION not in coordinator.ready_kinds
    with patch("mcp_telegram.delta_sync.time.time", return_value=1_000):
        await coordinator._execute_slice(DemandKind.DELTA_GAP_FILL)
    coordinator.scan(now=1_000)

    assert DemandKind.DM_DELETION_RECONCILIATION not in coordinator.ready_kinds
    assert coordinator._adapters[DemandKind.DELTA_GAP_FILL].status(1_000).release_at == 8_200  # type: ignore[union-attr]
    assert dependencies.conn.execute(
        "SELECT delta_refresh_requested_at,last_delta_checked_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone() == (None, 1_000)

    assert dependencies.conn.execute(
        "SELECT delta_message_id FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone() == (delta_cursor or 0,)
    assert dependencies.conn.execute(
        "SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'"
    ).fetchone() == (None,)


@pytest.mark.asyncio
async def test_claim_write_failure_preserves_demand_for_reclaim_before_scanner(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, _objects = composition_dependencies
    dialog_id = 7199
    dependencies.conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (?,'synced')", (dialog_id,))
    dependencies.conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (?,1,'explicit',0)",
        (dialog_id,),
    )
    dependencies.conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (?,'user',1)", (dialog_id,))
    dependencies.conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (?,1,1)", (dialog_id,))
    dependencies.conn.execute(
        """CREATE TRIGGER fail_dm_claim
           BEFORE INSERT ON daemon_state
           WHEN NEW.key='delta_dm_gap_scan_state' AND NEW.value LIKE '%\"status\": \"verifying\"%'
           BEGIN SELECT RAISE(ABORT, 'injected DM claim failure'); END"""
    )
    dependencies.conn.commit()
    calls = 0

    class RecordingScanner:
        async def run_dm_gap_scan_page(self, dialog_id: int, message_ids: Sequence[int]) -> int:
            nonlocal calls
            del dialog_id, message_ids
            calls += 1
            row = cast(
                tuple[object, ...] | None,
                dependencies.conn.execute(
                    "SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'"
                ).fetchone(),
            )
            assert row is not None and isinstance(row[0], str) and '"status": "verifying"' in row[0]
            return 0

    composed = replace(dependencies, dm_gap_scanner=RecordingScanner())
    first = TelegramDemandCoordinator(
        build_durable_adapter_map(composed), dependencies.shutdown_event, clock=lambda: 1_000
    )
    kind = DemandKind.DM_DELETION_RECONCILIATION
    assert kind in first.ready_kinds
    await first._execute_slice(kind)
    first._active_kind = None
    assert calls == 0
    assert (
        '"status": "running"'
        in dependencies.conn.execute("SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'").fetchone()[0]
    )

    dependencies.conn.execute("DROP TRIGGER fail_dm_claim")
    dependencies.conn.commit()
    recovered = TelegramDemandCoordinator(
        build_durable_adapter_map(composed), dependencies.shutdown_event, clock=lambda: 1_120
    )
    assert kind in recovered.ready_kinds
    await recovered._execute_slice(kind)
    assert calls == 1


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
    assert status is not None and status.release_at == 400 and status.selected is not None

    budget = RpcAttemptBudget(limit=1, selected=status.selected)
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
        writer=inline_writer(  # type: ignore[arg-type]
            dependencies.conn
        ),
    )
    repair = ScheduledRepairDemandAdapter(reconciler, account_id=1).status(now)
    discovery = ScheduledDiscoveryDemandAdapter(reconciler, account_id=1).status(now)

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
    dependencies.conn.execute("INSERT INTO daemon_state(key,value) VALUES ('delta_dm_gap_scan_state', NULL)")
    dependencies.conn.commit()
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
    manager = EventHandlerManager(
        client,  # type: ignore[arg-type]
        dependencies.conn,
        dependencies.shutdown_event,
        writer=inline_writer(dependencies.conn),
        profile_detail_ttl_seconds=300,
    )
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

    adapter = LinkedChatFactDemandAdapter(manager, dependencies.conn, account_id=dependencies.account_id)
    status = adapter.status(time.time())
    assert status is not None and status.selected is not None
    budget = RpcAttemptBudget(limit=1, selected=status.selected)
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await adapter.run_slice(budget)

    assert physical_attempts == 1
    assert budget.attempts == 1
    assert dependencies.conn.execute(
        "SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'"
    ).fetchone() == (None,)
    assert physical_scopes == [
        (
            TelegramRpcSource.LINKED_CHAT_REFRESH,
            DemandKind.LINKED_CHAT_REFRESH,
            AcquisitionKind.LINKED_CHAT_RESOLUTION,
        )
    ]


@pytest.mark.asyncio
async def test_malformed_dm_checkpoint_does_not_block_realtime_receive_only_ingestion(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    from telethon.tl import types  # type: ignore[import-untyped]

    from mcp_telegram.demand_wiring import DemandOfferSink
    from mcp_telegram.event_handlers import EventHandlerManager, _NewMessageEvent

    dependencies, _objects = composition_dependencies
    dialog_id = 7188
    dependencies.conn.execute("INSERT INTO daemon_state(key,value) VALUES ('delta_dm_gap_scan_state', NULL)")
    dependencies.conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (?,'synced')", (dialog_id,))
    dependencies.conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (?,1,'explicit',0)",
        (dialog_id,),
    )
    dependencies.conn.commit()
    manager = EventHandlerManager(
        SimpleNamespace(),  # type: ignore[arg-type]
        dependencies.conn,
        dependencies.shutdown_event,
        writer=inline_writer(dependencies.conn),
        profile_detail_ttl_seconds=300,
    )

    class _DemandSink:
        def offer(self, _kind: DemandKind) -> bool:
            return True

    manager.bind_demand_sink(cast(DemandOfferSink, _DemandSink()))
    message = build_mock_message(id=1, text="realtime")
    message.fwd_from = SimpleNamespace(from_name=None, from_id=types.PeerChannel(channel_id=8))
    with rpc_scope(TelegramRpcSource.REALTIME_EVENT):
        await manager.on_new_message(
            cast(_NewMessageEvent, SimpleNamespace(chat_id=dialog_id, message=message, is_private=False))
        )

    assert dependencies.conn.execute(
        "SELECT message_id,text FROM messages WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == (1, "realtime")
    assert dependencies.conn.execute(
        "SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'"
    ).fetchone() == (None,)


@pytest.mark.asyncio
async def test_composition_builds_executable_coordinator(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, _objects = composition_dependencies
    coordinator = build_durable_coordinator(dependencies)

    assert coordinator.state.value == "new"


@pytest.mark.asyncio
async def test_self_profile_cadence_survives_database_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    cadence = SQLiteSelfProfileCadence(conn, 60.0, writer=inline_writer(conn))

    assert cadence.status(100.0).release_at == 0.0
    await cadence.mark_refreshed(100.0)
    conn.close()

    restarted_conn = sqlite3.connect(str(db_path))
    try:
        restarted_cadence = SQLiteSelfProfileCadence(restarted_conn, 60.0, writer=inline_writer(restarted_conn))
        assert restarted_cadence.status(110.0).release_at == 160.0
        assert not restarted_cadence.status(159.0).is_ready(159.0)
        assert restarted_cadence.status(160.0).is_ready(160.0)
    finally:
        restarted_conn.close()


@pytest.mark.asyncio
async def test_adapter_composition_is_readonly_and_shares_writer(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, _objects = composition_dependencies
    writer = await SQLiteWriter.open(dependencies.db_path)
    reader = open_sync_db_reader(dependencies.db_path)
    try:
        adapters = build_durable_adapter_map(replace(dependencies, conn=reader, writer=writer))
        for kind in (
            DemandKind.HOT_ACTIVITY_PAGE,
            DemandKind.ARCHIVE_BACKFILL,
            DemandKind.ARCHIVE_INCREMENTAL,
            DemandKind.COLD_PEER_PAGE,
        ):
            assert cast(HotActivityDemandAdapter, adapters[kind]).writer is writer
        assert (
            cast(DmDeletionReconciliationDemandAdapter, adapters[DemandKind.DM_DELETION_RECONCILIATION])._writer
            is writer
        )
        topic_adapter = cast(TopicSnapshotDemandAdapter, adapters[DemandKind.TOPIC_SNAPSHOT])
        assert topic_adapter._writer is writer
        assert topic_adapter._conn is reader
        changes_before = reader.total_changes
        assert topic_adapter.status(0.0) is None
        assert reader.total_changes == changes_before
        assert reader.execute("SELECT COUNT(*) FROM topic_catalog_state").fetchone()[0] == 0
    finally:
        await writer.aclose()
        reader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate_during_rpc", [False, True])
async def test_linked_chat_coordinator_runs_selected_sibling_and_owns_publication(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
    invalidate_during_rpc: bool,
) -> None:
    from telethon.tl.functions.channels import GetFullChannelRequest  # type: ignore[import-untyped]
    from telethon.tl.types import Channel, ChatPhotoEmpty, InputChannel, InputPeerChannel, PeerChannel  # type: ignore[import-untyped]

    from mcp_telegram.event_handlers import EventHandlerManager
    from mcp_telegram.linked_chat_fact import linked_chat_fact_owner
    from mcp_telegram.sync_transactions import write_transaction

    dependencies, _ = composition_dependencies
    conn = dependencies.conn
    first, second = -1000000000123, -1000000000456
    for dialog_id in (first, second):
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (?,'channel')", (dialog_id,))
        linked_chat_fact_owner.ensure_cold_demand(conn, dialog_id, 1)
    conn.commit()
    requested: list[int] = []

    class Session:
        def get_input_entity(self, peer: object) -> InputPeerChannel:
            channel_id = cast(PeerChannel, peer).channel_id
            return InputPeerChannel(channel_id, 1)

    class Client:
        session = Session()

        async def __call__(self, request: object) -> object:
            channel = cast(GetFullChannelRequest, request).channel
            assert isinstance(channel, InputChannel)
            raw = channel.channel_id
            requested.append(raw)
            if invalidate_during_rpc:

                def invalidate(database: sqlite3.Connection) -> None:
                    with write_transaction(database):
                        linked_chat_fact_owner.invalidate_from_update(database, second, 2)

                await dependencies.writer.run(invalidate)
            return SimpleNamespace(
                full_chat=SimpleNamespace(id=raw, linked_chat_id=None),
                chats=[Channel(id=raw, title="channel", photo=ChatPhotoEmpty(), date=None)],
            )

    manager = EventHandlerManager(
        Client(),  # type: ignore[arg-type]
        conn,
        dependencies.shutdown_event,
        writer=dependencies.writer,
        profile_detail_ttl_seconds=300,
    )
    adapter = LinkedChatFactDemandAdapter(manager, conn, account_id=dependencies.account_id)
    # ORDER BY channel id makes the more negative second peer the first pending row.
    initial = adapter.status(time.time())
    assert initial is not None and initial.selected is not None
    excluded = initial.selected
    sibling = adapter.status(time.time(), excluded=frozenset({excluded}))
    assert sibling is not None and sibling.selected is not None
    # Select the actual second row regardless of the canonical negative-id ordering.
    second = int(sibling.selected.key.target)
    adapters = dict(build_durable_adapter_map(dependencies))
    adapters[DemandKind.LINKED_CHAT_REFRESH] = adapter
    coordinator = TelegramDemandCoordinator(adapters)
    manager.bind_demand_sink(coordinator)
    coordinator._excluded.add(excluded)
    coordinator.scan()
    await coordinator._execute_slice(DemandKind.LINKED_CHAT_REFRESH)
    assert requested == [-second - 1_000_000_000_000]
    row = cast(
        tuple[int, int | None],
        conn.execute(
            "SELECT generation,pending_generation FROM linked_chat_fact_state WHERE channel_id=?", (second,)
        ).fetchone(),
    )
    assert row == ((1, 1) if invalidate_during_rpc else (1, None))
    if invalidate_during_rpc:
        assert sibling.selected in coordinator._excluded
        assert conn.execute("SELECT linked_chat_resolved_at FROM dialogs WHERE dialog_id=?", (second,)).fetchone() == (
            None,
        )
    else:
        assert sibling.selected not in coordinator._invocations
        assert (
            conn.execute("SELECT linked_chat_resolved_at FROM dialogs WHERE dialog_id=?", (second,)).fetchone()[0]
            is not None
        )
    assert conn.execute(
        "SELECT generation,pending_generation FROM linked_chat_fact_state WHERE channel_id=?",
        (int(excluded.key.target),),
    ).fetchone() == (0, 0)
