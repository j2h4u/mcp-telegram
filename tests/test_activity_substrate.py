from __future__ import annotations

import asyncio

import pytest
from telethon.tl.functions.messages import SearchRequest  # type: ignore[import-untyped]
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty  # type: ignore[import-untyped]

from mcp_telegram.activity_substrate import ActivityClient, call_with_timeout
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    current_demand_token,
    demand_context,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope, rpc_attempt_budget

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
