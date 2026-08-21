"""Neutral activity transport contract and RPC timeout policy.

The daemon owns the concrete Telegram client.  Activity workers depend on this
small protocol instead of importing the global archive worker, which keeps the
transport policy and its cancellation semantics in one place.
"""

import asyncio
from collections.abc import Coroutine
from typing import Protocol


class ActivityClient(Protocol):
    """Minimal Telegram client surface required by activity use cases."""

    def __call__(self, request: object) -> Coroutine[object, object, object]: ...

    def get_input_entity(self, dialog_id: int) -> Coroutine[object, object, object]: ...


# Upper bound on one activity SearchRequest await.  The explicit wait/cancel
# policy below deliberately does not await cancellation completion: Telethon
# can leave an MTProto future unresolved after startup FloodWait.
ACTIVITY_RPC_TIMEOUT_S: float = 120.0


async def call_with_timeout(client: ActivityClient, request: object) -> object:
    """Invoke a Telegram RPC with a hard deadline and abandon on overrun.

    ``asyncio.wait_for`` awaits the wrapped task after cancellation.  Activity
    workers must return promptly when that task wedges, so use ``asyncio.wait``
    and explicitly cancel the pending task instead.  Completed tasks propagate
    their original exception through ``task.result()``.
    """
    task = asyncio.create_task(client(request))
    done, _pending = await asyncio.wait({task}, timeout=ACTIVITY_RPC_TIMEOUT_S)
    if not done:
        task.cancel()
        raise TimeoutError(f"RPC exceeded {ACTIVITY_RPC_TIMEOUT_S}s deadline")
    return task.result()


__all__ = ["ACTIVITY_RPC_TIMEOUT_S", "ActivityClient", "call_with_timeout"]
