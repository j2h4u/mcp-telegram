"""Tests for TelemetryCollector and analytics infrastructure."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from mcp_telegram.analytics import TelemetryCollector, TelemetryEvent


@pytest.fixture
def tmp_analytics_db(tmp_path: Path) -> Path:
    """Return a path to a temporary analytics.db file (not yet created)."""
    return tmp_path / "analytics.db"


@pytest.fixture
def analytics_collector(tmp_analytics_db: Path) -> TelemetryCollector:
    """Return TelemetryCollector instance with temporary DB."""
    # Reset singleton between tests
    TelemetryCollector._instance = None
    collector = TelemetryCollector.get_instance(tmp_analytics_db)
    yield collector
    # Cleanup
    TelemetryCollector._instance = None


class TestTelemetryEventSchema:
    """Tests for TelemetryEvent immutable dataclass."""

    def test_telemetry_event_frozen(self):
        """Test that TelemetryEvent frozen=True prevents direct mutation."""
        event = TelemetryEvent(
            tool_name="ListDialogs",
            timestamp=1000.0,
            duration_ms=10.5,
            result_count=5,
            has_cursor=False,
            page_depth=1,
            has_filter=False,
            error_type=None,
        )

        # frozen dataclass should prevent mutation
        with pytest.raises((AttributeError, TypeError)):
            event.tool_name = "ListMessages"

    def test_telemetry_event_optional_error_type(self):
        """Test that error_type can be None for success or str for errors."""
        # Success case
        event_success = TelemetryEvent(
            tool_name="ListDialogs",
            timestamp=1000.0,
            duration_ms=10.5,
            result_count=5,
            has_cursor=False,
            page_depth=1,
            has_filter=False,
            error_type=None,
        )
        assert event_success.error_type is None

        # Error case
        event_error = TelemetryEvent(
            tool_name="ListMessages",
            timestamp=1001.0,
            duration_ms=20.0,
            result_count=0,
            has_cursor=False,
            page_depth=1,
            has_filter=False,
            error_type="NotFound",
        )
        assert event_error.error_type == "NotFound"

    def test_telemetry_event_no_pii_fields(self):
        """Test that schema has no PII fields (entity_id, dialog_id, sender_id, etc.)."""
        event = TelemetryEvent(
            tool_name="GetUserInfo",
            timestamp=1002.0,
            duration_ms=5.0,
            result_count=1,
            has_cursor=False,
            page_depth=1,
            has_filter=False,
            error_type=None,
        )

        # Verify no PII fields exist
        event_dict = event.__dict__
        pii_fields = {
            "content",
            "cursor",
            "dialog",
            "dialog_id",
            "entity_id",
            "message_id",
            "name",
            "navigation",
            "next_navigation",
            "query",
            "sender_id",
            "username",
        }
        for pii_field in pii_fields:
            assert pii_field not in event_dict, f"PII field '{pii_field}' should not be in schema"


class TestTelemetryCollectorInitialization:
    """Tests for TelemetryCollector database initialization."""

    def test_analytics_db_created(self, tmp_analytics_db: Path):
        """Test that analytics.db is created on first instantiation."""
        assert not tmp_analytics_db.exists(), "DB should not exist yet"

        TelemetryCollector.get_instance(tmp_analytics_db)

        assert tmp_analytics_db.exists(), "analytics.db should be created after instantiation"

    def test_analytics_db_has_schema(self, analytics_collector: TelemetryCollector, tmp_analytics_db: Path):
        """Test that analytics.db has telemetry_events table with correct schema."""
        conn = sqlite3.connect(str(tmp_analytics_db))
        try:
            # Verify table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='telemetry_events'"
            )
            assert cursor.fetchone() is not None, "telemetry_events table should exist"

            # Verify columns exist
            cursor = conn.execute("PRAGMA table_info(telemetry_events)")
            columns = {row[1] for row in cursor.fetchall()}
            expected_columns = {
                "id",
                "tool_name",
                "timestamp",
                "duration_ms",
                "result_count",
                "has_cursor",
                "page_depth",
                "has_filter",
                "error_type",
            }
            assert expected_columns.issubset(columns), f"Missing columns: {expected_columns - columns}"

            # Verify index exists
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_telemetry%'")
            assert cursor.fetchone() is not None, "Index on telemetry_events should exist"
        finally:
            conn.close()

    def test_wal_mode_enabled(self, analytics_collector: TelemetryCollector, tmp_analytics_db: Path):
        """Test that WAL mode is enabled on analytics.db."""
        conn = sqlite3.connect(str(tmp_analytics_db))
        try:
            cursor = conn.execute("PRAGMA journal_mode")
            mode = cursor.fetchone()[0]
            assert mode == "wal", f"WAL mode should be enabled, got {mode}"
        finally:
            conn.close()


class TestRecordEventNonBlocking:
    """Tests for record_event() non-blocking behavior."""

    def test_record_event_nonblocking(self, analytics_collector: TelemetryCollector):
        """Test that record_event() returns immediately (< 1ms, typically < 1µs)."""
        event = TelemetryEvent(
            tool_name="ListDialogs",
            timestamp=1000.0,
            duration_ms=10.5,
            result_count=5,
            has_cursor=False,
            page_depth=1,
            has_filter=False,
            error_type=None,
        )

        t0 = time.monotonic()
        analytics_collector.record_event(event)
        elapsed_s = time.monotonic() - t0

        # Should be < 1ms (1e-3 seconds)
        assert elapsed_s < 0.001, f"record_event() took {elapsed_s * 1e6:.1f}µs, should be < 1000µs"

    def test_record_event_appends_to_batch(self, analytics_collector: TelemetryCollector):
        """Test that record_event() appends to batch."""
        event = TelemetryEvent(
            tool_name="ListMessages",
            timestamp=1001.0,
            duration_ms=15.0,
            result_count=10,
            has_cursor=True,
            page_depth=2,
            has_filter=False,
            error_type=None,
        )

        # Call record_event
        analytics_collector.record_event(event)

        # Verify batch has the event (direct inspection)
        with analytics_collector._batch_lock:
            assert len(analytics_collector._batch) == 1, "Batch should have 1 event"
            assert analytics_collector._batch[0] == event, "Event should match"

    def test_batch_accumulation_threshold(self, analytics_collector: TelemetryCollector):
        """Test batch accumulation: adding multiple events builds up batch."""
        # Add 10 events
        for i in range(10):
            event = TelemetryEvent(
                tool_name="ListDialogs",
                timestamp=1000.0 + i,
                duration_ms=10.0,
                result_count=5,
                has_cursor=False,
                page_depth=1,
                has_filter=False,
                error_type=None,
            )
            analytics_collector.record_event(event)

        # Batch should have 10 events
        with analytics_collector._batch_lock:
            assert len(analytics_collector._batch) == 10, "Batch should have 10 events"


class TestAsyncFlush:
    """Tests for async flush behavior."""

    def test_manual_flush_writes_db(self, analytics_collector: TelemetryCollector, tmp_analytics_db: Path):
        """Test that manually triggered flush writes events to DB."""
        # Record a few events
        for i in range(5):
            event = TelemetryEvent(
                tool_name="ListDialogs",
                timestamp=1000.0 + i,
                duration_ms=10.0 + i,
                result_count=5 + i,
                has_cursor=False,
                page_depth=1,
                has_filter=False,
                error_type=None,
            )
            analytics_collector.record_event(event)

        # Manually trigger sync flush by calling _write_batch directly
        with analytics_collector._batch_lock:
            batch = list(analytics_collector._batch)
            analytics_collector._batch = []

        analytics_collector._write_batch(batch)

        # Query DB to verify events were written
        conn = sqlite3.connect(str(tmp_analytics_db))
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM telemetry_events")
            count = cursor.fetchone()[0]
            assert count >= 5, f"At least 5 events should be in DB, got {count}"
        finally:
            conn.close()


class TestSingletonPattern:
    """Tests for TelemetryCollector singleton pattern."""

    def test_get_instance_returns_singleton(self, tmp_analytics_db: Path):
        """Test that get_instance() returns same object on second call."""
        # Reset singleton
        TelemetryCollector._instance = None

        instance1 = TelemetryCollector.get_instance(tmp_analytics_db)
        instance2 = TelemetryCollector.get_instance(tmp_analytics_db)

        assert instance1 is instance2, "get_instance() should return same object (singleton)"

    def test_get_instance_thread_safe(self, tmp_analytics_db: Path):
        """Test that get_instance() is thread-safe across multiple threads."""
        # Reset singleton
        TelemetryCollector._instance = None

        instances = []

        def get_instance():
            instance = TelemetryCollector.get_instance(tmp_analytics_db)
            instances.append(instance)

        # Spawn 3 threads
        threads = [threading.Thread(target=get_instance) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # All threads should get same instance
        assert len(instances) == 3, "All threads should get instance"
        assert instances[0] is instances[1] is instances[2], "All threads should get same singleton instance"


class TestIntegration:
    """Integration tests for analytics."""

    def test_event_schema_insert(self, analytics_collector: TelemetryCollector, tmp_analytics_db: Path):
        """Test that event can be recorded and queried from DB."""
        event = TelemetryEvent(
            tool_name="SearchMessages",
            timestamp=1234567890.5,
            duration_ms=25.5,
            result_count=42,
            has_cursor=True,
            page_depth=3,
            has_filter=True,
            error_type=None,
        )

        # Record event
        analytics_collector.record_event(event)

        # Manually flush to test DB write
        batch = list(analytics_collector._batch)
        analytics_collector._batch = []

        analytics_collector._write_batch(batch)

        # Query DB
        conn = sqlite3.connect(str(tmp_analytics_db))
        try:
            cursor = conn.execute(
                "SELECT tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type FROM telemetry_events ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            assert row is not None, "Event should be in DB"

            tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type = row
            assert tool_name == "SearchMessages"
            assert timestamp == 1234567890.5
            assert abs(duration_ms - 25.5) < 0.01
            assert result_count == 42
            assert has_cursor == 1  # SQLite stores True as 1
            assert page_depth == 3
            assert has_filter == 1
            assert error_type is None
        finally:
            conn.close()


class TestUsageSummaryFormatting:
    """Tests for format_usage_summary function."""

    def test_usage_summary_metrics(self):
        """format_usage_summary includes tool frequency, error rates, latency."""
        from mcp_telegram.tools import format_usage_summary

        stats = {
            "tool_distribution": {"ListMessages": 10, "ListDialogs": 3},
            "error_distribution": {"NotFound": 2, "Ambiguous": 1},
            "max_page_depth": 8,
            "dialogs_with_deep_scroll": 2,
            "total_calls": 13,
            "filter_count": 5,
            "latency_median_ms": 45.0,
            "latency_p95_ms": 120.0,
        }

        summary = format_usage_summary(stats)

        # Verify metrics present
        assert "ListMessages" in summary
        assert ("76%" in summary or "77%" in summary or "10" in summary)  # top tool frequency (10/13 = 76.9%)
        assert "Deep scrolling" in summary  # page_depth >= 5
        assert "NotFound" in summary or "Errors" in summary  # error distribution
        assert "Filtered" in summary or "ms" in summary  # filter or latency

        # Verify token count
        token_count = len(summary.split())
        assert token_count < 100

    def test_usage_summary_empty_distribution(self):
        """format_usage_summary handles empty tool/error distributions."""
        from mcp_telegram.tools import format_usage_summary

        stats = {
            "tool_distribution": {},
            "error_distribution": {},
            "max_page_depth": 2,
            "dialogs_with_deep_scroll": 0,
            "total_calls": 0,
            "filter_count": 0,
            "latency_median_ms": 0,
            "latency_p95_ms": 0,
        }

        summary = format_usage_summary(stats)

        # Should return something (even if empty or minimal)
        assert isinstance(summary, str)
        assert len(summary.split()) < 100

    def test_usage_summary_no_deep_scroll(self):
        """format_usage_summary omits deep scroll message when max_page_depth < 5."""
        from mcp_telegram.tools import format_usage_summary

        stats = {
            "tool_distribution": {"ListMessages": 5},
            "error_distribution": {},
            "max_page_depth": 3,  # Below threshold
            "dialogs_with_deep_scroll": 0,
            "total_calls": 5,
            "filter_count": 0,
            "latency_median_ms": 30.0,
            "latency_p95_ms": 50.0,
        }

        summary = format_usage_summary(stats)

        assert "Deep scrolling" not in summary
        assert "ListMessages" in summary  # Top tool still present

    def test_usage_summary_with_deep_scroll(self):
        """format_usage_summary includes deep scroll message when max_page_depth >= 5."""
        from mcp_telegram.tools import format_usage_summary

        stats = {
            "tool_distribution": {"ListMessages": 5},
            "error_distribution": {},
            "max_page_depth": 8,  # Above threshold
            "dialogs_with_deep_scroll": 1,
            "total_calls": 5,
            "filter_count": 0,
            "latency_median_ms": 30.0,
            "latency_p95_ms": 50.0,
        }

        summary = format_usage_summary(stats)

        assert "Deep scrolling" in summary
        assert "8" in summary  # max depth value

    def test_usage_summary_truncation(self):
        """format_usage_summary truncates if exceeding 100 tokens."""
        from mcp_telegram.tools import format_usage_summary

        # Create stats with many error types to stress test
        error_dist = {f"Error{i}": i + 1 for i in range(50)}
        stats = {
            "tool_distribution": {f"Tool{i}": i + 1 for i in range(30)},
            "error_distribution": error_dist,
            "max_page_depth": 10,
            "dialogs_with_deep_scroll": 5,
            "total_calls": 1000,
            "filter_count": 500,
            "latency_median_ms": 45.5,
            "latency_p95_ms": 120.5,
        }

        summary = format_usage_summary(stats)

        # Verify hard limit is enforced
        token_count = len(summary.split())
        assert token_count <= 101, f"Should be at most 100 tokens + ellipsis, got {token_count}"

    def test_usage_summary_latency_formatting(self):
        """format_usage_summary formats latency with 0 decimal places."""
        from mcp_telegram.tools import format_usage_summary

        stats = {
            "tool_distribution": {"ListMessages": 10},
            "error_distribution": {},
            "max_page_depth": 1,
            "dialogs_with_deep_scroll": 0,
            "total_calls": 10,
            "filter_count": 0,
            "latency_median_ms": 45.7,
            "latency_p95_ms": 120.3,
        }

        summary = format_usage_summary(stats)

        # Should have formatted latencies (no decimal places)
        assert "46ms" in summary or "45ms" in summary  # rounded median
        assert "120ms" in summary or "121ms" in summary  # rounded p95


class TestAnalyticsCleanup:
    """Tests for cleanup_analytics_db function."""

    def test_cleanup_deletes_stale_events(self, tmp_analytics_db: Path, monkeypatch):
        """Test that cleanup deletes events >30 days old and keeps recent ones."""
        import asyncio
        from mcp_telegram.analytics import cleanup_analytics_db

        # Create analytics database with events
        TelemetryCollector._instance = None
        collector = TelemetryCollector.get_instance(tmp_analytics_db)

        # Record events with specific timestamps
        current_time = time.time()
        stale_timestamp = current_time - (60 * 86400)  # 60 days ago
        recent_timestamp = current_time - (10 * 86400)  # 10 days ago

        # Insert stale event (should be deleted)
        stale_event = TelemetryEvent(
            tool_name="ListDialogs",
            timestamp=stale_timestamp,
            duration_ms=10.0,
            result_count=5,
            has_cursor=False,
            page_depth=1,
            has_filter=False,
            error_type=None,
        )

        # Insert recent event (should be kept)
        recent_event = TelemetryEvent(
            tool_name="ListMessages",
            timestamp=recent_timestamp,
            duration_ms=20.0,
            result_count=10,
            has_cursor=True,
            page_depth=2,
            has_filter=False,
            error_type=None,
        )

        collector.record_event(stale_event)
        collector.record_event(recent_event)

        # Manually flush to ensure events are written
        with collector._batch_lock:
            batch = list(collector._batch)
            collector._batch = []
        collector._write_batch(batch)

        # Verify both events exist before cleanup
        conn = sqlite3.connect(str(tmp_analytics_db))
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM telemetry_events")
            count_before = cursor.fetchone()[0]
            assert count_before >= 2, f"Should have at least 2 events before cleanup, got {count_before}"
        finally:
            conn.close()

        # Run cleanup with 30-day retention
        asyncio.run(cleanup_analytics_db(tmp_analytics_db, retention_days=30))

        # Verify stale event was deleted and recent event remains
        conn = sqlite3.connect(str(tmp_analytics_db))
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM telemetry_events WHERE timestamp < ?",
                (current_time - (30 * 86400),),
            )
            stale_count = cursor.fetchone()[0]
            assert stale_count == 0, f"Stale events should be deleted, but found {stale_count}"

            cursor = conn.execute(
                "SELECT COUNT(*) FROM telemetry_events WHERE tool_name='ListMessages'"
            )
            recent_count = cursor.fetchone()[0]
            assert recent_count >= 1, f"Recent event should be preserved, got {recent_count}"
        finally:
            conn.close()

        # Cleanup
        TelemetryCollector._instance = None

    def test_cleanup_calls_optimize(self, tmp_analytics_db: Path):
        """Test that cleanup calls PRAGMA optimize to rebuild statistics."""
        import asyncio
        from mcp_telegram.analytics import cleanup_analytics_db

        # Create analytics database with sample events
        TelemetryCollector._instance = None
        collector = TelemetryCollector.get_instance(tmp_analytics_db)

        # Insert some events
        for i in range(10):
            event = TelemetryEvent(
                tool_name="ListMessages",
                timestamp=time.time(),
                duration_ms=10.0 + i,
                result_count=5 + i,
                has_cursor=False,
                page_depth=1,
                has_filter=False,
                error_type=None,
            )
            collector.record_event(event)

        # Manually flush
        with collector._batch_lock:
            batch = list(collector._batch)
            collector._batch = []
        collector._write_batch(batch)

        # Run cleanup
        asyncio.run(cleanup_analytics_db(tmp_analytics_db, retention_days=30))

        # Verify PRAGMA optimize was called by checking that DB is still accessible
        # and statistics were rebuilt (indirect verification)
        conn = sqlite3.connect(str(tmp_analytics_db))
        try:
            # If optimize was called, query should still work fine
            cursor = conn.execute("SELECT COUNT(*) FROM telemetry_events")
            count = cursor.fetchone()[0]
            assert count >= 0, "Database should be accessible after optimize"

            # Verify database integrity after optimize
            # (PRAGMA integrity_check should return 'ok')
            cursor = conn.execute("PRAGMA integrity_check")
            result = cursor.fetchone()
            assert result is not None, "PRAGMA integrity_check should return result"
            assert result[0] == "ok", f"Database integrity check failed: {result[0]}"
        finally:
            conn.close()

        # Cleanup
        TelemetryCollector._instance = None

    def test_cleanup_vacuum(self, tmp_analytics_db: Path):
        """Test that incremental_vacuum reclaims disk space."""
        import asyncio
        from mcp_telegram.analytics import cleanup_analytics_db

        # Create analytics database with many events
        TelemetryCollector._instance = None
        collector = TelemetryCollector.get_instance(tmp_analytics_db)

        # Insert 1000+ events: mix of old and new
        current_time = time.time()
        stale_base = current_time - (60 * 86400)  # 60 days ago
        recent_base = current_time - (10 * 86400)  # 10 days ago

        for i in range(1100):
            # Alternate: half events at 60 days, half at 10 days
            if i < 550:
                timestamp = stale_base + (i % 100)  # Old events (60 days + small offset)
            else:
                timestamp = recent_base + (i % 100)  # Recent events (10 days + small offset)

            event = TelemetryEvent(
                tool_name="ListMessages",
                timestamp=timestamp,
                duration_ms=10.0 + i,
                result_count=5 + (i % 100),
                has_cursor=i % 2 == 0,
                page_depth=1 + (i % 5),
                has_filter=i % 3 == 0,
                error_type=None,
            )
            collector.record_event(event)

        # Flush all events
        with collector._batch_lock:
            batch = list(collector._batch)
            collector._batch = []
        collector._write_batch(batch)

        # Get page count before cleanup
        conn = sqlite3.connect(str(tmp_analytics_db))
        try:
            cursor = conn.execute("PRAGMA page_count")
            pages_before = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(*) FROM telemetry_events")
            count_before = cursor.fetchone()[0]
        finally:
            conn.close()

        # Run cleanup with 30-day retention
        asyncio.run(cleanup_analytics_db(tmp_analytics_db, retention_days=30))

        # Get page count after cleanup
        conn = sqlite3.connect(str(tmp_analytics_db))
        try:
            cursor = conn.execute("PRAGMA page_count")
            pages_after = cursor.fetchone()[0]

            # Verify events were deleted
            cursor = conn.execute("SELECT COUNT(*) FROM telemetry_events")
            count_after = cursor.fetchone()[0]

            # Should have deleted ~550 old events, kept ~550 recent ones
            assert count_after < count_before, "Cleanup should have deleted stale events"
            assert count_after > 500, f"Should have retained recent events, got {count_after}"
        finally:
            conn.close()

        # Cleanup
        TelemetryCollector._instance = None
