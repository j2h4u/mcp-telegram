"""The installed Telethon loop survives local channel access decisions."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from telethon import functions, types
from telethon._updates import ChannelState, PrematureEndReason, SessionState
from telethon.sessions import SQLiteSession

from mcp_telegram.peer_access import PeerAccess, revoke_peer
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc import TelegramRpcGate
from test_telegram_rpc import _set_sender
from test_update_delivery import DATE, _client, _database, _prepare_migration_transport


async def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer, channel=False)
    client._delivery_repository = None
    await client.verify_startup_account(99)
    scope = client.retained_auth_scope()
    assert scope is not None
    access = PeerAccess(scope)
    await writer.run(access.bind)
    client.bind_peer_access(access)
    _prepare_migration_transport(client, monkeypatch)
    client._catch_up = False
    client._message_box.load(
        SessionState(99, 0, False, 10, 3, int(DATE.timestamp()), 0, None),
        [ChannelState(123, 20), ChannelState(124, 30)],
    )
    client._mb_entity_cache.extend(
        [], [types.ChannelForbidden(123, 456, "lost"), types.ChannelForbidden(124, 457, "healthy")]
    )
    return path, writer, client, access


async def _close(writer: SQLiteWriter, client: TelegramRpcGate, access: PeerAccess, task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await client._disconnect_main_sender()
    cast(SQLiteSession, client.session).close()
    await writer.run(access.unbind)
    await writer.aclose()
    tasks = cast(list[asyncio.Task[object]], vars(client)["_test_tasks"])
    for pending in tasks:
        pending.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.close_rpc_scheduler()


async def test_lost_channel_does_not_disconnect_or_block_healthy_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, writer, client, access = await _setup(tmp_path, monkeypatch)
    lost = -1000000000123

    def persist_loss(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            revoke_peer(conn, lost)
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (?, 'access_lost')", (lost,))
            conn.execute(
                "INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES (?, 1, 1, 'retained')", (lost,)
            )

    await writer.run(persist_loss)
    sent: list[int] = []
    progressed = asyncio.Event()

    def respond(request: object) -> object:
        assert isinstance(request, functions.updates.GetChannelDifferenceRequest)
        sent.append(cast(types.InputChannel, request.channel).channel_id)
        progressed.set()
        return types.updates.ChannelDifferenceEmpty(pts=31, final=True)

    _set_sender(client, respond)
    box = client._message_box
    original_end = box.end_channel_difference

    def end(request: functions.updates.GetChannelDifferenceRequest, reason: PrematureEndReason, cache: object) -> None:
        original_end(request, reason, cache)
        if cast(types.InputChannel, request.channel).channel_id == 123:
            box.try_begin_get_diff(124, "healthy work")

    monkeypatch.setattr(box, "end_channel_difference", end)
    box.try_begin_get_diff(123, "fixture gap")
    task = asyncio.create_task(client._update_loop())
    try:
        await asyncio.wait_for(progressed.wait(), 3)
        for _ in range(100):
            if box.map[124].pts == 31:
                break
            await asyncio.sleep(0)
        assert sent == [124]
        assert 123 not in box.map and box.map[124].pts == 31
        assert client._updates_error is None and client.is_connected() and not task.done()
        assert client.account_safety_status() is None
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (lost,)).fetchone() == (
                "access_lost",
            )
            assert conn.execute("SELECT text FROM messages WHERE dialog_id=?", (lost,)).fetchone() == ("retained",)
    finally:
        await _close(writer, client, access, task)


@pytest.mark.parametrize("failure", ["pending_write", "identity_unavailable"])
async def test_temporary_access_failure_retries_same_gap_when_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _, writer, client, access = await _setup(tmp_path, monkeypatch)
    backoff, resume, progressed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep

    async def controlled_sleep(delay: float, result: object = None) -> object:
        if delay == 5:
            backoff.set()
            await resume.wait()
            return result
        return await original_sleep(delay, result)

    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
    sent: list[int] = []

    def respond(request: object) -> object:
        assert isinstance(request, functions.updates.GetChannelDifferenceRequest)
        sent.append(cast(types.InputChannel, request.channel).channel_id)
        progressed.set()
        return types.updates.ChannelDifferenceEmpty(pts=21, final=True)

    _set_sender(client, respond)
    box = client._message_box
    original_scope = client.retained_auth_scope
    if failure == "pending_write":
        access.revoke(-1000000000123)
    else:
        monkeypatch.setattr(client, "retained_auth_scope", lambda: None)
    box.try_begin_get_diff(123, "fixture gap")
    task = asyncio.create_task(client._update_loop())
    try:
        await asyncio.wait_for(backoff.wait(), 3)
        assert sent == [] and box.map[123].pts == 20 and 123 in box.getting_diff_for
        assert client._updates_error is None and client.is_connected() and not task.done()
        await writer.run(access.reconcile)
        monkeypatch.setattr(client, "retained_auth_scope", original_scope)
        resume.set()
        await asyncio.wait_for(progressed.wait(), 3)
        for _ in range(100):
            if box.map[123].pts == 21:
                break
            await original_sleep(0)
        assert sent == [123] and box.map[123].pts == 21
        assert client._updates_error is None and client.is_connected() and not task.done()
        assert client.account_safety_status() is None
    finally:
        resume.set()
        await _close(writer, client, access, task)
