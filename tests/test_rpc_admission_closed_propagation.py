from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest

from mcp_telegram import activity_cold_backfill, activity_peer_resolve, activity_sync
from mcp_telegram.activity_cold_backfill import ColdBackfillHistoryPacing, ColdBackfillPacing
from mcp_telegram.activity_peer_sweep import PeerSweepRequest, _resolve_peer_for_sweep
from mcp_telegram.scheduled_messages import _load_candidate_entity
from mcp_telegram.telegram_rpc_scheduler import (
    RPC_SOURCE_SERVICE_CLASS,
    RpcAdmissionClosedError,
    TelegramRpcScope,
    TelegramRpcSource,
)


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
    def __init__(self, error: RpcAdmissionClosedError) -> None:
        self.error = error

    async def __call__(self, _request: object, **_kwargs: object) -> object:
        raise self.error

    async def get_input_entity(self, _dialog_id: int) -> object:
        raise self.error

    async def get_entity(self, _dialog_id: int) -> object:
        raise self.error


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
async def test_cold_backfill_safe_wrapper_propagates_scheduler_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = _closed_error(TelegramRpcSource.ACTIVITY_COLD_BACKFILL)

    async def fail(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise closed

    monkeypatch.setattr(activity_cold_backfill, "run_cold_backfill_pass", fail)
    pacing = ColdBackfillPacing(
        idle_s=10,
        history=ColdBackfillHistoryPacing(batch_s=1, enroll_s=1, access_retry_s=1),
    )

    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
            await activity_cold_backfill._run_cold_backfill_pass_safe(
                _ClosedActivityClient(closed), conn, asyncio.Event(), pacing=pacing, timeout_s=1
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
        )
        with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
            await _resolve_peer_for_sweep(request)


@pytest.mark.asyncio
async def test_peer_resolver_propagates_scheduler_close() -> None:
    closed = _closed_error(TelegramRpcSource.ACTIVITY_COLD_BACKFILL)

    with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
        await activity_peer_resolve.resolve_input_peer(_ClosedActivityClient(closed), 42)


@pytest.mark.asyncio
async def test_scheduled_entity_resolution_propagates_scheduler_close() -> None:
    closed = _closed_error(TelegramRpcSource.SCHEDULED_MESSAGES)

    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
            await _load_candidate_entity(_ClosedScheduledClient(closed), conn, 42, "channel", None)
