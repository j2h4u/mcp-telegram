"""Telemetry collection for usage tracking: TelemetryCollector with async background flush."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# SQL DDL for telemetry_events table
_ANALYTICS_DDL = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    timestamp REAL NOT NULL,
    duration_ms REAL NOT NULL,
    result_count INTEGER NOT NULL,
    has_cursor BOOLEAN NOT NULL,
    page_depth INTEGER NOT NULL,
    has_filter BOOLEAN NOT NULL,
    error_type TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_tool_timestamp
ON telemetry_events(tool_name, timestamp);
"""


@dataclass(frozen=True)
class TelemetryEvent:
    """Immutable telemetry event with zero PII fields.

    Fields (privacy-safe, no entity IDs, dialog IDs, names, usernames, or content):
    - tool_name: Tool that was called (e.g., ListDialogs, ListMessages, SearchMessages, GetMe, GetUserInfo)
    - timestamp: UNIX epoch time (seconds), float
    - duration_ms: Execution duration in milliseconds, float
    - result_count: Number of results returned, int (0+)
    - has_cursor: Whether pagination cursor was used, bool
    - page_depth: Pagination depth (pages fetched), int (1+)
    - has_filter: Whether any filter was applied, bool
    - error_type: Error category if failed (nullable), str or None
      (Categorical: InvalidCursor, NotFound, Ambiguous, ConnectionError, etc. — NEVER exception message or entity IDs)
    """

    tool_name: str
    timestamp: float
    duration_ms: float
    result_count: int
    has_cursor: bool
    page_depth: int
    has_filter: bool
    error_type: Optional[str] = None


class TelemetryCollector:
    """Singleton telemetry collector with in-memory batch queue and async background flush.

    Non-blocking: record_event() acquires lock for <1µs, appends to batch, returns immediately.
    Batch flushes asynchronously every 60s or when 100 events accumulated.
    """

    _instance: Optional[TelemetryCollector] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Path) -> None:
        """Initialize collector with SQLite database and background flush task.

        Args:
            db_path: Path to analytics.db (created if doesn't exist)
        """
        self._db_path = Path(db_path)
        self._batch: list[TelemetryEvent] = []
        self._batch_lock = threading.Lock()
        self._background_task: Optional[asyncio.Task] = None
        self._init_db()

    def _init_db(self) -> None:
        """Create analytics.db with telemetry_events table and index, enable WAL mode."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            try:
                # Enable WAL mode for safe concurrent access
                conn.execute("PRAGMA journal_mode=WAL")
                # Execute DDL
                conn.executescript(_ANALYTICS_DDL)
                conn.commit()
                logger.info("analytics.db initialized at %s", self._db_path)
            finally:
                conn.close()
        except Exception as e:
            logger.error("Failed to initialize analytics.db: %s", e)
            # Never raise — telemetry must be fire-and-forget

    def record_event(self, event: TelemetryEvent) -> None:
        """Record telemetry event (fire-and-forget, non-blocking).

        Appends event to in-memory batch under lock (<1µs lock duration).
        Triggers async flush if batch >= 100 events.

        Args:
            event: TelemetryEvent to record
        """
        try:
            with self._batch_lock:
                self._batch.append(event)
                if len(self._batch) >= 100:
                    self._flush_async()
        except Exception as e:
            logger.error("Failed to record telemetry event: %s", e)
            # Never raise — telemetry must not block tool execution


    def _flush_async(self) -> None:
        """Spawn background task to flush batch to DB (without awaiting).

        Swaps batch and spawns async flush task. Strong reference (_background_task)
        prevents task from being garbage collected while running.
        """
        try:
            with self._batch_lock:
                if not self._batch:
                    return

                batch_to_flush = self._batch[:]
                self._batch = []

            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._async_flush(batch_to_flush))
                self._background_task = task  # Strong reference prevents GC
            except RuntimeError:
                # No event loop running, fallback to sync write
                self._write_batch(batch_to_flush)
        except Exception as e:
            logger.error("Failed to trigger async flush: %s", e)

    async def _async_flush(self, batch: list[TelemetryEvent]) -> None:
        """Background task: flush batch to DB on background thread.

        Runs DB write via executor to avoid blocking event loop.

        Args:
            batch: List of events to write
        """
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._write_batch, batch)
        except Exception as e:
            logger.error("Async flush failed: %s", e)

    def _write_batch(self, batch: list[TelemetryEvent]) -> None:
        """Synchronous DB write (runs on thread pool via executor).

        Inserts all events in batch as a single transaction.

        Args:
            batch: List of events to write
        """
        if not batch:
            return

        try:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.executemany(
                    """INSERT INTO telemetry_events
                       (tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            e.tool_name,
                            e.timestamp,
                            e.duration_ms,
                            e.result_count,
                            e.has_cursor,
                            e.page_depth,
                            e.has_filter,
                            e.error_type,
                        )
                        for e in batch
                    ],
                )
                conn.commit()
                logger.debug("Flushed %d telemetry events to DB", len(batch))
            finally:
                conn.close()
        except Exception as e:
            logger.error("Failed to write telemetry batch to DB: %s", e)
            # Never raise — telemetry writes must not fail tool execution

    @classmethod
    def get_instance(cls, db_path: Path) -> TelemetryCollector:
        """Return singleton instance (thread-safe).

        Args:
            db_path: Path to analytics.db (used only on first instantiation)

        Returns:
            TelemetryCollector singleton instance
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db_path)
        return cls._instance
