"""Typed scopes and bounded weighted-fair admission for Telegram RPC attempts."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from .flood import TelegramRpcThrottled
from .telegram_demand import (
    AcquisitionKind,
    DemandToken,
    RpcAttemptBudget,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    current_demand_token,
    demand_context,
    transferred_demand_context,
)
from .telegram_rpc_consumers import (
    TELEGRAM_DEMAND_CONTRACTS,
    TELEGRAM_RPC_CONSUMERS,
    DemandKind,
    RpcServiceClass,
    TelegramRpcSource,
    demand_contract,
)

logger = logging.getLogger(__name__)


class TelegramRpcSchedulerPolicy(Protocol):
    """Runtime scheduler settings required by the transport boundary."""

    @property
    def interactive_weight(self) -> int: ...

    @property
    def live_sync_weight(self) -> int: ...

    @property
    def background_weight(self) -> int: ...

    @property
    def interactive_queue_capacity(self) -> int: ...

    @property
    def live_sync_queue_capacity(self) -> int: ...

    @property
    def background_queue_capacity(self) -> int: ...

    @property
    def interactive_deadline_seconds(self) -> float: ...

    @property
    def live_sync_deadline_seconds(self) -> float: ...

    @property
    def background_deadline_seconds(self) -> float: ...

    @property
    def admission_retry_seconds(self) -> int: ...

    @property
    def update_loop_retry_seconds(self) -> float: ...


RPC_SOURCE_SERVICE_CLASS: Mapping[TelegramRpcSource, RpcServiceClass] = MappingProxyType(
    {source: spec.admission.service_class for source, spec in TELEGRAM_RPC_CONSUMERS.items()}
)

# Temporary PR1 bridge for source-only call sites. The value is explicit because
# sources which own several demand kinds cannot be inferred without losing the
# operation identity. Migrated callers install their precise DemandKind before
# entering the transport.
LEGACY_DEMAND_KIND_BY_SOURCE: Mapping[TelegramRpcSource, DemandKind] = MappingProxyType(
    {
        TelegramRpcSource.MCP_INTERACTIVE: DemandKind.MCP_REMOTE_ACQUISITION,
        TelegramRpcSource.MESSAGE_READ_FALLBACK: DemandKind.MESSAGE_READ_FALLBACK,
        TelegramRpcSource.DIALOG_RESOLUTION: DemandKind.DIALOG_TRAVERSAL,
        TelegramRpcSource.TOPIC_RESOLUTION: DemandKind.TOPIC_LOOKUP,
        TelegramRpcSource.ENTITY_INFO_FOREGROUND: DemandKind.FOREGROUND_ENTITY_FACTS,
        TelegramRpcSource.ENTITY_INFO_REFRESH: DemandKind.ENTITY_PROFILE_REFRESH,
        TelegramRpcSource.ACCOUNT_TRACE: DemandKind.ACCOUNT_TRACE_PAGE,
        TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE: DemandKind.TELETHON_UPDATE_DIFFERENCE,
        TelegramRpcSource.RECONNECT_DIFFERENCE: DemandKind.RECONNECT_DIFFERENCE,
        TelegramRpcSource.REALTIME_EVENT: DemandKind.REALTIME_EVENT_ACQUISITION,
        TelegramRpcSource.DELTA_SYNC: DemandKind.DELTA_GAP_FILL,
        TelegramRpcSource.ACTIVITY_HOT_SWEEP: DemandKind.HOT_ACTIVITY_PAGE,
        TelegramRpcSource.FACT_HYDRATION_LIVE: DemandKind.LIVE_HYDRATION_BATCH,
        TelegramRpcSource.FULL_SYNC: DemandKind.FULL_SYNC_PAGE,
        TelegramRpcSource.DIALOG_SYNC: DemandKind.DIALOG_BOOTSTRAP,
        TelegramRpcSource.ACTIVITY_ARCHIVE: DemandKind.ARCHIVE_INCREMENTAL,
        TelegramRpcSource.ACTIVITY_COLD_BACKFILL: DemandKind.COLD_PEER_PAGE,
        TelegramRpcSource.FACT_HYDRATION_BACKFILL: DemandKind.BACKFILL_HYDRATION_BATCH,
        TelegramRpcSource.FOLDER_RECONCILIATION: DemandKind.FOLDER_SNAPSHOT,
        TelegramRpcSource.TOPIC_RECONCILIATION: DemandKind.TOPIC_SNAPSHOT,
        TelegramRpcSource.MESSAGE_FACT_REFRESH: DemandKind.MESSAGE_FACT_REFRESH,
        TelegramRpcSource.REACTION_REFRESH: DemandKind.REACTION_REFRESH_BATCH,
        TelegramRpcSource.READ_RECEIPT_PROBE: DemandKind.READ_RECEIPT_BATCH,
        TelegramRpcSource.SCHEDULED_MESSAGES: DemandKind.SCHEDULED_REPAIR,
        TelegramRpcSource.MAINTENANCE: DemandKind.SELF_PROFILE_MAINTENANCE,
    }
)


def _validate_legacy_demand_kinds() -> None:
    if set(LEGACY_DEMAND_KIND_BY_SOURCE) != set(TelegramRpcSource):
        raise RuntimeError("legacy Telegram demand bridge must cover every RPC source")
    for source, kind in LEGACY_DEMAND_KIND_BY_SOURCE.items():
        if TELEGRAM_DEMAND_CONTRACTS[kind].source is not source:
            raise RuntimeError(f"legacy Telegram demand bridge conflicts with source {source.value}")


_validate_legacy_demand_kinds()


@dataclass(frozen=True, slots=True)
class TelegramRpcScope:
    """One explicitly classified operation scope inherited by nested helpers."""

    source: TelegramRpcSource
    service_class: RpcServiceClass
    deadline: float | None
    owner_task: asyncio.Task[object] | None
    demand_kind: DemandKind | None = None
    acquisition_kind: AcquisitionKind | None = None
    source_outstanding_limit: int | None = None
    attempt_budget: RpcAttemptBudget | None = None


@dataclass(frozen=True, slots=True)
class _DeadlineOverride:
    deadline: float
    owner_task: asyncio.Task[object] | None


_RPC_DEADLINE: ContextVar[_DeadlineOverride | None] = ContextVar("telegram_rpc_deadline", default=None)
_RPC_ATTEMPT_BUDGET: ContextVar[RpcAttemptBudget | None] = ContextVar("telegram_rpc_attempt_budget", default=None)
_LEGACY_DEMAND_TOKEN: ContextVar[DemandToken | None] = ContextVar("telegram_legacy_demand_token", default=None)


UnclassifiedTelegramRpcError = UnclassifiedTelegramDemandError


class TelegramRpcAdmissionDeferred(TelegramRpcThrottled):
    """Recoverable local deferral raised before Telegram received the RPC."""


def _current_task() -> asyncio.Task[object] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _validate_source(source: TelegramRpcSource) -> None:
    if not isinstance(source, TelegramRpcSource):
        raise TypeError("source must be a TelegramRpcSource")


def _validate_deadline(deadline: float | None) -> None:
    if deadline is None:
        return
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= 0:
        raise ValueError("deadline must be a positive monotonic timestamp")


def _validate_timeout(timeout_seconds: float | None) -> None:
    if timeout_seconds is None:
        return
    if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")


@contextmanager
def rpc_scope(
    source: TelegramRpcSource,
    *,
    deadline: float | None = None,
    timeout_seconds: float | None = None,
    acquisition_kind: AcquisitionKind | None = None,
) -> Iterator[TelegramRpcScope]:
    """Bridge one legacy source scope to a registered demand context.

    An existing root context always wins: nested helpers cannot replace its
    kind, source, or class. They may only tighten its deadline or identify the
    acquisition they perform.
    """
    _validate_source(source)
    if deadline is not None and timeout_seconds is not None:
        raise ValueError("provide deadline or timeout_seconds, not both")
    _validate_deadline(deadline)
    _validate_timeout(timeout_seconds)
    if acquisition_kind is not None and not isinstance(acquisition_kind, AcquisitionKind):
        raise TypeError("acquisition_kind must be an AcquisitionKind")
    caller_deadline = time.monotonic() + float(timeout_seconds) if timeout_seconds is not None else deadline

    try:
        current_demand_token()
    except UnclassifiedTelegramDemandError:
        inherited = _LEGACY_DEMAND_TOKEN.get()
        if inherited is not None and inherited.source is source:
            with transferred_demand_context(inherited):
                with _rpc_scope_details(caller_deadline, acquisition_kind):
                    yield current_rpc_scope()
        else:
            with demand_context(LEGACY_DEMAND_KIND_BY_SOURCE[source], deadline=caller_deadline) as token:
                legacy_token = _LEGACY_DEMAND_TOKEN.set(token)
                try:
                    with _rpc_scope_details(caller_deadline, acquisition_kind):
                        yield current_rpc_scope()
                finally:
                    _LEGACY_DEMAND_TOKEN.reset(legacy_token)
    else:
        with _rpc_scope_details(caller_deadline, acquisition_kind):
            yield current_rpc_scope()


@contextmanager
def _rpc_scope_details(
    caller_deadline: float | None,
    acquisition_kind: AcquisitionKind | None,
) -> Iterator[None]:
    root = current_demand_token()
    resolved_deadline = root.admission_deadline
    if caller_deadline is not None:
        resolved_deadline = min(resolved_deadline, float(caller_deadline))
    deadline_token = _RPC_DEADLINE.set(_DeadlineOverride(resolved_deadline, _current_task()))
    try:
        if acquisition_kind is None:
            yield
        else:
            with acquisition_context(acquisition_kind):
                yield
    finally:
        _RPC_DEADLINE.reset(deadline_token)


def current_rpc_scope() -> TelegramRpcScope:
    """Return the current validated demand identity as a transport scope."""
    token = current_demand_token()
    deadline = token.admission_deadline
    override = _RPC_DEADLINE.get()
    if override is not None and override.owner_task is _current_task():
        deadline = min(deadline, override.deadline)
    contract = demand_contract(token.kind)
    return TelegramRpcScope(
        source=token.source,
        service_class=token.service_class,
        deadline=deadline,
        owner_task=token.owner_task,
        demand_kind=token.kind,
        acquisition_kind=token.acquisition_kind,
        source_outstanding_limit=contract.source_outstanding_limit,
        attempt_budget=_RPC_ATTEMPT_BUDGET.get(),
    )


@contextmanager
def rpc_attempt_budget(budget: RpcAttemptBudget) -> Iterator[RpcAttemptBudget]:
    """Bind one durable slice's actual-attempt budget to all nested sends."""
    if not isinstance(budget, RpcAttemptBudget):
        raise TypeError("budget must be an RpcAttemptBudget")
    active = _RPC_ATTEMPT_BUDGET.get()
    if active is not None and active is not budget:
        raise RuntimeError("cannot replace an active Telegram RPC attempt budget")
    reset_token = _RPC_ATTEMPT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _RPC_ATTEMPT_BUDGET.reset(reset_token)


