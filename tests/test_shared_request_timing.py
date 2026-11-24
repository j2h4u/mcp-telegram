from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from mcp_telegram.request_timing import timing_context
from mcp_telegram.runtime_observations import RuntimeObservationSink
from mcp_telegram.sqlite_writer import SQLiteWriter
from tests.test_daemon_api import make_server
from tests.test_sqlite_writer import _database


class Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record(self, **kwargs: object) -> None:
        self.events.append(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list_dialogs", "get_sync_status"])
async def test_slow_shared_dispatch_records_safe_method(method: str) -> None:
    server = make_server()
    server._policy = replace(server._policy, slow_request_seconds=0.0)
    sink = Sink()
    server.bind_runtime_observation_sink(cast(RuntimeObservationSink, sink))
    response = await server._dispatch_request_with_timing({"method": method}, method, "1234abcd", "a" * 32)
    assert isinstance(response, dict)
    assert len(sink.events) == 1
    event = sink.events[0]
    payload = cast(dict[str, object], event["payload"])
    assert payload["method"] == method
    assert payload["request_id"] == "1234abcd"
    assert event["dialog_id"] is None
    if method == "list_dialogs":
        assert payload["local_projection_ms"] is not None
        assert cast(float, payload["unattributed_upper_bound_ms"]) >= cast(float, payload["local_projection_ms"])
    else:
        assert payload["unattributed_ms"] is not None


@pytest.mark.asyncio
async def test_fast_success_is_not_recorded_and_cancellation_is_recorded() -> None:
    server = make_server()
    sink = Sink()
    server.bind_runtime_observation_sink(cast(RuntimeObservationSink, sink))
    server._dispatch_with_error_projection = AsyncMock(return_value={"ok": True})
    await server._dispatch_request_with_timing({"method": "list_dialogs"}, "list_dialogs", None, "a" * 32)
    assert sink.events == []
    server._dispatch_with_error_projection = AsyncMock(return_value={"ok": False, "error": "private value"})
    await server._dispatch_request_with_timing({"method": "get_sync_status"}, "get_sync_status", None, "a" * 32)
    assert len(sink.events) == 1 and sink.events[0]["outcome"] == "tool_error"
    assert "private value" not in str(sink.events[0])
    sink.events.clear()
    server._dispatch_with_error_projection = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await server._dispatch_request_with_timing({"method": "list_dialogs"}, "list_dialogs", None, "b" * 32)
    assert len(sink.events) == 1 and sink.events[0]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_writer_records_real_queue_and_callback_boundaries(tmp_path: Path) -> None:
    writer = await SQLiteWriter.open(_database(tmp_path))
    try:
        with timing_context("c" * 32) as timing:
            assert timing is not None
            assert await writer.run(lambda conn: cast(int, conn.execute("SELECT 42").fetchone()[0])) == 42
            assert set(timing.writer_phases) == {"admission", "queue", "work"}
            assert all(value >= 0 for value in timing.writer_phases.values())
    finally:
        await writer.aclose()


@pytest.mark.asyncio
async def test_writer_distinguishes_queued_work(tmp_path: Path) -> None:
    import threading

    writer = await SQLiteWriter.open(_database(tmp_path), capacity=2)
    entered, release = threading.Event(), threading.Event()

    def hold(_conn: object) -> None:
        entered.set()
        assert release.wait(5)

    try:
        first = asyncio.create_task(writer.run(hold))
        assert await asyncio.to_thread(entered.wait, 5)
        with timing_context("d" * 32) as timing:
            assert timing is not None
            second = asyncio.create_task(writer.run(lambda _conn: 42))
            await asyncio.sleep(0.02)
            release.set()
            assert await second == 42
            assert timing.writer_phases["queue"] >= 10
        await first
    finally:
        release.set()
        await writer.aclose()


@pytest.mark.asyncio
async def test_slow_dispatch_persists_through_real_observation_sink(tmp_path: Path) -> None:
    import json
    import sqlite3
    from contextlib import closing

    from mcp_telegram.config import RuntimeObservationConfig
    from mcp_telegram.sync_db import ensure_sync_schema

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    try:
        server = make_server()
        server._policy = replace(server._policy, slow_request_seconds=0.0)
        server.bind_runtime_observation_sink(sink)
        await server._dispatch_request_with_timing({"method": "list_dialogs"}, "list_dialogs", "1234abcd", "e" * 32)
        await sink.aclose()
        with closing(sqlite3.connect(path)) as conn:
            row = cast(
                tuple[str, int | None, str] | None,
                conn.execute(
                    "SELECT operation_id, dialog_id, payload_json FROM runtime_observations WHERE kind='daemon.request_timing'"
                ).fetchone(),
            )
        assert row is not None and row[0:2] == ("e" * 32, None)
        payload = cast(dict[str, object], json.loads(row[2]))
        assert payload["method"] == "list_dialogs" and cast(float, payload["local_projection_ms"]) >= 0
        assert payload["request_id"] == "1234abcd"
    finally:
        await sink.aclose()
        await writer.aclose()


@pytest.mark.asyncio
async def test_writer_cancelled_during_admission_is_measured_without_enqueuing(tmp_path: Path) -> None:
    import threading

    writer = await SQLiteWriter.open(_database(tmp_path), capacity=1)
    entered, release = threading.Event(), threading.Event()
    called = False

    def hold(_conn: object) -> None:
        entered.set()
        assert release.wait(5)

    def forbidden(_conn: object) -> None:
        nonlocal called
        called = True

    try:
        first = asyncio.create_task(writer.run(hold))
        assert await asyncio.to_thread(entered.wait, 5)
        with timing_context("f" * 32) as timing:
            assert timing is not None
            waiter = asyncio.create_task(writer.run(forbidden))
            await asyncio.sleep(0.02)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert timing.writer_phases["admission"] >= 10
            assert set(timing.writer_phases) == {"admission"}
            assert not called
        release.set()
        await first
        assert await writer.run(lambda _conn: 42) == 42
    finally:
        release.set()
        await writer.aclose()
