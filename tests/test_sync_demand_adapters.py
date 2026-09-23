"""Durable demand adapters for history, delta, and dialog synchronization."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import ChannelPrivateError, RPCError  # type: ignore[import-untyped]
from telethon.tl.types import PeerChannel  # type: ignore[import-untyped]

from helpers import MockTotalList, build_mock_message
from mcp_telegram.delta_sync import (
    AccessProbePolicy,
    DeltaAccessProbeDemandAdapter,
    DeltaGapFillDemandAdapter,
    DeltaSyncWorker,
)
from mcp_telegram.dialog_sync import DialogLightReconciliationDemandAdapter, DialogReconciliationWorker
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.message_history.contracts import (
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from mcp_telegram.message_history.ports import ForwardGapPagePort, FullHistoryPagePort
from mcp_telegram.message_history.telegram_adapter import (
    TelethonForwardGapPageAdapter,
    TelethonFullHistoryPageAdapter,
    TelethonHistoryAccessProbe,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncDmEnrollmentDemandAdapter, FullSyncWorker
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
)
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
    worker = FullSyncWorker(TelethonFullHistoryPageAdapter(client), conn, asyncio.Event())
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
async def test_full_history_forward_name_enrichment_keeps_entity_lookup_attribution(
    conn: sqlite3.Connection,
) -> None:
    dialog_id = 102
    _seed_history_dialog(conn, dialog_id, status="syncing")
    observed_scopes: list[TelegramRpcScope] = []
    message = build_mock_message(id=1)
    message.fwd_from = SimpleNamespace(
        from_id=PeerChannel(channel_id=123),
        from_name=None,
        date=None,
        channel_post=None,
    )

    async def get_messages(**_kwargs: object) -> MockTotalList:
        return MockTotalList([message], total=1)

    async def get_entity(_peer: object) -> object:
        observed_scopes.append(current_rpc_scope())
        return SimpleNamespace(title="Forwarded source")

    worker = FullSyncWorker(
        TelethonFullHistoryPageAdapter(
            SimpleNamespace(get_messages=get_messages, get_entity=get_entity),
            entity_lookup_context=lambda: acquisition_context(AcquisitionKind.ENTITY_LOOKUP),
        ),
        conn,
        asyncio.Event(),
    )
    budget = RpcAttemptBudget(limit=1)
    await FullSyncDemandAdapter(worker).run_slice(budget)

    assert observed_scopes[0].demand_kind is DemandKind.FULL_SYNC_PAGE
    assert observed_scopes[0].acquisition_kind is AcquisitionKind.ENTITY_LOOKUP
    assert observed_scopes[0].attempt_budget is budget


def _publish_generation(conn: sqlite3.Connection, generation: int = 1) -> None:
    conn.execute("UPDATE dialog_directory_publication SET generation=?", (generation,))
    conn.commit()


def test_dm_enrollment_source_has_no_telegram_dialog_traversal() -> None:
    source = Path(__file__).parents[1].joinpath("src/mcp_telegram/sync_worker.py").read_text(encoding="utf-8")
    assert "iter_dialogs" not in source
    assert "GetDialogsRequest" not in source


@pytest.mark.asyncio
async def test_dm_enrollment_consumes_completed_publication_locally_and_idempotently(
    conn: sqlite3.Connection,
) -> None:
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,type,name,read_inbox_max_id,read_outbox_max_id,identity_complete) VALUES (?,?,?,?,?,1)",
        [(111, "user", "Alice", 17, None), (112, "bot", "Helper", None, 23), (113, "group", "Group", 99, 99)],
    )
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (114,'access_lost')")
    seed_full_history_enrollment(conn, 114, enabled=True, source="automatic")
    conn.execute(
        "INSERT INTO dialogs(dialog_id,type,name,read_inbox_max_id,read_outbox_max_id,identity_complete) VALUES (114,'user','Lost',31,41,1)"
    )
    seed_full_history_enrollment(conn, 112, enabled=False, source="explicit")
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (112,'not_synced')")
    conn.execute(
        "INSERT INTO entities(id,type,name,username,name_normalized,updated_at) VALUES (111,'user','Richer','richer','richer',100)"
    )
    _publish_generation(conn)

    class NoTelegramCalls:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unexpected Telegram method: {name}")

    worker = FullSyncWorker(cast(FullHistoryPagePort, NoTelegramCalls()), conn, asyncio.Event())
    adapter = FullSyncDmEnrollmentDemandAdapter(worker)
    assert adapter.status(10.0) is not None
    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute(
        "SELECT dialog_id,status,read_inbox_max_id,read_outbox_max_id FROM synced_dialogs ORDER BY dialog_id"
    ).fetchall() == [
        (111, "syncing", 17, None),
        (112, "not_synced", None, 23),
        (114, "access_lost", 31, 41),
    ]
    assert conn.execute("SELECT dialog_id FROM full_history_enrollment ORDER BY dialog_id").fetchall() == [
        (111,),
        (112,),
        (114,),
    ]
    assert conn.execute("SELECT name,username FROM entities WHERE id=111").fetchone() == ("Richer", "richer")
    assert conn.execute("SELECT id,type,name FROM entities WHERE id IN (112,113,114) ORDER BY id").fetchall() == [
        (112, "bot", "Helper"),
        (114, "user", "Lost"),
    ]
    assert conn.execute(
        "SELECT value FROM daemon_state WHERE key='full_sync_dm_enrollment_last_publication_generation'"
    ).fetchone() == ("1",)
    assert adapter.status(10.0) is None
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute(
        "SELECT value FROM daemon_state WHERE key='full_sync_dm_enrollment_last_publication_generation'"
    ).fetchone() == ("1",)


def test_dm_enrollment_waits_for_a_completed_publication(conn: sqlite3.Connection) -> None:
    worker = FullSyncWorker(cast(FullHistoryPagePort, object()), conn, asyncio.Event())
    adapter = FullSyncDmEnrollmentDemandAdapter(worker)
    assert adapter.status(10.0) is None
    conn.execute("UPDATE dialog_directory_state SET status='in_progress'")
    conn.commit()
    assert adapter.status(10.0) is None


@pytest.mark.asyncio
async def test_dm_enrollment_restarts_after_interruption_without_replaying_telegram(
    conn: sqlite3.Connection,
) -> None:
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,type,name,read_inbox_max_id,read_outbox_max_id,identity_complete) VALUES (?,?,?,?,?,1)",
        [(121, "user", "One", 1, 2), (122, "user", "Two", 3, 4)],
    )
    _publish_generation(conn, 7)
    worker = FullSyncWorker(cast(FullHistoryPagePort, object()), conn, asyncio.Event())
    original = worker._consume_one_canonical_dm
    calls = 0

    def fail_once(row: tuple[object, ...], now: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted")
        return original(row, now)

    worker._consume_one_canonical_dm = fail_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="interrupted"):
        await FullSyncDmEnrollmentDemandAdapter(worker).run_slice(RpcAttemptBudget(limit=1))
    assert (
        conn.execute(
            "SELECT value FROM daemon_state WHERE key='full_sync_dm_enrollment_last_publication_generation'"
        ).fetchone()
        is None
    )
    assert conn.execute("SELECT dialog_id FROM full_history_enrollment").fetchall() == []

    worker._consume_one_canonical_dm = original  # type: ignore[method-assign]
    await FullSyncDmEnrollmentDemandAdapter(worker).run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute("SELECT dialog_id FROM full_history_enrollment ORDER BY dialog_id").fetchall() == [
        (121,),
        (122,),
    ]
    assert conn.execute(
        "SELECT value FROM daemon_state WHERE key='full_sync_dm_enrollment_last_publication_generation'"
    ).fetchone() == ("7",)


def test_delta_gap_status_uses_refresh_or_recency_boundary_without_writes(conn: sqlite3.Connection) -> None:
    _seed_history_dialog(conn, 201, status="synced", last_delta_checked_at=100)
    worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), conn, asyncio.Event())
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
        TelethonForwardGapPageAdapter(SimpleNamespace(iter_messages=iter_messages)), conn, asyncio.Event()
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
    worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), conn, asyncio.Event())
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
    worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), conn, asyncio.Event())
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
    [
        TelegramRpcAdmissionDeferred,
        TelegramRpcThrottled,
        RpcAdmissionClosedError,
        RpcAttemptBudgetExhaustedError,
        RPCError,
    ],
)
async def test_delta_gap_adapter_propagates_coordinator_outcomes_without_checkpoint(
    conn: sqlite3.Connection,
    failure_type: type[BaseException],
) -> None:
    dialog_id = 206
    _seed_history_dialog(conn, dialog_id, status="synced", refresh_requested_at=1)
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, 10, 1, 'baseline')", (dialog_id,)
    )
    conn.commit()

    async def iter_messages(**_kwargs: object) -> AsyncIterator[object]:
        if failure_type is TelegramRpcAdmissionDeferred:
            raise TelegramRpcAdmissionDeferred(retry_after_seconds=7)
        if failure_type is TelegramRpcThrottled:
            raise TelegramRpcThrottled(retry_after_seconds=11)
        if failure_type is RpcAdmissionClosedError:
            raise RpcAdmissionClosedError(current_rpc_scope(), "scheduler closed")
        if failure_type is RpcAttemptBudgetExhaustedError:
            raise RpcAttemptBudgetExhaustedError("slice attempt budget exhausted")
        raise RPCError(None, "delta failed")
        yield  # pragma: no cover

    adapter = DeltaGapFillDemandAdapter(
        DeltaSyncWorker(
            TelethonForwardGapPageAdapter(SimpleNamespace(iter_messages=iter_messages)), conn, asyncio.Event()
        )
    )

    expected_failure = MessageHistoryUnavailableError if failure_type is RPCError else failure_type
    with pytest.raises(expected_failure):
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

    client = SimpleNamespace(get_messages=get_messages)
    worker = FullSyncWorker(
        TelethonFullHistoryPageAdapter(client),
        conn,
        asyncio.Event(),
        total_messages_probe=TelethonHistoryAccessProbe(client),
    )
    adapter = FullSyncDemandAdapter(worker)
    with patch("mcp_telegram.sync_worker.time.time", return_value=1000):
        with pytest.raises(MessageHistoryUnavailableError):
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
async def test_full_sync_total_repair_persists_access_loss_reason(conn: sqlite3.Connection) -> None:
    dialog_id = 205
    _seed_history_dialog(conn, dialog_id, status="synced")

    class AccessLostProbe:
        async def probe_total_messages(self, dialog_id: int) -> int | None:
            del dialog_id
            raise MessageHistoryAccessLostError("history access lost", reason_code="ChannelPrivateError")

    worker = FullSyncWorker(
        cast(FullHistoryPagePort, object()),
        conn,
        asyncio.Event(),
        total_messages_probe=AccessLostProbe(),
    )

    await FullSyncDemandAdapter(worker).run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        "access_lost",
    )
    assert conn.execute(
        "SELECT reason_code FROM conversation_history_events WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == ("ChannelPrivateError",)


@pytest.mark.asyncio
async def test_full_sync_page_adapter_propagates_rpc_failure_and_preserves_progress(conn: sqlite3.Connection) -> None:
    dialog_id = 207
    _seed_history_dialog(conn, dialog_id, status="syncing")
    conn.execute("UPDATE synced_dialogs SET sync_progress=77 WHERE dialog_id=?", (dialog_id,))
    conn.commit()

    async def get_messages(**_kwargs: object) -> MockTotalList:
        raise RPCError(None, "history failed")

    client = SimpleNamespace(get_messages=get_messages)
    adapter = FullSyncDemandAdapter(FullSyncWorker(TelethonFullHistoryPageAdapter(client), conn, asyncio.Event()))
    with pytest.raises(MessageHistoryUnavailableError):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute(
        "SELECT status, sync_progress FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == (
        "syncing",
        77,
    )


@pytest.mark.asyncio
async def test_full_sync_page_adapter_defers_to_coordinator_without_local_sleep(conn: sqlite3.Connection) -> None:
    dialog_id = 208
    _seed_history_dialog(conn, dialog_id, status="syncing")

    async def get_messages(**_kwargs: object) -> MockTotalList:
        raise TelegramRpcAdmissionDeferred(retry_after_seconds=9)

    client = SimpleNamespace(get_messages=get_messages)
    adapter = FullSyncDemandAdapter(FullSyncWorker(TelethonFullHistoryPageAdapter(client), conn, asyncio.Event()))
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

    client = SimpleNamespace(get_messages=get_messages)
    worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy(), TelethonHistoryAccessProbe(client))
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

    client = SimpleNamespace(get_messages=get_messages, iter_messages=iter_messages)
    worker = DeltaSyncWorker(TelethonForwardGapPageAdapter(client), conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy(), TelethonHistoryAccessProbe(client))

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    get_messages.assert_awaited_once()
    assert conn.execute(
        "SELECT stage, total_messages FROM delta_access_recovery_state WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == ("gap_fill", 12)
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        "access_lost",
    )

    class Observer:
        def __init__(self) -> None:
            self.rows: list[tuple[Mapping[str, int], str]] = []

        def observe_delta_gap_fill(self, metrics: Mapping[str, int], *, reason: str) -> None:
            self.rows.append((metrics, reason))

    observer = Observer()
    restarted = DeltaAccessProbeDemandAdapter(
        worker, _delta_policy(), TelethonHistoryAccessProbe(client), observer=observer
    )
    await restarted.run_slice(RpcAttemptBudget(limit=1))

    assert len(observer.rows) == 1
    recovery_metrics, _ = observer.rows[0]
    assert recovery_metrics["recovery"] == recovery_metrics["recovery_completion_count"] == 1
    assert recovery_metrics["completed"] == 1
    assert (
        recovery_metrics["slice_count"]
        == recovery_metrics["completed"] + recovery_metrics["deferred"] + recovery_metrics["failed"]
    )
    assert recovery_metrics["fetched_count"] == sum(
        recovery_metrics[key]
        for key in ("new_key_count", "preexisting_key_count", "uncommitted_unique_count", "duplicate_count")
    )
    assert conn.execute("SELECT 1 FROM delta_access_recovery_state WHERE dialog_id=?", (dialog_id,)).fetchone() is None
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == ("syncing",)


@pytest.mark.asyncio
async def test_recovery_query_failure_does_not_reuse_prior_slice_metrics(conn: sqlite3.Connection) -> None:
    dialog_id = 304
    _seed_history_dialog(conn, dialog_id, status="synced", refresh_requested_at=1)
    conn.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (?, ?, 1)", (dialog_id, 10))
    conn.commit()

    async def iter_messages(**_kwargs: object) -> AsyncIterator[object]:
        for message_id in range(11, 18):
            yield build_mock_message(id=message_id, text=f"message {message_id}")

    client = SimpleNamespace(iter_messages=iter_messages)
    worker = DeltaSyncWorker(
        TelethonForwardGapPageAdapter(client),
        conn,
        asyncio.Event(),
    )

    class Observer:
        def __init__(self) -> None:
            self.rows: list[tuple[Mapping[str, int], str]] = []

        def observe_delta_gap_fill(self, metrics: Mapping[str, int], *, reason: str) -> None:
            self.rows.append((metrics, reason))

    observer = Observer()
    scheduled = DeltaGapFillDemandAdapter(worker, observer=observer)
    await scheduled.run_slice(RpcAttemptBudget(limit=1))
    assert observer.rows[-1][0]["new_key_count"] == 7

    conn.execute("UPDATE synced_dialogs SET status='access_lost' WHERE dialog_id=?", (dialog_id,))
    conn.execute(
        "INSERT INTO delta_access_recovery_state(dialog_id,stage,total_messages,probe_succeeded_at,retry_at,updated_at) "
        "VALUES (?, 'gap_fill', 20, 1, NULL, 1)",
        (dialog_id,),
    )
    conn.commit()
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy(), TelethonHistoryAccessProbe(client), observer)

    with patch("mcp_telegram.delta_sync.full_history_enabled", side_effect=RuntimeError("lookup failed")):
        with pytest.raises(RuntimeError, match="lookup failed"):
            await adapter.run_slice(RpcAttemptBudget(limit=1))

    metrics, reason = observer.rows[-1]
    assert reason == "error"
    assert metrics["recovery"] == 1
    assert metrics["failed"] == 1
    assert metrics["fetched_count"] == metrics["new_key_count"] == 0
    assert metrics["page_count"] == 0


@pytest.mark.asyncio
async def test_non_enrolled_recovery_is_observed_as_skipped_not_terminal(conn: sqlite3.Connection) -> None:
    dialog_id = 306
    _seed_history_dialog(conn, dialog_id, status="access_lost")
    conn.execute("UPDATE full_history_enrollment SET enabled=0 WHERE dialog_id=?", (dialog_id,))
    conn.execute(
        "INSERT INTO delta_access_recovery_state(dialog_id,stage,total_messages,probe_succeeded_at,retry_at,updated_at) "
        "VALUES (?, 'gap_fill', 20, 1, NULL, 1)",
        (dialog_id,),
    )
    conn.commit()

    class Probe:
        async def probe_total_messages(self, dialog_id: int) -> int | None:
            raise AssertionError(f"unexpected probe for {dialog_id}")

    class Observer:
        def __init__(self) -> None:
            self.rows: list[tuple[Mapping[str, int], str]] = []

        def observe_delta_gap_fill(self, metrics: Mapping[str, int], *, reason: str) -> None:
            self.rows.append((metrics, reason))

    observer = Observer()
    worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy(), Probe(), observer)

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert len(observer.rows) == 1
    metrics, reason = observer.rows[0]
    assert reason == "skipped"
    assert metrics["completed"] == metrics["recovery_completion_count"] == 1
    assert metrics["terminal_count"] == metrics["page_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_kind", "raises", "retry_at", "reason"),
    [
        ("flood", False, 1100, "flood_wait"),
        ("budget", True, None, "budget_exhausted"),
    ],
)
async def test_access_recovery_retains_local_flood_retry_and_observes_budget_deferral(
    conn: sqlite3.Connection,
    error_kind: str,
    raises: bool,
    retry_at: int | None,
    reason: str,
) -> None:
    dialog_id = 305
    _seed_history_dialog(conn, dialog_id, status="access_lost")
    conn.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (?, ?, 1)", (dialog_id, 10))
    conn.execute(
        "INSERT INTO delta_access_recovery_state(dialog_id,stage,total_messages,probe_succeeded_at,retry_at,updated_at) "
        "VALUES (?, 'gap_fill', 10, 1, NULL, 1)",
        (dialog_id,),
    )
    conn.commit()

    class HistoryPort:
        async def fetch_page(
            self,
            _dialog_id: int,
            *,
            after_message_id: int,
            should_stop: object,
        ) -> object:
            del after_message_id, should_stop
            if error_kind == "flood":
                raise TelegramRpcThrottled(retry_after_seconds=600)
            raise RpcAttemptBudgetExhaustedError("budget exhausted")

    class Probe:
        async def probe_total_messages(self, dialog_id: int) -> int | None:
            del dialog_id
            return 10

    class Observer:
        def __init__(self) -> None:
            self.rows: list[tuple[Mapping[str, int], str]] = []

        def observe_delta_gap_fill(self, metrics: Mapping[str, int], *, reason: str) -> None:
            self.rows.append((metrics, reason))

    observer = Observer()
    worker = DeltaSyncWorker(cast(ForwardGapPagePort, HistoryPort()), conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy(), Probe(), observer=observer)

    with patch("mcp_telegram.delta_sync.time.time", return_value=1000):
        if raises:
            with pytest.raises(RpcAttemptBudgetExhaustedError):
                await adapter.run_slice(RpcAttemptBudget(limit=1))
        else:
            await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute(
        "SELECT retry_at FROM delta_access_recovery_state WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == (retry_at,)
    assert len(observer.rows) == 1
    metrics, observed_reason = observer.rows[0]
    assert observed_reason == reason
    assert metrics["deferred"] == 1
    assert metrics["completed"] == metrics["failed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_kind", "expected_retry"),
    [
        ("deferred", 1007),
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
    client = SimpleNamespace(get_messages=get_messages)
    worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy(), TelethonHistoryAccessProbe(client))

    with patch("mcp_telegram.delta_sync.time.time", return_value=1000):
        await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        "access_lost",
    )
    assert conn.execute(
        "SELECT access_next_revalidate_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == (expected_retry,)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["saturated", "expired"])
async def test_access_probe_propagates_admission_failure_without_retry_boundary(
    conn: sqlite3.Connection,
    error_kind: str,
) -> None:
    dialog_id = 304
    _seed_history_dialog(conn, dialog_id, status="access_lost")

    class AdmissionFailureProbe:
        async def probe_total_messages(self, dialog_id: int) -> int | None:
            del dialog_id
            if error_kind == "saturated":
                raise RpcAdmissionSaturatedError(current_rpc_scope(), "probe capacity is full")
            raise RpcAdmissionExpiredError(current_rpc_scope(), "probe deadline elapsed")

    worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy(), AdmissionFailureProbe())

    with patch("mcp_telegram.delta_sync.time.time", return_value=1000):
        with pytest.raises((RpcAdmissionSaturatedError, RpcAdmissionExpiredError)):
            await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute(
        "SELECT access_next_revalidate_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == (None,)


@pytest.mark.asyncio
async def test_access_probe_propagates_exhausted_slice_budget(conn: sqlite3.Connection) -> None:
    dialog_id = 304
    _seed_history_dialog(conn, dialog_id, status="access_lost")

    class ExhaustedProbe:
        async def probe_total_messages(self, dialog_id: int) -> int | None:
            del dialog_id
            raise RpcAttemptBudgetExhaustedError("slice attempt budget exhausted")

    worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), conn, asyncio.Event())
    adapter = DeltaAccessProbeDemandAdapter(worker, _delta_policy(), ExhaustedProbe())

    with patch("mcp_telegram.delta_sync.time.time", return_value=1000):
        with pytest.raises(RpcAttemptBudgetExhaustedError):
            await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute(
        "SELECT access_next_revalidate_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == (None,)
    assert conn.execute("SELECT 1 FROM delta_access_recovery_state WHERE dialog_id=?", (dialog_id,)).fetchone() is None


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
