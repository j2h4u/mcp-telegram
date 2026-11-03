"""Folder publication has a short claim and no event-loop lock over commit."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest
from telethon.tl import types

import mcp_telegram.sqlite_writer as writer_module
from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.intake import capture_folder_intake
from mcp_telegram.folders.refresh import FolderRefresher
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.folders.telegram_adapter import folder_update_from_native
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import enable_runtime_writes, write_transaction


@pytest.mark.parametrize("change", ["intake", "auth"])
async def test_intake_during_real_commit_revokes_result_and_later_delivery_wins(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as seed:
        seed.execute(
            "UPDATE dialog_directory_publication SET account_id=1,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        seed.commit()
    entered, release, armed = threading.Event(), threading.Event(), threading.Event()

    class BlockingConnection(sqlite3.Connection):
        def commit(self) -> None:
            if armed.is_set():
                armed.clear()
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("commit gate")
            super().commit()

    def open_writer(db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, factory=BlockingConnection)
        conn.execute("PRAGMA foreign_keys=ON")
        enable_runtime_writes(conn)
        return conn

    monkeypatch.setattr(writer_module, "open_runtime_sync_db", open_writer)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    cell = ScopeCell()
    scope = TelegramAuthScope(1, 1, 2, 3)
    cell.publish(scope)

    class Gateway:
        async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
            armed.set()
            return FolderRuleObservation((FolderRule(4, "Old observation"),), "old", started_at)

    try:
        repo = SQLiteFolderSnapshotRepository(reader)
        refresher = FolderRefresher(
            Gateway(), repo, writer=writer, auth_scope=lambda: scope, capture_scope=cell.capture, claim_scope=cell.claim
        )
        task = asyncio.create_task(refresher.refresh(completed_at=100))
        assert await asyncio.to_thread(entered.wait, 3)
        change_id = None
        if change == "intake":
            capture_folder_intake([types.UpdateDialogFilters()])
        else:
            change_id = cell.begin_change()
        heartbeat = asyncio.create_task(asyncio.sleep(0, result="alive"))
        assert await heartbeat == "alive"
        release.set()
        result = await task
        assert not result.accepted and result.observation_order is None
        if change == "auth":
            assert change_id is not None
            cell.end_change(change_id)
            cell.publish(scope)

        def deliver(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                order = allocate_observation_order(conn)
                native = types.DialogFilter(4, types.TextWithEntities("Later", []), [], [], [])
                assert SQLiteFolderSnapshotRepository(conn).apply_supplied_update(
                    folder_update_from_native(types.UpdateDialogFilter(4, native)), account_id=1, order=order, now=101
                )

        await writer.run(deliver)
        assert reader.execute("SELECT title FROM telegram_folder_rules").fetchone()[0] == "Later"
    finally:
        release.set()
        reader.close()
        await writer.aclose()
