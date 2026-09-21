"""Boundary tests for the Telegram message-history adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from telethon.errors import ChannelPrivateError, RPCError  # type: ignore[import-untyped]

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
        self.messages = [build_mock_message(id=2), build_mock_message(id=1)]
        self.total = 42
        self.error: BaseException | None = None

    async def get_messages(self, **kwargs: object) -> MockTotalList:
        self.get_messages_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return MockTotalList(self.messages, total=self.total)

    async def get_entity(self, _peer: object) -> object:
        return SimpleNamespace(id=999, first_name="Forwarded")

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
