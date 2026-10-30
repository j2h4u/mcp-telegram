"""Single process-wide executor for durable Telegram demand."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_demand import (
    DemandStatus,
    DemandToken,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    RpcRetryAuthorization,
    demand_context,
)
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkRevision
from mcp_telegram.telegram_rpc_consumers import (
    DURABLE_DEMAND_ORDER,
    TELEGRAM_DEMAND_CONTRACTS,
    DemandContract,
    DemandKind,
    ExecutionMode,
    demand_contract,
)
from mcp_telegram.telegram_rpc_recovery import RecoveryCause, backoff_seconds
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
)

logger = logging.getLogger(__name__)

DEFAULT_SAFETY_SCAN_SECONDS = 60.0
MIN_WAIT_SECONDS = 0.001
_DEFERRED_ERRORS = (
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    RpcAttemptBudgetExhaustedError,
    RpcAdmissionSaturatedError,
    RpcAdmissionExpiredError,
)


class CoordinatorState(StrEnum):
    """Lifecycle states for the one coordinator task."""

    NEW = "new"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(slots=True)
class _Invocation:
    budget: RpcAttemptBudget
    deadline: float | None


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

    def __init__(  # noqa: PLR0913 - constructor dependencies are the coordinator's public wiring surface.
        self,
        adapters: Mapping[DemandKind, DurableDemandAdapter],
        shutdown_event: asyncio.Event | None = None,
        *,
        clock: Callable[[], float] = time.time,
        safety_scan_seconds: float = DEFAULT_SAFETY_SCAN_SECONDS,
        observer: object | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        rpc_eligibility: Callable[[str | None], TelegramRpcThrottled | TelegramRpcMethodRecoveryPending | None]
        | None = None,
        prepare_durable_retry: Callable[[RpcAttemptBudget, str | None], RpcRetryAuthorization | None] | None = None,
    ) -> None:
        validate_durable_adapters(adapters)
        if not math.isfinite(safety_scan_seconds) or safety_scan_seconds <= 0:
            raise ValueError("safety_scan_seconds must be finite and positive")
        self._adapters: Mapping[DemandKind, DurableDemandAdapter] = MappingProxyType(dict(adapters))
        self._shutdown_event = shutdown_event or asyncio.Event()
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._rpc_eligibility = rpc_eligibility or (lambda _method: None)
        self._prepare_durable_retry = prepare_durable_retry or (lambda _budget, _method: None)
        self._safety_scan_seconds = float(safety_scan_seconds)
        self._observer = observer
        self._canonical_progress: Callable[[], None] | None = None
        self._wake = asyncio.Event()
        self._state = CoordinatorState.NEW
        self._queue = deque[DemandKind]()
        self._queued: set[DemandKind] = set()
        self._queued_since: dict[DemandKind, float] = {}
        self._statuses: dict[DemandKind, DemandStatus] = {}
        self._authoritative_ready: set[DemandKind] = set()
        self._offered: set[DemandKind] = set()
        self._active_kind: DemandKind | None = None
        self._active_revision: WorkRevision | None = None
        self._next_release_at: float | None = None
        self._status_failed_until: dict[DemandKind, float] = {}
        self._invocations: dict[WorkRevision, _Invocation] = {}
        self._excluded: set[WorkRevision] = set()
        self._exclusion_reasons: dict[WorkRevision, str] = {}
        self._eligibility_waits: dict[WorkRevision, float | None] = {}
        self._released_gate_waits: set[WorkRevision] = set()
        self._transient_waits: dict[WorkRevision, float] = {}
        self._transient_stages: dict[WorkRevision, int] = {}
        self._revision_methods: dict[WorkRevision, str] = {}
        self._eligibility_generation = 0
        self._slice_eligibility_generation = 0
        self.scan()

    def bind_canonical_progress(self, callback: Callable[[], None]) -> None:
        """Attach the account owner's accepted business progress signal."""
        self._canonical_progress = callback

    @property
    def state(self) -> CoordinatorState:
        return self._state

    @property
    def ready_kinds(self) -> tuple[DemandKind, ...]:
        return tuple(self._queue)

    @property
    def offered_kinds(self) -> frozenset[DemandKind]:
        return frozenset(self._offered)

    @property
    def statuses(self) -> Mapping[DemandKind, DemandStatus]:
        return MappingProxyType(dict(self._statuses))

    @property
    def next_release_at(self) -> float | None:
        return self._next_release_at

    def inspect_work(self, selected: WorkRevision) -> dict[str, object]:
        """Snapshot exact-work coordinator state without changing admission."""
        active = self._active_revision
        status = self._statuses.get(selected.key.demand_kind)
        invocation = self._invocations.get(selected)
        now = self._monotonic_clock()
        hold, deadline = self._inspect_hold(selected, now)
        active_for_target = active is not None and active.key == selected.key
        selected_for_kind = status is not None and status.selected == selected
        return {
            "coordinator_state": self._state.value,
            "active_for_target": active_for_target,
            "active_revision_matches": active == selected if active_for_target else None,
            "selected_for_kind": selected_for_kind,
            "excluded": selected in self._excluded,
            "hold_kind": hold,
            "reason": "retryable_failure" if hold == "transient_backoff" else self._exclusion_reasons.get(selected),
            "retry_after_seconds": max(0.0, deadline - now) if deadline is not None else None,
            "attempts": invocation.budget.attempts if invocation is not None else None,
            "invocation_remaining_seconds": (
                max(0.0, invocation.deadline - now)
                if invocation is not None and invocation.deadline is not None
                else None
            ),
        }

    def _inspect_hold(self, selected: WorkRevision, now: float) -> tuple[str, float | None]:
        transient = self._transient_waits.get(selected)
        gate_wait = selected in self._eligibility_waits
        if transient is not None and now < transient:
            return "transient_backoff", transient
        if gate_wait:
            return "gate_wait", self._eligibility_waits[selected]
        if selected in self._excluded:
            return "excluded", None
        return "none", None

    def shutdown(self) -> None:
        """Request a prompt stop for a coordinator that owns its event."""
        self._shutdown_event.set()
        self._wake.set()

    def eligibility_changed(self, method_key: str | None = None) -> None:
        """Wake the pump after gate eligibility changes."""
        self._eligibility_generation += 1
        if method_key is None:
            self._release_ready_gate_waits()
        else:
            for revision, invocation in tuple(self._invocations.items()):
                if self._wait_ready(revision) and self._method_for_work(revision, invocation.budget) == method_key:
                    self._release_gate_wait(revision)
            for revision, method in tuple(self._revision_methods.items()):
                if self._wait_ready(revision) and method == method_key:
                    self._release_gate_wait(revision)
        self._wake.set()

    def _release_ready_gate_waits(self) -> None:
        for revision in tuple(self._eligibility_waits):
            if not self._wait_ready(revision):
                continue
            invocation = self._invocations.get(revision)
            method = self._method_for_work(revision, None if invocation is None else invocation.budget)
            if self._rpc_eligibility(method) is not None:
                continue
            self._release_gate_wait(revision)

    def _release_gate_wait(self, revision: WorkRevision) -> None:
        if revision in self._eligibility_waits and self._exclusion_reasons.get(revision) in {
            "admission_deferred",
            "admission_saturated",
            "flood_wait",
            "method_recovery_pending",
        }:
            self._released_gate_waits.add(revision)
        self._eligibility_waits.pop(revision, None)
        self._excluded.discard(revision)
        self._exclusion_reasons.pop(revision, None)

    def _transient_ready(self, revision: WorkRevision) -> bool:
        return self._monotonic_clock() >= self._transient_waits.get(revision, 0.0)

    def _wait_ready(self, revision: WorkRevision) -> bool:
        deadline = self._eligibility_waits.get(revision)
        return self._transient_ready(revision) and (deadline is None or self._monotonic_clock() >= deadline)

    @contextmanager
    def protocol_scope(self, kind: DemandKind) -> Iterator[DemandToken]:
        """Attribute Telethon-owned recovery without owning its lifecycle."""
        contract = demand_contract(kind)
        if contract.execution_mode is not ExecutionMode.PROTOCOL:
            raise RuntimeError(f"{kind.value} is not registered for protocol execution")
        with demand_context(kind) as token:
            yield token

    def offer(self, kind: DemandKind) -> bool:
        """Record a cheap post-commit wakeup and coalesce until the next scan."""
        self._validate_durable_kind(kind)
        if self._state in {CoordinatorState.STOPPING, CoordinatorState.STOPPED}:
            return False
        if kind in self._offered or kind in self._queued or kind is self._active_kind:
            return False
        self._offered.add(kind)
        self._wake.set()
        return True

    def scan(self, *, now: float | None = None, clear_offers: bool = True) -> tuple[DemandKind, ...]:
        """Perform one complete authoritative status scan in stable order."""
        observed_at = self._now() if now is None else self._validate_now(now)
        self._prepare_expired_gate_waits(self._monotonic_clock())
        for revision, eligible_at in self._transient_waits.items():
            if self._monotonic_clock() >= eligible_at and revision not in self._eligibility_waits:
                self._excluded.discard(revision)
                self._exclusion_reasons.pop(revision, None)
        if clear_offers:
            self._offered.clear()
        for kind in DURABLE_DEMAND_ORDER:
            self._scan_kind(kind, observed_at)
        self._recompute_next_release(observed_at)
        return self.ready_kinds

    def _prepare_expired_gate_waits(self, monotonic_now: float) -> None:
        for revision, eligible_at in tuple(self._eligibility_waits.items()):
            if (
                eligible_at is not None
                and monotonic_now >= eligible_at
                and (revision not in self._transient_waits or monotonic_now >= self._transient_waits[revision])
            ):
                self._release_gate_wait(revision)

    def _scan_kind(self, kind: DemandKind, observed_at: float) -> None:
        if observed_at < self._status_failed_until.get(kind, -math.inf):
            return
        try:
            while True:
                excluded = frozenset(self._excluded)
                status = self._adapters[kind].status(observed_at, excluded=excluded)
                if status is not None and not isinstance(status, DemandStatus):
                    raise TypeError("adapter returned an invalid status")
                if status is None:
                    self._apply_status(kind, None, observed_at)
                    break
                self._apply_status(kind, status, observed_at)
                if status.selected is None or status.selected not in self._excluded or status.selected in excluded:
                    break
        except Exception as exc:  # noqa: BLE001 - one bad domain cannot stop the pump
            self._record_status_failure(kind, observed_at, exc)
            return
        self._status_failed_until.pop(kind, None)

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
        selected = status.selected
        if selected is not None:
            self._prune_revised_invocations(selected)
        if selected is not None and selected in self._excluded:
            self._authoritative_ready.discard(kind)
            self._remove_queued(kind)
            return
        status, is_ready = self._eligibility_status(kind, status, observed_at)
        self._statuses[kind] = status
        if not is_ready:
            self._authoritative_ready.discard(kind)
            self._remove_queued(kind)
            return
        self._authoritative_ready.add(kind)
        if kind not in self._queued and kind is not self._active_kind:
            self._queue.append(kind)
            self._queued.add(kind)
            self._queued_since[kind] = observed_at

    def _prune_revised_invocations(self, selected: WorkRevision) -> None:
        for revision in tuple(self._invocations):
            if revision.key == selected.key and revision != selected:
                self._invocations.pop(revision, None)
                self._excluded.discard(revision)
                self._exclusion_reasons.pop(revision, None)
                self._eligibility_waits.pop(revision, None)
                self._released_gate_waits.discard(revision)
                self._revision_methods.pop(revision, None)
                self._transient_waits.pop(revision, None)
                self._transient_stages.pop(revision, None)

    def _method_for_work(
        self,
        selected: WorkRevision | None,
        budget: RpcAttemptBudget | None = None,
        advisory: str | None = None,
    ) -> str | None:
        pending = None if selected is None else self._revision_methods.get(selected)
        physical = None if budget is None else budget.last_method
        return pending or physical or advisory

    def _eligibility_status(
        self, kind: DemandKind, status: DemandStatus, observed_at: float
    ) -> tuple[DemandStatus, bool]:
        selected = status.selected
        if not status.is_ready(observed_at) or selected is None:
            return status, False
        invocation = self._invocations.get(selected)
        method = self._method_for_work(
            selected, None if invocation is None else invocation.budget, status.request_method
        )
        gate = self._rpc_eligibility(method)
        if isinstance(gate, TelegramRpcAdmissionDeferred):
            self._park_method_deferral(kind, selected, method, gate)
            return self._gate_release(status, gate.eligible_at, observed_at), False
        if isinstance(gate, (TelegramRpcThrottled, TelegramRpcMethodRecoveryPending)):
            if isinstance(gate, TelegramRpcMethodRecoveryPending):
                self._revision_methods[selected] = gate.method_key
                self._excluded.add(selected)
                self._exclusion_reasons[selected] = "method_recovery_pending"
                self._eligibility_waits[selected] = None
            else:
                self._park_throttle(selected, method, gate)
            return status, False
        self._eligibility_waits.pop(selected, None)
        return status, True

    def _park_method_deferral(
        self,
        kind: DemandKind,
        selected: WorkRevision,
        method: str | None,
        gate: TelegramRpcAdmissionDeferred,
    ) -> None:
        method = gate.method_key or method
        invocation = self._invocations.get(selected)
        if invocation is None and gate.generation is not None:
            contract = demand_contract(kind)
            if contract.max_rpc_attempts_per_slice is None:
                raise RuntimeError(f"durable demand {kind.value} has no slice budget")
            invocation = _Invocation(
                RpcAttemptBudget(limit=contract.max_rpc_attempts_per_slice, selected=selected),
                None,
            )
            self._invocations[selected] = invocation
        if method is not None and invocation is not None:
            self._prepare_durable_retry(invocation.budget, method)
        if method is not None:
            self._revision_methods[selected] = method
        self._excluded.add(selected)
        self._exclusion_reasons[selected] = "admission_deferred"
        self._eligibility_waits[selected] = gate.eligible_at

    def _gate_release(self, status: DemandStatus, eligible_at: float | None, observed_at: float) -> DemandStatus:
        if eligible_at is None:
            return status
        release_at = observed_at + max(0.0, eligible_at - self._monotonic_clock())
        return DemandStatus(
            max(status.release_at, release_at),
            status.freshness_deadline,
            status.selected,
            status.request_method,
        )

    async def run(self) -> None:
        """Run the critical process-wide durable pump until shutdown."""
        if self._state is not CoordinatorState.NEW:
            raise RuntimeError("Telegram demand coordinator can run only once")
        self._state = CoordinatorState.RUNNING
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
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except TelegramRpcThrottled:
            raise
        finally:
            self._state = CoordinatorState.STOPPING
            self._active_kind = None
            self._state = CoordinatorState.STOPPED

    async def _execute_slice(self, kind: DemandKind) -> None:
        contract = demand_contract(kind)
        limit = contract.max_rpc_attempts_per_slice
        if limit is None:
            raise RuntimeError(f"durable demand {kind.value} has no slice budget")
        selected_at = self._now()
        queued_at = self._queued_since.pop(kind, selected_at)
        status = self._statuses.get(kind)
        if status is None or status.selected is None:
            raise RuntimeError(f"durable demand {kind.value} did not select an exact work revision")
        invocation = self._invocations.get(status.selected)
        if invocation is None:
            invocation = _Invocation(
                RpcAttemptBudget(limit=limit, selected=status.selected),
                self._monotonic_clock() + contract.admission_timeout_seconds,
            )
            self._invocations[status.selected] = invocation
        if status.selected in self._transient_waits:
            if self._monotonic_clock() < self._transient_waits[status.selected]:
                return
            if (
                self._rpc_eligibility(self._method_for_work(status.selected, invocation.budget, status.request_method))
                is not None
            ):
                return
            self._transient_waits.pop(status.selected)
            self._activate_transient_retry(status, invocation, limit, contract.admission_timeout_seconds)
        self._resume_gate_wait(status, invocation, contract.admission_timeout_seconds)
        if not self._authorize_invocation(kind, status, invocation):
            return
        await self._run_invocation(kind, status, invocation, selected_at, queued_at)

    def _resume_gate_wait(self, status: DemandStatus, invocation: _Invocation, timeout: float) -> None:
        selected = status.selected
        if selected is None or selected not in self._released_gate_waits:
            return
        self._released_gate_waits.discard(selected)
        method = self._method_for_work(selected, invocation.budget, status.request_method)
        now = self._monotonic_clock()
        if invocation.budget.exhausted or invocation.deadline is None or now < invocation.deadline:
            return
        if self._rpc_eligibility(method) is not None:
            return
        authorization = self._prepare_durable_retry(invocation.budget, method)
        if authorization is not None and now < authorization.deadline:
            invocation.deadline = authorization.deadline
        else:
            invocation.budget._retry_candidate = None
            invocation.deadline = now + timeout

    def _activate_transient_retry(
        self, status: DemandStatus, invocation: _Invocation, allowance: int, timeout: float
    ) -> None:
        budget = invocation.budget
        method = self._method_for_work(status.selected, budget, status.request_method)
        authorization = self._prepare_durable_retry(budget, method) if budget.exhausted else None
        if authorization is not None and self._monotonic_clock() < authorization.deadline:
            invocation.deadline = authorization.deadline
            return
        budget._retry_candidate = None
        budget.limit = (
            budget.attempts + min(budget.remaining, allowance) if not budget.exhausted else budget.attempts + allowance
        )
        invocation.deadline = self._monotonic_clock() + timeout

    def _authorize_invocation(self, kind: DemandKind, status: DemandStatus, invocation: _Invocation) -> bool:
        if invocation.deadline is None:
            invocation.deadline = self._monotonic_clock() + demand_contract(kind).admission_timeout_seconds
        if not invocation.budget.exhausted and self._monotonic_clock() < invocation.deadline:
            return True
        method = self._method_for_work(status.selected, invocation.budget, status.request_method)
        authorization = self._prepare_durable_retry(invocation.budget, method)
        if authorization is not None and self._monotonic_clock() < authorization.deadline:
            invocation.deadline = authorization.deadline
            return True
        if status.selected is not None:
            self._excluded.add(status.selected)
            self._exclusion_reasons[status.selected] = "retry_not_authorized"
            self._eligibility_waits[status.selected] = None
        self._observe("deferred", kind, actual_attempts=invocation.budget.attempts, reason="retry_not_authorized")
        return False

    async def _run_invocation(
        self,
        kind: DemandKind,
        status: DemandStatus,
        invocation: _Invocation,
        selected_at: float,
        queued_at: float,
    ) -> None:
        selected = status.selected
        if selected is None:
            return
        budget = invocation.budget
        self._slice_eligibility_generation = self._eligibility_generation
        self._active_kind = kind
        self._active_revision = selected
        self._observe("selected", kind, queue_age_seconds=max(0.0, selected_at - queued_at), status=status)
        assert invocation.deadline is not None
        try:
            remaining = max(0.0, invocation.deadline - self._monotonic_clock())
            with demand_context(kind, deadline=invocation.deadline):
                async with asyncio.timeout(remaining):
                    outcome = await self._run_adapter(kind, budget, selected)
        except asyncio.CancelledError:
            raise
        except RpcAdmissionClosedError:
            if self._handle_closed_admission(kind, budget):
                return
            raise
        except _DEFERRED_ERRORS as exc:
            self._handle_deferred(kind, budget, exc)
        except TelegramRpcThrottled as exc:
            self._handle_throttle(kind, budget, exc)
        except Exception as exc:  # noqa: BLE001 - adapter failure must not kill sibling demand
            self._handle_slice_failure(kind, budget, exc)
        else:
            self._handle_outcome(kind, status, budget, outcome)
        finally:
            self._active_revision = None

    async def _run_adapter(self, kind: DemandKind, budget: RpcAttemptBudget, selected: WorkRevision) -> SliceOutcome:
        attempts_before = budget.attempts
        try:
            return await self._adapters[kind].run_slice(budget)
        finally:
            if budget.attempts > attempts_before:
                self._revision_methods.pop(selected, None)

    def _handle_outcome(
        self, kind: DemandKind, status: DemandStatus, budget: RpcAttemptBudget, outcome: object
    ) -> None:
        selected = status.selected
        if selected is None:
            return
        if not isinstance(outcome, SliceOutcome) or outcome.selected != selected:
            self._exclude_revision(kind, selected, budget, "invalid_slice_outcome")
        elif self._committed_outcome(outcome, selected):
            self._invocations.pop(selected, None)
            self._excluded.discard(selected)
            self._exclusion_reasons.pop(selected, None)
            self._eligibility_waits.pop(selected, None)
            self._released_gate_waits.discard(selected)
            self._revision_methods.pop(selected, None)
            self._transient_waits.pop(selected, None)
            self._transient_stages.pop(selected, None)
            if budget.attempts > 0 and self._canonical_progress is not None:
                self._canonical_progress()
            self._observe("completed", kind, actual_attempts=budget.attempts)
        elif outcome.kind is SliceOutcomeKind.RETRYABLE_FAILURE:
            self._schedule_transient_retry(kind, selected, budget)
        elif outcome.kind is SliceOutcomeKind.DEFERRED and self._handle_outcome_deferred(kind, status, budget):
            return
        else:
            self._exclude_revision(kind, selected, budget, outcome.kind.value)

    @staticmethod
    def _committed_outcome(outcome: SliceOutcome, selected: WorkRevision) -> bool:
        return (
            outcome.kind in {SliceOutcomeKind.PROGRESSED, SliceOutcomeKind.TERMINAL}
            and outcome.committed_revision_for(selected) is not None
        )

    def _schedule_transient_retry(self, kind: DemandKind, selected: WorkRevision, budget: RpcAttemptBudget) -> None:
        stage = self._transient_stages.get(selected, 0)
        self._transient_stages[selected] = stage + 1
        self._transient_waits[selected] = self._monotonic_clock() + backoff_seconds(RecoveryCause.TRANSIENT, stage)
        self._excluded.add(selected)
        self._exclusion_reasons[selected] = "retryable_failure"
        self._observe("deferred", kind, actual_attempts=budget.attempts, reason="retryable_failure")

    def _handle_outcome_deferred(self, kind: DemandKind, status: DemandStatus, budget: RpcAttemptBudget) -> bool:
        method = self._method_for_work(budget.selected, budget, status.request_method)
        eligibility = self._rpc_eligibility(method)
        if isinstance(eligibility, TelegramRpcThrottled):
            self._handle_throttle(kind, budget, eligibility)
            return True
        if not isinstance(eligibility, (TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending)):
            return False
        self._handle_deferred(kind, budget, eligibility)
        return True

    def _handle_closed_admission(self, kind: DemandKind, budget: RpcAttemptBudget) -> bool:
        if self._shutdown_event.is_set():
            self._observe("deferred", kind, actual_attempts=budget.attempts, reason="shutdown")
            return True
        self._shutdown_event.set()
        self._wake.set()
        return False

    def _handle_deferred(self, kind: DemandKind, budget: RpcAttemptBudget, exc: Exception) -> None:
        if self._handle_capacity_admission(kind, budget, exc):
            return
        if isinstance(exc, TelegramRpcAdmissionDeferred):
            method = exc.method_key or self._method_for_work(budget.selected, budget)
            if method is not None:
                self._prepare_durable_retry(budget, method)
            if budget.selected is not None:
                if method is not None:
                    self._revision_methods[budget.selected] = method
                self._excluded.add(budget.selected)
                self._exclusion_reasons[budget.selected] = "admission_deferred"
                self._eligibility_waits[budget.selected] = exc.eligible_at
            self._observe("deferred", kind, actual_attempts=budget.attempts, reason="admission_deferred")
            return
        if isinstance(exc, TelegramRpcMethodRecoveryPending):
            if budget.selected is not None:
                self._revision_methods[budget.selected] = exc.method_key
                self._prepare_durable_retry(budget, exc.method_key)
                self._excluded.add(budget.selected)
                self._exclusion_reasons[budget.selected] = "method_recovery_pending"
                self._eligibility_waits[budget.selected] = None
            self._observe("deferred", kind, actual_attempts=budget.attempts, reason="method_recovery_pending")
            return
        self._handle_pacing_deferred(kind, budget, exc)

    def _handle_capacity_admission(self, kind: DemandKind, budget: RpcAttemptBudget, exc: Exception) -> bool:
        if not isinstance(exc, TelegramRpcAdmissionDeferred) or exc.reason != "capacity":
            return False
        method = exc.method_key or self._method_for_work(budget.selected, budget)
        if budget.selected is not None and method is not None:
            self._revision_methods[budget.selected] = method
        self._handle_capacity_deferred(kind, budget)
        return True

    def _handle_throttle(self, kind: DemandKind, budget: RpcAttemptBudget, exc: TelegramRpcThrottled) -> None:
        method = self._method_for_work(budget.selected, budget)
        hint = self._rpc_eligibility(method)
        if isinstance(hint, (TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending)):
            self._handle_deferred(kind, budget, hint)
            if budget.selected is not None and isinstance(hint, TelegramRpcAdmissionDeferred):
                self._strengthen_throttle_wait(budget.selected, exc, capacity=hint.reason == "capacity")
            return
        if budget.selected is not None:
            self._park_throttle(budget.selected, method, exc)
            if isinstance(hint, TelegramRpcThrottled) and (hint.latched or not exc.latched):
                self._park_throttle(budget.selected, method, hint)
        self._observe("deferred", kind, actual_attempts=budget.attempts, reason="flood_wait")

    def _strengthen_throttle_wait(self, selected: WorkRevision, exc: TelegramRpcThrottled, *, capacity: bool) -> None:
        current = self._eligibility_waits.get(selected)
        if current is None and not capacity:
            return
        self._excluded.add(selected)
        self._exclusion_reasons[selected] = "flood_wait"
        if exc.latched:
            self._eligibility_waits[selected] = None
            return
        assert exc.retry_after_seconds is not None
        self._eligibility_waits[selected] = max(current or 0.0, self._monotonic_clock() + exc.retry_after_seconds)

    def _park_throttle(self, selected: WorkRevision, method: str | None, exc: TelegramRpcThrottled) -> None:
        if method is not None:
            self._revision_methods[selected] = method
        self._excluded.add(selected)
        self._exclusion_reasons[selected] = "flood_wait"
        if exc.latched:
            self._eligibility_waits[selected] = None
            return
        assert exc.retry_after_seconds is not None
        previous = self._eligibility_waits.get(selected)
        self._eligibility_waits[selected] = max(previous or 0.0, self._monotonic_clock() + exc.retry_after_seconds)

    def _handle_pacing_deferred(self, kind: DemandKind, budget: RpcAttemptBudget, exc: Exception) -> None:
        if isinstance(exc, RpcAdmissionSaturatedError):
            self._handle_capacity_deferred(kind, budget)
            return
        if isinstance(exc, RpcAttemptBudgetExhaustedError):
            reason = "attempt_budget_exhausted"
        else:
            reason = "admission_expired"
        if budget.selected is not None:
            self._excluded.add(budget.selected)
            self._exclusion_reasons[budget.selected] = reason
        self._observe("deferred", kind, actual_attempts=budget.attempts, reason=reason)

    def _handle_capacity_deferred(self, kind: DemandKind, budget: RpcAttemptBudget) -> None:
        if budget.selected is not None:
            self._excluded.add(budget.selected)
            self._exclusion_reasons[budget.selected] = "admission_saturated"
            self._eligibility_waits[budget.selected] = None
            if self._slice_eligibility_generation != self._eligibility_generation:
                self._release_ready_gate_waits()
        self._observe("deferred", kind, actual_attempts=budget.attempts, reason="admission_saturated")

    def _handle_slice_failure(self, kind: DemandKind, budget: RpcAttemptBudget, exc: Exception) -> None:
        if budget.selected is not None:
            self._excluded.add(budget.selected)
            self._exclusion_reasons[budget.selected] = (
                "deadline_exceeded" if isinstance(exc, TimeoutError) else "unclassified_failure"
            )
        logger.warning(
            "telegram_demand_slice_failed kind=%s error_type=%s sqlite_errorcode=%s sqlite_errorname=%s",
            kind.value,
            type(exc).__name__,
            getattr(exc, "sqlite_errorcode", None),
            getattr(exc, "sqlite_errorname", None),
        )
        self._observe("failed", kind, actual_attempts=budget.attempts, reason=type(exc).__name__)

    def _pop_ready(self, *, now: float) -> DemandKind | None:
        if not self._queue:
            return None
        kind = self._queue.popleft()
        self._queued.remove(kind)
        return kind

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
        delays = [min(boundaries) - now]
        monotonic_now = self._monotonic_clock()
        delays.extend(
            eligible_at - monotonic_now
            for eligible_at in self._eligibility_waits.values()
            if eligible_at is not None and eligible_at > monotonic_now
        )
        delays.extend(
            deadline - monotonic_now for deadline in self._transient_waits.values() if deadline > monotonic_now
        )
        return max(MIN_WAIT_SECONDS, min(delays))

    def _remove_queued(self, kind: DemandKind) -> None:
        if kind not in self._queued:
            return
        self._queued.remove(kind)
        self._queue.remove(kind)
        self._queued_since.pop(kind, None)

    def _recompute_next_release(self, now: float) -> None:
        boundaries = [status.release_at for status in self._statuses.values() if status.release_at > now]
        boundaries.extend(until for until in self._status_failed_until.values() if until > now)
        self._next_release_at = min(boundaries, default=None)

    def _exclude_revision(
        self, kind: DemandKind, selected: WorkRevision, budget: RpcAttemptBudget, reason: str
    ) -> None:
        self._excluded.add(selected)
        self._exclusion_reasons[selected] = reason
        self._observe("deferred", kind, actual_attempts=budget.attempts, reason=reason)

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
