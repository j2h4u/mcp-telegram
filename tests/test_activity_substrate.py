from __future__ import annotations

import asyncio
from typing import cast

import pytest
from telethon.tl.functions.messages import SearchRequest  # type: ignore[import-untyped]
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty  # type: ignore[import-untyped]

from mcp_telegram.activity_substrate import ActivityClient, call_with_timeout
from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    current_demand_token,
    demand_context,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    RpcServiceClass,
    current_rpc_scope,
    rpc_attempt_budget,
)
from tests.test_telegram_rpc_scheduler_integration import (
    _ControlledLimiter,
    _make_gate,
    _ScalarRequest,
    _SynchronousSender,
    _wait_until,
)

_TEST_TIMEOUT_S = 0.01


@pytest.mark.asyncio
async def test_call_with_timeout_cancels_a_wedged_rpc_without_waiting() -> None:
    cancelled = asyncio.Event()

    class HangingClient:
        async def __call__(self, request: object) -> object:
            del request
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def get_input_entity(self, dialog_id: int) -> object:
            del dialog_id
            return object()

    client: ActivityClient = HangingClient()
    with pytest.raises(TimeoutError):
        with demand_context(DemandKind.ARCHIVE_INCREMENTAL):
            await call_with_timeout(client, object(), timeout_s=_TEST_TIMEOUT_S)

    await asyncio.wait_for(cancelled.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_call_with_timeout_preserves_rpc_exception() -> None:
    expected = RuntimeError("rpc failed")

    class FailingClient:
        async def __call__(self, request: object) -> object:
            del request
            raise expected

        async def get_input_entity(self, dialog_id: int) -> object:
            del dialog_id
            return object()

    with pytest.raises(RuntimeError, match="rpc failed"):
        with demand_context(DemandKind.ARCHIVE_INCREMENTAL):
            await call_with_timeout(FailingClient(), object(), timeout_s=1.0)


@pytest.mark.asyncio
async def test_call_with_timeout_rebinds_caller_source_to_detached_task() -> None:
    observed: list[tuple[DemandKind, TelegramRpcSource, AcquisitionKind | None]] = []

    class ScopedClient:
        async def __call__(self, request: object) -> object:
            del request
            token = current_demand_token()
            observed.append((token.kind, token.source, token.acquisition_kind))
            return object()

        async def get_input_entity(self, dialog_id: int) -> object:
            del dialog_id
            return object()

    with demand_context(DemandKind.HOT_ACTIVITY_PAGE):
        with acquisition_context(AcquisitionKind.MESSAGE_SEARCH_PAGE):
            await call_with_timeout(ScopedClient(), object(), timeout_s=1.0)

    assert observed == [
        (DemandKind.HOT_ACTIVITY_PAGE, TelegramRpcSource.ACTIVITY_HOT_SWEEP, AcquisitionKind.MESSAGE_SEARCH_PAGE)
    ]


@pytest.mark.asyncio
async def test_call_with_timeout_transfers_slice_budget_to_detached_search() -> None:
    budget = RpcAttemptBudget(limit=1)
    observed_budgets: list[RpcAttemptBudget | None] = []

    class BudgetedClient:
        async def __call__(self, request: object) -> object:
            assert isinstance(request, SearchRequest)
            active_budget = current_rpc_scope().attempt_budget
            observed_budgets.append(active_budget)
            assert active_budget is not None
            active_budget.debit()
            return object()

        async def get_input_entity(self, dialog_id: int) -> object:
            del dialog_id
            return object()

    request = SearchRequest(
        peer=InputPeerEmpty(),
        q="",
        filter=InputMessagesFilterEmpty(),
        min_date=None,
        max_date=None,
        offset_id=0,
        add_offset=0,
        limit=100,
        max_id=0,
        min_id=0,
        hash=0,
    )
    with demand_context(DemandKind.HOT_ACTIVITY_PAGE):
        with acquisition_context(AcquisitionKind.MESSAGE_SEARCH_PAGE):
            with rpc_attempt_budget(budget):
                await call_with_timeout(BudgetedClient(), request, timeout_s=1.0)
                with pytest.raises(RpcAttemptBudgetExhaustedError):
                    await call_with_timeout(BudgetedClient(), request, timeout_s=1.0)

    assert observed_budgets == [budget, budget]
    assert budget.attempts == 1


@pytest.mark.asyncio
async def test_parent_cancel_stops_child_before_blocked_gate_opens() -> None:
    entered = asyncio.Event()
    gate = asyncio.Event()
    cancelled = asyncio.Event()
    dispatched: list[object] = []

    class BlockedClient:
        async def __call__(self, request: object) -> object:
            entered.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            dispatched.append(request)
            return object()

        async def get_input_entity(self, dialog_id: int) -> object:
            return object()

    async def parent() -> None:
        with demand_context(DemandKind.ARCHIVE_INCREMENTAL):
            await call_with_timeout(BlockedClient(), object(), timeout_s=10)

    task = asyncio.create_task(parent())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    gate.set()
    await asyncio.sleep(0)
    assert dispatched == []


@pytest.mark.asyncio
async def test_parent_cancel_removes_actual_gate_ticket_before_limiter_release() -> None:
    limiter = _ControlledLimiter()
    events: list[RpcAdmissionEvent] = []
    gate = _make_gate(limiter, TelegramRpcSchedulerConfig(), events)
    sent: list[tuple[TelegramRpcSource, object]] = []
    gate._main_sender = gate._sender = _SynchronousSender(sent)  # pyright: ignore[reportAttributeAccessIssue]
    cancelled_request = _ScalarRequest("cancelled")
    survivor_request = _ScalarRequest("survivor")

    async def parent() -> None:
        with demand_context(DemandKind.ARCHIVE_INCREMENTAL):
            with acquisition_context(AcquisitionKind.MESSAGE_SEARCH_PAGE):
                await call_with_timeout(cast(ActivityClient, gate), cancelled_request, timeout_s=10)

    async def survivor() -> object:
        with demand_context(DemandKind.FULL_SYNC_PAGE):
            with acquisition_context(AcquisitionKind.MESSAGE_HISTORY_PAGE):
                return await gate(survivor_request)

    task = asyncio.create_task(parent())
    following: asyncio.Task[object] | None = None
    try:
        await _wait_until(lambda: limiter.acquisitions == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        await _wait_until(lambda: all(depth == 0 for depth in gate._admission_scheduler.queue_depths().values()))
        following = asyncio.create_task(survivor())
        await limiter.allow_one()
        await asyncio.wait_for(following, timeout=1)
        assert sent == [(TelegramRpcSource.FULL_SYNC, survivor_request)]
        assert any(event.kind is RpcAdmissionEventKind.CANCELLED for event in events)
        assert gate._admission_scheduler.queue_depths() == dict.fromkeys(RpcServiceClass, 0)
        assert gate._admission_scheduler.active_depths() == dict.fromkeys(RpcServiceClass, 0)
    finally:
        for pending in (task, following):
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        await gate.close_rpc_scheduler()
