from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

import mcp_telegram.runtime_observations as runtime_observations
from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.runtime_observations import (
    RuntimeObservationSink,
    encode_payload,
    prune_runtime_observations,
    record_runtime_observation,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


def test_runtime_event_payload_is_bounded_and_kind_is_allowlisted() -> None:
    assert encode_payload({"value": 1}) == '{"value":1}'
    with pytest.raises(ValueError, match="1024"):
        encode_payload({"value": "x" * 1024})
    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(ValueError, match="unsupported"):
            record_runtime_observation(conn, kind="arbitrary.debug")


def test_runtime_event_pruning_applies_ttl_and_cap(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        for event_id, observed_at in enumerate((1_000, 9_000, 9_100, 9_200), start=1):
            record_runtime_observation(
                conn,
                kind="mcp.call",
                tool_name=f"tool_{event_id}",
                observed_at_ms=observed_at,
            )
        deleted = prune_runtime_observations(conn, ttl_seconds=5, row_cap=2, now_ms=10_000)
        conn.commit()
        assert deleted == 2
        assert conn.execute("SELECT tool_name FROM runtime_observations ORDER BY id").fetchall() == [
            ("tool_3",),
            ("tool_4",),
        ]
        assert conn.execute(
            "SELECT value FROM daemon_state WHERE key='runtime_observations_last_cap_truncation_ms'"
        ).fetchone() == ("10000",)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_runtime_observation_sink_skips_prune_after_close_is_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    record_runtime_observation(conn, kind="telegram.rpc_admission", observed_at_ms=1_000)
    conn.commit()
    inserted = threading.Event()
    release = threading.Event()
    original_record = runtime_observations.record_runtime_observation

    def record_then_wait(conn: sqlite3.Connection, **kwargs: object) -> int:
        result = original_record(conn, **kwargs)  # type: ignore[arg-type]
        inserted.set()
        release.wait(timeout=2)
        return result

    monkeypatch.setattr("mcp_telegram.runtime_observations.time.time", lambda: 10.0)
    monkeypatch.setattr(runtime_observations, "record_runtime_observation", record_then_wait)
    sink = RuntimeObservationSink(
        conn,
        retention_ttl_seconds=5,
        policy=replace(RuntimeObservationConfig(), prune_every_writes=1),
    )

    sink.record(kind="telegram.rpc_admission", outcome="queued")
    assert inserted.wait(timeout=0.5)
    close_task = asyncio.create_task(sink.aclose())
    await asyncio.sleep(0.05)
    assert sink._close_requested.is_set()
    release.set()
    await asyncio.wait_for(close_task, timeout=1.0)

    assert conn.execute("SELECT outcome FROM runtime_observations ORDER BY id").fetchall() == [(None,), ("queued",)]
    conn.close()


@pytest.mark.asyncio
async def test_runtime_observation_sink_waits_for_python_prune_then_joins_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    started = threading.Event()
    release = threading.Event()
    original_prune = runtime_observations.prune_runtime_observations

    def blocked_prune(conn: sqlite3.Connection, *, ttl_seconds: int) -> int:
        started.set()
        release.wait(timeout=2)
        return original_prune(conn, ttl_seconds=ttl_seconds)

    monkeypatch.setattr(runtime_observations, "prune_runtime_observations", blocked_prune)
    sink = RuntimeObservationSink(
        path,
        retention_ttl_seconds=5,
        policy=replace(
            RuntimeObservationConfig(),
            prune_every_writes=1,
            shutdown_drain_grace_seconds=0.05,
        ),
    )
    sink.record(kind="telegram.rpc_admission", outcome="queued")
    assert started.wait(timeout=0.5)

    close_task = asyncio.create_task(sink.aclose())
    await asyncio.sleep(0.1)
    assert not close_task.done()
    release.set()
    await asyncio.wait_for(close_task, timeout=1.0)
    assert not sink._writer.is_alive()


@pytest.mark.asyncio
async def test_runtime_observation_sink_callback_is_prompt_and_fifo_under_contention(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    caller = _open_sync_db(path)
    caller.execute("CREATE TABLE app_work (value TEXT NOT NULL)")
    caller.commit()
    caller.execute("BEGIN IMMEDIATE")
    caller.execute("INSERT INTO app_work VALUES ('caller-work')")
    sink = RuntimeObservationSink(
        path,
        retention_ttl_seconds=5,
        policy=replace(RuntimeObservationConfig(), writer_busy_timeout_ms=1, queue_capacity=16),
    )

    started = time.monotonic()
    for event_id in range(3):
        sink.record(kind="telegram.rpc_admission", outcome=f"event-{event_id}", observed_at_ms=event_id)
    assert time.monotonic() - started < 0.05
    assert caller.in_transaction

    caller.commit()
    await sink.aclose()
    assert caller.execute("SELECT value FROM app_work").fetchall() == [("caller-work",)]
    assert caller.execute("SELECT outcome FROM runtime_observations ORDER BY id").fetchall() == [
        ("event-0",),
        ("event-1",),
        ("event-2",),
    ]
    caller.close()


@pytest.mark.asyncio
async def test_runtime_observation_sink_retries_busy_head_until_fifo_lock_releases(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    lock = sqlite3.connect(path, check_same_thread=False)
    lock.execute("BEGIN IMMEDIATE")
    sink = RuntimeObservationSink(
        path,
        retention_ttl_seconds=5,
        policy=replace(RuntimeObservationConfig(), writer_busy_timeout_ms=1, queue_capacity=32),
    )
    for event_id in range(16):
        sink.record(kind="telegram.rpc_admission", outcome=f"event-{event_id}", observed_at_ms=event_id)

    def release_lock() -> None:
        time.sleep(0.5)
        lock.rollback()
        lock.close()

    releaser = threading.Thread(target=release_lock)
    releaser.start()
    await sink.aclose()
    releaser.join()

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT outcome FROM runtime_observations ORDER BY id").fetchall() == [
            (f"event-{event_id}",) for event_id in range(16)
        ]
    assert sink.busy_retries > 0
    assert sink.permanent_failures == 0
    assert not sink._writer.is_alive()


def test_runtime_observation_sink_logs_aggregate_queue_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    sink = RuntimeObservationSink(
        path,
        retention_ttl_seconds=5,
        policy=replace(RuntimeObservationConfig(), queue_capacity=1),
    )
    started = threading.Event()
    release = threading.Event()

    def block_writer(*_args: object, **_kwargs: object) -> bool:
        started.set()
        release.wait(timeout=1)
        return True

    monkeypatch.setattr(sink, "_write_job", block_writer)
    sink.record(kind="telegram.rpc_admission", outcome="head")
    assert started.wait(timeout=0.5)
    callback_started = time.monotonic()
    for event_id in range(16):
        sink.record(kind="telegram.rpc_admission", outcome=f"overflow-{event_id}")
    assert time.monotonic() - callback_started < 0.05
    release.set()
    sink.close()

    assert sink.queue_full_drops > 0
    assert caplog.text.count("runtime_observation_sink_summary") == 1


@pytest.mark.asyncio
async def test_runtime_observation_sink_survives_a_permanent_job_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    original = runtime_observations.record_runtime_observation
    calls = 0

    def fail_once(conn: sqlite3.Connection, **kwargs: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("permanent test failure")
        return original(conn, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_observations, "record_runtime_observation", fail_once)
    sink = RuntimeObservationSink(
        path,
        retention_ttl_seconds=5,
        policy=replace(RuntimeObservationConfig(), queue_capacity=8),
    )
    sink.record(kind="telegram.rpc_admission", outcome="discarded")
    sink.record(kind="telegram.rpc_admission", outcome="persisted")
    await sink.aclose()

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT outcome FROM runtime_observations ORDER BY id").fetchall() == [("persisted",)]
    assert sink.permanent_failures == 1
    assert "runtime_observation_sink_summary" in caplog.text


@pytest.mark.asyncio
async def test_runtime_observation_sink_interrupts_persistent_lock_after_grace(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    lock = _open_sync_db(path)
    lock.execute("BEGIN IMMEDIATE")
    sink = RuntimeObservationSink(
        path,
        retention_ttl_seconds=5,
        policy=replace(
            RuntimeObservationConfig(),
            writer_busy_timeout_ms=1,
            queue_capacity=8,
            shutdown_drain_grace_seconds=0.8,
        ),
    )
    sink.record(kind="telegram.rpc_admission", outcome="locked")

    started = time.monotonic()
    await asyncio.wait_for(sink.aclose(), timeout=1.0)
    assert time.monotonic() - started < 1.0
    assert sink.busy_retries >= 1
    assert sink.permanent_failures == 0
    assert "runtime_observation_sink_summary" in caplog.text
    assert not sink._writer.is_alive()
    lock.rollback()
    lock.close()


@pytest.mark.asyncio
async def test_runtime_observation_sink_interrupts_long_sqlite_operation_after_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    started = threading.Event()
    original = runtime_observations.record_runtime_observation

    def long_sqlite_operation(conn: sqlite3.Connection, **kwargs: object) -> int:
        started.set()
        conn.execute(
            "WITH RECURSIVE counter(n) AS ("
            "SELECT 1 UNION ALL SELECT n + 1 FROM counter WHERE n < 1000000000"
            ") SELECT sum(n) FROM counter"
        ).fetchone()
        return original(conn, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_observations, "record_runtime_observation", long_sqlite_operation)
    sink = RuntimeObservationSink(
        path,
        retention_ttl_seconds=5,
        policy=replace(RuntimeObservationConfig(), shutdown_drain_grace_seconds=0.05),
    )
    sink.record(kind="telegram.rpc_admission", outcome="interrupted")
    assert started.wait(timeout=0.5)

    await asyncio.wait_for(sink.aclose(), timeout=1.0)
    assert not sink._writer.is_alive()
    assert sink.shutdown_grace_drops == 1
    assert sink.permanent_failures == 0


@pytest.mark.asyncio
async def test_runtime_observation_sink_counts_queued_shutdown_drops_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    started = threading.Event()
    release = threading.Event()

    def blocked_write(*_args: object, **_kwargs: object) -> bool:
        started.set()
        release.wait(timeout=2)
        return True

    sink = RuntimeObservationSink(
        path,
        retention_ttl_seconds=5,
        policy=replace(
            RuntimeObservationConfig(),
            queue_capacity=4,
            shutdown_drain_grace_seconds=0.05,
        ),
    )
    monkeypatch.setattr(sink, "_write_job", blocked_write)
    for event_id in range(5):
        sink.record(kind="telegram.rpc_admission", outcome=f"queued-{event_id}")
    assert started.wait(timeout=0.5)

    close_task = asyncio.create_task(sink.aclose())
    await asyncio.sleep(0.1)
    assert not close_task.done()
    release.set()
    await asyncio.wait_for(close_task, timeout=1.0)

    assert sink.shutdown_grace_drops == 3
    assert sink.queue_full_drops == 1
    assert not sink._writer.is_alive()


def test_runtime_observation_sink_rejects_after_writer_startup_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "missing" / "sync.db"
    sink = RuntimeObservationSink(path, retention_ttl_seconds=5, policy=RuntimeObservationConfig())

    with pytest.raises(RuntimeError, match="writer failed"):
        sink.record(kind="telegram.rpc_admission", outcome="rejected")
    sink.close()

    assert sink.writer_error is not None
    assert sink.startup_failures == 1
    assert sink.rejected_submissions == 1
    assert sink.startup_drops == 0
    assert "runtime_observation_writer_startup_failed" in caplog.text


def test_runtime_observation_sink_counts_jobs_accepted_before_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    entered = threading.Event()
    release = threading.Event()

    def fail_open(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        entered.set()
        release.wait(timeout=2)
        raise OSError("open failed")

    monkeypatch.setattr(runtime_observations.sqlite3, "connect", fail_open)
    sink = RuntimeObservationSink(
        path,
        retention_ttl_seconds=5,
        policy=replace(RuntimeObservationConfig(), queue_capacity=4),
    )
    assert entered.wait(timeout=0.5)
    sink.record(kind="telegram.rpc_admission", outcome="queued-before-failure")
    release.set()
    sink.close()

    assert sink.startup_failures == 1
    assert sink.startup_drops == 1


def test_runtime_observation_sink_does_not_commit_callers_transaction(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("CREATE TABLE app_work (value TEXT NOT NULL)")
    sink = RuntimeObservationSink(
        conn,
        retention_ttl_seconds=5,
        policy=replace(RuntimeObservationConfig(), writer_busy_timeout_ms=25),
    )

    conn.execute("INSERT INTO app_work VALUES ('uncommitted')")
    assert conn.in_transaction
    sink.record(kind="telegram.rpc_admission", outcome="queued")

    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM app_work").fetchone() == (0,)
    # The caller's write transaction holds SQLite's single-writer lock, so a
    # contended admission observation may be dropped after the bounded wait.
    assert conn.execute("SELECT COUNT(*) FROM runtime_observations").fetchone() == (0,)
    sink.close()
    conn.close()


def test_runtime_observation_sink_drops_bounded_lock_contention_and_rejects_after_close(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("CREATE TABLE app_work (value TEXT NOT NULL)")
    sink = RuntimeObservationSink(
        conn,
        retention_ttl_seconds=5,
        policy=replace(RuntimeObservationConfig(), writer_busy_timeout_ms=25),
    )

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO app_work VALUES ('writer-lock')")
    assert sink.record(kind="telegram.rpc_admission", outcome="queued") is None
    assert conn.in_transaction
    conn.rollback()
    sink.close()

    with pytest.raises(RuntimeError, match="sink is closed"):
        sink.record(kind="telegram.rpc_admission", outcome="queued")
    conn.close()
