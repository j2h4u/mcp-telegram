"""A retired sole writer must not leave a ready daemon alive."""

import asyncio
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram.daemon import (
    _create_tracked_task,
    _ensure_folder_projection,
    _monitor_sqlite_writer,
    _SyncMainContext,
)
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader


@dataclass
class _ReadyAPI:
    _ready: bool = True
    startup_detail: str = ""


@pytest.mark.asyncio
async def test_retired_owner_clears_readiness_and_requests_shutdown(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    shutdown = asyncio.Event()
    api = _ReadyAPI()
    ctx = cast(
        _SyncMainContext,
        SimpleNamespace(
            writer=writer,
            shutdown_event=shutdown,
            api_server=api,
            rpc_observation_sink=None,
            background_tasks=set(),
        ),
    )
    monitor = _create_tracked_task(ctx, _monitor_sqlite_writer(ctx), name="sqlite_write_owner", critical=True)
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            await writer.run(lambda conn: conn.close())
        with pytest.raises(RuntimeError, match="SQLite write owner failed"):
            await monitor
        await asyncio.wait_for(shutdown.wait(), timeout=1)
        assert not api._ready
        assert "sqlite_write_owner" in api.startup_detail
    finally:
        await writer.aclose()


@pytest.mark.asyncio
async def test_folder_read_without_due_expiry_does_not_wait_for_busy_owner(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    started, release = threading.Event(), threading.Event()

    def pause(_conn: sqlite3.Connection) -> None:
        started.set()
        assert release.wait(2)

    pending = asyncio.create_task(writer.run(pause))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert (
            await asyncio.wait_for(_ensure_folder_projection(SQLiteFolderSnapshotRepository(reader), writer), 0.2)
            is None
        )
        assert not pending.done()
    finally:
        release.set()
        await pending
        await writer.aclose()
        reader.close()
