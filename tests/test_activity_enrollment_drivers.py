import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, Mock

import pytest

from mcp_telegram.activity_cold_backfill import (
    ColdBackfillHistoryPacing,
    ColdBackfillPacing,
    ColdPeerPageDemandAdapter,
)
from mcp_telegram.activity_hot_sweep import HotActivityDemandAdapter
from mcp_telegram.activity_peer_sweep import WorkingSetEnrollmentSliceResult
from mcp_telegram.config import ActivityHotSweepConfig
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource
from sqlite_writer_helpers import inline_writer


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["hot", "cold"])
@pytest.mark.parametrize("consumed", [False, True])
async def test_driver_delegates_enrollment_gate_and_continues_only_when_unconsumed(
    monkeypatch: pytest.MonkeyPatch, driver: str, consumed: bool
) -> None:
    module = f"mcp_telegram.activity_{'hot_sweep' if driver == 'hot' else 'cold_backfill'}"
    enrollment = AsyncMock(return_value=WorkingSetEnrollmentSliceResult(consumed=consumed))
    monkeypatch.setattr(f"{module}.run_working_set_enrollment_slice", enrollment)
    release_lookup = Mock(side_effect=AssertionError("slice driver must delegate enrollment due checks"))
    monkeypatch.setattr(f"{module}.working_set_enrollment_release_at", release_lookup)
    with closing(sqlite3.connect(":memory:")) as conn:
        client = AsyncMock()
        shutdown = asyncio.Event()
        if driver == "hot":
            policy = ActivityHotSweepConfig(jitter_max_seconds=0)
            adapter = HotActivityDemandAdapter(client, conn, shutdown, policy, 120, writer=inline_writer(conn))
            peer_work = Mock(return_value=None)
            monkeypatch.setattr(f"{module}._select_due_hot_window", peer_work)
            source = TelegramRpcSource.ACTIVITY_HOT_SWEEP
            cadence = policy.loop_interval_seconds
        else:
            pacing = ColdBackfillPacing(ColdBackfillHistoryPacing(5, 1800, 3600))
            adapter = ColdPeerPageDemandAdapter(client, conn, shutdown, pacing, 120, writer=inline_writer(conn))
            peer_work = AsyncMock()
            monkeypatch.setattr(f"{module}.run_cold_backfill_pass", peer_work)
            source = TelegramRpcSource.ACTIVITY_COLD_BACKFILL
            cadence = pacing.history.enroll_s

        await adapter.run_slice(RpcAttemptBudget(1))

        enrollment.assert_awaited_once_with(
            client, conn, source=source, cadence_s=cadence, timeout_s=120, writer=adapter.writer
        )
        assert peer_work.call_count == int(not consumed)
        release_lookup.assert_not_called()
