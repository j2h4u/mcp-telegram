"""TTL-bound application service for refreshing reaction snapshots."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from .contracts import ReactionFreshness
from .ports import ReactionSnapshotRepository, TelegramReactionGateway

logger = logging.getLogger(__name__)


class _LoggerLike(Protocol):
    def warning(self, msg: str, *args: object) -> None: ...


class ReactionFreshener:
    """Refresh stale reactions only for ids in the active result window."""

    def __init__(
        self,
        repository: ReactionSnapshotRepository,
        gateway: TelegramReactionGateway,
        *,
        freshness_ttl_seconds: int,
        now: Callable[[], float] = time.time,
        log: _LoggerLike = logger,
    ) -> None:
        if (
            isinstance(freshness_ttl_seconds, bool)
            or not isinstance(freshness_ttl_seconds, int)
            or freshness_ttl_seconds < 1
        ):
            raise ValueError("freshness_ttl_seconds must be an integer >= 1")
        self._repository = repository
        self._gateway = gateway
        self._freshness_ttl_seconds = freshness_ttl_seconds
        self._now = now
        self._logger = log

    async def refresh(self, dialog_id: int, entity: object, message_ids: list[int]) -> ReactionFreshness:
        if not message_ids:
            return ReactionFreshness(0, 0, 0, 0, "not_requested")
        now = int(self._now())
        state, fresh_ids, stale_ids = self._repository.stale_reaction_ids(
            dialog_id, message_ids, now - self._freshness_ttl_seconds
        )
        if state != "active" or not stale_ids:
            return ReactionFreshness(
                len(message_ids), len(fresh_ids), len(stale_ids), 0, "fresh" if not stale_ids else state
            )
        result = await self._gateway.fetch_reactions(entity, stale_ids)
        if result.ok:
            # The use case owns this atomic write. The repository implementation
            # scopes it with a savepoint so an unrelated outer transaction is not
            # committed as a side effect of refreshing a read result.
            with self._repository.transaction():
                refreshed = self._repository.persist_reaction_snapshots(dialog_id, result.messages, now)
            return ReactionFreshness(len(message_ids), len(fresh_ids), len(stale_ids), refreshed, "refreshed")
        failure = result.failure
        assert failure is not None
        if failure.kind.value == "flood_wait":
            self._logger.warning(
                "jit_reactions_floodwait dialog_id=%d stale_count=%d seconds=%d",
                dialog_id,
                len(stale_ids),
                failure.retry_after or 0,
            )
        elif failure.kind.value != "access_lost":
            self._logger.warning("jit_reactions_failed dialog_id=%d error_type=%s", dialog_id, failure.error_type)
        return ReactionFreshness(
            len(message_ids), len(fresh_ids), len(stale_ids), 0, failure.kind.value, failure.retry_after
        )
