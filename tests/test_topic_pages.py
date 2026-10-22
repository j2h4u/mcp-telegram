"""Single-page Telegram topic transport contracts, independent of read tools."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.tl.types import (
    ChannelForbidden,
    ChatForbidden,
    ForumTopic,
    ForumTopicDeleted,
    PeerNotifySettings,
    PeerUser,
)

from mcp_telegram.topics.contracts import TopicCursor, TopicSourceUnavailableError
from mcp_telegram.topics.telegram_adapter import TelethonTelegramTopicGateway


def _topic(topic_id: int) -> ForumTopic:
    return ForumTopic(
        id=topic_id,
        date=datetime.fromtimestamp(100 + topic_id, UTC),
        peer=PeerUser(42),
        title=f"Topic {topic_id}",
        icon_color=6,
        top_message=topic_id * 10,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=PeerUser(42),
        notify_settings=PeerNotifySettings(),
        icon_emoji_id=77,
    )


class _Client:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[GetForumTopicsRequest] = []
        self.entity_requests: list[object] = []

    async def get_input_entity(self, entity: object) -> object:
        self.entity_requests.append(entity)
        return PeerUser(42)

    async def get_entity(self, entity: object) -> object:
        raise AssertionError("a page fetch must use its supplied entity")

    async def __call__(self, request: object) -> object:
        assert isinstance(request, GetForumTopicsRequest), "optional emoji must not acquire inside page budget"
        self.requests.append(request)
        return self.response


class _EntityClient(_Client):
    async def get_entity(self, entity: object) -> object:
        assert entity == 42
        self.entity_requests.append(entity)
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [False, True])
async def test_concrete_forbidden_entity_reports_access_loss_not_missing_capability(channel: bool) -> None:
    entity = (
        ChannelForbidden(id=42, access_hash=0, title="Unavailable")
        if channel
        else ChatForbidden(id=42, title="Unavailable")
    )
    client = _EntityClient(entity)

    with pytest.raises(TopicSourceUnavailableError, match="^source_access_unavailable$"):
        await TelethonTelegramTopicGateway(client).fetch_entity(42)

    assert client.entity_requests == [42]
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_order", [False, True])
async def test_one_page_uses_exact_durable_cursor_and_server_order_date(creation_order: bool) -> None:
    client = _Client(
        SimpleNamespace(
            topics=[_topic(2)],
            count=4,
            messages=[SimpleNamespace(id=20, date=datetime.fromtimestamp(900, UTC))],
            order_by_create_date=creation_order,
        )
    )
    gateway = TelethonTelegramTopicGateway(client)

    page = await gateway.fetch_page("known entity", TopicCursor(120, 10, 1))

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.offset_date == datetime.fromtimestamp(120, UTC)
    assert (request.offset_id, request.offset_topic) == (10, 1)
    assert page.count == 4
    assert page.order_by_create_date is creation_order
    assert page.next_cursor == TopicCursor(102 if creation_order else 900, 20, 2)
    assert page.topics[0].title == "Topic 2"
    assert page.topics[0].icon_emoji_id == 77
    assert page.topics[0].icon_emoji is None


@pytest.mark.asyncio
async def test_short_page_with_remaining_count_retains_next_cursor() -> None:
    client = _Client(SimpleNamespace(topics=[_topic(1)], count=9, messages=[], order_by_create_date=True))

    page = await TelethonTelegramTopicGateway(client).fetch_page("entity", TopicCursor())

    assert page.count == 9
    assert len(page.topics) == 1
    assert page.next_cursor == TopicCursor(101, 10, 1)


@pytest.mark.asyncio
async def test_explicit_deleted_constructor_is_not_unknown_or_ordinary_empty() -> None:
    client = _Client(SimpleNamespace(topics=[ForumTopicDeleted(7)], count=1, messages=[], order_by_create_date=False))

    page = await TelethonTelegramTopicGateway(client).fetch_page("entity", TopicCursor())

    assert page.topics == ()
    assert page.deleted_ids == (7,)
    assert page.count == 1


@pytest.mark.asyncio
async def test_unknown_constructor_does_not_become_complete_empty() -> None:
    client = _Client(SimpleNamespace(topics=[SimpleNamespace(id=7)], count=1, messages=[], order_by_create_date=False))

    with pytest.raises(TopicSourceUnavailableError):
        await TelethonTelegramTopicGateway(client).fetch_page("entity", TopicCursor())


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [None, -1, True, "0"])
async def test_invalid_count_does_not_become_empty_receipt(count: object) -> None:
    client = _Client(SimpleNamespace(topics=[], count=count, messages=[], order_by_create_date=False))

    with pytest.raises(TopicSourceUnavailableError):
        await TelethonTelegramTopicGateway(client).fetch_page("entity", TopicCursor())


@pytest.mark.asyncio
async def test_missing_cursor_date_does_not_invent_pagination_position() -> None:
    client = _Client(SimpleNamespace(topics=[_topic(1)], count=2, messages=[], order_by_create_date=False))

    page = await TelethonTelegramTopicGateway(client).fetch_page("entity", TopicCursor())

    assert page.next_cursor is None
    assert page.count == 2


@pytest.mark.asyncio
async def test_deleted_last_row_cannot_invent_next_page_cursor() -> None:
    client = _Client(SimpleNamespace(topics=[ForumTopicDeleted(7)], count=2, messages=[], order_by_create_date=False))

    page = await TelethonTelegramTopicGateway(client).fetch_page("entity", TopicCursor())

    assert page.next_cursor is None
    assert page.deleted_ids == (7,)
    assert page.count == 2


@pytest.mark.asyncio
async def test_final_resumed_deleted_page_can_be_adjudicated_by_catalog_owner() -> None:
    client = _Client(SimpleNamespace(topics=[ForumTopicDeleted(7)], count=2, messages=[], order_by_create_date=False))

    page = await TelethonTelegramTopicGateway(client).fetch_page("entity", TopicCursor(100, 10, 1))

    assert page.next_cursor is None
    assert page.deleted_ids == (7,)
    assert page.count == 2
