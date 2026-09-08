from __future__ import annotations

import asyncio

import pytest

from mcp_telegram.activity_substrate import ActivityClient, call_with_timeout
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, current_rpc_scope, rpc_scope

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
        with rpc_scope(TelegramRpcSource.ACTIVITY_ARCHIVE):
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
        with rpc_scope(TelegramRpcSource.ACTIVITY_ARCHIVE):
            await call_with_timeout(FailingClient(), object(), timeout_s=1.0)


@pytest.mark.asyncio
async def test_call_with_timeout_rebinds_caller_source_to_detached_task() -> None:
    observed: list[TelegramRpcSource] = []

    class ScopedClient:
        async def __call__(self, request: object) -> object:
            del request
            observed.append(current_rpc_scope().source)
            return object()

        async def get_input_entity(self, dialog_id: int) -> object:
            del dialog_id
            return object()

    with rpc_scope(TelegramRpcSource.ACTIVITY_HOT_SWEEP):
        await call_with_timeout(ScopedClient(), object(), timeout_s=1.0)

    assert observed == [TelegramRpcSource.ACTIVITY_HOT_SWEEP]
