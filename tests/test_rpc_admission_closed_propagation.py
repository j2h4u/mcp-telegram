from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest

from mcp_telegram import activity_peer_resolve, activity_sync
from mcp_telegram.activity_peer_sweep import PeerSweepRequest, _resolve_peer_for_sweep
from mcp_telegram.scheduled_messages import ScheduledMessageReconciler, ScheduledReconciliationPolicy
from mcp_telegram.telegram_rpc_scheduler import (
    RPC_SOURCE_SERVICE_CLASS,
    RpcAdmissionClosedError,
    TelegramRpcScope,
    TelegramRpcSource,
)
from sqlite_writer_helpers import inline_writer


def _closed_error(source: TelegramRpcSource) -> RpcAdmissionClosedError:
    scope = TelegramRpcScope(source, RPC_SOURCE_SERVICE_CLASS[source], None, None)
    return RpcAdmissionClosedError(scope, "scheduler closed")


class _ClosedActivityClient:
    def __init__(self, error: RpcAdmissionClosedError) -> None:
        self.error = error

    async def __call__(self, request: object) -> object:
        del request
        raise self.error

    async def get_input_entity(self, dialog_id: int) -> object:
        del dialog_id
        raise self.error


class _ClosedScheduledClient:
    def __init__(self, error: RpcAdmissionClosedError, *, close_on_resolution: bool = True) -> None:
        self.error = error
        self.close_on_resolution = close_on_resolution

    async def __call__(self, _request: object, **_kwargs: object) -> object:
        raise self.error

    async def get_input_entity(self, _dialog_id: int) -> object:
        if self.close_on_resolution:
            raise self.error
        return _dialog_id


@pytest.mark.asyncio
async def test_activity_sync_search_propagates_scheduler_close(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = _closed_error(TelegramRpcSource.ACTIVITY_ARCHIVE)

    async def fail(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise closed

    monkeypatch.setattr(activity_sync, "call_with_timeout", fail)

    with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
        await activity_sync._search_backfill_batch(
            _ClosedActivityClient(closed), 0, asyncio.Event(), total_fetched=0, timeout_s=1
        )


@pytest.mark.asyncio
async def test_peer_sweep_resolution_propagates_scheduler_close() -> None:
    closed = _closed_error(TelegramRpcSource.ACTIVITY_COLD_BACKFILL)

    with closing(sqlite3.connect(":memory:")) as conn:
        request = PeerSweepRequest(
            client=_ClosedActivityClient(closed),
            conn=conn,
            dialog_id=42,
            offset_id=0,
            min_id=0,
            limit=10,
            timeout_s=1,
            writer=inline_writer(conn),
        )
        with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
            await _resolve_peer_for_sweep(request)


@pytest.mark.asyncio
async def test_peer_resolver_propagates_scheduler_close() -> None:
    closed = _closed_error(TelegramRpcSource.ACTIVITY_COLD_BACKFILL)

    with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
        await activity_peer_resolve.resolve_input_peer(_ClosedActivityClient(closed), 42)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_on_resolution", [True, False])
async def test_scheduled_snapshot_propagates_scheduler_close(close_on_resolution: bool) -> None:
    closed = _closed_error(TelegramRpcSource.SCHEDULED_MESSAGES)

    with closing(sqlite3.connect(":memory:")) as conn:
        worker = ScheduledMessageReconciler(
            _ClosedScheduledClient(closed, close_on_resolution=close_on_resolution),
            conn,
            asyncio.Event(),
            policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=1),
            writer=inline_writer(conn),
        )
        with pytest.raises(RpcAdmissionClosedError, match="scheduler closed") as raised:
            await worker._fetch_snapshot_or_stop(42, discovery=True, now=100)
        assert raised.value is closed


@pytest.mark.asyncio
async def test_scheduled_candidate_eligibility_needs_no_rpc_when_scheduler_is_closed() -> None:
    closed = _closed_error(TelegramRpcSource.SCHEDULED_MESSAGES)

    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(
            "CREATE TABLE dialogs(dialog_id INTEGER);"
            "CREATE TABLE scheduled_messages(dialog_id INTEGER, message_state TEXT);"
            "INSERT INTO dialogs VALUES (-10042);"
            "INSERT INTO scheduled_messages VALUES (42, 'unknown_missing');"
        )
        worker = ScheduledMessageReconciler(
            _ClosedScheduledClient(closed),
            conn,
            asyncio.Event(),
            policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=1),
            writer=inline_writer(conn),
        )
        assert await worker._discover_eligibility(-10042) is True
        assert await worker._discover_eligibility(42) is True
        assert await worker._discover_eligibility(99) is False
