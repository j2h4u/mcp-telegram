"""Narrow structural ports for the reaction capability."""

from __future__ import annotations

from typing import Protocol

from .contracts import ReactionDetailFetchResult


class TelegramReactionGateway(Protocol):
    async def fetch_reaction_page(
        self, entity: object, message_id: int, *, offset: str | None, limit: int
    ) -> ReactionDetailFetchResult: ...