@contextmanager
def preserve_or_rpc_scope(source: TelegramRpcSource) -> Iterator[None]:
    """Keep a valid caller scope, or classify a directly invoked adapter."""
    try:
        current_demand_token()
    except UnclassifiedTelegramDemandError:
        with rpc_scope(source):
            yield
    else:
        yield


def create_detached_rpc_task[T](
    awaitable: Coroutine[object, object, T],
    *,
    source: TelegramRpcSource,
    timeout_seconds: float,
    name: str | None = None,
    demand_token: DemandToken | None = None,
) -> asyncio.Task[T]:
    """Create detached work with a new source, owner, and bounded lifetime."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    return create_scoped_rpc_task(
        awaitable,
        source=source,
        deadline=_expires_at(timeout_seconds),
        name=name,
        demand_token=demand_token,
    )


def _expires_at(lifetime_seconds: float) -> float:
    """Convert a finite task lifetime to a monotonic expiry timestamp."""
    return time.monotonic() + lifetime_seconds


def create_scoped_rpc_task[T](  # noqa: PLR0913 - compatibility task factory carries explicit context policy
    awaitable: Coroutine[object, object, T],
    *,
    source: TelegramRpcSource,
    deadline: float | None = None,
    name: str | None = None,
    sanitize_context: bool = True,
    demand_token: DemandToken | None = None,
) -> asyncio.Task[T]:
    """Create a task in a sanitized context with an explicit source.

    Long-lived durable daemon loops may omit an operation deadline; each RPC
    still receives its configured class deadline at admission. Finite detached
    work should use :func:`create_detached_rpc_task`.
    """
    del sanitize_context  # Scoped tasks always start from a clean context.
    _validate_source(source)
    _validate_deadline(deadline)
    if demand_token is not None and demand_token.source is not source:
        raise ValueError("transferred demand token source must match the task source")
    started = False

    async def run_scoped() -> T:
        nonlocal started
        started = True
        if demand_token is None:
            with rpc_scope(source, deadline=deadline):
                return await awaitable
        with transferred_demand_context(demand_token):
            with _rpc_scope_details(deadline, None):
                return await awaitable

    task = asyncio.get_running_loop().create_task(run_scoped(), name=name, context=Context())

    def close_unstarted_awaitable(_task: asyncio.Task[T]) -> None:
        if not started:
            awaitable.close()

    task.add_done_callback(close_unstarted_awaitable)
    return task


class RpcAdmissionEventKind(StrEnum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    RESUBMITTED = "retry"
    CLOSED = "closed"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class RpcAdmissionEvent:
    """Content-free scheduler observation safe for operational telemetry."""

    kind: RpcAdmissionEventKind
    source: TelegramRpcSource | None
    service_class: RpcServiceClass | None
    queue_depth: int
    total_depth: int
    active_depth: int = 0
    total_outstanding: int = 0
    wait_seconds: float | None = None
    reason: str | None = None
    demand_kind: DemandKind | None = None
    acquisition_kind: AcquisitionKind | None = None


@dataclass(frozen=True, slots=True)
class RpcAdmission:
    """Proof that one scalar RPC attempt crossed the admission arbiter."""

    source: TelegramRpcSource
    service_class: RpcServiceClass
    wait_seconds: float
    sequence: int
    demand_kind: DemandKind | None = None
    acquisition_kind: AcquisitionKind | None = None


class RpcAdmissionError(RuntimeError):
    """Base class for typed failures before Telegram dispatch."""

    def __init__(self, scope: TelegramRpcScope, message: str) -> None:
        self.source = scope.source
        self.service_class = scope.service_class
        super().__init__(message)


class RpcAdmissionSaturatedError(RpcAdmissionError):
    """The source or service class reached its outstanding bound."""


class RpcAdmissionExpiredError(RpcAdmissionError):
    """The attempt's deadline elapsed before dispatch."""


