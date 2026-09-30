"""Boundary tests for the Telegram message-history adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import pytest
from telethon.errors import ChannelPrivateError, RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetHistoryRequest  # type: ignore[import-untyped]

from helpers import MockTotalList, build_mock_message
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.message_contracts import ExtractedMessage, StoredMessage
from mcp_telegram.message_history.contracts import (
    FullHistoryPage,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from mcp_telegram.message_history.telegram_adapter import (
    TelethonForwardGapPageAdapter,
    TelethonFullHistoryPageAdapter,
    TelethonHistoryAccessProbe,
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
        self.requests: list[object] = []

    async def get_input_entity(self, peer: object) -> object:
        return peer

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        raw_request = cast(GetHistoryRequest, request)
        response = await self.get_messages(entity=7, limit=100, offset_id=raw_request.offset_id)
        return type("History", (), {"messages": list(response), "count": response.total, "users": [], "chats": []})()

    async def get_messages(self, **kwargs: object) -> MockTotalList:
        self.get_messages_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return MockTotalList(self.messages, total=self.total)

    def iter_messages(self, **kwargs: object) -> AsyncIterator[object]:
        self.iter_messages_calls.append(kwargs)

        async def _iterate() -> AsyncIterator[object]:
            if self.error is not None:
                raise self.error
            for message in self.messages:
                yield message

        return _iterate()


class _ForwardClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.forward_response: object | None = None
        self.remote_peer_lookups = 0
        self.session = _PeerSession()

    async def get_input_entity(self, peer: object) -> object:
        self.remote_peer_lookups += 1
        return await super().get_input_entity(peer)

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.forward_response is not None:
            return self.forward_response
        return types.messages.Messages(messages=self.messages, topics=[], chats=[], users=[])


class _PeerSession:
    def __init__(self) -> None:
        self.peer: object = types.InputPeerUser(user_id=7, access_hash=123)

    def get_input_entity(self, _peer: object) -> object:
        if isinstance(self.peer, BaseException):
            raise self.peer
        return self.peer


@pytest.mark.asyncio
async def test_full_history_maps_one_backward_page_and_reads_total() -> None:
    client = _Client()
    with demand_context(DemandKind.FULL_SYNC_PAGE):
        page = await TelethonFullHistoryPageAdapter(client).fetch_page(7, before_message_id=13)

    assert len(client.requests) == 1
    request = cast(GetHistoryRequest, client.requests[0])
    assert request.limit == 100
    assert request.offset_id == 13
    assert [row.message.message_id for row in page.messages] == [2, 1]
    assert page.total_messages == 42
    assert page.next_before_message_id == 1


@pytest.mark.asyncio
async def test_full_history_cursor_includes_message_empty_and_filters_it_from_rows() -> None:
    client = _Client()
    client.messages = [
        build_mock_message(id=900),
        types.MessageEmpty(id=800, peer_id=types.PeerUser(user_id=7)),
        build_mock_message(id=700),
    ]

    page = await TelethonFullHistoryPageAdapter(client).fetch_page(7, before_message_id=0)

    assert [row.message.message_id for row in page.messages] == [900, 700]
    assert page.next_before_message_id == 700


@pytest.mark.asyncio
async def test_all_message_empty_page_advances_cursor_without_rows() -> None:
    client = _Client()
    client.messages = [types.MessageEmpty(id=800, peer_id=types.PeerUser(user_id=7))]

    page = await TelethonFullHistoryPageAdapter(client).fetch_page(7, before_message_id=900)

    assert page.messages == ()
    assert page.next_before_message_id == 800


@pytest.mark.asyncio
async def test_nonempty_raw_page_without_positive_ids_is_unavailable() -> None:
    client = _Client()
    client.messages = [object()]

    with pytest.raises(MessageHistoryUnavailableError, match="no usable message IDs"):
        await TelethonFullHistoryPageAdapter(client).fetch_page(7, before_message_id=0)


@pytest.mark.asyncio
async def test_forward_gap_maps_one_bounded_exclusive_page() -> None:
    client = _ForwardClient()
    client.messages = [build_mock_message(id=15), build_mock_message(id=14)]
    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7,
        after_message_id=13,
        should_stop=lambda: False,
    )

    request = cast(GetHistoryRequest, client.requests[0])
    assert (request.offset_id, request.add_offset, request.limit) == (14, -100, 100)
    assert [row.message.message_id for row in page.messages] == [14, 15]
    assert page.complete is True


@pytest.mark.asyncio
async def test_forward_gap_empty_page_is_complete() -> None:
    client = _ForwardClient()
    client.messages = []

    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7,
        after_message_id=13,
        should_stop=lambda: False,
    )

    assert page.messages == ()
    assert page.complete is True


@pytest.mark.asyncio
async def test_forward_gap_uncached_peer_does_not_try_remote_resolution() -> None:
    client = _ForwardClient()
    client.session.peer = ValueError("peer is not cached")

    with pytest.raises(MessageHistoryUnavailableError, match="peer is not cached"):
        await TelethonForwardGapPageAdapter(client).fetch_page(
            7, after_message_id=13, should_stop=lambda: False
        )

    assert client.remote_peer_lookups == 0
    assert client.requests == []


@pytest.mark.asyncio
async def test_forward_gap_full_message_empty_page_is_terminal() -> None:
    client = _ForwardClient()
    client.messages = [types.MessageEmpty(id=message_id, peer_id=types.PeerUser(user_id=7)) for message_id in range(1, 101)]

    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7, after_message_id=0, should_stop=lambda: False
    )

    assert page.messages == ()
    assert page.complete is True


@pytest.mark.asyncio
async def test_forward_gap_short_slice_commits_then_confirms_empty_terminal_page() -> None:
    client = _ForwardClient()
    client.forward_response = types.messages.MessagesSlice(
        count=50,
        messages=[build_mock_message(id=14)],
        topics=[],
        chats=[],
        users=[],
    )

    adapter = TelethonForwardGapPageAdapter(client)
    first = await adapter.fetch_page(7, after_message_id=13, should_stop=lambda: False)
    client.forward_response = types.messages.MessagesSlice(
        count=50, messages=[], topics=[], chats=[], users=[]
    )
    terminal = await adapter.fetch_page(7, after_message_id=14, should_stop=lambda: False)

    assert [row.message.message_id for row in first.messages] == [14]
    assert first.complete is False
    assert terminal.messages == ()
    assert terminal.complete is True
    assert len(client.requests) == 2
    assert [cast(GetHistoryRequest, request).offset_id for request in client.requests] == [14, 15]


@pytest.mark.asyncio
async def test_forward_gap_exactly_full_page_requires_continuation() -> None:
    client = _ForwardClient()
    client.messages = [build_mock_message(id=message_id) for message_id in range(14, 114)]

    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7,
        after_message_id=13,
        should_stop=lambda: False,
    )

    assert len(page.messages) == 100
    assert page.complete is False


@pytest.mark.asyncio
async def test_forward_gap_marks_interruption_before_normalization() -> None:
    client = _ForwardClient()
    page = await TelethonForwardGapPageAdapter(client).fetch_page(
        7,
        after_message_id=13,
        should_stop=lambda: True,
    )

    assert page.messages == ()
    assert page.complete is False


@pytest.mark.asyncio
async def test_forward_gap_keeps_received_rows_when_interrupted() -> None:
    client = _ForwardClient()
    client.messages = [build_mock_message(id=15), build_mock_message(id=14)]
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

    assert [row.message.message_id for row in page.messages] == [14]
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
    client = _ForwardClient() if adapter_kind == "forward" else _Client()
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
    client = _ForwardClient()
    client.error = error

    with pytest.raises(type(error)) as caught:
        await TelethonForwardGapPageAdapter(client).fetch_page(7, after_message_id=0, should_stop=lambda: False)

    assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_kind", ["full", "forward", "probe"])
async def test_ordinary_rpc_failure_is_translated_at_telegram_boundary(adapter_kind: str) -> None:
    client = _ForwardClient() if adapter_kind == "forward" else _Client()
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
        FullHistoryPage(messages=(message,) * 101, total_messages=None, next_before_message_id=1)
    with pytest.raises(ValueError, match="require a next cursor"):
        FullHistoryPage(messages=(message,), total_messages=1, next_before_message_id=None)
