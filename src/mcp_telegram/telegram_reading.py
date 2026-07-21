"""Pure contracts for bounded Telegram read enrichment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from .message_contracts import ExtractedMessage


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
class ReadDateFetchResult:
    """One Telegram outbox read-date probe; ``read_at`` is never inferred."""

    read_at: int | None = None
    status: str = "unavailable"
    failure: GatewayFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


class TelegramFragmentGateway(Protocol):
    async def fetch_context(self, dialog_id: int, anchor_message_id: int, window_size: int) -> FragmentFetchResult: ...


class TelegramHistoryGateway(Protocol):
    async def fetch_history(
        self, dialog_id: int, kwargs: Mapping[str, object], self_id: int | None
    ) -> HistoryFetchResult: ...


class TelegramReadReceiptGateway(Protocol):
    async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult: ...
