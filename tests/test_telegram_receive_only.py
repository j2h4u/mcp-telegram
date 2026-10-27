"""The real vendor loop retains gaps while passive delivered updates continue."""

import asyncio
import time
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon import functions, types
from telethon._updates import SessionState
from telethon._updates.messagebox import ENTRY_ACCOUNT

from mcp_telegram.telegram_rpc import _canonical_wire_method
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred
from mcp_telegram.telegram_update_admission import GateMessageBox
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


async def test_vendor_difference_deferral_keeps_gap_and_passive_delivery_then_resumes(  # noqa: PLR0915 - one vendor lifecycle
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = wire[0]
    box = cast(GateMessageBox, gate._message_box)
    now = datetime.now(UTC)
    box.load(SessionState(42, 2, False, 10, 0, int(now.timestamp()), 1, None), [])
    box.try_begin_get_diff(ENTRY_ACCOUNT, "test existing gap")
    gate._authorized = True
    gate._catch_up = False
    hint: TelegramRpcAdmissionDeferred | None = None
    calls = 0
    dispatched: list[object] = []
    received = asyncio.Event()
    recovered = asyncio.Event()
    monkeypatch.setattr(box, "_eligibility", lambda _method: hint)

    async def difference(request: object, *, ordered: bool) -> object:
        nonlocal hint, calls
        del ordered
        assert isinstance(request, functions.updates.GetDifferenceRequest)
        calls += 1
        if calls == 1:
            hint = TelegramRpcAdmissionDeferred(
                100, method_key=_canonical_wire_method(request), eligible_at=time.monotonic() + 100, reason="server"
            )
            raise hint
        recovered.set()
        return types.updates.DifferenceEmpty(now, 2)

    async def dispatch(update: object) -> None:
        dispatched.append(update)
        received.set()

    monkeypatch.setattr(gate, "_call_registered", difference)
    monkeypatch.setattr(gate, "_dispatch_update", dispatch)
    loop = asyncio.create_task(gate._update_loop())
    try:
        while calls == 0:
            await asyncio.sleep(0)
        update = types.UpdateUserStatus(7, types.UserStatusOffline(now))
        gate._updates_queue.put_nowait(types.UpdateShort(update, now))
        await asyncio.wait_for(received.wait(), 2)
        assert calls == 1 and ENTRY_ACCOUNT in box.getting_diff_for
        assert box.map[ENTRY_ACCOUNT].pts == 10 and box.seq == 1
        assert gate._main_sender.physical_connected() and gate._updates_error is None
        hint = None
        gate._notify_rpc_eligibility_changed()
        await asyncio.wait_for(recovered.wait(), 2)
        for _ in range(5):
            await asyncio.sleep(0)
        assert ENTRY_ACCOUNT not in box.getting_diff_for
        assert box.seq == 2 and box.map[ENTRY_ACCOUNT].pts == 10
        assert dispatched == [update]
        assert gate._main_sender.physical_connected()
    finally:
        loop.cancel()
        await loop


async def test_ordinary_vendor_value_error_still_ends_its_gap(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = wire[0]
    box = cast(GateMessageBox, gate._message_box)
    now = datetime.now(UTC)
    box.load(SessionState(42, 2, False, 10, 0, int(now.timestamp()), 1, None), [])
    box.try_begin_get_diff(ENTRY_ACCOUNT, "test ordinary failure")
    gate._authorized = True
    gate._catch_up = False
    called = asyncio.Event()

    async def invalid_difference(request: object, *, ordered: bool) -> object:
        del request, ordered
        called.set()
        raise ValueError("ordinary vendor failure")

    monkeypatch.setattr(gate, "_call_registered", invalid_difference)
    loop = asyncio.create_task(gate._update_loop())
    try:
        await asyncio.wait_for(called.wait(), 2)
        for _ in range(5):
            await asyncio.sleep(0)
        assert ENTRY_ACCOUNT not in box.getting_diff_for
        assert gate._main_sender.physical_connected()
    finally:
        loop.cancel()
        await loop


def test_mixed_passive_envelope_preserves_account_gap_checkpoint(wire: _WireHarness) -> None:
    gate = wire[0]
    box = cast(GateMessageBox, gate._message_box)
    previous = datetime.fromtimestamp(100, UTC)
    current = datetime.fromtimestamp(200, UTC)
    box.load(SessionState(42, 2, False, 10, 0, 100, 1, None), [])
    box.try_begin_get_diff(ENTRY_ACCOUNT, "pending account gap")
    deletion = types.UpdateDeleteMessages([1], 11, 1)
    status = types.UpdateUserStatus(7, types.UserStatusOffline(current))
    result: list[object] = []
    box.process_updates(types.Updates([deletion, status], [], [], current, 2), gate._mb_entity_cache, result)
    assert result == [status]
    snapshot = gate._capture_delivery_snapshot([])
    assert (snapshot.pts, snapshot.seq, snapshot.date) == (10, 1, previous)
    assert ENTRY_ACCOUNT in box.getting_diff_for


@pytest.mark.parametrize("already_queued", [False, True])
async def test_real_unsent_difference_yields_to_passive_delivery_when_gate_becomes_held(  # noqa: PLR0915 - connected vendor lifecycle
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, already_queued: bool
) -> None:
    from mcp_telegram import telegram_rpc

    gate, sender, connection, sink = wire
    box = cast(GateMessageBox, gate._message_box)
    now = datetime.now(UTC)
    box.load(SessionState(42, 2, False, 10, 0, int(now.timestamp()), 1, None), [])
    box.try_begin_get_diff(ENTRY_ACCOUNT, "late gate hold")
    gate._authorized = True
    gate._catch_up = False
    received = asyncio.Event()

    async def dispatch(_update: object) -> None:
        received.set()

    monkeypatch.setattr(gate, "_dispatch_update", dispatch)
    if already_queued:
        gate._admission_scheduler._next_write_at = time.monotonic() + 20
    loop = asyncio.create_task(gate._update_loop())
    preparing: asyncio.Task[None] | None = None
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        if already_queued:
            preparing = asyncio.create_task(sender._prepare_state(state))
            while not any(gate._admission_scheduler.queue_depths().values()):
                await asyncio.sleep(0)
        monkeypatch.setattr(telegram_rpc, "_COOLDOWN_DEADLINE", time.monotonic() + 30)
        gate._notify_rpc_eligibility_changed()
        if preparing is None:
            preparing = asyncio.create_task(sender._prepare_state(state))
        with pytest.raises(TelegramRpcAdmissionDeferred):
            await asyncio.wait_for(preparing, 2)
        status = types.UpdateUserStatus(7, types.UserStatusOffline(now))
        gate._updates_queue.put_nowait(types.UpdateShort(status, now))
        await asyncio.wait_for(received.wait(), 2)
        assert ENTRY_ACCOUNT in box.getting_diff_for
        assert (box.map[ENTRY_ACCOUNT].pts, box.seq) == (10, 1)
        assert sink.frames == [] and gate._updates_error is None
        assert gate._main_sender.physical_connected()
        monkeypatch.setattr(telegram_rpc, "_COOLDOWN_DEADLINE", 0.0)
        gate._admission_scheduler._next_write_at = 0.0
        gate._notify_rpc_eligibility_changed()
        resumed = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        await sender._prepare_state(resumed)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        resumed.future.set_result(types.updates.DifferenceEmpty(now, 2))
        async with asyncio.timeout(2):
            while ENTRY_ACCOUNT in box.getting_diff_for:
                await asyncio.sleep(0.001)
        assert len(sink.frames) == 1
    finally:
        if preparing is not None and not preparing.done():
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)
        loop.cancel()
        await loop
