"""Daemon-owned Telethon adapter for precise outbox read dates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, cast

from telethon.tl.functions.messages import GetOutboxReadDateRequest
from telethon.tl.types import TypeInputPeer

from .telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from .telegram_reading import ReadDateFetchResult, TelegramReadReceiptGateway


class _TelegramClientLike(Protocol):
    async def __call__(self, request: object) -> object: ...


class TelethonTelegramReadReceiptGateway:
    """Fetch Telegram's exact read date without touching SQLite or MCP state."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramClientLike, client)

    async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
        try:
            response = await self._client(GetOutboxReadDateRequest(peer=cast(TypeInputPeer, entity), msg_id=message_id))
            value = getattr(response, "date", None)
            if not isinstance(value, datetime):
                # Telegram can return an empty/permission-limited response. It
                # is a successful probe with no event timestamp, not an error.
                return ReadDateFetchResult(status="missing")
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return ReadDateFetchResult(read_at=int(value.timestamp()), status="complete")
        except CATCHABLE_GATEWAY_FAILURES as exc:
            return ReadDateFetchResult(
                status="unavailable",
                failure=translate_gateway_failure(exc),
            )


__all__ = ["TelegramReadReceiptGateway", "TelethonTelegramReadReceiptGateway"]
