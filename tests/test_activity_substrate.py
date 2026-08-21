from __future__ import annotations

import asyncio

import pytest

from mcp_telegram.activity_substrate import ActivityClient, call_with_timeout

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
        await call_with_timeout(FailingClient(), object(), timeout_s=1.0)
