"""Telemetry collection for usage tracking: TelemetryCollector with async background flush."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

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
    - tool_name: Tool that was called (e.g., ListDialogs, ListMessages, SearchMessages, GetMyAccount, GetUserInfo)
    - timestamp: UNIX epoch time (seconds), float
    - duration_ms: Execution duration in milliseconds, float
    - result_count: Number of results returned, int (0+)
    - has_cursor: Whether continuation state from a previous page was reused, bool
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
    error_type: str | None = None


class TelemetryCollector:
    """Singleton telemetry collector with in-memory batch queue and async background flush.

    Non-blocking: record_event() acquires lock for <1µs, appends to batch, returns immediately.
    Batch flushes asynchronously every 60s or when 100 events accumulated.
    """

    _instance: TelemetryCollector | None = None
    _lock = threading.Lock()

    def __init__(self, db_path: Path) -> None:
        """Initialize collector with SQLite database and background flush task.

        Args:
            db_path: Path to analytics.db (created if doesn't exist)
        """
        self._db_path = Path(db_path)
        self._batch: list[TelemetryEvent] = []
        self._batch_lock = threading.Lock()
        self._background_task: asyncio.Task | None = None
        self._init_db()

    def _init_db(self) -> None:
        """Create analytics.db with telemetry_events table and index, enable WAL mode."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            try:
                # WAL mode for safe concurrent access
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_ANALYTICS_DDL)
                conn.commit()
                logger.info("analytics.db initialized at %s", self._db_path)
            finally:
                conn.close()
        except Exception as e:
            # Telemetry is fire-and-forget — never raise
            logger.error("Failed to initialize analytics.db: %s", e, exc_info=True)

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
                    self._flush_async_unlocked()
        except Exception as e:
            logger.error("Failed to record telemetry event: %s", e, exc_info=True)


    def _flush_async_unlocked(self) -> None:
        """Internal: spawn background task to flush batch (caller must hold _batch_lock).

        Swaps batch and spawns async flush task. Strong reference (_background_task)
        prevents task from being garbage collected while running.

        IMPORTANT: Must be called while holding self._batch_lock to avoid races.
        """
        try:
            if not self._batch:
                return

            batch_to_flush = self._batch[:]

            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._async_flush(batch_to_flush))
                task.add_done_callback(self._on_flush_done)
                self._background_task = task
                self._batch = []
            except RuntimeError:
                # No event loop — synchronous fallback (blocks while holding lock)
                logger.debug("telemetry_flush_sync_fallback: no running event loop")
                self._write_batch(batch_to_flush)
                self._batch = []
        except Exception as e:
            logger.error("Failed to trigger async flush: %s", e, exc_info=True)

    @staticmethod
    def _on_flush_done(task: asyncio.Task) -> None:
        """Log unhandled exceptions from background flush tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Background flush task failed: %s", exc)

    def _flush_async(self) -> None:
        """Public: spawn background task to flush batch to DB (thread-safe).

        Acquires lock and delegates to _flush_async_unlocked.
        """
        try:
            with self._batch_lock:
                self._flush_async_unlocked()
        except Exception as e:
            logger.error("Failed to trigger async flush: %s", e, exc_info=True)

    async def _async_flush(self, batch: list[TelemetryEvent]) -> None:
        """Background task: flush batch to DB on background thread.

        Runs DB write via executor to avoid blocking event loop.

        Args:
            batch: List of events to write
        """
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_batch, batch)
        except Exception as e:
            logger.error("Async flush failed: %s", e, exc_info=True)

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
                            event.tool_name,
                            event.timestamp,
                            event.duration_ms,
                            event.result_count,
                            event.has_cursor,
                            event.page_depth,
                            event.has_filter,
                            event.error_type,
                        )
                        for event in batch
                    ],
                )
                conn.commit()
                logger.debug("Flushed %d telemetry events to DB", len(batch))
            finally:
                conn.close()
        except Exception as e:
            logger.error("Failed to write telemetry batch to DB: %s", e, exc_info=True)

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


async def cleanup_analytics_db(db_path: Path, retention_days: int = 30) -> None:
    """Delete stale telemetry events and optimize database (non-blocking).

    Async wrapper that delegates to _sync_cleanup() on thread pool to avoid
    blocking the event loop during database maintenance operations.

    Args:
        db_path: Path to analytics.db
        retention_days: Delete events older than this many days (default 30)
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _sync_cleanup, db_path, retention_days)


def _sync_cleanup(db_path: Path, retention_days: int) -> None:
    """Synchronous database cleanup (runs on thread pool).

    Deletes telemetry events older than retention_days, rebuilds statistics
    with PRAGMA optimize, and reclaims disk space with PRAGMA incremental_vacuum.

    Args:
        db_path: Path to analytics.db
        retention_days: Delete events older than this many days
    """
    try:
        cutoff_timestamp = time.time() - (retention_days * 86400)
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "DELETE FROM telemetry_events WHERE timestamp < ?",
                (cutoff_timestamp,),
            )
            deleted_count = cursor.rowcount
            conn.commit()

            conn.execute("PRAGMA optimize")
            conn.commit()

            # Incremental VACUUM is non-blocking on WAL
            conn.execute("PRAGMA incremental_vacuum(1000)")
            conn.commit()

            logger.info(
                "Analytics cleanup: deleted %d events (>%d days), optimized, vacuumed",
                deleted_count,
                retention_days,
            )
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to cleanup analytics.db: %s", e, exc_info=True)


def format_usage_summary(stats: dict) -> str:
    """Generate <100 token natural-language summary of usage patterns.

    Input dict keys:
    - tool_distribution: dict[str, int] — {tool_name: count}
    - error_distribution: dict[str, int] — {error_type: count}
    - max_page_depth: int
    - dialogs_with_deep_scroll: int (estimated)
    - total_calls: int
    - filter_count: int
    - latency_median_ms: float
    - latency_p95_ms: float

    Output: natural-language string, target 60-80 tokens, < 100 hard limit.
    """
    parts = []

    if stats.get("tool_distribution"):
        top_tools = sorted(stats["tool_distribution"].items(), key=lambda x: x[1], reverse=True)[:2]
        if top_tools:
            top_tool, top_count = top_tools[0]
            top_pct = int(top_count * 100 / stats["total_calls"]) if stats["total_calls"] > 0 else 0
            parts.append(f"Most active: {top_tool} ({top_pct}% of calls)")

    if stats.get("max_page_depth", 0) >= 5:
        parts.append(f"Deep scrolling detected: max page depth {stats['max_page_depth']}")

    if stats.get("error_distribution"):
        errors_str = ", ".join(
            [f"{err} ({cnt})" for err, cnt in sorted(stats["error_distribution"].items(), key=lambda x: x[1], reverse=True)[:3]]
        )
        parts.append(f"Errors: {errors_str}")

    if stats.get("total_calls", 0) > 0 and stats.get("filter_count", 0) > 0:
        filter_pct = int(stats["filter_count"] * 100 / stats["total_calls"])
        parts.append(f"Filtered queries: {filter_pct}%")

    median = stats.get("latency_median_ms", 0)
    p95 = stats.get("latency_p95_ms", 0)
    if median or p95:
        parts.append(f"Response time: {median:.0f}ms median, {p95:.0f}ms p95")

    summary = " ".join(parts)

    # Safety: if summary exceeds 100 tokens, truncate gracefully
    tokens = summary.split()
    if len(tokens) > 100:
        summary = " ".join(tokens[:100]) + "..."

    return summary
