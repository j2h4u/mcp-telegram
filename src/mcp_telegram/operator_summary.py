"""Single-pass operator report from durable runtime telemetry."""

# pyright: reportAny=false

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .daemon import read_operator_summary_snapshot

_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}
_MIN_DURATION_LENGTH = 2
_MILLISECONDS_PER_SECOND = 1000
_SLOW_MCP_MS = 1000
_RUNTIME_FAILURE_KINDS = {"runtime.task_failed", "runtime.catch_up_request_failed"}
_SNAPSHOT_METADATA_INDEX = 3

Observation = dict[str, object]


@dataclass(frozen=True, slots=True)
class OperatorSummary:
    """Rendered report plus whether the requested telemetry window was complete."""

    text: str
    window_complete: bool


@dataclass(frozen=True, slots=True)
class SummarySnapshot:
    observations: list[Observation]
    history_started_ms: int | None
    dialog_counts: list[tuple[str, int]]
    coverage_markers: dict[str, object]


@dataclass(frozen=True, slots=True)
class McpSummary:
    calls: int
    errors: dict[str, int]
    latencies: list[float]
    slow_calls: list[Observation]


@dataclass(frozen=True, slots=True)
class RpcSourceSummary:
    dispatched: int = 0
    worst_wait_ms: float = 0
    max_queue: int = 0


@dataclass(frozen=True, slots=True)
class DemandSummary:
    """Demand units and actual Telegram attempts kept as separate measures."""

    counts: dict[str, int]
    actual_attempts: int
    oldest_overdue_seconds: float | None
    oldest_queue_age_seconds: float | None
    selection_matches: int
    selection_mismatches: int
    reasons: dict[str, int]
    by_kind: dict[tuple[str, str | None], dict[str, int]]


def parse_since(value: str) -> int:
    """Parse a compact operator window such as 30m, 24h, or 2d."""
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


def _as_float(value: object) -> float:
    return float(cast(float | int | str, value))


def _as_int(value: object) -> int:
    return int(cast(float | int | str, value))


def _load_snapshot(db_path: Path, since_ms: int) -> SummarySnapshot:
    snapshot_result = cast(tuple[object, ...], read_operator_summary_snapshot(db_path, since_ms))
    observations = cast(list[Observation], snapshot_result[0])
    history_row = cast(dict[str, object] | None, snapshot_result[1])
    dialog_rows = cast(list[dict[str, object]], snapshot_result[2])
    coverage_markers = (
        dict(cast(Mapping[str, object], snapshot_result[_SNAPSHOT_METADATA_INDEX]))
        if len(snapshot_result) > _SNAPSHOT_METADATA_INDEX
        else {}
    )
    history_started_ms = _as_int(history_row["value"]) if history_row is not None else None
    dialog_counts = [(str(row["status"]), _as_int(row["count"])) for row in dialog_rows]
    return SummarySnapshot(
        observations=list(observations),
        history_started_ms=history_started_ms,
        dialog_counts=dialog_counts,
        coverage_markers=coverage_markers,
    )


def _rows_for_kind(observations: list[Observation], kind: str) -> list[Observation]:
    return [row for row in observations if row["kind"] == kind]


def _rows_for_outcome(observations: list[Observation], outcome: str) -> list[Observation]:
    return [row for row in observations if row["outcome"] == outcome]


def _duration_values(rows: list[Observation]) -> list[float]:
    return [_as_float(row["duration_ms"]) for row in rows if row["duration_ms"] is not None]


def _error_counts(rows: list[Observation]) -> dict[str, int]:
    errors: dict[str, int] = defaultdict(int)
    for row in rows:
        reason = row["reason_code"] or row["error_type"]
        if reason:
            errors[str(reason)] += 1
    return dict(errors)


def _slow_calls(rows: list[Observation]) -> list[Observation]:
    return sorted(
        (row for row in rows if row["duration_ms"] is not None and _as_float(row["duration_ms"]) >= _SLOW_MCP_MS),
        key=lambda row: _as_float(row["duration_ms"]),
        reverse=True,
    )


