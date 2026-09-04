"""Signal-driven shutdown orchestration for the sync daemon."""

import asyncio
import logging
import signal
import sqlite3

from .sqlite_checkpoint import checkpoint_sqlite_connection

logger = logging.getLogger(__name__)


def register_shutdown_handler(
    conn: sqlite3.Connection,
    loop: asyncio.AbstractEventLoop,
    feedback_conn: sqlite3.Connection | None = None,
) -> asyncio.Event:
    """Register SIGTERM handling and return the daemon's shutdown event.

    The production sync database is checkpointed first.  The optional
    feedback database is attempted independently so either failure cannot
    prevent the event from being set and the daemon from exiting.
    """
    shutdown_event = asyncio.Event()

    def _on_sigterm() -> None:
        logger.info("SIGTERM received — checkpointing sync.db")
        try:
            checkpoint_sqlite_connection(conn)
        except Exception:
            logger.exception("sync.db shutdown error")

        try:
            if feedback_conn is not None:
                checkpoint_sqlite_connection(feedback_conn)
        except Exception:
            logger.exception("feedback.db shutdown error (suppressed — shutdown continues)")
        finally:
            shutdown_event.set()

    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    return shutdown_event
