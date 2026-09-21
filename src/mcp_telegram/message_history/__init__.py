"""Transport-neutral message-history contracts and Telegram adapters."""

from .contracts import (
    HISTORY_PAGE_SIZE,
    ForwardGapPage,
    FullHistoryPage,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from .ports import ForwardGapPagePort, FullHistoryPagePort

__all__ = [
    "HISTORY_PAGE_SIZE",
    "ForwardGapPage",
    "ForwardGapPagePort",
    "FullHistoryPage",
    "FullHistoryPagePort",
    "MessageHistoryAccessLostError",
    "MessageHistoryUnavailableError",
]