def _mcp_summary(observations: list[Observation]) -> McpSummary:
    rows = _rows_for_kind(observations, "mcp.call")
    return McpSummary(
        calls=len(rows),
        errors=_error_counts(rows),
        latencies=_duration_values(rows),
        slow_calls=_slow_calls(rows),
    )


def _rpc_summary(observations: list[Observation]) -> tuple[int, int, dict[str, RpcSourceSummary]]:
    rows = _rows_for_kind(observations, "telegram.rpc_admission")
    summaries = _rows_for_outcome(rows, "summary")
    sources: dict[str, RpcSourceSummary] = {}
    for row in summaries:
        payload = json.loads(str(row["payload_json"] or "{}"))
        source = str(payload.get("source") or "unknown")
        previous = sources.get(source, RpcSourceSummary())
        sources[source] = RpcSourceSummary(
            dispatched=previous.dispatched + _as_int(payload.get("dispatched_count") or 0),
            worst_wait_ms=max(previous.worst_wait_ms, float(payload.get("max_wait_ms") or 0)),
            max_queue=max(previous.max_queue, _as_int(payload.get("queue_depth_max") or 0)),
        )
    cancelled = sum(1 for row in rows if row["outcome"] == "cancelled")
    return len(summaries), cancelled, sources


def _demand_summary(observations: list[Observation]) -> DemandSummary:  # noqa: PLR0914
    rows = _rows_for_kind(observations, "telegram.demand")
    counts: dict[str, int] = defaultdict(int)
    reasons: dict[str, int] = defaultdict(int)
    by_kind: dict[tuple[str, str | None], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    actual_attempts = 0
    oldest_overdue_seconds: float | None = None
    oldest_queue_age_seconds: float | None = None
    selection_matches = 0
    selection_mismatches = 0
    for row in rows:
        outcome = str(row["outcome"] or "observed")
        payload = json.loads(str(row["payload_json"] or "{}"))
        units = _as_int(payload.get("demand_units") or 0)
        counts[outcome] += units
        demand_kind = str(payload.get("demand_kind") or "unknown")
        acquisition_kind = payload.get("acquisition_kind")
        acquisition = None if acquisition_kind is None else str(acquisition_kind)
        by_kind[(demand_kind, acquisition)][outcome] += units
        actual_attempts += _as_int(payload.get("actual_attempts") or 0)
        oldest_overdue_seconds = _max_payload_float(oldest_overdue_seconds, payload, "oldest_overdue_seconds")
        oldest_queue_age_seconds = _max_payload_float(oldest_queue_age_seconds, payload, "queue_age_seconds")
        matches, mismatches = _selection_counts(outcome, payload, units)
        selection_matches += matches
        selection_mismatches += mismatches
        _add_demand_reason(reasons, row["reason_code"], units)
    return DemandSummary(
        counts=dict(counts),
        actual_attempts=actual_attempts,
        oldest_overdue_seconds=oldest_overdue_seconds,
        oldest_queue_age_seconds=oldest_queue_age_seconds,
        selection_matches=selection_matches,
        selection_mismatches=selection_mismatches,
        reasons=dict(reasons),
        by_kind={key: dict(value) for key, value in by_kind.items()},
    )


def _max_payload_float(current: float | None, payload: Mapping[str, object], key: str) -> float | None:
    value = payload.get(key)
    return current if value is None else max(current or 0.0, _as_float(value))


def _selection_counts(outcome: str, payload: Mapping[str, object], units: int) -> tuple[int, int]:
    if outcome != "predicted_selection":
        return 0, 0
    match = payload.get("selection_match")
    if match is True:
        return units, 0
    if match is False:
        return 0, units
    return 0, 0


def _add_demand_reason(reasons: dict[str, int], reason: object, units: int) -> None:
    if reason:
        reasons[str(reason)] += units or 1


def _sync_counts(observations: list[Observation]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in observations:
        kind = str(row["kind"])
        if kind.startswith(("sync.", "telegram.inbox_read")):
            counts[(kind, str(row["outcome"] or "observed"))] += 1
    return dict(counts)


def _runtime_lines(observations: list[Observation]) -> list[str]:
    starts = [row for row in observations if row["kind"] == "runtime.started"]
    stops = [row for row in observations if row["kind"] == "runtime.stopped"]
    failures = [row for row in observations if row["kind"] in _RUNTIME_FAILURE_KINDS]
    lines = [f"Runtime: starts={len(starts)}, clean stops={len(stops)}, task failures={len(failures)}"]
    if not starts:
        return lines
    latest = starts[-1]
    stopped = any(row["runtime_instance_id"] == latest["runtime_instance_id"] for row in stops)
    lines.append(
        f"Current recorded instance: started {_utc(_as_int(latest['observed_at_ms']) / 1000)}; "
        f"stop recorded={'yes' if stopped else 'no'}"
    )
    return lines


def _mcp_lines(summary: McpSummary) -> list[str]:
    error_detail = ", ".join(f"{key}={value}" for key, value in sorted(summary.errors.items()))
    error_suffix = f" ({error_detail})" if error_detail else ""
    latencies = summary.latencies
    lines = [
        (
            f"MCP: calls={summary.calls}, errors={sum(summary.errors.values())}{error_suffix}; "
            f"latency median={_ms(_percentile(latencies, 0.5))}, p95={_ms(_percentile(latencies, 0.95))}, "
            f"max={_ms(max(latencies) if latencies else None)}"
        )
    ]
    if summary.slow_calls:
        lines.append(
            "Slow MCP calls: "
            + ", ".join(f"{row['tool_name']}={_ms(_as_float(row['duration_ms']))}" for row in summary.slow_calls[:5])
        )
    return lines


def _rpc_lines(summary_count: int, cancelled: int, sources: dict[str, RpcSourceSummary]) -> list[str]:
    actual_attempts = sum(summary.dispatched for summary in sources.values())
    lines = [
        (f"Telegram RPC admission: summaries={summary_count}, cancelled={cancelled}, actual attempts={actual_attempts}")
    ]
    ordered = sorted(sources.items(), key=lambda item: item[1].worst_wait_ms, reverse=True)
    if not ordered:
        return [*lines, "  none"]
    lines.extend(
        f"  {source}: dispatched={summary.dispatched}, worst wait={_ms(summary.worst_wait_ms)}, "
        f"max queue={summary.max_queue}"
        for source, summary in ordered
    )
    return lines


def _demand_lines(summary: DemandSummary) -> list[str]:
    if not summary.counts and summary.actual_attempts == 0:
        return []
    labels = (
        "offered",
        "locally_satisfied",
        "ready",
        "coalesced_wakeup",
        "predicted_selection",
        "completed",
        "deferred",
        "failed",
    )
    fields = _demand_fields(summary, labels)
    lines = ["Demand: " + ", ".join(fields)]
    lines.extend(_demand_kind_lines(summary, labels))
    return lines


def _demand_fields(summary: DemandSummary, labels: tuple[str, ...]) -> list[str]:
    fields = [f"{label.replace('_', ' ')}={summary.counts[label]}" for label in labels if summary.counts.get(label)]
    fields.append(f"actual attempts={summary.actual_attempts}")
    if summary.selection_matches or summary.selection_mismatches:
        fields.append(f"selection matches={summary.selection_matches}")
        fields.append(f"mismatches={summary.selection_mismatches}")
    if summary.oldest_queue_age_seconds is not None:
        fields.append(f"oldest queue age={_ms(summary.oldest_queue_age_seconds * _MILLISECONDS_PER_SECOND)}")
    if summary.oldest_overdue_seconds is not None:
        fields.append(f"oldest overdue={_ms(summary.oldest_overdue_seconds * _MILLISECONDS_PER_SECOND)}")
    if summary.reasons:
        fields.append("reasons=" + ",".join(f"{key}={value}" for key, value in sorted(summary.reasons.items())))
    return fields


def _demand_kind_lines(summary: DemandSummary, labels: tuple[str, ...]) -> list[str]:
    lines: list[str] = []
    for (demand_kind, acquisition_kind), counts in sorted(summary.by_kind.items()):
        detail = ", ".join(f"{label.replace('_', ' ')}={counts[label]}" for label in labels if counts.get(label))
        if detail:
            suffix = f"/{acquisition_kind}" if acquisition_kind else ""
            lines.append(f"  {demand_kind}{suffix}: {detail}")
    return lines


def _coverage_complete(snapshot: SummarySnapshot, since_ms: int) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if snapshot.history_started_ms is None or snapshot.history_started_ms > since_ms:
        reasons.append("history boundary")
    markers = snapshot.coverage_markers
    cap_ms = markers.get("last_cap_truncation_ms")
    if cap_ms is not None and _as_int(cap_ms) >= since_ms:
        reasons.append("retention cap truncation")
    gap_ms = markers.get("telemetry_gap_ms")
    if gap_ms is not None and _as_int(gap_ms) >= since_ms:
        reasons.append("telemetry gap")
    if markers.get("loss_observed") or markers.get("queue_full_drops") or markers.get("writer_failures"):
        reasons.append("telemetry loss")
    return not reasons, reasons


def _is_notable(row: Observation) -> bool:
    return bool(row["reason_code"] or row["error_type"] or row["kind"] in _RUNTIME_FAILURE_KINDS)


def _notable_detail(row: Observation) -> object:
    for field in ("reason_code", "error_type", "outcome"):
        value = row[field]
        if value:
            return value
    return "observed"


def _notable_lines(observations: list[Observation]) -> list[str]:
    notable = [row for row in observations if _is_notable(row)]
    if not notable:
        return ["Recent notable events: none"]
    lines = ["Recent notable events:"]
    for row in reversed(notable[-20:]):
        detail = _notable_detail(row)
        subject = row["tool_name"] or row["kind"]
        lines.append(f"  {_utc(_as_int(row['observed_at_ms']) / 1000)} {subject}: {detail}")
    return lines


def build_operator_summary(  # noqa: PLR0914
    db_path: Path, *, since_seconds: int, now: float | None = None
) -> OperatorSummary:
    """Build a content-free operational report from one read-only DB connection."""
    effective_now = time.time() if now is None else now
    since_ms = int((effective_now - since_seconds) * 1000)
    snapshot = _load_snapshot(db_path, since_ms)
    observations = snapshot.observations
    window_complete, coverage_reasons = _coverage_complete(snapshot, since_ms)
    last_ms = max((_as_int(row["observed_at_ms"]) for row in observations), default=None)
    last_event = _utc(last_ms / 1000) if last_ms is not None else "none"
    status_text = ", ".join(f"{status}={count}" for status, count in snapshot.dialog_counts) or "none"
    rpc_count, rpc_cancelled, rpc_sources = _rpc_summary(observations)
    demand_summary = _demand_summary(observations)
    coverage_text = "complete" if window_complete else "partial/unreliable"
    if coverage_reasons:
        coverage_text += " (" + ", ".join(coverage_reasons) + ")"

    lines = [
        f"mcp-telegram operational summary: {_utc(effective_now - since_seconds)} .. {_utc(effective_now)}",
        f"Telemetry: {len(observations)} events; window={coverage_text}; last event={last_event}",
        *_runtime_lines(observations),
        f"Dialog state: {status_text}",
        *_mcp_lines(_mcp_summary(observations)),
        *_rpc_lines(rpc_count, rpc_cancelled, rpc_sources),
        *_demand_lines(demand_summary),
    ]
    sync_counts = _sync_counts(observations)
    lines.append("Sync observations:")
    if sync_counts:
        lines.extend(f"  {kind} {outcome}={count}" for (kind, outcome), count in sorted(sync_counts.items()))
    else:
        lines.append("  none")
    lines.extend(_notable_lines(observations))
    return OperatorSummary(text="\n".join(lines), window_complete=window_complete)
