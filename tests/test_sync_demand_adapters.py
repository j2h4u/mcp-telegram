"""Durable demand adapters for history, delta, and dialog synchronization."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import ChannelPrivateError, RPCError  # type: ignore[import-untyped]
from telethon.tl import types

from helpers import MockTotalList, build_mock_message
from mcp_telegram.delta_sync import (
    AccessProbePolicy,
    DeltaAccessProbeDemandAdapter,
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
    _DeltaSyncClient,
)
from mcp_telegram.dialog_sync import (
    DialogBootstrapDemandAdapter,
    DialogFullReconciliationDemandAdapter,
    DialogLightReconciliationDemandAdapter,
    DialogReconciliationWorker,
)
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncDmEnrollmentDemandAdapter, FullSyncWorker
from mcp_telegram.telegram_demand import AcquisitionKind, RpcAttemptBudget, RpcAttemptBudgetExhaustedError
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcScope,
    current_rpc_scope,
)
from tests.history_enrollment_helpers import seed_full_history_enrollment


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    return path


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = _open_sync_db(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _seed_history_dialog(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    status: str,
    last_synced_at: int | None = None,
    last_delta_checked_at: int | None = None,
    refresh_requested_at: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, last_synced_at, last_delta_checked_at, delta_refresh_requested_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (dialog_id, status, last_synced_at, last_delta_checked_at, refresh_requested_at),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=True)
    conn.commit()


def _delta_policy() -> AccessProbePolicy:
    return AccessProbePolicy(
        interval_seconds=86_400.0,
        max_dialogs_per_cycle=1,
        cooldown_seconds=100,
        probe_pause_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_full_sync_adapter_status_is_read_only_and_slice_has_precise_scope(conn: sqlite3.Connection) -> None:
    dialog_id = 101
    _seed_history_dialog(conn, dialog_id, status="syncing")
    observed_scopes: list[TelegramRpcScope] = []

    async def get_messages(**_kwargs: object) -> MockTotalList:
        observed_scopes.append(current_rpc_scope())
        return MockTotalList([], total=0)

    client = SimpleNamespace(get_messages=get_messages)
    worker = FullSyncWorker(client, conn, asyncio.Event())
    adapter = FullSyncDemandAdapter(worker)
    changes_before = conn.total_changes

    assert adapter.status(500.0) is not None
    assert conn.total_changes == changes_before

    budget = RpcAttemptBudget(limit=1)
    await adapter.run_slice(budget)
    scope = observed_scopes[0]
    assert scope.demand_kind is DemandKind.FULL_SYNC_PAGE
    assert scope.acquisition_kind is AcquisitionKind.MESSAGE_HISTORY_PAGE
    assert scope.attempt_budget is budget
    assert adapter.status(500.0) is None


@pytest.mark.asyncio
async def test_dm_enrollment_adapter_resumes_committed_cursor_with_distinct_root(conn: sqlite3.Connection) -> None:
    user = types.User(id=111, first_name="Alice", access_hash=999)
    dialog = SimpleNamespace(
        id=111,
        entity=user,
        message=SimpleNamespace(id=777),
        date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    observed_options: list[dict[str, object]] = []
    observed_scopes: list[TelegramRpcScope] = []

    async def iter_dialogs(**kwargs: object) -> AsyncIterator[object]:
        observed_options.append(kwargs)
        observed_scopes.append(current_rpc_scope())
        if len(observed_options) == 1:
            yield dialog
            raise RpcAttemptBudgetExhaustedError("slice complete")

    worker = FullSyncWorker(SimpleNamespace(iter_dialogs=iter_dialogs), conn, asyncio.Event())
    adapter = FullSyncDmEnrollmentDemandAdapter(worker)
    changes_before = conn.total_changes
    assert adapter.status(10.0) is not None
    assert conn.total_changes == changes_before

    first_budget = RpcAttemptBudget(limit=32)
    await adapter.run_slice(first_budget)

    assert adapter.status(10.0) is not None
    assert observed_options[0] == {}
    assert observed_scopes[0].demand_kind is DemandKind.FULL_SYNC_DM_ENROLLMENT
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.DIALOG_TRAVERSAL
    assert observed_scopes[0].attempt_budget is first_budget
    assert conn.execute(
        "SELECT value FROM daemon_state WHERE key = 'full_sync_dm_enrollment_offset_id'"
    ).fetchone() == ("777",)

    restarted_adapter = FullSyncDmEnrollmentDemandAdapter(
        FullSyncWorker(SimpleNamespace(iter_dialogs=iter_dialogs), conn, asyncio.Event())
    )
    await restarted_adapter.run_slice(RpcAttemptBudget(limit=32))

    assert observed_options[1]["offset_id"] == 777
    assert isinstance(observed_options[1]["offset_peer"], types.InputPeerUser)
    assert observed_options[1]["ignore_pinned"] is True
    assert restarted_adapter.status(10.0) is None


@pytest.mark.asyncio
async def test_dm_enrollment_adapter_propagates_rpc_failure_without_completing_cycle(
    conn: sqlite3.Connection,
) -> None:
    async def iter_dialogs(**_kwargs: object) -> AsyncIterator[object]:
        raise RPCError(None, "enrollment failed")
        yield  # pragma: no cover

    worker = FullSyncWorker(SimpleNamespace(iter_dialogs=iter_dialogs), conn, asyncio.Event())
    adapter = FullSyncDmEnrollmentDemandAdapter(worker)

    with pytest.raises(RPCError, match="enrollment failed"):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute(
        "SELECT value FROM daemon_state WHERE key='full_sync_dm_enrollment_status'"
    ).fetchone() == ("in_progress",)


@pytest.mark.asyncio
async def test_dm_enrollment_adapter_defers_to_coordinator_without_local_retry(conn: sqlite3.Connection) -> None:
    async def iter_dialogs(**_kwargs: object) -> AsyncIterator[object]:
        raise TelegramRpcAdmissionDeferred(retry_after_seconds=7)
        yield  # pragma: no cover

    adapter = FullSyncDmEnrollmentDemandAdapter(
        FullSyncWorker(SimpleNamespace(iter_dialogs=iter_dialogs), conn, asyncio.Event())
    )
    with (
        patch("mcp_telegram.sync_worker.sleep_through_flood", new=AsyncMock()) as sleep,
        pytest.raises(TelegramRpcAdmissionDeferred),
    ):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    sleep.assert_not_awaited()


def test_delta_gap_status_uses_refresh_or_recency_boundary_without_writes(conn: sqlite3.Connection) -> None:
    _seed_history_dialog(conn, 201, status="synced", last_delta_checked_at=100)
    worker = DeltaSyncWorker(cast(_DeltaSyncClient, SimpleNamespace()), conn, asyncio.Event())
    adapter = DeltaGapFillDemandAdapter(worker)
    changes_before = conn.total_changes

    assert adapter.status(50.0).release_at == 3700.0  # type: ignore[union-attr]
    assert conn.total_changes == changes_before

    conn.execute("UPDATE synced_dialogs SET delta_refresh_requested_at = 75 WHERE dialog_id = 201")
    conn.commit()
    assert adapter.status(50.0).release_at == 75.0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_delta_gap_slice_commits_one_page_and_keeps_durable_continuation(
    conn: sqlite3.Connection,
) -> None:
    dialog_id = 202
    _seed_history_dialog(conn, dialog_id, status="synced", refresh_requested_at=1)
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, 100, 1, 'baseline')",
        (dialog_id,),
    )
    conn.commit()
    pages = [[build_mock_message(id=message_id, text=str(message_id)) for message_id in range(101, 201)], []]
    observed_scopes: list[TelegramRpcScope] = []
    observed_limits: list[object] = []

    async def iter_messages(**kwargs: object) -> AsyncIterator[object]:
        observed_scopes.append(current_rpc_scope())
        observed_limits.append(kwargs["limit"])
        for message in pages.pop(0):
            yield message

    worker = DeltaSyncWorker(
        cast(_DeltaSyncClient, SimpleNamespace(iter_messages=iter_messages)), conn, asyncio.Event()
    )
    adapter = DeltaGapFillDemandAdapter(worker)
    budget = RpcAttemptBudget(limit=1)

    await adapter.run_slice(budget)

    assert conn.execute("SELECT MAX(message_id) FROM messages WHERE dialog_id = ?", (dialog_id,)).fetchone() == (200,)
    assert (
        conn.execute(
            "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone()[0]
        is not None
    )
    assert adapter.status(10_000.0).is_ready(10_000.0)  # type: ignore[union-attr]
    assert observed_limits == [100]
    assert observed_scopes[0].demand_kind is DemandKind.DELTA_GAP_FILL
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.MESSAGE_HISTORY_PAGE
    assert observed_scopes[0].attempt_budget is budget

    await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert (
        conn.execute(
            "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone()[0]
        is None
    )


@pytest.mark.asyncio
async def test_delta_gap_adapter_resumes_dm_tombstone_cursor_after_restart(conn: sqlite3.Connection) -> None:
    dialog_id = 203
    _seed_history_dialog(conn, dialog_id, status="synced", last_delta_checked_at=100)
    conn.execute("INSERT INTO entities (id, type, updated_at) VALUES (?, 'user', 1)", (dialog_id,))
    conn.executemany(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, ?, 1, 'message')",
        ((dialog_id, message_id) for message_id in range(1, 102)),
    )
    conn.commit()

    class Scanner:
        def __init__(self) -> None:
            self.pages: list[tuple[int, ...]] = []

        async def run_dm_gap_scan_page(self, dialog_id: int, message_ids: Sequence[int]) -> int:
            del dialog_id
            self.pages.append(tuple(message_ids))
            return 0

    scanner = Scanner()
    worker = DeltaSyncWorker(cast(_DeltaSyncClient, SimpleNamespace()), conn, asyncio.Event())
    adapter = DeltaGapFillDemandAdapter(worker, scanner)

    with patch("mcp_telegram.delta_sync.time.time", return_value=1000):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert len(scanner.pages) == 1
    assert scanner.pages[0] == tuple(range(1, 101))
    state = cast(
        tuple[str], conn.execute("SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'").fetchone()
    )[0]
    assert '"message_cursor": 100' in state

    restarted = DeltaGapFillDemandAdapter(worker, scanner)
    with patch("mcp_telegram.delta_sync.time.time", return_value=1000):
        await restarted.run_slice(RpcAttemptBudget(limit=1))
    assert scanner.pages[1] == (101,)


@pytest.mark.asyncio
async def test_delta_gap_tombstone_cursor_does_not_skip_after_earlier_dialog_is_deleted(
    conn: sqlite3.Connection,
) -> None:
    for dialog_id in (203, 204, 205):
        _seed_history_dialog(conn, dialog_id, status="synced", last_delta_checked_at=100)
        conn.execute("INSERT INTO entities (id, type, updated_at) VALUES (?, 'user', 1)", (dialog_id,))
        conn.execute(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, 1, 1, 'message')",
            (dialog_id,),
        )
    conn.commit()

    class Scanner:
        def __init__(self) -> None:
            self.dialog_ids: list[int] = []

        async def run_dm_gap_scan_page(self, dialog_id: int, message_ids: Sequence[int]) -> int:
            assert message_ids == (1,)
            self.dialog_ids.append(dialog_id)
            return 0

    scanner = Scanner()
    worker = DeltaSyncWorker(cast(_DeltaSyncClient, SimpleNamespace()), conn, asyncio.Event())
    adapter = DeltaGapFillDemandAdapter(worker, scanner)

    with patch("mcp_telegram.delta_sync.time.time", return_value=1000):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert scanner.dialog_ids == [203]

    conn.execute("DELETE FROM synced_dialogs WHERE dialog_id=203")
    conn.commit()
    restarted = DeltaGapFillDemandAdapter(worker, scanner)
    with patch("mcp_telegram.delta_sync.time.time", return_value=1000):
        await restarted.run_slice(RpcAttemptBudget(limit=1))

    assert scanner.dialog_ids == [203, 204]
    raw_state = cast(
        tuple[str], conn.execute("SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'").fetchone()
    )[0]
    state = cast(dict[str, object], json.loads(raw_state))
    assert state["dialog_id_cursor"] == 204
    assert "dialog_index" not in state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type",
    [TelegramRpcAdmissionDeferred, TelegramRpcThrottled, RpcAdmissionClosedError, RPCError],
)
async def test_delta_gap_adapter_propagates_coordinator_outcomes_without_checkpoint(
    conn: sqlite3.Connection,
    failure_type: type[BaseException],
) -> None:
    dialog_id = 206
    _seed_history_dialog(conn, dialog_id, status="synced", refresh_requested_at=1)
    conn.execute("INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, 10, 1, 'baseline')", (dialog_id,))
    conn.commit()

    async def iter_messages(**_kwargs: object) -> AsyncIterator[object]:
        if failure_type is TelegramRpcAdmissionDeferred:
            raise TelegramRpcAdmissionDeferred(retry_after_seconds=7)
        if failure_type is TelegramRpcThrottled:
            raise TelegramRpcThrottled(retry_after_seconds=11)
        if failure_type is RpcAdmissionClosedError:
            raise RpcAdmissionClosedError(current_rpc_scope(), "scheduler closed")
        raise RPCError(None, "delta failed")
        yield  # pragma: no cover

    adapter = DeltaGapFillDemandAdapter(
        DeltaSyncWorker(cast(_DeltaSyncClient, SimpleNamespace(iter_messages=iter_messages)), conn, asyncio.Event())
    )

    with pytest.raises(failure_type):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute(
        "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone() == (1, None)


@pytest.mark.asyncio
async def test_full_sync_total_repair_uses_durable_retry_boundary(conn: sqlite3.Connection) -> None:
    dialog_id = 204
    _seed_history_dialog(conn, dialog_id, status="synced")
    conn.execute("UPDATE synced_dialogs SET total_messages=NULL WHERE dialog_id=?", (dialog_id,))
    conn.commit()

    calls = 0

    async def get_messages(**_kwargs: object) -> MockTotalList:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary Telegram failure")
        return MockTotalList([], total=17)

    worker = FullSyncWorker(SimpleNamespace(get_messages=get_messages), conn, asyncio.Event())
    adapter = FullSyncDemandAdapter(worker)
    with patch("mcp_telegram.sync_worker.time.time", return_value=1000):
        with pytest.raises(OSError, match="temporary Telegram failure"):
            await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert calls == 1
    assert conn.execute(
        "SELECT value FROM daemon_state WHERE key='full_sync_total_messages_repair_retry_at'"
    ).fetchone() == ("1060",)

    with patch("mcp_telegram.sync_worker.time.time", return_value=1001):
        assert adapter.status(1001.0).release_at == 1060.0  # type: ignore[union-attr]
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert calls == 1

    with patch("mcp_telegram.sync_worker.time.time", return_value=1060):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert calls == 2
    assert conn.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (17,)


@pytest.mark.asyncio
async def test_full_sync_page_adapter_propagates_rpc_failure_and_preserves_progress(conn: sqlite3.Connection) -> None:
    dialog_id = 207
    _seed_history_dialog(conn, dialog_id, status="syncing")
    conn.execute("UPDATE synced_dialogs SET sync_progress=77 WHERE dialog_id=?", (dialog_id,))
    conn.commit()

    async def get_messages(**_kwargs: object) -> MockTotalList:
        raise RPCError(None, "history failed")

    adapter = FullSyncDemandAdapter(FullSyncWorker(SimpleNamespace(get_messages=get_messages), conn, asyncio.Event()))
    with pytest.raises(RPCError, match="history failed"):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute("SELECT status, sync_progress FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        "syncing",
        77,
    )


@pytest.mark.asyncio
async def test_full_sync_page_adapter_defers_to_coordinator_without_local_sleep(conn: sqlite3.Connection) -> None:
    dialog_id = 208
    _seed_history_dialog(conn, dialog_id, status="syncing")

    async def get_messages(**_kwargs: object) -> MockTotalList:
        raise TelegramRpcAdmissionDeferred(retry_after_seconds=9)

    adapter = FullSyncDemandAdapter(FullSyncWorker(SimpleNamespace(get_messages=get_messages), conn, asyncio.Event()))
    with (
        patch("mcp_telegram.sync_worker.sleep_through_flood", new=AsyncMock()) as sleep,
        pytest.raises(TelegramRpcAdmissionDeferred),
    ):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_probe_adapter_restores_non_enrolled_peer_with_precise_scope(conn: sqlite3.Connection) -> None:
    dialog_id = 301
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1)",
        (dialog_id,),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=False)
    conn.commit()
    observed_scopes: list[TelegramRpcScope] = []

    async def get_messages(**_kwargs: object) -> MockTotalList:
        observed_scopes.append(current_rpc_scope())
        return MockTotalList([], total=0)

    client = cast(_DeltaSyncClient, SimpleNamespace(get_messages=get_messages))
    worker = DeltaSyncWorker(client, conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy())
    changes_before = conn.total_changes

    assert adapter.status(500.0).release_at == 101.0  # type: ignore[union-attr]
    assert conn.total_changes == changes_before
    budget = RpcAttemptBudget(limit=1)
    await adapter.run_slice(budget)

    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone() != (
        "access_lost",
    )
    assert observed_scopes[0].demand_kind is DemandKind.DELTA_ACCESS_PROBE
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.MESSAGE_LOOKUP
    assert observed_scopes[0].attempt_budget is budget


@pytest.mark.asyncio
async def test_access_probe_adapter_persists_probe_to_gap_fill_handoff(conn: sqlite3.Connection) -> None:
    dialog_id = 302
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', 1)",
        (dialog_id,),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=True)
    conn.commit()
    get_messages = AsyncMock(return_value=MockTotalList([], total=12))

    async def iter_messages(**_kwargs: object) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover

    worker = DeltaSyncWorker(
        cast(_DeltaSyncClient, SimpleNamespace(get_messages=get_messages, iter_messages=iter_messages)),
        conn,
        asyncio.Event(),
    )
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy())

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    get_messages.assert_awaited_once()
    assert conn.execute(
        "SELECT stage, total_messages FROM delta_access_recovery_state WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == ("gap_fill", 12)
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        "access_lost",
    )

    restarted = DeltaAccessProbeDemandAdapter(worker, _delta_policy())
    await restarted.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute("SELECT 1 FROM delta_access_recovery_state WHERE dialog_id=?", (dialog_id,)).fetchone() is None
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == ("syncing",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_kind", "expected_retry"),
    [
        ("deferred", 1007),
        ("saturated", None),
        ("expired", None),
        ("access", 1100),
        ("flood", 1200),
        ("network", 1100),
        ("rpc", 1100),
    ],
)
async def test_access_probe_slice_records_retry_policy_for_probe_outcomes(
    conn: sqlite3.Connection,
    error_kind: str,
    expected_retry: int | None,
) -> None:
    """Probe failures preserve access_lost and apply their documented retry policy."""
    dialog_id = 303
    _seed_history_dialog(conn, dialog_id, status="access_lost")
    get_messages = AsyncMock()

    async def fail(**_kwargs: object) -> object:
        if error_kind == "deferred":
            raise TelegramRpcAdmissionDeferred(retry_after_seconds=7)
        if error_kind == "saturated":
            raise RpcAdmissionSaturatedError(current_rpc_scope(), "probe capacity is full")
        if error_kind == "expired":
            raise RpcAdmissionExpiredError(current_rpc_scope(), "probe deadline elapsed")
        if error_kind == "access":
            raise ChannelPrivateError(request=None)
        if error_kind == "flood":
            raise TelegramRpcThrottled(retry_after_seconds=200)
        if error_kind == "network":
            raise OSError("probe connection reset")
        raise RPCError(None, "probe RPC failed")

    get_messages.side_effect = fail
    worker = DeltaSyncWorker(cast(_DeltaSyncClient, SimpleNamespace(get_messages=get_messages)), conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy())

    with patch("mcp_telegram.delta_sync.time.time", return_value=1000):
        await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        "access_lost",
    )
    assert conn.execute(
        "SELECT access_next_revalidate_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == (expected_retry,)
    assert conn.execute("SELECT 1 FROM delta_access_recovery_state WHERE dialog_id=?", (dialog_id,)).fetchone() is None


@pytest.mark.asyncio
async def test_dialog_bootstrap_adapter_status_and_slice_use_committed_state(
    conn: sqlite3.Connection,
    db_path: Path,
) -> None:
    observed_scopes: list[TelegramRpcScope] = []

    async def iter_dialogs(**_kwargs: object) -> AsyncIterator[object]:
        observed_scopes.append(current_rpc_scope())
        return
        yield  # pragma: no cover

    adapter = DialogBootstrapDemandAdapter(
        SimpleNamespace(iter_dialogs=iter_dialogs),
        conn,
        db_path,
        asyncio.Event(),
    )
    changes_before = conn.total_changes
    assert adapter.status(10.0) is not None
    assert conn.total_changes == changes_before

    budget = RpcAttemptBudget(limit=32)
    await adapter.run_slice(budget)

    assert adapter.status(10.0) is None
    assert observed_scopes[0].demand_kind is DemandKind.DIALOG_BOOTSTRAP
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.DIALOG_TRAVERSAL
    assert observed_scopes[0].attempt_budget is budget


@pytest.mark.asyncio
async def test_dialog_bootstrap_adapter_propagates_rpc_failure_and_preserves_in_progress_state(
    conn: sqlite3.Connection,
    db_path: Path,
) -> None:
    async def iter_dialogs(**_kwargs: object) -> AsyncIterator[object]:
        raise RPCError(None, "bootstrap failed")
        yield  # pragma: no cover

    adapter = DialogBootstrapDemandAdapter(
        SimpleNamespace(iter_dialogs=iter_dialogs),
        conn,
        db_path,
        asyncio.Event(),
    )

    with pytest.raises(RPCError, match="bootstrap failed"):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute("SELECT value FROM daemon_state WHERE key='bootstrap_sweep_status'").fetchone() == (
        "in_progress",
    )


@pytest.mark.asyncio
async def test_dialog_bootstrap_adapter_defers_to_coordinator_without_local_retry(
    conn: sqlite3.Connection,
    db_path: Path,
) -> None:
    async def iter_dialogs(**_kwargs: object) -> AsyncIterator[object]:
        raise TelegramRpcAdmissionDeferred(retry_after_seconds=5)
        yield  # pragma: no cover

    adapter = DialogBootstrapDemandAdapter(
        SimpleNamespace(iter_dialogs=iter_dialogs),
        conn,
        db_path,
        asyncio.Event(),
    )
    with (
        patch("mcp_telegram.dialog_sync.sleep_through_flood", new=AsyncMock()) as sleep,
        pytest.raises(TelegramRpcAdmissionDeferred),
    ):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_dialog_light_adapter_clears_one_durable_dirty_flag(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO dialogs (dialog_id, name, type, hidden, needs_refresh, snapshot_at) "
        "VALUES (401, 'old', 'user', 0, 1, 1)"
    )
    conn.commit()
    observed_scopes: list[TelegramRpcScope] = []

    async def get_entity(_dialog_id: int) -> object:
        observed_scopes.append(current_rpc_scope())
        return SimpleNamespace(
            id=401,
            first_name="new",
            last_name=None,
            title=None,
            username=None,
            access_hash=1,
            bot=False,
            broadcast=False,
            date=None,
            forum=False,
        )

    worker = DialogReconciliationWorker(SimpleNamespace(get_entity=get_entity), conn, asyncio.Event())
    adapter = DialogLightReconciliationDemandAdapter(worker)
    changes_before = conn.total_changes
    assert adapter.status(10.0) is not None
    assert conn.total_changes == changes_before
    budget = RpcAttemptBudget(limit=8)

    await adapter.run_slice(budget)

    assert adapter.status(10.0) is None
    assert observed_scopes[0].demand_kind is DemandKind.DIALOG_LIGHT_RECONCILIATION
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.ENTITY_LOOKUP
    assert observed_scopes[0].attempt_budget is budget


@pytest.mark.asyncio
async def test_dialog_full_adapter_completes_generation_under_precise_scope(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO daemon_state (key, value) VALUES ('dialog_reconciliation_last_full_at', '100')")
    conn.commit()
    observed_scopes: list[TelegramRpcScope] = []

    async def iter_dialogs(**_kwargs: object) -> AsyncIterator[object]:
        observed_scopes.append(current_rpc_scope())
        return
        yield  # pragma: no cover

    worker = DialogReconciliationWorker(SimpleNamespace(iter_dialogs=iter_dialogs), conn, asyncio.Event())
    adapter = DialogFullReconciliationDemandAdapter(worker, interval_seconds=50.0)
    changes_before = conn.total_changes

    status = adapter.status(200.0)
    assert status.release_at == 150.0
    assert status.freshness_deadline == 150.0
    assert conn.total_changes == changes_before
    budget = RpcAttemptBudget(limit=32)
    await adapter.run_slice(budget)

    scope = observed_scopes[0]
    assert scope.demand_kind is DemandKind.DIALOG_FULL_RECONCILIATION
    assert scope.acquisition_kind is AcquisitionKind.DIALOG_TRAVERSAL
    assert scope.attempt_budget is budget
    assert conn.execute(
        "SELECT status, generation FROM dialog_full_reconciliation_state WHERE singleton=1"
    ).fetchone() == ("idle", 1)
    assert conn.execute("SELECT value FROM daemon_state WHERE key='dialog_reconciliation_last_full_at'").fetchone() != (
        "100",
    )


@pytest.mark.asyncio
async def test_dialog_full_adapter_propagates_throttle_and_preserves_generation(conn: sqlite3.Connection) -> None:
    async def iter_dialogs(**_kwargs: object) -> AsyncIterator[object]:
        raise TelegramRpcThrottled(retry_after_seconds=13)
        yield  # pragma: no cover

    worker = DialogReconciliationWorker(SimpleNamespace(iter_dialogs=iter_dialogs), conn, asyncio.Event())
    adapter = DialogFullReconciliationDemandAdapter(worker, interval_seconds=50.0)

    with pytest.raises(TelegramRpcThrottled) as caught:
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert caught.value.retry_after_seconds == 13
    assert conn.execute(
        "SELECT status, generation FROM dialog_full_reconciliation_state WHERE singleton=1"
    ).fetchone() == ("in_progress", 1)


@pytest.mark.parametrize("state", ["idle", "in_progress"])
def test_dialog_full_adapter_never_completed_has_no_freshness_debt(
    conn: sqlite3.Connection,
    state: str,
) -> None:
    conn.execute(
        "UPDATE dialog_full_reconciliation_state SET status=? WHERE singleton=1",
        (state,),
    )
    conn.commit()
    worker = DialogReconciliationWorker(SimpleNamespace(iter_dialogs=AsyncMock()), conn, asyncio.Event())
    adapter = DialogFullReconciliationDemandAdapter(worker, interval_seconds=50.0)

    status = adapter.status(1_700_000_000.0)

    assert status.release_at == 0.0
    assert status.freshness_deadline is None
    assert status.is_ready(1_700_000_000.0)
    assert status.overdue_seconds(1_700_000_000.0) == 0.0


@pytest.mark.asyncio
async def test_dialog_full_adapter_resumes_message_cursor_and_preserves_changed_unseen_row(
    conn: sqlite3.Connection,
) -> None:
    cursor_date = datetime(2026, 1, 2, tzinfo=UTC)
    user = types.User(id=401, first_name="Seen", access_hash=999)
    dialog = SimpleNamespace(
        id=401,
        dialog=SimpleNamespace(read_inbox_max_id=None, read_outbox_max_id=None, unread_mark=False),
        entity=user,
        message=SimpleNamespace(id=777, date=cursor_date),
        pinned=False,
        folder_id=None,
        read_inbox_max_id=None,
        read_outbox_max_id=None,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_count=0,
        unread_mark=False,
        draft=None,
        date=cursor_date,
    )
    conn.executemany(
        "INSERT INTO dialogs(dialog_id, name, type, hidden) VALUES (?, ?, 'user', 0)",
        ((401, "Old seen"), (402, "Locally changed")),
    )
    conn.commit()
    observed_options: list[dict[str, object]] = []

    async def iter_dialogs(**kwargs: object) -> AsyncIterator[object]:
        observed_options.append(kwargs)
        if len(observed_options) == 1:
            yield dialog
            raise RpcAttemptBudgetExhaustedError("slice complete")

    worker = DialogReconciliationWorker(SimpleNamespace(iter_dialogs=iter_dialogs), conn, asyncio.Event())
    adapter = DialogFullReconciliationDemandAdapter(worker, interval_seconds=50.0)

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute(
        "SELECT status, offset_id, observed_count FROM dialog_full_reconciliation_state WHERE singleton=1"
    ).fetchone() == ("in_progress", 777, 1)
    conn.execute("UPDATE dialogs SET name='Changed during sweep' WHERE dialog_id=402")
    conn.commit()

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert observed_options[1]["offset_date"] == cursor_date
    assert observed_options[1]["offset_id"] == 777
    assert isinstance(observed_options[1]["offset_peer"], types.InputPeerUser)
    assert conn.execute("SELECT hidden FROM dialogs WHERE dialog_id=402").fetchone() == (0,)
    assert conn.execute(
        "SELECT status, generation FROM dialog_full_reconciliation_state WHERE singleton=1"
    ).fetchone() == ("idle", 1)
