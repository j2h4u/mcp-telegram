"""Focused contracts for streamed Telegram history selection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.functions.messages import SearchRequest
from telethon.tl.types import InputPeerChannel, InputPeerUser

from mcp_telegram.pagination import HistoryDirection
from mcp_telegram.reading.history_scan import HistoryScanRequest, scan_history
from mcp_telegram.telegram_reading import GatewayFailure, GatewayFailureKind, HistoryMessage


def _message(message_id: int, timestamp: int | None = None) -> HistoryMessage:
    return HistoryMessage(
        message={"message_id": message_id, "sent_at": timestamp},
        date=datetime.fromtimestamp(timestamp, tz=UTC) if timestamp is not None else None,
    )


def _request(
    *,
    direction: str = "newest",
    limit: int = 2,
    anchor: int | None = None,
    since: int | None = None,
    until: int | None = None,
) -> HistoryScanRequest:
    return HistoryScanRequest(
        dialog_id=42,
        limit=limit,
        direction=direction,
        direction_enum=HistoryDirection(direction),
        anchor_msg_id=anchor,
        sender_id=7,
        topic_id=8,
        unread_after_id=9,
        since_utc=since,
        until_utc=until,
    )


class _Gateway:
    def __init__(self, items: Sequence[HistoryMessage | GatewayFailure]) -> None:
        self.items = items
        self.calls: list[tuple[int, dict[str, object], int | None]] = []
        self.consumed = 0
        self.closed = False

    def stream_history(
        self, dialog_id: int, kwargs: Mapping[str, object], self_id: int | None
    ) -> AsyncGenerator[HistoryMessage | GatewayFailure]:
        self.calls.append((dialog_id, dict(kwargs), self_id))

        async def stream() -> AsyncGenerator[HistoryMessage | GatewayFailure]:
            try:
                for item in self.items:
                    self.consumed += 1
                    yield item
            finally:
                self.closed = True

        return stream()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        ("newest", [_message(5, 200), _message(4, 100)], 100, 200, [4], "exhausted"),
        ("oldest", [_message(1, 100), _message(2, 200)], 100, 200, [1], "window_edge"),
    ],
)
async def test_scan_uses_inclusive_since_and_exclusive_until(
    case: tuple[str, list[HistoryMessage], int, int, list[int], str],
) -> None:
    direction, items, since, until, expected, stop = case
    gateway = _Gateway(items)

    result = await scan_history(gateway, _request(direction=direction, since=since, until=until), self_id=1)

    assert [message["message_id"] for message in result.messages] == expected
    assert result.stop == stop
    assert gateway.consumed == 2
    assert gateway.closed


@pytest.mark.asyncio
async def test_scan_preserves_resume_at_consumed_window_edge_and_closes_stream() -> None:
    gateway = _Gateway([_message(5, 90), _message(4, 150)])

    result = await scan_history(gateway, _request(since=100), self_id=None)

    assert result.stop == "window_edge"
    assert result.resume_after == _message(5, 90)
    assert gateway.consumed == 1
    assert gateway.closed


@pytest.mark.asyncio
async def test_scan_uses_direction_specific_seek_options_and_one_stream() -> None:
    oldest = _Gateway([])
    await scan_history(oldest, _request(direction="oldest", anchor=10, until=200), self_id=1)
    assert len(oldest.calls) == 1
    assert oldest.calls[0][1] == {
        "limit": 32,
        "wait_time": 0,
        "offset_id": 10,
        "from_user": 7,
        "reply_to": 8,
        "min_id": 9,
        "reverse": True,
    }

    newest = _Gateway([])
    await scan_history(newest, _request(direction="newest", anchor=10, since=100, until=200), self_id=1)
    assert len(newest.calls) == 1
    assert newest.calls[0][1] == {
        "limit": 32,
        "wait_time": 0,
        "offset_id": 10,
        "from_user": 7,
        "reply_to": 8,
        "min_id": 9,
        "offset_date": datetime.fromtimestamp(200, tz=UTC),
    }


@pytest.mark.asyncio
async def test_telethon_reverse_search_omits_max_date_and_newest_keeps_upper_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TelegramClient(MemorySession(), 1, "offline-test")

    async def get_input_entity(entity: object) -> object:
        return entity

    async def get_peer_id(_peer: object) -> int:
        return 7

    monkeypatch.setattr(client, "get_input_entity", get_input_entity)
    monkeypatch.setattr(client, "get_peer_id", get_peer_id)
    peer = InputPeerChannel(42, 100)
    sender = InputPeerUser(7, 200)
    session = client.session
    assert session is not None
    try:
        oldest = client.iter_messages(peer, limit=1, reverse=True, offset_id=10, from_user=sender)
        await oldest._init(**oldest.kwargs)
        oldest_request = cast(SearchRequest, vars(oldest)["request"])
        assert isinstance(oldest_request, SearchRequest)
        assert oldest_request.offset_id == 11
        assert oldest_request.max_date is None

        upper = datetime.fromtimestamp(200, tz=UTC)
        newest = client.iter_messages(peer, limit=1, offset_date=upper, from_user=sender)
        await newest._init(**newest.kwargs)
        newest_request = cast(SearchRequest, vars(newest)["request"])
        assert isinstance(newest_request, SearchRequest)
        assert newest_request.max_date == upper

        captured = _Gateway([])
        await scan_history(
            captured,
            _request(limit=500, since=100, until=200),
            self_id=None,
        )
        kwargs = captured.calls[0][1]
        assert kwargs["limit"] == 8000
        assert kwargs["wait_time"] == 0
        long_iterator = client.iter_messages(
            peer,
            limit=cast(int, kwargs["limit"]),
            wait_time=cast(float, kwargs["wait_time"]),
            from_user=cast(int, kwargs["from_user"]),
            offset_date=cast(datetime, kwargs["offset_date"]),
        )
        await long_iterator._init(**long_iterator.kwargs)
        assert long_iterator.wait_time == 0
    finally:
        session.close()


@pytest.mark.asyncio
async def test_candidate_limit_retains_prefix_cursor_while_natural_exhaustion_does_not() -> None:
    candidates = [_message(message_id, 100) for message_id in range(40, 8, -1)]
    bounded = _Gateway(candidates)
    result = await scan_history(bounded, _request(since=0, until=10, limit=2), self_id=None)
    assert result.stop == "candidate_limit"
    assert result.messages == ()
    assert result.resume_after == candidates[31]
    assert bounded.consumed == 32

    exhausted = _Gateway(candidates[:2])
    result = await scan_history(exhausted, _request(limit=3), self_id=None)
    assert result.stop == "exhausted"
    assert result.resume_after is None


@pytest.mark.asyncio
async def test_scan_deduplicates_sparse_candidates_and_handles_unknown_dates() -> None:
    gateway = _Gateway([_message(3, 150), _message(3, 150), _message(4), _message(5, 200)])

    result = await scan_history(
        gateway,
        _request(direction="oldest", since=100, until=200, limit=3),
        self_id=None,
    )

    assert [message["message_id"] for message in result.messages] == [3]
    assert result.stop == "window_edge"
    assert result.resume_after == _message(5, 200)

    unbounded = _Gateway([_message(5)])
    result = await scan_history(unbounded, _request(limit=1), self_id=None)
    assert len(result.messages) == 1
    assert result.stop == "output_limit"


@pytest.mark.asyncio
async def test_nonmonotonic_date_edge_is_resumable_to_later_matching_candidate() -> None:
    first = _Gateway([_message(5, 90), _message(4, 150)])
    edge = await scan_history(first, _request(since=100), self_id=None)
    assert edge.stop == "window_edge"

    next_stream = _Gateway([_message(4, 150)])
    resumed = await scan_history(next_stream, _request(since=100, anchor=5), self_id=None)
    assert [message["message_id"] for message in resumed.messages] == [4]


@pytest.mark.asyncio
async def test_scan_fails_on_invalid_directional_cursor_without_exhausting() -> None:
    gateway = _Gateway([_message(4, 150), _message(5, 151), _message(3, 152)])

    result = await scan_history(gateway, _request(since=100), self_id=None)

    assert result.stop == "failure"
    assert result.failure is not None
    assert result.failure.error_type == "HistoryScanError"
    assert gateway.consumed == 2


@pytest.mark.asyncio
async def test_output_limit_resume_uses_only_consumed_prefix() -> None:
    gateway = _Gateway([_message(5, 150), _message(4, 151), _message(3, 152)])

    result = await scan_history(gateway, _request(limit=2), self_id=None)

    assert result.stop == "output_limit"
    assert result.resume_after == _message(4, 151)
    assert gateway.consumed == 2
    assert gateway.closed


@pytest.mark.asyncio
async def test_failure_after_prefix_retains_internal_messages_and_cancels_stream() -> None:
    failure = GatewayFailure(
        kind=GatewayFailureKind.TRANSIENT,
        error_type="OSError",
        error_message="temporary",
        retryable=True,
    )
    gateway = _Gateway([_message(5, 150), failure])

    result = await scan_history(gateway, _request(limit=2), self_id=None)

    assert result.stop == "failure"
    assert len(result.messages) == 1
    assert result.resume_after == _message(5, 150)
    assert result.failure == failure
    assert gateway.closed


@pytest.mark.asyncio
async def test_scan_cancellation_closes_owned_iterator() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingGateway:
        def stream_history(
            self, dialog_id: int, kwargs: Mapping[str, object], self_id: int | None
        ) -> AsyncGenerator[HistoryMessage]:
            _ = (dialog_id, kwargs, self_id)

            async def stream() -> AsyncGenerator[HistoryMessage]:
                try:
                    started.set()
                    await release.wait()
                    yield _message(1, 100)
                finally:
                    self_closed.set()

            return stream()

    self_closed = asyncio.Event()
    task = asyncio.create_task(scan_history(BlockingGateway(), _request(), self_id=None))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert self_closed.is_set()
