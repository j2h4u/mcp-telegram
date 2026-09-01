"""Telethon adapter for forum and private-bot topic snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast

from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import (  # type: ignore[import-untyped]
    GetCustomEmojiDocumentsRequest,
    GetForumTopicsRequest,
)
from telethon.tl.types import DocumentAttributeCustomEmoji, TypeInputPeer  # type: ignore[import-untyped]

from ..flood import TelegramRpcThrottled
from .contracts import TopicFact, TopicSourceUnavailableError
from .ports import TelegramTopicGateway


class TopicClient(Protocol):
    async def __call__(self, request: object) -> object: ...

    async def get_input_entity(self, entity: object) -> object: ...


class _TopicLike(Protocol):
    id: int
    title: str | None
    icon_emoji_id: int | None
    icon_color: int | None
    date: datetime | None


class _TopicsResultLike(Protocol):
    topics: tuple[_TopicLike, ...] | list[_TopicLike] | None


class _DocumentLike(Protocol):
    id: int
    attributes: tuple[object, ...] | list[object]


class TelethonTelegramTopicGateway(TelegramTopicGateway):
    def __init__(self, client: TopicClient) -> None:
        self._client = client
        self._emoji_alt_by_id: dict[int, str] = {}

    async def fetch_topics(self, entity: object) -> tuple[TopicFact, ...]:
        try:
            peer = cast(TypeInputPeer, await self._client.get_input_entity(entity))
            result = await self._client(
                GetForumTopicsRequest(peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=100)
            )
        except TelegramRpcThrottled:
            raise
        except (RPCError, TypeError) as exc:
            raise TopicSourceUnavailableError("Telegram topic source is unavailable") from exc

        result_topics = cast(_TopicsResultLike, result).topics or ()
        emoji_by_id = await self._resolve_icon_emojis(result_topics)
        return tuple(
            TopicFact(
                topic_id=int(topic.id),
                title=topic.title or "",
                icon_emoji_id=topic.icon_emoji_id,
                icon_emoji=emoji_by_id.get(topic.icon_emoji_id) if topic.icon_emoji_id is not None else None,
                icon_color=_optional_int(getattr(topic, "icon_color", None)),
                date=_timestamp(topic.date),
                is_general=_is_general(topic),
            )
            for topic in result_topics
        )

    async def _resolve_icon_emojis(self, topics: tuple[_TopicLike, ...] | list[_TopicLike]) -> dict[int, str]:
        icon_ids = {int(topic.icon_emoji_id) for topic in topics if topic.icon_emoji_id is not None}
        missing_ids = sorted(icon_ids - self._emoji_alt_by_id.keys())
        if missing_ids:
            try:
                documents = cast(
                    list[_DocumentLike],
                    await self._client(GetCustomEmojiDocumentsRequest(document_id=missing_ids)),
                )
            except TelegramRpcThrottled:
                raise
            except RPCError, TypeError:
                return {
                    icon_id: self._emoji_alt_by_id[icon_id] for icon_id in icon_ids if icon_id in self._emoji_alt_by_id
                }
            self._emoji_alt_by_id.update(_custom_emoji_alts(documents))
        return {icon_id: self._emoji_alt_by_id[icon_id] for icon_id in icon_ids if icon_id in self._emoji_alt_by_id}


def _is_general(topic: _TopicLike) -> bool:
    return bool(getattr(topic, "is_general", False)) or int(topic.id) == 1


def _timestamp(value: object) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _custom_emoji_alts(documents: list[_DocumentLike]) -> dict[int, str]:
    resolved: dict[int, str] = {}
    for document in documents:
        attribute = next(
            (attribute for attribute in document.attributes if isinstance(attribute, DocumentAttributeCustomEmoji)),
            None,
        )
        if attribute is not None and attribute.alt:
            resolved[int(document.id)] = str(attribute.alt)
    return resolved
