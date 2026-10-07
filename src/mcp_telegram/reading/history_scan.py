"""Bounded, resumable selection over one streamed Telegram history read."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ..pagination import HistoryDirection
from ..telegram_reading import (
    GatewayFailure,
    GatewayFailureKind,
    HistoryMessage,
    TelegramHistoryGateway,
)

HistoryStop = Literal["exhausted", "output_limit", "candidate_limit", "window_edge", "failure"]


@dataclass(frozen=True, slots=True)
class HistoryScanRequest:
    dialog_id: int
    limit: int
    direction: str
    direction_enum: HistoryDirection
    anchor_msg_id: int | None
    sender_id: int | None
    topic_id: int | None
    unread_after_id: int | None
    unread: bool = False
    since_utc: int | None = None
    until_utc: int | None = None


@dataclass(frozen=True, slots=True)
class HistoryScanResult:
    messages: tuple[dict[str, object], ...]
    stop: HistoryStop
    resume_after: HistoryMessage | None
    failure: GatewayFailure | None = None


@dataclass(slots=True)
class _ScanState:
    messages: list[dict[str, object]]
    seen: set[int]
    cursor: HistoryMessage | None
    previous_id: int | None
    candidates: int = 0


def _history_kwargs(request: HistoryScanRequest, candidate_limit: int) -> dict[str, object]:
    kwargs: dict[str, object] = {"limit": candidate_limit, "wait_time": 0}
    kwargs.update(
        {
            key: value
            for key, value in (
                ("offset_id", request.anchor_msg_id),
                ("from_user", request.sender_id),
                ("reply_to", request.topic_id),
                ("min_id", request.unread_after_id),
            )
            if value is not None
        }
    )
    if request.direction == "oldest":
        kwargs["reverse"] = True
    elif request.until_utc is not None:
        kwargs["offset_date"] = datetime.fromtimestamp(request.until_utc, tz=UTC)
    return kwargs


def _failure(message: str) -> GatewayFailure:
    return GatewayFailure(
        kind=GatewayFailureKind.TRANSIENT,
        error_type="HistoryScanError",
        error_message=message,
        retryable=True,
    )


def _progress_id(
    item: HistoryMessage,
    request: HistoryScanRequest,
    previous_id: int | None,
    seen: set[int],
) -> tuple[int | None, GatewayFailure | None]:
    message_id = item.message.get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        return None, _failure("Telegram history returned an invalid message ID")
    if message_id in seen:
        return None, None
    progresses = previous_id is None or (
        message_id > previous_id if request.direction == "oldest" else message_id < previous_id
    )
    if not progresses:
        return None, _failure("Telegram history did not advance in the requested direction")
    return message_id, None


def _window_edge(item: HistoryMessage, request: HistoryScanRequest) -> bool:
    if item.date is None:
        return False
    timestamp = item.date.timestamp()
    return (request.direction == "newest" and request.since_utc is not None and timestamp < request.since_utc) or (
        request.direction == "oldest" and request.until_utc is not None and timestamp >= request.until_utc
    )


def _in_window(item: HistoryMessage, request: HistoryScanRequest) -> bool:
    if item.date is None:
        return request.since_utc is None and request.until_utc is None
    timestamp = item.date.timestamp()
    return (request.since_utc is None or timestamp >= request.since_utc) and (
        request.until_utc is None or timestamp < request.until_utc
    )


def _consume(
    item: HistoryMessage | GatewayFailure,
    request: HistoryScanRequest,
    state: _ScanState,
    candidate_limit: int,
) -> HistoryScanResult | None:
    state.candidates += 1
    if isinstance(item, GatewayFailure):
        return HistoryScanResult(tuple(state.messages), "failure", state.cursor, item)
    message_id, failure = _progress_id(item, request, state.previous_id, state.seen)
    if failure is not None:
        return HistoryScanResult(tuple(state.messages), "failure", state.cursor, failure)
    if message_id is not None:
        state.previous_id = message_id
        state.seen.add(message_id)
        state.cursor = item
        if _window_edge(item, request):
            return HistoryScanResult(tuple(state.messages), "window_edge", state.cursor)
        if _in_window(item, request):
            state.messages.append(item.message)
            if len(state.messages) >= request.limit:
                return HistoryScanResult(tuple(state.messages), "output_limit", state.cursor)
    if state.candidates >= candidate_limit:
        return HistoryScanResult(tuple(state.messages), "candidate_limit", state.cursor)
    return None


async def scan_history(
    gateway: TelegramHistoryGateway,
    request: HistoryScanRequest,
    *,
    self_id: int | None,
) -> HistoryScanResult:
    """Select a bounded prefix and retain a safe cursor for the next request."""
    if request.limit < 1 or request.direction not in {"newest", "oldest"}:
        raise ValueError("history scan requires a positive limit and a valid direction")
    bounded = request.since_utc is not None or request.until_utc is not None
    candidate_limit = request.limit * 16 if bounded else request.limit
    kwargs: Mapping[str, object] = _history_kwargs(request, candidate_limit)
    state = _ScanState([], set(), None, request.anchor_msg_id)
    stream = gateway.stream_history(request.dialog_id, kwargs, self_id)
    async with aclosing(stream):
        async for item in stream:
            result = _consume(item, request, state, candidate_limit)
            if result is not None:
                return result
    return HistoryScanResult(tuple(state.messages), "exhausted", None)
