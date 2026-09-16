"""Task-local timing evidence for one daemon request.

This is deliberately a small application seam for the request observability
contract.  It is not a tracing tree: each named boundary is measured in
isolation and nested measurements are retained as nested evidence.
"""

from __future__ import annotations

import contextvars
import math
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType

TIMING_KIND = "daemon.request_timing"
TIMING_VERSION = 1
_MAX_OPERATION_ID_LENGTH = 64
_MAX_RPC_ATTEMPTS = 1000
_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{8}$")
TIMING_PHASES = (
    "resolution",
    "local_projection",
    "telegram_fallback",
    "rpc_admission",
    "rpc_execution",
    "response_shape",
)
TIMING_TOP_LEVEL_PHASES = ("resolution", "local_projection", "telegram_fallback", "response_shape")
TIMING_NESTED_LEAF_PHASES = ("rpc_admission", "rpc_execution")
TIMING_NESTED_PARENTS = ("resolution", "telegram_fallback")
TIMING_REQUIRED_PHASES_BY_ROUTE = MappingProxyType(
    {
        "local_history": ("resolution", "local_projection", "response_shape"),
        "local_context": ("resolution", "local_projection", "response_shape"),
        "local_non_sent_state": ("resolution", "local_projection", "response_shape"),
        "telegram_fallback": ("resolution", "telegram_fallback", "response_shape"),
        "telegram_context_fallback": TIMING_TOP_LEVEL_PHASES,
        "telegram_topic_fallback": TIMING_TOP_LEVEL_PHASES,
    }
)
TIMING_ROUTES = frozenset(
    {
        "local_history",
        "local_context",
        "local_non_sent_state",
        "telegram_fallback",
        "telegram_context_fallback",
        "telegram_topic_fallback",
    }
)
TIMING_SERVED_SOURCES = frozenset({"local", "telegram", "error", "unknown"})

_current: contextvars.ContextVar[DaemonRequestTiming | None] = contextvars.ContextVar(
    "daemon_request_timing", default=None
)
_GENERATED_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32,64}$")


def _duration_ms(started_at: float, ended_at: float | None = None) -> float:
    value = (time.monotonic() if ended_at is None else ended_at) - started_at
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value * 1000


def _required_phases(route: str | None) -> tuple[str, ...]:
    return () if route is None else TIMING_REQUIRED_PHASES_BY_ROUTE.get(route, ())


def attribution_from_counts(measured: int, required: int) -> str:
    """Classify attribution from the canonical measured/required phase counts."""
    if measured <= 0 or required <= 0:
        return "unavailable"
    if measured == required:
        return "complete"
    if measured < required:
        return "partial"
    return "unavailable"


def _phase_attribution(required: tuple[str, ...], phases: dict[str, float]) -> tuple[str, int]:
    measured = sum(phase in phases for phase in required)
    return attribution_from_counts(measured, len(required)), measured


def _unattributed_duration(
    total_duration_ms: float,
    required: tuple[str, ...],
    phases: dict[str, float],
    attribution: str,
) -> float | None:
    if attribution != "complete":
        return None
    remainder = total_duration_ms - sum(phases[phase] for phase in required)
    return remainder if math.isfinite(remainder) and remainder >= 0 else None


