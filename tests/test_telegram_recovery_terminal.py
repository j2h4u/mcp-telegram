"""Terminal failure causes remain distinct from unknown request outcomes."""

import asyncio
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telethon.errors import RPCError
from telethon.network.connection.tcpfull import ConnectionTcpFull
from telethon.tl import functions, types

from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.daemon import _connect_telegram
from mcp_telegram.telegram_rpc import TelegramRpcBudget, TelegramRpcGate
from mcp_telegram.telegram_rpc_recovery import RecoveryCause
from mcp_telegram.telegram_transport import TransportConnectError, TransportWriteError, _write_frame
from tests.test_daemon import _typed_ctx
from tests.test_telegram_auto_resume import _prepared_packet
from tests.test_telegram_auto_resume import trial_wire as trial_wire
from tests.test_telegram_rpc_method_wire import _clock, _scope
from tests.test_telegram_rpc_safety import Clock, Store, recovery
from tests.test_telegram_rpc_safety import clock as clock
from tests.test_telegram_rpc_wire_integration import _Circuit, _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.mark.parametrize("backend", [False, True])
async def test_trial_terminal_transient_preserves_outcome_and_cause(
    trial_wire: tuple[_WireHarness, Clock], backend: bool
) -> None:
    wire, _ = trial_wire
    gate, sender, connection, _ = wire
    response, packet = await _prepared_packet(wire, trial_wire[1])
    assert sender.write_packet(packet, connection._send)
    request = functions.users.GetUsersRequest([types.InputUserSelf()])
    error = RPCError(request, "failure", code=503) if backend else ConnectionError("lost")
    packet.state.state.future.set_exception(error)
    with pytest.raises(type(error)):
        await response
    ledger = gate._ensure_safety_ledger()
    trial = cast(dict[str, object], ledger._record["recovery"])
    assert trial["state"] == "fencing"
    automatic = cast(dict[str, object], trial["automatic"])
    assert automatic["cause"] == "transient"
    intents = cast(dict[str, dict[str, object]], ledger._record["intents"])
    assert len(intents) == (0 if backend else 1)
    if not backend:
        assert next(iter(intents.values()))["phase"] == "unknown"


