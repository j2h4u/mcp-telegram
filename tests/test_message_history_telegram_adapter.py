"""Boundary tests for the Telegram message-history adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.errors import ChannelPrivateError, RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetHistoryRequest  # type: ignore[import-untyped]
from telethon.tl.types import InputPeerUser  # type: ignore[import-untyped]

from helpers import MockTotalList, build_mock_message
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.message_contracts import ExtractedMessage, StoredMessage
from mcp_telegram.message_history.contracts import (
    FullHistoryPage,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
    TopicAttributionMessage,
    TopicAttributionPage,
    TopicAttributionPageProjectionError,
)
from mcp_telegram.message_history.telegram_adapter import (
    TelethonForwardGapPageAdapter,
    TelethonFullHistoryPageAdapter,
    TelethonHistoryAccessProbe,
    TelethonTopicAttributionPageAdapter,
)
from mcp_telegram.telegram_demand import demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred


class _Client:
    def __init__(self) -> None:
        self.get_messages_calls: list[dict[str, object]] = []
        self.iter_messages_calls: list[dict[str, object]] = []
        self.messages: list[object] = [build_mock_message(id=2), build_mock_message(id=1)]
        self.total = 42
        self.error: BaseException | None = None
        self.get_entity_calls = 0
        self.requests: list[object] = []
        self.session = SimpleNamespace(get_input_entity=lambda _dialog_id: InputPeerUser(7, 0))

    async def get_messages(self, **kwargs: object) -> MockTotalList:
        self.get_messages_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return MockTotalList(self.messages, total=self.total)

    async def get_entity(self, _peer: object) -> object:
        self.get_entity_calls += 1
        raise AssertionError("topic-attribution adapter must not resolve entities")

    async def __call__(self, request: object, **_kwargs: object) -> object:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(messages=self.messages)

    def iter_messages(self, **kwargs: object) -> AsyncIterator[object]:
        self.iter_messages_calls.append(kwargs)

        async def _iterate() -> AsyncIterator[object]:
            if self.error is not None:
                raise self.error
            for message in self.messages:
                yield message

        return _iterate()


@pytest.mark.asyncio
async def test_full_history_maps_one_backward_page_and_reads_total() -> None:
    client = _Client()
    with demand_context(DemandKind.FULL_SYNC_PAGE):
        page = await TelethonFullHistoryPageAdapter(client).fetch_page(7, before_message_id=13)

    assert client.get_messages_calls == [{"entity": 7, "limit": 100, "offset_id": 13}]
    assert [row.message.message_id for row in page.messages] == [2, 1]
    assert page.total_messages == 42


@pytest.mark.asyncio
async def test_topic_attribution_page_uses_one_rpc_without_forward_sender_resolution() -> None:
    client = _Client()
    message = build_mock_message(id=17)
    message.fwd_from = SimpleNamespace(from_name=None, from_id=SimpleNamespace(user_id=999))
    message.reply_to = SimpleNamespace(
        reply_to_msg_id=None,
        forum_topic=True,
        reply_to_top_id=11,
        reply_to_reply_top_id=None,
        reply_to_peer_id=None,
    )
    client.messages = [message]

    page = await TelethonTopicAttributionPageAdapter(client).fetch_page(7, before_message_id=23)

    assert client.get_messages_calls == []
    assert len(client.requests) == 1
    request = client.requests[0]
    assert isinstance(request, GetHistoryRequest)
    assert request.offset_id == 23
    assert request.limit == 100
    assert client.get_entity_calls == 0
    assert [(row.message_id, row.forum_topic_id) for row in page.messages] == [(17, 11)]
    assert page.next_cursor == 17
    assert page.complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        SimpleNamespace(reply_to=None),
        SimpleNamespace(
            id=1,
            reply_to=SimpleNamespace(
                reply_to_msg_id=None,
                forum_topic=True,
                reply_to_top_id="invalid",
                reply_to_reply_top_id=None,
                reply_to_peer_id=None,
            ),
        ),
    ],
)
async def test_topic_attribution_invalid_raw_projection_is_typed_after_one_request(message: object) -> None:
    client = _Client()
    client.messages = [message]

    with pytest.raises(TopicAttributionPageProjectionError):
        await TelethonTopicAttributionPageAdapter(client).fetch_page(7, before_message_id=0)

    assert len(client.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", [None, object()])
async def test_topic_attribution_invalid_session_peer_fails_without_request(peer: object | None) -> None:
    client = _Client()
    client.session = SimpleNamespace(get_input_entity=lambda _dialog_id: peer)

    with pytest.raises(MessageHistoryUnavailableError):
        await TelethonTopicAttributionPageAdapter(client).fetch_page(7, before_message_id=0)

    assert client.requests == []


def test_topic_attribution_page_retains_cursor_and_completion_invariants() -> None:
    message = TopicAttributionMessage(message_id=7, forum_topic_id=None)
    assert TopicAttributionPage(messages=(), next_cursor=None, complete=False).complete is False
    with pytest.raises(ValueError, match="oldest raw message id"):
        TopicAttributionPage(messages=(message,), next_cursor=8, complete=False)
    with pytest.raises(ValueError, match="empty page"):
        TopicAttributionPage(messages=(), next_cursor=7, complete=True)
    with pytest.raises(TypeError, match="complete"):
        TopicAttributionPage(messages=(message,), next_cursor=7, complete=cast(bool, "yes"))


@pytest.mark.asyncio
async def test_forward_gap_maps_one_bounded_exclusive_page() -> None:
    client = _Client()
    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7,
        after_message_id=13,
        should_stop=lambda: False,
    )

    assert client.iter_messages_calls == [{"entity": 7, "min_id": 13, "reverse": True, "limit": 100}]
    assert [row.message.message_id for row in page.messages] == [2, 1]
    assert page.complete is True


@pytest.mark.asyncio
async def test_forward_gap_empty_page_is_complete() -> None:
    client = _Client()
    client.messages = []

    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7,
        after_message_id=13,
        should_stop=lambda: False,
    )

    assert page.messages == ()
    assert page.complete is True


@pytest.mark.asyncio
async def test_forward_gap_exactly_full_page_requires_continuation() -> None:
    client = _Client()
    client.messages = [build_mock_message(id=message_id) for message_id in range(1, 101)]

    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7,
        after_message_id=13,
        should_stop=lambda: False,
    )

    assert len(page.messages) == 100
    assert page.complete is False


@pytest.mark.asyncio
async def test_forward_gap_marks_interruption_before_normalization() -> None:
    client = _Client()
    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7,
        after_message_id=13,
        should_stop=lambda: True,
    )

    assert page.messages == ()
    assert page.complete is False


@pytest.mark.asyncio
async def test_forward_gap_keeps_received_rows_when_interrupted() -> None:
    client = _Client()
    checks = 0

    def should_stop() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7,
        after_message_id=13,
        should_stop=should_stop,
    )

    assert [row.message.message_id for row in page.messages] == [2]
    assert page.complete is False


@pytest.mark.asyncio
async def test_access_probe_is_separate_and_uses_limit_one() -> None:
    client = _Client()
    total = await TelethonHistoryAccessProbe(client).probe_total_messages(7)

    assert total == 42
    assert client.get_messages_calls == [{"entity": 7, "limit": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_kind", ["full", "forward", "probe"])
async def test_access_loss_is_translated_at_telegram_boundary(adapter_kind: str) -> None:
    client = _Client()
    client.error = ChannelPrivateError(request=None)
    with pytest.raises(MessageHistoryAccessLostError) as caught:
        if adapter_kind == "full":
            await TelethonFullHistoryPageAdapter(client).fetch_page(7, before_message_id=0)
        elif adapter_kind == "forward":
            await TelethonForwardGapPageAdapter(client).fetch_page(7, after_message_id=0, should_stop=lambda: False)
        else:
            await TelethonHistoryAccessProbe(client).probe_total_messages(7)
    assert caught.value.reason_code == "ChannelPrivateError"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [TelegramRpcAdmissionDeferred(retry_after_seconds=3), TelegramRpcThrottled(retry_after_seconds=4)],
)
async def test_forward_gap_passes_scheduler_outcomes_without_translation(error: BaseException) -> None:
    client = _Client()
    client.error = error

    with pytest.raises(type(error)) as caught:
        await TelethonForwardGapPageAdapter(client).fetch_page(7, after_message_id=0, should_stop=lambda: False)

    assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_kind", ["full", "forward", "probe"])
async def test_ordinary_rpc_failure_is_translated_at_telegram_boundary(adapter_kind: str) -> None:
    client = _Client()
    client.error = RPCError(None, "history failed")
    with pytest.raises(MessageHistoryUnavailableError):
        if adapter_kind == "full":
            await TelethonFullHistoryPageAdapter(client).fetch_page(7, before_message_id=0)
        elif adapter_kind == "forward":
            await TelethonForwardGapPageAdapter(client).fetch_page(7, after_message_id=0, should_stop=lambda: False)
        else:
            await TelethonHistoryAccessProbe(client).probe_total_messages(7)


def test_full_history_page_rejects_more_than_protocol_limit() -> None:
    message = ExtractedMessage(
        message=StoredMessage(
            dialog_id=1,
            message_id=1,
            sent_at=1,
            text=None,
            sender_id=None,
            sender_first_name=None,
            reply_to_msg_id=None,
            forum_topic_id=None,
            edit_date=None,
            grouped_id=None,
            reply_to_peer_id=None,
            out=0,
            is_service=0,
            post_author=None,
        ),
        reply_count=0,
    )
    with pytest.raises(ValueError, match="at most 100"):
        FullHistoryPage(messages=(message,) * 101, total_messages=None)
