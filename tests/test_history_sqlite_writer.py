"""History orchestration uses the real owner with read-only loop connections."""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from mcp_telegram.delta_sync import DeltaSyncWorker
from mcp_telegram.message_history.contracts import ForwardGapPage, FullHistoryPage, MessageHistoryAccessLostError
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_worker import FullSyncWorker
from tests.helpers import build_mock_message
from tests.history_enrollment_helpers import seed_full_history_enrollment


@pytest.mark.asyncio
async def test_history_workers_commit_through_owner_with_readonly_readers(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with _open_sync_db(path) as setup:
        setup.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'not_synced')")
        seed_full_history_enrollment(setup, 1, enabled=True)
    setup.close()
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)

    class FullPort:
        async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
            if before_message_id == 0:
                return FullHistoryPage((extract_message_row(dialog_id, build_mock_message(id=2, text="full")),), 1, 2)
            return FullHistoryPage((), 0, None)

    class DeltaPort:
        async def fetch_page(
            self, dialog_id: int, *, after_message_id: int, should_stop: Callable[[], bool]
        ) -> ForwardGapPage:
            return ForwardGapPage(
                (extract_message_row(dialog_id, build_mock_message(id=3, text="delta")),), complete=True
            )

    try:
        full = FullSyncWorker(FullPort(), reader, asyncio.Event(), writer=writer)
        assert not await full.process_one_batch()
        assert await full.process_one_batch()
        assert reader.execute("SELECT status FROM synced_dialogs WHERE dialog_id=1").fetchone() == ("synced",)
        delta = DeltaSyncWorker(DeltaPort(), reader, asyncio.Event(), writer=writer)
        assert await delta.fetch_delta_slice_for_dialog(1) == 1
        assert delta._last_delta_slice_succeeded
        assert reader.execute("SELECT last_delta_checked_at FROM synced_dialogs WHERE dialog_id=1").fetchone()[0]
        assert reader.execute("SELECT message_id FROM messages ORDER BY message_id").fetchall() == [(2,), (3,)]
        assert reader.execute("SELECT delta_message_id FROM synced_dialogs WHERE dialog_id=1").fetchone() == (3,)
        assert reader.execute(
            "SELECT value FROM daemon_state WHERE key='canonical_observation_sequence'"
        ).fetchone() == ("3",)
    finally:
        await writer.aclose()
        reader.close()


@pytest.mark.asyncio
async def test_delta_access_loss_is_committed_before_reporting_it(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with _open_sync_db(path) as setup:
        setup.execute("INSERT INTO synced_dialogs(dialog_id,status,delta_refresh_requested_at) VALUES (1,'synced',1)")
        seed_full_history_enrollment(setup, 1, enabled=True)
    setup.close()
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)

    class LostAccessPort:
        async def fetch_page(
            self, dialog_id: int, *, after_message_id: int, should_stop: Callable[[], bool]
        ) -> ForwardGapPage:
            raise MessageHistoryAccessLostError("private", reason_code="ChannelPrivateError")

    try:
        worker = DeltaSyncWorker(LostAccessPort(), reader, asyncio.Event(), writer=writer)
        assert await worker.fetch_delta_slice_for_dialog(1) == 0
        assert worker._last_delta_slice_access_lost
        assert not worker._last_delta_slice_succeeded
        assert reader.execute("SELECT status FROM synced_dialogs WHERE dialog_id=1").fetchone() == ("access_lost",)
        assert reader.execute("SELECT reason_code FROM conversation_history_events WHERE dialog_id=1").fetchone() == (
            "ChannelPrivateError",
        )
    finally:
        await writer.aclose()
        reader.close()
