"""Access-loss fences at history acquisition await boundaries."""

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import cast

import pytest
from telethon.tl.types import InputPeerUser
from telethon.tl.types.messages import Messages

from mcp_telegram.activity_cold_backfill import _cold_claim_matches
from mcp_telegram.activity_hot_sweep import _begin_hot_window, _hot_state_matches
from mcp_telegram.activity_peer_sweep import PeerSweepRequest, enroll_activity_dialog, sweep_peer_once
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.message_history.contracts import FullHistoryPage, HistoryAccessProbeResult
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import _apply_migrations
from mcp_telegram.sync_worker import FullSyncWorker
from tests.history_enrollment_helpers import seed_full_history_enrollment
from tests.sqlite_writer_helpers import inline_writer

_scope_cell = ScopeCell()
assert _scope_cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 123, 2, 99))


class _ProbeScope:
    def capture_scope(self) -> ScopeToken | None:
        return _scope_cell.capture()

    def claim_scope(self, token: ScopeToken) -> bool:
        return _scope_cell.claim(token)


@pytest.mark.parametrize("loss_at", ["resolve", "search"])
@pytest.mark.parametrize("tier", ["hot", "cold"])
async def test_page_rejects_access_loss_during_await(monkeypatch: pytest.MonkeyPatch, loss_at: str, tier: str) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        _apply_migrations(conn)
        conn.execute("UPDATE dialog_directory_state SET account_id=123 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(1,'synced')")
        conn.commit()
        enroll_activity_dialog(conn, 1, "user", last_activity_at=1)
        conn.commit()
        state = _begin_hot_window(conn, 1)
        assert state is not None
        conn.execute("UPDATE activity_dialog_state SET cold_status='running',cold_next_retry_at=100 WHERE dialog_id=1")
        conn.commit()

        def current(db: sqlite3.Connection) -> bool:
            return _hot_state_matches(db, state) if tier == "hot" else _cold_claim_matches(db, 1, 0, 100)

        entered, release = asyncio.Event(), asyncio.Event()
        searches = 0

        class Client:
            async def __call__(self, request: object) -> object:
                raise AssertionError("Search must use the governed request boundary")

            async def get_input_entity(self, dialog_id: int) -> InputPeerUser:
                if loss_at == "resolve":
                    entered.set()
                    await release.wait()
                return InputPeerUser(dialog_id, 1)

        async def search(client: object, request: object, *, timeout_s: float) -> Messages:
            nonlocal searches
            searches += 1
            if loss_at == "search":
                entered.set()
                await release.wait()
            return Messages(messages=[], users=[], chats=[], topics=[])

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", search)
        request = PeerSweepRequest(Client(), conn, 1, 0, 0, 20, 1, writer=inline_writer(conn), accept_page=current)
        task = asyncio.create_task(sweep_peer_once(request))
        await entered.wait()
        conn.execute("UPDATE synced_dialogs SET status='access_lost' WHERE dialog_id=1")
        conn.commit()
        release.set()
        result = await task
        assert not result.accepted
        assert searches == (1 if loss_at == "search" else 0)
        assert not current(conn)
        assert conn.execute(
            "SELECT hot_cursor,hot_page_offset_id FROM activity_dialog_state WHERE dialog_id=1"
        ).fetchone() == (None, 0)


async def test_total_repair_rechecks_access_after_cursor_writer() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        _apply_migrations(conn)
        conn.execute("UPDATE dialog_directory_state SET account_id=123 WHERE singleton=1")
        for dialog_id in (1, 2):
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(?,'synced')", (dialog_id,))
            seed_full_history_enrollment(conn, dialog_id, enabled=True)
        conn.commit()
        entered, release = asyncio.Event(), asyncio.Event()
        calls: list[int] = []

        class Writer:
            async def run[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
                result = operation(conn)
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                return result

        class Port(_ProbeScope):
            async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
                raise AssertionError("unexpected history")

            async def probe_total_messages(self, dialog_id: int) -> HistoryAccessProbeResult:
                calls.append(dialog_id)
                return HistoryAccessProbeResult(17, ())

        port = Port()
        worker = FullSyncWorker(
            port, conn, asyncio.Event(), total_messages_probe=port, writer=cast(SQLiteWriter, Writer())
        )
        task = asyncio.ensure_future(worker.repair_one_total_messages(expected_dialog_id=1))
        await entered.wait()
        conn.execute("UPDATE synced_dialogs SET status='access_lost' WHERE dialog_id=1")
        conn.commit()
        release.set()
        await task
        assert calls == []
        assert conn.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id=1").fetchone() == (None,)
        await worker.repair_one_total_messages(expected_dialog_id=2)
        assert calls == [2]
        assert conn.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id=2").fetchone() == (17,)
        assert conn.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id=1").fetchone() == (1,)


async def test_total_repair_discards_result_after_access_loss() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        _apply_migrations(conn)
        conn.execute("UPDATE dialog_directory_state SET account_id=123 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status,full_sync_retry_at) VALUES(1,'synced',1)")
        seed_full_history_enrollment(conn, 1, enabled=True)
        conn.commit()
        entered, release = asyncio.Event(), asyncio.Event()

        class Port(_ProbeScope):
            async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
                raise AssertionError("unexpected history")

            async def probe_total_messages(self, dialog_id: int) -> HistoryAccessProbeResult:
                entered.set()
                await release.wait()
                return HistoryAccessProbeResult(17, ())

        port = Port()
        worker = FullSyncWorker(port, conn, asyncio.Event(), total_messages_probe=port, writer=inline_writer(conn))
        task = asyncio.ensure_future(worker.repair_one_total_messages(expected_dialog_id=1))
        await entered.wait()
        conn.execute("UPDATE synced_dialogs SET status='access_lost' WHERE dialog_id=1")
        conn.commit()
        release.set()
        await task
        assert conn.execute(
            "SELECT total_messages,full_sync_retry_at FROM synced_dialogs WHERE dialog_id=1"
        ).fetchone() == (None, 1)
        assert conn.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id=1").fetchone() == (1,)
