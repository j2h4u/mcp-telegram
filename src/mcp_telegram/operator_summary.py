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
from .request_timing import (
    TIMING_NESTED_LEAF_PHASES,
    TIMING_NESTED_PARENTS,
    TIMING_ROUTES,
    TIMING_SERVED_SOURCES,
    TIMING_TOP_LEVEL_PHASES,
    attribution_from_counts,
    is_valid_request_id,
)

_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}
_MIN_DURATION_LENGTH = 2
_MILLISECONDS_PER_SECOND = 1000
_DEFAULT_SLOW_MCP_MS = 1_000.0
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
    oldest_queue_age_seconds: float | None
    greatest_freshness_debt_seconds: float | None
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


def _slow_calls(rows: list[Observation], *, slow_mcp_ms: float) -> list[Observation]:
    return sorted(
        (row for row in rows if row["duration_ms"] is not None and _as_float(row["duration_ms"]) >= slow_mcp_ms),
        key=lambda row: _as_float(row["duration_ms"]),
        reverse=True,
    )


def _mcp_summary(observations: list[Observation], *, slow_mcp_ms: float) -> McpSummary:
    rows = _rows_for_kind(observations, "mcp.call")
    timings: dict[object, list[Observation]] = defaultdict(list)
    for timing in _rows_for_kind(observations, "daemon.request_timing"):
        operation_id = timing.get("operation_id")
        if operation_id:
            timings[operation_id].append(timing)
    slow_calls = []
    for row in _slow_calls(rows, slow_mcp_ms=slow_mcp_ms):
        enriched = dict(row)
        matching_timing_rows = timings.get(row.get("operation_id"), [])
        timing_rows = [timing for timing in matching_timing_rows if _timing_duration(timing) is not None]
        if timing_rows:
            enriched["_timing"] = max(
                timing_rows,
                key=lambda timing: (_timing_duration(timing) or 0.0, str(timing.get("payload_json", ""))),
            )
        if matching_timing_rows:
            enriched["_timing_total_count"] = len(matching_timing_rows)
            enriched["_timing_valid_count"] = len(timing_rows)
            enriched["_timing_invalid_count"] = len(matching_timing_rows) - len(timing_rows)
        slow_calls.append(enriched)
    return McpSummary(
        calls=len(rows),
        errors=_error_counts(rows),
        latencies=_duration_values(rows),
        slow_calls=slow_calls,
    )


def _timing_duration(row: Observation) -> float | None:
    try:
        duration = float(cast(float | int | str, row.get("duration_ms")))
    except TypeError, ValueError:
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


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


