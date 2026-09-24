"""Telethon adapters for persistent backward and bounded forward history."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from typing import Protocol, cast

from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetHistoryRequest  # type: ignore[import-untyped]
from telethon.tl.types import TypeInputPeer  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from ..messages.telegram_adapter import PeerNameClient, extract_message_row, resolve_forward_entity_name_map
from ..telegram_access import ACCESS_LOST_ERRORS
from .contracts import (
    MESSAGE_HISTORY_PAGE_SIZE,
    ExtractedMessage,
    ForwardGapPage,
    FullHistoryPage,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from .ports import ForwardGapPagePort, FullHistoryPagePort


class _TelegramHistoryClient(Protocol):
    async def get_messages(self, **kwargs: object) -> object: ...

    async def get_input_entity(self, peer: object) -> object: ...

    async def __call__(self, request: object, **kwargs: object) -> object: ...

    def iter_messages(self, **kwargs: object) -> AsyncIterator[object]: ...

    async def get_entity(self, peer: object) -> object: ...


class TelethonFullHistoryPageAdapter(FullHistoryPagePort):
    """Translate one exact Telegram backward page into canonical message rows."""

    def __init__(
        self,
        client: object,
        *,
        entity_lookup_context: Callable[[], AbstractContextManager[object]] | None = None,
    ) -> None:
        self._client = cast(_TelegramHistoryClient, client)
        self._entity_lookup_context = entity_lookup_context or _empty_context

    async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
        try:
            input_entity = await self._client.get_input_entity(dialog_id)
            response = await _fetch_raw_history_page(self._client, input_entity, before_message_id)
            raw_messages = tuple(cast(Sequence[object], getattr(response, "messages", ()) or ()))
            raw_count = len(raw_messages)
            next_cursor = _history_cursor(raw_messages, before_message_id)
            messages = await _normalize_full_history_messages(
                dialog_id, response, self._client, input_entity, self._entity_lookup_context
            )
        except ACCESS_LOST_ERRORS as exc:
            raise MessageHistoryAccessLostError(
                f"message history access lost for dialog {dialog_id}", reason_code=type(exc).__name__
            ) from exc
        except (RPCError, TimeoutError, OSError) as exc:
            raise MessageHistoryUnavailableError(f"message history unavailable for dialog {dialog_id}") from exc
        raw_total = cast(object, getattr(response, "count", getattr(response, "total", None)))
        total_messages = raw_count if raw_total is None else _optional_nonnegative_int(raw_total)
        return FullHistoryPage(messages=messages, total_messages=total_messages, next_before_message_id=next_cursor)


async def _fetch_raw_history_page(
    client: _TelegramHistoryClient,
    input_entity: object,
    before_message_id: int,
) -> object:
    return await client(
        GetHistoryRequest(
            peer=cast(TypeInputPeer, input_entity),
            offset_id=before_message_id,
            offset_date=None,
            add_offset=0,
            limit=MESSAGE_HISTORY_PAGE_SIZE,
            max_id=0,
            min_id=0,
            hash=0,
        )
    )


def _history_cursor(raw_messages: Sequence[object], before_message_id: int) -> int | None:
    raw_ids = [message_id for item in raw_messages if (message_id := _positive_id(item)) is not None]
    cursor = min(raw_ids) if raw_ids else None
    if raw_messages and cursor is None:
        raise MessageHistoryUnavailableError("Telegram history page has no usable message IDs")
    if cursor is not None and before_message_id > 0 and cursor >= before_message_id:
        raise MessageHistoryUnavailableError("Telegram history cursor did not move backwards")
    return cursor


async def _normalize_full_history_messages(
    dialog_id: int,
    response: object,
    client: _TelegramHistoryClient,
    input_entity: object,
    entity_lookup_context: Callable[[], AbstractContextManager[object]],
) -> tuple[ExtractedMessage, ...]:
    raw_messages = tuple(cast(Sequence[object], getattr(response, "messages", ()) or ()))
    entities = {
        get_peer_id(entity): entity
        for entity in (
            *cast(Sequence[object], getattr(response, "users", ()) or ()),
            *cast(Sequence[object], getattr(response, "chats", ()) or ()),
        )
    }
    messages = tuple(item for item in raw_messages if not isinstance(item, types.MessageEmpty))
    for message in messages:
        finish_init = getattr(message, "_finish_init", None)
        if callable(finish_init):
            finish_init(client, entities, input_entity)
    with entity_lookup_context():
        entity_name_map = await resolve_forward_entity_name_map(messages, cast(PeerNameClient, client))
    return tuple(extract_message_row(dialog_id, message, entity_name_map=entity_name_map) for message in messages)


def _positive_id(message: object) -> int | None:
    value = getattr(message, "id", None)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _empty_context() -> AbstractContextManager[object]:
    return nullcontext()


class TelethonForwardGapPageAdapter(ForwardGapPagePort):
    """Translate one bounded Telegram forward page into canonical message rows."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramHistoryClient, client)

    async def fetch_page(
        self,
        dialog_id: int,
        *,
        after_message_id: int,
        should_stop: Callable[[], bool],
    ) -> ForwardGapPage:
        messages: list[object] = []
        complete = True
        try:
            async for message in self._client.iter_messages(
                entity=dialog_id,
                min_id=after_message_id,
                reverse=True,
                limit=MESSAGE_HISTORY_PAGE_SIZE,
            ):
                if should_stop():
                    complete = False
                    break
                messages.append(message)
        except ACCESS_LOST_ERRORS as exc:
            raise MessageHistoryAccessLostError(
                f"message history access lost for dialog {dialog_id}", reason_code=type(exc).__name__
            ) from exc
        except (RPCError, TimeoutError, OSError) as exc:
            raise MessageHistoryUnavailableError(f"message history unavailable for dialog {dialog_id}") from exc
        if len(messages) == MESSAGE_HISTORY_PAGE_SIZE:
            complete = False
        normalized = tuple(extract_message_row(dialog_id, message) for message in messages)
        return ForwardGapPage(messages=normalized, complete=complete)


class TelethonHistoryAccessProbe:
    """Keep the single-message access/total probe outside history page ports."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramHistoryClient, client)

    async def probe_total_messages(self, dialog_id: int) -> int | None:
        try:
            response = await self._client.get_messages(entity=dialog_id, limit=1)
        except ACCESS_LOST_ERRORS as exc:
            raise MessageHistoryAccessLostError(
                f"message history access lost for dialog {dialog_id}", reason_code=type(exc).__name__
            ) from exc
        except (RPCError, TimeoutError, OSError) as exc:
            raise MessageHistoryUnavailableError(f"message history unavailable for dialog {dialog_id}") from exc
        return _optional_nonnegative_int(getattr(response, "total", None))


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


__all__ = [
    "TelethonForwardGapPageAdapter",
    "TelethonFullHistoryPageAdapter",
    "TelethonHistoryAccessProbe",
]
