"""Automatic recovery uses durable known outcomes and the existing physical gate."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import cast

import pytest
from telethon.network.connection.tcpfull import FullPacketCodec
from telethon.tl import functions, types
from telethon.tl.functions.updates import GetStateRequest

from mcp_telegram import telegram_rpc
from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptEvidence
from mcp_telegram.telegram_rpc import TelegramRpcBudget, TelegramRpcGate, TelegramRpcSafetyLedger, TelegramRpcThrottled
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import RpcServiceClass, TelegramRpcScope, TelegramRpcSource
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender, _OwnedPacket
from tests.test_telegram_rpc_safety import Clock, Store, generation, intents, record, recovery
from tests.test_telegram_rpc_safety import clock as clock
from tests.test_telegram_rpc_wire_integration import _Circuit, _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire
from tests.test_telegram_transport import _Writer


def _automatic(store: Store) -> dict[str, object]:
    return cast(dict[str, object], recovery(store)["automatic"])


async def _waiting(store: Store, clock: Clock) -> TelegramRpcSafetyLedger:
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    ledger.configure_auto_resume(enabled=True)
    await ledger.record_floor(clock.time() + 10, circuit_open=True)
    assert not await ledger.maintain_auto_resume()
    assert recovery(store)["state"] == "waiting"
    return ledger


async def test_automatic_wait_honors_later_server_floor_and_does_not_replenish(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    await ledger.record_floor(clock.time() + 100_000)
    clock.advance(99_999)
    assert not await ledger.consume_auto_resume()
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    assert not await restarted.maintain_auto_resume()
    clock.advance(2)
    assert await restarted.maintain_auto_resume()
    assert await restarted.consume_auto_resume()
    assert cast(dict[str, int], _automatic(store)["attempts_by_policy"])["flood_wait"] == 1
    assert not await restarted.consume_auto_resume()


async def test_automatic_attempts_back_off_and_exhaust_after_six(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    for attempt, delay in enumerate(telegram_rpc._AUTO_RESUME_DELAYS, start=1):
        clock.advance(delay - 1)
        assert not await ledger.consume_auto_resume()
        clock.advance(1)
        assert await ledger.consume_auto_resume()
        assert cast(dict[str, int], _automatic(store)["attempts_by_policy"])["flood_wait"] == attempt
        await ledger.record_floor(clock.time() + 1, circuit_open=True)
        await ledger.maintain_auto_resume()
    assert ledger.protection_reason == "automatic_resume_exhausted"
    clock.advance(10_000_000)
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    assert not await restarted.maintain_auto_resume()
    assert not await restarted.consume_auto_resume()
    assert cast(dict[str, int], _automatic(store)["attempts_by_policy"])["flood_wait"] == 6


async def test_automatic_wait_rebase_is_persisted_once_per_boot(clock: Clock) -> None:
    store = Store()
    await _waiting(store, clock)
    clock.advance(1000)
    clock.monotonic_seconds = 20
    rebooted = store.ledger(boot_id="boot-b")
    rebooted.configure_auto_resume(enabled=True)
    assert not await rebooted.maintain_auto_resume()
    due = cast(dict[str, object], _automatic(store)["scheduled_due"])
    assert due["boot_id"] == "boot-b"
    deadline = due["until_monotonic"]
    clock.advance(1800)
    restarted = store.ledger(boot_id="boot-b")
    restarted.configure_auto_resume(enabled=True)
    assert not await restarted.maintain_auto_resume()
    assert cast(dict[str, object], _automatic(store)["scheduled_due"])["until_monotonic"] == deadline
    clock.advance(1800)
    assert await restarted.maintain_auto_resume()


async def test_wall_clock_jump_cannot_shorten_automatic_wait(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    clock.utc += 1_000_000
    assert not await ledger.consume_auto_resume()
    clock.monotonic_seconds += 3600
    assert await ledger.consume_auto_resume()


async def test_disabled_automatic_mode_does_not_arm_or_consume(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    await ledger.record_floor(clock.time(), circuit_open=True)
    before = store.raw
    assert not await ledger.maintain_auto_resume()
    assert store.raw == before
    ledger.configure_auto_resume(enabled=True)
    await ledger.maintain_auto_resume()
    clock.advance(3600)
    ledger.configure_auto_resume(enabled=False)
    before = store.raw
    assert not await ledger.maintain_auto_resume()
    assert not await ledger.consume_auto_resume()
    assert store.raw == before


async def test_consumed_trial_crash_waits_for_a_new_trial(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    clock.advance(3600)
    assert await ledger.consume_auto_resume()
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    assert restarted.protection_reason == "recovery_trial_incomplete"
    assert not await restarted.maintain_auto_resume()
    assert not await restarted.consume_auto_resume()
    clock.advance(7200)
    assert await restarted.consume_auto_resume()


async def test_unknown_intent_is_never_acknowledged_by_automatic_recovery(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    clock.advance(3600)
    assert await ledger.consume_auto_resume()
    await ledger.prepare("uncertain", "test.request")
    ledger.mark_claimed("uncertain")
    await ledger.complete("uncertain", known_outcome=False)
    before = store.raw
    clock.advance(1_000_000)
    assert not await ledger.maintain_auto_resume()
    assert not await ledger.consume_auto_resume()
    assert store.raw == before
    assert intents(store)["uncertain"]["phase"] == "unknown"


async def test_historical_acknowledgment_is_retained_without_rewriting(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    await ledger.prepare("historical", "test.request")
    ledger.mark_claimed("historical")
    await ledger.complete("historical", known_outcome=False)
    await ledger.authorize_recovery(generation(ledger), acknowledged=True)
    assert await ledger.consume_recovery()
    await ledger.finish_recovery(42)
    before = dict(intents(store)["historical"])
    ledger.configure_auto_resume(enabled=True)
    await ledger.record_floor(clock.time() + 1, circuit_open=True)
    await ledger.maintain_auto_resume()
    clock.advance(3600)
    assert await ledger.consume_auto_resume()
    assert intents(store)["historical"] == before


@pytest.mark.parametrize("fault", ["scope", "owner", "corrupt", "storage"])
async def test_invalid_or_unwritable_safety_state_never_consumes(clock: Clock, fault: str) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    clock.advance(3600)
    if fault == "scope":
        ledger = store.ledger(key_id=100)
    elif fault == "owner":
        store.owners = (43, 43)
    elif fault == "corrupt":
        store.raw = "{}"
        ledger = store.ledger()
    else:
        store.fail_next = True
    ledger.configure_auto_resume(enabled=True)
    if fault == "storage":
        with pytest.raises(Exception, match="synthetic storage failure"):
            await ledger.consume_auto_resume()
    else:
        assert not await ledger.consume_auto_resume()
    assert ledger.protection_reason is not None


async def test_competing_consumers_get_one_durable_trial(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    clock.advance(3600)
    assert sorted(await asyncio.gather(ledger.consume_auto_resume(), ledger.consume_auto_resume())) == [False, True]
    assert cast(dict[str, int], _automatic(store)["attempts_by_policy"])["flood_wait"] == 1


async def test_cancelled_slow_consume_stays_owned_and_crash_cannot_retry(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    clock.advance(3600)
    store.block()
    consuming = asyncio.create_task(ledger.consume_auto_resume())
    await store.entered.wait()
    clock.advance(1000)
    consuming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consuming
    assert recovery(store)["state"] == "waiting"
    store.released.set()
    await ledger.drain()
    assert recovery(store)["state"] == "consumed"
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    assert not await restarted.consume_auto_resume()


def _wire_clock(wire: _WireHarness, monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock(time.time(), time.monotonic())
    monkeypatch.setattr(telegram_rpc, "time", value)
    monkeypatch.setattr(wire[0]._admission_scheduler, "_clock", value.monotonic)
    return value


@pytest.fixture
async def trial_wire(wire: _WireHarness, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[_WireHarness, Clock]]:
    gate = wire[0]
    clock = _wire_clock(wire, monkeypatch)
    gate.configure_auto_resume(enabled=True)
    await gate.prepare_startup_safety()
    await gate._ensure_safety_ledger().record_floor(clock.time() + 1, circuit_open=True)
    assert not await gate.maintain_auto_resume()
    clock.advance(3600)
    assert await gate.maintain_auto_resume()
    assert not wire[1].physical_connected()
    fresh = TelegramRpcGate(
        gate.session,
        1,
        "test",
        rpc_budget=TelegramRpcBudget(30, 60),
        circuit_status=_Circuit,
        fallback_wait_seconds=1,
        cooldown_buffer_seconds=0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=gate._safety_store,
    )
    monkeypatch.setattr(fresh._admission_scheduler, "_clock", clock.monotonic)
    sender = cast(GuardedMTProtoSender, fresh._main_sender)
    connection = GuardedConnectionTcpFull("127.0.0.1", 443, 2, loggers=fresh._log)
    connection.bind_owner(sender)
    connection._connected = True
    sink = _Writer()
    connection._writer = cast(asyncio.StreamWriter, sink)
    connection._codec = FullPacketCodec(connection)
    sender._connection = connection
    sender._user_connected = True
    fresh._transport_state = telegram_rpc._TransportBoundaryState.READY
    fresh.configure_auto_resume(enabled=True)
    await fresh.prepare_startup_safety()
    await fresh.verify_startup_account(42)
    recovery = fresh._ensure_safety_ledger()._automatic_recovery()
    assert recovery is not None and recovery["state"] == "consumed"
    try:
        yield (fresh, sender, connection, sink), clock
    finally:
        for state in tuple(sender._owned_states):
            if not state.future.done():
                state.future.cancel()
        await asyncio.sleep(0)
        await asyncio.gather(*sender._observing, return_exceptions=True)
        await fresh._admission_scheduler.close()
        await connection.disconnect()


async def _prepared_packet(wire: _WireHarness, clock: Clock) -> tuple[asyncio.Future[object], _OwnedPacket]:
    gate, sender, connection, _ = wire
    trial = gate.automatic_trial_active()
    if trial:
        gate.bind_auto_resume_identity()
    scope = TelegramRpcScope(
        TelegramRpcSource.MAINTENANCE if trial else TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.BACKGROUND if trial else RpcServiceClass.INTERACTIVE,
        clock.monotonic() + 10,
        asyncio.current_task(),
        demand_kind=DemandKind.SELF_PROFILE_MAINTENANCE if trial else None,
        attempt_budget=RpcAttemptBudget(1 if trial else 2),
        attempt_evidence=RpcAttemptEvidence(),
    )
    request = functions.users.GetUsersRequest([types.InputUserSelf()]) if trial else GetStateRequest()
    response = sender.send_owned(request, scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    return response, connection._send_queue.get_nowait()


async def test_real_wire_success_and_canonical_progress_are_both_required_for_reset(
    trial_wire: tuple[_WireHarness, Clock],
) -> None:
    wire, clock = trial_wire
    gate, sender, connection, sink = wire
    await gate.finish_startup_auto_resume()
    gate.note_canonical_progress()
    clock.advance(3600)
    assert not await gate.maintain_auto_resume()
    assert gate._ensure_safety_ledger()._automatic_recovery() is not None
    response, packet = await _prepared_packet(wire, clock)
    assert sender.write_packet(packet, connection._send)
    packet.state.state.future.set_result("complete")
    assert await response == "complete"
    assert sink.frames
    assert not await gate.maintain_auto_resume()
    assert gate._ensure_safety_ledger()._automatic_recovery() is not None
    gate.note_canonical_progress()
    assert not await gate.maintain_auto_resume()
    assert gate._ensure_safety_ledger()._automatic_recovery() is None


async def test_disable_revokes_prepared_automatic_trial_packet(
    trial_wire: tuple[_WireHarness, Clock],
) -> None:
    wire, clock = trial_wire
    gate, sender, connection, sink = wire
    response, packet = await _prepared_packet(wire, clock)
    budget = cast(telegram_rpc._WireAttempt, sender.application_owner(response)).scope.attempt_budget
    assert budget is not None and budget.limit == 1
    gate.configure_auto_resume(enabled=False)
    assert not sender.write_packet(packet, connection._send)
    assert budget.attempts == 0
    assert sink.frames == []
    response.cancel()


async def test_recovery_identity_claim_spends_exactly_one_attempt(trial_wire: tuple[_WireHarness, Clock]) -> None:
    wire, clock = trial_wire
    _, sender, connection, sink = wire
    response, packet = await _prepared_packet(wire, clock)
    budget = cast(telegram_rpc._WireAttempt, sender.application_owner(response)).scope.attempt_budget
    assert budget is not None and budget.limit == 1 and budget.attempts == 0
    assert sender.write_packet(packet, connection._send)
    assert budget.attempts == 1 and sink.frames
    response.cancel()


async def test_stop_during_blocked_intent_commit_prevents_physical_write(
    trial_wire: tuple[_WireHarness, Clock],
) -> None:
    wire, clock = trial_wire
    gate, _, _, sink = wire
    ledger = gate._ensure_safety_ledger()
    entered, released, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    gate.set_auto_resume_stop(stopped.is_set)
    original = ledger._store

    async def blocked_save(expected: str | None, encoded: str) -> None:
        entered.set()
        await released.wait()
        await original.save_record(expected, encoded)

    ledger._store = replace(original, save_record=blocked_save)
    preparing = asyncio.create_task(_prepared_packet(wire, clock))
    await entered.wait()
    stopped.set()
    released.set()
    await asyncio.sleep(0)
    await ledger.drain()
    assert not await gate.maintain_auto_resume()
    assert sink.frames == []
    preparing.cancel()
    await asyncio.gather(preparing, return_exceptions=True)
    assert sink.frames == []


async def test_stop_revokes_prepared_trial_packet_without_stopping_controls(
    trial_wire: tuple[_WireHarness, Clock],
) -> None:
    wire, clock = trial_wire
    gate, sender, connection, sink = wire
    stopped = asyncio.Event()
    gate.set_auto_resume_stop(stopped.is_set)
    response, packet = await _prepared_packet(wire, clock)
    budget = cast(telegram_rpc._WireAttempt, sender.application_owner(response)).scope.attempt_budget
    assert budget is not None and budget.limit == 1
    stopped.set()
    assert not sender.write_packet(packet, connection._send)
    assert budget.attempts == 0
    assert gate._control_scope_valid()
    assert sink.frames == []
    response.cancel()


async def test_startup_monitor_cannot_restart_before_startup_preparation(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _wire_clock(wire, monkeypatch)
    gate = wire[0]
    gate.configure_auto_resume(enabled=True)
    ledger = gate._ensure_safety_ledger()
    await ledger.record_floor(clock.time() + 1, circuit_open=True)
    await ledger.maintain_auto_resume()
    clock.advance(3600)
    assert not await gate.maintain_auto_resume()
    await gate.prepare_startup_safety()
    assert not await gate.maintain_auto_resume()


async def test_manual_recovery_cannot_consume_automatic_payload(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    clock.advance(3600)
    before = store.raw
    assert not await ledger.consume_recovery()
    assert store.raw == before
    with pytest.raises(TelegramRpcThrottled):
        ledger.require_writable()


async def test_malformed_automatic_receipt_fails_closed(clock: Clock) -> None:
    store = Store()
    await _waiting(store, clock)
    malformed = record(store)
    cast(dict[str, object], malformed["recovery"])["automatic"] = {"attempts": True, "due": {}}
    store.raw = json.dumps(malformed)
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    assert restarted.protection_reason == "safety_storage_failure"
    assert not await restarted.maintain_auto_resume()
