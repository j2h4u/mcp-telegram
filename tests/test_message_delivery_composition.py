"""Real vendor intake reaches durable SQLite with bounded delivery evidence."""

import asyncio
import sqlite3
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest
from telethon import types
from telethon.tl.core.tlmessage import TLMessage

from mcp_telegram.message_delivery_observations import DeliveryTrace, MessageDeliveryObservations
from tests.test_daemon_api import _close_tracked_sqlite_connections as _close_tracked_sqlite_connections
from tests.test_message_delivery_observations import Sink
from tests.test_telegram_rpc_wire_integration import wire as wire
from tests.test_telegram_update_fairness_delivery import _Delivery, _stop, _update, _wait_message
from tests.test_telegram_update_fairness_delivery import delivery as delivery


@pytest.mark.parametrize("outgoing", [False, True])
async def test_decoded_other_client_update_reaches_canonical_commit(delivery: _Delivery, outgoing: bool) -> None:
    gate, sender, _, _ = delivery.wire
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    gate.set_message_delivery_observer(observer)
    envelope = _update(delivery.now, 11, 11)
    assert isinstance(envelope.update, types.UpdateNewMessage)
    assert isinstance(envelope.update.message, types.Message)
    envelope.update.message.out = outgoing
    await sender._handle_update(cast(TLMessage, cast(object, SimpleNamespace(obj=envelope))))
    loop = asyncio.create_task(gate._update_loop())
    try:
        await _wait_message(delivery.reader, 55, 11)
        async with asyncio.timeout(3):
            while gate._active_delivery is not None:
                await asyncio.sleep(0)
    finally:
        await _stop(loop)
    observer.flush()
    samples = [cast(dict[str, object], row["payload"]) for row in sink.rows if row["outcome"] == "sample"]
    assert len(samples) == 1
    assert samples[0]["source"] == "push"
    assert any(
        (dialog, message) == (55, 11)
        for dialog, message, _ in cast(list[tuple[int, int, int | None]], samples[0]["identities"])
    )
    stages = [stage for stage, _ in cast(list[tuple[str, int]], samples[0]["stages"])]
    assert stages[0] == "decoded_received"
    assert "admitted" in stages
    assert "committed" in stages


async def test_self_outgoing_response_is_not_published_as_push(delivery: _Delivery) -> None:
    gate, sender, _, _ = delivery.wire
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    gate.set_message_delivery_observer(observer)
    envelope = _update(delivery.now, 12, 11)
    sender._store_own_updates(envelope)
    loop = asyncio.create_task(gate._update_loop())
    try:
        async with asyncio.timeout(3):
            while not sink.rows and gate._updates_queue.real_count:
                await asyncio.sleep(0)
            while gate._updates_queue.real_count:
                await asyncio.sleep(0)
    finally:
        await _stop(loop)
    observer.flush()
    samples = [cast(dict[str, object], row["payload"]) for row in sink.rows if row["outcome"] == "sample"]
    assert len(samples) == 1
    assert samples[0]["source"] == "rpc_response"
    stages = [stage for stage, _ in cast(list[tuple[str, int]], samples[0]["stages"])]
    assert "decoded_received" not in stages
    assert "self_outgoing" in stages
    assert (
        delivery.reader.execute("select 1 from messages where dialog_id=? and message_id=?", (55, 12)).fetchone()
        is None
    )


async def test_two_message_envelope_keeps_commit_stage(delivery: _Delivery) -> None:
    gate, sender, _, _ = delivery.wire
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    gate.set_message_delivery_observer(observer)
    first = _update(delivery.now, 21, 11).update
    second = _update(delivery.now, 22, 12).update
    envelope = types.Updates(updates=[first, second], users=[], chats=[], date=delivery.now, seq=0)
    await sender._handle_update(cast(TLMessage, cast(object, SimpleNamespace(obj=envelope))))
    loop = asyncio.create_task(gate._update_loop())
    try:
        await _wait_message(delivery.reader, 55, 21)
        await _wait_message(delivery.reader, 55, 22)
        async with asyncio.timeout(3):
            while gate._active_delivery is not None:
                await asyncio.sleep(0)
    finally:
        await _stop(loop)
    observer.flush()
    sample = cast(dict[str, object], next(row["payload"] for row in sink.rows if row["outcome"] == "sample"))
    stages = [stage for stage, _ in cast(list[tuple[str, int]], sample["stages"])]
    assert "committed" in stages
    assert cast(dict[str, int], sample["stage_counts"])["committed"] == 2


async def test_short_dm_records_identity_and_commit(delivery: _Delivery) -> None:
    gate, sender, _, _ = delivery.wire
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    gate.set_message_delivery_observer(observer)
    envelope = types.UpdateShortMessage(
        id=31, user_id=55, message="fixture", pts=11, pts_count=1, date=delivery.now, out=True
    )
    await sender._handle_update(cast(TLMessage, cast(object, SimpleNamespace(obj=envelope))))
    loop = asyncio.create_task(gate._update_loop())
    try:
        await _wait_message(delivery.reader, 55, 31)
        async with asyncio.timeout(3):
            while gate._active_delivery is not None:
                await asyncio.sleep(0)
    finally:
        await _stop(loop)
    observer.flush()
    sample = cast(dict[str, object], next(row["payload"] for row in sink.rows if row["outcome"] == "sample"))
    assert any(
        (dialog, message) == (55, 31)
        for dialog, message, _ in cast(list[tuple[int, int, int | None]], sample["identities"])
    )
    assert "committed" in [name for name, _ in cast(list[tuple[str, int]], sample["stages"])]


