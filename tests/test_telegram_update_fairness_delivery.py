"""The vendor update loop preserves canonical delivery while sharing catch-up turns."""

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon import types
from telethon._updates import ChannelState, SessionState

from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred
from mcp_telegram.telegram_update_admission import GateMessageBox, UpdateIntakeQueue
from mcp_telegram.update_delivery import UpdateDeliveryRepository
from tests.test_daemon_api import _close_tracked_sqlite_connections as _close_tracked_sqlite_connections
from tests.test_daemon_api import make_server
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire
from tests.test_update_delivery import _message, _pending, _safe
from tests.test_update_delivery_handlers import _manager, _seed


@dataclass
class _Delivery:
    wire: _WireHarness
    path: Path
    reader: sqlite3.Connection
    writer: SQLiteWriter
    now: datetime
    box: GateMessageBox
    extra_channels: list[int]


@pytest.fixture
async def delivery(wire: _WireHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Delivery]:
    gate, sender, _, _ = wire
    path = tmp_path / "delivery.sqlite"
    ensure_sync_schema(path)
    _seed(path, 55)
    _seed(path, -1000000000123)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    try:
        now = datetime.now(UTC)
        gate._mb_entity_cache.set_self_user(42, False, 999)
        channel = types.ChannelForbidden(123, 456, "fixture")
        session = gate.session
        assert session is not None
        session.process_entities(types.contacts.ResolvedPeer(types.PeerChannel(123), [channel], []))
        gate._mb_entity_cache.extend([types.User(55, access_hash=555, first_name="Fixture")], [channel])
        box = cast(GateMessageBox, gate._message_box)
        box.load(SessionState(42, 2, False, 10, 0, int(now.timestamp()), 1, None), [ChannelState(123, 20)])
        sender._user_connected = False
        gate.configure_update_delivery(UpdateDeliveryRepository(writer))
        sender._user_connected = True
        manager = _manager(gate, reader, writer)
        manager.set_self_id(42)
        gate._authorized = True
        gate._catch_up = False
        await gate.start_update_delivery(42)
        restore = gate._restore_delivery_message_box

        extra_channels: list[int] = []

        def restore_with_gap(account_id: int) -> None:
            restore(account_id)
            box.try_begin_get_diff(123, "fairness integration")
            for channel_id in extra_channels:
                extra = types.ChannelForbidden(channel_id, channel_id * 2, "fixture")
                gate._mb_entity_cache.extend([], [extra])
                session.process_entities(types.contacts.ResolvedPeer(types.PeerChannel(channel_id), [extra], []))
                box.try_set_channel_state(channel_id, 20)
                box.try_begin_get_diff(channel_id, "fairness integration")

        monkeypatch.setattr(gate, "_restore_delivery_message_box", restore_with_gap)
        yield _Delivery(wire, path, reader, writer, now, box, extra_channels)
    finally:
        await writer.aclose()
        reader.close()


def _update(now: datetime, message_id: int, pts: int, *, channel: bool = False) -> types.UpdateShort:
    message = _message(message_id, channel=channel)
    update = types.UpdateNewChannelMessage(message, pts, 1) if channel else types.UpdateNewMessage(message, pts, 1)
    return types.UpdateShort(update, now)


async def _wait_message(reader: sqlite3.Connection, dialog_id: int, message_id: int) -> None:
    async with asyncio.timeout(3):
        while (
            reader.execute(
                "SELECT 1 FROM messages WHERE dialog_id=? AND message_id=?", (dialog_id, message_id)
            ).fetchone()
            is None
        ):
            await asyncio.sleep(0)


async def _stop(loop: asyncio.Task[None]) -> None:
    loop.cancel()
    await loop


async def test_dm_input_commits_before_unsent_channel_and_local_read_uses_no_rpc(delivery: _Delivery) -> None:
    gate, sender, _, sink = delivery.wire
    delivery.extra_channels.extend([124, 125])
    gate._updates_queue.put_nowait(_update(delivery.now, 11, 11))
    loop = asyncio.create_task(gate._update_loop())
    try:
        await _wait_message(delivery.reader, 55, 11)
        await asyncio.wait_for(sender._send_queue.next_state(), 3)
        assert not sink.frames
        async with asyncio.timeout(3):
            while _pending(delivery.path) is not None:
                await asyncio.sleep(0)
        assert _safe(delivery.path).pts == 11
        before = len(sink.frames)
        api = make_server(delivery.reader, gate)
        api.self_id = 42
        response = await api._list_messages({"dialog_id": 55, "limit": 20, "message_state": "sent"})
        assert response["ok"] is True and "fixture" in str(response["data"])
        assert len(sink.frames) == before
    finally:
        await _stop(loop)


@pytest.mark.parametrize("outgoing", [False, True])
async def test_full_push_body_reaches_local_read_without_refetch(delivery: _Delivery, outgoing: bool) -> None:
    gate, _, _, sink = delivery.wire
    message = _message(11)
    message.out = outgoing
    if outgoing:
        message.from_id = types.PeerUser(42)
    message.message = "primary push body"
    gate._updates_queue.put_nowait(types.UpdateShort(types.UpdateNewMessage(message, 11, 1), delivery.now))
    loop = asyncio.create_task(gate._update_loop())
    try:
        await _wait_message(delivery.reader, 55, 11)
        async with asyncio.timeout(3):
            while _pending(delivery.path) is not None:
                await asyncio.sleep(0)
        assert delivery.reader.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=11").fetchone() == (
            "primary push body",
        )
        assert delivery.reader.execute(
            "SELECT message_id FROM messages_fts WHERE messages_fts MATCH ? AND dialog_id = ?", ("primary", 55)
        ).fetchone() == (11,)
        api = make_server(delivery.reader, gate)
        api.self_id = 42
        response = await api._list_messages({"dialog_id": 55, "limit": 20, "message_state": "sent"})
        assert response["ok"] is True and "primary push body" in str(response["data"])
        assert not sink.frames
    finally:
        await _stop(loop)


