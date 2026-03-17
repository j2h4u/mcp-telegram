from __future__ import annotations

import logging
import sqlite3
import time

from xdg_base_dirs import xdg_state_home

from ..errors import (
    no_usage_data_text,
    usage_stats_db_missing_text,
    usage_stats_query_error_text,
)
from ._base import ToolArgs, ToolResult, _text_response, mcp_tool

logger = logging.getLogger(__name__)


class GetUsageStats(ToolArgs):
    """Get actionable usage statistics from telemetry (last 30 days)."""

    pass


@mcp_tool("secondary/helper")
async def get_usage_stats(args: GetUsageStats) -> ToolResult:

    # Get analytics DB path
    db_dir = xdg_state_home() / "mcp-telegram"
    db_path = db_dir / "analytics.db"

    # Query analytics DB (30-day window)
    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        since = int(time.time()) - 30 * 86400

        # Tool distribution
        tool_dist = dict(
            cursor.execute(
                "SELECT tool_name, COUNT(*) FROM telemetry_events WHERE timestamp >= ? GROUP BY tool_name ORDER BY COUNT(*) DESC",
                (since,),
            ).fetchall()
        )

        # Error distribution
        error_dist = dict(
            cursor.execute(
                "SELECT error_type, COUNT(*) FROM telemetry_events WHERE timestamp >= ? AND error_type IS NOT NULL GROUP BY error_type ORDER BY COUNT(*) DESC",
                (since,),
            ).fetchall()
        )

        # Page depth stats
        max_depth_result = cursor.execute(
            "SELECT MAX(page_depth) FROM telemetry_events WHERE timestamp >= ?",
            (since,),
        ).fetchone()
        max_depth = max_depth_result[0] if max_depth_result and max_depth_result[0] is not None else 0

        # Filter usage
        filter_count_result = cursor.execute(
            "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ? AND has_filter = 1",
            (since,),
        ).fetchone()
        filter_count = filter_count_result[0] if filter_count_result else 0

        # Total calls
        total_calls_result = cursor.execute(
            "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ?",
            (since,),
        ).fetchone()
        total_calls = total_calls_result[0] if total_calls_result else 0

        # Latency percentiles
        latencies = cursor.execute(
            "SELECT duration_ms FROM telemetry_events WHERE timestamp >= ? ORDER BY duration_ms",
            (since,),
        ).fetchall()

        # Compute percentiles
        latency_median_ms = 0
        latency_p95_ms = 0
        if latencies:
            sorted_latencies = [lat[0] for lat in latencies]
            latency_median_ms = sorted_latencies[len(sorted_latencies) // 2]
            p95_idx = int(len(sorted_latencies) * 0.95)
            latency_p95_ms = sorted_latencies[p95_idx] if p95_idx < len(sorted_latencies) else sorted_latencies[-1]

        from ..analytics import format_usage_summary
        summary = format_usage_summary(
            {
                "tool_distribution": tool_dist,
                "error_distribution": error_dist,
                "max_page_depth": max_depth,
                "dialogs_with_deep_scroll": 0,
                "total_calls": total_calls,
                "filter_count": filter_count,
                "latency_median_ms": latency_median_ms,
                "latency_p95_ms": latency_p95_ms,
            }
        )

        return ToolResult(content=_text_response(summary if summary else no_usage_data_text()))

    except sqlite3.OperationalError as exc:
        # Table doesn't exist or DB not initialized yet
        if "no such table" in str(exc):
            return ToolResult(content=_text_response(usage_stats_db_missing_text()))
        logger.error("GetUsageStats query failed: %s", exc)
        return ToolResult(content=_text_response(usage_stats_query_error_text(type(exc).__name__)))
    except Exception as exc:
        logger.error("GetUsageStats query failed: %s", exc)
        return ToolResult(content=_text_response(usage_stats_query_error_text(type(exc).__name__)))
    finally:
        if conn is not None:
            conn.close()
