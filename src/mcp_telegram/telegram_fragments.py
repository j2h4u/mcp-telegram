"""Bounded fragment fetching and persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Sequence
from typing import Protocol, cast

from .hydration_queue import HydrationPriority
from .messages.sqlite_bundle import insert_messages_with_fts
from .messages.telegram_adapter import extract_message_row
from .telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from .telegram_reading import FragmentFetchResult, TelegramFragmentGateway
from .telegram_rpc_scheduler import TelegramRpcSource, preserve_or_rpc_scope


class _TelegramClientLike(Protocol):
    async def get_input_entity(self, dialog_id: int) -> object: ...

    async def get_messages(self, entity: object, ids: list[int]) -> object: ...

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]: ...


class FragmentContextService:
    """Persist one bounded context window around an anchor after a successful fetch."""

    def __init__(self, conn: sqlite3.Connection, gateway: TelegramFragmentGateway) -> None:
        self._conn = conn
        self._gateway = gateway

    async def fetch(self, dialog_id: int, anchor_message_id: int, context_size: int) -> FragmentFetchResult:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'fragment')", (dialog_id,)
            )
        with preserve_or_rpc_scope(TelegramRpcSource.MESSAGE_READ_FALLBACK):
            result = await self._gateway.fetch_context(dialog_id, anchor_message_id, context_size)
        if not result.ok or not result.messages:
            return result
        with self._conn:
            insert_messages_with_fts(self._conn, result.messages, priority=HydrationPriority.BACKFILL)
        return result


class TelethonTelegramFragmentGateway:
    """Fragment adapter that inherits the interactive caller's RPC scope."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramClientLike, client)

    async def fetch_context(self, dialog_id: int, anchor_message_id: int, window_size: int) -> FragmentFetchResult:
        try:
            entity = await self._client.get_input_entity(dialog_id)
            before_count = window_size // 2
            after_count = window_size - before_count - 1
            first_message_id = max(1, anchor_message_id - before_count)
            last_message_id = anchor_message_id + after_count
            fetched = await self._client.get_messages(entity, ids=list(range(first_message_id, last_message_id + 1)))
            return FragmentFetchResult(
                messages=tuple(
                    extract_message_row(dialog_id, message)
                    for message in cast(Sequence[object | None], fetched)
                    if message is not None
                )
            )
        except CATCHABLE_GATEWAY_FAILURES as exc:
            return FragmentFetchResult(failure=translate_gateway_failure(exc))
