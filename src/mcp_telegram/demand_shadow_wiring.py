"""Production-safe bridges between legacy workers and the PR1 demand shadow."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import Context
from typing import Protocol

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.rpc_admission_observations import DemandEvidenceOutcome
from mcp_telegram.telegram_demand import (
    DemandToken,
    UnclassifiedTelegramDemandError,
    current_demand_token,
    demand_context,
    transferred_demand_context,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred

logger = logging.getLogger(__name__)


class DemandShadow(Protocol):
    """Narrow public shadow API consumed by production owners."""

    def offer(self, kind: DemandKind) -> bool: ...

    def begin_cycle(self, kind: DemandKind) -> DemandToken: ...

    def after_cycle_scan(
        self,
        token: DemandToken | None = None,
        *,
        actual_kind: DemandKind | None = None,
        outcome: DemandEvidenceOutcome | str | None = None,
        reason: str | None = None,
    ) -> tuple[DemandKind, ...]: ...


type DemandCycleRunner = Callable[[DemandKind, Callable[[], Awaitable[object]]], Awaitable[object]]


def offer_durable_demand(shadow: DemandShadow | None, *kinds: DemandKind) -> None:
    """Send post-commit wakeup hints without letting shadow telemetry affect work."""
    if shadow is None:
        return
    for kind in kinds:
        try:
            shadow.offer(kind)
        except Exception:
            logger.warning("telegram_demand_shadow_offer_failed kind=%s", kind.value, exc_info=True)


async def run_legacy_demand_cycle[T](
    shadow: DemandShadow | None,
    kind: DemandKind,
    operation: Callable[[], Awaitable[T]],
) -> T:
    """Attribute one real legacy cycle and preserve its original failure behavior."""
    if shadow is None:
        return await _run_without_shadow(kind, operation)

    try:
        token = shadow.begin_cycle(kind)
    except Exception:
        logger.warning("telegram_demand_shadow_begin_failed kind=%s", kind.value, exc_info=True)
        return await _run_in_demand_context(kind, operation)

    return await _run_shadowed_cycle(shadow, token, kind, operation)


async def _run_without_shadow[T](kind: DemandKind, operation: Callable[[], Awaitable[T]]) -> T:
    active = _active_demand_token()
    if active is None:
        return await _run_in_demand_context(kind, operation)
    if active.kind is not kind:
        raise RuntimeError(f"active demand kind {active.kind.value} cannot execute {kind.value}")
    return await operation()


async def _run_in_demand_context[T](kind: DemandKind, operation: Callable[[], Awaitable[T]]) -> T:
    with demand_context(kind):
        return await operation()


def _active_demand_token() -> DemandToken | None:
    try:
        return current_demand_token()
    except UnclassifiedTelegramDemandError:
        return None


async def _run_shadowed_cycle[T](
    shadow: DemandShadow,
    token: DemandToken,
    kind: DemandKind,
    operation: Callable[[], Awaitable[T]],
) -> T:
    try:
        result = await _execute_shadow_operation(token, operation)
    except asyncio.CancelledError:
        _finish_cycle(shadow, token, kind, DemandEvidenceOutcome.DEFERRED, "cancelled")
        raise
    except Exception as exc:
        outcome, reason = _cycle_error_outcome(exc)
        _finish_cycle(shadow, token, kind, outcome, reason)
        raise
    _finish_cycle(shadow, token, kind, DemandEvidenceOutcome.COMPLETED, None)
    return result


def _cycle_error_outcome(exc: Exception) -> tuple[DemandEvidenceOutcome, str]:
    if isinstance(exc, TelegramRpcAdmissionDeferred):
        return DemandEvidenceOutcome.DEFERRED, "admission_deferred"
    if isinstance(exc, TelegramRpcThrottled):
        reason = "circuit_open" if exc.retry_after_seconds is None else "flood_wait"
        return DemandEvidenceOutcome.DEFERRED, reason
    return DemandEvidenceOutcome.FAILED, type(exc).__name__


async def _execute_shadow_operation[T](token: DemandToken, operation: Callable[[], Awaitable[T]]) -> T:
    async def execute() -> T:
        with transferred_demand_context(token):
            return await operation()

    if _active_demand_token() is None:
        return await execute()
    return await asyncio.get_running_loop().create_task(execute(), context=Context())


def _finish_cycle(
    shadow: DemandShadow,
    token: DemandToken,
    kind: DemandKind,
    outcome: DemandEvidenceOutcome,
    reason: str | None,
) -> None:
    try:
        shadow.after_cycle_scan(token, actual_kind=kind, outcome=outcome, reason=reason)
    except Exception:
        logger.warning("telegram_demand_shadow_finish_failed kind=%s", kind.value, exc_info=True)


__all__ = ["DemandCycleRunner", "DemandShadow", "offer_durable_demand", "run_legacy_demand_cycle"]
