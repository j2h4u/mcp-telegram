"""Small producer-side seam for durable demand wakeups."""

from __future__ import annotations

import logging
from typing import Protocol

from .telegram_rpc_consumers import DemandKind

logger = logging.getLogger(__name__)


class DemandOfferSink(Protocol):
    """Required sink for post-commit durable demand offers."""

    def offer(self, kind: DemandKind) -> bool: ...


def offer_durable_demand(sink: DemandOfferSink, *kinds: DemandKind) -> None:
    """Best-effort wakeup hints after a producer transaction commits."""
    for kind in kinds:
        try:
            sink.offer(kind)
        except Exception:
            logger.warning("telegram_demand_offer_failed kind=%s", kind.value, exc_info=True)


__all__ = ["DemandOfferSink", "offer_durable_demand"]
