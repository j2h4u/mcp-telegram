"""Local rich queue cleanup keeps its own bounded writer lifecycle."""

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram import daemon
from mcp_telegram.config import SchedulingConfig
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import DemandStatus, DurableDemandAdapter, RpcAttemptBudget
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
from mcp_telegram.telegram_rpc_consumers import DURABLE_DEMAND_ORDER, DemandKind
from tests.test_fact_demand_adapters import _IdleDemandAdapter


def _seed(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES (1,?,1,'kept')",
            [(1,), (2,), (3,), (4,), (5,)],
        )
        conn.executemany(
            "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,priority) VALUES ('rich_composition',1,?,1,0)",
            [(1,), (2,), (3,), (4,), (5,)],
        )
        conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES('rich_composition_hydration_cursor',?)",
            ('{"cursor":null,"done":true}',),
        )


def _state(conn: sqlite3.Connection) -> dict[str, object]:
    row = cast(
        tuple[str],
        conn.execute("SELECT value FROM daemon_state WHERE key='rich_composition_hydration_cursor'").fetchone(),
    )
    return cast(dict[str, object], json.loads(row[0]))


def _context(
    writer: SQLiteWriter, shutdown: asyncio.Event, coordinator: TelegramDemandCoordinator | None
) -> daemon._SyncMainContext:
    scheduling = SchedulingConfig()
    scheduling = replace(scheduling, fact_hydration=replace(scheduling.fact_hydration, max_jobs_per_cycle=2))
    return cast(
        daemon._SyncMainContext,
        SimpleNamespace(
            writer=writer,
            shutdown_event=shutdown,
            coordinator=coordinator,
            scheduling=scheduling,
            api_server=SimpleNamespace(self_id=42, _ready=True),
            background_tasks=set(),
            handler_manager=None,
        ),
    )


async def test_tracked_cleanup_finishes_while_network_coordinator_is_blocked(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    shutdown = asyncio.Event()
    started = asyncio.Event()
    network = asyncio.Event()
    selected = WorkRevision(WorkKey(DemandKind.DIALOG_BOOTSTRAP, 42, "blocked"), "1")

    class BlockedNetwork:
        def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
            del now
            return None if selected in excluded else DemandStatus(0, selected=selected)

        async def run_slice(self, budget: RpcAttemptBudget) -> None:
            assert budget.selected == selected
            started.set()
            await network.wait()

    adapters = {kind: cast(DurableDemandAdapter, _IdleDemandAdapter()) for kind in DURABLE_DEMAND_ORDER}
    adapters[DemandKind.DIALOG_BOOTSTRAP] = cast(DurableDemandAdapter, BlockedNetwork())
    coordinator = TelegramDemandCoordinator(adapters, shutdown)
    ctx = _context(writer, shutdown, coordinator)
    coordinator_task = asyncio.create_task(coordinator.run())
    try:
        await writer.run(_seed)
        await asyncio.wait_for(started.wait(), 5)
        daemon._start_rich_history_cleanup(ctx)
        cleanup = next(task for task in ctx.background_tasks if task.get_name() == "rich_history_cleanup")
        await asyncio.wait_for(cleanup, 5)
        assert not coordinator_task.done() and not network.is_set()
        assert reader.execute("SELECT count(*), min(text) FROM messages").fetchone() == (5, "kept")
        assert reader.execute("SELECT count(*) FROM hydration_jobs").fetchone() == (0,)
        state = _state(reader)
        assert state["done"] is True and state["cleanup_done"] is True
    finally:
        shutdown.set()
        coordinator_task.cancel()
        await asyncio.gather(coordinator_task, return_exceptions=True)
        await daemon._cancel_background_tasks(ctx)
        reader.close()
        await writer.aclose()


async def test_cleanup_stop_reopens_at_committed_page(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    await writer.run(_seed)
    shutdown = asyncio.Event()
    ctx = _context(writer, shutdown, None)

    entered = threading.Event()
    release = threading.Event()

    class StopAfterPage:
        async def run[T](self, callback: Callable[[sqlite3.Connection], T]) -> T:
            def held_page(conn: sqlite3.Connection) -> T:
                entered.set()
                assert release.wait(5)
                return callback(conn)

            return await writer.run(held_page)

    ctx.writer = cast(SQLiteWriter, StopAfterPage())
    daemon._start_rich_history_cleanup(ctx)
    cleanup = next(task for task in ctx.background_tasks if task.get_name() == "rich_history_cleanup")
    assert await asyncio.to_thread(entered.wait, 5)
    cleanup.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    await writer.aclose()
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert reader.execute("SELECT count(*) FROM hydration_jobs").fetchone() == (3,)
    assert _state(reader)["cleanup_cursor"] == [1, 2]
    writer = await SQLiteWriter.open(path)
    shutdown.clear()
    ctx.writer = writer
    try:
        daemon._start_rich_history_cleanup(ctx)
        cleanup = next(task for task in ctx.background_tasks if task.get_name() == "rich_history_cleanup")
        await asyncio.wait_for(cleanup, 5)
        assert reader.execute("SELECT count(*) FROM hydration_jobs").fetchone() == (0,)
        assert reader.execute("SELECT count(*) FROM messages WHERE rich_message IS NULL").fetchone() == (5,)
    finally:
        shutdown.set()
        await daemon._cancel_background_tasks(ctx)
        reader.close()
        await writer.aclose()


async def test_cleanup_stops_when_bound_account_changes(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    shutdown = asyncio.Event()
    ctx = _context(writer, shutdown, None)
    try:
        await writer.run(_seed)
        ctx.api_server.self_id = 43
        daemon._start_rich_history_cleanup(ctx)
        cleanup = next(task for task in ctx.background_tasks if task.get_name() == "rich_history_cleanup")
        await asyncio.wait_for(cleanup, 5)
        assert reader.execute("SELECT count(*) FROM hydration_jobs").fetchone() == (5,)
        state = _state(reader)
        assert "cleanup_cursor" not in state
    finally:
        shutdown.set()
        await daemon._cancel_background_tasks(ctx)
        reader.close()
        await writer.aclose()
