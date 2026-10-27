"""Tests for durable archive demand adapters."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict, Unpack, cast
from unittest.mock import AsyncMock, patch

import pytest
from telethon.tl.functions.messages import SearchRequest
from telethon.tl.types import PeerUser

from mcp_telegram import activity_sync
from mcp_telegram.activity_sync import ArchiveBackfillDemandAdapter, ArchiveIncrementalDemandAdapter
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.telegram_demand import DemandStatus, DurableDemandAdapter, RpcAttemptBudget
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkRevision
from mcp_telegram.telegram_rpc import _canonical_wire_method
from mcp_telegram.telegram_rpc_consumers import DURABLE_DEMAND_ORDER, DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    TelegramRpcScope,
    current_rpc_scope,
)
from sqlite_writer_helpers import inline_writer

_TEST_TIMEOUT_S = 0.05


def _selected_budget(adapter: DurableDemandAdapter, limit: int = 1) -> RpcAttemptBudget:
    status = adapter.status(time.time())
    assert status is not None and status.selected is not None
    return RpcAttemptBudget(limit=limit, selected=status.selected)


@dataclass
class FakeReplies:
    replies: int = 0


@dataclass
class FakeMessage:
    id: int
    date: datetime
    message: str
    peer_id: object
    replies: object | None = None
    reactions: object | None = None
    out: bool = True


class _SearchEntityLike(Protocol):
    id: int
    first_name: str | None
    last_name: str | None
    title: str | None
    username: str | None


@dataclass
class FakeSearchResult:
    messages: list[FakeMessage]
    users: list[_SearchEntityLike] = field(default_factory=list)
    chats: list[_SearchEntityLike] = field(default_factory=list)
    count: int | None = None


class _MsgKwargs(TypedDict, total=False):
    text: str
    replies: int
    out: bool


class _FakeClient:
    def __init__(self, batches: list[FakeSearchResult]) -> None:
        self._batches = list(batches)
        self.scopes: list[TelegramRpcScope] = []

    async def __call__(self, request: object) -> FakeSearchResult:
        del request
        self.scopes.append(current_rpc_scope())
        return self._batches.pop(0) if self._batches else FakeSearchResult(messages=[])

    async def get_input_entity(self, dialog_id: int) -> object:
        del dialog_id
        return object()


class _IdleDemandAdapter(DurableDemandAdapter):
    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        del now
        del excluded
        return None

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        del budget
        raise AssertionError("idle demand adapter must not execute")


@dataclass
class _DemandObserver:
    events: list[dict[str, object]] = field(default_factory=list)

    def observe_demand(self, **event: object) -> None:
        self.events.append(event)


class _ThrottledClient:
    def __init__(self, error: TelegramRpcThrottled, *, dispatch: bool, on_dispatch: object | None = None) -> None:
        self._error = error
        self._dispatch = dispatch
        self._on_dispatch = on_dispatch
        self.calls = 0

    async def __call__(self, request: object) -> object:
        self.calls += 1
        scope = current_rpc_scope()
        if self._dispatch:
            assert scope.attempt_budget is not None
            scope.attempt_budget.debit()
            scope.attempt_budget.last_method = _canonical_wire_method(request)
            if callable(self._on_dispatch):
                self._on_dispatch()
        raise self._error

    async def get_input_entity(self, dialog_id: int) -> object:
        del dialog_id
        return object()


def _coordinator(  # noqa: PLR0913
    kind: DemandKind,
    adapter: DurableDemandAdapter,
    *,
    observer: _DemandObserver,
    shutdown_event: asyncio.Event | None = None,
    rpc_eligibility: Callable[
        [str | None], TelegramRpcAdmissionDeferred | TelegramRpcMethodRecoveryPending | TelegramRpcThrottled | None
    ]
    | None = None,
    monotonic_clock: Callable[[], float] | None = None,
) -> TelegramDemandCoordinator:
    adapters: dict[DemandKind, DurableDemandAdapter] = {kind: _IdleDemandAdapter() for kind in DURABLE_DEMAND_ORDER}
    adapters[kind] = adapter
    return TelegramDemandCoordinator(
        adapters,
        shutdown_event,
        clock=lambda: 100.0,
        monotonic_clock=monotonic_clock or time.monotonic,
        observer=observer,
        rpc_eligibility=rpc_eligibility,
    )


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    return sqlite3.connect(db_path)


def _msg(msg_id: int, user_id: int, ts: int, **kwargs: Unpack[_MsgKwargs]) -> FakeMessage:
    return FakeMessage(
        id=msg_id,
        date=datetime.fromtimestamp(ts, tz=UTC),
        message=kwargs.get("text", "hi"),
        peer_id=PeerUser(user_id=user_id),
        replies=FakeReplies(replies=kwargs.get("replies", 0)) if kwargs.get("replies", 0) else None,
        out=kwargs.get("out", True),
    )


@dataclass
class _TrimMessage:
    id: int
    date: datetime | None


def test_trim_incremental_batch_keeps_message_at_min_date() -> None:
    min_date = 1_700_000_000
    in_window, past_window = activity_sync._trim_incremental_batch(
        [
            _TrimMessage(2, datetime.fromtimestamp(min_date + 1, tz=UTC)),
            _TrimMessage(1, datetime.fromtimestamp(min_date, tz=UTC)),
        ],
        min_date,
    )
    assert [message.id for message in in_window] == [2, 1]
    assert past_window is False


def test_trim_incremental_batch_stops_at_first_message_before_window() -> None:
    min_date = 1_700_000_000
    in_window, past_window = activity_sync._trim_incremental_batch(
        [
            _TrimMessage(3, datetime.fromtimestamp(min_date + 2, tz=UTC)),
            _TrimMessage(2, datetime.fromtimestamp(min_date - 1, tz=UTC)),
            _TrimMessage(1, datetime.fromtimestamp(min_date + 1, tz=UTC)),
        ],
        min_date,
    )
    assert [message.id for message in in_window] == [3]
    assert past_window is True


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = _make_db(tmp_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_archive_backfill_adapter_commits_one_page_with_precise_scope(conn: sqlite3.Connection) -> None:
    client = _FakeClient([FakeSearchResult(messages=[_msg(100, 42, 1_700_000_100)]), FakeSearchResult(messages=[])])
    adapter = ArchiveBackfillDemandAdapter(
        client, conn, asyncio.Event(), _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )

    assert adapter.status(1_700_000_000.0) is not None
    budget = _selected_budget(adapter)
    await adapter.run_slice(budget)
    state = dict(
        cast(list[tuple[str, str | None]], conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    )
    assert state["backfill_offset_id"] == "100"
    assert state["backfill_complete"] == "0"
    assert client.scopes[0].demand_kind is DemandKind.ARCHIVE_BACKFILL
    assert client.scopes[0].attempt_budget is budget

    await adapter.run_slice(_selected_budget(adapter))
    assert adapter.status(1_700_000_000.0) is None


@pytest.mark.asyncio
async def test_archive_commits_through_owner_with_readonly_loop_connection(conn: sqlite3.Connection) -> None:
    path = Path(cast(str, conn.execute("PRAGMA database_list").fetchone()[2]))
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    try:
        client = _FakeClient([FakeSearchResult(messages=[_msg(100, 42, 1_700_000_100)]), FakeSearchResult(messages=[])])
        adapter = ArchiveBackfillDemandAdapter(
            client, reader, asyncio.Event(), _TEST_TIMEOUT_S, writer=writer, account_id=123
        )
        await adapter.run_slice(_selected_budget(adapter))
        assert reader.execute("SELECT value FROM activity_sync_state WHERE key='backfill_offset_id'").fetchone() == (
            "100",
        )
        assert reader.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=42 AND message_id=100").fetchone() == (1,)
        await adapter.run_slice(_selected_budget(adapter))
        assert adapter.status(time.time()) is None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM activity_sync_state")
    finally:
        reader.close()
        await writer.aclose()


@pytest.mark.asyncio
async def test_archive_incremental_adapter_resumes_from_key_value_state(conn: sqlite3.Connection) -> None:
    last_sync_at = int(time.time()) - 7_200
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.execute(
            "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES ('last_sync_at', ?)",
            (str(last_sync_at),),
        )
    client = _FakeClient([FakeSearchResult(messages=[_msg(12, 42, last_sync_at + 30)]), FakeSearchResult(messages=[])])
    adapter = ArchiveIncrementalDemandAdapter(
        client, conn, asyncio.Event(), 3_600.0, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )

    initial = adapter.status(float(last_sync_at))
    assert initial is not None
    assert initial.release_at == last_sync_at + 3_600
    first_budget = _selected_budget(adapter)
    await adapter.run_slice(first_budget)
    state = dict(
        cast(list[tuple[str, str | None]], conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    )
    assert state["incremental_min_date"] == str(last_sync_at - 60)
    assert state["incremental_offset_id"] == "12"
    assert client.scopes[0].demand_kind is DemandKind.ARCHIVE_INCREMENTAL
    assert client.scopes[0].attempt_budget is first_budget

    await adapter.run_slice(_selected_budget(adapter))
    final_state = dict(
        cast(list[tuple[str, str | None]], conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    )
    assert "incremental_min_date" not in final_state
    assert "incremental_offset_id" not in final_state
    assert int(final_state["last_sync_at"] or 0) > last_sync_at


@pytest.mark.asyncio
async def test_archive_backfill_finite_throttle_is_owned_by_coordinator(conn: sqlite3.Connection) -> None:
    error = TelegramRpcThrottled(retry_after_seconds=17)
    held = False
    monotonic_now = 100.0
    adapter = ArchiveBackfillDemandAdapter(
        _ThrottledClient(error, dispatch=True, on_dispatch=lambda: _set_true()),
        conn,
        asyncio.Event(),
        _TEST_TIMEOUT_S,
        writer=inline_writer(conn),
        account_id=123,
    )
    observer = _DemandObserver()
    request_method = _canonical_wire_method(
        SearchRequest(
            peer=activity_sync.InputPeerEmpty(),
            q="",
            filter=activity_sync.InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_id=0,
            add_offset=0,
            limit=activity_sync._BACKFILL_BATCH_LIMIT,
            max_id=0,
            min_id=0,
            hash=0,
            from_id=activity_sync.InputPeerSelf(),
        )
    )

    def _set_true() -> None:
        nonlocal held
        held = True

    def rpc_eligibility(method: str | None) -> TelegramRpcAdmissionDeferred | None:
        if held and method == request_method and monotonic_now < 117.0:
            return TelegramRpcAdmissionDeferred(
                17, method_key=request_method, generation="g1", eligible_at=117.0, reason="flood_wait"
            )
        return None

    coordinator = _coordinator(
        DemandKind.ARCHIVE_BACKFILL,
        adapter,
        observer=observer,
        rpc_eligibility=rpc_eligibility,
        monotonic_clock=lambda: monotonic_now,
    )

    await coordinator._execute_slice(DemandKind.ARCHIVE_BACKFILL)
    coordinator._active_kind = None
    coordinator.scan(now=100.0)
    assert coordinator.next_release_at is None
    assert coordinator._wait_delay(100.0) == pytest.approx(17.0, abs=0.001)
    assert observer.events[-1] == {
        "outcome": "deferred",
        "demand_kind": DemandKind.ARCHIVE_BACKFILL,
        "actual_attempts": 1,
        "queue_age_seconds": None,
        "freshness_debt_seconds": None,
        "reason": "admission_deferred",
    }
    monotonic_now = 117.0
    ready = coordinator.scan(now=117.0)
    assert DemandKind.ARCHIVE_BACKFILL in ready
    assert conn.execute("SELECT value FROM activity_sync_state WHERE key='backfill_offset_id'").fetchone() == ("0",)


@pytest.mark.asyncio
async def test_archive_incremental_latched_throttle_stops_coordinator_without_local_completion(
    conn: sqlite3.Connection,
) -> None:
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.execute("INSERT OR REPLACE INTO activity_sync_state(key, value) VALUES ('last_sync_at', '1')")
    error = TelegramRpcThrottled(latched=True, detail="test circuit open")
    client = _ThrottledClient(error, dispatch=False)
    adapter = ArchiveIncrementalDemandAdapter(
        client, conn, asyncio.Event(), 10.0, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )
    observer = _DemandObserver()
    shutdown_event = asyncio.Event()
    coordinator = _coordinator(
        DemandKind.ARCHIVE_INCREMENTAL,
        adapter,
        observer=observer,
        shutdown_event=shutdown_event,
    )

    await coordinator._execute_slice(DemandKind.ARCHIVE_INCREMENTAL)
    assert shutdown_event.is_set() is False
    assert client.calls == 1
    assert conn.execute("SELECT value FROM activity_sync_state WHERE key='last_sync_at'").fetchone() == ("1",)
    assert conn.execute("SELECT value FROM activity_sync_state WHERE key='incremental_min_date'").fetchone() is None
    assert [event["outcome"] for event in observer.events] == ["selected", "deferred"]


@pytest.mark.asyncio
async def test_archive_runtime_commits_page_and_leaves_rpc_connection_idle(conn: sqlite3.Connection) -> None:
    from mcp_telegram.sync_transactions import enable_runtime_writes

    class IdleClient(_FakeClient):
        async def __call__(self, request: object) -> FakeSearchResult:
            assert not conn.in_transaction
            assert conn.execute("PRAGMA query_only").fetchone() == (1,)
            return await super().__call__(request)

    enable_runtime_writes(conn)
    client = IdleClient([FakeSearchResult(messages=[_msg(100, 42, 1_700_000_100)])])
    adapter = ArchiveBackfillDemandAdapter(
        client, conn, asyncio.Event(), timeout_s=_TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )
    await adapter.run_slice(_selected_budget(adapter))

    assert conn.execute("SELECT message_id FROM messages").fetchall() == [(100,)]
    assert conn.execute("SELECT value FROM activity_sync_state WHERE key='backfill_offset_id'").fetchone() == ("100",)
    assert not conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone() == (1,)


@pytest.mark.asyncio
async def test_archive_page_failure_rolls_back_messages_and_checkpoint(conn: sqlite3.Connection) -> None:
    from mcp_telegram.sync_transactions import enable_runtime_writes

    enable_runtime_writes(conn)
    client = _FakeClient([FakeSearchResult(messages=[_msg(100, 42, 1_700_000_100)])])
    adapter = ArchiveBackfillDemandAdapter(
        client, conn, asyncio.Event(), timeout_s=_TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )
    with patch.object(activity_sync, "_upsert_entities_from_search", side_effect=RuntimeError("identity write failed")):
        with pytest.raises(RuntimeError, match="identity write failed"):
            await adapter.run_slice(_selected_budget(adapter))

    assert conn.execute("SELECT message_id FROM messages").fetchall() == []
    assert conn.execute("SELECT value FROM activity_sync_state WHERE key='backfill_offset_id'").fetchone() == ("0",)
    assert not conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone() == (1,)


@pytest.mark.asyncio
async def test_incremental_timeout_preserves_page_and_anchor(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.executemany(
            "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES (?,?)",
            [
                ("last_sync_at", "100"),
                ("incremental_min_date", "40"),
                ("incremental_offset_id", "12"),
                ("incremental_started_at", "200"),
            ],
        )
    before = activity_sync._load_state(conn)
    with patch.object(activity_sync, "call_with_timeout", new=AsyncMock(side_effect=TimeoutError)):
        adapter = ArchiveIncrementalDemandAdapter(
            _FakeClient([]), conn, asyncio.Event(), 10, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )
        await adapter.run_slice(_selected_budget(adapter))
    assert activity_sync._load_state(conn) == before


@pytest.mark.asyncio
async def test_incremental_next_window_starts_before_scan_arrivals(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.execute("INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES ('last_sync_at','100')")
    client = _FakeClient([FakeSearchResult(messages=[_msg(12, 42, 190)]), FakeSearchResult(messages=[])])
    adapter = ArchiveIncrementalDemandAdapter(
        client, conn, asyncio.Event(), 10, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )
    with patch.object(activity_sync.time, "time", return_value=200):
        await adapter.run_slice(_selected_budget(adapter))
    restarted = ArchiveIncrementalDemandAdapter(
        client, conn, asyncio.Event(), 10, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )
    with patch.object(activity_sync.time, "time", return_value=1000):
        await restarted.run_slice(_selected_budget(restarted))
        assert activity_sync._load_state(conn)["last_sync_at"] == "200"
        window = activity_sync._prepare_incremental_slice(conn, False)
    assert window is not None
    assert window[:2] == (140, 0)


@pytest.mark.asyncio
async def test_empty_incremental_cycles_receive_new_business_revisions(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.execute("INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES ('last_sync_at','100')")
    client = _FakeClient([FakeSearchResult(messages=[]), FakeSearchResult(messages=[]), FakeSearchResult(messages=[])])
    adapter = ArchiveIncrementalDemandAdapter(
        client, conn, asyncio.Event(), 0, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )

    with patch.object(activity_sync.time, "time", return_value=200):
        first = adapter.status(200.0)
        assert first is not None and first.selected is not None
        first_result = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=first.selected))
        assert first_result.kind is SliceOutcomeKind.TERMINAL
    path = cast(str, conn.execute("PRAGMA database_list").fetchone()[2])
    reopened = sqlite3.connect(path)
    try:
        restarted = ArchiveIncrementalDemandAdapter(
            client, reopened, asyncio.Event(), 0, _TEST_TIMEOUT_S, writer=inline_writer(reopened), account_id=123
        )
        with patch.object(activity_sync.time, "time", return_value=200):
            second = restarted.status(200.0, excluded=frozenset({first.selected}))
            assert second is not None and second.selected is not None
            assert second.selected.revision != first.selected.revision
            await restarted.run_slice(RpcAttemptBudget(limit=1, selected=second.selected))

        with reopened:
            reopened.execute(
                "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES('incremental_started_at','180')"
            )
            reopened.execute(
                "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES('incremental_min_date','120')"
            )
            reopened.execute(
                "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES('incremental_offset_id','0')"
            )
        third = restarted.status(180.0)
        assert third is not None and third.selected is not None
        with patch.object(activity_sync.time, "time", return_value=180):
            await restarted.run_slice(RpcAttemptBudget(limit=1, selected=third.selected))

        assert reopened.execute("SELECT value FROM activity_sync_state WHERE key='last_sync_at'").fetchone() == ("180",)
        assert reopened.execute(
            "SELECT value FROM activity_sync_state WHERE key='incremental_completed_generation'"
        ).fetchone() == ("3",)
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_backfill_completion_anchors_original_start(conn: sqlite3.Connection) -> None:
    client = _FakeClient([FakeSearchResult(messages=[_msg(12, 42, 190)]), FakeSearchResult(messages=[])])
    adapter = ArchiveBackfillDemandAdapter(
        client, conn, asyncio.Event(), _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )
    with patch.object(activity_sync.time, "time", return_value=200):
        await adapter.run_slice(_selected_budget(adapter))
    with patch.object(activity_sync.time, "time", return_value=1000):
        restarted = ArchiveBackfillDemandAdapter(
            client, conn, asyncio.Event(), _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )
        await restarted.run_slice(_selected_budget(restarted))
    assert activity_sync._load_state(conn)["last_sync_at"] == "200"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), TelegramRpcAdmissionDeferred(retry_after_seconds=7)])
async def test_incremental_interruption_keeps_window(conn: sqlite3.Connection, error: BaseException) -> None:
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.executemany(
            "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES (?,?)",
            [
                ("last_sync_at", "100"),
                ("incremental_min_date", "40"),
                ("incremental_offset_id", "12"),
                ("incremental_started_at", "200"),
            ],
        )
    before = activity_sync._load_state(conn)
    with patch.object(activity_sync, "call_with_timeout", new=AsyncMock(side_effect=error)):
        with pytest.raises(type(error)):
            adapter = ArchiveIncrementalDemandAdapter(
                _FakeClient([]), conn, asyncio.Event(), 10, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
            )
            await adapter.run_slice(_selected_budget(adapter))
    assert activity_sync._load_state(conn) == before


@pytest.mark.asyncio
async def test_incremental_shutdown_keeps_window(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.executemany(
            "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES (?,?)",
            [
                ("last_sync_at", "100"),
                ("incremental_min_date", "40"),
                ("incremental_offset_id", "12"),
                ("incremental_started_at", "200"),
            ],
        )
    before = activity_sync._load_state(conn)
    shutdown = asyncio.Event()
    shutdown.set()
    adapter = ArchiveIncrementalDemandAdapter(
        _FakeClient([]), conn, shutdown, 10, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
    )
    await adapter.run_slice(_selected_budget(adapter))
    assert activity_sync._load_state(conn) == before
