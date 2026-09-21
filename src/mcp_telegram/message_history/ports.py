"""Focused transport-neutral ports for persistent message history."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .contracts import ForwardGapPage, FullHistoryPage, TopicAttributionPage


class FullHistoryPagePort(Protocol):
    """Fetch one backward page older than an exclusive local checkpoint."""

    async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage: ...


class ForwardGapPagePort(Protocol):
    """Fetch one bounded forward page newer than an exclusive local checkpoint."""

    async def fetch_page(
        self,
        dialog_id: int,
        *,
        after_message_id: int,
        should_stop: Callable[[], bool],
    ) -> ForwardGapPage: ...


class TopicAttributionPagePort(Protocol):
    """Fetch one raw page for the temporary topic-attribution campaign."""

    async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> TopicAttributionPage: ...


__all__ = ["ForwardGapPagePort", "FullHistoryPagePort", "TopicAttributionPagePort"]
