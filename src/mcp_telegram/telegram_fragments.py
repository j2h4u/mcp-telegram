"""Bounded fragment fetching and persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Protocol, cast

from .hydration_queue import HydrationPriority
from .messages.sqlite_bundle import insert_messages_with_fts
from .messages.telegram_adapter import extract_message_row
from .observation_order import allocate_observation_order
from .sqlite_writer import SQLiteWriter
from .sync_transactions import write_transaction
from .telegram_demand import AcquisitionKind
from .telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from .telegram_reading import FragmentFetchResult, TelegramFragmentGateway
from .telegram_rpc_scheduler import TelegramRpcSource, rpc_scope


class _TelegramClientLike(Protocol):
    async def get_input_entity(self, dialog_id: int) -> object: ...

    async def get_messages(self, entity: object, ids: list[int]) -> object: ...

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]: ...


class FragmentContextService:
    """Persist one bounded context window around an anchor after a successful fetch."""

    def __init__(self, conn: sqlite3.Connection, gateway: TelegramFragmentGateway, *, writer: SQLiteWriter) -> None:
        self._conn = conn
        self._writer = writer
        self._gateway = gateway

    async def fetch(self, dialog_id: int, anchor_message_id: int, context_size: int) -> FragmentFetchResult:
        observation_order = await self._writer.run(lambda conn: self._prepare_fetch(conn, dialog_id))
        with rpc_scope(
            TelegramRpcSource.MESSAGE_READ_FALLBACK,
            acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE,
        ):
            result = await self._gateway.fetch_context(dialog_id, anchor_message_id, context_size)
        if not result.ok or not result.messages:
            return result
        result = replace(
            result,
            messages=tuple(replace(message, observation_order=observation_order) for message in result.messages),
        )
        await self._writer.run(lambda conn: self._persist_messages(conn, result))
        return result

    @staticmethod
    def _prepare_fetch(conn: sqlite3.Connection, dialog_id: int) -> int:
        with write_transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'fragment')", (dialog_id,)
            )
            return allocate_observation_order(conn)

    @staticmethod
    def _persist_messages(conn: sqlite3.Connection, result: FragmentFetchResult) -> None:
        with write_transaction(conn):
            insert_messages_with_fts(conn, result.messages, priority=HydrationPriority.BACKFILL)


class TelethonTelegramFragmentGateway:
    """Fragment adapter that inherits the interactive caller's RPC scope."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramClientLike, client)

    async def fetch_context(self, dialog_id: int, anchor_message_id: int, window_size: int) -> FragmentFetchResult:
        try:
            with rpc_scope(
                TelegramRpcSource.MESSAGE_READ_FALLBACK,
                acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
            ):
                entity = await self._client.get_input_entity(dialog_id)
            before_count = window_size // 2
            after_count = window_size - before_count - 1
            first_message_id = max(1, anchor_message_id - before_count)
            last_message_id = anchor_message_id + after_count
            with rpc_scope(
                TelegramRpcSource.MESSAGE_READ_FALLBACK,
                acquisition_kind=AcquisitionKind.MESSAGE_LOOKUP,
            ):
                fetched = await self._client.get_messages(
                    entity, ids=list(range(first_message_id, last_message_id + 1))
                )
            return FragmentFetchResult(
                messages=tuple(
                    extract_message_row(dialog_id, message)
                    for message in cast(Sequence[object | None], fetched)
                    if message is not None
                )
            )
        except CATCHABLE_GATEWAY_FAILURES as exc:
            return FragmentFetchResult(failure=translate_gateway_failure(exc))
