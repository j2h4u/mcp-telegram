"""Public Telethon reconnect recovery owned by the sync daemon."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from .telegram_demand import AcquisitionKind, acquisition_context, current_demand_token, require_execution_mode
from .telegram_rpc_consumers import DemandKind, ExecutionMode
from .telegram_rpc_scheduler import RpcAdmissionClosedError

logger = logging.getLogger(__name__)


class ReconnectClient(Protocol):
    """Public client surface required for reconnect update recovery."""

    def is_connected(self) -> bool: ...

    async def catch_up(self) -> None: ...


async def _request_catch_up(
    client: ReconnectClient,
    observe: Callable[[str, str, str | None], None] | None,
) -> bool:
    token = require_execution_mode(ExecutionMode.DURABLE)
    if token.kind is not DemandKind.RECONNECT_DIFFERENCE:
        raise RuntimeError("reconnect catch-up requires reconnect_difference demand")
    with acquisition_context(AcquisitionKind.UPDATE_DIFFERENCE):
        try:
            await client.catch_up()
        except RpcAdmissionClosedError:
            raise
        except Exception:
            logger.warning("telegram reconnect catch_up failed", exc_info=True)
            if observe is not None:
                observe("runtime.catch_up_request_failed", "failed", "catch_up_exception")
            return False
        logger.info("telegram reconnect catch_up requested")
        if observe is not None:
            observe("runtime.catch_up_requested", "requested", None)
        return True


def _observe_connection_transition(
    observe: Callable[[str, str, str | None], None] | None,
    *,
    connected: bool,
    was_connected: bool,
) -> None:
    if connected != was_connected and observe is not None:
        observe("runtime.connection_observed", "connected" if connected else "disconnected", None)


async def run_reconnect_catch_up_loop(
    client: ReconnectClient,
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float,
    observe: Callable[[str, str, str | None], None] | None = None,
) -> None:
    """Recover missed updates once for each observed reconnect transition.

    Telethon owns initial startup catch-up through ``TelegramClient``'s
    ``catch_up=True`` option. This loop only invokes the public ``catch_up``
    method after observing a disconnected to connected transition. A failed
    recovery remains pending and retries at the configured poll cadence until
    the public connection state changes back to disconnected or recovery
    succeeds.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    token = current_demand_token()
    if token.kind is not DemandKind.RECONNECT_DIFFERENCE:
        raise RuntimeError("reconnect loop requires reconnect_difference demand")
    require_execution_mode(ExecutionMode.DURABLE)

    was_connected = bool(client.is_connected())
    recovery_needed = False
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break
        except TimeoutError:
            pass

        connected = bool(client.is_connected())
        _observe_connection_transition(observe, connected=connected, was_connected=was_connected)
        if not connected:
            recovery_needed = False
        elif not was_connected:
            recovery_needed = True
        if connected and recovery_needed and await _request_catch_up(client, observe):
            recovery_needed = False
        was_connected = connected


__all__ = ["ReconnectClient", "run_reconnect_catch_up_loop"]
