"""Transport- and storage-neutral reaction contracts."""

from __future__ import annotations

from dataclasses import dataclass

from ..telegram_reading import GatewayFailure


@dataclass(frozen=True, slots=True)
class ReactionAggregate:
    """One aggregate counter reported by Telegram for a message."""

    emoji: str
    count: int


@dataclass(frozen=True, slots=True)
class ReactionEvent:
    """One individual reaction returned by Telegram."""

    reactor_id: int | None
    emoji: str
    reacted_at: int | None


@dataclass(frozen=True, slots=True)
class ReactionSnapshot:
    """Reaction facts for one message, independent of their persistence model."""

    message_id: int
    aggregates: tuple[ReactionAggregate, ...]
    events: tuple[ReactionEvent, ...] = ()
    events_status: str = "unavailable"


@dataclass(frozen=True, slots=True)
class ReactionFetchResult:
    messages: tuple[ReactionSnapshot | None, ...] = ()
    failure: GatewayFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True, slots=True)
class ReactionFreshness:
    requested_count: int
    fresh_count: int
    stale_count: int
    refreshed_count: int
    status: str
    retry_after: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_count": self.requested_count,
            "fresh_count": self.fresh_count,
            "stale_count": self.stale_count,
            "refreshed_count": self.refreshed_count,
            "status": self.status,
            "retry_after": self.retry_after,
        }