def _nested_payload(nested_phases: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    return {
        parent: {f"{phase}_ms": duration for phase, duration in phases.items() if phase in TIMING_NESTED_LEAF_PHASES}
        for parent, phases in nested_phases.items()
        if parent in TIMING_NESTED_PARENTS
    }


@dataclass(slots=True)
class DaemonRequestTiming:
    """Bounded evidence accumulated by one daemon request task."""

    operation_id: str
    request_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    route_attempted: str | None = None
    served_source: str | None = None
    phases: dict[str, float] = field(default_factory=dict)
    nested_phases: dict[str, dict[str, float]] = field(default_factory=dict)
    _active_phases: list[str] = field(default_factory=list, repr=False)
    fallback_attempted: bool = False
    rpc_attempts: int = 0
    attempts_capped: bool = False

    def set_route(self, route: str) -> None:
        if route in TIMING_ROUTES:
            self.route_attempted = route

    def set_served_source(self, source: str) -> None:
        if source in TIMING_SERVED_SOURCES:
            self.served_source = source

    def mark_fallback(self, route: str = "telegram_fallback") -> None:
        self.set_route(route)
        self.fallback_attempted = True

    def add_phase(self, phase: str, duration_ms: float) -> None:
        if phase not in TIMING_PHASES:
            return
        if not math.isfinite(duration_ms) or duration_ms < 0:
            return
        self.phases[phase] = self.phases.get(phase, 0.0) + duration_ms

    @contextmanager
    def phase(self, phase: str) -> Iterator[None]:
        started_at = time.monotonic()
        self._active_phases.append(phase)
        try:
            yield
        finally:
            duration_ms = _duration_ms(started_at)
            self.add_phase(phase, duration_ms)
            if phase in TIMING_NESTED_LEAF_PHASES:
                parent = next(
                    (
                        candidate
                        for candidate in reversed(self._active_phases[:-1])
                        if candidate in TIMING_NESTED_PARENTS
                    ),
                    None,
                )
                if parent is not None and duration_ms >= 0:
                    nested = self.nested_phases.setdefault(parent, {})
                    nested[phase] = nested.get(phase, 0.0) + duration_ms
            self._active_phases.pop()

    def record_rpc_attempt(self) -> None:
        if self.rpc_attempts >= _MAX_RPC_ATTEMPTS:
            self.attempts_capped = True
            return
        self.rpc_attempts = min(self.rpc_attempts + 1, _MAX_RPC_ATTEMPTS)

    def payload(self) -> dict[str, object]:
        """Return the closed, content-free timing payload."""
        total_duration_ms = self.duration_ms()
        route_attempted = (
            self.route_attempted
            if isinstance(self.route_attempted, str) and self.route_attempted in TIMING_ROUTES
            else None
        )
        served_source = (
            self.served_source
            if isinstance(self.served_source, str) and self.served_source in TIMING_SERVED_SOURCES
            else None
        )
        request_id = (
            self.request_id if isinstance(self.request_id, str) and _REQUEST_ID_RE.fullmatch(self.request_id) else None
        )
        required_phases = _required_phases(route_attempted)
        attribution, measured_required_phase_count = _phase_attribution(required_phases, self.phases)
        unattributed_ms = _unattributed_duration(total_duration_ms, required_phases, self.phases, attribution)
        not_applicable = (
            [phase for phase in TIMING_TOP_LEVEL_PHASES if phase not in required_phases]
            if route_attempted is not None
            else []
        )
        return {
            "version": TIMING_VERSION,
            "phase_model": "top_level_with_nested_rpc",
            "request_id": request_id,
            "route_attempted": route_attempted,
            "served_source": served_source,
            "resolution_ms": self.phases.get("resolution"),
            "local_projection_ms": self.phases.get("local_projection"),
            "telegram_fallback_ms": self.phases.get("telegram_fallback"),
            "rpc_admission_ms": self.phases.get("rpc_admission"),
            "rpc_execution_ms": self.phases.get("rpc_execution"),
            "response_shape_ms": self.phases.get("response_shape"),
            "rpc_attempts": self.rpc_attempts,
            "attempts_capped": self.attempts_capped,
            "fallback_attempted": self.fallback_attempted,
            "measured_required_phase_count": measured_required_phase_count,
            "required_phase_count": len(required_phases),
            "not_applicable_phases": not_applicable,
            "nested_phases": _nested_payload(self.nested_phases),
            "unattributed_ms": unattributed_ms,
        }

    def duration_ms(self) -> float:
        return _duration_ms(self.started_at)


@contextmanager
def timing_context(operation_id: str | None, request_id: str | None = None) -> Iterator[DaemonRequestTiming | None]:
    """Bind a fresh timing accumulator and always restore the parent state."""
    if operation_id is None:
        token = _current.set(None)
        try:
            yield None
        finally:
            _current.reset(token)
        return
    timing = DaemonRequestTiming(operation_id=operation_id, request_id=request_id)
    token = _current.set(timing)
    try:
        yield timing
    finally:
        _current.reset(token)


def current_timing() -> DaemonRequestTiming | None:
    """Return timing state for the current daemon operation, if any."""
    return _current.get()


@contextmanager
def timing_phase(phase: str) -> Iterator[None]:
    """Measure a named phase when a daemon request is active."""
    timing = current_timing()
    if timing is None:
        yield
        return
    with timing.phase(phase):
        yield


def standalone_operation_id(value: object) -> str:
    """Accept a bounded opaque ID or generate a root for standalone daemon calls."""
    if (
        isinstance(value, str)
        and len(value) <= _MAX_OPERATION_ID_LENGTH
        and _GENERATED_OPERATION_ID_RE.fullmatch(value)
    ):
        return value
    return uuid.uuid4().hex


def standalone_request_id(value: object) -> str | None:
    """Preserve a generated daemon request ID, or leave an absent one unavailable."""
    if value is None:
        return None
    if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value):
        return value
    return None


def is_valid_request_id(value: object) -> bool:
    """Return whether a supplied daemon request ID matches the generated shape."""
    return isinstance(value, str) and bool(_REQUEST_ID_RE.fullmatch(value))


def is_valid_operation_id(value: object) -> bool:
    """Return whether a supplied operation ID matches the generated shape."""
    return (
        isinstance(value, str)
        and len(value) <= _MAX_OPERATION_ID_LENGTH
        and bool(_GENERATED_OPERATION_ID_RE.fullmatch(value))
    )


__all__ = [
    "TIMING_KIND",
    "TIMING_NESTED_LEAF_PHASES",
    "TIMING_NESTED_PARENTS",
    "TIMING_PHASES",
    "TIMING_REQUIRED_PHASES_BY_ROUTE",
    "TIMING_ROUTES",
    "TIMING_SERVED_SOURCES",
    "TIMING_TOP_LEVEL_PHASES",
    "DaemonRequestTiming",
    "attribution_from_counts",
    "current_timing",
    "is_valid_operation_id",
    "is_valid_request_id",
    "standalone_operation_id",
    "standalone_request_id",
    "timing_context",
    "timing_phase",
]
