"""Bounded aggregation for high-volume Telegram RPC admission observations."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .telegram_rpc_scheduler import RpcAdmissionEvent, RpcAdmissionEventKind, RpcServiceClass, TelegramRpcSource


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


class RpcAdmissionObservationAggregator:
    """Coalesce routine queue traffic while preserving terminal outcomes raw."""

    def __init__(
        self,
        recorder: ObservationRecorder,
        *,
        summary_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if summary_interval_seconds <= 0:
            raise ValueError("summary_interval_seconds must be positive")
        self._recorder = recorder
        self._summary_interval_seconds = summary_interval_seconds
        self._clock = clock
        self._last_flush_at = clock()
        self._aggregates: dict[tuple[TelegramRpcSource, RpcServiceClass], _AdmissionAggregate] = {}

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

    def flush_if_due(self) -> None:
        now = self._clock()
        if now - self._last_flush_at >= self._summary_interval_seconds:
            self.flush(now=now)

    def flush(self, *, now: float | None = None) -> None:
        """Persist and clear the current bounded aggregate window."""
        flush_at = self._clock() if now is None else now
        aggregates, self._aggregates = self._aggregates, {}
        self._last_flush_at = flush_at
        for (source, service_class), aggregate in aggregates.items():
            dispatched = aggregate.dispatched_count
            average_wait_ms = aggregate.wait_total_seconds * 1000 / dispatched if dispatched else None
            self._recorder.record(
                kind="telegram.rpc_admission",
                outcome="summary",
                duration_ms=average_wait_ms,
                result_count=dispatched,
                payload={
                    "source": source.value,
                    "service_class": service_class.value,
                    "queued_count": aggregate.queued_count,
                    "dispatched_count": dispatched,
                    "max_wait_ms": aggregate.wait_max_seconds * 1000,
                    "queue_depth_max": aggregate.queue_depth_max,
                    "total_depth_max": aggregate.total_depth_max,
                    "active_depth_max": aggregate.active_depth_max,
                    "total_outstanding_max": aggregate.total_outstanding_max,
                    "window_seconds": self._summary_interval_seconds,
                },
            )

    def _aggregate(self, event: RpcAdmissionEvent) -> None:
        if event.source is None or event.service_class is None:
            self._record_raw(event)
            return
        key = (event.source, event.service_class)
        self._aggregates.setdefault(key, _AdmissionAggregate()).add(event)

    def _record_raw(self, event: RpcAdmissionEvent) -> None:
        self._recorder.record(
            kind="telegram.rpc_admission",
            outcome=event.kind.value,
            reason_code=event.reason,
            duration_ms=None if event.wait_seconds is None else event.wait_seconds * 1000,
            payload={
                "source": None if event.source is None else event.source.value,
                "service_class": None if event.service_class is None else event.service_class.value,
                "queue_depth": event.queue_depth,
                "total_depth": event.total_depth,
                "active_depth": event.active_depth,
                "total_outstanding": event.total_outstanding,
            },
        )


__all__ = ["ObservationRecorder", "RpcAdmissionObservationAggregator"]
