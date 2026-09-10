"""Single process-wide executor for durable Telegram demand."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from types import MappingProxyType

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_demand import (
    DemandStatus,
    DemandToken,
    DurableDemandAdapter,
    RpcAttemptBudget,
    demand_context,
)
from mcp_telegram.telegram_rpc_consumers import (
    DURABLE_DEMAND_ORDER,
    TELEGRAM_DEMAND_CONTRACTS,
    DemandContract,
    DemandKind,
    ExecutionMode,
    demand_contract,
)
from mcp_telegram.telegram_rpc_scheduler import RpcAdmissionClosedError, TelegramRpcAdmissionDeferred

logger = logging.getLogger(__name__)

DEFAULT_SAFETY_SCAN_SECONDS = 60.0
MIN_WAIT_SECONDS = 0.001


class CoordinatorState(StrEnum):
    """Lifecycle states for the one coordinator task."""

    NEW = "new"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


def _durable_kinds(contracts: Mapping[DemandKind, DemandContract]) -> frozenset[DemandKind]:
    return frozenset(kind for kind, contract in contracts.items() if contract.execution_mode is ExecutionMode.DURABLE)


def validate_durable_adapters(
    adapters: Mapping[DemandKind, DurableDemandAdapter],
    *,
    contracts: Mapping[DemandKind, DemandContract] = TELEGRAM_DEMAND_CONTRACTS,
) -> None:
    """Fail startup unless the complete explicit durable order has adapters."""
    expected = set(DURABLE_DEMAND_ORDER)
    _validate_adapter_keys(adapters)
    _validate_durable_contracts(contracts, expected)
    _validate_adapter_coverage(adapters, expected)
    for kind in DURABLE_DEMAND_ORDER:
        _validate_adapter_contract(kind, adapters[kind], contracts)


def _validate_adapter_keys(adapters: Mapping[DemandKind, DurableDemandAdapter]) -> None:
    if any(not isinstance(kind, DemandKind) for kind in adapters):
        raise TypeError("durable adapter keys must be DemandKind values")


def _validate_durable_contracts(contracts: Mapping[DemandKind, DemandContract], expected: set[DemandKind]) -> None:
    if _durable_kinds(contracts) != expected:
        raise RuntimeError("durable demand contracts do not match the explicit durable demand order")


def _validate_adapter_coverage(adapters: Mapping[DemandKind, DurableDemandAdapter], expected: set[DemandKind]) -> None:
    actual = set(adapters)
    if actual == expected:
        return
    missing = sorted(kind.value for kind in expected - actual)
    unexpected = sorted(kind.value for kind in actual - expected)
    raise RuntimeError(f"durable adapter coverage mismatch: missing={missing}, unexpected={unexpected}")


def _validate_adapter_contract(
    kind: DemandKind,
    adapter: DurableDemandAdapter,
    contracts: Mapping[DemandKind, DemandContract],
) -> None:
    if not callable(getattr(adapter, "status", None)) or not callable(getattr(adapter, "run_slice", None)):
        raise TypeError(f"durable adapter {kind.value} must implement status() and run_slice()")
    limit = contracts[kind].max_rpc_attempts_per_slice
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise RuntimeError(f"durable demand {kind.value} has no positive slice budget")


class TelegramDemandCoordinator:
    """Run one bounded durable slice at a time until shutdown."""

    def __init__(
        self,
        adapters: Mapping[DemandKind, DurableDemandAdapter],
        shutdown_event: asyncio.Event | None = None,
        *,
        clock: Callable[[], float] = time.time,
        safety_scan_seconds: float = DEFAULT_SAFETY_SCAN_SECONDS,
        observer: object | None = None,
    ) -> None:
        validate_durable_adapters(adapters)
        if not math.isfinite(safety_scan_seconds) or safety_scan_seconds <= 0:
            raise ValueError("safety_scan_seconds must be finite and positive")
        self._adapters: Mapping[DemandKind, DurableDemandAdapter] = MappingProxyType(dict(adapters))
        self._shutdown_event = shutdown_event or asyncio.Event()
        self._clock = clock
        self._safety_scan_seconds = float(safety_scan_seconds)
        self._observer = observer
        self._wake = asyncio.Event()
        self._state = CoordinatorState.NEW
        self._queue = deque[DemandKind]()
        self._queued: set[DemandKind] = set()
        self._queued_since: dict[DemandKind, float] = {}
        self._statuses: dict[DemandKind, DemandStatus] = {}
        self._authoritative_ready: set[DemandKind] = set()
        self._offered: set[DemandKind] = set()
        self._active_kind: DemandKind | None = None
        self._next_release_at: float | None = None
        self._status_failed_until: dict[DemandKind, float] = {}
        self._suppressed_until: dict[DemandKind, float] = {}
        self._global_release_at: float | None = None
        self._run_task: asyncio.Task[None] | None = None
        self.scan()

    @property
    def state(self) -> CoordinatorState:
        return self._state

    @property
    def ready_kinds(self) -> tuple[DemandKind, ...]:
        return tuple(self._queue)

    @property
    def queued_kinds(self) -> tuple[DemandKind, ...]:
        return self.ready_kinds

    @property
    def active_kind(self) -> DemandKind | None:
        return self._active_kind

    @property
    def offered_kinds(self) -> frozenset[DemandKind]:
        return frozenset(self._offered)

    @property
    def statuses(self) -> Mapping[DemandKind, DemandStatus]:
        return MappingProxyType(dict(self._statuses))

    @property
    def authoritative_ready_kinds(self) -> tuple[DemandKind, ...]:
        return tuple(kind for kind in DURABLE_DEMAND_ORDER if kind in self._authoritative_ready)

    @property
    def next_release_at(self) -> float | None:
        return self._next_release_at

    def shutdown(self) -> None:
        """Request a prompt stop for a coordinator that owns its event."""
        self._shutdown_event.set()
        self._wake.set()

    @contextmanager
    def protocol_scope(self, kind: DemandKind) -> Iterator[DemandToken]:
        """Attribute Telethon-owned recovery without owning its lifecycle."""
        contract = demand_contract(kind)
        if contract.execution_mode is not ExecutionMode.PROTOCOL:
            raise RuntimeError(f"{kind.value} is not registered for protocol execution")
        with demand_context(kind) as token:
            yield token

    def offer(self, kind: DemandKind) -> bool:
        """Record a post-commit wakeup and coalesce repeats until the next scan."""
        self._validate_durable_kind(kind)
        if self._state in {CoordinatorState.STOPPING, CoordinatorState.STOPPED}:
            return False
        if kind in self._offered or kind in self._queued or kind is self._active_kind:
            return False
        accepted = True
        self.scan(clear_offers=False)
        self._offered.add(kind)
        self._wake.set()
        return accepted

    def scan(self, *, now: float | None = None, clear_offers: bool = True) -> tuple[DemandKind, ...]:
        """Perform one complete authoritative status scan in stable order."""
        observed_at = self._now() if now is None else self._validate_now(now)
        if clear_offers:
            self._offered.clear()
        for kind in DURABLE_DEMAND_ORDER:
            self._scan_kind(kind, observed_at)
        self._recompute_next_release(observed_at)
        return self.ready_kinds

    def _scan_kind(self, kind: DemandKind, observed_at: float) -> None:
        if observed_at < self._status_failed_until.get(kind, -math.inf):
            return
        try:
            status = self._adapters[kind].status(observed_at)
            if status is not None and not isinstance(status, DemandStatus):
                raise TypeError("adapter returned an invalid status")
        except Exception as exc:  # noqa: BLE001 - one bad domain cannot stop the pump
            self._record_status_failure(kind, observed_at, exc)
            return
        self._status_failed_until.pop(kind, None)
        self._apply_status(kind, status, observed_at)

    def _record_status_failure(self, kind: DemandKind, observed_at: float, exc: Exception) -> None:
        self._status_failed_until[kind] = observed_at + self._safety_scan_seconds
        self._statuses.pop(kind, None)
        self._authoritative_ready.discard(kind)
        self._remove_queued(kind)
        logger.warning(
            "telegram_demand_status_failed kind=%s error_type=%s",
            kind.value,
            type(exc).__name__,
        )

    def _apply_status(self, kind: DemandKind, status: DemandStatus | None, observed_at: float) -> None:
        if status is None:
            self._statuses.pop(kind, None)
            self._authoritative_ready.discard(kind)
            self._remove_queued(kind)
            return
        self._statuses[kind] = status
        is_ready = status.is_ready(observed_at) and not self._is_suppressed(kind, observed_at)
        if not is_ready:
            self._authoritative_ready.discard(kind)
            self._remove_queued(kind)
            return
        self._authoritative_ready.add(kind)
        if kind not in self._queued and kind is not self._active_kind:
            self._queue.append(kind)
            self._queued.add(kind)
            self._queued_since[kind] = observed_at

    def timer_scan(self, *, now: float | None = None) -> tuple[DemandKind, ...]:
        """Run the same complete scan used by the timer wakeup."""
        return self.scan(now=now)

    async def run_one_slice(self, *, now: float | None = None) -> DemandKind | None:
        """Execute one selected slice; primarily useful for deterministic tests."""
        if self._state is not CoordinatorState.NEW:
            raise RuntimeError("run_one_slice requires a new coordinator")
        self.scan(now=now)
        kind = self._pop_ready(now=self._now() if now is None else now)
        if kind is None:
            return None
        try:
            await self._execute_slice(kind)
        finally:
            self._active_kind = None
            self.scan(now=self._now())
        return kind

    async def run(self) -> None:
        """Run the critical process-wide durable pump until shutdown."""
        if self._state is not CoordinatorState.NEW:
            raise RuntimeError("Telegram demand coordinator can run only once")
        self._state = CoordinatorState.RUNNING
        self._run_task = asyncio.current_task()
        try:
            while not self._shutdown_event.is_set():
                self._wake.clear()
                now = self._now()
                self.scan(now=now)
                if self._shutdown_event.is_set():
                    break
                kind = self._pop_ready(now=now)
                if kind is None:
                    await self._wait_for_wakeup(self._wait_delay(now))
                    continue
                try:
                    await self._execute_slice(kind)
                finally:
                    self._active_kind = None
                    self.scan(now=self._now())
        except asyncio.CancelledError:
            raise
        finally:
            self._state = CoordinatorState.STOPPING
            self._active_kind = None
            self._run_task = None
            self._state = CoordinatorState.STOPPED

    async def _execute_slice(self, kind: DemandKind) -> None:
        contract = demand_contract(kind)
        limit = contract.max_rpc_attempts_per_slice
        if limit is None:
            raise RuntimeError(f"durable demand {kind.value} has no slice budget")
        budget = RpcAttemptBudget(limit=limit)
        selected_at = self._now()
        queued_at = self._queued_since.pop(kind, selected_at)
        status = self._statuses.get(kind)
        self._active_kind = kind
        self._observe("selected", kind, queue_age_seconds=max(0.0, selected_at - queued_at), status=status)
        try:
            await self._adapters[kind].run_slice(budget)
        except asyncio.CancelledError:
            raise
        except RpcAdmissionClosedError:
            if self._handle_closed_admission(kind, budget):
                return
            raise
        except TelegramRpcAdmissionDeferred as exc:
            self._handle_admission_deferred(kind, budget, exc)
        except TelegramRpcThrottled as exc:
            if self._handle_throttle(kind, budget, exc):
                raise
        except Exception as exc:  # noqa: BLE001 - adapter failure must not kill sibling demand
            self._handle_slice_failure(kind, budget, exc)
        else:
            self._observe("completed", kind, actual_attempts=budget.attempts)

    def _handle_closed_admission(self, kind: DemandKind, budget: RpcAttemptBudget) -> bool:
        if self._shutdown_event.is_set():
            self._observe("deferred", kind, actual_attempts=budget.attempts, reason="shutdown")
            return True
        self._shutdown_event.set()
        self._wake.set()
        return False

    def _handle_admission_deferred(
        self, kind: DemandKind, budget: RpcAttemptBudget, exc: TelegramRpcAdmissionDeferred
    ) -> None:
        self._suppress(kind, exc.retry_after_seconds)
        self._observe("deferred", kind, actual_attempts=budget.attempts, reason="admission_deferred")

    def _handle_throttle(self, kind: DemandKind, budget: RpcAttemptBudget, exc: TelegramRpcThrottled) -> bool:
        if exc.latched:
            self._shutdown_event.set()
            self._wake.set()
            return True
        self._global_release_at = max(self._global_release_at or 0.0, self._now() + (exc.retry_after_seconds or 0))
        self._observe("deferred", kind, actual_attempts=budget.attempts, reason="flood_wait")
        return False

    def _handle_slice_failure(self, kind: DemandKind, budget: RpcAttemptBudget, exc: Exception) -> None:
        self._suppress(kind, self._safety_scan_seconds)
        logger.warning(
            "telegram_demand_slice_failed kind=%s error_type=%s",
            kind.value,
            type(exc).__name__,
        )
        self._observe("failed", kind, actual_attempts=budget.attempts, reason=type(exc).__name__)

    def _pop_ready(self, *, now: float) -> DemandKind | None:
        if self._global_release_at is not None and now < self._global_release_at:
            return None
        if self._global_release_at is not None:
            self._global_release_at = None
        if not self._queue:
            return None
        kind = self._queue.popleft()
        self._queued.remove(kind)
        return kind

    def _suppress(self, kind: DemandKind, seconds: int | float | None) -> None:
        duration = self._safety_scan_seconds if seconds is None else max(0.0, float(seconds))
        self._suppressed_until[kind] = self._now() + duration

    def _is_suppressed(self, kind: DemandKind, now: float) -> bool:
        until = self._suppressed_until.get(kind)
        if until is None or now >= until:
            self._suppressed_until.pop(kind, None)
            return False
        return True

    async def _wait_for_wakeup(self, timeout: float) -> None:
        shutdown_wait = asyncio.create_task(self._shutdown_event.wait())
        wake_wait = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                (shutdown_wait, wake_wait),
                timeout=max(MIN_WAIT_SECONDS, timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (shutdown_wait, wake_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(shutdown_wait, wake_wait, return_exceptions=True)

    def _wait_delay(self, now: float) -> float:
        boundaries = [now + self._safety_scan_seconds]
        if self._next_release_at is not None:
            boundaries.append(self._next_release_at)
        if self._global_release_at is not None:
            boundaries.append(self._global_release_at)
        boundaries.extend(until for until in self._suppressed_until.values() if until > now)
        return max(MIN_WAIT_SECONDS, min(boundaries) - now)

    def _remove_queued(self, kind: DemandKind) -> None:
        if kind not in self._queued:
            return
        self._queued.remove(kind)
        self._queue.remove(kind)
        self._queued_since.pop(kind, None)

    def _recompute_next_release(self, now: float) -> None:
        boundaries = [status.release_at for status in self._statuses.values() if status.release_at > now]
        boundaries.extend(until for until in self._status_failed_until.values() if until > now)
        boundaries.extend(until for until in self._suppressed_until.values() if until > now)
        if self._global_release_at is not None and self._global_release_at > now:
            boundaries.append(self._global_release_at)
        self._next_release_at = min(boundaries, default=None)

    def _observe(  # noqa: PLR0913 - telemetry fields are an explicit bounded schema
        self,
        outcome: str,
        kind: DemandKind,
        *,
        actual_attempts: int = 0,
        queue_age_seconds: float | None = None,
        status: DemandStatus | None = None,
        reason: str | None = None,
    ) -> None:
        observer = self._observer
        if observer is None:
            return
        callback = getattr(observer, "observe_demand", observer if callable(observer) else None)
        if callback is None:
            return
        freshness = None if status is None else status.overdue_seconds(self._now())
        try:
            callback(
                outcome=outcome,
                demand_kind=kind,
                actual_attempts=actual_attempts,
                queue_age_seconds=queue_age_seconds,
                freshness_debt_seconds=freshness if freshness and freshness > 0 else None,
                reason=reason,
            )
        except Exception:
            logger.debug("telegram_demand_observation_failed kind=%s", kind.value, exc_info=True)

    def _validate_durable_kind(self, kind: DemandKind) -> None:
        if not isinstance(kind, DemandKind):
            raise TypeError("kind must be a DemandKind")
        if kind not in self._adapters or demand_contract(kind).execution_mode is not ExecutionMode.DURABLE:
            raise RuntimeError(f"{kind.value} is not registered for durable execution")

    def _now(self) -> float:
        return self._validate_now(self._clock())

    @staticmethod
    def _validate_now(now: float) -> float:
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise ValueError("now must be a finite non-negative timestamp")
        return float(now)


__all__ = ["CoordinatorState", "TelegramDemandCoordinator", "validate_durable_adapters"]
