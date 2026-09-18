"""Focused tests for bounded forward history publication."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Protocol, cast

import pytest

from helpers import build_mock_message
from mcp_telegram.delta_sync import DeltaGapFillDemandAdapter, DeltaSyncWorker
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.history_enrollment import disable_history
from mcp_telegram.message_contracts import ExtractedMessage
from mcp_telegram.message_history.contracts import (
    ForwardGapPage,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    current_rpc_scope,
)
from tests.history_enrollment_helpers import seed_full_history_enrollment


class _SQLiteCursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...


class _SQLiteConnection(Protocol):
    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _SQLiteCursor: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...


@pytest.fixture()
def sync_db(tmp_path: Path) -> Iterator[_SQLiteConnection]:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = cast(_SQLiteConnection, _open_sync_db(db_path))
    yield conn
    conn.close()


def _seed_dialog(conn: _SQLiteConnection, dialog_id: int, *, refresh_requested_at: int | None = None) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs(dialog_id, status, delta_refresh_requested_at) VALUES (?, 'synced', ?)",
        (dialog_id, refresh_requested_at),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=True)
    conn.commit()


def _message(dialog_id: int, message_id: int) -> ExtractedMessage:
    return extract_message_row(dialog_id, build_mock_message(id=message_id, text=str(message_id)))


class _ForwardPort:
    def __init__(
        self, pages: Sequence[ForwardGapPage | BaseException | str], conn: _SQLiteConnection | None = None
    ) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[int, bool]] = []
        self.conn = conn
        self.disable_after_fetch = False

    async def fetch_page(
        self,
        dialog_id: int,
        *,
        after_message_id: int,
        should_stop: Callable[[], bool],
    ) -> ForwardGapPage:
        self.calls.append((after_message_id, should_stop()))
        page = self.pages.pop(0)
        if isinstance(page, str):
            if page == "deferred":
                raise TelegramRpcAdmissionDeferred(retry_after_seconds=7)
            if page == "saturated":
                raise RpcAdmissionSaturatedError(current_rpc_scope(), "full")
            if page == "expired":
                raise RpcAdmissionExpiredError(current_rpc_scope(), "expired")
            if page == "flood":
                raise TelegramRpcThrottled(retry_after_seconds=3)
            raise MessageHistoryUnavailableError("ordinary failure")
        if isinstance(page, BaseException):
            raise page
        if self.disable_after_fetch and self.conn is not None:
            disable_history(cast(sqlite3.Connection, self.conn), dialog_id, now=2)
        return page


@pytest.mark.asyncio
async def test_zero_baseline_clears_refresh_without_rpc(sync_db: _SQLiteConnection) -> None:
    dialog_id = 100
    _seed_dialog(sync_db, dialog_id, refresh_requested_at=1)
    port = _ForwardPort([])
    worker = DeltaSyncWorker(port, cast(sqlite3.Connection, sync_db), asyncio.Event())

    assert await worker.fetch_delta_slice_for_dialog(dialog_id) == 0
    assert port.calls == []
    row = sync_db.execute(
        "SELECT last_delta_checked_at, delta_refresh_requested_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert row[1] is None


@pytest.mark.asyncio
async def test_exact_page_continues_then_partial_page_completes(sync_db: _SQLiteConnection) -> None:
    dialog_id = 101
    _seed_dialog(sync_db, dialog_id, refresh_requested_at=1)
    sync_db.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (?, ?, 1)", (dialog_id, 100))
    sync_db.commit()
    first = ForwardGapPage(tuple(_message(dialog_id, value) for value in range(101, 201)), complete=True)
    second = ForwardGapPage((_message(dialog_id, 201),), complete=True)
    port = _ForwardPort([first, second])
    worker = DeltaSyncWorker(port, cast(sqlite3.Connection, sync_db), asyncio.Event())

    assert await worker.fetch_delta_slice_for_dialog(dialog_id) == 100
    first_checkpoint = sync_db.execute(
        "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert first_checkpoint is not None and first_checkpoint[0] is not None
    assert await worker.fetch_delta_slice_for_dialog(dialog_id) == 1
    assert port.calls == [(100, False), (200, False)]
    assert sync_db.execute("SELECT MAX(message_id) FROM messages WHERE dialog_id=?", (dialog_id,)).fetchone() == (201,)
    final_checkpoint = sync_db.execute(
        "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert final_checkpoint is not None and final_checkpoint[0] is None
    assert worker._last_delta_slice_completed is True


@pytest.mark.asyncio
async def test_restart_uses_committed_forward_checkpoint(sync_db: _SQLiteConnection) -> None:
    dialog_id = 102
    _seed_dialog(sync_db, dialog_id, refresh_requested_at=1)
    sync_db.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (?, ?, 1)", (dialog_id, 10))
    sync_db.commit()
    first_port = _ForwardPort([ForwardGapPage((_message(dialog_id, 11),), complete=False)])
    first_worker = DeltaSyncWorker(first_port, cast(sqlite3.Connection, sync_db), asyncio.Event())

    assert await first_worker.fetch_delta_slice_for_dialog(dialog_id) == 1
    second_port = _ForwardPort([ForwardGapPage((), complete=True)])
    second_worker = DeltaSyncWorker(second_port, cast(sqlite3.Connection, sync_db), asyncio.Event())

    assert await second_worker.fetch_delta_slice_for_dialog(dialog_id) == 0
    assert second_port.calls == [(11, False)]


@pytest.mark.asyncio
async def test_interrupted_forward_page_publishes_rows_and_continuation(sync_db: _SQLiteConnection) -> None:
    dialog_id = 103
    _seed_dialog(sync_db, dialog_id, refresh_requested_at=1)
    sync_db.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (?, ?, 1)", (dialog_id, 10))
    sync_db.commit()
    port = _ForwardPort([ForwardGapPage((_message(dialog_id, 11),), complete=False)])
    worker = DeltaSyncWorker(port, cast(sqlite3.Connection, sync_db), asyncio.Event())

    assert await worker.fetch_delta_slice_for_dialog(dialog_id) == 1
    assert sync_db.execute(
        "SELECT COUNT(*) FROM messages WHERE dialog_id=? AND message_id=11", (dialog_id,)
    ).fetchone() == (1,)
    row = sync_db.execute(
        "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone()
    assert row is not None and row[0] is not None and row[1] is None


@pytest.mark.asyncio
async def test_disable_after_fetch_discards_rows_and_checkpoint(sync_db: _SQLiteConnection) -> None:
    dialog_id = 104
    _seed_dialog(sync_db, dialog_id, refresh_requested_at=1)
    sync_db.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (?, ?, 1)", (dialog_id, 10))
    sync_db.commit()
    port = _ForwardPort([ForwardGapPage((_message(dialog_id, 11),), complete=True)], sync_db)
    port.disable_after_fetch = True
    worker = DeltaSyncWorker(port, cast(sqlite3.Connection, sync_db), asyncio.Event())

    assert await worker.fetch_delta_slice_for_dialog(dialog_id) == 0
    assert sync_db.execute(
        "SELECT COUNT(*) FROM messages WHERE dialog_id=? AND message_id=11", (dialog_id,)
    ).fetchone() == (0,)
    assert sync_db.execute(
        "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone() == (None, None)


@pytest.mark.asyncio
async def test_access_loss_marks_dialog_without_publishing(sync_db: _SQLiteConnection) -> None:
    dialog_id = 105
    _seed_dialog(sync_db, dialog_id, refresh_requested_at=1)
    sync_db.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (?, ?, 1)", (dialog_id, 10))
    sync_db.commit()
    port = _ForwardPort([MessageHistoryAccessLostError("gone", reason_code="ChannelPrivateError")])
    worker = DeltaSyncWorker(port, cast(sqlite3.Connection, sync_db), asyncio.Event())

    assert await worker.fetch_delta_slice_for_dialog(dialog_id) == 0
    access_row = sync_db.execute(
        "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
    ).fetchone()
    assert access_row is not None and access_row[0] == "access_lost"
    assert sync_db.execute(
        "SELECT reason_code FROM conversation_history_events WHERE dialog_id=?", (dialog_id,)
    ).fetchone() == ("ChannelPrivateError",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_kind",
    [
        "deferred",
        "saturated",
        "expired",
        "flood",
        "ordinary",
    ],
)
async def test_page_failures_preserve_checkpoint_and_reach_demand_coordinator(
    sync_db: _SQLiteConnection, error_kind: str
) -> None:
    dialog_id = 106
    _seed_dialog(sync_db, dialog_id, refresh_requested_at=1)
    sync_db.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (?, ?, 1)", (dialog_id, 10))
    sync_db.commit()
    worker = DeltaSyncWorker(
        _ForwardPort([error_kind]),
        cast(sqlite3.Connection, sync_db),
        asyncio.Event(),
    )
    adapter = DeltaGapFillDemandAdapter(worker)

    expected_error = {
        "deferred": TelegramRpcAdmissionDeferred,
        "saturated": RpcAdmissionSaturatedError,
        "expired": RpcAdmissionExpiredError,
        "flood": TelegramRpcThrottled,
        "ordinary": MessageHistoryUnavailableError,
    }[error_kind]
    with pytest.raises(expected_error):
        await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert sync_db.execute(
        "SELECT delta_refresh_requested_at, last_delta_checked_at FROM synced_dialogs WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone() == (1, None)
