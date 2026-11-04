"""Queue fairness uses the vendor loop and the guarded physical sender."""

import asyncio
import time
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon import types
from telethon._updates import GapError, MessageBox, PrematureEndReason, SessionState
from telethon._updates.messagebox import ENTRY_ACCOUNT, ENTRY_SECRET, PossibleGap, State

from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred
from mcp_telegram.telegram_update_admission import GateMessageBox, UpdateIntakeQueue
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.mark.parametrize("still_lost", [False, True])
def test_local_access_loss_marker_rechecks_before_channel_retirement(wire: _WireHarness, still_lost: bool) -> None:
    gate = wire[0]
    box, _ = setup_gap(wire)
    box.try_set_channel_state(123, 20)
    box.try_begin_get_diff(123, "access test")
    request = types.InputChannel(123, 456)
    from telethon.tl.functions.updates import GetChannelDifferenceRequest

    difference = GetChannelDifferenceRequest(request, types.ChannelMessagesFilterEmpty(), 20, 10)
    box.retire_access_lost(difference, lambda: still_lost)
    box.end_channel_difference(difference, PrematureEndReason.TEMPORARY_SERVER_ISSUES, gate._mb_entity_cache)
    assert (123 in box.map) is not still_lost
    assert (123 in box.getting_diff_for) is not still_lost


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


@pytest.mark.parametrize("entry", [ENTRY_ACCOUNT, ENTRY_SECRET])
@pytest.mark.parametrize("gap_due", [False, True])
async def test_due_account_deadline_preempts_busy_channels(wire: _WireHarness, entry: object, gap_due: bool) -> None:
    gate = wire[0]
    box = cast(GateMessageBox, gate._message_box)
    now = asyncio.get_running_loop().time()
    box.load(SessionState(42, 2, False, 10, 3, int(datetime.now(UTC).timestamp()), 1, None), [])
    box.map[ENTRY_ACCOUNT].deadline = now + 100
    box.map[ENTRY_SECRET].deadline = now + 100
    if gap_due:
        box.possible_gaps[entry] = PossibleGap(now, [])
    else:
        box.map[entry].deadline = now
    for channel_id in range(100, 220):
        box.map[channel_id] = State(20, now + 100)
        box.try_begin_get_diff(channel_id, "busy channel")
    assert box.get_difference() is not None
    assert entry in box.getting_diff_for and entry not in box.possible_gaps
    assert len(box.getting_diff_for) == 121


async def test_uninitialized_account_never_selects_secret_difference(wire: _WireHarness) -> None:
    box = cast(GateMessageBox, wire[0]._message_box)
    box.map.clear()
    box.map[ENTRY_SECRET] = State(1, 0)
    box.getting_diff_for.add(ENTRY_SECRET)
    assert box.get_difference() is None


@pytest.mark.parametrize("final", [False, True])
async def test_account_deadline_matures_during_channel_responses(wire: _WireHarness, final: bool) -> None:
    from telethon.tl.functions.updates import GetChannelDifferenceRequest

    gate = wire[0]
    box = cast(GateMessageBox, gate._message_box)
    now = asyncio.get_running_loop().time()
    box.load(SessionState(42, 2, False, 10, 0, int(datetime.now(UTC).timestamp()), 1, None), [])
    box.map[ENTRY_ACCOUNT].deadline = now + 100
    for channel_id in range(100, 220):
        box.map[channel_id] = State(20, now + 100)
        box.try_begin_get_diff(channel_id, "busy channel")
    request = GetChannelDifferenceRequest(types.InputChannel(100, 200), types.ChannelMessagesFilterEmpty(), 20, 10)
    for _ in range(3):
        assert box.get_difference() is None
        response = types.updates.ChannelDifference(
            pts=20, new_messages=[], other_updates=[], chats=[], users=[], final=final
        )
        box.apply_channel_difference(request, response, gate._mb_entity_cache)
        if final:
            box.try_begin_get_diff(100, "next busy channel")
    box.map[ENTRY_ACCOUNT].deadline = asyncio.get_running_loop().time()
    assert box.get_difference() is not None
    assert ENTRY_ACCOUNT in box.getting_diff_for and len(box.getting_diff_for) == 121


async def test_due_deadline_waits_for_queued_input_and_honors_floor(wire: _WireHarness) -> None:
    gate = wire[0]
    box, now = setup_gap(wire)
    box.getting_diff_for.discard(ENTRY_ACCOUNT)
    box.map[ENTRY_ACCOUNT].deadline = 0
    queue = cast(UpdateIntakeQueue, gate._updates_queue)
    queue.put_nowait(envelope(now))
    assert box.get_difference() is None and ENTRY_ACCOUNT not in box.getting_diff_for
    queue.get_nowait()
    box._eligibility = lambda _method: TelegramRpcAdmissionDeferred(60, eligible_at=time.monotonic() + 60)
    assert box.get_difference() is None and ENTRY_ACCOUNT in box.getting_diff_for


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
