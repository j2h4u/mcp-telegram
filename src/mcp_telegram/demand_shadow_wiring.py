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
        try:
            active = current_demand_token()
        except UnclassifiedTelegramDemandError:
            active = None
        if active is not None:
            if active.kind is not kind:
                raise RuntimeError(f"active demand kind {active.kind.value} cannot execute {kind.value}")
            return await operation()
        with demand_context(kind):
            return await operation()

    try:
        token = shadow.begin_cycle(kind)
    except Exception:
        logger.warning("telegram_demand_shadow_begin_failed kind=%s", kind.value, exc_info=True)
        with demand_context(kind):
            return await operation()

    async def execute() -> T:
        with transferred_demand_context(token):
            return await operation()

    try:
        try:
            current_demand_token()
        except UnclassifiedTelegramDemandError:
            result = await execute()
        else:
            result = await asyncio.get_running_loop().create_task(execute(), context=Context())
    except asyncio.CancelledError:
        _finish_cycle(shadow, token, kind, DemandEvidenceOutcome.DEFERRED, "cancelled")
        raise
    except TelegramRpcAdmissionDeferred:
        _finish_cycle(shadow, token, kind, DemandEvidenceOutcome.DEFERRED, "admission_deferred")
        raise
    except TelegramRpcThrottled as exc:
        reason = "circuit_open" if exc.retry_after_seconds is None else "flood_wait"
        _finish_cycle(shadow, token, kind, DemandEvidenceOutcome.DEFERRED, reason)
        raise
    except Exception as exc:
        _finish_cycle(shadow, token, kind, DemandEvidenceOutcome.FAILED, type(exc).__name__)
        raise
    _finish_cycle(shadow, token, kind, DemandEvidenceOutcome.COMPLETED, None)
    return result


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
