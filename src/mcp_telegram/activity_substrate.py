"""Neutral activity transport contract and RPC timeout policy.

The daemon owns the concrete Telegram client.  Activity workers depend on this
small protocol instead of importing the global archive worker, which keeps the
transport policy and its cancellation semantics in one place.
"""

import asyncio
from collections.abc import Coroutine
from typing import Protocol

from .telegram_rpc_scheduler import create_detached_rpc_task, current_rpc_scope


class ActivityClient(Protocol):
    """Minimal Telegram client surface required by activity use cases."""

    def __call__(self, request: object) -> Coroutine[object, object, object]: ...

    def get_input_entity(self, dialog_id: int) -> Coroutine[object, object, object]: ...


async def call_with_timeout(client: ActivityClient, request: object, *, timeout_s: float) -> object:
    """Invoke a Telegram RPC with a hard deadline and abandon on overrun.

    ``asyncio.wait_for`` awaits the wrapped task after cancellation.  Activity
    workers must return promptly when that task wedges, so use ``asyncio.wait``
    and explicitly cancel the pending task instead.  Completed tasks propagate
    their original exception through ``task.result()``.
    """
    # The scheduler scope is owned by the caller task.  A plain create_task()
    # copies that context but keeps the caller as its owner, which the gate
    # correctly rejects.  Rebind the same source to an explicitly detached
    # task so timed-out activity calls remain classified at the transport.
    scope = current_rpc_scope()
    task = create_detached_rpc_task(
        client(request),
        source=scope.source,
        timeout_seconds=timeout_s,
        name="activity-telegram-rpc",
    )
    done, _pending = await asyncio.wait({task}, timeout=timeout_s)
    if not done:
        task.cancel()
        task.add_done_callback(_consume_cancelled_task)
        raise TimeoutError(f"RPC exceeded {timeout_s}s deadline")
    return task.result()


def _consume_cancelled_task(task: asyncio.Task[object]) -> None:
    """Drain a timed-out detached RPC's eventual cancellation result."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


__all__ = ["ActivityClient", "call_with_timeout"]
