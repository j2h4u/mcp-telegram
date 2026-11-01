"""Queue fairness uses the vendor loop and the guarded physical sender."""

import asyncio
import time
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon import types
from telethon._updates import GapError, MessageBox, SessionState
from telethon._updates.messagebox import ENTRY_ACCOUNT

from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred
from mcp_telegram.telegram_update_admission import GateMessageBox, UpdateIntakeQueue
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


def setup_gap(wire: _WireHarness) -> tuple[GateMessageBox, datetime]:
    gate = wire[0]
    box = cast(GateMessageBox, gate._message_box)
    now = datetime.now(UTC)
    box.load(SessionState(42, 2, False, 10, 0, int(now.timestamp()), 1, None), [])
    box.try_begin_get_diff(ENTRY_ACCOUNT, "test fairness")
    gate._authorized = True
    gate._catch_up = False
    return box, now


def envelope(now: datetime) -> object:
    return types.UpdateShort(types.UpdateUserStatus(7, types.UserStatusOffline(now)), now)


def test_only_real_gap_envelope_grants_credit(wire: _WireHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = wire[0]
    box, now = setup_gap(wire)
    box.wakeup_queued = True
    with pytest.raises(GapError):
        box.process_updates(box.wakeup, gate._mb_entity_cache, [])
    assert not box.wakeup_queued and not box.difference_credit

    def gap(*_args: object) -> tuple[list[object], list[object]]:
        raise GapError

    monkeypatch.setattr(MessageBox, "process_updates", gap)
    with pytest.raises(GapError):
        box.process_updates(envelope(now), gate._mb_entity_cache, [])
    assert box.difference_credit


async def test_real_input_precedes_ready_difference_and_sentinel_grants_no_credit(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, _, _ = wire
    box, now = setup_gap(wire)
    received = asyncio.Event()

    async def dispatch(_update: object) -> None:
        received.set()

    monkeypatch.setattr(gate, "_dispatch_update", dispatch)
    queue = cast(UpdateIntakeQueue, gate._updates_queue)
    assert queue is box.intake_queue and queue is sender._updates_queue
    queue.put_nowait(box.wakeup)
    box.wakeup_queued = True
    queue.put_nowait(envelope(now))
    assert queue.real_count == 1 and box.intake_due()
    loop = asyncio.create_task(gate._update_loop())
    try:
        await asyncio.wait_for(received.wait(), 2)
        assert box.difference_credit and queue.real_count == 0
        await asyncio.wait_for(sender._send_queue.next_state(), 2)
    finally:
        loop.cancel()
        await loop


async def test_late_input_yields_unsent_difference_and_preserves_gap(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    box, now = setup_gap(wire)
    received = asyncio.Event()

    async def dispatch(_update: object) -> None:
        received.set()

    monkeypatch.setattr(gate, "_dispatch_update", dispatch)
    gate._admission_scheduler._next_write_at = time.monotonic() + 20
    loop = asyncio.create_task(gate._update_loop())
    preparing = None
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        preparing = asyncio.create_task(sender._prepare_state(state))
        async with asyncio.timeout(2):
            while not any(gate._admission_scheduler.queue_depths().values()):
                await asyncio.sleep(0)
        gate._updates_queue.put_nowait(envelope(now))
        with pytest.raises(TelegramRpcAdmissionDeferred):
            await asyncio.wait_for(preparing, 2)
        await asyncio.wait_for(received.wait(), 2)
        assert sink.frames == [] and ENTRY_ACCOUNT in box.getting_diff_for
        assert gate._main_sender.physical_connected() and gate._updates_error is None
        gate._admission_scheduler._next_write_at = 0
        gate._admission_scheduler.notify_eligibility_changed()
        resumed = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        await sender._prepare_state(resumed)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        assert len(sink.frames) == 1 and not box.difference_credit
        resumed.future.set_result(types.updates.DifferenceEmpty(now, 2))
        async with asyncio.timeout(2):
            while ENTRY_ACCOUNT in box.getting_diff_for:
                await asyncio.sleep(0)
    finally:
        if preparing is not None and not preparing.done():
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)
        loop.cancel()
        await loop


async def test_input_after_permit_revokes_unsent_packet_without_transport_hold(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    box, now = setup_gap(wire)
    received = asyncio.Event()

    async def dispatch(_update: object) -> None:
        received.set()

    monkeypatch.setattr(gate, "_dispatch_update", dispatch)
    loop = asyncio.create_task(gate._update_loop())
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        await sender._prepare_state(state)
        packet = connection._send_queue.get_nowait()
        old_sequence = packet.permit.admission.sequence
        old_attempt = gate._require_wire_owner(packet.state.owner).attempt_id
        gate._updates_queue.put_nowait(envelope(now))
        assert not sender.write_packet(packet, connection._send)
        assert not sink.frames and not box.difference_credit
        deferred = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        with pytest.raises(TelegramRpcAdmissionDeferred):
            await sender._prepare_state(deferred)
        await asyncio.wait_for(received.wait(), 2)
        assert box.difference_credit and ENTRY_ACCOUNT in box.getting_diff_for
        assert gate._main_sender.physical_connected() and gate._updates_error is None
        assert gate._wire_fault is None and gate.account_safety_status() is None
        assert old_sequence not in gate._admission_scheduler._active
        ledger = gate._ensure_safety_ledger()
        assert old_attempt not in cast(dict[str, object], ledger._record["intents"])
    finally:
        loop.cancel()
        await loop


async def test_claimed_difference_keeps_input_queued_until_response_applied(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    box, now = setup_gap(wire)
    received = asyncio.Event()

    async def dispatch(_update: object) -> None:
        received.set()

    monkeypatch.setattr(gate, "_dispatch_update", dispatch)
    loop = asyncio.create_task(gate._update_loop())
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        gate._updates_queue.put_nowait(envelope(now))
        owner = gate._require_wire_owner(sender.require_owned(state).owner)
        assert owner.claimed == 1 and gate._difference_admission_deferral(owner.scope) is None
        await asyncio.sleep(0)
        assert not received.is_set() and cast(UpdateIntakeQueue, gate._updates_queue).real_count == 1
        gate._admission_scheduler._next_write_at = 0
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        assert owner.claimed == 2 and not received.is_set()
        state.future.set_result(types.updates.DifferenceEmpty(now, 2))
        await asyncio.wait_for(received.wait(), 2)
        assert len(sink.frames) == 2 and box.seq == 2 and box.difference_credit
    finally:
        loop.cancel()
        await loop


async def test_input_credit_survives_failed_dispatch_and_spent_at_first_claim(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    box, now = setup_gap(wire)
    received = asyncio.Event()

    async def dispatch(_update: object) -> None:
        received.set()

    monkeypatch.setattr(gate, "_dispatch_update", dispatch)
    gate._updates_queue.put_nowait(envelope(now))
    loop = asyncio.create_task(gate._update_loop())
    try:
        await asyncio.wait_for(received.wait(), 2)
        state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        await sender._prepare_state(state)
        packet = connection._send_queue.get_nowait()
        gate._updates_queue.put_nowait(envelope(now))
        original = gate._admission_scheduler.record_dispatch
        monkeypatch.setattr(gate._admission_scheduler, "record_dispatch", lambda _admission: False)
        assert not sender.write_packet(packet, connection._send)
        assert box.difference_credit and not sink.frames
        monkeypatch.setattr(gate._admission_scheduler, "record_dispatch", original)
        retry = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        await sender._prepare_state(retry)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        assert not box.difference_credit and len(sink.frames) == 1
        retry.future.set_result(types.updates.DifferenceEmpty(now, 2))
    finally:
        loop.cancel()
        await loop
