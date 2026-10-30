from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from telethon import types
from telethon.errors import FloodWaitError

from mcp_telegram.telegram_rpc import TelegramRpcAdmissionDeferred, TelegramRpcGate, _TransportBoundaryState
from tests.test_telegram_rpc import (
    _gate,
    _MemorySafetyStore,
    _RawFutureSender,
    _reset_process_policy,  # noqa: F401 - local autouse fixture
    _scripted_authenticator,  # noqa: F401 - local autouse fixture
    _set_sender,
    _wait_for,
)


def _probe_gate() -> tuple[TelegramRpcGate, _RawFutureSender]:
    gate = _gate()
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    return gate, sender


def _store(gate: TelegramRpcGate) -> _MemorySafetyStore:
    return cast(_MemorySafetyStore, vars(gate)["_test_safety_store"])


def _record(gate: TelegramRpcGate) -> dict[str, object]:
    return cast(dict[str, object], json.loads(_store(gate).raw))


@pytest.mark.asyncio
@pytest.mark.parametrize("close_scheduler", [False, True])
async def test_disconnect_waits_for_reconnect_flood_receipt(close_scheduler: bool) -> None:
    gate, sender = _probe_gate()
    saving = asyncio.Event()
    commit = asyncio.Event()
    callback = asyncio.create_task(gate._handle_auto_reconnect())
    await _wait_for(lambda: sender.calls == 1)
    _store(gate).block_floor_saves = True
    _store(gate).floor_save_started = saving
    _store(gate).floor_save_release = commit
    closing = asyncio.create_task(gate.close_rpc_scheduler()) if close_scheduler else gate.disconnect()
    sender.futures[0].set_exception(FloodWaitError(request=None, capture=20))
    try:
        await asyncio.wait_for(saving.wait(), 2)
        await asyncio.sleep(0)
        assert not closing.done()
        assert sender.disconnect_calls == 0
    finally:
        commit.set()
    await callback
    await closing
    record = _record(gate)
    assert cast(float, cast(dict[str, object], record["floor"])["until_utc"]) > 0
    assert sender.disconnect_calls == 1
    if not close_scheduler:
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_disconnect_failure_after_reconnect_preserves_known_settlement() -> None:
    gate = _gate()

    class FailingSender(_RawFutureSender):
        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            raise OSError("reconnect teardown failure")

    sender = FailingSender()
    _set_sender(gate, sender)
    callback = asyncio.create_task(gate._handle_auto_reconnect())
    await _wait_for(lambda: sender.calls == 1)
    closing = gate.disconnect()
    await asyncio.sleep(0)
    assert sender.disconnect_calls == 0
    sender.futures[0].set_result([types.User(id=1, is_self=True)])
    await callback
    with pytest.raises(OSError, match="reconnect teardown failure"):
        await closing
    record = _record(gate)
    assert not record["intents"] and record["protection"] is None
    with pytest.raises(OSError, match="reconnect teardown failure"):
        await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_direct_main_disconnect_drains_claimed_reconnect() -> None:
    gate, sender = _probe_gate()
    callback = asyncio.create_task(gate._handle_auto_reconnect())
    await _wait_for(lambda: sender.calls == 1)
    closing = asyncio.create_task(gate._disconnect_main_sender())
    await asyncio.sleep(0.02)
    before_release = sender.disconnect_calls
    sender.futures[0].set_result([types.User(id=1, is_self=True)])
    assert await asyncio.gather(callback, closing) == [None, None]
    await gate.close_rpc_scheduler()
    assert before_release == 0
    record = _record(gate)
    assert not record["intents"] and record["protection"] is None


@pytest.mark.asyncio
async def test_cancel_disconnect_before_first_step_keeps_teardown_owned() -> None:
    gate, sender = _probe_gate()
    first = gate.disconnect()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    second = gate.disconnect()
    outcome = await asyncio.gather(second, return_exceptions=True)
    await gate._disconnect_main_sender()
    await gate.close_rpc_scheduler()
    assert outcome == [None]
    assert sender.disconnect_calls == 1


@pytest.mark.asyncio
async def test_cancel_disconnect_waiter_keeps_claimed_probe_draining() -> None:
    gate, sender = _probe_gate()
    callback = asyncio.create_task(gate._handle_auto_reconnect())
    await _wait_for(lambda: sender.calls == 1)
    first = gate.disconnect()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = gate.disconnect()
    await asyncio.sleep(0)
    assert not second.done() and sender.disconnect_calls == 0
    sender.futures[0].set_result([types.User(id=1, is_self=True)])
    await callback
    await second
    assert sender.disconnect_calls == 1
    record = _record(gate)
    assert not record["intents"] and record["protection"] is None
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_completed_disconnect_allows_new_transport_connect() -> None:
    gate = _gate(initially_ready=False)
    await gate.disconnect()
    gate._begin_transport_connect()
    assert gate._transport_state is _TransportBoundaryState.CONNECTING
    assert not gate._reconnect_quiescing
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_failed_disconnect_cannot_be_superseded() -> None:
    gate = _gate(initially_ready=False)

    async def failed_teardown() -> None:
        raise OSError("failed teardown")

    owner = asyncio.create_task(failed_teardown())
    await asyncio.gather(owner, return_exceptions=True)
    gate._owned_disconnect = owner
    with pytest.raises(TelegramRpcAdmissionDeferred):
        gate._begin_transport_connect()
    assert gate._owned_disconnect is owner
    await gate.close_rpc_scheduler()
