"""Independent diagnostics use the same owner as mandatory archive writes."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from mcp_telegram import runtime_observations
from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.runtime_observations import RuntimeObservationSink, record_runtime_observation
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction


@pytest.mark.asyncio
async def test_shared_owner_batches_and_preserves_core_visibility(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    transactions: list[str] = []
    await writer.run(
        lambda conn: conn.set_trace_callback(
            lambda statement: transactions.append(statement) if statement.startswith("BEGIN") else None
        )
    )
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    try:
        for _ in range(20):
            assert sink.record(kind="mcp.call", tool_name="test")

        def core(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                conn.execute("INSERT INTO daemon_state(key,value) VALUES ('core', 'committed')")

        await writer.run(core)
        await sink.aclose()
        assert sink.counters.successful_writes == 20
        assert len(transactions) == 2  # One core transaction and one diagnostic batch.
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runtime_observations").fetchone()[0] == 20
            assert conn.execute("SELECT value FROM daemon_state WHERE key='core'").fetchone()[0] == "committed"
        assert not sink.record(kind="mcp.call")
        assert sink.counters.rejected_submissions == 1
    finally:
        await sink.aclose()
        await writer.aclose()


@pytest.mark.asyncio
async def test_shared_observation_buffer_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(
        path,
        writer=writer,
        retention_ttl_seconds=3600,
        policy=replace(RuntimeObservationConfig(), queue_capacity=2),
    )
    try:
        assert sink.record(kind="mcp.call")
        assert sink.record(kind="mcp.call")
        assert not sink.record(kind="mcp.call")
        assert sink.counters.queue_full_drops == 1
        await sink.aclose()
        assert sink.counters.successful_writes == 2
    finally:
        await sink.aclose()
        await writer.aclose()


@pytest.mark.asyncio
async def test_shared_observation_failure_is_optional(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    await writer.aclose()
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    assert sink.record(kind="mcp.call")
    await sink.aclose()
    assert sink.counters.permanent_failures == 1
    assert sink.counters.successful_writes == 0


@pytest.mark.asyncio
async def test_prune_failure_does_not_lose_committed_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def fail(_conn: sqlite3.Connection, **_kwargs: object) -> object:
        raise ValueError("injected maintenance failure")

    monkeypatch.setattr(runtime_observations, "_plan_runtime_observations_batch", fail)
    sink = RuntimeObservationSink(
        path, writer=writer, retention_ttl_seconds=5, policy=RuntimeObservationConfig(prune_every_writes=1)
    )
    try:
        assert sink.record(kind="mcp.call")
        for _ in range(100):
            if sink.counters.successful_writes:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await sink.aclose()
        assert sink.counters.successful_writes == 1
        assert sink.counters.permanent_failures == 0
    finally:
        await sink.aclose()
        await writer.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_shutdown_drops_only_buffered_jobs_and_settles_accepted_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_close: bool
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    started = threading.Event()
    release = threading.Event()
    original = RuntimeObservationSink._write_batch

    def held_batch(conn: sqlite3.Connection, jobs: list[runtime_observations._ObservationJob]) -> None:
        started.set()
        assert release.wait(timeout=2)
        original(conn, jobs)

    monkeypatch.setattr(RuntimeObservationSink, "_write_batch", staticmethod(held_batch))
    sink = RuntimeObservationSink(
        path,
        writer=writer,
        retention_ttl_seconds=3600,
        policy=replace(RuntimeObservationConfig(), shutdown_drain_grace_seconds=0.01),
    )
    try:
        for _ in range(40):
            assert sink.record(kind="mcp.call")
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        closing_task = asyncio.create_task(sink.aclose())
        await asyncio.sleep(0)
        if cancel_close:
            closing_task.cancel()
        await asyncio.sleep(0.05)
        assert not closing_task.done()
        assert sink.counters.shutdown_grace_drops == 8
        release.set()
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                await closing_task
        else:
            await closing_task
        assert sink.counters.successful_writes == 32
        assert sink.counters.permanent_failures == 0
    finally:
        release.set()
        await sink.aclose()
        await writer.aclose()


@pytest.mark.asyncio
async def test_core_work_runs_between_diagnostic_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []
    original = RuntimeObservationSink._write_batch

    def batch(conn: sqlite3.Connection, jobs: list[runtime_observations._ObservationJob]) -> None:
        order.append("batch")
        entered.set()
        assert release.wait(timeout=2)
        original(conn, jobs)

    monkeypatch.setattr(RuntimeObservationSink, "_write_batch", staticmethod(batch))
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    try:
        for _ in range(65):
            assert sink.record(kind="mcp.call")
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        core = asyncio.create_task(writer.run(lambda _conn: order.append("core")))
        await asyncio.sleep(0)
        release.set()
        await core
        await sink.aclose()
        assert order == ["batch", "core", "batch", "batch"]
        assert sink.counters.successful_writes == 65
    finally:
        release.set()
        await sink.aclose()
        await writer.aclose()


@pytest.mark.asyncio
async def test_shutdown_does_not_submit_prune_after_planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            record_runtime_observation(conn, kind="mcp.call", observed_at_ms=1)

    await writer.run(seed)
    planning = threading.Event()
    release = threading.Event()
    original = runtime_observations._plan_runtime_observations_batch

    def plan(conn: sqlite3.Connection, **kwargs: int) -> runtime_observations._PrunePlan:
        planning.set()
        assert release.wait(timeout=2)
        return original(conn, **kwargs)

    monkeypatch.setattr(runtime_observations, "_plan_runtime_observations_batch", plan)
    sink = RuntimeObservationSink(
        path, writer=writer, retention_ttl_seconds=5, policy=RuntimeObservationConfig(prune_every_writes=1)
    )
    try:
        assert sink.record(kind="mcp.call")
        for _ in range(100):
            if planning.is_set():
                break
            await asyncio.sleep(0.01)
        assert planning.is_set()
        closing_task = asyncio.create_task(sink.aclose())
        await asyncio.sleep(0)
        release.set()
        await closing_task
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runtime_observations").fetchone()[0] == 2
        assert sink.counters.successful_writes == 1
        assert sink.counters.permanent_failures == 0
    finally:
        release.set()
        await sink.aclose()
        await writer.aclose()


@pytest.mark.asyncio
async def test_http_telemetry_admission_precedes_task_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_telegram import server

    waiting = asyncio.Event()

    async def deliver(_event: dict[str, object]) -> None:
        await waiting.wait()

    monkeypatch.setattr(server, "_send_telemetry_event", deliver)
    monkeypatch.setattr(server, "_telemetry_delivery_drops", 0)
    for _ in range(server._TELEMETRY_DELIVERY_CAPACITY + 10):
        server._schedule_telemetry({})
    assert len(server._telemetry_tasks) == server._TELEMETRY_DELIVERY_CAPACITY
    assert server._telemetry_delivery_drops == 10
    waiting.set()
    await server.flush_telemetry()
