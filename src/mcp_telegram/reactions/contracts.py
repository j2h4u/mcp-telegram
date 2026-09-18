"""Transport- and storage-neutral reaction contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..telegram_reading import GatewayFailure


class ReactionPersistenceBusyError(RuntimeError):
    """Raised when local reaction persistence is temporarily contended."""


class ReactionAggregateSource(StrEnum):
    """Writers of Telegram reaction aggregate observations, ordered by rank."""

    LEGACY = "legacy"
    BACKGROUND = "background"
    HISTORY = "history"
    DELTA = "delta"
    REALTIME_MESSAGE = "realtime_message"
    MESSAGE_EDIT = "message_edit"
    RAW_UPDATE = "raw_update"

    @property
    def rank(self) -> int:
        return {
            ReactionAggregateSource.LEGACY: 0,
            ReactionAggregateSource.BACKGROUND: 10,
            ReactionAggregateSource.HISTORY: 20,
            ReactionAggregateSource.DELTA: 30,
            ReactionAggregateSource.REALTIME_MESSAGE: 40,
            ReactionAggregateSource.MESSAGE_EDIT: 50,
            ReactionAggregateSource.RAW_UPDATE: 60,
        }[self]


@dataclass(frozen=True, slots=True, order=True)
class ReactionObservationBoundary:
    """The durable ordering key for one aggregate observation."""

    observed_at: int
    source_rank: int
    sequence: int
    source: str


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
class ReactionDetailPage:
    """One bounded page from messages.getMessageReactionsList."""

    events: tuple[ReactionEvent, ...]
    next_offset: str | None


@dataclass(frozen=True, slots=True)
class ReactionDetailFetchResult:
    page: ReactionDetailPage | None = None
    failure: GatewayFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None and self.page is not None


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
