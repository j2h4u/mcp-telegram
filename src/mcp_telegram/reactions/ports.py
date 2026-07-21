"""Narrow structural ports for the reaction capability."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Protocol

from .contracts import ReactionFetchResult, ReactionSnapshot


class TelegramReactionGateway(Protocol):
    async def fetch_reactions(self, entity: object, message_ids: Sequence[int]) -> ReactionFetchResult: ...


class ReactionSnapshotRepository(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...

    def stale_reaction_ids(
        self, dialog_id: int, message_ids: Sequence[int], threshold: int
    ) -> tuple[str, set[int], list[int]]: ...

    def persist_reaction_snapshots(
        self, dialog_id: int, snapshots: Sequence[ReactionSnapshot | None], checked_at: int
    ) -> int: ...
