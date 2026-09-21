"""Telethon adapters for persistent backward and bounded forward history."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from typing import Protocol, cast

from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetHistoryRequest  # type: ignore[import-untyped]
from telethon.tl.types import TypeInputPeer  # type: ignore[import-untyped]

from ..messages.telegram_adapter import (
    PeerNameClient,
    extract_message_row,
    extract_reply_and_topic,
    resolve_forward_entity_name_map,
)
from ..telegram_access import ACCESS_LOST_ERRORS
from .contracts import (
    MESSAGE_HISTORY_PAGE_LIMIT,
    ForwardGapPage,
    FullHistoryPage,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
    TopicAttributionMessage,
    TopicAttributionPage,
    TopicAttributionPageProjectionError,
)
from .ports import ForwardGapPagePort, FullHistoryPagePort, TopicAttributionPagePort


class _TelegramHistoryClient(Protocol):
    async def get_messages(self, **kwargs: object) -> object: ...

    def iter_messages(self, **kwargs: object) -> AsyncIterator[object]: ...

    async def get_entity(self, peer: object) -> object: ...

    async def __call__(self, request: object, **kwargs: object) -> object: ...


class _TopicAttributionMessageLike(Protocol):
    id: int | str


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
            response = await self._client.get_messages(
                entity=dialog_id,
                limit=MESSAGE_HISTORY_PAGE_LIMIT,
                offset_id=before_message_id,
            )
            raw_messages = tuple(cast(Sequence[object], response))
            with self._entity_lookup_context():
                entity_name_map = await resolve_forward_entity_name_map(
                    raw_messages,
                    cast(PeerNameClient, self._client),
                )
            messages = tuple(
                extract_message_row(dialog_id, message, entity_name_map=entity_name_map) for message in raw_messages
            )
        except ACCESS_LOST_ERRORS as exc:
            raise MessageHistoryAccessLostError(
                f"message history access lost for dialog {dialog_id}", reason_code=type(exc).__name__
            ) from exc
        except (RPCError, TimeoutError, OSError) as exc:
            raise MessageHistoryUnavailableError(f"message history unavailable for dialog {dialog_id}") from exc
        total_messages = _optional_nonnegative_int(getattr(response, "total", None))
        return FullHistoryPage(messages=messages, total_messages=total_messages)


class TelethonTopicAttributionPageAdapter(TopicAttributionPagePort):
    """Fetch one campaign page without normalizing messages or resolving entities."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramHistoryClient, client)

    async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> TopicAttributionPage:
        try:
            peer = _session_input_peer(self._client, dialog_id)
            response = await self._client(
                GetHistoryRequest(
                    peer=peer,
                    limit=MESSAGE_HISTORY_PAGE_LIMIT,
                    offset_date=None,
                    offset_id=before_message_id,
                    add_offset=0,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            )
            raw_messages = tuple(cast(Sequence[object], getattr(response, "messages", ()) or ()))
        except ACCESS_LOST_ERRORS as exc:
            raise MessageHistoryAccessLostError(
                f"message history access lost for dialog {dialog_id}", reason_code=type(exc).__name__
            ) from exc
        except (RPCError, TimeoutError, OSError) as exc:
            raise MessageHistoryUnavailableError(f"message history unavailable for dialog {dialog_id}") from exc
        try:
            messages = tuple(
                TopicAttributionMessage(
                    message_id=int(cast(_TopicAttributionMessageLike, message).id),
                    forum_topic_id=extract_reply_and_topic(message)[1],
                )
                for message in raw_messages
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise TopicAttributionPageProjectionError("invalid topic-attribution page projection") from exc
        return TopicAttributionPage(
            messages=messages,
            next_cursor=min((message.message_id for message in messages), default=None),
            complete=len(raw_messages) < MESSAGE_HISTORY_PAGE_LIMIT,
        )


def _session_input_peer(client: object, dialog_id: int) -> TypeInputPeer:
    """Resolve an already-known peer from Telethon's local session only."""
    session = getattr(client, "session", None)
    getter = getattr(session, "get_input_entity", None)
    if not callable(getter):
        raise MessageHistoryUnavailableError(f"message history unavailable for dialog {dialog_id}")
    try:
        peer = getter(dialog_id)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise MessageHistoryUnavailableError(f"message history unavailable for dialog {dialog_id}") from exc
    if hasattr(peer, "__await__"):
        close = getattr(peer, "close", None)
        if callable(close):
            close()
        raise MessageHistoryUnavailableError(f"message history unavailable for dialog {dialog_id}")
    if not isinstance(peer, (types.InputPeerUser, types.InputPeerChat, types.InputPeerChannel, types.InputPeerSelf)):
        raise MessageHistoryUnavailableError(f"message history unavailable for dialog {dialog_id}")
    return cast(TypeInputPeer, peer)


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
                limit=MESSAGE_HISTORY_PAGE_LIMIT,
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
        if len(messages) == MESSAGE_HISTORY_PAGE_LIMIT:
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
    "TelethonTopicAttributionPageAdapter",
]
