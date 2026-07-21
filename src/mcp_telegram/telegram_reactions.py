"""TTL-bound reaction refresh service and its Telethon adapter."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime
from typing import Protocol, cast

from telethon.tl import types
from telethon.tl.functions.messages import GetMessageReactionsListRequest

from .sync_worker import extract_message_row
from .telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from .telegram_reaction_queries import persist_reaction_snapshots, stale_reaction_ids
from .telegram_reading import (
    ReactionEvent,
    ReactionFetchResult,
    ReactionFreshness,
    ReactionMessage,
    TelegramReactionGateway,
)

REACTIONS_TTL_SECONDS = 600
logger = logging.getLogger(__name__)


class _TelegramClientLike(Protocol):
    async def get_messages(self, entity: object, ids: list[int]) -> object: ...

    async def __call__(self, request: object) -> object: ...

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

    @staticmethod
    def _peer_id(peer: object) -> int | None:
        """Extract a marked Telegram peer id without resolving remote entities."""
        peer_id: int | None = None
        if isinstance(peer, types.PeerUser):
            peer_id = peer.user_id
        elif isinstance(peer, types.PeerChat):
            peer_id = -peer.chat_id
        elif isinstance(peer, types.PeerChannel):
            peer_id = -1000000000000 - peer.channel_id
        else:
            # Keep the adapter friendly to narrow test doubles while retaining
            # a concrete object boundary for production Telethon responses.
            user_id = cast(object, getattr(peer, "user_id", None))
            chat_id = cast(object, getattr(peer, "chat_id", None))
            channel_id = cast(object, getattr(peer, "channel_id", None))
            if isinstance(user_id, int):
                peer_id = user_id
            elif isinstance(chat_id, int):
                peer_id = -chat_id
            elif isinstance(channel_id, int):
                peer_id = -1000000000000 - channel_id
        return peer_id

    @staticmethod
    def _emoji(reaction: object) -> str:
        if isinstance(reaction, types.ReactionEmoji):
            return reaction.emoticon
        if isinstance(reaction, types.ReactionCustomEmoji):
            return f"custom:{reaction.document_id}"
        if isinstance(reaction, types.ReactionPaid):
            return "paid"
        emoticon = cast(object, getattr(reaction, "emoticon", None))
        if isinstance(emoticon, str):
            return emoticon
        return str(reaction)

    @staticmethod
    def _timestamp(value: object) -> int | None:
        if isinstance(value, datetime):
            return int(value.timestamp())
        return None

    async def _fetch_reaction_events(self, entity: object, message_id: int) -> tuple[tuple[ReactionEvent, ...], str]:
        """Fetch individual reaction events with an explicit bounded completeness state."""
        if not callable(self._client):
            return (), "unavailable"
        events: list[ReactionEvent] = []
        offset: str | None = None
        # Telegram pages are deliberately bounded: a massive reaction list must
        # not turn a read into an unbounded network operation. The status records
        # whether all pages were observed, so partial data is never presented as
        # complete history.
        for _ in range(10):
            try:
                request = GetMessageReactionsListRequest(
                    peer=cast(types.TypeInputPeer, entity),
                    id=message_id,
                    limit=100,
                    offset=offset,
                )
                response = cast(types.messages.MessageReactionsList, await self._client(request))
                events.extend(
                    ReactionEvent(
                        reactor_id=self._peer_id(item.peer_id),
                        emoji=self._emoji(item.reaction),
                        reacted_at=self._timestamp(item.date),
                    )
                    for item in response.reactions
                )
                next_offset = response.next_offset
            except CATCHABLE_GATEWAY_FAILURES:
                return tuple(events), "unavailable"
            if next_offset is None:
                return tuple(events), "complete"
            try:
                offset = str(next_offset)
            except TypeError, ValueError:
                return tuple(events), "partial"
        return tuple(events), "partial"

    async def fetch_reactions(self, entity: object, message_ids: Sequence[int]) -> ReactionFetchResult:
        try:
            fetched = await self._client.get_messages(entity, ids=list(message_ids))
            messages_list: list[ReactionMessage | None] = []
            for message_id, message in zip(message_ids, cast(Sequence[object | None], fetched), strict=False):
                messages_list.append(
                    None if message is None else await self._message_with_events(entity, message_id, message)
                )
            messages = tuple(messages_list)
            return ReactionFetchResult(messages=messages)
        except CATCHABLE_GATEWAY_FAILURES as exc:
            return ReactionFetchResult(failure=translate_gateway_failure(exc))

    async def _message_with_events(self, entity: object, message_id: int, message: object) -> ReactionMessage:
        events: tuple[ReactionEvent, ...] = ()
        events_status = "unavailable"
        try:
            events, events_status = await self._fetch_reaction_events(entity, message_id)
        except CATCHABLE_GATEWAY_FAILURES:
            # Individual detail failures must not discard aggregate counters.
            events, events_status = (), "unavailable"
        return ReactionMessage(
            message_id,
            tuple(extract_message_row(0, message).reactions),
            events,
            events_status,
        )