def _demand_summary(observations: list[Observation]) -> DemandSummary:
    rows = _rows_for_kind(observations, "telegram.demand")
    counts: dict[str, int] = defaultdict(int)
    reasons: dict[str, int] = defaultdict(int)
    by_kind: dict[tuple[str, str | None], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    actual_attempts = 0
    oldest_queue_age_seconds: float | None = None
    greatest_freshness_debt_seconds: float | None = None
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
        oldest_queue_age_seconds = _max_payload_float(oldest_queue_age_seconds, payload, "queue_age_seconds")
        greatest_freshness_debt_seconds = _max_payload_float(
            greatest_freshness_debt_seconds,
            payload,
            "freshness_debt_seconds",
        )
        _add_demand_reason(reasons, row["reason_code"], units)
    return DemandSummary(
        counts=dict(counts),
        actual_attempts=actual_attempts,
        oldest_queue_age_seconds=oldest_queue_age_seconds,
        greatest_freshness_debt_seconds=greatest_freshness_debt_seconds,
        reasons=dict(reasons),
        by_kind={key: dict(value) for key, value in by_kind.items()},
    )


def _max_payload_float(current: float | None, payload: Mapping[str, object], key: str) -> float | None:
    value = payload.get(key)
    return current if value is None else max(current or 0.0, _as_float(value))


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
        details = [_slow_call_detail(row) for row in summary.slow_calls[:5]]
        lines.append("Slow MCP calls: " + "; ".join(details))
    return lines


def _slow_call_detail(row: Observation) -> str:
    detail = f"{row['tool_name']}={_ms(_as_float(row['duration_ms']))}"
    timing = row.get("_timing")
    if not isinstance(timing, dict):
        return detail + ", attribution=unavailable" + _timing_detail_metadata(None, row)
    payload = _timing_payload(timing)
    if payload is None:
        return detail + ", attribution=unavailable" + _timing_detail_metadata(None, row)
    detail += (
        f", contributor={_timing_contributor(payload)}, attribution={_timing_attribution(payload)}, "
        f"completeness={_timing_completeness(payload)}"
    )
    return detail + _timing_detail_metadata(payload, row)


def _timing_detail_metadata(payload: Mapping[str, object] | None, row: Observation) -> str:
    detail = ""
    if payload is not None:
        unattributed = payload.get("unattributed_ms")
        if isinstance(unattributed, (int, float)) and math.isfinite(float(unattributed)) and unattributed >= 0:
            detail += f", unattributed={_ms(float(unattributed))}"
    total_count = row.get("_timing_total_count")
    valid_count = row.get("_timing_valid_count")
    invalid_count = row.get("_timing_invalid_count")
    if isinstance(total_count, int) and total_count > 0:
        valid = valid_count if isinstance(valid_count, int) else 0
        discarded = invalid_count if isinstance(invalid_count, int) else total_count - valid
        detail += f", timing_rows={valid}/{total_count}, discarded={discarded}, aggregation=largest_request"
    return detail + (_timing_route_source_detail(payload) if payload is not None else "")


def _timing_route_source_detail(payload: Mapping[str, object]) -> str:
    route_attempted = payload.get("route_attempted")
    served_source = payload.get("served_source")
    request_id = payload.get("request_id")
    attempted = (
        route_attempted if isinstance(route_attempted, str) and route_attempted in TIMING_ROUTES else "unavailable"
    )
    served = (
        served_source if isinstance(served_source, str) and served_source in TIMING_SERVED_SOURCES else "unavailable"
    )
    request = request_id if is_valid_request_id(request_id) else "unavailable"
    return f", attempted={attempted}, served={served}, request_id={request}"


def _timing_payload(timing: Mapping[str, object]) -> dict[str, object] | None:
    try:
        decoded = json.loads(str(timing.get("payload_json") or "{}"))
    except TypeError, ValueError:
        return None
    return dict(decoded) if isinstance(decoded, Mapping) else None


def _timing_contributor(payload: Mapping[str, object]) -> str:
    """Return the largest measured boundary without summing nested timings."""
    candidates = _timing_candidates(payload)
    if not candidates:
        return "unavailable"
    winner = max(candidates, key=lambda candidate: candidates[candidate])
    return f"{winner}={_ms(candidates[winner])}"


def _timing_candidates(payload: Mapping[str, object]) -> dict[str, float]:
    nested = _nested_timing_candidates(payload)
    candidates: dict[str, float] = {}
    for phase in TIMING_TOP_LEVEL_PHASES:
        value = _finite_nonnegative(payload.get(f"{phase}_ms"))
        if value is None:
            continue
        exclusive = nested.get(phase)
        candidates[f"{phase}_exclusive"] = max(0.0, value - exclusive) if exclusive is not None else value
    candidates.update(_nested_timing_leaf_candidates(payload))
    candidates.update(
        {
            phase: value
            for phase in TIMING_NESTED_LEAF_PHASES
            if (value := _finite_nonnegative(payload.get(f"{phase}_ms"))) is not None
        }
    )
    return candidates


def _finite_nonnegative(value: object) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
        return None
    return float(value)


def _nested_timing_candidates(payload: Mapping[str, object]) -> dict[str, float]:
    raw_nested = payload.get("nested_phases")
    if not isinstance(raw_nested, Mapping):
        return {}
    totals: dict[str, float] = {}
    for parent in TIMING_NESTED_PARENTS:
        group = raw_nested.get(parent)
        if not isinstance(group, Mapping):
            continue
        nested_total = sum(_nested_timing_leaves(group).values())
        parent_value = _finite_nonnegative(payload.get(f"{parent}_ms"))
        if parent_value is not None:
            totals[parent] = min(parent_value, nested_total)
    return totals


def _nested_timing_leaves(payload: Mapping[str, object]) -> dict[str, float]:
    leaves: dict[str, float] = {}
    for phase in TIMING_NESTED_LEAF_PHASES:
        value = _finite_nonnegative(payload.get(f"{phase}_ms"))
        if value is not None:
            leaves[phase] = value
    return leaves


def _nested_timing_leaf_candidates(payload: Mapping[str, object]) -> dict[str, float]:
    raw_nested = payload.get("nested_phases")
    if not isinstance(raw_nested, Mapping):
        return {}
    candidates: dict[str, float] = {}
    for parent in TIMING_NESTED_PARENTS:
        group = raw_nested.get(parent)
        if not isinstance(group, Mapping):
            continue
        for phase, value in _nested_timing_leaves(group).items():
            candidates[f"{parent}.{phase}"] = value
    return candidates


def _timing_completeness(payload: Mapping[str, object]) -> str:
    measured = payload.get("measured_required_phase_count")
    total = payload.get("required_phase_count")
    if isinstance(measured, int) and isinstance(total, int) and 0 <= measured <= total:
        return f"{measured}/{total}"
    return "unavailable"


def _timing_attribution(payload: Mapping[str, object]) -> str:
    measured = payload.get("measured_required_phase_count")
    total = payload.get("required_phase_count")
    if isinstance(measured, int) and isinstance(total, int) and 0 <= measured <= total:
        return attribution_from_counts(measured, total)
    return "unavailable"


def _coverage_text(window_complete: bool, reasons: list[str]) -> str:
    text = "complete" if window_complete else "partial/unreliable"
    return f"{text} ({', '.join(reasons)})" if reasons else text


def _slow_threshold_ms(seconds: float) -> float:
    return seconds * _MILLISECONDS_PER_SECOND if math.isfinite(seconds) and seconds > 0 else _DEFAULT_SLOW_MCP_MS


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


def _linked_chat_rpc_lines(observations: list[Observation]) -> list[str]:
    attempts: dict[tuple[str, str, str | None], int] = defaultdict(int)
    for row in _rows_for_outcome(_rows_for_kind(observations, "telegram.rpc_request"), "summary"):
        parsed = _get_full_channel_attempt(row)
        if parsed is not None:
            key, count = parsed
            attempts[key] += count
    if not attempts:
        return []
    total = sum(attempts.values())
    dimensions = ", ".join(
        f"{source}/{demand_kind}{f'/{acquisition}' if acquisition else ''}={count}"
        for (source, demand_kind, acquisition), count in sorted(attempts.items())
    )
    return [f"GetFullChannel RPC attempts: {total} ({dimensions})"]


def _get_full_channel_attempt(
    row: Observation,
) -> tuple[tuple[str, str, str | None], int] | None:
    payload = json.loads(str(row["payload_json"] or "{}"))
    if payload.get("request_class") != "get_full_channel":
        return None
    source = str(payload.get("source") or "unknown")
    demand_kind = str(payload.get("demand_kind") or "unknown")
    acquisition_kind = payload.get("acquisition_kind")
    acquisition = None if acquisition_kind is None else str(acquisition_kind)
    return (source, demand_kind, acquisition), _as_int(payload.get("actual_attempts") or 0)


def _demand_lines(summary: DemandSummary) -> list[str]:
    if not summary.counts and summary.actual_attempts == 0:
        return []
    labels = ("selected", "completed", "deferred", "failed")
    fields = _demand_fields(summary, labels)
    lines = ["Demand: " + ", ".join(fields)]
    lines.extend(_demand_kind_lines(summary, labels))
    return lines


def _demand_fields(summary: DemandSummary, labels: tuple[str, ...]) -> list[str]:
    fields = [f"{label.replace('_', ' ')}={summary.counts[label]}" for label in labels if summary.counts.get(label)]
    fields.append(f"actual attempts={summary.actual_attempts}")
    if summary.oldest_queue_age_seconds is not None:
        fields.append(f"oldest queue age={_ms(summary.oldest_queue_age_seconds * _MILLISECONDS_PER_SECOND)}")
    if summary.greatest_freshness_debt_seconds is not None:
        fields.append(f"max freshness debt={_ms(summary.greatest_freshness_debt_seconds * _MILLISECONDS_PER_SECOND)}")
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
    db_path: Path,
    *,
    since_seconds: int,
    now: float | None = None,
    slow_request_seconds: float = _DEFAULT_SLOW_MCP_MS / _MILLISECONDS_PER_SECOND,
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
    coverage_text = _coverage_text(window_complete, coverage_reasons)
    slow_mcp_ms = _slow_threshold_ms(slow_request_seconds)
    lines = [
        f"mcp-telegram operational summary: {_utc(effective_now - since_seconds)} .. {_utc(effective_now)}",
        f"Telemetry: {len(observations)} events; window={coverage_text}; last event={last_event}",
        *_runtime_lines(observations),
        f"Dialog state: {status_text}",
        *_mcp_lines(_mcp_summary(observations, slow_mcp_ms=slow_mcp_ms)),
        *_rpc_lines(rpc_count, rpc_cancelled, rpc_sources),
        *_linked_chat_rpc_lines(observations),
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
