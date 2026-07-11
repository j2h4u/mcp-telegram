from __future__ import annotations

import asyncio

import pytest

from mcp_telegram.correlation import correlation_context, current_correlation_ids, record_correlation_id
from mcp_telegram.server import app, bootstrap_server


def test_bootstrap_returns_canonical_app_without_re_registering_handlers() -> None:
    handlers_before = dict(app.request_handlers)

    assert bootstrap_server() is app
    assert app.request_handlers == handlers_before


@pytest.mark.asyncio
async def test_correlation_context_isolated_between_concurrent_tasks() -> None:
    barrier = asyncio.Barrier(2)

    async def collect(request_id: str) -> tuple[str, ...]:
        with correlation_context():
            record_correlation_id(request_id)
            await barrier.wait()
            await asyncio.sleep(0)
            return current_correlation_ids()

    first, second = await asyncio.gather(collect("first"), collect("second"))

    assert first == ("first",)
    assert second == ("second",)
    assert current_correlation_ids() == ()
