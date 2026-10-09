"""Focused tests for the topic refresh capability boundary."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest, GetForumTopicsRequest
from telethon.tl.types import DocumentAttributeCustomEmoji, InputStickerSetEmpty

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_demand import AcquisitionKind, RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcSource,
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)
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
        self.scopes: list[tuple[TelegramRpcSource, DemandKind, AcquisitionKind | None]] = []

    async def fetch_topics(self, entity: object) -> tuple[TopicFact, ...]:
        self.entities.append(entity)
        scope = current_rpc_scope()
        assert scope.demand_kind is not None
        self.scopes.append((scope.source, scope.demand_kind, scope.acquisition_kind))
        return (TopicFact(topic_id=1, title="General", is_general=True),)


class _Repository:
    def __init__(self) -> None:
        self.writes: list[tuple[int, tuple[TopicFact, ...]]] = []

    def begin_snapshot(self) -> int:
        return 1

    def upsert_topics(
        self, dialog_id: int, topics: tuple[TopicFact, ...], *, observation_order: int | None = None
    ) -> None:
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
async def test_topic_refresh_preserves_interactive_resolution_scope() -> None:
    gateway = _Gateway()
    repository = _Repository()

    with rpc_scope(TelegramRpcSource.TOPIC_RESOLUTION):
        await TopicRefresher(gateway, repository).refresh(42, _Entity(forum=True))

    assert gateway.scopes == [
        (
            TelegramRpcSource.TOPIC_RESOLUTION,
            DemandKind.TOPIC_LOOKUP,
            AcquisitionKind.TOPIC_SNAPSHOT,
        )
    ]


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
async def test_telethon_gateway_resolves_custom_topic_icon_to_unicode_emoji() -> None:
    class Client:
        def __init__(self) -> None:
            self.requests: list[object] = []

        async def get_input_entity(self, entity: object) -> object:
            return entity

        async def __call__(self, request: object) -> object:
            self.requests.append(request)
            if isinstance(request, GetForumTopicsRequest):
                topic = SimpleNamespace(
                    id=306001,
                    title=".",
                    icon_emoji_id=987,
                    icon_color=0x6FB9F0,
                    date=None,
                )
                return SimpleNamespace(topics=[topic])
            assert isinstance(request, GetCustomEmojiDocumentsRequest)
            attribute = DocumentAttributeCustomEmoji(alt="📊", stickerset=InputStickerSetEmpty())
            return [SimpleNamespace(id=987, attributes=[attribute])]

    client = Client()
    gateway = TelethonTelegramTopicGateway(client)

    first = await gateway.fetch_topics(object())
    second = await gateway.fetch_topics(object())

    assert (
        first
        == second
        == (
            TopicFact(
                topic_id=306001,
                title=".",
                icon_emoji_id=987,
                icon_emoji="📊",
                icon_color=0x6FB9F0,
            ),
        )
    )
    custom_emoji_requests = [
        request for request in client.requests if isinstance(request, GetCustomEmojiDocumentsRequest)
    ]
    assert len(custom_emoji_requests) == 1


@pytest.mark.asyncio
async def test_telethon_gateway_does_not_mask_flood_wait() -> None:
    class Client:
        async def get_input_entity(self, entity: object) -> object:
            return entity

        async def __call__(self, request: object) -> object:
            raise TelegramRpcThrottled(retry_after_seconds=3)

    with pytest.raises(TelegramRpcThrottled):
        await TelethonTelegramTopicGateway(Client()).fetch_topics(object())


def _page(topic_ids: list[int], *, count: int, create_order: bool = False) -> SimpleNamespace:
    from datetime import UTC, datetime

    return SimpleNamespace(
        topics=[
            SimpleNamespace(
                id=topic_id,
                title=str(topic_id),
                icon_emoji_id=987,
                date=datetime(2020, 1, 1, tzinfo=UTC),
                top_message=topic_id + 1000,
            )
            for topic_id in topic_ids
        ],
        messages=[SimpleNamespace(id=topic_id + 1000, date=datetime(2026, 1, 1, tzinfo=UTC)) for topic_id in topic_ids],
        count=count,
        order_by_create_date=create_order,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("create_order", [False, True])
async def test_topic_pagination_preserves_demand_and_deduplicates(create_order: bool) -> None:
    class Client:
        def __init__(self) -> None:
            self.requests: list[GetForumTopicsRequest] = []
            self.scopes: list[object] = []

        async def get_input_entity(self, entity: object) -> object:
            return entity

        async def __call__(self, request: object) -> object:
            assert isinstance(request, GetForumTopicsRequest)
            assert isinstance(request, GetForumTopicsRequest)  # No optional emoji RPC spends discovery budget.
            self.requests.append(request)
            scope = current_rpc_scope()
            self.scopes.append(scope.demand_kind)
            assert scope.attempt_budget is not None
            scope.attempt_budget.debit()
            if len(self.requests) == 1:
                return _page(list(range(1, 101)), count=150, create_order=create_order)
            return _page(list(range(100, 151)), count=150, create_order=create_order)

    client = Client()
    budget = RpcAttemptBudget(limit=2)
    with rpc_scope(TelegramRpcSource.TOPIC_RECONCILIATION), rpc_attempt_budget(budget):
        topics = await TelethonTelegramTopicGateway(client).fetch_topics(object())
    assert budget.attempts == 2
    assert len(topics) == 150
    assert len(client.requests) == 2
    assert client.scopes == [DemandKind.TOPIC_SNAPSHOT, DemandKind.TOPIC_SNAPSHOT]
    assert client.requests[1].offset_id == 1100
    assert client.requests[1].offset_topic == 100
    assert client.requests[1].offset_date is not None
    assert client.requests[1].offset_date.year == (2020 if create_order else 2026)


@pytest.mark.asyncio
async def test_topic_short_server_pages_continue_until_count() -> None:
    class Client:
        async def get_input_entity(self, entity: object) -> object:
            return entity

        async def __call__(self, request: object) -> object:
            assert isinstance(request, GetForumTopicsRequest)
            return _page([request.offset_topic + 1], count=3)

    with rpc_scope(TelegramRpcSource.TOPIC_RECONCILIATION):
        topics = await TelethonTelegramTopicGateway(Client()).fetch_topics(object())
    assert [topic.topic_id for topic in topics] == [1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deferred", "budget", "nonprogress", "empty"])
async def test_partial_topic_pages_never_publish_snapshot(failure: str) -> None:
    from mcp_telegram.telegram_demand import RpcAttemptBudgetExhaustedError
    from mcp_telegram.topics.contracts import TopicSourceUnavailableError

    class Client:
        async def get_input_entity(self, entity: object) -> object:
            return entity

        async def __call__(self, request: object) -> object:
            assert isinstance(request, GetForumTopicsRequest)
            if request.offset_topic:
                if failure == "deferred":
                    raise TelegramRpcThrottled(retry_after_seconds=3)
                if failure == "budget":
                    raise RpcAttemptBudgetExhaustedError("budget exhausted")
                if failure == "empty":
                    return _page([], count=200)
            return _page(list(range(1, 101)), count=200)

    repository = _Repository()
    error = (
        TelegramRpcThrottled
        if failure == "deferred"
        else RpcAttemptBudgetExhaustedError
        if failure == "budget"
        else TopicSourceUnavailableError
    )
    with pytest.raises(error):
        await TopicRefresher(TelethonTelegramTopicGateway(Client()), repository).refresh(1, _Entity(forum=True))
    assert repository.writes == []
