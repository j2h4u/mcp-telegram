"""Temporary Gate capacity pressure resumes the original bounded invocation."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import cast

import pytest
from telethon.tl import functions

from mcp_telegram.telegram_demand import AcquisitionKind, DemandStatus, DurableDemandAdapter
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator, _Invocation
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind, WorkKey, WorkRevision
from mcp_telegram.telegram_rpc import TelegramRpcGate
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)
from tests.test_telegram_demand_coordinator import _Adapter, _adapters
from tests.test_telegram_rpc_method_wire import _clock, _flood, _selected_budget
from tests.test_telegram_rpc_scheduler import _ControlledLimiter, _wait_until
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


async def _hold_capacity(gate: TelegramRpcGate, *, cancelled: bool) -> Callable[[], Awaitable[None]]:
    scheduler = gate._admission_scheduler
    with rpc_scope(TelegramRpcSource.FACT_HYDRATION_LIVE, acquisition_kind=AcquisitionKind.MESSAGE_LOOKUP):
        scope = replace(current_rpc_scope(), request_method="tests.Holder")
    scheduler._capacities[scope.service_class] = 1
    if not cancelled:
        admission = await scheduler.admit(scope)

        async def complete() -> None:
            scheduler.complete(admission)

        return complete
    limiter = _ControlledLimiter()
    previous = scheduler._limiter
    scheduler._limiter = limiter
    holder = asyncio.create_task(scheduler.admit(scope))
    await _wait_until(lambda: limiter.acquisitions == 1)

    async def cancel() -> None:
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        scheduler._limiter = previous
        await limiter.allow_one()

    return cancel


def _capacity_coordinator(
    gate: TelegramRpcGate,
) -> tuple[_Adapter, TelegramDemandCoordinator, list[TelegramRpcAdmissionDeferred]]:
    kind = DemandKind.LIVE_HYDRATION_BATCH
    adapter = _Adapter(kind, DemandStatus(0))
    adapter.selected = WorkRevision(WorkKey(kind, 42, "message"), "1")
    adapter.selections = [adapter.selected]
    adapters = _adapters()
    adapters[kind] = adapter
    coordinator = TelegramDemandCoordinator(
        cast(Mapping[DemandKind, DurableDemandAdapter], adapters),
        rpc_eligibility=gate.rpc_eligibility,
        prepare_durable_retry=gate.prepare_durable_retry,
    )
    gate.set_rpc_eligibility_observer(coordinator.eligibility_changed)
    deferrals: list[TelegramRpcAdmissionDeferred] = []

    async def acquire() -> None:
        with rpc_scope(TelegramRpcSource.FACT_HYDRATION_LIVE, acquisition_kind=AcquisitionKind.MESSAGE_LOOKUP):
            with rpc_attempt_budget(adapter.run_calls[-1]):
                try:
                    await gate(functions.messages.GetMessagesRequest([]))
                except TelegramRpcAdmissionDeferred as exc:
                    deferrals.append(exc)
                    raise
        adapter.current_status = None

    adapter.on_run = acquire
    return adapter, coordinator, deferrals


@pytest.mark.parametrize("cancelled_holder", [False, True])
async def test_gate_capacity_release_resumes_same_revision_without_new_budget_or_deadline(
    wire: _WireHarness, cancelled_holder: bool
) -> None:
    gate, sender, connection, sink = wire
    adapter, coordinator, deferrals = _capacity_coordinator(gate)
    release = await _hold_capacity(gate, cancelled=cancelled_holder)
    first = asyncio.create_task(coordinator._execute_slice(adapter.kind))
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 1)
        with pytest.raises(RpcAdmissionSaturatedError):
            await sender._prepare_state(state)
        await asyncio.wait_for(first, 1)
        budget = adapter.run_calls[0]
        invocation = coordinator._invocations[adapter.selected]
        original_deadline, original_limit = invocation.deadline, budget.limit
        assert [error.reason for error in deferrals] == ["capacity"]
        assert budget.attempts == 0 and sink.frames == []
        assert adapter.selected in coordinator._eligibility_waits
    finally:
        await release()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    coordinator._active_kind = None
    coordinator.scan()
    assert coordinator.ready_kinds == (adapter.kind,)
    assert coordinator._invocations[adapter.selected] is invocation
    assert invocation.deadline == original_deadline
    retry = asyncio.create_task(coordinator._execute_slice(adapter.kind))
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 1)
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        state.future.set_result("complete")
        await asyncio.wait_for(retry, 1)
        assert adapter.run_calls == [budget, budget]
        assert budget.attempts == 1 and budget.limit == original_limit
        assert invocation.deadline == original_deadline
        assert len(sink.frames) == 1
        assert adapter.selected not in coordinator._invocations
    finally:
        if not retry.done():
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("reason", ["capacity", "server"])
async def test_bound_credit_wait_retains_gate_horizon_even_after_it_expires(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, expired: bool, reason: str
) -> None:
    gate, _, _, _ = wire
    clock = _clock(wire, monkeypatch)
    method = await _flood(wire, clock)
    clock.advance(3601)
    budget = _selected_budget()
    authorization = gate.prepare_durable_retry(budget, method)
    assert authorization is not None and budget.selected is not None
    kind = budget.selected.key.demand_kind
    adapters = _adapters({kind: DemandStatus(0, request_method=method)})
    adapter = adapters[kind]
    adapter.selected = budget.selected
    adapter.selections = [budget.selected]
    adapter.run_error = TelegramRpcAdmissionDeferred(1, method_key=method, reason=reason)
    coordinator = TelegramDemandCoordinator(
        cast(Mapping[DemandKind, DurableDemandAdapter], adapters),
        clock=clock.time,
        monotonic_clock=clock.monotonic,
        rpc_eligibility=gate.rpc_eligibility,
        prepare_durable_retry=gate.prepare_durable_retry,
    )
    invocation = _Invocation(budget, authorization.deadline)
    coordinator._invocations[budget.selected] = invocation
    initial = budget.limit, budget.attempts
    await coordinator._execute_slice(kind)
    assert invocation.deadline == authorization.deadline
    clock.advance(authorization.deadline - clock.monotonic() + 1 if expired else 1)
    coordinator._active_kind = None
    coordinator.eligibility_changed(None)
    coordinator.scan()
    adapter.run_error = None
    adapter.outcome_kind = SliceOutcomeKind.NO_PROGRESS
    await coordinator._execute_slice(kind)
    assert len(adapter.run_calls) == (1 if expired else 2)
    assert all(item is budget for item in adapter.run_calls)
    assert invocation.deadline == authorization.deadline
    assert (budget.limit, budget.attempts) == initial
