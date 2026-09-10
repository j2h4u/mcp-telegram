"""Bounded aggregation for high-volume Telegram RPC admission observations."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypedDict, cast

from .telegram_demand import AcquisitionKind
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import RpcAdmissionEvent, RpcAdmissionEventKind, RpcServiceClass, TelegramRpcSource

logger = logging.getLogger(__name__)
_MILLISECONDS_PER_SECOND = 1_000


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
    ) -> None: ...


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


class DemandObservationHook(Protocol):
    """Narrow observer seam the durable coordinator may call after a slice."""

    def observe_demand(  # noqa: PLR0913 - this is the stable telemetry boundary
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
    ) -> None: ...


# Compatibility name for composition code that still refers to demand evidence.
DemandEvidenceObserver = DemandObservationHook


class ProfilePairObservationHook(Protocol):
    """Best-effort aggregate observer for Entity Profile pair lifecycles."""

    def observe_profile_pair(  # noqa: PLR0913 - this is the privacy-safe boundary
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
    ) -> None: ...


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

    def add(self, event: RpcAdmissionEvent) -> None:
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


class RpcAdmissionObservationAggregator:
    """Coalesce routine queue traffic while preserving terminal outcomes raw."""

    def __init__(
        self,
        recorder: ObservationRecorder,
        *,
        policy: RpcAdmissionObservationPolicy,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        summary_interval_seconds = policy.rpc_summary_interval_seconds
        if summary_interval_seconds <= 0:
            raise ValueError("summary_interval_seconds must be positive")
        self._recorder = recorder
        self._summary_interval_seconds = summary_interval_seconds
        self._clock = clock
        self._last_flush_at = clock()
        self._aggregates: dict[
            tuple[
                TelegramRpcSource,
                RpcServiceClass,
                DemandKind | None,
                AcquisitionKind | None,
            ],
            _AdmissionAggregate,
        ] = {}
        self._demand_aggregates: dict[
            tuple[DemandKind, AcquisitionKind | None, DemandEvidenceOutcome, str | None],
            _DemandAggregate,
        ] = {}
        self._profile_aggregates: dict[tuple[object, ...], _ProfilePairAggregate] = {}
        self._state_lock = threading.Lock()
        self._flush_lock = threading.Lock()

    def observe(self, event: RpcAdmissionEvent) -> None:
        """Record one scheduler event without blocking its admission path."""
        try:
            if event.kind in {RpcAdmissionEventKind.QUEUED, RpcAdmissionEventKind.DISPATCHED}:
                self._aggregate(event)
            else:
                self._record_raw(event)
            self.flush_if_due()
        except Exception:  # noqa: BLE001 - telemetry cannot break scheduler admission
            # Operational telemetry remains best effort at the scheduler callback boundary.
            return

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

    def _flush_locked(self, *, now: float | None) -> None:  # noqa: PLR0912, PLR0914, PLR0915
        """Persist one snapshot while serialising source/class summaries."""
        flush_at = self._clock() if now is None else now
        with self._state_lock:
            aggregates, self._aggregates = self._aggregates, {}
            demand_aggregates, self._demand_aggregates = self._demand_aggregates, {}
            self._last_flush_at = flush_at
        for (source, service_class, demand_kind, acquisition_kind), aggregate in aggregates.items():
            dispatched = aggregate.dispatched_count
            average_wait_ms = (
                aggregate.wait_total_seconds * _MILLISECONDS_PER_SECOND / dispatched if dispatched else None
            )
            try:
                admission_payload: dict[str, object] = {
                    "source": source.value,
                    "service_class": service_class.value,
                    "queued_count": aggregate.queued_count,
                    "dispatched_count": dispatched,
                    "max_wait_ms": aggregate.wait_max_seconds * _MILLISECONDS_PER_SECOND,
                    "queue_depth_max": aggregate.queue_depth_max,
                    "total_depth_max": aggregate.total_depth_max,
                    "active_depth_max": aggregate.active_depth_max,
                    "total_outstanding_max": aggregate.total_outstanding_max,
                    "window_seconds": self._summary_interval_seconds,
                }
                if demand_kind is not None:
                    admission_payload["demand_kind"] = demand_kind.value
                    admission_payload["actual_attempts"] = dispatched
                if acquisition_kind is not None:
                    admission_payload["acquisition_kind"] = acquisition_kind.value
                self._recorder.record(
                    kind="telegram.rpc_admission",
                    outcome="summary",
                    duration_ms=average_wait_ms,
                    result_count=dispatched,
                    payload=admission_payload,
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
        for (
            demand_kind,
            acquisition_kind,
            outcome,
            reason,
        ), demand_aggregate in demand_aggregates.items():
            try:
                payload = _demand_payload(
                    demand_kind=demand_kind,
                    acquisition_kind=acquisition_kind,
                    aggregate=demand_aggregate,
                    window_seconds=self._summary_interval_seconds,
                )
                self._recorder.record(
                    kind="telegram.demand",
                    outcome=outcome.value,
                    reason_code=reason,
                    result_count=None,
                    payload=payload,
                )
            except Exception:
                with self._state_lock:
                    self._demand_aggregates.setdefault(
                        (demand_kind, acquisition_kind, outcome, reason),
                        _DemandAggregate(),
                    ).merge(demand_aggregate)
                logger.exception(
                    "demand_observation_flush_failed demand_kind=%s acquisition_kind=%s outcome=%s",
                    demand_kind.value,
                    None if acquisition_kind is None else acquisition_kind.value,
                    outcome.value,
                )
        with self._state_lock:
            profile_aggregates, self._profile_aggregates = self._profile_aggregates, {}
        for key, aggregate in profile_aggregates.items():
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
            try:
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
            except Exception:  # noqa: BLE001 - telemetry cannot affect profile correctness
                with self._state_lock:
                    self._profile_aggregates.setdefault(key, _ProfilePairAggregate()).merge(aggregate)
                logger.debug("entity_profile_pair_summary_flush_failed")

    def _aggregate(self, event: RpcAdmissionEvent) -> None:
        if event.source is None or event.service_class is None:
            self._record_raw(event)
            return
        key = (event.source, event.service_class, event.demand_kind, event.acquisition_kind)
        with self._state_lock:
            self._aggregates.setdefault(key, _AdmissionAggregate()).add(event)

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
    _validate_nonnegative_int(actual_attempts, "actual_attempts")
    _validate_nonnegative_int(retries, "retries")
    for value, name in (
        (pair_readiness_latency_ms, "pair_readiness_latency_ms"),
        (reused_age_ms, "reused_age_ms"),
    ):
        _validate_optional_nonnegative_number(value, name)
    for value, name in (
        (full_profile_outcome, "full_profile_outcome"),
        (personal_channel_outcome, "personal_channel_outcome"),
    ):
        if value is not None and value not in _PROFILE_SECTION_OUTCOMES:
            raise ValueError(f"{name} is invalid")
    if reuse_rejection_reason is not None and reuse_rejection_reason not in _PROFILE_REUSE_REASONS:
        raise ValueError("reuse_rejection_reason is invalid")
    if retries > actual_attempts:
        raise ValueError("retries cannot exceed actual_attempts")
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
    "DemandEvidenceObserver",
    "DemandEvidenceOutcome",
    "DemandObservationHook",
    "ObservationRecorder",
    "ProfilePairObservationHook",
    "RpcAdmissionObservationAggregator",
    "RpcAdmissionObservationPolicy",
]
