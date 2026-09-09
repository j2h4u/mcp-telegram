"""Process-local shadow coordinator for durable Telegram demand."""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType

from mcp_telegram.telegram_demand import (
    DemandPrediction,
    DemandStatus,
    DemandToken,
    DurableDemandAdapter,
    RpcAttemptEvidence,
    create_demand_token,
    demand_context,
)
from mcp_telegram.telegram_rpc_consumers import (
    TELEGRAM_DEMAND_CONTRACTS,
    DemandContract,
    DemandKind,
    ExecutionMode,
    demand_contract,
)


def _durable_kinds(contracts: Mapping[DemandKind, DemandContract]) -> frozenset[DemandKind]:
    return frozenset(kind for kind, contract in contracts.items() if contract.execution_mode is ExecutionMode.DURABLE)


def validate_durable_adapters(
    adapters: Mapping[DemandKind, DurableDemandAdapter],
    *,
    contracts: Mapping[DemandKind, DemandContract] = TELEGRAM_DEMAND_CONTRACTS,
) -> None:
    """Fail startup unless every durable kind has exactly one valid adapter."""
    if any(not isinstance(kind, DemandKind) for kind in adapters):
        raise TypeError("durable adapter keys must be DemandKind values")
    expected = _durable_kinds(contracts)
    actual = set(adapters)
    if actual != expected:
        missing = sorted(kind.value for kind in expected - actual)
        unexpected = sorted(kind.value for kind in actual - expected)
        raise RuntimeError(f"durable adapter coverage mismatch: missing={missing}, unexpected={unexpected}")
    for kind, adapter in adapters.items():
        if not callable(getattr(adapter, "status", None)) or not callable(getattr(adapter, "run_slice", None)):
            raise TypeError(f"durable adapter {kind.value} must implement status() and run_slice()")


