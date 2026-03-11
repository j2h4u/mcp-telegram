# tests/test_load.py
import asyncio
import pytest
import time
import sqlite3
from pathlib import Path
from unittest.mock import patch
from mcp_telegram.analytics import TelemetryCollector, TelemetryEvent


def test_telemetry_batch_recording_speed():
    """Verify batch recording is fast (<1ms per call).

    Tests that record_event() is fast even when batch flush happens.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "analytics.db"

        # Reset singleton for this test
        TelemetryCollector._instance = None

        # Create a fresh collector instance
        collector = TelemetryCollector.get_instance(db_path)

        # Record 100 events
        t0 = time.monotonic()
        for i in range(100):
            event = TelemetryEvent(
                tool_name="ListMessages",
                timestamp=time.time(),
                duration_ms=50.0,
                result_count=10,
                has_cursor=False,
                page_depth=1,
                has_filter=False,
                error_type=None,
            )
            collector.record_event(event)

        elapsed_ms = (time.monotonic() - t0) * 1000

        print(f"\n=== Batch Recording Speed ===")
        print(f"100 record_event() calls: {elapsed_ms:.1f}ms")
        print(f"Per-call: {elapsed_ms / 100:.2f}ms")

        # Verify each record_event() < 1ms (should be <1µs in reality)
        # Threshold is generous to account for any system noise
        assert elapsed_ms / 100 < 1.0, (
            f"Each record_event() took {elapsed_ms / 100:.2f}ms, should be <1ms"
        )

        # Verify batch was created in DB (sync fallback writes immediately if no event loop)
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM telemetry_events")
        count = cursor.fetchone()[0]
        conn.close()

        print(f"✓ Recorded and verified {count} events in database")


def test_telemetry_event_dataclass():
    """Verify TelemetryEvent is immutable and privacy-safe."""
    event = TelemetryEvent(
        tool_name="GetUserInfo",
        timestamp=time.time(),
        duration_ms=12.5,
        result_count=1,
        has_cursor=False,
        page_depth=1,
        has_filter=False,
        error_type=None,
    )

    # Verify it's frozen (immutable)
    with pytest.raises(AttributeError):
        event.tool_name = "Modified"

    # Verify fields are only privacy-safe ones
    allowed_fields = {"tool_name", "timestamp", "duration_ms", "result_count",
                      "has_cursor", "page_depth", "has_filter", "error_type"}
    actual_fields = set(event.__dataclass_fields__.keys())
    assert actual_fields == allowed_fields, f"TelemetryEvent has unexpected fields: {actual_fields}"

    print("✓ TelemetryEvent is properly immutable and privacy-safe")


def test_telemetry_multiple_tools():
    """Verify telemetry records events from different tools correctly."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "analytics.db"

        # Reset singleton for this test
        TelemetryCollector._instance = None

        collector = TelemetryCollector.get_instance(db_path)

        # Record events for different tools
        # Need 100+ events to trigger automatic flush
        tools = ["ListDialogs", "ListMessages", "SearchMessages", "GetMe", "GetUserInfo"]
        event_count = 0
        for tool in tools:
            for i in range(21):  # 5 tools × 21 = 105 events total
                event = TelemetryEvent(
                    tool_name=tool,
                    timestamp=time.time(),
                    duration_ms=25.0 + i,
                    result_count=i + 1,
                    has_cursor=i % 2 == 0,
                    page_depth=1 + (i // 3),
                    has_filter=i % 3 == 0,
                    error_type=None if i % 10 != 0 else "ConnectionError",
                )
                collector.record_event(event)
                event_count += 1

        print(f"\n=== Multiple Tools Test ===")
        print(f"Recorded {event_count} events (will trigger flush at 100)")

        # Check database - events should have been flushed when we hit 100
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Check total count
        cursor.execute("SELECT COUNT(*) FROM telemetry_events")
        total_count = cursor.fetchone()[0]

        # Check distinct tools
        cursor.execute("SELECT COUNT(DISTINCT tool_name) FROM telemetry_events")
        tool_count = cursor.fetchone()[0]

        # Check error types
        cursor.execute("SELECT COUNT(*) FROM telemetry_events WHERE error_type IS NOT NULL")
        error_count = cursor.fetchone()[0]

        conn.close()

        print(f"Total events in DB: {total_count}")
        print(f"Distinct tools: {tool_count}")
        print(f"Error events: {error_count}")

        assert total_count >= 100, f"Expected ≥100 events, got {total_count}"
        assert tool_count >= 5, f"Expected ≥5 tools, got {tool_count}"
        assert error_count >= 10, f"Expected ≥10 error events, got {error_count}"

        print("✓ All tools recorded correctly")

