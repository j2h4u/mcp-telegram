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
from ..telegram_demand import UnclassifiedTelegramDemandError
from ..telegram_rpc_scheduler import current_rpc_scope
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


class _MessageLike(Protocol):
    id: int
    date: datetime | None


class _TopicsResultLike(Protocol):
    messages: list[_MessageLike]
    topics: tuple[_TopicLike, ...] | list[_TopicLike] | None


class _DocumentLike(Protocol):
    id: int
    attributes: tuple[object, ...] | list[object]


_TOPIC_PAGE_SIZE = 100


class TelethonTelegramTopicGateway(TelegramTopicGateway):
    """Topic adapter that inherits the caller's reconciliation RPC scope."""

    def __init__(self, client: TopicClient) -> None:
        self._client = client
        self._emoji_alt_by_id: dict[int, str] = {}

    async def fetch_topics(self, entity: object) -> tuple[TopicFact, ...]:
        result_topics: list[_TopicLike] = []
        seen_ids: set[int] = set()
        seen_cursors: set[tuple[int | None, int, int]] = set()
        offset_date = None
        offset_id = offset_topic = 0
        try:
            peer = cast(TypeInputPeer, await self._client.get_input_entity(entity))
            while True:
                result = await self._client(
                    GetForumTopicsRequest(
                        peer=peer,
                        offset_date=offset_date,
                        offset_id=offset_id,
                        offset_topic=offset_topic,
                        limit=_TOPIC_PAGE_SIZE,
                    )
                )
                page = cast(_TopicsResultLike, result).topics or ()
                count = _optional_int(getattr(result, "count", None))
                if not page:
                    if count is not None and len(seen_ids) < count:
                        raise TopicSourceUnavailableError(
                            "Telegram topic pagination ended before the complete snapshot"
                        )
                    break
                unseen = [topic for topic in page if int(topic.id) not in seen_ids]
                if not unseen:
                    raise TopicSourceUnavailableError("Telegram topic pagination made no progress")
                for topic in unseen:
                    if int(topic.id) not in seen_ids:
                        result_topics.append(topic)
                        seen_ids.add(int(topic.id))
                if count is not None and len(seen_ids) >= count:
                    break
                if count is None and len(page) < _TOPIC_PAGE_SIZE:
                    break
                offset_date, offset_id, offset_topic = _topic_cursor(cast(_TopicsResultLike, result), page[-1])
                cursor = (_timestamp(offset_date), offset_id, offset_topic)
                if cursor in seen_cursors:
                    raise TopicSourceUnavailableError("Telegram topic pagination repeated its cursor")
                seen_cursors.add(cursor)
        except TelegramRpcThrottled:
            raise
        except (RPCError, TypeError) as exc:
            raise TopicSourceUnavailableError("Telegram topic source is unavailable") from exc

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
        try:
            bounded_discovery = current_rpc_scope().demand_kind is not None
        except UnclassifiedTelegramDemandError:
            bounded_discovery = False
        # Optional icon labels must never spend the topic discovery demand's RPC allowance.
        if missing_ids and not bounded_discovery:
            try:
                documents = cast(
                    list[_DocumentLike],
                    await self._client(GetCustomEmojiDocumentsRequest(document_id=missing_ids)),
                )
            except TelegramRpcThrottled, RPCError, TypeError:
                return {
                    icon_id: self._emoji_alt_by_id[icon_id] for icon_id in icon_ids if icon_id in self._emoji_alt_by_id
                }
            self._emoji_alt_by_id.update(_custom_emoji_alts(documents))
        return {icon_id: self._emoji_alt_by_id[icon_id] for icon_id in icon_ids if icon_id in self._emoji_alt_by_id}


def _topic_cursor(result: _TopicsResultLike, last: _TopicLike) -> tuple[datetime | None, int, int]:
    offset_id = int(getattr(last, "top_message", 0))
    if getattr(result, "order_by_create_date", False):
        offset_date = last.date
    else:
        last_message = next((message for message in result.messages if message.id == offset_id), None)
        offset_date = last_message.date if last_message is not None else None
    if offset_date is None:
        raise TopicSourceUnavailableError("Telegram topic pagination is missing its date cursor")
    return offset_date, offset_id, int(last.id)


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
