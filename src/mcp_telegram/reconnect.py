"""Public Telethon reconnect recovery owned by the sync daemon."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class ReconnectClient(Protocol):
    """Public client surface required for reconnect update recovery."""

    def is_connected(self) -> bool: ...

    async def catch_up(self) -> None: ...


async def run_reconnect_catch_up_loop(
    client: ReconnectClient,
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float,
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

    was_connected = bool(client.is_connected())
    recovery_needed = False
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break
        except TimeoutError:
            pass

        connected = bool(client.is_connected())
        if not connected:
            recovery_needed = False
        elif not was_connected:
            recovery_needed = True
        if connected and recovery_needed:
            try:
                await client.catch_up()
            except Exception:
                logger.warning("telegram reconnect catch_up failed", exc_info=True)
            else:
                logger.info("telegram reconnect catch_up complete")
                recovery_needed = False
        was_connected = connected


__all__ = ["ReconnectClient", "run_reconnect_catch_up_loop"]
