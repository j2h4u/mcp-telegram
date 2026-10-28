"""Automatic recovery uses durable known outcomes and the existing physical gate."""

import asyncio
import json
import time
from dataclasses import replace
from typing import cast

import pytest
from telethon.tl.functions.updates import GetStateRequest

from mcp_telegram import telegram_rpc
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptEvidence
from mcp_telegram.telegram_rpc import TelegramRpcSafetyLedger, TelegramRpcThrottled
from mcp_telegram.telegram_rpc_scheduler import RpcServiceClass, TelegramRpcScope, TelegramRpcSource
from mcp_telegram.telegram_transport import _OwnedPacket
from tests.test_telegram_rpc_safety import Clock, Store, generation, intents, record, recovery
from tests.test_telegram_rpc_safety import clock as clock
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


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
    assert _automatic(store)["attempts"] == 1
    assert not await restarted.consume_auto_resume()


async def test_automatic_attempts_back_off_and_exhaust_after_six(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    for attempt, delay in enumerate(telegram_rpc._AUTO_RESUME_DELAYS, start=1):
        clock.advance(delay - 1)
        assert not await ledger.consume_auto_resume()
        clock.advance(1)
        assert await ledger.consume_auto_resume()
        assert _automatic(store)["attempts"] == attempt
        await ledger.record_floor(clock.time() + 1, circuit_open=True)
    assert ledger.protection_reason == "automatic_resume_exhausted"
    clock.advance(10_000_000)
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    assert not await restarted.maintain_auto_resume()
    assert not await restarted.consume_auto_resume()
    assert _automatic(store)["attempts"] == 6


async def test_automatic_wait_rebase_is_persisted_once_per_boot(clock: Clock) -> None:
    store = Store()
    await _waiting(store, clock)
    clock.advance(1000)
    clock.monotonic_seconds = 20
    rebooted = store.ledger(boot_id="boot-b")
    rebooted.configure_auto_resume(enabled=True)
    assert not await rebooted.maintain_auto_resume()
    due = cast(dict[str, object], _automatic(store)["due"])
    assert due["boot_id"] == "boot-b"
    deadline = due["until_monotonic"]
    clock.advance(1800)
    restarted = store.ledger(boot_id="boot-b")
    restarted.configure_auto_resume(enabled=True)
    assert not await restarted.maintain_auto_resume()
    assert cast(dict[str, object], _automatic(store)["due"])["until_monotonic"] == deadline
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


async def test_consumed_trial_crash_is_manual_even_after_wait(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    clock.advance(3600)
    assert await ledger.consume_auto_resume()
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    clock.advance(1_000_000)
    assert restarted.protection_reason == "recovery_trial_incomplete"
    assert restarted.auto_resume_status()["state"] == "manual"
    assert not await restarted.maintain_auto_resume()
    assert not await restarted.consume_auto_resume()


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
    assert _automatic(store)["attempts"] == 1


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


async def _wire_trial(wire: _WireHarness, clock: Clock) -> None:
    gate = wire[0]
    gate.configure_auto_resume(enabled=True)
    await gate.prepare_startup_safety()
    await gate._ensure_safety_ledger().record_floor(clock.time() + 1, circuit_open=True)
    assert not await gate.maintain_auto_resume()
    clock.advance(3600)
    assert await gate.maintain_auto_resume()
    dc_id, key_id = gate._offline_wire_identity()
    gate._safety = TelegramRpcSafetyLedger(gate._safety_store, dc_id=dc_id, key_id=key_id)
    gate.configure_auto_resume(enabled=True)
    await gate.prepare_startup_safety()
    await gate.verify_startup_account(42)
    recovery = gate._ensure_safety_ledger()._automatic_recovery()
    assert recovery is not None and recovery["state"] == "consumed"


async def _prepared_packet(wire: _WireHarness, clock: Clock) -> tuple[asyncio.Future[object], _OwnedPacket]:
    _, sender, connection, _ = wire
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE,
        RpcServiceClass.INTERACTIVE,
        clock.monotonic() + 10,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(2),
        attempt_evidence=RpcAttemptEvidence(),
    )
    response = sender.send_owned(GetStateRequest(), scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    return response, connection._send_queue.get_nowait()


async def test_real_wire_success_and_canonical_progress_are_both_required_for_reset(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _wire_clock(wire, monkeypatch)
    gate, sender, connection, sink = wire
    await _wire_trial(wire, clock)
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
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _wire_clock(wire, monkeypatch)
    gate, sender, connection, sink = wire
    await _wire_trial(wire, clock)
    response, packet = await _prepared_packet(wire, clock)
    gate.configure_auto_resume(enabled=False)
    assert not sender.write_packet(packet, connection._send)
    assert sink.frames == []
    response.cancel()


async def test_stop_during_blocked_intent_commit_prevents_physical_write(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _wire_clock(wire, monkeypatch)
    gate, _, _, sink = wire
    await _wire_trial(wire, clock)
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
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _wire_clock(wire, monkeypatch)
    gate, sender, connection, sink = wire
    await _wire_trial(wire, clock)
    stopped = asyncio.Event()
    gate.set_auto_resume_stop(stopped.is_set)
    response, packet = await _prepared_packet(wire, clock)
    stopped.set()
    assert not sender.write_packet(packet, connection._send)
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
