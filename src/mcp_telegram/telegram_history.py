"""Telethon adapter for uncached history reads."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Protocol, cast

from .daemon_message import _MessageLike as _DaemonMessageLike
from .daemon_message import message_to_dict
from .telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from .telegram_reading import HistoryFetchResult


class _TelegramClientLike(Protocol):
    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]: ...


class TelethonTelegramHistoryGateway:
    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramClientLike, client)

    async def fetch_history(
        self, dialog_id: int, kwargs: Mapping[str, object], self_id: int | None
    ) -> HistoryFetchResult:
        try:
            messages = [
                message_to_dict(cast(_DaemonMessageLike, message), dialog_id=dialog_id, self_id=self_id)
                async for message in self._client.iter_messages(dialog_id, **dict(kwargs))
            ]
            return HistoryFetchResult(messages=tuple(messages))
        except CATCHABLE_GATEWAY_FAILURES as exc:
            return HistoryFetchResult(failure=translate_gateway_failure(exc))
