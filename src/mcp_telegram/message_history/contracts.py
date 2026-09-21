"""Immutable observations returned by message-history acquisition adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..message_contracts import ExtractedMessage

MESSAGE_HISTORY_PAGE_LIMIT: Final = 100
TOPIC_ATTRIBUTION_EXTRACTOR_VERSION: Final = 1


class MessageHistoryAccessLostError(RuntimeError):
    """The remote dialog is no longer accessible to the authenticated account."""

    reason_code: str

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class MessageHistoryUnavailableError(RuntimeError):
    """A message-history request failed with an ordinary remote error."""


class TopicAttributionPageProjectionError(RuntimeError):
    """A raw Telegram page cannot satisfy the campaign's narrow projection contract."""


@dataclass(frozen=True, slots=True)
class FullHistoryPage:
    """One normalized backward history page and its optional Telegram total."""

    messages: tuple[ExtractedMessage, ...]
    total_messages: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple):
            raise TypeError("messages must be a tuple")
        if len(self.messages) > MESSAGE_HISTORY_PAGE_LIMIT:
            raise ValueError("full history pages must contain at most 100 messages")
        if any(not isinstance(message, ExtractedMessage) for message in self.messages):
            raise TypeError("messages must contain ExtractedMessage values")
        if self.total_messages is not None and (
            isinstance(self.total_messages, bool) or not isinstance(self.total_messages, int) or self.total_messages < 0
        ):
            raise ValueError("total_messages must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class TopicAttributionMessage:
    """The only remote facts the temporary topic repair may consume."""

    message_id: int
    forum_topic_id: int | None

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int) or self.message_id < 1:
            raise ValueError("message_id must be a positive integer")
        if self.forum_topic_id is not None and (
            isinstance(self.forum_topic_id, bool) or not isinstance(self.forum_topic_id, int) or self.forum_topic_id < 1
        ):
            raise ValueError("forum_topic_id must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class TopicAttributionPage:
    """A raw backward Telegram page, projected without entity resolution."""

    messages: tuple[TopicAttributionMessage, ...]
    next_cursor: int | None
    complete: bool

    def __post_init__(self) -> None:
        _validate_topic_attribution_messages(self.messages)
        _validate_topic_attribution_cursor(self.messages, self.next_cursor)
        _validate_page_complete(self.complete)


def _validate_topic_attribution_messages(messages: object) -> None:
    if not isinstance(messages, tuple):
        raise TypeError("messages must be a tuple")
    if len(messages) > MESSAGE_HISTORY_PAGE_LIMIT:
        raise ValueError("topic-attribution pages must contain at most 100 messages")
    if any(not isinstance(message, TopicAttributionMessage) for message in messages):
        raise TypeError("messages must contain TopicAttributionMessage values")


def _validate_topic_attribution_cursor(messages: tuple[TopicAttributionMessage, ...], next_cursor: int | None) -> None:
    if next_cursor is not None and (
        isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor < 1
    ):
        raise ValueError("next_cursor must be a positive integer or None")
    if messages and next_cursor != min(message.message_id for message in messages):
        raise ValueError("next_cursor must be the oldest raw message id")
    if not messages and next_cursor is not None:
        raise ValueError("an empty page cannot have a next_cursor")


def _validate_page_complete(complete: object) -> None:
    if not isinstance(complete, bool):
        raise TypeError("complete must be a boolean")


@dataclass(frozen=True, slots=True)
class ForwardGapPage:
    """One normalized forward page plus whether its source was exhausted."""

    messages: tuple[ExtractedMessage, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple):
            raise TypeError("messages must be a tuple")
        if len(self.messages) > MESSAGE_HISTORY_PAGE_LIMIT:
            raise ValueError("forward gap pages must contain at most 100 messages")
        if any(not isinstance(message, ExtractedMessage) for message in self.messages):
            raise TypeError("messages must contain ExtractedMessage values")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a boolean")


__all__ = [
    "MESSAGE_HISTORY_PAGE_LIMIT",
    "ForwardGapPage",
    "FullHistoryPage",
    "MessageHistoryAccessLostError",
    "MessageHistoryUnavailableError",
    "TopicAttributionMessage",
    "TopicAttributionPage",
    "TopicAttributionPageProjectionError",
]
