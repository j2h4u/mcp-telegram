"""Read-position work survives the daemon API's SQLite row factory."""

import asyncio
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram.daemon import _DaemonClient, _initialize_read_positions, _select_read_position_dialog_ids
from mcp_telegram.message_fact_refresh import ReadReceiptDemandAdapter
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.state import telegram_session_lease
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema, open_sync_db_reader
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkRevision
from tests.test_daemon_api import _insert_synced_dialog, make_server
from tests.test_dialog_directory import _dialog
from tests.test_telegram_recovery_data_plane import _reply
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.mark.parametrize(("limit", "expected"), [(None, [1, 2]), (0, []), (1, [1])])
def test_due_dialog_selection_projects_values_with_daemon_row_factory(
    tmp_path: Path, limit: int | None, expected: list[int]
) -> None:
    path = tmp_path / "selection.db"
    ensure_sync_schema(path)
    with closing(_open_sync_db(path)) as conn, closing(sqlite3.connect(":memory:")) as feedback:
        _insert_synced_dialog(conn, 1, status="synced", enrollment_enabled=True)
        _insert_synced_dialog(conn, 2, status="synced", enrollment_enabled=True)
        make_server(conn, feedback_conn=feedback)
        assert _select_read_position_dialog_ids(conn, limit, now=int(time.time())) == expected
        with pytest.raises(ValueError, match="non-negative"):
            _select_read_position_dialog_ids(conn, -1, now=int(time.time()))


async def test_composed_read_position_dispatches_and_persists(wire: _WireHarness, tmp_path: Path) -> None:
    gate, _, _, sink = wire
    path = tmp_path / "sync.db"
    with closing(_open_sync_db(path)) as seed:
        seed.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        _insert_synced_dialog(seed, 55, status="synced", enrollment_enabled=True)
    conn = open_sync_db_reader(path)
    feedback = sqlite3.connect(":memory:")
    writer = await SQLiteWriter.open(path)
    try:
        assert isinstance(conn.execute("SELECT dialog_id FROM synced_dialogs").fetchone(), tuple)
        with telegram_session_lease(tmp_path) as lease:
            gate.bind_session_lease(lease)
            assert gate.capture_image_scope() is not None
            gate._mb_entity_cache.extend([types.User(55, first_name="Person", access_hash=123)], [])
            make_server(conn, gate, feedback_conn=feedback, writer=writer)
            assert isinstance(conn.execute("SELECT dialog_id FROM synced_dialogs").fetchone(), sqlite3.Row)

            async def run(selected: WorkRevision) -> SliceOutcome:
                return await _initialize_read_positions(
                    cast(_DaemonClient, gate), conn, asyncio.Event(), writer=writer, max_dialogs=100, selected=selected
                )

            adapter = ReadReceiptDemandAdapter(conn, run, account_id=42)
            status = adapter.status(time.time())
            assert status is not None and status.selected is not None
            task = asyncio.create_task(adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected)))
            dialog = _dialog(55, 7)
            dialog.read_inbox_max_id = 7
            dialog.read_outbox_max_id = 5
            response = types.messages.PeerDialogs(
                [dialog],
                [],
                [],
                [],
                types.updates.State(0, 0, datetime.now(UTC), 0, 0),  # pyright: ignore[reportArgumentType]
            )
            await _reply(wire, functions.messages.GetPeerDialogsRequest, response)
            assert (await asyncio.wait_for(task, 3)).kind is SliceOutcomeKind.PROGRESSED
            assert len(sink.frames) == 1
    finally:
        await writer.aclose()
        conn.close()
        feedback.close()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute(
            "SELECT read_inbox_max_id, read_outbox_max_id FROM synced_dialogs WHERE dialog_id=55"
        ).fetchone() == (7, 5)
