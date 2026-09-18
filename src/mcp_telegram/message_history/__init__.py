"""Transport-neutral message-history contracts and Telegram adapters."""

from .contracts import (
    ForwardGapPage,
    FullHistoryPage,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from .ports import ForwardGapPagePort, FullHistoryPagePort

__all__ = [
    "ForwardGapPage",
    "ForwardGapPagePort",
    "FullHistoryPage",
    "FullHistoryPagePort",
    "MessageHistoryAccessLostError",
    "MessageHistoryUnavailableError",
]
