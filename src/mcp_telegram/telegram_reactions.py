"""TTL-bound reaction refresh service and its Telethon adapter."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Protocol, cast

from .sync_worker import extract_message_row
from .telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from .telegram_reaction_queries import persist_reaction_snapshots, stale_reaction_ids
from .telegram_reading import ReactionFetchResult, ReactionFreshness, ReactionMessage, TelegramReactionGateway

REACTIONS_TTL_SECONDS = 600
logger = logging.getLogger(__name__)


class _TelegramClientLike(Protocol):
    async def get_messages(self, entity: object, ids: list[int]) -> object: ...

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]: ...


class _LoggerLike(Protocol):
    def warning(self, msg: str, *args: object) -> None: ...


class ReactionFreshener:
    """Refresh stale reactions only for ids in the active result window."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        gateway: TelegramReactionGateway,
        *,
        now: Callable[[], float] = time.time,
        log: _LoggerLike = logger,
    ) -> None:
        self._conn, self._gateway, self._now, self._logger = conn, gateway, now, log

    async def refresh(self, dialog_id: int, entity: object, message_ids: list[int]) -> ReactionFreshness:
        if not message_ids:
            return ReactionFreshness(0, 0, 0, 0, "not_requested")
        now = int(self._now())
        state, fresh_ids, stale_ids = stale_reaction_ids(
            self._conn, dialog_id, message_ids, now - REACTIONS_TTL_SECONDS
        )
        if state != "active" or not stale_ids:
            return ReactionFreshness(
                len(message_ids), len(fresh_ids), len(stale_ids), 0, "fresh" if not stale_ids else state
            )
        result = await self._gateway.fetch_reactions(entity, stale_ids)
        if result.ok:
            refreshed = persist_reaction_snapshots(self._conn, dialog_id, result.messages, now)
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


class TelethonTelegramReactionGateway:
    """Telethon adapter that normalizes reaction rows without SQLite access."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramClientLike, client)

    async def fetch_reactions(self, entity: object, message_ids: Sequence[int]) -> ReactionFetchResult:
        try:
            fetched = await self._client.get_messages(entity, ids=list(message_ids))
            messages = tuple(
                None
                if message is None
                else ReactionMessage(message_id, tuple(extract_message_row(0, message).reactions))
                for message_id, message in zip(message_ids, cast(Sequence[object | None], fetched), strict=False)
            )
            return ReactionFetchResult(messages=messages)
        except CATCHABLE_GATEWAY_FAILURES as exc:
            return ReactionFetchResult(failure=translate_gateway_failure(exc))
