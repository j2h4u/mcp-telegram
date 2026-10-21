"""Inline owner double for SQL-unit fixtures, not runtime ownership tests."""

import asyncio
import sqlite3
from collections.abc import Callable
from typing import cast

from mcp_telegram.event_handlers import EventHandlerManager, UpdateProcessingBarrier, _EventHandlerClient
from mcp_telegram.runtime_observations import RuntimeObservationSink, record_runtime_observation
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction


class _InlineWriter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    async def run[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return operation(self.conn)


def inline_writer(conn: sqlite3.Connection) -> SQLiteWriter:
    """Keep isolated SQL scenarios synchronous; use a real owner for concurrency."""
    return cast(SQLiteWriter, _InlineWriter(conn))


class _InlineObservationSink:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(self, **event: object) -> bool:
        with write_transaction(self.conn):
            record_runtime_observation(self.conn, **event)  # type: ignore[arg-type]
        return True


def inline_observation_sink(conn: sqlite3.Connection) -> RuntimeObservationSink:
    """Persist observations immediately in isolated SQL tests only."""
    return cast(RuntimeObservationSink, _InlineObservationSink(conn))


def event_manager(
    client: _EventHandlerClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    update_barrier: UpdateProcessingBarrier | None = None,
) -> EventHandlerManager:
    """Build a SQL-unit manager; runtime ownership tests use the real writer."""
    manager = EventHandlerManager(client, conn, shutdown_event, update_barrier, writer=inline_writer(conn))
    manager.bind_observation_sink(inline_observation_sink(conn))
    return manager
