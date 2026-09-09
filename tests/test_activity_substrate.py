from __future__ import annotations

import asyncio

import pytest

from mcp_telegram.activity_substrate import ActivityClient, call_with_timeout
from mcp_telegram.telegram_demand import AcquisitionKind, acquisition_context, current_demand_token, demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource

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
