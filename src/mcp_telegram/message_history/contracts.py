"""Immutable observations returned by message-history acquisition adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..message_contracts import ExtractedMessage

_MAX_HISTORY_PAGE_SIZE: Final = 100


class MessageHistoryAccessLostError(RuntimeError):
    """The remote dialog is no longer accessible to the authenticated account."""


class MessageHistoryUnavailableError(RuntimeError):
    """A message-history request failed with an ordinary remote error."""


@dataclass(frozen=True, slots=True)
class FullHistoryPage:
    """One normalized backward history page and its optional Telegram total."""

    messages: tuple[ExtractedMessage, ...]
    total_messages: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple):
            raise TypeError("messages must be a tuple")
        if any(not isinstance(message, ExtractedMessage) for message in self.messages):
            raise TypeError("messages must contain ExtractedMessage values")
        if self.total_messages is not None and (
            isinstance(self.total_messages, bool) or not isinstance(self.total_messages, int) or self.total_messages < 0
        ):
            raise ValueError("total_messages must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class ForwardGapPage:
    """One normalized forward page plus whether its source was exhausted."""

    messages: tuple[ExtractedMessage, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple):
            raise TypeError("messages must be a tuple")
        if len(self.messages) > _MAX_HISTORY_PAGE_SIZE:
            raise ValueError("forward gap pages must contain at most 100 messages")
        if any(not isinstance(message, ExtractedMessage) for message in self.messages):
            raise TypeError("messages must contain ExtractedMessage values")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a boolean")


__all__ = [
    "ForwardGapPage",
    "FullHistoryPage",
    "MessageHistoryAccessLostError",
    "MessageHistoryUnavailableError",
]
