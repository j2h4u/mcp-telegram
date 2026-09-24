"""Immutable observations returned by message-history acquisition adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..message_contracts import ExtractedMessage

MESSAGE_HISTORY_PAGE_SIZE: Final = 100
TOPIC_ATTRIBUTION_EXTRACTOR_VERSION: Final = 1


class MessageHistoryAccessLostError(RuntimeError):
    """The remote dialog is no longer accessible to the authenticated account."""

    reason_code: str

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class MessageHistoryUnavailableError(RuntimeError):
    """A message-history request failed with an ordinary remote error."""


@dataclass(frozen=True, slots=True)
class FullHistoryPage:
    """One normalized backward history page and its optional Telegram total."""

    messages: tuple[ExtractedMessage, ...]
    total_messages: int | None
    next_before_message_id: int | None

    def __post_init__(self) -> None:
        _validate_full_history_messages(self.messages)
        _validate_total_messages(self.total_messages)
        _validate_next_cursor(self.messages, self.next_before_message_id)


def _validate_full_history_messages(messages: tuple[ExtractedMessage, ...]) -> None:
    if not isinstance(messages, tuple):
        raise TypeError("messages must be a tuple")
    if len(messages) > MESSAGE_HISTORY_PAGE_SIZE:
        raise ValueError("full history pages must contain at most 100 messages")
    if any(not isinstance(message, ExtractedMessage) for message in messages):
        raise TypeError("messages must contain ExtractedMessage values")


def _validate_total_messages(total_messages: int | None) -> None:
    if total_messages is not None and (
        isinstance(total_messages, bool) or not isinstance(total_messages, int) or total_messages < 0
    ):
        raise ValueError("total_messages must be a non-negative integer or None")


def _validate_next_cursor(messages: tuple[ExtractedMessage, ...], cursor: int | None) -> None:
    if messages and cursor is None:
        raise ValueError("non-empty full history pages require a next cursor")
    if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor <= 0):
        raise ValueError("next_before_message_id must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class ForwardGapPage:
    """One normalized forward page plus whether its source was exhausted."""

    messages: tuple[ExtractedMessage, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple):
            raise TypeError("messages must be a tuple")
        if len(self.messages) > MESSAGE_HISTORY_PAGE_SIZE:
            raise ValueError("forward gap pages must contain at most 100 messages")
        if any(not isinstance(message, ExtractedMessage) for message in self.messages):
            raise TypeError("messages must contain ExtractedMessage values")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a boolean")


__all__ = [
    "MESSAGE_HISTORY_PAGE_SIZE",
    "ExtractedMessage",
    "ForwardGapPage",
    "FullHistoryPage",
    "MessageHistoryAccessLostError",
    "MessageHistoryUnavailableError",
]
