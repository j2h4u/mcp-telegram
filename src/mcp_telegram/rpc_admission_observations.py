"""Bounded aggregation for high-volume Telegram RPC admission observations."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypedDict, cast

from .flood import FloodWaitObservation
from .telegram_demand import AcquisitionKind, DemandObservationHook
from .telegram_rpc_consumers import DemandKind, telegram_rpc_consumer
from .telegram_rpc_scheduler import (
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    RpcServiceClass,
    TelegramRpcScope,
    TelegramRpcSource,
)

logger = logging.getLogger(__name__)
_MILLISECONDS_PER_SECOND = 1_000
_DELTA_GAP_FILL_COUNTERS = (
    "slice_count",
    "completed",
    "deferred",
    "failed",
    "scheduled",
    "recovery",
    "actual_attempts",
    "attempted_slices",
    "page_count",
    "empty_page_count",
    "fetched_count",
    "new_key_count",
    "preexisting_key_count",
    "uncommitted_unique_count",
    "duplicate_count",
    "continuation_count",
    "terminal_count",
    "recovery_completion_count",
)
_DELTA_GAP_FILL_REASONS = frozenset(
    {
        "admission_deferred",
        "admission_saturated",
        "admission_expired",
        "budget_exhausted",
        "flood_wait",
        "access_lost",
        "history_unavailable",
        "cancelled",
        "error",
        "skipped",
        "empty_page",
        "continuation",
        "terminal",
        "completed",
    }
)


def _validated_delta_gap_fill_metrics(metrics: Mapping[str, int]) -> dict[str, int] | None:
    values = {key: metrics.get(key, 0) for key in _DELTA_GAP_FILL_COUNTERS}
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values.values()):
        return None
    return values if _delta_gap_fill_metrics_reconcile(values) else None


def _delta_gap_fill_metrics_reconcile(values: Mapping[str, int]) -> bool:
    fetched_categories = ("new_key_count", "preexisting_key_count", "uncommitted_unique_count", "duplicate_count")
    return all(
        (
            values["slice_count"] == 1,
            values["slice_count"] == values["completed"] + values["deferred"] + values["failed"],
            values["fetched_count"] == sum(values[key] for key in fetched_categories),
            values["page_count"] >= values["empty_page_count"],
            values["recovery_completion_count"] <= values["recovery"],
        )
    )


class ObservationRecorder(Protocol):
    """Minimal nonblocking persistence seam used by the aggregator."""

    def record(  # noqa: PLR0913 - mirrors the narrow subset of sink keywords this adapter emits
        self,
        *,
        kind: str,
        outcome: str | None = None,
        reason_code: str | None = None,
        duration_ms: float | None = None,
        result_count: int | None = None,
        payload: Mapping[str, object] | None = None,
        observed_at_ms: int | None = None,
        operation_id: str | None = None,
    ) -> bool | None: ...


class RpcAdmissionObservationPolicy(Protocol):
    """Operator-owned aggregation cadence supplied by runtime composition."""

    @property
    def rpc_summary_interval_seconds(self) -> float: ...


class DemandEvidenceOutcome(StrEnum):
    """Final lifecycle outcomes emitted by the durable coordinator."""

    SELECTED = "selected"
    COMPLETED = "completed"
    DEFERRED = "deferred"
    FAILED = "failed"


# Compatibility name for composition code that still refers to demand evidence.
class _ProfileObservationValues(TypedDict):
    mode: str
    eligible_pair: bool
    outcome: str
    actual_attempts: int
    retries: int
    full_profile_outcome: str | None
    personal_channel_outcome: str | None
    pair_ready: bool
    pair_readiness_latency_ms: float | None
    local_satisfaction: bool
    prevented_request: bool
    reuse_rejection_reason: str | None
    reused_age_ms: float | None
    stale_writer_rejected: bool
    measurement_complete: bool


_DEMAND_EVIDENCE_OUTCOMES = frozenset(
    {
        DemandEvidenceOutcome.SELECTED,
        DemandEvidenceOutcome.COMPLETED,
        DemandEvidenceOutcome.DEFERRED,
        DemandEvidenceOutcome.FAILED,
    }
)

_AdmissionKey = tuple[TelegramRpcSource, RpcServiceClass, DemandKind | None, AcquisitionKind | None]
_DemandKey = tuple[DemandKind, AcquisitionKind | None, DemandEvidenceOutcome, str | None]
_ProfileKey = tuple[
    str,
    bool,
    str,
    str | None,
    str | None,
    bool,
    bool,
    str | None,
    bool,
    bool,
]

_METHOD_NAME_LIMIT = 256
_RPC_PAYLOAD_BYTES = 1024
_OBSERVATION_ID_HEX_LENGTH = 32
_METHOD_BUCKET_LIMIT = 16
_SAMPLED_ROOT_LIMIT = 2
_ROOT_EVENT_LIMIT = 12
_RPC_PHASES = (
    "write_claim",
    "write_return",
    "known_response",
    "unknown_outcome",
    "vendor_cached",
    "rejected",
    "retry",
)
_RPC_REASONS = frozenset(
    {
        "dc_migration",
        "server_transient",
        "deadline_elapsed",
        "attempt_budget_exhausted",
        "capacity",
        "queue_full",
        "source_limit",
        "closed",
        "cancelled",
        "replay",
        "flood_wait",
        "unknown",
        "RpcAdmissionSaturatedError",
        "RpcAdmissionExpiredError",
    }
)
_MethodKey = tuple[str, TelegramRpcSource, RpcServiceClass, DemandKind | None, AcquisitionKind | None]


def _observation_method(method: str | None, method_names: frozenset[str]) -> str:
    """Validate against names supplied by the existing vendor owner."""
    if isinstance(method, str) and len(method) <= _METHOD_NAME_LIMIT and method in method_names:
        return method
    return "unknown"


def _rpc_payload_fits(payload: Mapping[str, object]) -> bool:
    # Match the existing persistence wire contract without depending on its implementation.
    encoded = json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return len(encoded.encode("utf-8")) <= _RPC_PAYLOAD_BYTES


def _valid_rpc_scope(scope: TelegramRpcScope) -> bool:
    return (
        isinstance(scope.source, TelegramRpcSource)
        and isinstance(scope.service_class, RpcServiceClass)
        and (scope.demand_kind is None or isinstance(scope.demand_kind, DemandKind))
        and (scope.acquisition_kind is None or isinstance(scope.acquisition_kind, AcquisitionKind))
    )


@dataclass(slots=True)
class _MethodAggregate:
    started_at: float
    started_wall_ms: int
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_RPC_PHASES, 0))

    def merge(self, other: _MethodAggregate) -> None:
        if other.started_at < self.started_at:
            self.started_at, self.started_wall_ms = other.started_at, other.started_wall_ms
        for phase, count in other.counts.items():
            self.counts[phase] += count


@dataclass(frozen=True, slots=True)
class _RpcSample:
    operation_id: str
    payload: dict[str, object]
    observed_at_ms: int


@dataclass(slots=True)
class _AdmissionAggregate:
    queued_count: int = 0
    dispatched_count: int = 0
    wait_total_seconds: float = 0.0
    wait_max_seconds: float = 0.0
    queue_depth_max: int = 0
    total_depth_max: int = 0
    active_depth_max: int = 0
    total_outstanding_max: int = 0
    bucket_started_at: float | None = None

    def add(self, event: RpcAdmissionEvent, *, observed_at: float) -> None:
        self.bucket_started_at = (
            observed_at if self.bucket_started_at is None else min(self.bucket_started_at, observed_at)
        )
        if event.kind is RpcAdmissionEventKind.QUEUED:
            self.queued_count += 1
        elif event.kind is RpcAdmissionEventKind.DISPATCHED:
            self.dispatched_count += 1
            wait_seconds = event.wait_seconds or 0.0
            self.wait_total_seconds += wait_seconds
            self.wait_max_seconds = max(self.wait_max_seconds, wait_seconds)
        self.queue_depth_max = max(self.queue_depth_max, event.queue_depth)
        self.total_depth_max = max(self.total_depth_max, event.total_depth)
        self.active_depth_max = max(self.active_depth_max, event.active_depth)
        self.total_outstanding_max = max(self.total_outstanding_max, event.total_outstanding)

    def merge(self, other: _AdmissionAggregate) -> None:
        """Restore an unpersisted snapshot without losing newer observations."""
        self.queued_count += other.queued_count
        self.dispatched_count += other.dispatched_count
        self.wait_total_seconds += other.wait_total_seconds
        self.wait_max_seconds = max(self.wait_max_seconds, other.wait_max_seconds)
        self.queue_depth_max = max(self.queue_depth_max, other.queue_depth_max)
        self.total_depth_max = max(self.total_depth_max, other.total_depth_max)
        self.active_depth_max = max(self.active_depth_max, other.active_depth_max)
        self.total_outstanding_max = max(self.total_outstanding_max, other.total_outstanding_max)
        if other.bucket_started_at is not None:
            self.bucket_started_at = (
                other.bucket_started_at
                if self.bucket_started_at is None
                else min(self.bucket_started_at, other.bucket_started_at)
            )


class RpcAdmissionObservationAggregator:
    """Coalesce routine queue traffic while preserving terminal outcomes raw."""

    def __init__(
        self,
        recorder: ObservationRecorder,
        *,
        policy: RpcAdmissionObservationPolicy,
        clock: Callable[[], float] = time.monotonic,
        method_names: frozenset[str] = frozenset(),
    ) -> None:
        summary_interval_seconds = policy.rpc_summary_interval_seconds
        if summary_interval_seconds <= 0:
            raise ValueError("summary_interval_seconds must be positive")
        self._recorder = recorder
        self._method_names = method_names
        self._summary_interval_seconds = summary_interval_seconds
        self._clock = clock
        self._last_flush_at = clock()
        self._aggregates: dict[_AdmissionKey, _AdmissionAggregate] = {}
        self._demand_aggregates: dict[_DemandKey, _DemandAggregate] = {}
        self._profile_aggregates: dict[_ProfileKey, _ProfilePairAggregate] = {}
        self._method_aggregates: dict[_MethodKey, _MethodAggregate] = {}
        self._method_overflow: _MethodAggregate | None = None
        self._rpc_samples: dict[str, list[_RpcSample]] = {}
        self._rpc_unsampled_events = 0
        self._rpc_truncated_events = 0
        self._rpc_lost_rows = 0
        self._rpc_window_started_at = self._last_flush_at
        self._rpc_window_started_wall_ms = int(time.time() * _MILLISECONDS_PER_SECOND)
        self._rpc_secret = secrets.token_bytes(32)
        self._delta_gap_fill: dict[str, int] = dict.fromkeys(_DELTA_GAP_FILL_COUNTERS, 0)
        self._delta_gap_fill_reasons: dict[str, int] = {}
        self._delta_gap_fill_rejected = 0
        self._delta_gap_fill_window_started_at = self._last_flush_at
        self._state_lock = threading.Lock()
        self._flush_lock = threading.Lock()

    def observe(self, event: RpcAdmissionEvent) -> None:
        """Record one scheduler event without blocking its admission path."""
        try:
            if event.observation_scope is not None and event.kind in {
                RpcAdmissionEventKind.RESUBMITTED,
                RpcAdmissionEventKind.REJECTED,
                RpcAdmissionEventKind.EXPIRED,
                RpcAdmissionEventKind.CANCELLED,
                RpcAdmissionEventKind.CLOSED,
            }:
                self.observe_rpc_event(
                    scope=event.observation_scope,
                    phase="retry" if event.kind is RpcAdmissionEventKind.RESUBMITTED else "rejected",
                    reason=event.reason,
                    request_method=event.observation_method,
                )
            if event.kind in {RpcAdmissionEventKind.QUEUED, RpcAdmissionEventKind.DISPATCHED}:
                self._aggregate(event)
            else:
                self._record_raw(event)
            # The gate's paired claim callback must enter this same snapshot before flushing.
            if event.kind is not RpcAdmissionEventKind.DISPATCHED or event.observation_scope is None:
                self.flush_if_due()
        except Exception:  # noqa: BLE001 - telemetry cannot break scheduler admission
            # Operational telemetry remains best effort at the scheduler callback boundary.
            return

    def observe_rpc_event(  # noqa: PLR0913 - one passive callback shared by existing gate seams
        self,
        *,
        scope: TelegramRpcScope,
        phase: str,
        request_method: str | None = None,
        admission_sequence: int | None = None,
        replay_ordinal: int | None = None,
        reason: str | None = None,
        attempt_ordinal: int | None = None,
    ) -> None:
        """Bounded method counters and biased root samples; never admit work."""
        try:
            if phase not in _RPC_PHASES or not _valid_rpc_scope(scope):
                return
            method = _observation_method(request_method or scope.request_method, self._method_names)
            key = (method, scope.source, scope.service_class, scope.demand_kind, scope.acquisition_kind)
            now, wall_ms = self._clock(), int(time.time() * _MILLISECONDS_PER_SECOND)
            operation_id, target_ref = self._rpc_identity(scope)
            payload = self._rpc_sample_payload(scope, phase, method, target_ref, reason)
            if phase in {"write_claim", "write_return"}:
                payload["attempt_ordinal"] = (
                    attempt_ordinal if attempt_ordinal is not None else payload.get("root_attempts", 0)
                )
            if admission_sequence is not None:
                payload["admission_sequence"] = admission_sequence
            if replay_ordinal is not None:
                payload["replay_ordinal"] = replay_ordinal
            with self._state_lock:
                aggregate = self._method_bucket(key, now, wall_ms)
                aggregate.counts[phase] += 1
                self._sample_rpc_event(operation_id, payload, wall_ms)
            self.flush_if_due()
        except Exception:  # noqa: BLE001 - neither payloads nor observer failures enter the product path
            with self._state_lock:
                self._rpc_lost_rows += 1

    def _rpc_identity(self, scope: TelegramRpcScope) -> tuple[str | None, str | None]:
        selected = None if scope.attempt_budget is None else scope.attempt_budget.selected
        if selected is not None:
            key = selected.key
            target = json.dumps([key.demand_kind.value, key.account_id, key.target], separators=(",", ":"))
            return self._rpc_ref([target, selected.revision]), self._rpc_ref([target])
        if scope.attempt_evidence is not None:
            return self._rpc_ref([scope.attempt_evidence.observation_id]), None
        return None, None

    def _rpc_ref(self, values: list[str]) -> str:
        encoded = json.dumps(values, separators=(",", ":")).encode("utf-8")
        return hashlib.blake2b(encoded, key=self._rpc_secret, digest_size=16).hexdigest()

    @staticmethod
    def _rpc_sample_payload(
        scope: TelegramRpcScope, phase: str, method: str, target_ref: str | None, reason: str | None
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "method": method,
            "source": scope.source.value,
            "phase": phase,
            "consumer_trigger": telegram_rpc_consumer(scope.source).acquisition.trigger.value,
            "causal_scope": "partial_runtime_sample",
        }
        if reason is not None:
            payload["reason"] = reason if reason in _RPC_REASONS else "unknown"
        if scope.demand_kind is not None:
            payload["demand_kind"] = scope.demand_kind.value
        if scope.acquisition_kind is not None:
            payload["acquisition_kind"] = scope.acquisition_kind.value
        if target_ref is not None:
            payload["target_ref"] = target_ref
            payload["trigger"] = "durable_selection"
        else:
            payload["trigger"] = "unknown"
        if scope.owned_attempt_id is not None:
            # UUIDs originate in the gate; no protocol/session identifier is accepted.
            token = scope.owned_attempt_id
            if len(token) == _OBSERVATION_ID_HEX_LENGTH and all(char in "0123456789abcdef" for char in token):
                payload["attempt_ref"] = token
        payload.update(RpcAdmissionObservationAggregator._rpc_budget_fields(scope))
        return payload

    @staticmethod
    def _rpc_budget_fields(scope: TelegramRpcScope) -> dict[str, object]:
        payload: dict[str, object] = {}
        budget = scope.attempt_budget
        if budget is not None:
            payload.update(root_attempts=budget.attempts, budget_limit=budget.limit, budget_remaining=budget.remaining)
        elif scope.attempt_evidence is not None:
            payload["root_attempts"] = scope.attempt_evidence.actual_attempts
        if scope.deadline is not None and _is_finite(scope.deadline):
            remaining_ms = (scope.deadline - time.monotonic()) * _MILLISECONDS_PER_SECOND
            if _is_finite(remaining_ms):
                payload["deadline_remaining_ms"] = max(0.0, remaining_ms)
            else:
                payload["payload_truncated"] = True
        return payload

    def _method_bucket(self, key: _MethodKey | None, now: float, wall_ms: int) -> _MethodAggregate:
        if key is not None and key in self._method_aggregates:
            return self._method_aggregates[key]
        if key is not None and len(self._method_aggregates) < _METHOD_BUCKET_LIMIT:
            aggregate = _MethodAggregate(now, wall_ms)
            self._method_aggregates[key] = aggregate
            return aggregate
        if self._method_overflow is None:
            self._method_overflow = _MethodAggregate(now, wall_ms)
        return self._method_overflow

    def _sample_rpc_event(self, operation_id: str | None, payload: dict[str, object], wall_ms: int) -> None:
        if operation_id is None or (
            operation_id not in self._rpc_samples and len(self._rpc_samples) >= _SAMPLED_ROOT_LIMIT
        ):
            self._rpc_unsampled_events += 1
            return
        samples = self._rpc_samples.setdefault(operation_id, [])
        if len(samples) >= _ROOT_EVENT_LIMIT:
            self._rpc_truncated_events += 1
            return
        # Drop optional dimensions in a fixed order, retaining explicit truncation evidence.
        for optional in (None, "acquisition_kind", "consumer_trigger", "demand_kind", "method"):
            if optional is not None:
                payload.pop(optional, None)
                payload["payload_truncated"] = True
            if not _rpc_payload_fits(payload):
                continue
            samples.append(_RpcSample(operation_id, payload, wall_ms))
            return
        self._rpc_truncated_events += 1

    def observe_flood_wait(
        self,
        observation: FloodWaitObservation,
    ) -> None:
        """Persist one content-free, source-attributed FloodWait observation."""
        payload: dict[str, object] = {
            "source": observation.source,
            "service_class": observation.service_class,
            "request_method": observation.request_method,
            "origin": observation.origin,
            "actual_dispatch": observation.actual_dispatch,
            "cooldown_until_utc_ms": observation.cooldown_until_utc_ms,
            "circuit_open": observation.circuit_open,
        }
        if observation.demand_kind is not None:
            payload["demand_kind"] = observation.demand_kind
        if observation.acquisition_kind is not None:
            payload["acquisition_kind"] = observation.acquisition_kind
        if observation.admission_sequence is not None:
            payload["admission_sequence"] = observation.admission_sequence
        if observation.dispatch_at_monotonic is not None:
            payload["dispatch_at_monotonic"] = observation.dispatch_at_monotonic
        try:
            self._recorder.record(
                kind="telegram.rpc_admission",
                outcome="flood_wait",
                reason_code=observation.origin,
                duration_ms=observation.seconds * _MILLISECONDS_PER_SECOND,
                payload=payload,
                observed_at_ms=observation.observed_at_ms,
            )
        except Exception:
            logger.exception("flood_wait_observation_failed source=%s", observation.source)

    def observe_demand(  # noqa: PLR0913 - explicit bounded evidence dimensions
        self,
        *,
        outcome: DemandEvidenceOutcome | str,
        demand_kind: DemandKind,
        acquisition_kind: AcquisitionKind | None = None,
        demand_units: int = 1,
        actual_attempts: int = 0,
        queue_age_seconds: float | None = None,
        freshness_debt_seconds: float | None = None,
        reason: str | None = None,
    ) -> None:
        """Aggregate final coordinator outcomes without storing work identity.

        ``demand_units`` counts product demand and ``actual_attempts`` counts
        Telegram attempts.  They intentionally remain separate fields.
        """
        try:
            normalized_outcome = _normalize_demand_outcome(outcome)
            if not isinstance(demand_kind, DemandKind):
                raise TypeError("demand_kind must be a DemandKind")
            if acquisition_kind is not None and not isinstance(acquisition_kind, AcquisitionKind):
                raise TypeError("acquisition_kind must be an AcquisitionKind")
            _validate_positive_int(demand_units, "demand_units")
            _validate_nonnegative_int(actual_attempts, "actual_attempts")
            _validate_optional_nonnegative_number(queue_age_seconds, "queue_age_seconds")
            _validate_optional_nonnegative_number(freshness_debt_seconds, "freshness_debt_seconds")
            normalized_reason = _normalize_reason(reason)
            key = (demand_kind, acquisition_kind, normalized_outcome, normalized_reason)
            with self._state_lock:
                aggregate = self._demand_aggregates.setdefault(key, _DemandAggregate())
                aggregate.add(
                    demand_units=demand_units,
                    actual_attempts=actual_attempts,
                    queue_age_seconds=queue_age_seconds,
                    freshness_debt_seconds=freshness_debt_seconds,
                )
            self.flush_if_due()
        except Exception:  # noqa: BLE001 - telemetry cannot break demand producers
            return

    def observe_profile_pair(  # noqa: PLR0913 - explicit bounded evidence dimensions
        self,
        *,
        mode: str,
        eligible_pair: bool,
        outcome: str,
        actual_attempts: int = 0,
        retries: int = 0,
        full_profile_outcome: str | None = None,
        personal_channel_outcome: str | None = None,
        pair_ready: bool = False,
        pair_readiness_latency_ms: float | None = None,
        local_satisfaction: bool = False,
        prevented_request: bool = False,
        reuse_rejection_reason: str | None = None,
        reused_age_ms: float | None = None,
        stale_writer_rejected: bool = False,
        measurement_complete: bool = True,
    ) -> None:
        """Aggregate one profile lifecycle without retaining work identity.

        ``actual_attempts`` is deliberately separate from the transport
        admission aggregate.  It describes the profile operation's own
        GetFullUser dispatches and is never added to admission counters.
        """
        try:
            values = _validate_profile_observation(
                mode=mode,
                eligible_pair=eligible_pair,
                outcome=outcome,
                actual_attempts=actual_attempts,
                retries=retries,
                full_profile_outcome=full_profile_outcome,
                personal_channel_outcome=personal_channel_outcome,
                pair_ready=pair_ready,
                pair_readiness_latency_ms=pair_readiness_latency_ms,
                local_satisfaction=local_satisfaction,
                prevented_request=prevented_request,
                reuse_rejection_reason=reuse_rejection_reason,
                reused_age_ms=reused_age_ms,
                stale_writer_rejected=stale_writer_rejected,
                measurement_complete=measurement_complete,
            )
            key = (
                values["mode"],
                values["eligible_pair"],
                values["outcome"],
                values["full_profile_outcome"],
                values["personal_channel_outcome"],
                values["local_satisfaction"],
                values["prevented_request"],
                values["reuse_rejection_reason"],
                values["stale_writer_rejected"],
                values["measurement_complete"],
            )
            with self._state_lock:
                aggregate = self._profile_aggregates.setdefault(key, _ProfilePairAggregate())
                aggregate.add(
                    actual_attempts=values["actual_attempts"],
                    retries=values["retries"],
                    pair_ready=values["pair_ready"],
                    pair_readiness_latency_ms=values["pair_readiness_latency_ms"],
                    reused_age_ms=values["reused_age_ms"],
                )
            self.flush_if_due()
        except Exception:  # noqa: BLE001 - telemetry cannot affect profile correctness
            return

    def observe_delta_gap_fill(self, metrics: Mapping[str, int], *, reason: str) -> None:
        """Aggregate one content-free forward gap-fill slice into a bounded window."""
        try:
            values = _validated_delta_gap_fill_metrics(metrics)
            if values is None:
                with self._state_lock:
                    self._delta_gap_fill_rejected += 1
                self.flush_if_due()
                return
            self._aggregate_delta_gap_fill(values, reason)
        except Exception:  # noqa: BLE001 - telemetry cannot affect the sync slice
            return

    def _aggregate_delta_gap_fill(self, values: Mapping[str, int], reason: str) -> None:
        normalized_reason = reason if reason in _DELTA_GAP_FILL_REASONS else "error"
        with self._state_lock:
            for key, value in values.items():
                self._delta_gap_fill[key] += value
            self._delta_gap_fill_reasons[normalized_reason] = self._delta_gap_fill_reasons.get(normalized_reason, 0) + 1
        self.flush_if_due()

    def flush_if_due(self) -> None:
        now = self._clock()
        with self._state_lock:
            due = now - self._last_flush_at >= self._summary_interval_seconds
        if due:
            self._try_flush(now=now)

    def flush(self, *, now: float | None = None) -> None:
        """Persist every pending summary, waiting for an in-progress flush if needed."""
        with self._flush_lock:
            self._flush_locked(now=now)

    def _try_flush(self, *, now: float) -> None:
        """Start a due callback flush only when no other flush owns the recorder."""
        if not self._flush_lock.acquire(blocking=False):
            return
        try:
            self._flush_locked(now=now)
        finally:
            self._flush_lock.release()

    def _flush_locked(self, *, now: float | None) -> None:
        """Persist one snapshot while serialising source/class summaries."""
        flush_at = self._clock() if now is None else now
        with self._state_lock:
            aggregates, self._aggregates = self._aggregates, {}
            demand_aggregates, self._demand_aggregates = self._demand_aggregates, {}
            self._last_flush_at = flush_at
        self._flush_admission_aggregates(aggregates, flush_at=flush_at, flush_wall_at=time.time())
        self._flush_demand_aggregates(demand_aggregates)
        with self._state_lock:
            profile_aggregates, self._profile_aggregates = self._profile_aggregates, {}
            delta_gap_fill, self._delta_gap_fill = (
                self._delta_gap_fill,
                cast(dict[str, int], dict.fromkeys(_DELTA_GAP_FILL_COUNTERS, 0)),
            )
            delta_reasons, self._delta_gap_fill_reasons = self._delta_gap_fill_reasons, {}
            delta_rejected, self._delta_gap_fill_rejected = self._delta_gap_fill_rejected, 0
            delta_window_started_at = self._delta_gap_fill_window_started_at
        self._flush_profile_aggregates(profile_aggregates)
        self._flush_rpc_methods(flush_at)
        self._flush_delta_gap_fill(delta_gap_fill, delta_reasons, delta_rejected, delta_window_started_at, flush_at)
        with self._state_lock:
            self._last_flush_at = max(self._last_flush_at, self._clock())

    def _record_rpc_row(
        self,
        *,
        outcome: str,
        payload: Mapping[str, object],
        observed_at_ms: int,
        operation_id: str | None = None,
        result_count: int | None = None,
    ) -> bool:
        try:
            return (
                self._recorder.record(
                    kind="telegram.rpc_request",
                    outcome=outcome,
                    payload=payload,
                    observed_at_ms=observed_at_ms,
                    operation_id=operation_id,
                    result_count=result_count,
                )
                is not False
            )
        except Exception:  # noqa: BLE001 - sink rejection is coverage loss, never a gate failure
            return False

    def _flush_rpc_methods(self, flush_at: float) -> None:
        flush_at = max(flush_at, self._clock())
        wall_ms = int(time.time() * _MILLISECONDS_PER_SECOND)
        with self._state_lock:
            methods, self._method_aggregates = self._method_aggregates, {}
            overflow, self._method_overflow = self._method_overflow, None
            samples, self._rpc_samples = self._rpc_samples, {}
            unsampled, self._rpc_unsampled_events = self._rpc_unsampled_events, 0
            truncated, self._rpc_truncated_events = self._rpc_truncated_events, 0
            lost, self._rpc_lost_rows = self._rpc_lost_rows, 0
            started_at, self._rpc_window_started_at = self._rpc_window_started_at, flush_at
            started_wall_ms, self._rpc_window_started_wall_ms = self._rpc_window_started_wall_ms, wall_ms
        if not methods and overflow is None and not samples and not lost:
            return
        rejected = self._flush_method_summaries(methods, overflow, flush_at, wall_ms)
        lost += self._flush_rpc_samples(samples)
        coverage = {
            "configured_window_seconds": self._summary_interval_seconds,
            "window_seconds": max(0.0, flush_at - started_at),
            "bucket_started_at_ms": started_wall_ms,
            "bucket_ended_at_ms": wall_ms,
            "method_bucket_limit": _METHOD_BUCKET_LIMIT,
            "sample_root_limit": _SAMPLED_ROOT_LIMIT,
            "sample_event_limit": _ROOT_EVENT_LIMIT,
            "method_attribution_complete": overflow is None,
            "overflow_events": 0 if overflow is None else sum(overflow.counts.values()),
            "unsampled_events": unsampled,
            "truncated_events": truncated,
            "lost_rows": lost,
            "unaccepted_summary_rows": rejected,
            "sampling": "first_roots_partial",
            "correlation_scope": "runtime_owner_target",
        }
        if not self._record_rpc_row(outcome="coverage", payload=coverage, observed_at_ms=wall_ms):
            with self._state_lock:
                self._rpc_lost_rows += lost + 1
                self._rpc_unsampled_events += unsampled
                self._rpc_truncated_events += truncated

    def _flush_method_summaries(
        self,
        methods: dict[_MethodKey, _MethodAggregate],
        overflow: _MethodAggregate | None,
        flush_at: float,
        wall_ms: int,
    ) -> int:
        rejected = 0
        snapshots: list[tuple[_MethodKey | None, _MethodAggregate]] = list(methods.items())
        if overflow is not None:
            snapshots.append((None, overflow))
        for key, aggregate in snapshots:
            payload = self._method_payload(key, aggregate, flush_at, wall_ms)
            if not self._record_rpc_row(
                outcome="method_summary",
                result_count=aggregate.counts["write_claim"],
                payload=payload,
                observed_at_ms=wall_ms,
            ):
                rejected += 1
                with self._state_lock:
                    self._method_bucket(key, aggregate.started_at, aggregate.started_wall_ms).merge(aggregate)
        return rejected

    def _flush_rpc_samples(self, samples: dict[str, list[_RpcSample]]) -> int:
        lost = 0
        for root_samples in samples.values():
            for sample in root_samples:
                if not self._record_rpc_row(
                    outcome="causal_sample",
                    operation_id=sample.operation_id,
                    payload=sample.payload,
                    observed_at_ms=sample.observed_at_ms,
                ):
                    lost += 1
        return lost

    @staticmethod
    def _method_payload(
        key: _MethodKey | None, aggregate: _MethodAggregate, flush_at: float, wall_ms: int
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            **aggregate.counts,
            "dispatch_semantics": "write_claim_v1",
            "actual_attempts": aggregate.counts["write_claim"],
            "bucket_started_at_ms": aggregate.started_wall_ms,
            "bucket_ended_at_ms": wall_ms,
            "window_seconds": max(0.0, flush_at - aggregate.started_at),
        }
        if key is None:
            payload["method"] = "overflow"
            return payload
        method, source, service, demand, acquisition = key
        payload.update(method=method, source=source.value, service_class=service.value)
        if demand is not None:
            payload["demand_kind"] = demand.value
        if acquisition is not None:
            payload["acquisition_kind"] = acquisition.value
        if method.startswith("telethon.tl.functions.channels.GetFullChannelRequest:"):
            payload["request_class"] = "get_full_channel"
        return payload

    def _flush_delta_gap_fill(
        self,
        counters: dict[str, int],
        reasons: dict[str, int],
        rejected: int,
        window_started_at: float,
        flush_at: float,
    ) -> None:
        if not counters["slice_count"] and not rejected:
            with self._state_lock:
                self._delta_gap_fill_window_started_at = flush_at
            return
        payload: dict[str, object] = {
            **counters,
            "rejected_slice_count": rejected,
            "slice_outcome_scope": "forward_slice",
            "window_seconds": max(0.0, flush_at - window_started_at),
        }
        if reasons:
            payload["reason_counts"] = reasons
        try:
            accepted = self._recorder.record(
                kind="sync.delta_gap_fill",
                outcome="summary",
                result_count=counters["slice_count"] + rejected,
                payload=payload,
            )
            if accepted is False:
                with self._state_lock:
                    self._merge_delta_gap_fill(counters, reasons, rejected)
                logger.warning("delta_gap_fill_summary_queue_full")
                return
            with self._state_lock:
                self._delta_gap_fill_window_started_at = flush_at
        except Exception:
            with self._state_lock:
                self._merge_delta_gap_fill(counters, reasons, rejected)
            logger.exception("delta_gap_fill_summary_flush_failed")

    def _merge_delta_gap_fill(self, counters: Mapping[str, int], reasons: Mapping[str, int], rejected: int) -> None:
        for key, value in counters.items():
            self._delta_gap_fill[key] += value
        for key, value in reasons.items():
            self._delta_gap_fill_reasons[key] = self._delta_gap_fill_reasons.get(key, 0) + value
        self._delta_gap_fill_rejected += rejected

    def _flush_admission_aggregates(
        self, aggregates: dict[_AdmissionKey, _AdmissionAggregate], *, flush_at: float, flush_wall_at: float
    ) -> None:
        for (source, service_class, demand_kind, acquisition_kind), aggregate in aggregates.items():
            try:
                accepted = self._record_admission_summary(
                    source,
                    service_class,
                    demand_kind,
                    acquisition_kind,
                    aggregate,
                    flush_at=flush_at,
                    flush_wall_at=flush_wall_at,
                )
                if accepted is False:
                    with self._state_lock:
                        self._aggregates.setdefault(
                            (source, service_class, demand_kind, acquisition_kind), _AdmissionAggregate()
                        ).merge(aggregate)
                    logger.warning(
                        "rpc_admission_summary_queue_full source=%s service_class=%s",
                        source.value,
                        service_class.value,
                    )
            except Exception:
                with self._state_lock:
                    self._aggregates.setdefault(
                        (source, service_class, demand_kind, acquisition_kind), _AdmissionAggregate()
                    ).merge(aggregate)
                logger.exception(
                    "rpc_admission_summary_flush_failed source=%s service_class=%s",
                    source.value,
                    service_class.value,
                )

    def _record_admission_summary(  # noqa: PLR0913 - explicit timestamps keep bucket timing at the recorder boundary
        self,
        source: TelegramRpcSource,
        service_class: RpcServiceClass,
        demand_kind: DemandKind | None,
        acquisition_kind: AcquisitionKind | None,
        aggregate: _AdmissionAggregate,
        *,
        flush_at: float,
        flush_wall_at: float,
    ) -> bool | None:
        dispatched = aggregate.dispatched_count
        elapsed = max(
            0.0, flush_at - (aggregate.bucket_started_at if aggregate.bucket_started_at is not None else flush_at)
        )
        bucket_ended_at_ms = int(flush_wall_at * _MILLISECONDS_PER_SECOND)
        bucket_started_at_ms = bucket_ended_at_ms - int(elapsed * _MILLISECONDS_PER_SECOND)
        payload: dict[str, object] = {
            "source": source.value,
            "service_class": service_class.value,
            "queued_count": aggregate.queued_count,
            "dispatched_count": dispatched,
            "max_wait_ms": aggregate.wait_max_seconds * _MILLISECONDS_PER_SECOND,
            "queue_depth_max": aggregate.queue_depth_max,
            "total_depth_max": aggregate.total_depth_max,
            "active_depth_max": aggregate.active_depth_max,
            "total_outstanding_max": aggregate.total_outstanding_max,
            "window_seconds": elapsed,
            "bucket_started_at_ms": bucket_started_at_ms,
            "bucket_ended_at_ms": bucket_ended_at_ms,
        }
        if demand_kind is not None:
            payload["demand_kind"] = demand_kind.value
            payload["actual_attempts"] = dispatched
        if acquisition_kind is not None:
            payload["acquisition_kind"] = acquisition_kind.value
        return self._recorder.record(
            kind="telegram.rpc_admission",
            outcome="summary",
            duration_ms=(aggregate.wait_total_seconds * _MILLISECONDS_PER_SECOND / dispatched if dispatched else None),
            result_count=dispatched,
            payload=payload,
            observed_at_ms=bucket_ended_at_ms,
        )

    def _flush_demand_aggregates(self, aggregates: dict[_DemandKey, _DemandAggregate]) -> None:
        for (demand_kind, acquisition_kind, outcome, reason), aggregate in aggregates.items():
            try:
                self._recorder.record(
                    kind="telegram.demand",
                    outcome=outcome.value,
                    reason_code=reason,
                    result_count=None,
                    payload=_demand_payload(
                        demand_kind=demand_kind,
                        acquisition_kind=acquisition_kind,
                        aggregate=aggregate,
                        window_seconds=self._summary_interval_seconds,
                    ),
                )
            except Exception:
                with self._state_lock:
                    self._demand_aggregates.setdefault(
                        (demand_kind, acquisition_kind, outcome, reason),
                        _DemandAggregate(),
                    ).merge(aggregate)
                logger.exception(
                    "demand_observation_flush_failed demand_kind=%s acquisition_kind=%s outcome=%s",
                    demand_kind.value,
                    None if acquisition_kind is None else acquisition_kind.value,
                    outcome.value,
                )

    def _flush_profile_aggregates(self, aggregates: dict[_ProfileKey, _ProfilePairAggregate]) -> None:
        for key, aggregate in aggregates.items():
            try:
                self._record_profile_summary(key, aggregate)
            except Exception:  # noqa: BLE001 - telemetry cannot affect profile correctness
                with self._state_lock:
                    self._profile_aggregates.setdefault(key, _ProfilePairAggregate()).merge(aggregate)
                logger.debug("entity_profile_pair_summary_flush_failed")

    def _record_profile_summary(self, key: _ProfileKey, aggregate: _ProfilePairAggregate) -> None:
        (
            mode,
            eligible_pair,
            outcome,
            full_profile_outcome,
            personal_channel_outcome,
            local_satisfaction,
            prevented_request,
            reuse_rejection_reason,
            stale_writer_rejected,
            measurement_complete,
        ) = key
        payload: dict[str, object] = {
            "mode": mode,
            "eligible_pair": eligible_pair,
            "event_count": aggregate.event_count,
            "actual_attempts": aggregate.actual_attempts,
            "retries": aggregate.retries,
            "pair_ready_count": aggregate.pair_ready_count,
            "local_satisfaction": local_satisfaction,
            "prevented_request": prevented_request,
            "stale_writer_rejected": stale_writer_rejected,
            "measurement_complete": measurement_complete,
            "window_seconds": self._summary_interval_seconds,
        }
        if full_profile_outcome is not None:
            payload["full_profile_outcome"] = full_profile_outcome
        if personal_channel_outcome is not None:
            payload["personal_channel_outcome"] = personal_channel_outcome
        if reuse_rejection_reason is not None:
            payload["reuse_rejection_reason"] = reuse_rejection_reason
        if aggregate.readiness_latency_count:
            payload["pair_readiness_latency_ms"] = (
                aggregate.readiness_latency_total_ms / aggregate.readiness_latency_count
            )
        if aggregate.reused_age_count:
            payload["reused_age_ms"] = aggregate.reused_age_max_ms
        self._recorder.record(
            kind="entity_profile.pair",
            outcome=cast(str, outcome),
            result_count=aggregate.event_count,
            duration_ms=(
                aggregate.readiness_latency_total_ms / aggregate.readiness_latency_count
                if aggregate.readiness_latency_count
                else None
            ),
            payload=payload,
        )

    def _aggregate(self, event: RpcAdmissionEvent) -> None:
        if event.source is None or event.service_class is None:
            self._record_raw(event)
            return
        key = (event.source, event.service_class, event.demand_kind, event.acquisition_kind)
        observed_at = self._clock()
        with self._state_lock:
            self._aggregates.setdefault(key, _AdmissionAggregate()).add(event, observed_at=observed_at)

    def _record_raw(self, event: RpcAdmissionEvent) -> None:
        self._recorder.record(
            kind="telegram.rpc_admission",
            outcome=event.kind.value,
            reason_code=event.reason,
            duration_ms=None if event.wait_seconds is None else event.wait_seconds * _MILLISECONDS_PER_SECOND,
            payload={
                "source": None if event.source is None else event.source.value,
                "service_class": None if event.service_class is None else event.service_class.value,
                "queue_depth": event.queue_depth,
                "total_depth": event.total_depth,
                "active_depth": event.active_depth,
                "total_outstanding": event.total_outstanding,
                **({"demand_kind": event.demand_kind.value} if event.demand_kind is not None else {}),
                **({"acquisition_kind": event.acquisition_kind.value} if event.acquisition_kind is not None else {}),
            },
        )


@dataclass(slots=True)
class _DemandAggregate:
    demand_units: int = 0
    actual_attempts: int = 0
    queue_age_seconds: float | None = None
    freshness_debt_seconds: float | None = None

    def add(
        self,
        *,
        demand_units: int,
        actual_attempts: int,
        queue_age_seconds: float | None,
        freshness_debt_seconds: float | None,
    ) -> None:
        self.demand_units += demand_units
        self.actual_attempts += actual_attempts
        if queue_age_seconds is not None:
            self.queue_age_seconds = max(self.queue_age_seconds or 0.0, queue_age_seconds)
        if freshness_debt_seconds is not None:
            self.freshness_debt_seconds = max(self.freshness_debt_seconds or 0.0, freshness_debt_seconds)

    def merge(self, other: _DemandAggregate) -> None:
        self.add(
            demand_units=other.demand_units,
            actual_attempts=other.actual_attempts,
            queue_age_seconds=other.queue_age_seconds,
            freshness_debt_seconds=other.freshness_debt_seconds,
        )


@dataclass(slots=True)
class _ProfilePairAggregate:
    event_count: int = 0
    actual_attempts: int = 0
    retries: int = 0
    pair_ready_count: int = 0
    readiness_latency_total_ms: float = 0.0
    readiness_latency_count: int = 0
    reused_age_max_ms: float = 0.0
    reused_age_count: int = 0

    def add(
        self,
        *,
        actual_attempts: int,
        retries: int,
        pair_ready: bool,
        pair_readiness_latency_ms: float | None,
        reused_age_ms: float | None,
    ) -> None:
        self.event_count += 1
        self.actual_attempts += actual_attempts
        self.retries += retries
        self.pair_ready_count += int(pair_ready)
        if pair_readiness_latency_ms is not None:
            self.readiness_latency_total_ms += pair_readiness_latency_ms
            self.readiness_latency_count += 1
        if reused_age_ms is not None:
            self.reused_age_max_ms = max(self.reused_age_max_ms, reused_age_ms)
            self.reused_age_count += 1

    def merge(self, other: _ProfilePairAggregate) -> None:
        self.event_count += other.event_count
        self.actual_attempts += other.actual_attempts
        self.retries += other.retries
        self.pair_ready_count += other.pair_ready_count
        self.readiness_latency_total_ms += other.readiness_latency_total_ms
        self.readiness_latency_count += other.readiness_latency_count
        self.reused_age_max_ms = max(self.reused_age_max_ms, other.reused_age_max_ms)
        self.reused_age_count += other.reused_age_count


def _demand_payload(
    *,
    demand_kind: DemandKind,
    acquisition_kind: AcquisitionKind | None,
    aggregate: _DemandAggregate,
    window_seconds: float,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "demand_kind": demand_kind.value,
        "demand_units": aggregate.demand_units,
        "actual_attempts": aggregate.actual_attempts,
        "window_seconds": window_seconds,
    }
    if acquisition_kind is not None:
        payload["acquisition_kind"] = acquisition_kind.value
    if aggregate.queue_age_seconds is not None:
        payload["queue_age_seconds"] = aggregate.queue_age_seconds
    if aggregate.freshness_debt_seconds is not None:
        payload["freshness_debt_seconds"] = aggregate.freshness_debt_seconds
    return payload


_PROFILE_MODES = frozenset({"enabled", "disabled", "observe_only"})
_PROFILE_OUTCOMES = frozenset({"committed", "reused", "failed", "stale_writer_rejected", "section_committed"})
_PROFILE_SECTION_OUTCOMES = frozenset({"usable", "partial", "absent", "unavailable"})
_PROFILE_REUSE_REASONS = frozenset(
    {
        "missing_identity",
        "missing_receipt",
        "outcome_not_reusable",
        "identity_mismatch",
        "materialization_mismatch",
        "invalid_observation",
        "stale",
        "section_not_fresh",
        "cursor_mismatch",
        "ineligible_pair",
        "auth_scope_changed",
    }
)


def _validate_profile_observation(  # noqa: PLR0913 - explicit bounded telemetry contract
    *,
    mode: str,
    eligible_pair: bool,
    outcome: str,
    actual_attempts: int,
    retries: int,
    full_profile_outcome: str | None,
    personal_channel_outcome: str | None,
    pair_ready: bool,
    pair_readiness_latency_ms: float | None,
    local_satisfaction: bool,
    prevented_request: bool,
    reuse_rejection_reason: str | None,
    reused_age_ms: float | None,
    stale_writer_rejected: bool,
    measurement_complete: bool,
) -> _ProfileObservationValues:
    if mode not in _PROFILE_MODES or outcome not in _PROFILE_OUTCOMES:
        raise ValueError("profile telemetry mode or outcome is invalid")
    _validate_profile_booleans(
        eligible_pair=eligible_pair,
        pair_ready=pair_ready,
        local_satisfaction=local_satisfaction,
        prevented_request=prevented_request,
        stale_writer_rejected=stale_writer_rejected,
        measurement_complete=measurement_complete,
    )
    _validate_profile_counters(actual_attempts=actual_attempts, retries=retries)
    _validate_profile_numbers(
        pair_readiness_latency_ms=pair_readiness_latency_ms,
        reused_age_ms=reused_age_ms,
    )
    _validate_profile_outcomes(
        full_profile_outcome=full_profile_outcome,
        personal_channel_outcome=personal_channel_outcome,
        reuse_rejection_reason=reuse_rejection_reason,
    )
    return {
        "mode": mode,
        "eligible_pair": eligible_pair,
        "outcome": outcome,
        "actual_attempts": actual_attempts,
        "retries": retries,
        "full_profile_outcome": full_profile_outcome,
        "personal_channel_outcome": personal_channel_outcome,
        "pair_ready": pair_ready,
        "pair_readiness_latency_ms": pair_readiness_latency_ms,
        "local_satisfaction": local_satisfaction,
        "prevented_request": prevented_request,
        "reuse_rejection_reason": reuse_rejection_reason,
        "reused_age_ms": reused_age_ms,
        "stale_writer_rejected": stale_writer_rejected,
        "measurement_complete": measurement_complete,
    }


def _validate_profile_booleans(  # noqa: PLR0913 - explicit bounded telemetry contract
    *,
    eligible_pair: bool,
    pair_ready: bool,
    local_satisfaction: bool,
    prevented_request: bool,
    stale_writer_rejected: bool,
    measurement_complete: bool,
) -> None:
    for value, name in (
        (eligible_pair, "eligible_pair"),
        (pair_ready, "pair_ready"),
        (local_satisfaction, "local_satisfaction"),
        (prevented_request, "prevented_request"),
        (stale_writer_rejected, "stale_writer_rejected"),
        (measurement_complete, "measurement_complete"),
    ):
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be a boolean")


def _validate_profile_counters(*, actual_attempts: int, retries: int) -> None:
    _validate_nonnegative_int(actual_attempts, "actual_attempts")
    _validate_nonnegative_int(retries, "retries")
    if retries > actual_attempts:
        raise ValueError("retries cannot exceed actual_attempts")


def _validate_profile_numbers(*, pair_readiness_latency_ms: float | None, reused_age_ms: float | None) -> None:
    for value, name in (
        (pair_readiness_latency_ms, "pair_readiness_latency_ms"),
        (reused_age_ms, "reused_age_ms"),
    ):
        _validate_optional_nonnegative_number(value, name)


def _validate_profile_outcomes(
    *,
    full_profile_outcome: str | None,
    personal_channel_outcome: str | None,
    reuse_rejection_reason: str | None,
) -> None:
    for value, name in (
        (full_profile_outcome, "full_profile_outcome"),
        (personal_channel_outcome, "personal_channel_outcome"),
    ):
        if value is not None and value not in _PROFILE_SECTION_OUTCOMES:
            raise ValueError(f"{name} is invalid")
    if reuse_rejection_reason is not None and reuse_rejection_reason not in _PROFILE_REUSE_REASONS:
        raise ValueError("reuse_rejection_reason is invalid")


def _is_finite(value: int | float) -> bool:
    return value == value and value not in {float("inf"), float("-inf")}


def _validate_nonnegative_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _validate_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _validate_optional_nonnegative_number(value: float | None, name: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not _is_finite(value)
    ):
        raise ValueError(f"{name} must be a finite non-negative number")


def _normalize_demand_outcome(value: DemandEvidenceOutcome | str) -> DemandEvidenceOutcome:
    try:
        outcome = DemandEvidenceOutcome(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported demand evidence outcome: {value}") from exc
    if outcome not in _DEMAND_EVIDENCE_OUTCOMES:
        raise ValueError(f"unsupported demand evidence outcome: {value}")
    return outcome


def _normalize_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise TypeError("reason must be a string")
    normalized = reason.strip()
    return normalized[:64] if normalized else None


__all__ = [
    "DemandEvidenceOutcome",
    "DemandObservationHook",
    "ObservationRecorder",
    "RpcAdmissionObservationAggregator",
    "RpcAdmissionObservationPolicy",
]
