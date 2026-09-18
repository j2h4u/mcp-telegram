"""Telethon implementation of the reaction gateway port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol, cast

from telethon.tl import types
from telethon.tl.functions.messages import GetMessageReactionsListRequest

from ..telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from ..telegram_rpc_scheduler import RpcAdmissionClosedError
from .contracts import (
    ReactionDetailFetchResult,
    ReactionDetailPage,
    ReactionEvent,
)
from .ports import TelegramReactionGateway


class _TelegramClientLike(Protocol):
    async def get_input_entity(self, entity: object) -> object: ...

    def __call__(self, request: object) -> Awaitable[object]: ...


class TelethonTelegramReactionGateway(TelegramReactionGateway):
    """Reaction adapter that inherits the caller's refresh RPC scope."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramClientLike, client)

    @staticmethod
    def _peer_id(peer: object) -> int | None:
        result: int | None = None
        if isinstance(peer, types.PeerUser):
            result = peer.user_id
        elif isinstance(peer, types.PeerChat):
            result = -peer.chat_id
        elif isinstance(peer, types.PeerChannel):
            result = -1000000000000 - peer.channel_id
        else:
            user_id = getattr(peer, "user_id", None)
            chat_id = getattr(peer, "chat_id", None)
            channel_id = getattr(peer, "channel_id", None)
            if isinstance(user_id, int):
                result = user_id
            elif isinstance(chat_id, int):
                result = -chat_id
            elif isinstance(channel_id, int):
                result = -1000000000000 - channel_id
        return result

    @staticmethod
    def _emoji(reaction: object) -> str:
        if isinstance(reaction, types.ReactionEmoji):
            return reaction.emoticon
        if isinstance(reaction, types.ReactionCustomEmoji):
            return f"custom:{reaction.document_id}"
        if isinstance(reaction, types.ReactionPaid):
            return "paid"
        emoticon = getattr(reaction, "emoticon", None)
        return emoticon if isinstance(emoticon, str) else str(reaction)

    @staticmethod
    def _timestamp(value: object) -> int | None:
        return int(value.timestamp()) if isinstance(value, datetime) else None

    async def fetch_reaction_page(
        self, entity: object, message_id: int, *, offset: str | None, limit: int
    ) -> ReactionDetailFetchResult:
        """Fetch exactly one detail page; aggregate data is never re-read here."""
        try:
            resolve = getattr(self._client, "get_input_entity", None)
            peer = (
                await cast(Callable[[object], Awaitable[object]], resolve)(entity)
                if isinstance(entity, int) and callable(resolve)
                else entity
            )
            response = cast(
                types.messages.MessageReactionsList,
                await self._client(
                    GetMessageReactionsListRequest(
                        peer=cast(types.TypeInputPeer, peer), id=message_id, limit=limit, offset=offset
                    )
                ),
            )
            next_raw = response.next_offset
            next_offset = None if next_raw is None else str(next_raw)
            events = tuple(
                ReactionEvent(
                    reactor_id=self._peer_id(item.peer_id),
                    emoji=self._emoji(item.reaction),
                    reacted_at=self._timestamp(item.date),
                )
                for item in response.reactions
            )
            return ReactionDetailFetchResult(page=ReactionDetailPage(events, next_offset))
        except RpcAdmissionClosedError:
            raise
        except CATCHABLE_GATEWAY_FAILURES as exc:
            return ReactionDetailFetchResult(failure=translate_gateway_failure(exc))
