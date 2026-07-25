"""Telethon adapter for forum and private-bot topic snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast

from telethon.errors import FloodWaitError, RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetForumTopicsRequest  # type: ignore[import-untyped]
from telethon.tl.types import TypeInputPeer  # type: ignore[import-untyped]

from .contracts import TopicFact, TopicSourceUnavailableError
from .ports import TelegramTopicGateway


class TopicClient(Protocol):
    async def __call__(self, request: object) -> object: ...

    async def get_input_entity(self, entity: object) -> object: ...


class _TopicLike(Protocol):
    id: int
    title: str | None
    icon_emoji_id: int | None
    date: datetime | None


class _TopicsResultLike(Protocol):
    topics: tuple[_TopicLike, ...] | list[_TopicLike] | None


class TelethonTelegramTopicGateway(TelegramTopicGateway):
    def __init__(self, client: TopicClient) -> None:
        self._client = client

    async def fetch_topics(self, entity: object) -> tuple[TopicFact, ...]:
        try:
            peer = cast(TypeInputPeer, await self._client.get_input_entity(entity))
            result = await self._client(
                GetForumTopicsRequest(peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=100)
            )
        except FloodWaitError:
            raise
        except (RPCError, TypeError) as exc:
            raise TopicSourceUnavailableError("Telegram topic source is unavailable") from exc

        result_topics = cast(_TopicsResultLike, result).topics or ()
        return tuple(
            TopicFact(
                topic_id=int(topic.id),
                title=topic.title or "",
                icon_emoji_id=topic.icon_emoji_id,
                date=_timestamp(topic.date),
                is_general=_is_general(topic),
            )
            for topic in result_topics
        )


def _is_general(topic: _TopicLike) -> bool:
    return bool(getattr(topic, "is_general", False)) or int(topic.id) == 1


def _timestamp(value: object) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None
