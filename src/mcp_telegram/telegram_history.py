"""Telethon adapter for uncached history reads."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime
from typing import Protocol, cast

from .telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from .telegram_message_projection import MessageLike, message_to_dict
from .telegram_reading import GatewayFailure, HistoryMessage


class _TelegramClientLike(Protocol):
    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]: ...


class TelethonTelegramHistoryGateway:
    """Shared history adapter; the interactive reading caller owns its RPC scope."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramClientLike, client)

    async def stream_history(
        self, dialog_id: int, kwargs: Mapping[str, object], self_id: int | None
    ) -> AsyncGenerator[HistoryMessage | GatewayFailure]:
        try:
            iterator = self._client.iter_messages(dialog_id, **dict(kwargs))
        except CATCHABLE_GATEWAY_FAILURES as exc:
            yield translate_gateway_failure(exc)
            return

        try:
            while True:
                try:
                    message = await anext(iterator)
                except StopAsyncIteration:
                    return
                except CATCHABLE_GATEWAY_FAILURES as exc:
                    yield translate_gateway_failure(exc)
                    return

                raw_date = getattr(message, "date", None)
                projected = message_to_dict(cast(MessageLike, message), dialog_id=dialog_id, self_id=self_id)
                yield HistoryMessage(
                    message=projected,
                    date=raw_date if isinstance(raw_date, datetime) else None,
                )
        finally:
            close = getattr(iterator, "aclose", None)
            if callable(close):
                await cast(Callable[[], Awaitable[object]], close)()
