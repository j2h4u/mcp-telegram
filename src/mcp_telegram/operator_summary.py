"""Single-pass operator report from durable runtime telemetry."""

# pyright: reportAny=false

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .sync_db import open_sync_db_reader

_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}
_MIN_DURATION_LENGTH = 2
_MILLISECONDS_PER_SECOND = 1000


def parse_since(value: str) -> int:
    """Parse a compact operator window such as 30m, 15h, or 2d."""
    normalized = value.strip().lower()
    if len(normalized) < _MIN_DURATION_LENGTH or normalized[-1] not in _DURATION_UNITS:
        raise ValueError("--since must use m, h, or d, for example: 30m, 15h, 2d")
    amount_text = normalized[:-1]
    if not amount_text.isdecimal() or int(amount_text) <= 0:
        raise ValueError("--since must be a positive duration, for example: 30m, 15h, 2d")
    return int(amount_text) * _DURATION_UNITS[normalized[-1]]


def _utc(seconds: float | int) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1)]


def _ms(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= _MILLISECONDS_PER_SECOND:
        return f"{value / _MILLISECONDS_PER_SECOND:.1f}s"
    return f"{value:.0f}ms"


@dataclass(frozen=True, slots=True)
class OperatorSummary:
    """Rendered report plus whether the requested telemetry window was complete."""

    text: str
    window_complete: bool


def build_operator_summary(  # noqa: PLR0912, PLR0914, PLR0915
    db_path: Path, *, since_seconds: int, now: float | None = None
) -> OperatorSummary:
    """Build a content-free operational report from one read-only DB connection."""
    effective_now = time.time() if now is None else now
    since_ms = int((effective_now - since_seconds) * 1000)
    conn = open_sync_db_reader(db_path)
    conn.row_factory = lambda cursor, row: {column[0]: row[index] for index, column in enumerate(cursor.description)}
    try:
        observations = conn.execute(
            "SELECT * FROM runtime_observations WHERE observed_at_ms>=? ORDER BY observed_at_ms,id",
            (since_ms,),
        ).fetchall()
        history_row = conn.execute(
            "SELECT value FROM daemon_state WHERE key='runtime_observations_history_started_at_ms'"
        ).fetchone()
        dialog_rows = conn.execute("SELECT status,COUNT(*) count FROM synced_dialogs GROUP BY status").fetchall()
    finally:
        conn.close()

    history_started_ms = int(history_row["value"]) if history_row is not None else None
    window_complete = history_started_ms is not None and history_started_ms <= since_ms
    starts = [row for row in observations if row["kind"] == "runtime.started"]
    stops = [row for row in observations if row["kind"] == "runtime.stopped"]
    last_observed_ms = max((int(row["observed_at_ms"]) for row in observations), default=None)

    mcp_rows = [row for row in observations if row["kind"] == "mcp.call"]
    mcp_errors: dict[str, int] = defaultdict(int)
    mcp_latencies = [float(row["duration_ms"]) for row in mcp_rows if row["duration_ms"] is not None]
    slow_mcp = sorted(
        (
            row
            for row in mcp_rows
            if row["duration_ms"] is not None and float(row["duration_ms"]) >= _MILLISECONDS_PER_SECOND
        ),
        key=lambda row: float(row["duration_ms"]),
        reverse=True,
    )
    for row in mcp_rows:
        reason = row["reason_code"] or row["error_type"]
        if reason:
            mcp_errors[str(reason)] += 1

    rpc_rows = [row for row in observations if row["kind"] == "telegram.rpc_admission"]
    rpc_cancelled = sum(1 for row in rpc_rows if row["outcome"] == "cancelled")
    rpc_by_source: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"windows": 0, "dispatched": 0, "worst_wait_ms": 0.0, "max_queue": 0}
    )
    for row in rpc_rows:
        if row["outcome"] != "summary":
            continue
        payload = json.loads(row["payload_json"] or "{}")
        source = str(payload.get("source") or "unknown")
        summary = rpc_by_source[source]
        summary["windows"] = int(summary["windows"]) + 1
        summary["dispatched"] = int(summary["dispatched"]) + int(payload.get("dispatched_count") or 0)
        summary["worst_wait_ms"] = max(float(summary["worst_wait_ms"]), float(payload.get("max_wait_ms") or 0))
        summary["max_queue"] = max(int(summary["max_queue"]), int(payload.get("queue_depth_max") or 0))

    failed_runtime = [
        row for row in observations if row["kind"] in {"runtime.task_failed", "runtime.catch_up_request_failed"}
    ]
    sync_counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in observations:
        if str(row["kind"]).startswith(("sync.", "telegram.inbox_read")):
            sync_counts[(str(row["kind"]), str(row["outcome"] or "observed"))] += 1

    lines = [
        f"mcp-telegram operational summary: {_utc(effective_now - since_seconds)} .. {_utc(effective_now)}",
        (
            f"Telemetry: {len(observations)} events; window={'complete' if window_complete else 'partial'}; "
            f"last event={_utc(last_observed_ms / 1000) if last_observed_ms is not None else 'none'}"
        ),
        f"Runtime: starts={len(starts)}, clean stops={len(stops)}, task failures={len(failed_runtime)}",
    ]
    if starts:
        latest_start = starts[-1]
        latest_instance = latest_start["runtime_instance_id"]
        matching_stop = any(row["runtime_instance_id"] == latest_instance for row in stops)
        lines.append(
            f"Current recorded instance: started {_utc(int(latest_start['observed_at_ms']) / 1000)}; "
            f"stop recorded={'yes' if matching_stop else 'no'}"
        )

    status_text = ", ".join(f"{row['status']}={row['count']}" for row in dialog_rows) or "none"
    lines.append(f"Dialog state: {status_text}")
    lines.append(
        f"MCP: calls={len(mcp_rows)}, errors={sum(mcp_errors.values())}"
        f"{f' ({", ".join(f"{key}={value}" for key, value in sorted(mcp_errors.items()))})' if mcp_errors else ''}; "
        f"latency median={_ms(_percentile(mcp_latencies, 0.5))}, "
        f"p95={_ms(_percentile(mcp_latencies, 0.95))}, max={_ms(max(mcp_latencies) if mcp_latencies else None)}"
    )
    if slow_mcp:
        lines.append(
            "Slow MCP calls: "
            + ", ".join(f"{row['tool_name']}={_ms(float(row['duration_ms']))}" for row in slow_mcp[:5])
        )
    lines.append(
        f"Telegram RPC admission: summaries={sum(1 for row in rpc_rows if row['outcome'] == 'summary')}, cancelled={rpc_cancelled}"
    )
    for source, summary in sorted(
        rpc_by_source.items(), key=lambda item: float(item[1]["worst_wait_ms"]), reverse=True
    ):
        lines.append(
            f"  {source}: dispatched={summary['dispatched']}, worst wait={_ms(float(summary['worst_wait_ms']))}, "
            f"max queue={summary['max_queue']}"
        )
    if sync_counts:
        lines.append("Sync observations:")
        for (kind, outcome), count in sorted(sync_counts.items()):
            lines.append(f"  {kind} {outcome}={count}")

    notable = [
        row
        for row in observations
        if row["reason_code"]
        or row["error_type"]
        or row["kind"] in {"runtime.task_failed", "runtime.catch_up_request_failed"}
    ]
    if notable:
        lines.append("Recent notable events:")
        for row in reversed(notable[-20:]):
            detail = row["reason_code"] or row["error_type"] or row["outcome"] or "observed"
            subject = row["tool_name"] or row["kind"]
            lines.append(f"  {_utc(int(row['observed_at_ms']) / 1000)} {subject}: {detail}")
    else:
        lines.append("Recent notable events: none")

    return OperatorSummary(text="\n".join(lines), window_complete=window_complete)
