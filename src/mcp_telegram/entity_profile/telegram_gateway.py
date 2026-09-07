"""Bounded Telegram gateway for background entity-profile refreshes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


class BoundedTelegramGateway:
    """Apply one deadline to every Telegram operation used by entity details."""

    def __init__(self, client: object, *, timeout_seconds: float) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def __call__(self, request: object) -> object:
        return await asyncio.wait_for(self._client(request), timeout=self._timeout_seconds)  # type: ignore[operator]

    async def get_entity(self, entity_id: int) -> object:
        return await asyncio.wait_for(self._client.get_entity(entity_id), timeout=self._timeout_seconds)  # type: ignore[attr-defined]

    async def get_messages(self, entity: object, ids: list[int]) -> object:
        return await asyncio.wait_for(self._client.get_messages(entity, ids=ids), timeout=self._timeout_seconds)  # type: ignore[attr-defined]

    async def _iterate(self, iterator: AsyncIterator[object]) -> AsyncIterator[object]:
        while True:
            try:
                item = await asyncio.wait_for(iterator.__anext__(), timeout=self._timeout_seconds)
            except StopAsyncIteration:
                return
            yield item

    def iter_participants(self, peer: object, limit: int) -> AsyncIterator[object]:
        return self._iterate(self._client.iter_participants(peer, limit=limit))  # type: ignore[attr-defined]

    def iter_dialogs(self) -> AsyncIterator[object]:
        return self._iterate(self._client.iter_dialogs())  # type: ignore[attr-defined]
