"""The daemon composes real durable owners before any acquisition can run."""

import sqlite3
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from mcp_telegram.config import SchedulingConfig
from mcp_telegram.daemon import _build_demand_runtime, _HistorySyncRuntime
from mcp_telegram.delta_sync import DeltaSyncWorker
from mcp_telegram.demand_composition import DemandCompositionDependencies
from mcp_telegram.fact_hydration import MessageFactHydrationWorker
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.sync_worker import FullSyncWorker
from mcp_telegram.telegram_demand import DurableDemandAdapter, RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind, WorkRevision
from mcp_telegram.telegram_rpc_consumers import DURABLE_DEMAND_ORDER, DemandKind
from tests.test_daemon import _typed_ctx
from tests.test_demand_composition import composition_dependencies as composition_dependencies


def _seed_dialog(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES (42,'user',0)")


async def _run_local_seed(adapter: DurableDemandAdapter) -> WorkRevision:
    status = adapter.status(100.0)
    assert status is not None and status.selected is not None
    assert status.selected.key.account_id == 77
    assert status.selected.key.target == "seed"
    budget = RpcAttemptBudget(limit=1, selected=status.selected)
    outcome = await adapter.run_slice(budget)
    assert outcome.kind is SliceOutcomeKind.PROGRESSED
    assert outcome.committed_revision is not None
    assert budget.attempts == 0
    return status.selected


async def test_daemon_runtime_binds_gate_and_verified_account_to_real_local_seed(
    composition_dependencies: tuple[DemandCompositionDependencies, dict[str, object]],
) -> None:
    dependencies, _objects = composition_dependencies
    client = cast(MagicMock, dependencies.client)
    cast(MagicMock, client.rpc_eligibility).return_value = None
    cast(MagicMock, client.prepare_durable_retry).return_value = None
    scheduling = SchedulingConfig()
    writer = await SQLiteWriter.open(dependencies.db_path)
    try:
        ctx = _typed_ctx(
            db_path=dependencies.db_path,
            conn=dependencies.conn,
            writer=writer,
            client=client,
            shutdown_event=dependencies.shutdown_event,
            api_server=SimpleNamespace(
                self_id=77,
                _get_entity_info_service=lambda: SimpleNamespace(
                    refresh_coordinator=dependencies.entity_refresh_coordinator,
                ),
            ),
            scheduling=scheduling,
            topic_refresher=None,
            message_fact_refresh_policy=dependencies.message_fact_refresh_policy,
            folder_projection_worker=dependencies.folder_projection_worker,
            fact_hydration_worker=MessageFactHydrationWorker(
                client,
                dependencies.conn,
                dependencies.shutdown_event,
                writer=writer,
                handlers=(),
                interval_seconds=scheduling.fact_hydration.interval_seconds,
                max_requests_per_cycle=scheduling.fact_hydration.max_requests_per_cycle,
                max_jobs_per_cycle=scheduling.fact_hydration.max_jobs_per_cycle,
                retry_delay_seconds=scheduling.fact_hydration.retry_delay_seconds,
                circuit_retry_seconds=scheduling.fact_hydration.circuit_retry_seconds,
                max_attempts=scheduling.fact_hydration.max_attempts,
                pause_between_requests_seconds=scheduling.fact_hydration.pause_between_requests_seconds,
                backfill_debt_limit=scheduling.fact_hydration.backfill_debt_limit,
            ),
            self_profile_cadence=dependencies.self_profile_cadence,
            draft_owner=dependencies.draft_owner,
        )
        history = _HistorySyncRuntime(
            FullSyncWorker(MagicMock(), dependencies.conn, dependencies.shutdown_event, writer=writer),
            DeltaSyncWorker(MagicMock(), dependencies.conn, dependencies.shutdown_event, writer=writer),
            MagicMock(),
        )
        runtime = _build_demand_runtime(ctx, history, dependencies.dialog_directory, dependencies.startup_identity)
        try:
            cast(MagicMock, client.set_rpc_eligibility_observer).assert_called_once_with(
                runtime.coordinator.eligibility_changed
            )
            assert set(runtime.coordinator._adapters) == set(DURABLE_DEMAND_ORDER)
            assert runtime.message_fact_refresh_deps.conn is not dependencies.conn
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                runtime.message_fact_refresh_deps.conn.execute(
                    "INSERT INTO daemon_state(key,value) VALUES ('unowned-test-write','forbidden')"
                )
            runtime.message_fact_refresh_deps.conn.rollback()
            adapter = runtime.coordinator._adapters[DemandKind.SCHEDULED_DISCOVERY]
            initial = await _run_local_seed(adapter)
            assert dependencies.conn.execute("SELECT dialog_id FROM scheduled_reconciliation_state").fetchall() == [
                (77,)
            ]
            assert dependencies.conn.execute(
                "SELECT account_id,directory_generation FROM scheduled_sync_state WHERE key='account'"
            ).fetchone() == (77, None)

            # A later locally observed peer extends the verified account's finite seed.
            await writer.run(_seed_dialog)
            selected = await _run_local_seed(adapter)
            assert selected != initial
            assert dependencies.conn.execute(
                "SELECT dialog_id,coverage_state,baseline_observed_at "
                "FROM scheduled_reconciliation_state ORDER BY dialog_id"
            ).fetchall() == [(42, "unknown", None), (77, "unknown", None)]
            assert dependencies.conn.execute(
                "SELECT account_id FROM scheduled_sync_state WHERE key='account'"
            ).fetchone() == (77,)
            client.assert_not_called()
            cast(MagicMock, client.get_me).assert_not_called()
            cast(MagicMock, client.get_input_entity).assert_not_called()
            cast(MagicMock, client.get_entity).assert_not_called()
        finally:
            runtime.message_fact_refresh_deps.conn.close()
    finally:
        await writer.aclose()
