"""Focused tests for the topic refresh capability boundary."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetForumTopicsRequest

from mcp_telegram.topics.contracts import TopicFact, is_topic_capable
from mcp_telegram.topics.refresh import TopicRefresher
from mcp_telegram.topics.telegram_adapter import TelethonTelegramTopicGateway


@dataclass
class _Entity:
    forum: bool = False
    bot: bool = False
    bot_forum_view: bool = False


class _Gateway:
    def __init__(self) -> None:
        self.entities: list[object] = []

    async def fetch_topics(self, entity: object) -> tuple[TopicFact, ...]:
        self.entities.append(entity)
        return (TopicFact(topic_id=1, title="General", is_general=True),)


class _Repository:
    def __init__(self) -> None:
        self.writes: list[tuple[int, tuple[TopicFact, ...]]] = []

    def upsert_topics(self, dialog_id: int, topics: tuple[TopicFact, ...]) -> None:
        self.writes.append((dialog_id, topics))


def test_topic_capability_includes_forum_supergroups_and_private_bot_views() -> None:
    assert is_topic_capable(_Entity(forum=True))
    assert is_topic_capable(_Entity(bot=True, bot_forum_view=True))
    assert not is_topic_capable(_Entity(bot=True))
    assert not is_topic_capable(_Entity(bot_forum_view=True))


@pytest.mark.asyncio
async def test_refreshes_private_bot_topics_when_bot_forum_view_is_enabled() -> None:
    gateway = _Gateway()
    repository = _Repository()
    bot = _Entity(bot=True, bot_forum_view=True)

    count = await TopicRefresher(gateway, repository).refresh(8583106747, bot)

    assert count == 1
    assert gateway.entities == [bot]
    assert repository.writes == [(8583106747, (TopicFact(topic_id=1, title="General", is_general=True),))]


@pytest.mark.asyncio
async def test_does_not_fetch_topics_for_ordinary_private_bot() -> None:
    gateway = _Gateway()
    repository = _Repository()

    count = await TopicRefresher(gateway, repository).refresh(42, _Entity(bot=True))

    assert count == 0
    assert gateway.entities == []
    assert repository.writes == []


@pytest.mark.asyncio
async def test_telethon_gateway_uses_input_peer_before_fetching_topics() -> None:
    class Client:
        def __init__(self) -> None:
            self.input_requests: list[object] = []
            self.requests: list[object] = []

        async def get_input_entity(self, entity: object) -> object:
            self.input_requests.append(entity)
            return "input-peer"

        async def __call__(self, request: object) -> object:
            self.requests.append(request)
            topic = SimpleNamespace(id=306001, title="Topic", icon_emoji_id=None, date=None)
            return SimpleNamespace(topics=[topic])

    client = Client()
    entity = object()

    topics = await TelethonTelegramTopicGateway(client).fetch_topics(entity)

    assert client.input_requests == [entity]
    assert len(client.requests) == 1
    assert isinstance(client.requests[0], GetForumTopicsRequest)
    assert topics == (TopicFact(topic_id=306001, title="Topic"),)


@pytest.mark.asyncio
async def test_telethon_gateway_does_not_mask_flood_wait() -> None:
    class Client:
        async def get_input_entity(self, entity: object) -> object:
            return entity

        async def __call__(self, request: object) -> object:
            raise FloodWaitError(request=request, capture=3)

    with pytest.raises(FloodWaitError):
        await TelethonTelegramTopicGateway(Client()).fetch_topics(object())
