"""Public Telethon reconnect recovery owned by the sync daemon."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from .telegram_demand import (
    AcquisitionKind,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    current_demand_token,
    demand_context,
    require_execution_mode,
)
from .telegram_rpc_consumers import DemandKind, ExecutionMode
from .telegram_rpc_scheduler import RpcAdmissionClosedError

logger = logging.getLogger(__name__)


class ReconnectClient(Protocol):
    """Public client surface required for reconnect update recovery."""

    @property
    def reconnect_event(self) -> asyncio.Event: ...

    async def catch_up(self) -> None: ...


async def _request_catch_up(
    client: ReconnectClient,
    observe: Callable[[str, str, str | None], None] | None,
) -> bool:
    try:
        current_demand_token()
    except UnclassifiedTelegramDemandError:
        with demand_context(DemandKind.RECONNECT_DIFFERENCE):
            return await _request_catch_up_in_context(client, observe)
    return await _request_catch_up_in_context(client, observe)


async def _request_catch_up_in_context(
    client: ReconnectClient,
    observe: Callable[[str, str, str | None], None] | None,
) -> bool:
    token = require_execution_mode(ExecutionMode.INLINE)
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


async def _wait_for_reconnect_signal(client: ReconnectClient, shutdown_event: asyncio.Event) -> bool:
    reconnect_task = asyncio.create_task(client.reconnect_event.wait())
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    try:
        done, _ = await asyncio.wait(
            (reconnect_task, shutdown_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done:
            return False
        client.reconnect_event.clear()
        return True
    finally:
        for task in (reconnect_task, shutdown_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(reconnect_task, shutdown_task, return_exceptions=True)


async def run_reconnect_catch_up_loop(
    client: ReconnectClient,
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float,
    observe: Callable[[str, str, str | None], None] | None = None,
    on_reconnect: Callable[[], None] | None = None,
) -> None:
    """Recover missed updates once for each Telethon reconnect signal.

    Telethon owns initial startup catch-up through ``TelegramClient``'s
    ``catch_up=True`` option. This loop consumes the event signalled by
    Telethon's internal reconnect handler. A failed recovery remains pending
    and retries at the configured cadence; repeated signals stay coalesced by
    ``asyncio.Event`` until the current recovery succeeds.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    recovery_needed = False
    while not shutdown_event.is_set():
        if not recovery_needed:
            if not await _wait_for_reconnect_signal(client, shutdown_event):
                break
            recovery_needed = True
            if observe is not None:
                observe("runtime.connection_observed", "reconnected", None)
            if on_reconnect is not None:
                on_reconnect()

        if await _request_catch_up(client, observe):
            recovery_needed = False
            continue
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
        break


__all__ = ["ReconnectClient", "run_reconnect_catch_up_loop"]
