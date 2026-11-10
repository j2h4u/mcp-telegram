"""Bounded hydration queue observations and conditional clearance estimates."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import cast

from .hydration_queue import HydrationQueueRepository

KINDS = ("rich_composition", "media_metadata", "transcription")
MAX_GAP_MS = 15 * 60 * 1000
MIN_HISTORY_MS = 30 * 60 * 1000


def queue_snapshots(conn: sqlite3.Connection, now: int, *, paused: bool) -> list[dict[str, object]]:
    groups = {group.kind: group for group in HydrationQueueRepository(conn).snapshot(now)}
    row = cast(
        tuple[str] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key='rich_composition_hydration_cursor'").fetchone(),
    )
    scan_complete = cast(dict[str, object], json.loads(row[0])).get("done") is True if row else False
    result: list[dict[str, object]] = []
    for kind in KINDS:
        group = groups.get(kind)
        payload: dict[str, object] = {"kind": kind, "paused": paused}
        for field in ("active", "ready", "foreground", "backfill", "terminal"):
            payload[field] = getattr(group, field) if group else 0
        if kind == "rich_composition":
            payload["scan_complete"] = scan_complete
        result.append(payload)
    return result


def progress_line(payload: dict[str, object], estimate: str | None = None) -> str:
    counts = ", ".join(
        f"{field}={payload[field]}" for field in ("active", "ready", "foreground", "backfill", "terminal")
    )
    return f"{payload['kind']}: jobs {counts}" + (f"; queue-clearance ETA {estimate}" if estimate else "")


def _sample_problem(row: dict[str, object], boundary_ms: int | None) -> str | None:
    timestamp = int(cast(int, row["observed_at_ms"]))
    payload = cast(dict[str, object], row["payload"])
    if boundary_ms is not None and timestamp <= boundary_ms:
        return "telemetry loss or retention boundary"
    if payload.get("paused"):
        return "account protection pause"
    if payload.get("kind") == "rich_composition" and payload.get("scan_complete") is not True:
        return "historical scan incomplete"
    return None


def _valid_suffix(rows: list[dict[str, object]], boundary_ms: int | None) -> tuple[dict[str, object], str]:
    latest = rows[-1]
    first = latest
    for row in reversed(rows[:-1]):
        timestamp = int(cast(int, row["observed_at_ms"]))
        newer = int(cast(int, first["observed_at_ms"]))
        if timestamp >= newer or newer - timestamp > MAX_GAP_MS:
            return first, "sample gap or clock reversal"
        if row["runtime_instance_id"] != latest["runtime_instance_id"]:
            return first, "runtime restart"
        problem = _sample_problem(row, boundary_ms)
        if problem:
            return first, problem
        first = row
    return first, "insufficient history"


def clearance_estimate(rows: list[dict[str, object]], now_ms: int, boundary_ms: int | None) -> str:
    if not rows:
        return "unavailable (no samples)"
    latest = rows[-1]
    last_ms = int(cast(int, latest["observed_at_ms"]))
    if last_ms > now_ms or now_ms - last_ms > MAX_GAP_MS:
        return "unavailable (stale or future sample)"
    problem = _sample_problem(latest, boundary_ms)
    if problem:
        return f"unavailable ({problem})"
    first, reason = _valid_suffix(rows, boundary_ms)
    elapsed = last_ms - int(cast(int, first["observed_at_ms"]))
    if elapsed < MIN_HISTORY_MS:
        return f"unavailable ({reason})"
    payload = cast(dict[str, object], latest["payload"])
    active = int(cast(int, payload["active"]))
    initial = int(cast(int, cast(dict[str, object], first["payload"])["active"]))
    drain = initial - active
    if drain <= 0:
        return "unavailable (queue growing or flat)"
    remaining_ms = active * elapsed / drain
    finish = datetime.fromtimestamp((last_ms + remaining_ms) / 1000, tz=UTC)
    return (
        f"{finish:%Y-%m-%d %H:%M:%SZ}, {remaining_ms / 3600000:.1f}h remaining; "
        f"conditional on net drain {drain * 3600000 / elapsed:.1f} jobs/h over {elapsed / 60000:.0f}min; "
        f"sample age {(now_ms - last_ms) / 60000:.1f}min"
    )
