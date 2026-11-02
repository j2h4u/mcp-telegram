"""Signal-driven shutdown orchestration for the sync daemon."""

import asyncio
import logging
import signal

logger = logging.getLogger(__name__)


class ShutdownRequest:
    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.reason_code: str | None = None

    def request(self, reason_code: str) -> None:
        if self.reason_code is None:
            self.reason_code = reason_code
        self.event.set()


def register_shutdown_handler(
    loop: asyncio.AbstractEventLoop,
) -> ShutdownRequest:
    """Register SIGTERM handling and return the daemon's shutdown event.

    The write owner drains accepted operations and checkpoints later; the
    signal callback must never touch its connection or wait for SQLite.
    """
    shutdown = ShutdownRequest()

    def _on_sigterm() -> None:
        logger.info("SIGTERM received — requesting orderly shutdown")
        shutdown.request("sigterm")

    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    return shutdown
