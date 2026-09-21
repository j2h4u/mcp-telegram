"""Telethon adapters for persistent backward and bounded forward history."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from typing import Protocol, cast

from telethon.errors import RPCError  # type: ignore[import-untyped]

from ..messages.telegram_adapter import PeerNameClient, extract_message_row, resolve_forward_entity_name_map
from ..telegram_access import ACCESS_LOST_ERRORS
from .contracts import (
    MESSAGE_HISTORY_PAGE_LIMIT,
    ForwardGapPage,
    FullHistoryPage,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from .ports import ForwardGapPagePort, FullHistoryPagePort


class _TelegramHistoryClient(Protocol):
    async def get_messages(self, **kwargs: object) -> object: ...

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
]
