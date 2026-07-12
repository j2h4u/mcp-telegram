"""Pure contracts for bounded Telegram read enrichment."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from .sync_worker import ExtractedMessage, ReactionRecord


class GatewayFailureKind(StrEnum):
    FLOOD_WAIT = "flood_wait"
    ACCESS_LOST = "access_lost"
    TRANSIENT = "transient"
    INVALID_TARGET = "invalid_target"


@dataclass(frozen=True, slots=True)
class GatewayFailure:
    kind: GatewayFailureKind
    error_type: str
    error_message: str
    retryable: bool
    retry_after: int | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FragmentFetchResult:
    messages: tuple[ExtractedMessage, ...] = ()
    failure: GatewayFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True, slots=True)
class HistoryFetchResult:
    messages: tuple[dict[str, object], ...] = ()
    failure: GatewayFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True, slots=True)
class ReactionMessage:
    message_id: int
    rows: tuple[ReactionRecord, ...]


@dataclass(frozen=True, slots=True)
class ReactionFetchResult:
    messages: tuple[ReactionMessage | None, ...] = ()
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
        return asdict(self)


class TelegramFragmentGateway(Protocol):
    async def fetch_context(self, dialog_id: int, anchor_message_id: int, window_size: int) -> FragmentFetchResult: ...


class TelegramHistoryGateway(Protocol):
    async def fetch_history(
        self, dialog_id: int, kwargs: Mapping[str, object], self_id: int | None
    ) -> HistoryFetchResult: ...


class TelegramReactionGateway(Protocol):
    async def fetch_reactions(self, entity: object, message_ids: Sequence[int]) -> ReactionFetchResult: ...