class TelegramDemandCoordinator:
    """Observe final durable-selection decisions while legacy launchers execute.

    This PR1 coordinator deliberately has no execution method. It may inspect
    adapter status, but it cannot claim domain state or invoke ``run_slice``.
    """

    def __init__(
        self,
        adapters: Mapping[DemandKind, DurableDemandAdapter],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        validate_durable_adapters(adapters)
        self._adapters: Mapping[DemandKind, DurableDemandAdapter] = MappingProxyType(dict(adapters))
        self._clock = clock
        self._ready = deque[DemandKind]()
        self._ready_set: set[DemandKind] = set()
        self._ready_since: dict[DemandKind, float] = {}
        self._held: set[DemandKind] = set()
        self._authoritative_ready: set[DemandKind] = set()
        self._active_cycles: dict[int, tuple[RpcAttemptEvidence, DemandPrediction]] = {}
        self._offered: set[DemandKind] = set()
        self._statuses: dict[DemandKind, DemandStatus] = {}
        self._next_release_at: float | None = None
        self.scan()

    @property
    def ready_kinds(self) -> tuple[DemandKind, ...]:
        """Return the deduplicated FIFO shadow selection."""
        return tuple(self._ready)

    @property
    def offered_kinds(self) -> frozenset[DemandKind]:
        """Return wakeup hints waiting for the next full authoritative scan."""
        return frozenset(self._offered)

    @property
    def statuses(self) -> Mapping[DemandKind, DemandStatus]:
        """Return the latest immutable status snapshot for kinds with work."""
        return MappingProxyType(dict(self._statuses))

    @property
    def authoritative_ready_kinds(self) -> tuple[DemandKind, ...]:
        """Return ready domain kinds, including a head held for comparison."""
        return tuple(kind for kind in DemandKind if kind in self._authoritative_ready)

    @property
    def next_release_at(self) -> float | None:
        """Return the nearest future domain release boundary."""
        return self._next_release_at

    async def run[T](
        self,
        kind: DemandKind,
        operation: Callable[[], Awaitable[T]],
        *,
        deadline: float | None = None,
    ) -> T:
        """Run caller-owned work inside its immutable inline contract."""
        contract = demand_contract(kind)
        if contract.execution_mode is not ExecutionMode.INLINE:
            raise RuntimeError(f"{kind.value} is not registered for inline execution")
        with demand_context(kind, deadline=deadline):
            return await operation()

    @contextmanager
    def protocol_scope(self, kind: DemandKind) -> Iterator[DemandToken]:
        """Attribute Telethon-owned recovery without owning its lifecycle."""
        contract = demand_contract(kind)
        if contract.execution_mode is not ExecutionMode.PROTOCOL:
            raise RuntimeError(f"{kind.value} is not registered for protocol execution")
        with demand_context(kind) as token:
            yield token

    def offer(self, kind: DemandKind) -> bool:
        """Coalesce a durable wakeup hint and refresh its authoritative status."""
        if not isinstance(kind, DemandKind):
            raise TypeError("kind must be a DemandKind")
        if demand_contract(kind).execution_mode is not ExecutionMode.DURABLE:
            raise RuntimeError(f"{kind.value} is not registered for durable execution")
        if kind in self._ready_set or kind in self._held or kind in self._offered:
            return False
        self._offered.add(kind)
        now = self._now()
        self._refresh_kind(kind, now)
        self._recompute_next_release(now)
        return True

    def begin_cycle(self, kind: DemandKind) -> DemandToken:
        """Pop one predicted FIFO head and create the actual cycle root token."""
        if not isinstance(kind, DemandKind):
            raise TypeError("kind must be a DemandKind")
        if demand_contract(kind).execution_mode is not ExecutionMode.DURABLE:
            raise RuntimeError(f"{kind.value} is not registered for durable execution")
        now = self._now()
        predicted_kind = self._ready.popleft() if self._ready else None
        queue_age: float | None = None
        overdue: float | None = None
        if predicted_kind is not None:
            self._ready_set.remove(predicted_kind)
            queued_at = self._ready_since.pop(predicted_kind)
            queue_age = max(0.0, now - queued_at)
            overdue_value = self._statuses[predicted_kind].overdue_seconds(now)
            overdue = overdue_value if overdue_value > 0 else None
            self._held.add(predicted_kind)
        prediction = DemandPrediction(
            predicted_kind=predicted_kind,
            selected_at=now,
            queue_age_seconds=queue_age,
            overdue_seconds=overdue,
        )
        token = create_demand_token(kind, prediction=prediction)
        self._active_cycles[id(token.attempt_evidence)] = (token.attempt_evidence, prediction)
        return token

    def scan(self, *, now: float | None = None) -> tuple[DemandKind, ...]:
        """Reconstruct all shadow readiness from authoritative domain state."""
        observed_at = self._now() if now is None else self._validate_now(now)
        for kind in DemandKind:
            if kind in self._adapters:
                self._refresh_kind(kind, observed_at)
        self._offered.clear()
        self._recompute_next_release(observed_at)
        return self.ready_kinds

    def timer_scan(self, *, now: float | None = None) -> tuple[DemandKind, ...]:
        """Scan all adapters when a timer or periodic safety wakeup fires."""
        return self.scan(now=now)

    def after_cycle_scan(
        self,
        token: DemandToken | None = None,
        *,
        actual_kind: DemandKind | None = None,
        now: float | None = None,
    ) -> tuple[DemandKind, ...]:
        """Rescan after a cycle and rotate its predicted head if still ready."""
        if token is None:
            if actual_kind is not None:
                raise TypeError("actual_kind requires a cycle token")
            return self.scan(now=now)
        cycle_key, prediction = self._require_active_cycle(token, actual_kind)
        observed_at = self._now() if now is None else self._validate_now(now)
        self.scan(now=observed_at)
        self._active_cycles.pop(cycle_key)
        self._release_prediction(prediction, observed_at)
        self._recompute_next_release(observed_at)
        return self.ready_kinds

    def _require_active_cycle(
        self,
        token: DemandToken,
        actual_kind: DemandKind | None,
    ) -> tuple[int, DemandPrediction]:
        if not isinstance(token, DemandToken):
            raise TypeError("token must be a DemandToken")
        if actual_kind is None:
            raise TypeError("actual_kind is required with a cycle token")
        if not isinstance(actual_kind, DemandKind):
            raise TypeError("actual_kind must be a DemandKind")
        if token.kind is not actual_kind:
            raise ValueError("actual_kind conflicts with the cycle root token")
        cycle_key = id(token.attempt_evidence)
        active = self._active_cycles.get(cycle_key)
        if active is None or active[0] is not token.attempt_evidence:
            raise RuntimeError("cycle token is unknown or already completed")
        return cycle_key, active[1]

    def _release_prediction(self, prediction: DemandPrediction, observed_at: float) -> None:
        predicted_kind = prediction.predicted_kind
        if predicted_kind is not None:
            self._held.remove(predicted_kind)
            status = self._statuses.get(predicted_kind)
            if status is not None and status.is_ready(observed_at):
                self._ready.append(predicted_kind)
                self._ready_set.add(predicted_kind)
                self._ready_since[predicted_kind] = observed_at

    def _now(self) -> float:
        return self._validate_now(self._clock())

    @staticmethod
    def _validate_now(now: float) -> float:
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise ValueError("now must be a finite non-negative timestamp")
        return float(now)

    def _refresh_kind(self, kind: DemandKind, now: float) -> None:
        status = self._adapters[kind].status(now)
        if status is not None and not isinstance(status, DemandStatus):
            raise TypeError(f"durable adapter {kind.value} returned an invalid status")
        if status is None:
            self._statuses.pop(kind, None)
            self._authoritative_ready.discard(kind)
            self._remove_ready(kind)
            return
        self._statuses[kind] = status
        if status.is_ready(now):
            self._authoritative_ready.add(kind)
            if kind not in self._ready_set and kind not in self._held:
                self._ready.append(kind)
                self._ready_set.add(kind)
                self._ready_since[kind] = now
        else:
            self._authoritative_ready.discard(kind)
            self._remove_ready(kind)

    def _remove_ready(self, kind: DemandKind) -> None:
        if kind not in self._ready_set:
            return
        self._ready_set.remove(kind)
        self._ready.remove(kind)
        self._ready_since.pop(kind, None)

    def _recompute_next_release(self, now: float) -> None:
        future_releases = [status.release_at for status in self._statuses.values() if status.release_at > now]
        self._next_release_at = min(future_releases, default=None)
