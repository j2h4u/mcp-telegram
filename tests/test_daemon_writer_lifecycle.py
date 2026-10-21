"""Daemon shutdown settles durable receipts and diagnostics before closing their owner."""

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram import daemon
from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.runtime_observations import RuntimeObservationSink
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction


@pytest.mark.asyncio
async def test_shutdown_preserves_finalizer_commit_loss_boundary_and_final_marker(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(
        path,
        writer=writer,
        retention_ttl_seconds=3600,
        policy=RuntimeObservationConfig(queue_capacity=1),
    )
    stages: list[str] = []
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute("INSERT INTO daemon_state(key,value) VALUES ('forbidden','write')")
    assert sink.record(kind="runtime.connection_observed")
    assert not sink.record(kind="runtime.connection_observed")

    def persist_finalizer(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("INSERT INTO daemon_state(key,value) VALUES ('finalizer','committed')")

    class Client:
        async def disconnect(self) -> None:
            stages.append("disconnect")

        async def close_rpc_scheduler(self) -> None:
            await writer.run(persist_finalizer)
            assert reader.execute("SELECT value FROM daemon_state WHERE key='finalizer'").fetchone()[0] == "committed"
            stages.append("receipts_drained")

        def set_rpc_admission_observer(self, _observer: object) -> None:
            stages.append("observer_detached")

        def set_rpc_request_observer(self, _observer: object) -> None:
            pass

        def set_flood_event_observer(self, _observer: object) -> None:
            pass

    class Api:
        async def shutdown(self) -> None:
            stages.append("api_stopped")

    class Observer:
        def flush(self) -> None:
            stages.append("observer_flushed")
            assert sink.record(kind="telegram.rpc_admission", outcome="summary")

    feedback_conn = sqlite3.connect(":memory:")
    ctx = cast(
        daemon._SyncMainContext,
        SimpleNamespace(
            conn=reader,
            writer=writer,
            feedback_conn=feedback_conn,
            shutdown_event=asyncio.Event(),
            client=Client(),
            api_server=Api(),
            socket_path=tmp_path / "daemon.sock",
            unix_server=None,
            background_tasks=set(),
            coordinator=None,
            handler_manager=None,
            draft_owner=None,
            demand_runtime=None,
            rpc_admission_observer=Observer(),
            rpc_observation_sink=sink,
        ),
    )
    try:
        await daemon._shutdown_sync_main_context(ctx)
        assert ctx.shutdown_event.is_set()
        assert stages == ["api_stopped", "disconnect", "receipts_drained", "observer_detached", "observer_flushed"]
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            reader.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="unavailable"):
            await writer.run(lambda _conn: None)
        with closing(open_sync_db_reader(path)) as check:
            rows = cast(
                list[tuple[str, str]],
                check.execute("SELECT kind,payload_json FROM runtime_observations ORDER BY id").fetchall(),
            )
            assert [row[0] for row in rows] == [
                "runtime.connection_observed",
                "telegram.rpc_admission",
                "runtime.telemetry_loss",
                "runtime.stopped",
            ]
            loss_payload = cast(dict[str, object], json.loads(rows[-2][1]))
            assert loss_payload["queue_full_drops"] == 1
            assert (
                check.execute(
                    "SELECT value FROM daemon_state WHERE key='runtime_observations_last_queue_full_drops'"
                ).fetchone()[0]
                == "1"
            )
    finally:
        await sink.aclose()
        await writer.aclose()
        reader.close()
        feedback_conn.close()
