"""PrefetchCoordinator and background task coroutines for prefetch and delta refresh.

PrefetchCoordinator owns the in-flight dedup set and schedules asyncio tasks
via schedule(). Background tasks call MessageCache.store_messages() using the
same write path as live reads. No timer, sleep, or periodic loop — all prefetch
is triggered by user reads (Plan 02 wires this into capability_history).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cache import MessageCache
    from .pagination import HistoryDirection

logger = logging.getLogger(__name__)

PrefetchKey = tuple[int, str, int | None, int | None]


class PrefetchCoordinator:
    """Dedup coordinator for fire-and-forget background prefetch tasks.

    Maintains a set of in-flight keys so that a second schedule() call for the
    same dialog/direction/anchor tuple is a no-op. Keys are removed in the
    finally block of _run so failed tasks can be retried on the next user read.
    """

    def __init__(self) -> None:
        self._in_flight: set[PrefetchKey] = set()

    def schedule(
        self,
        coro: Coroutine[Any, Any, None],
        *,
        key: PrefetchKey,
    ) -> bool:
        """Schedule coro as an asyncio background task unless key is already in-flight.

        Returns True when the task was created, False when deduped.
        Callers must be running inside an asyncio event loop.
        """
        if key in self._in_flight:
            coro.close()  # prevent "coroutine never awaited" ResourceWarning
            return False
        self._in_flight.add(key)
        task = asyncio.create_task(self._run(coro, key=key))
        task.add_done_callback(lambda t: None)
        return True

    async def _run(
        self,
        coro: Coroutine[Any, Any, None],
        *,
        key: PrefetchKey,
    ) -> None:
        """Await coro, log result, and always release the key on exit."""
        try:
            await coro
            logger.debug("prefetch_done key=%r", key)
        except Exception:
            logger.warning("prefetch_failed key=%r", key, exc_info=True)
        finally:
            self._in_flight.discard(key)


def _next_prefetch_anchor(
    messages: list[object],
    direction: HistoryDirection,
) -> int | None:
    """Return the anchor_id for the next prefetch page given the current page.

    NEWEST: min(ids) — the next older page starts below the lowest id seen.
    OLDEST: max(ids) — the next newer page starts above the highest id seen.
    Returns None for an empty list.
    """
    from .pagination import HistoryDirection as _HD

    if not messages:
        return None
    ids = [getattr(m, "id", 0) for m in messages]
    if direction == _HD.NEWEST:
        return min(ids)
    return max(ids)


async def _prefetch_task(
    client: object,
    msg_cache: MessageCache,
    entity_id: int,
    direction: HistoryDirection,
    anchor_id: int | None,
    limit: int,
    topic_id: int | None,
) -> None:
    """Fetch one page of messages and write to MessageCache.

    NEWEST: uses max_id=anchor_id to fetch messages older than the anchor.
    OLDEST: uses min_id=anchor_id (or 1 when None) with reverse=True to fetch
            messages newer than the anchor (i.e. forward from the beginning).
    topic_id != 1 and != None adds reply_to for forum topic scoping.
    """
    from .pagination import HistoryDirection as _HD

    iter_kwargs: dict[str, object] = {"entity": entity_id, "limit": limit}
    if direction == _HD.NEWEST:
        iter_kwargs["max_id"] = anchor_id
    else:
        iter_kwargs["min_id"] = anchor_id if anchor_id is not None else 1
        iter_kwargs["reverse"] = True
    if topic_id is not None and topic_id != 1:
        iter_kwargs["reply_to"] = topic_id

    results = [msg async for msg in client.iter_messages(**iter_kwargs)]  # type: ignore[attr-defined]
    if results:
        msg_cache.store_messages(entity_id, results)


async def _delta_refresh_task(
    client: object,
    msg_cache: MessageCache,
    entity_id: int,
    last_id: int,
    limit: int,
    topic_id: int | None,
) -> None:
    """Fetch messages newer than last_id and write to MessageCache.

    Uses min_id=last_id with reverse=True so results arrive oldest-first and
    fill in the gap between the cached page and the current Telegram head.
    topic_id != 1 and != None adds reply_to for forum topic scoping.
    """
    iter_kwargs: dict[str, object] = {
        "entity": entity_id,
        "min_id": last_id,
        "limit": limit,
        "reverse": True,
    }
    if topic_id is not None and topic_id != 1:
        iter_kwargs["reply_to"] = topic_id

    results = [msg async for msg in client.iter_messages(**iter_kwargs)]  # type: ignore[attr-defined]
    if results:
        msg_cache.store_messages(entity_id, results)
