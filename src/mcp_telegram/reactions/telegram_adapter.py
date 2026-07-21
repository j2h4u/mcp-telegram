"""Telethon implementation of the reaction gateway port."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast

from telethon.tl import types
from telethon.tl.functions.messages import GetMessageReactionsListRequest

from ..telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from .contracts import ReactionEvent, ReactionFetchResult, ReactionSnapshot
from .ports import TelegramReactionGateway
from .projection import project_reaction_aggregates


class _TelegramClientLike(Protocol):
    async def get_messages(self, entity: object, ids: list[int]) -> object: ...

    async def __call__(self, request: object) -> object: ...


class TelethonTelegramReactionGateway(TelegramReactionGateway):
    """Telethon gateway that projects only reaction facts from each message."""

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

    async def _fetch_reaction_events(self, entity: object, message_id: int) -> tuple[tuple[ReactionEvent, ...], str]:
        if not callable(self._client):
            return (), "unavailable"
        events: list[ReactionEvent] = []
        offset: str | None = None
        for _ in range(10):
            try:
                response = cast(
                    types.messages.MessageReactionsList,
                    await self._client(
                        GetMessageReactionsListRequest(
                            peer=cast(types.TypeInputPeer, entity), id=message_id, limit=100, offset=offset
                        )
                    ),
                )
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
            snapshots: list[ReactionSnapshot | None] = []
            for message_id, message in zip(message_ids, cast(Sequence[object | None], fetched), strict=False):
                snapshots.append(
                    None if message is None else await self._snapshot_with_events(entity, message_id, message)
                )
            return ReactionFetchResult(messages=tuple(snapshots))
        except CATCHABLE_GATEWAY_FAILURES as exc:
            return ReactionFetchResult(failure=translate_gateway_failure(exc))

    async def _snapshot_with_events(self, entity: object, message_id: int, message: object) -> ReactionSnapshot:
        events: tuple[ReactionEvent, ...] = ()
        events_status = "unavailable"
        try:
            events, events_status = await self._fetch_reaction_events(entity, message_id)
        except CATCHABLE_GATEWAY_FAILURES:
            pass
        return ReactionSnapshot(
            message_id=message_id,
            aggregates=project_reaction_aggregates(getattr(message, "reactions", None)),
            events=events,
            events_status=events_status,
        )
