"""Small helpers for invoking durable demand adapters like the coordinator."""

from __future__ import annotations

import time

from mcp_telegram.telegram_demand import DurableDemandAdapter, RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import SliceOutcome


async def run_ready_slice(
    adapter: DurableDemandAdapter,
    *,
    limit: int = 1,
    now: float | None = None,
) -> SliceOutcome | None:
    """Invoke only selected, ready work using the adapter's current revision."""
    observed_at = time.time() if now is None else now
    status = adapter.status(observed_at)
    if status is None or not status.is_ready(observed_at):
        return None
    selected = status.selected
    assert selected is not None, "ready durable demand must identify its work revision"
    return await adapter.run_slice(RpcAttemptBudget(limit=limit, selected=selected))
