"""Bounded aggregation for high-volume Telegram RPC admission observations."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

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


_DEMAND_EVIDENCE_OUTCOMES = frozenset(DemandEvidenceOutcome)


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

    def flush_if_due(self) -> None:
        now = self._clock()
        with self._state_lock:
            due = now - self._last_flush_at >= self._summary_interval_seconds
        if due:
            self._try_flush(now=now)

    async def run_periodic_flush(self, shutdown_event: asyncio.Event) -> None:
        """Flush quiet windows on the configured cadence until shutdown."""
        while not shutdown_event.is_set():
            try:
                async with asyncio.timeout(self._summary_interval_seconds):
                    await shutdown_event.wait()
            except TimeoutError:
                self.flush()

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
        for (source, service_class, demand_kind, acquisition_kind), aggregate in aggregates.items():
            dispatched = aggregate.dispatched_count
            average_wait_ms = (
                aggregate.wait_total_seconds * _MILLISECONDS_PER_SECOND / dispatched if dispatched else None
            )
            try:
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
                    "window_seconds": self._summary_interval_seconds,
                }
                if demand_kind is not None:
                    payload["demand_kind"] = demand_kind.value
                    payload["actual_attempts"] = dispatched
                if acquisition_kind is not None:
                    payload["acquisition_kind"] = acquisition_kind.value
                self._recorder.record(
                    kind="telegram.rpc_admission",
                    outcome="summary",
                    duration_ms=average_wait_ms,
                    result_count=dispatched,
                    payload=payload,
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
    "RpcAdmissionObservationAggregator",
    "RpcAdmissionObservationPolicy",
]
