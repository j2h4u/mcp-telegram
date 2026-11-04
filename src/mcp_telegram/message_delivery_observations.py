"""Bounded, passive evidence for update delivery through existing owners."""

from __future__ import annotations

import json
import secrets
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol, cast

_MAX_TRACE_STAGES = 8
_MAX_TRACE_IDENTITIES = 4
_MAX_PAYLOAD_BYTES = 1024


def _bounded_sample(payload: dict[str, object]) -> dict[str, object]:
    identities = list(cast(list[tuple[int, int, int | None]], payload["identities"]))
    counts = dict(cast(dict[str, int], payload["stage_counts"]))
    payload["identities"] = identities
    payload["stage_counts"] = counts
    trimmed = 0
    while (
        len(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()) > _MAX_PAYLOAD_BYTES
    ):
        if identities:
            identities.pop()
        elif counts:
            counts.pop(max(counts))
        else:
            break
        trimmed += 1
        payload["payload_truncated"] = trimmed
    return payload


class DeliveryRecorder(Protocol):
    def record(
        self, *, kind: str, outcome: str | None = None, payload: Mapping[str, object] | None = None
    ) -> bool | None: ...


@dataclass(slots=True)
class DeliveryTrace:
    """Runtime-only identity and lifetime stage budget for one sampled envelope."""

    trace_id: str
    source: str
    scope: str
    stages: list[tuple[str, int]] = field(default_factory=list)
    stage_counts: dict[str, int] = field(default_factory=dict)
    identities: list[tuple[int, int, int | None]] = field(default_factory=list)
    truncated: int = 0


class MessageDeliveryObservations:
    """Count all finite stages and sample at most one root per scope per window."""

    def __init__(self, recorder: DeliveryRecorder, on_activity: Callable[[], None] | None = None) -> None:
        self._recorder = recorder
        self._on_activity = on_activity
        self._lock = threading.Lock()
        self._counts: Counter[tuple[str, str]] = Counter()
        self._samples: dict[str, DeliveryTrace] = {}
        self._decisions: dict[tuple[str, str], dict[str, object]] = {}
        self._decision_overwritten = 0
        self._unsampled = 0
        self._truncated = 0
        self._lost = 0

    def intake(self, *, source: str, scope: str, observed_at_ms: int) -> DeliveryTrace | None:
        if source not in {"push", "rpc_response", "recovery", "replay", "unknown"}:
            source = "unknown"
        if scope not in {"account", "channel"}:
            scope = "account"
        with self._lock:
            self._counts[(scope, "intake")] += 1
            if scope in self._samples:
                self._unsampled += 1
                trace = None
            else:
                stage = "decoded_received" if source == "push" else "intake"
                trace = DeliveryTrace(secrets.token_hex(8), source, scope, [(stage, observed_at_ms)])
                trace.stage_counts[stage] = 1
                self._samples[scope] = trace
        self._flush_if_due()
        return trace

    def _flush_if_due(self) -> None:
        try:
            if self._on_activity is not None:
                self._on_activity()
        except Exception:  # noqa: BLE001 - observer activity must remain passive
            pass

    def stage(self, trace: DeliveryTrace | None, *, scope: str, stage: str, observed_at_ms: int) -> None:
        if scope not in {"account", "channel"} or stage not in {
            "queued",
            "accepted",
            "gap",
            "suppressed",
            "admitted",
            "handler",
            "committed",
            "failed",
            "pts_accepted",
            "pts_duplicate",
            "possible_gap",
            "getting_difference",
            "self_outgoing",
            "seq_duplicate",
            "channel_too_long",
            "catchup_selected",
            "catchup_blocked",
        }:
            return
        with self._lock:
            self._counts[(scope, stage)] += 1
            if trace is None:
                return
            trace.stage_counts[stage] = trace.stage_counts.get(stage, 0) + 1
            selected = self._samples.get(scope)
            if selected is None:
                self._samples[scope] = trace
            elif selected is not trace:
                trace.truncated += 1
                self._truncated += 1
                return
            if any(recorded == stage for recorded, _ in trace.stages):
                return
            if len(trace.stages) >= _MAX_TRACE_STAGES:
                trace.truncated += 1
                self._truncated += 1
            else:
                trace.stages.append((stage, observed_at_ms))

    def identity(self, trace: DeliveryTrace | None, dialog_id: int, message_id: int, sent_at_ms: int | None) -> None:
        if trace is None:
            return
        with self._lock:
            if len(trace.identities) < _MAX_TRACE_IDENTITIES:
                trace.identities.append((dialog_id, message_id, sent_at_ms))
            else:
                trace.truncated += 1
                self._truncated += 1

    def decision(self, scope: str, payload: dict[str, object]) -> None:
        decision = payload.get("decision")
        if scope not in {"account", "channel"} or decision not in {
            "catchup_selected",
            "catchup_blocked",
            "physical_claim",
        }:
            return
        with self._lock:
            key = (scope, str(decision))
            if key in self._decisions:
                self._decision_overwritten += 1
            self._decisions[key] = dict(payload)

    def flush(self) -> None:
        with self._lock:
            counts, self._counts = self._counts, Counter()
            samples = [
                {
                    "trace_id": item.trace_id,
                    "scope": item.scope,
                    "source": item.source,
                    "identities": list(item.identities),
                    "stages": list(item.stages),
                    "stage_counts": dict(item.stage_counts),
                    "truncated": item.truncated,
                }
                for item in self._samples.values()
            ]
            self._samples = {}
            decisions, self._decisions = self._decisions, {}
            overwritten, self._decision_overwritten = self._decision_overwritten, 0
            unsampled, self._unsampled = self._unsampled, 0
            truncated, self._truncated = self._truncated, 0
            lost, self._lost = self._lost, 0
        if not counts and not decisions and not lost:
            return
        lost += sum(
            self._emit("count", {"scope": scope, "stage": stage, "count": count})
            for (scope, stage), count in counts.items()
        )
        lost += sum(self._emit("sample", _bounded_sample(sample)) for sample in samples)
        lost += sum(self._emit("catchup", {"scope": scope, **decision}) for (scope, _), decision in decisions.items())
        lost += self._emit(
            "coverage",
            {
                "sampled": len(samples),
                "unsampled": unsampled,
                "truncated": truncated,
                "lost_rows": lost,
                "decision_overwritten": overwritten,
            },
        )
        if lost:
            with self._lock:
                self._lost += lost

    def _emit(self, outcome: str, payload: dict[str, object]) -> int:
        try:
            return int(self._recorder.record(kind="telegram.update_flow", outcome=outcome, payload=payload) is False)
        except Exception:  # noqa: BLE001 - observation must never affect the update path
            return 1


current_delivery: ContextVar[tuple[MessageDeliveryObservations, DeliveryTrace | None, str] | None] = ContextVar(
    "message_delivery_diagnostic", default=None
)


def observe_current(stage: str, observed_at_ms: int) -> None:
    try:
        current = current_delivery.get()
        if current is not None:
            observer, trace, scope = current
            observer.stage(trace, scope=scope, stage=stage, observed_at_ms=observed_at_ms)
    except Exception:  # noqa: BLE001 - diagnostics must never affect message persistence
        pass
