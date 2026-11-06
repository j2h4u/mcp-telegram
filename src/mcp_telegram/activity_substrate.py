"""Neutral activity transport contract and RPC timeout policy.

The daemon owns the concrete Telegram client.  Activity workers depend on this
small protocol instead of importing the global archive worker, which keeps the
transport policy and its cancellation semantics in one place.
"""

import asyncio
from collections.abc import Coroutine
from typing import Protocol

from .image_authorization import ScopeToken
from .telegram_demand import current_demand_token
from .telegram_rpc_scheduler import create_detached_rpc_task, current_rpc_scope


class ActivityRpcClient(Protocol):
    """Telegram RPC surface used by local activity helpers."""

    def __call__(self, request: object) -> Coroutine[object, object, object]: ...


class ActivityClient(ActivityRpcClient, Protocol):
    """Activity sweep client with session authorization."""

    def get_input_entity(self, dialog_id: int) -> Coroutine[object, object, object]: ...

    def capture_image_scope(self) -> ScopeToken | None: ...

    def claim_image_scope(self, token: ScopeToken) -> bool: ...


async def call_with_timeout(client: ActivityRpcClient, request: object, *, timeout_s: float) -> object:
    """Pass the finite deadline to the gateway and retain its response drain."""
    # A plain create_task() copies the caller token but leaves the caller as its
    # owner, which the gate correctly rejects. Transfer the complete token so a
    # timed-out activity call keeps its exact root and nested acquisition.
    token = current_demand_token()
    attempt_budget = current_rpc_scope().attempt_budget
    task = create_detached_rpc_task(
        client(request),
        source=token.source,
        timeout_seconds=timeout_s,
        name="activity-telegram-rpc",
        demand_token=token,
        attempt_budget=attempt_budget,
    )
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_cancelled_task)
        raise
    return task.result()


def _consume_cancelled_task(task: asyncio.Task[object]) -> None:
    """Drain a timed-out detached RPC's eventual cancellation result."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


__all__ = ["ActivityClient", "ActivityRpcClient", "call_with_timeout"]