class ThrowingObserver(MessageDeliveryObservations):
    def intake(self, *, source: str, scope: str, observed_at_ms: int) -> DeliveryTrace | None:
        raise RuntimeError("diagnostic unavailable")


async def test_throwing_intake_observer_does_not_block_message(delivery: _Delivery) -> None:
    gate, sender, _, _ = delivery.wire
    gate.set_message_delivery_observer(ThrowingObserver(Sink()))
    envelope = _update(delivery.now, 51, 11)
    await sender._handle_update(cast(TLMessage, cast(object, SimpleNamespace(obj=envelope))))
    loop = asyncio.create_task(gate._update_loop())
    try:
        await _wait_message(delivery.reader, 55, 51)
    finally:
        await _stop(loop)
    assert (
        delivery.reader.execute("select 1 from messages where dialog_id=? and message_id=?", (55, 51)).fetchone()
        is not None
    )


async def test_retained_replay_has_no_fabricated_push_receipt(delivery: _Delivery) -> None:
    gate, sender, _, _ = delivery.wire
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    gate.set_message_delivery_observer(observer)
    envelope = _update(delivery.now, 61, 11)
    await sender._handle_update(cast(TLMessage, cast(object, SimpleNamespace(obj=envelope))))
    item = gate._updates_queue.get_nowait()
    updates: list[object] = []
    delivery.box.process_updates(item, gate._mb_entity_cache, updates)
    await gate._preprocess_updates(updates, [], [])
    repository = gate._delivery_repository
    safe = gate._delivery_safe
    assert repository is not None and safe is not None
    state = await repository.bind(42, safe.model_dump_json())
    assert state.pending is not None
    gate._active_delivery = None
    observer.flush()
    await gate._replay_pending_delivery(42, state)
    observer.flush()
    samples = [cast(dict[str, object], row["payload"]) for row in sink.rows if row["outcome"] == "sample"]
    replay = next(sample for sample in samples if sample["source"] == "replay")
    assert all(stage != "decoded_received" for stage, _ in cast(list[tuple[str, int]], replay["stages"]))
    assert (
        delivery.reader.execute("select 1 from messages where dialog_id=? and message_id=?", (55, 61)).fetchone()
        is not None
    )


async def test_delayed_writer_does_not_report_commit_early(
    delivery: _Delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, _, _ = delivery.wire
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    gate.set_message_delivery_observer(observer)
    original = delivery.writer.run
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(fn: Callable[[sqlite3.Connection], object]) -> object:
        if getattr(fn, "__name__", "") == "_persist_event_0":
            entered.set()
            await release.wait()
        return await original(fn)

    monkeypatch.setattr(delivery.writer, "run", delayed)
    envelope = _update(delivery.now, 71, 11)
    await sender._handle_update(cast(TLMessage, cast(object, SimpleNamespace(obj=envelope))))
    loop = asyncio.create_task(gate._update_loop())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        observer.flush()
        first = cast(dict[str, object], next(row["payload"] for row in sink.rows if row["outcome"] == "sample"))
        assert "committed" not in [stage for stage, _ in cast(list[tuple[str, int]], first["stages"])]
        release.set()
        await _wait_message(delivery.reader, 55, 71)
        async with asyncio.timeout(3):
            while gate._active_delivery is not None:
                await asyncio.sleep(0)
    finally:
        release.set()
        await _stop(loop)
    observer.flush()
    samples = [cast(dict[str, object], row["payload"]) for row in sink.rows if row["outcome"] == "sample"]
    assert samples[-1]["trace_id"] == first["trace_id"]
    assert "committed" in [stage for stage, _ in cast(list[tuple[str, int]], samples[-1]["stages"])]


async def test_physical_channel_claim_changes_live_snapshot_until_result(delivery: _Delivery) -> None:
    from mcp_telegram.state import telegram_session_lease

    gate, sender, connection, _ = delivery.wire
    with telegram_session_lease(delivery.path.parent) as lease:
        gate.bind_session_lease(lease)
        loop = asyncio.create_task(gate._update_loop())
        try:
            state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
            await sender._prepare_state(state)
            before = cast(dict[str, object], gate.message_delivery_snapshot(123)["channel"])
            assert before["catch_up_state"] != "in_flight"
            assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
            during = cast(dict[str, object], gate.message_delivery_snapshot(123)["channel"])
            assert during["catch_up_state"] == "in_flight"
            state.future.set_result(types.updates.ChannelDifferenceEmpty(pts=20, final=True))
            async with asyncio.timeout(3):
                while 123 in delivery.box.getting_diff_for:
                    await asyncio.sleep(0)
            after = cast(dict[str, object], gate.message_delivery_snapshot(123)["channel"])
            assert after["catch_up_state"] != "in_flight"
        finally:
            await _stop(loop)