async def test_claimed_channel_response_then_newer_pts_are_serially_persisted(delivery: _Delivery) -> None:
    gate, sender, connection, sink = delivery.wire
    loop = asyncio.create_task(gate._update_loop())
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        older = types.updates.ChannelDifference(
            pts=21,
            new_messages=[_message(13, channel=True)],
            other_updates=[],
            chats=[types.ChannelForbidden(123, 456, "fixture")],
            users=[],
            final=True,
        )
        gate._updates_queue.put_nowait(_update(delivery.now, 14, 22, channel=True))
        await asyncio.sleep(0)
        assert cast(UpdateIntakeQueue, gate._updates_queue).real_count == 1
        assert delivery.reader.execute("SELECT COUNT(*) FROM messages WHERE message_id IN (13,14)").fetchone() == (0,)
        state.future.set_result(older)
        await _wait_message(delivery.reader, -1000000000123, 13)
        await _wait_message(delivery.reader, -1000000000123, 14)
        async with asyncio.timeout(3):
            while _pending(delivery.path) is not None:
                await asyncio.sleep(0)
        assert _safe(delivery.path).channels[123] == 22
        assert delivery.reader.execute(
            "SELECT message_id,text FROM messages WHERE dialog_id=? AND message_id IN (13,14) ORDER BY message_id",
            (-1000000000123,),
        ).fetchall() == [(13, "fixture"), (14, "fixture")]
        assert len(sink.frames) == 1
    finally:
        await _stop(loop)


async def test_late_dm_defers_unclaimed_channel_then_commits_and_catches_up(delivery: _Delivery) -> None:
    gate, sender, connection, sink = delivery.wire
    gate._admission_scheduler._next_write_at = time.monotonic() + 20
    loop = asyncio.create_task(gate._update_loop())
    preparing: asyncio.Task[None] | None = None
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        preparing = asyncio.create_task(sender._prepare_state(state))
        async with asyncio.timeout(3):
            while not any(gate._admission_scheduler.queue_depths().values()):
                await asyncio.sleep(0)
        gate._updates_queue.put_nowait(_update(delivery.now, 11, 11))
        with pytest.raises(TelegramRpcAdmissionDeferred):
            await asyncio.wait_for(preparing, 3)
        await _wait_message(delivery.reader, 55, 11)
        assert not sink.frames and 123 in delivery.box.getting_diff_for
        assert gate._ensure_safety_ledger().status()["unresolved_request_count"] == 0
        assert not any(gate._admission_scheduler.queue_depths().values())
        gate._admission_scheduler._next_write_at = 0
        gate._admission_scheduler.notify_eligibility_changed()
        resumed = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        await sender._prepare_state(resumed)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        resumed.future.set_result(types.updates.ChannelDifferenceEmpty(pts=20, final=True))
        async with asyncio.timeout(3):
            while 123 in delivery.box.getting_diff_for or _pending(delivery.path) is not None:
                await asyncio.sleep(0)
        assert len(sink.frames) == 1 and _safe(delivery.path).pts == 11
    finally:
        if preparing is not None and not preparing.done():
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)
        await _stop(loop)


async def test_continuous_input_allows_claim_and_then_next_persisted_message(delivery: _Delivery) -> None:
    gate, sender, connection, sink = delivery.wire
    gate._updates_queue.put_nowait(_update(delivery.now, 11, 11))
    loop = asyncio.create_task(gate._update_loop())
    try:
        await _wait_message(delivery.reader, 55, 11)
        state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        gate._updates_queue.put_nowait(_update(delivery.now, 12, 12))
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        assert len(sink.frames) == 1
        assert cast(UpdateIntakeQueue, gate._updates_queue).real_count == 1
        state.future.set_result(types.updates.ChannelDifferenceEmpty(pts=20, final=True))
        await _wait_message(delivery.reader, 55, 12)
        async with asyncio.timeout(3):
            while _pending(delivery.path) is not None:
                await asyncio.sleep(0)
        assert _safe(delivery.path).pts == 12
    finally:
        await _stop(loop)


async def test_native_fences_advance_before_delivery_admission_wait(
    delivery: _Delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.contact_notes.intake import is_current, track_contact_note
    from mcp_telegram.folders.intake import marker

    gate = delivery.wire[0]
    repository = gate._delivery_repository
    assert repository is not None
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block_admission(*_args: object) -> object:
        entered.set()
        await release.wait()
        raise RuntimeError("test admission stopped")

    monkeypatch.setattr(repository, "admit", block_admission)
    before = marker()
    with track_contact_note(55) as note_marker:
        task = asyncio.create_task(
            gate._preprocess_updates([types.UpdateDialogFilters(), types.UpdateUser(55)], [], [])
        )
        await entered.wait()
        assert marker() > before
        assert not is_current(55, note_marker)
        release.set()
        with pytest.raises(RuntimeError, match="test admission stopped"):
            await task
