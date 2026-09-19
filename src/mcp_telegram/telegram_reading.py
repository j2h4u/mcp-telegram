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


class ReadDateReason(StrEnum):
    """Normalized internal outcomes for one exact Telegram read-date probe."""

    RESOLVED = "resolved"
    DATE_OMITTED = "date_omitted"
    MESSAGE_NOT_READ_YET = "message_not_read_yet"
    FLOOD_WAIT = "flood_wait"
    TRANSIENT = "transient"
    MESSAGE_TOO_OLD = "message_too_old"
    PRIVACY_RESTRICTED = "privacy_restricted"
    NOT_MUTUAL_CONTACT = "not_mutual_contact"
    INVALID_TARGET = "invalid_target"
    ACCESS_LOST = "access_lost"


READ_DATE_REASONS = tuple(ReadDateReason)
_RETRYABLE_READ_DATE_REASONS = frozenset(
    {
        ReadDateReason.DATE_OMITTED,
        ReadDateReason.MESSAGE_NOT_READ_YET,
        ReadDateReason.FLOOD_WAIT,
        ReadDateReason.TRANSIENT,
    }
)


def is_read_date_reason_retryable(reason: ReadDateReason) -> bool:
    """Return whether a normalized read-date reason should be retried."""
    return reason in _RETRYABLE_READ_DATE_REASONS


def normalize_read_date_reason(result: ReadDateFetchResult) -> ReadDateReason:
    """Return a valid reason for a result, including compatibility results."""
    if result.status == "complete":
        if result.read_at is None:
            raise ValueError("complete read-date result requires a non-null read_at")
        return ReadDateReason.RESOLVED
    if result.reason is not None:
        return ReadDateReason(result.reason)
    if result.status == "missing":
        return ReadDateReason.DATE_OMITTED
    if result.failure is not None:
        return {
            GatewayFailureKind.FLOOD_WAIT: ReadDateReason.FLOOD_WAIT,
            GatewayFailureKind.ACCESS_LOST: ReadDateReason.ACCESS_LOST,
            GatewayFailureKind.INVALID_TARGET: ReadDateReason.INVALID_TARGET,
            GatewayFailureKind.TRANSIENT: ReadDateReason.TRANSIENT,
        }[result.failure.kind]
    return ReadDateReason.TRANSIENT


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
    reason: ReadDateReason | None = None

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
