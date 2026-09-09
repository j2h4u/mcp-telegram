"""TTL-bound application service for refreshing reaction snapshots."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..telegram_rpc_scheduler import TelegramRpcSource, preserve_or_rpc_scope
from .contracts import ReactionFreshness, ReactionSnapshot
from .ports import ReactionSnapshotRepository, TelegramReactionGateway

logger = logging.getLogger(__name__)
_PERSISTENCE_RETRY_DELAYS_SECONDS = (0.25, 1.0, 2.0)


class _LoggerLike(Protocol):
    def warning(self, msg: str, *args: object) -> None: ...


@dataclass(frozen=True, slots=True)
class _FetchedPersistence:
    dialog_id: int
    requested_ids: list[int]
    fresh_ids: set[int]
    stale_ids: list[int]
    messages: Sequence[ReactionSnapshot | None]
    checked_at: int


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

    async def _persist_fetched_snapshot(self, fetched: _FetchedPersistence) -> ReactionFreshness:
        """Persist one fetched result, retrying local writer contention only."""
        for attempt, delay in enumerate((0.0, *_PERSISTENCE_RETRY_DELAYS_SECONDS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                with self._repository.transaction():
                    if not self._repository.history_enabled(fetched.dialog_id):
                        return ReactionFreshness(
                            len(fetched.requested_ids), len(fetched.fresh_ids), len(fetched.stale_ids), 0, "disabled"
                        )
                    refreshed = self._repository.persist_reaction_snapshots(
                        fetched.dialog_id, fetched.messages, fetched.checked_at
                    )
                return ReactionFreshness(
                    len(fetched.requested_ids),
                    len(fetched.fresh_ids),
                    len(fetched.stale_ids),
                    refreshed,
                    "refreshed",
                )
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if (
                    ("locked" not in message and "busy" not in message)
                    or attempt == len(_PERSISTENCE_RETRY_DELAYS_SECONDS)
                ):
                    raise
                self._logger.warning(
                    "reaction_snapshot_persist_busy dialog_id=%d attempt=%d",
                    fetched.dialog_id,
                    attempt + 1,
                )
        raise AssertionError("unreachable")

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
        with preserve_or_rpc_scope(TelegramRpcSource.REACTION_REFRESH):
            result = await self._gateway.fetch_reactions(entity, stale_ids)
        if result.ok:
            return await self._persist_fetched_snapshot(
                _FetchedPersistence(dialog_id, message_ids, fresh_ids, stale_ids, result.messages, now)
            )
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
