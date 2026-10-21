"""Signal-driven shutdown orchestration for the sync daemon."""

import asyncio
import logging
import signal

logger = logging.getLogger(__name__)


def register_shutdown_handler(
    loop: asyncio.AbstractEventLoop,
) -> asyncio.Event:
    """Register SIGTERM handling and return the daemon's shutdown event.

    The write owner drains accepted operations and checkpoints later; the
    signal callback must never touch its connection or wait for SQLite.
    """
    shutdown_event = asyncio.Event()

    def _on_sigterm() -> None:
        logger.info("SIGTERM received — requesting orderly shutdown")
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    return shutdown_event