class RpcAdmissionClosedError(RpcAdmissionError):
    """The arbiter shut down before dispatch."""


class _RateLimiter(Protocol):
    async def acquire(self) -> None: ...


class _UnlimitedRateLimiter:
    async def acquire(self) -> None:
        return None


class _TicketState(StrEnum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CLOSED = "closed"


@dataclass(slots=True)
class _AdmissionTicket:
    sequence: int
    scope: TelegramRpcScope
    enqueued_at: float
    deadline: float
    future: asyncio.Future[RpcAdmission]
    owner_task: asyncio.Task[object] | None
    owns_capacity_slot: bool
    timeout_handle: asyncio.TimerHandle | None = None
    state: _TicketState = _TicketState.QUEUED


AdmissionObserver = Callable[[RpcAdmissionEvent], None]
Clock = Callable[[], float]
ReadinessProbe = Callable[[], bool]
ReadinessWaiter = Callable[[], Awaitable[None]]


async def _ready_immediately() -> None:
    return None


@dataclass(frozen=True, slots=True)
class RpcTransportReadiness:
    """Account transport readiness checks used around limiter acquisition."""

    probe: ReadinessProbe = lambda: True
    wait: ReadinessWaiter = _ready_immediately


@dataclass(slots=True)
class _ActiveAttempt:
    scope: TelegramRpcScope
    owner_task: asyncio.Task[object] | None
    owns_capacity_slot: bool
    dispatch_recorded: bool = False


class TelegramRpcAdmissionScheduler:
    """One bounded weighted-fair arbiter in front of the account limiter.

    The single dispatcher waits for transport readiness and shared rate
    capacity, then selects a FIFO ticket from the weighted class cycle. Its lock
    and limiter acquisition never span request resolution or network I/O.
    Separate active accounting keeps each class's total queued and in-flight
    work bounded while allowing nested Telethon resolution to re-enter.
    """

    def __init__(
        self,
        *,
        policy: TelegramRpcSchedulerPolicy,
        limiter: _RateLimiter | None,
        observer: AdmissionObserver | None = None,
        clock: Clock = time.monotonic,
        readiness: RpcTransportReadiness | None = None,
    ) -> None:
        self._policy = policy
        self._limiter: _RateLimiter = limiter or _UnlimitedRateLimiter()
        self._observer = observer
        self._clock = clock
        self._readiness = readiness or RpcTransportReadiness()
        self._queues = {source: deque[_AdmissionTicket]() for source in TelegramRpcSource}
        self._source_cycles = {
            service_class: tuple(
                source for source in TelegramRpcSource if RPC_SOURCE_SERVICE_CLASS[source] is service_class
            )
            for service_class in RpcServiceClass
        }
        self._source_indices = dict.fromkeys(RpcServiceClass, 0)
        self._active_counts = dict.fromkeys(RpcServiceClass, 0)
        self._active_source_counts = dict.fromkeys(TelegramRpcSource, 0)
        self._active: dict[int, _ActiveAttempt] = {}
        self._capacities = {
            RpcServiceClass.INTERACTIVE: policy.interactive_queue_capacity,
            RpcServiceClass.LIVE_SYNC: policy.live_sync_queue_capacity,
            RpcServiceClass.BACKGROUND: policy.background_queue_capacity,
        }
        self._fair_cycle = self._build_fair_cycle(
            {
                RpcServiceClass.INTERACTIVE: policy.interactive_weight,
                RpcServiceClass.LIVE_SYNC: policy.live_sync_weight,
                RpcServiceClass.BACKGROUND: policy.background_weight,
            }
        )
        self._fair_index = 0
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._dispatcher: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _build_fair_cycle(weights: Mapping[RpcServiceClass, int]) -> tuple[RpcServiceClass, ...]:
        """Build a deterministic smooth weighted round-robin cycle."""
        divisor = math.gcd(*weights.values())
        normalized = {service_class: weight // divisor for service_class, weight in weights.items()}
        total = sum(normalized.values())
        scores = dict.fromkeys(RpcServiceClass, 0)
        preference = {service_class: -index for index, service_class in enumerate(RpcServiceClass)}
        order: list[RpcServiceClass] = []
        for _ in range(total):
            for service_class in RpcServiceClass:
                scores[service_class] += normalized[service_class]
            selected = max(RpcServiceClass, key=lambda item: (scores[item], preference[item]))
            scores[selected] -= total
            order.append(selected)
        return tuple(order)

    @property
    def fair_cycle(self) -> tuple[RpcServiceClass, ...]:
        """Expose the immutable cycle for deterministic policy verification."""
        return self._fair_cycle

    def queue_depths(self) -> dict[RpcServiceClass, int]:
        """Return a point-in-time queue-depth snapshot."""
        return {
            service_class: sum(len(self._queues[source]) for source in self._source_cycles[service_class])
            for service_class in RpcServiceClass
        }

    def source_queue_depths(self) -> dict[TelegramRpcSource, int]:
        """Return queued attempt counts by causal producer source."""
        return {source: len(queue) for source, queue in self._queues.items()}

    def active_depths(self) -> dict[RpcServiceClass, int]:
        """Return active external-operation slots for each service class."""
        return dict(self._active_counts)

    def outstanding_depths(self) -> dict[RpcServiceClass, int]:
        """Return queued plus active external-operation slots by class."""
        return {service_class: self._outstanding_depth(service_class) for service_class in RpcServiceClass}

    def source_outstanding_depths(self) -> dict[TelegramRpcSource, int]:
        """Return queued plus active external-operation slots by source."""
        return {source: self._source_outstanding_depth(source) for source in TelegramRpcSource}

    def expire_due(self) -> int:
        """Expire due tickets immediately; useful for fake-clock operation and tests."""
        return self._expire_due_tickets()

    def _expire_due_tickets(self) -> int:
        now = self._clock()
        due = [
            ticket
            for queue in self._queues.values()
            for ticket in queue
            if ticket.state is _TicketState.QUEUED and ticket.deadline <= now
        ]
        for ticket in due:
            self._expire_ticket(ticket)
        return len(due)

    async def admit(self, scope: TelegramRpcScope) -> RpcAdmission:
        """Queue one attempt until it is admitted, cancelled, or rejected."""
        self._validate_scope_contract(scope)
        loop = asyncio.get_running_loop()
        self._bind_loop(loop)
        now = self._clock()
        deadline = scope.deadline or now + self._configured_deadline_seconds(scope.service_class)
        if deadline <= now:
            self._emit_for_scope(
                RpcAdmissionEventKind.EXPIRED,
                scope,
                wait_seconds=0.0,
                reason="deadline_elapsed",
            )
            raise RpcAdmissionExpiredError(scope, "Telegram RPC admission deadline elapsed")

        async with self._lock:
            if self._closed:
                self._emit_for_scope(
                    RpcAdmissionEventKind.CLOSED,
                    scope,
                    wait_seconds=0.0,
                    reason="scheduler_closed",
                )
                raise RpcAdmissionClosedError(scope, "Telegram RPC admission scheduler is closed")
            self._prune_done_locked()
            queue = self._queues[scope.source]
            owner_task = _current_task()
            owns_capacity_slot = not self._owner_has_active_attempt_locked(owner_task, scope.source)
            source_limit = self._source_outstanding_limit(scope)
            if owns_capacity_slot and self._source_outstanding_depth(scope.source) >= source_limit:
                self._emit_for_scope(RpcAdmissionEventKind.REJECTED, scope, reason="source_outstanding_saturated")
                raise RpcAdmissionSaturatedError(
                    scope, f"{scope.source.value} Telegram RPC source outstanding capacity is full"
                )
            if (
                owns_capacity_slot
                and self._outstanding_depth(scope.service_class) >= self._capacities[scope.service_class]
            ):
                self._emit_for_scope(RpcAdmissionEventKind.REJECTED, scope, reason="outstanding_saturated")
                raise RpcAdmissionSaturatedError(
                    scope, f"{scope.service_class.value} Telegram RPC outstanding capacity is full"
                )
            self._sequence += 1
            future: asyncio.Future[RpcAdmission] = loop.create_future()
            ticket = _AdmissionTicket(
                self._sequence,
                scope,
                now,
                deadline,
                future,
                owner_task,
                owns_capacity_slot,
            )
            queue.append(ticket)
            ticket.timeout_handle = loop.call_later(max(0.0, deadline - now), self._expire_ticket, ticket)
            self._emit_for_scope(RpcAdmissionEventKind.QUEUED, scope)
            self._ensure_dispatcher_locked()

        try:
            return await future
        except asyncio.CancelledError:
            await self._cancel_ticket(ticket)
            raise

    def complete(self, admission: RpcAdmission) -> None:
        """Release active accounting synchronously, including during cancellation."""
        active = self._active.pop(admission.sequence, None)
        if active is None:
            return
        if active.owns_capacity_slot:
            self._active_counts[active.scope.service_class] -= 1
            self._active_source_counts[active.scope.source] -= 1
        if not self._closed and self._has_dispatchable_ticket_locked():
            self._ensure_dispatcher_locked()

    def record_dispatch(self, admission: RpcAdmission) -> None:
        """Record release to Telethon after the final transport-readiness check."""
        active = self._active.get(admission.sequence)
        if active is None or active.dispatch_recorded:
            return
        active.dispatch_recorded = True
        self._emit_for_scope(
            RpcAdmissionEventKind.DISPATCHED,
            active.scope,
            wait_seconds=admission.wait_seconds,
        )

    def record_retry(self, scope: TelegramRpcScope, *, reason: str) -> None:
        """Record that transport policy will submit the attempt again."""
        self._emit_for_scope(RpcAdmissionEventKind.RESUBMITTED, scope, reason=reason)

    def record_attempt_budget_exhausted(self, scope: TelegramRpcScope) -> None:
        """Record a durable slice yield before another sender attempt."""
        self._emit_for_scope(RpcAdmissionEventKind.REJECTED, scope, reason="attempt_budget_exhausted")

    def _configured_deadline_seconds(self, service_class: RpcServiceClass) -> float:
        """Return the injected admission duration for one service class."""
        if service_class is RpcServiceClass.INTERACTIVE:
            return self._policy.interactive_deadline_seconds
        if service_class is RpcServiceClass.LIVE_SYNC:
            return self._policy.live_sync_deadline_seconds
        return self._policy.background_deadline_seconds

    def set_observer(self, observer: AdmissionObserver | None) -> None:
        """Attach the daemon's operational telemetry sink at composition time."""
        self._observer = observer

    def record_unclassified(self) -> None:
        """Record a fail-closed transport access without request content."""
        self._emit(
            RpcAdmissionEvent(
                kind=RpcAdmissionEventKind.UNCLASSIFIED,
                source=None,
                service_class=None,
                queue_depth=0,
                total_depth=self._total_depth(),
                total_outstanding=self._total_outstanding(),
                reason="missing_or_inherited_scope",
            )
        )

    async def close(self) -> None:
        """Reject queued tickets and cancel active caller tasks deterministically."""
        shutdown_task = await self._start_shutdown(reason="shutdown")
        await asyncio.shield(shutdown_task)

    async def _start_shutdown(
        self,
        *,
        reason: str,
        failed_dispatcher: asyncio.Task[object] | None = None,
    ) -> asyncio.Task[None]:
        async with self._lock:
            if self._shutdown_task is not None:
                return self._shutdown_task
            self._closed = True
            initiator = _current_task()
            self._shutdown_task = asyncio.create_task(
                self._drain_shutdown(
                    reason=reason,
                    initiator=initiator,
                    failed_dispatcher=failed_dispatcher,
                ),
                name="telegram-rpc-shutdown",
            )
            return self._shutdown_task

    async def _drain_shutdown(
        self,
        *,
        reason: str,
        initiator: asyncio.Task[object] | None,
        failed_dispatcher: asyncio.Task[object] | None,
    ) -> None:
        async with self._lock:
            self._close_queued_tickets_locked(reason)
            drain_tasks = self._cancel_shutdown_tasks_locked(
                initiator=initiator,
                failed_dispatcher=failed_dispatcher,
            )
        await asyncio.gather(*drain_tasks, return_exceptions=True)

    def _close_queued_tickets_locked(self, reason: str) -> None:
        detail = self._shutdown_error_detail(reason)
        for queue in self._queues.values():
            while queue:
                ticket = queue.popleft()
                if ticket.state is not _TicketState.QUEUED:
                    continue
                ticket.state = _TicketState.CLOSED
                self._cancel_timer(ticket)
                if not ticket.future.done():
                    ticket.future.set_exception(RpcAdmissionClosedError(ticket.scope, detail))
                self._emit_for_scope(
                    RpcAdmissionEventKind.CLOSED,
                    ticket.scope,
                    wait_seconds=self._ticket_wait_seconds(ticket),
                    reason=reason,
                )

    def _cancel_shutdown_tasks_locked(
        self,
        *,
        initiator: asyncio.Task[object] | None,
        failed_dispatcher: asyncio.Task[object] | None,
    ) -> set[asyncio.Task[object]]:
        dispatcher = failed_dispatcher or self._dispatcher
        if dispatcher is not None and dispatcher is not failed_dispatcher:
            dispatcher.cancel()
        drain_tasks = self._active_owner_tasks_locked(exclude=initiator)
        self._clear_active_locked()
        for task in drain_tasks:
            task.cancel()
        if dispatcher is not None and dispatcher is not asyncio.current_task():
            drain_tasks.add(dispatcher)
        return drain_tasks

    @staticmethod
    def _shutdown_error_detail(reason: str) -> str:
        if reason == "limiter_failure":
            return "Telegram RPC limiter failed; scheduler closed"
        return "Telegram RPC admission scheduler closed before dispatch"

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("Telegram RPC admission scheduler cannot move between event loops")

    def _ensure_dispatcher_locked(self) -> None:
        if self._has_dispatchable_ticket_locked() and (self._dispatcher is None or self._dispatcher.done()):
            self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="telegram-rpc-admission")

    async def _dispatch_loop(self) -> None:
        current = asyncio.current_task()
        try:
            while await self._acquire_dispatch_capacity():
                await self._dispatch_one_ready_ticket()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("telegram_rpc_scheduler_dispatch_failed")
            await self._fail_closed_after_dispatch_error()
        finally:
            async with self._lock:
                if self._dispatcher is current:
                    self._dispatcher = None
                    if not self._closed and self._has_dispatchable_ticket_locked():
                        self._ensure_dispatcher_locked()

    async def _acquire_dispatch_capacity(self) -> bool:
        while True:
            await self._readiness.wait()
            async with self._lock:
                self._prune_done_locked()
                self._expire_due_tickets()
                if self._closed or not self._has_dispatchable_ticket_locked():
                    return False
            await self._limiter.acquire()
            if self._readiness.probe():
                return True

    async def _dispatch_one_ready_ticket(self) -> None:
        async with self._lock:
            self._prune_done_locked()
            self._expire_due_tickets()
            if self._closed or not self._readiness.probe():
                return
            ticket = self._select_ticket_locked()
            if ticket is not None:
                self._activate_ticket_locked(ticket)

    def _activate_ticket_locked(self, ticket: _AdmissionTicket) -> None:
        ticket.state = _TicketState.DISPATCHED
        self._cancel_timer(ticket)
        self._active[ticket.sequence] = _ActiveAttempt(
            ticket.scope,
            ticket.owner_task,
            ticket.owns_capacity_slot,
        )
        if ticket.owns_capacity_slot:
            self._active_counts[ticket.scope.service_class] += 1
            self._active_source_counts[ticket.scope.source] += 1
        wait_seconds = max(0.0, self._clock() - ticket.enqueued_at)
        admission = RpcAdmission(
            ticket.scope.source,
            ticket.scope.service_class,
            wait_seconds,
            ticket.sequence,
            ticket.scope.demand_kind,
            ticket.scope.acquisition_kind,
        )
        if not ticket.future.done():
            ticket.future.set_result(admission)

    def _select_ticket_locked(self) -> _AdmissionTicket | None:
        for _ in range(len(self._fair_cycle)):
            service_class = self._fair_cycle[self._fair_index]
            self._fair_index = (self._fair_index + 1) % len(self._fair_cycle)
            ticket = self._select_source_ticket_locked(service_class)
            if ticket is not None:
                return ticket
        return None

    def _select_source_ticket_locked(self, service_class: RpcServiceClass) -> _AdmissionTicket | None:
        sources = self._source_cycles[service_class]
        for _ in range(len(sources)):
            index = self._source_indices[service_class]
            source = sources[index]
            self._source_indices[service_class] = (index + 1) % len(sources)
            queue = self._queues[source]
            while queue and queue[0].state is not _TicketState.QUEUED:
                queue.popleft()
            ticket = next((item for item in queue if self._ticket_can_dispatch_locked(item)), None)
            if ticket is not None:
                queue.remove(ticket)
                return ticket
        return None

    async def _cancel_ticket(self, ticket: _AdmissionTicket) -> None:
        async with self._lock:
            if ticket.state is _TicketState.QUEUED:
                ticket.state = _TicketState.CANCELLED
                self._cancel_timer(ticket)
                self._remove_ticket_locked(ticket)
                self._emit_for_scope(
                    RpcAdmissionEventKind.CANCELLED,
                    ticket.scope,
                    wait_seconds=self._ticket_wait_seconds(ticket),
                    reason="caller_cancelled",
                )
            elif ticket.state is _TicketState.DISPATCHED and ticket.sequence in self._active:
                active = self._active.pop(ticket.sequence)
                if active.owns_capacity_slot:
                    self._active_counts[ticket.scope.service_class] -= 1
                    self._active_source_counts[ticket.scope.source] -= 1
                self._emit_for_scope(
                    RpcAdmissionEventKind.CANCELLED,
                    ticket.scope,
                    wait_seconds=self._ticket_wait_seconds(ticket),
                    reason="caller_cancelled_before_transport",
                )

    def _expire_ticket(self, ticket: _AdmissionTicket) -> None:
        if ticket.state is not _TicketState.QUEUED:
            return
        ticket.state = _TicketState.EXPIRED
        self._remove_ticket_locked(ticket)
        if not ticket.future.done():
            ticket.future.set_exception(
                RpcAdmissionExpiredError(ticket.scope, "Telegram RPC admission deadline elapsed")
            )
        self._emit_for_scope(
            RpcAdmissionEventKind.EXPIRED,
            ticket.scope,
            wait_seconds=self._ticket_wait_seconds(ticket),
            reason="deadline_elapsed",
        )

    def _remove_ticket_locked(self, ticket: _AdmissionTicket) -> None:
        queue = self._queues[ticket.scope.source]
        try:
            queue.remove(ticket)
        except ValueError:
            pass

    def _prune_done_locked(self) -> None:
        for queue in self._queues.values():
            retained = [ticket for ticket in queue if ticket.state is _TicketState.QUEUED and not ticket.future.done()]
            queue.clear()
            queue.extend(retained)

    async def _fail_closed_after_dispatch_error(self) -> None:
        await self._start_shutdown(reason="limiter_failure", failed_dispatcher=_current_task())

    @staticmethod
    def _cancel_timer(ticket: _AdmissionTicket) -> None:
        if ticket.timeout_handle is not None:
            ticket.timeout_handle.cancel()
            ticket.timeout_handle = None

    def _emit_for_scope(
        self,
        kind: RpcAdmissionEventKind,
        scope: TelegramRpcScope,
        *,
        wait_seconds: float | None = None,
        reason: str | None = None,
    ) -> None:
        self._emit(
            RpcAdmissionEvent(
                kind=kind,
                source=scope.source,
                service_class=scope.service_class,
                queue_depth=self.queue_depths()[scope.service_class],
                total_depth=self._total_depth(),
                active_depth=self._active_counts[scope.service_class],
                total_outstanding=self._total_outstanding(),
                wait_seconds=wait_seconds,
                reason=reason,
                demand_kind=scope.demand_kind,
                acquisition_kind=scope.acquisition_kind,
            )
        )

    def _emit(self, event: RpcAdmissionEvent) -> None:
        log = (
            logger.warning
            if event.kind
            in {
                RpcAdmissionEventKind.REJECTED,
                RpcAdmissionEventKind.EXPIRED,
                RpcAdmissionEventKind.UNCLASSIFIED,
            }
            else logger.debug
        )
        log(
            "telegram_rpc_admission event=%s kind=%s acquisition=%s source=%s class=%s "
            "queue_depth=%d total_depth=%d active_depth=%d total_outstanding=%d wait_seconds=%s reason=%s",
            event.kind.value,
            event.demand_kind.value if event.demand_kind is not None else None,
            event.acquisition_kind.value if event.acquisition_kind is not None else None,
            event.source.value if event.source is not None else None,
            event.service_class.value if event.service_class is not None else None,
            event.queue_depth,
            event.total_depth,
            event.active_depth,
            event.total_outstanding,
            event.wait_seconds,
            event.reason,
        )
        if self._observer is None:
            return
        try:
            self._observer(event)
        except Exception:
            logger.exception("telegram_rpc_admission_observer_failed")

    def _total_depth(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    def _ticket_wait_seconds(self, ticket: _AdmissionTicket) -> float:
        return max(0.0, self._clock() - ticket.enqueued_at)

    def _total_outstanding(self) -> int:
        return sum(self._outstanding_depth(service_class) for service_class in RpcServiceClass)

    def _outstanding_depth(self, service_class: RpcServiceClass) -> int:
        queued_slots = sum(
            ticket.owns_capacity_slot
            for source in self._source_cycles[service_class]
            for ticket in self._queues[source]
        )
        return queued_slots + self._active_counts[service_class]

    def _source_outstanding_depth(self, source: TelegramRpcSource) -> int:
        queued_slots = sum(ticket.owns_capacity_slot for ticket in self._queues[source])
        return queued_slots + self._active_source_counts[source]

    def _has_dispatchable_ticket_locked(self) -> bool:
        return any(self._ticket_can_dispatch_locked(ticket) for queue in self._queues.values() for ticket in queue)

    def _ticket_can_dispatch_locked(self, ticket: _AdmissionTicket) -> bool:
        return (
            not ticket.owns_capacity_slot
            or self._active_counts[ticket.scope.service_class] < self._capacities[ticket.scope.service_class]
        )

    def _owner_has_active_attempt_locked(
        self,
        owner_task: asyncio.Task[object] | None,
        source: TelegramRpcSource,
    ) -> bool:
        return owner_task is not None and any(
            active.owner_task is owner_task and active.scope.source is source
            for active in self._active.values()
        )

    @staticmethod
    def _source_outstanding_limit(scope: TelegramRpcScope) -> int:
        kind = scope.demand_kind or LEGACY_DEMAND_KIND_BY_SOURCE[scope.source]
        return demand_contract(kind).source_outstanding_limit

    @staticmethod
    def _validate_scope_contract(scope: TelegramRpcScope) -> None:
        if not isinstance(scope, TelegramRpcScope):
            raise TypeError("scope must be a TelegramRpcScope")
        kind = scope.demand_kind or LEGACY_DEMAND_KIND_BY_SOURCE[scope.source]
        contract = demand_contract(kind)
        if scope.source is not contract.source or scope.service_class is not contract.service_class:
            raise ValueError("Telegram RPC scope conflicts with its code-owned demand contract")
        if (
            scope.source_outstanding_limit is not None
            and scope.source_outstanding_limit != contract.source_outstanding_limit
        ):
            raise ValueError("Telegram RPC scope cannot override its source outstanding limit")

    def _active_owner_tasks_locked(
        self,
        *,
        exclude: asyncio.Task[object] | None,
    ) -> set[asyncio.Task[object]]:
        return {
            active.owner_task
            for active in self._active.values()
            if active.owner_task is not None and active.owner_task is not exclude and not active.owner_task.done()
        }

    def _clear_active_locked(self) -> None:
        self._active.clear()
        self._active_counts = dict.fromkeys(RpcServiceClass, 0)
        self._active_source_counts = dict.fromkeys(TelegramRpcSource, 0)


__all__ = [
    "LEGACY_DEMAND_KIND_BY_SOURCE",
    "RPC_SOURCE_SERVICE_CLASS",
    "RpcAdmission",
    "RpcAdmissionClosedError",
    "RpcAdmissionError",
    "RpcAdmissionEvent",
    "RpcAdmissionEventKind",
    "RpcAdmissionExpiredError",
    "RpcAdmissionSaturatedError",
    "RpcServiceClass",
    "RpcTransportReadiness",
    "TelegramRpcAdmissionDeferred",
    "TelegramRpcAdmissionScheduler",
    "TelegramRpcSchedulerPolicy",
    "TelegramRpcScope",
    "TelegramRpcSource",
    "UnclassifiedTelegramRpcError",
    "create_detached_rpc_task",
    "create_scoped_rpc_task",
    "current_rpc_scope",
    "preserve_or_rpc_scope",
    "rpc_attempt_budget",
    "rpc_scope",
]
