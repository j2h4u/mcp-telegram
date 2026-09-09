"""Task-local identity and narrow adapter contracts for Telegram demand."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from mcp_telegram.telegram_rpc_consumers import (
    DemandContract,
    DemandKind,
    ExecutionMode,
    RpcServiceClass,
    TelegramRpcSource,
    demand_contract,
)


class AcquisitionKind(StrEnum):
    """Telegram operation performed inside a root demand context."""

    ACCOUNT_SELF_PROFILE = "account_self_profile"
    DIALOG_TRAVERSAL = "dialog_traversal"
    ENTITY_LOOKUP = "entity_lookup"
    FOLDER_SNAPSHOT = "folder_snapshot"
    MESSAGE_HISTORY_PAGE = "message_history_page"
    MESSAGE_LOOKUP = "message_lookup"
    MESSAGE_SEARCH_PAGE = "message_search_page"
    REACTION_SNAPSHOT = "reaction_snapshot"
    READ_RECEIPT_SNAPSHOT = "read_receipt_snapshot"
    SCHEDULED_MESSAGES_SNAPSHOT = "scheduled_messages_snapshot"
    TOPIC_LOOKUP = "topic_lookup"
    TOPIC_SNAPSHOT = "topic_snapshot"
    UPDATE_DIFFERENCE = "update_difference"


@dataclass(frozen=True, slots=True)
class DemandPrediction:
    """Content-free shadow selection evidence attached to one legacy cycle."""

    predicted_kind: DemandKind | None
    selected_at: float
    queue_age_seconds: float | None
    overdue_seconds: float | None

    def __post_init__(self) -> None:
        _validate_timestamp(self.selected_at, "selected_at")
        for value, name in (
            (self.queue_age_seconds, "queue_age_seconds"),
            (self.overdue_seconds, "overdue_seconds"),
        ):
            if value is not None:
                _validate_timestamp(value, name)


@dataclass(slots=True, eq=False)
class RpcAttemptEvidence:
    """Mutable, non-enforcing count of real sends attributed to one root."""

    actual_attempts: int = 0

    def record_dispatch(self) -> None:
        """Record one sender dispatch without changing transport behavior."""
        self.actual_attempts += 1


@dataclass(frozen=True, slots=True)
class DemandToken:
    """Opaque, process-local causal identity inherited by nested helpers."""

    kind: DemandKind
    source: TelegramRpcSource
    service_class: RpcServiceClass
    admission_deadline: float
    acquisition_kind: AcquisitionKind | None
    owner_task: asyncio.Task[object] | None
    attempt_evidence: RpcAttemptEvidence
    prediction: DemandPrediction | None = None


@dataclass(frozen=True, slots=True)
class DemandStatus:
    """Authoritative durable-work readiness reported by a domain adapter."""

    release_at: float
    freshness_deadline: float | None = None

    def __post_init__(self) -> None:
        _validate_timestamp(self.release_at, "release_at")
        if self.freshness_deadline is not None:
            _validate_timestamp(self.freshness_deadline, "freshness_deadline")

    def is_ready(self, now: float) -> bool:
        """Return whether domain work may run at *now*."""
        _validate_timestamp(now, "now")
        return self.release_at <= now

    def overdue_seconds(self, now: float) -> float:
        """Return current freshness debt, or zero when no deadline is overdue."""
        _validate_timestamp(now, "now")
        if self.freshness_deadline is None:
            return 0.0
        return max(0.0, now - self.freshness_deadline)


class RpcAttemptBudgetExhaustedError(RuntimeError):
    """Raised before a send that would exceed a durable slice contract."""


@dataclass(slots=True)
class RpcAttemptBudget:
    """Mutable, process-local accounting for actual RPC attempts in one slice."""

    limit: int
    attempts: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError("limit must be a positive integer")
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or not 0 <= self.attempts <= self.limit
        ):
            raise ValueError("attempts must be an integer between zero and limit")

    @property
    def remaining(self) -> int:
        """Return how many actual attempts remain in this slice."""
        return self.limit - self.attempts

    @property
    def exhausted(self) -> bool:
        """Return whether another actual RPC attempt would exceed the slice."""
        return self.attempts >= self.limit

    def debit(self) -> None:
        """Charge one actual RPC attempt, failing before the bound is exceeded."""
        if self.exhausted:
            raise RpcAttemptBudgetExhaustedError("Telegram RPC attempt budget is exhausted")
        self.attempts += 1

    def try_debit(self) -> bool:
        """Charge one attempt and return false when the slice must yield."""
        if self.exhausted:
            return False
        self.attempts += 1
        return True


class DurableDemandAdapter(Protocol):
    """Narrow view over durable state owned by one demand kind."""

    def status(self, now: float) -> DemandStatus | None: ...

    async def run_slice(self, budget: RpcAttemptBudget) -> None: ...


class UnclassifiedTelegramDemandError(RuntimeError):
    """Raised when code reaches Telegram acquisition without root identity."""


_DEMAND_CONTEXT: ContextVar[DemandToken | None] = ContextVar("telegram_demand_context", default=None)


def _current_task() -> asyncio.Task[object] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _validate_timestamp(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative timestamp")


def resolve_admission_deadline(
    contract: DemandContract,
    caller_deadline: float | None,
    *,
    now: float,
) -> float:
    """Apply a caller deadline only when it tightens code-owned policy."""
    _validate_timestamp(now, "now")
    policy_deadline = now + contract.admission_timeout_seconds
    if caller_deadline is None:
        return policy_deadline
    _validate_timestamp(caller_deadline, "caller_deadline")
    if caller_deadline == 0:
        raise ValueError("caller_deadline must be a positive monotonic timestamp")
    return min(policy_deadline, float(caller_deadline))


def _validate_token(token: DemandToken) -> None:
    if not isinstance(token, DemandToken):
        raise TypeError("token must be a DemandToken")
    contract = demand_contract(token.kind)
    if token.source is not contract.source or token.service_class is not contract.service_class:
        raise ValueError("demand token conflicts with its registered root contract")
    if not isinstance(token.attempt_evidence, RpcAttemptEvidence):
        raise TypeError("demand token must carry RPC attempt evidence")


def create_demand_token(
    kind: DemandKind,
    *,
    deadline: float | None = None,
    prediction: DemandPrediction | None = None,
) -> DemandToken:
    """Create an uninstalled root token for explicit cycle transfer."""
    if not isinstance(kind, DemandKind):
        raise TypeError("kind must be a DemandKind")
    if prediction is not None and not isinstance(prediction, DemandPrediction):
        raise TypeError("prediction must be DemandPrediction")
    contract = demand_contract(kind)
    return DemandToken(
        kind=kind,
        source=contract.source,
        service_class=contract.service_class,
        admission_deadline=resolve_admission_deadline(contract, deadline, now=time.monotonic()),
        acquisition_kind=None,
        owner_task=_current_task(),
        attempt_evidence=RpcAttemptEvidence(),
        prediction=prediction,
    )


@contextmanager
def demand_context(kind: DemandKind, *, deadline: float | None = None) -> Iterator[DemandToken]:
    """Install one registered root identity for caller- or protocol-owned work."""
    if _DEMAND_CONTEXT.get() is not None:
        raise RuntimeError("nested root demand is invalid; use acquisition_context for nested helpers")
    token = create_demand_token(kind, deadline=deadline)
    reset_token = _DEMAND_CONTEXT.set(token)
    try:
        yield token
    finally:
        _DEMAND_CONTEXT.reset(reset_token)


@contextmanager
def acquisition_context(kind: AcquisitionKind) -> Iterator[DemandToken]:
    """Refine only nested acquisition identity while preserving the root contract."""
    if not isinstance(kind, AcquisitionKind):
        raise TypeError("kind must be an AcquisitionKind")
    root = current_demand_token()
    nested = replace(root, acquisition_kind=kind)
    reset_token = _DEMAND_CONTEXT.set(nested)
    try:
        yield nested
    finally:
        _DEMAND_CONTEXT.reset(reset_token)


@contextmanager
def transferred_demand_context(token: DemandToken) -> Iterator[DemandToken]:
    """Explicitly transfer registered root identity into the current task."""
    _validate_token(token)
    active = _DEMAND_CONTEXT.get()
    if active is not None and active.owner_task is _current_task():
        raise RuntimeError("cannot transfer demand identity over an active root context")
    transferred = replace(token, owner_task=_current_task())
    reset_token = _DEMAND_CONTEXT.set(transferred)
    try:
        yield transferred
    finally:
        _DEMAND_CONTEXT.reset(reset_token)


def current_demand_token() -> DemandToken:
    """Return the current registered root token or fail closed."""
    token = _DEMAND_CONTEXT.get()
    if token is None:
        raise UnclassifiedTelegramDemandError("Telegram acquisition has no demand context")
    if token.owner_task is not None and token.owner_task is not _current_task():
        raise UnclassifiedTelegramDemandError(
            "detached Telegram work inherited another task's demand context; transfer it explicitly"
        )
    _validate_token(token)
    return token


def require_execution_mode(expected: ExecutionMode) -> DemandToken:
    """Return current identity after verifying its code-owned execution mode."""
    if not isinstance(expected, ExecutionMode):
        raise TypeError("expected must be an ExecutionMode")
    token = current_demand_token()
    if demand_contract(token.kind).execution_mode is not expected:
        raise RuntimeError(f"{token.kind.value} is not registered for {expected.value} execution")
    return token