async def test_healthy_backend_error_does_not_create_an_account_hold(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, _ = wire
    clock = _clock(wire, monkeypatch)
    request = functions.users.GetFullUserRequest(types.InputUserSelf())
    response = sender.send_owned(request, _scope(clock))
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_exception(RPCError(request, "failure", code=503))
    with pytest.raises(RPCError):
        await response
    ledger = gate._ensure_safety_ledger()
    assert ledger.protection_reason is None
    assert ledger._automatic_recovery() is None


async def test_interrupted_sixth_flood_trial_latches_spent_budget_without_inventing_flood(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    ledger.configure_auto_resume(enabled=True)
    await ledger.record_floor(clock.time() + 1, circuit_open=True)
    await ledger.maintain_auto_resume()
    for _ in range(6):
        clock.advance(86400)
        assert await ledger.consume_auto_resume()
        ledger = store.ledger()
        ledger.configure_auto_resume(enabled=True)
        await ledger.maintain_auto_resume()
    assert recovery(store)["state"] == "exhausted"
    assert ledger.auto_resume_status()["block_reason"] == "flood_budget_spent"
    clock.advance(1000000)
    assert not await ledger.consume_auto_resume()


def test_writer_error_is_typed_at_physical_write_boundary() -> None:
    def fail(_frame: bytes) -> None:
        raise OSError("socket failed")

    with pytest.raises(TransportWriteError):
        _write_frame(fail, b"frame")


async def test_local_os_error_does_not_become_a_transport_retry(wire: _WireHarness) -> None:
    gate = wire[0]
    await gate.fail_startup_recovery(OSError("session save failed"))
    assert gate._wire_fault == "transport_integrity_failure"
    assert gate._recovery_cause is None
    assert gate._ensure_safety_ledger().protection_reason == "transport_integrity_failure"


async def test_integrity_failure_dominates_an_existing_fencing_episode(wire: _WireHarness) -> None:
    gate = wire[0]
    ledger = gate._ensure_safety_ledger()
    ledger.configure_auto_resume(enabled=True)
    await ledger.maintain_auto_resume(cause=RecoveryCause.TRANSIENT, quiesced=False)
    assert cast(dict[str, object], ledger._record["recovery"])["state"] == "fencing"
    await gate.fail_startup_recovery(OSError("session save failed"))
    assert ledger.protection_reason == "transport_integrity_failure"
    assert not await ledger.maintain_auto_resume(cause=RecoveryCause.TRANSIENT)


async def test_daemon_holds_local_session_save_error(wire: _WireHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = wire[0]
    gate.configure_auto_resume(enabled=True)
    assert gate.session is not None

    def fail_session_save() -> None:
        raise OSError("session save failed")

    monkeypatch.setattr(gate.session, "save", fail_session_save)

    async def connect_with_failed_session_save() -> None:
        assert gate.session is not None
        gate.session.save()

    monkeypatch.setattr(gate, "connect", connect_with_failed_session_save)
    shutdown = asyncio.Event()
    task = asyncio.create_task(_connect_telegram(_typed_ctx(client=gate, shutdown_event=shutdown)))
    try:
        for _ in range(1000):
            if gate._ensure_safety_ledger().protection_reason == "transport_integrity_failure":
                break
            await asyncio.sleep(0.01)
        assert gate._ensure_safety_ledger().protection_reason == "transport_integrity_failure"
        assert not await gate.maintain_auto_resume()
        assert not task.done()
    finally:
        shutdown.set()
        await task
    gate._safety = None
    assert gate._ensure_safety_ledger().protection_reason == "transport_integrity_failure"


async def test_actual_socket_connect_error_selects_transient_recovery(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, _, connection, _ = wire
    monkeypatch.setattr(ConnectionTcpFull, "connect", AsyncMock(side_effect=OSError("socket failed")))
    with pytest.raises(TransportConnectError) as caught:
        await connection.connect()
    await gate.fail_startup_recovery(caught.value)
    gate.configure_auto_resume(enabled=True)
    await gate.prepare_startup_safety()
    assert not await gate.maintain_auto_resume()
    trial = gate._ensure_safety_ledger()._automatic_recovery()
    assert trial is not None
    assert cast(dict[str, object], trial["automatic"])["cause"] == "transient"


async def test_vendor_socket_retries_reach_daemon_as_transient(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = wire[0]
    gate = TelegramRpcGate(
        original.session,
        1,
        "test",
        rpc_budget=TelegramRpcBudget(30, 60),
        circuit_status=_Circuit,
        fallback_wait_seconds=1,
        cooldown_buffer_seconds=0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=original._safety_store,
    )
    monkeypatch.setattr(gate._main_sender, "_retries", 1)
    monkeypatch.setattr(gate._main_sender, "_delay", 0)
    gate.configure_auto_resume(enabled=True)
    monkeypatch.setattr(ConnectionTcpFull, "connect", AsyncMock(side_effect=OSError("socket failed")))
    shutdown = asyncio.Event()
    task = asyncio.create_task(_connect_telegram(_typed_ctx(client=gate, shutdown_event=shutdown)))
    try:
        for _ in range(1000):
            if gate._recovery_cause is not None:
                break
            await asyncio.sleep(0.01)
        assert gate._recovery_cause == "transient"
        assert not await gate.maintain_auto_resume()
        trial = gate._ensure_safety_ledger()._automatic_recovery()
        assert trial is not None
        assert cast(dict[str, object], trial["automatic"])["cause"] == "transient"
    finally:
        shutdown.set()
        await task
        await gate._admission_scheduler.close()
