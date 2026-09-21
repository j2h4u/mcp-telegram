"""Transport-neutral message-history contracts and Telegram adapters."""

from .contracts import (
    MESSAGE_HISTORY_PAGE_LIMIT,
    ForwardGapPage,
    FullHistoryPage,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from .ports import ForwardGapPagePort, FullHistoryPagePort

__all__ = [
    "MESSAGE_HISTORY_PAGE_LIMIT",
    "ForwardGapPage",
    "ForwardGapPagePort",
    "FullHistoryPage",
    "FullHistoryPagePort",
    "MessageHistoryAccessLostError",
    "MessageHistoryUnavailableError",
]
