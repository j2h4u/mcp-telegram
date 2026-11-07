"""Recovery does not invent full-history coverage from a fresh delta timestamp."""

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_telegram.delta_sync import AccessProbePolicy, DeltaAccessProbeDemandAdapter, DeltaSyncWorker
from mcp_telegram.message_history.contracts import ForwardGapPage, HistoryAccessProbeResult
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.reading.sqlite_projection import _build_access_metadata
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget
from tests.helpers import build_mock_message
from tests.history_enrollment_helpers import seed_full_history_enrollment
from tests.history_scope_helpers import HistoryScope
from tests.sqlite_writer_helpers import inline_writer


class _History(HistoryScope):
    def __init__(self) -> None:
        super().__init__()
        self.pages = 0

    async def probe_total_messages(self, dialog_id: int) -> HistoryAccessProbeResult:
        assert dialog_id == 55
        return HistoryAccessProbeResult(2, ())

    async def fetch_page(
        self, dialog_id: int, *, after_message_id: int, should_stop: Callable[[], bool]
    ) -> ForwardGapPage:
        assert dialog_id == 55 and not should_stop()
        self.pages += 1
        if self.pages == 1:
            assert after_message_id == 10
            message = extract_message_row(55, build_mock_message(id=11, text="recovered"))
            return ForwardGapPage((message,), complete=False)
        assert after_message_id == 11
        return ForwardGapPage((), complete=True)


def _metadata(conn: sqlite3.Connection) -> dict:
    status = conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=55").fetchone()[0]
    return _build_access_metadata(conn, 55, status)


async def _run(adapter: DeltaAccessProbeDemandAdapter, now: int) -> None:
    status = adapter.status(float(now))
    assert status is not None and status.selected is not None
    with patch("mcp_telegram.delta_sync.time.time", return_value=now):
        await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))


@pytest.mark.parametrize("depth_complete", [True, False])
async def test_access_recovery_keeps_history_depth_independent_of_delta_progress(
    tmp_path: Path, depth_complete: bool
) -> None:
    path = tmp_path / "recovery.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=123 WHERE singleton=1")
        conn.execute(
            "INSERT INTO synced_dialogs(dialog_id,status,access_lost_at,delta_message_id,last_synced_at,history_depth_complete) "
            "VALUES (55,'access_lost',1,10,100,?)",
            (int(depth_complete),),
        )
        seed_full_history_enrollment(conn, 55, enabled=True)
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES (55,10,10,'retained')")
        conn.commit()
        history = _History()
        worker = DeltaSyncWorker(history, conn, asyncio.Event(), writer=inline_writer(conn))
        adapter = DeltaAccessProbeDemandAdapter(
            worker, AccessProbePolicy(enabled=True, cooldown_seconds=1), history, account_id=123
        )
        expected_horizon = 100 if depth_complete else None

        await _run(adapter, 1000)
        after_probe = _metadata(conn)
        assert (after_probe["dialog_access"], after_probe["last_synced_at"], after_probe["observation_horizon_at"]) == (
            "archived",
            100,
            expected_horizon,
        )

        await _run(adapter, 1100)
        partial = _metadata(conn)
        assert (partial["dialog_access"], partial["last_synced_at"], partial["observation_horizon_at"]) == (
            "archived",
            100,
            expected_horizon,
        )
        assert conn.execute("SELECT text FROM messages WHERE dialog_id=55 ORDER BY message_id").fetchall() == [
            ("retained",),
            ("recovered",),
        ]

        await _run(adapter, 1200)
        complete = _metadata(conn)
        assert complete["dialog_access"] == "unknown"
        assert complete["last_synced_at"] == 1200
        assert complete["history_depth_complete"] is depth_complete
        assert complete["observation_horizon_at"] == (1200 if depth_complete else None)
        assert complete["coverage"] == ("complete_as_of" if depth_complete else "partial")
    finally:
        conn.close()
