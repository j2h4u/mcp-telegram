"""A claimed read budget failure resumes only through the real Gate episode."""

import asyncio
from collections.abc import Mapping
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram.peer_access import peer_target
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudgetExhaustedError,
)
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, rpc_attempt_budget, rpc_scope
from tests.test_telegram_demand_coordinator import _adapters
from tests.test_telegram_rpc_method_wire import _clock, _scope
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


async def test_claimed_read_budget_failure_waits_for_gate_and_reuses_revision(  # noqa: PLR0914, PLR0915 - physical composition
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    from mcp_telegram import telegram_demand, telegram_rpc_scheduler

    monkeypatch.setattr(telegram_demand, "time", clock)
    monkeypatch.setattr(telegram_rpc_scheduler, "time", clock)
    kind = DemandKind.DELTA_GAP_FILL
    adapters = _adapters({kind: DemandStatus(0)})
    adapter = adapters[kind]
    selected = WorkRevision(WorkKey(kind, 42, "peer:42"), "revision-1")
    adapter.selected = selected
    adapter.selections = [selected]
    coordinator = TelegramDemandCoordinator(
        cast(Mapping[DemandKind, DurableDemandAdapter], adapters),
        clock=clock.time,
        monotonic_clock=clock.monotonic,
        rpc_eligibility=gate.rpc_eligibility,
        prepare_durable_retry=gate.prepare_durable_retry,
    )
    gate.set_rpc_eligibility_observer(coordinator.eligibility_changed)

    async def acquire() -> None:
        budget = adapter.run_calls[-1]
        with rpc_scope(TelegramRpcSource.DELTA_SYNC, acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE):
            with rpc_attempt_budget(budget), peer_target(42):
                await gate(functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0))

    adapter.on_run = acquire
    first = asyncio.create_task(coordinator._execute_slice(kind))
    state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await sender._prepare_state(state)
    await asyncio.wait_for(first, 2)
    await asyncio.gather(*sender._observing)
    budget = adapter.run_calls[0]
    assert budget.attempts == budget.limit == 1 and len(sink.frames) == 1
    method = budget.last_method
    assert method is not None and gate.account_safety_status() is None
    assert gate.rpc_eligibility(method) is not None
    clock.advance(1)
    gate._admission_scheduler._next_write_at = 0.0
    unrelated = sender.send_owned(functions.users.GetFullUserRequest(types.InputUserSelf()), _scope(clock))
    other = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(other)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    other.future.set_result("unrelated")
    assert await unrelated == "unrelated"
    assert gate.rpc_eligibility(method) is not None
    coordinator._active_kind = None
    other_revision = WorkRevision(WorkKey(kind, 42, "peer:43"), "revision-1")
    adapter.selections.insert(0, other_revision)
    adapter.request_methods[other_revision] = method
    coordinator.scan()
    assert kind not in coordinator.ready_kinds and gate.rpc_eligibility(method) is not None
    adapter.selections.remove(other_revision)
    clock.advance(298)
    coordinator.scan()
    assert kind not in coordinator.ready_kinds and budget.attempts == budget.limit == 1
    assert coordinator.inspect_work(selected)["reason"] == "read_budget_retry"
    clock.advance(2)
    coordinator.scan()
    assert kind in coordinator.ready_kinds
    second = asyncio.create_task(coordinator._execute_slice(kind))
    state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result("ok")
    await asyncio.wait_for(second, 2)
    assert adapter.run_calls == [budget, budget] and budget.attempts == 2
    assert len(sink.frames) == 3 and selected not in coordinator._invocations


async def test_transient_outcome_cannot_refill_claimed_read_budget_without_gate_credit() -> None:
    from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
    from tests.test_telegram_demand_coordinator import _Clock

    clock = _Clock()
    kind = DemandKind.DELTA_GAP_FILL
    adapters = _adapters({kind: DemandStatus(0)})
    adapter = adapters[kind]
    adapter.outcome_kind = SliceOutcomeKind.RETRYABLE_FAILURE

    async def failed_read() -> None:
        budget = adapter.run_calls[-1]
        budget.debit()
        budget._read_budget_failure_method = "messages.getHistory"

    adapter.on_run = failed_read
    coordinator = TelegramDemandCoordinator(adapters, clock=clock, monotonic_clock=clock)
    await coordinator._execute_slice(kind)
    budget = adapter.run_calls[0]
    coordinator._active_kind = None
    clock.value += 31
    coordinator.scan()
    await coordinator._execute_slice(kind)
    assert adapter.run_calls == [budget]
    assert budget.attempts == budget.limit == 1


@pytest.mark.parametrize("reason", ["read_budget_retry", "method_mixed_retry"])
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("during_run", [False, True])
async def test_method_wait_reason_survives_until_eligibility_wakes(
    reason: str, pending: bool, during_run: bool
) -> None:
    from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending
    from tests.test_telegram_demand_coordinator import _Clock

    clock = _Clock()
    kind = DemandKind.DELTA_GAP_FILL
    method = "messages.getHistory"
    adapters = _adapters({kind: DemandStatus(0, request_method=method)})
    adapter = adapters[kind]
    adapter.actual_method = method
    hold = (
        TelegramRpcMethodRecoveryPending(method, "generation", reason=reason)
        if pending
        else TelegramRpcAdmissionDeferred(30, method_key=method, eligible_at=130, reason=reason)
    )
    current: list[TelegramRpcAdmissionDeferred | TelegramRpcMethodRecoveryPending | None] = [hold]
    coordinator = TelegramDemandCoordinator(
        adapters, clock=clock, monotonic_clock=clock, rpc_eligibility=lambda _: current[0]
    )
    if during_run:
        adapter.run_error = hold
        await coordinator._execute_slice(kind, retained_status=DemandStatus(0, selected=adapter.selected))
        coordinator._active_kind = None
    else:
        coordinator.scan()
    assert coordinator.inspect_work(adapter.selected)["reason"] == reason
    current[0] = None
    clock.value = 131
    coordinator.eligibility_changed(method)
    coordinator.scan()
    assert kind in coordinator.ready_kinds
    assert coordinator.inspect_work(adapter.selected)["reason"] is None
