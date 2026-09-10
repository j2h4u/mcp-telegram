from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon import (
    _ensure_demand_runtime,
    _offer_startup_demands,
    _prime_runtime,
    _run_daemon_lifetime,
    _shutdown_sync_main_context,
)
from mcp_telegram.own_only import OwnOnlyContext
from mcp_telegram.telegram_rpc_consumers import DemandKind


def _ctx(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "background_tasks": set(),
        "shutdown_event": asyncio.Event(),
        "coordinator": None,
        "demand_runtime": None,
        "handler_manager": MagicMock(),
        "api_server": MagicMock(self_id=1, _ready=False, startup_detail="", self_profile=None),
        "conn": MagicMock(),
        "client": MagicMock(),
        "folder_projection_worker": MagicMock(),
        "fact_hydration_worker": MagicMock(),
        "socket_path": Path("/tmp/mcp-telegram-test.sock"),
        "unix_server": None,
        "feedback_conn": MagicMock(),
        "rpc_admission_observer": None,
        "rpc_observation_sink": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_sync_main_has_one_coordinator_task_and_no_retired_launcher() -> None:
    daemon = __import__("mcp_telegram.daemon", fromlist=["sync_main"])
    source = inspect.getsource(daemon.sync_main)
    composition = inspect.getsource(_ensure_demand_runtime)
    assert composition.count('name="telegram_demand_coordinator"') == 1
    for retired in (
        "_run_sync_loop",
        "run_activity_sync_loop",
        "run_delta_catch_up_loop",
        "run_access_probe_loop",
        "run_hot_sweep_loop",
        "run_cold_backfill_loop",
        "folder_projection_worker",
        "initialize_read_positions",
    ):
        assert retired not in source


def test_startup_demands_are_offered_to_coordinator() -> None:
    coordinator = MagicMock()
    ctx = _ctx(coordinator=coordinator)

    _offer_startup_demands(ctx)

    assert [call.args[0] for call in coordinator.offer.call_args_list] == [
        DemandKind.FULL_SYNC_DM_ENROLLMENT,
        DemandKind.DIALOG_BOOTSTRAP,
        DemandKind.FULL_SYNC_PAGE,
        DemandKind.READ_RECEIPT_BATCH,
    ]


@pytest.mark.asyncio
async def test_prime_runtime_waits_for_coordinator_profile_and_offers_folder() -> None:
    coordinator = MagicMock()
    handler = MagicMock()
    api = SimpleNamespace(self_id=42, self_profile={"id": 42}, startup_detail="", _ready=False)
    ctx = _ctx(
        coordinator=coordinator,
        handler_manager=handler,
        api_server=api,
        conn=MagicMock(),
        client=MagicMock(),
        socket_path=Path("/tmp/mcp-telegram-test.sock"),
        own_only_context=None,
        self_profile_cadence=None,
    )

    with patch(
        "mcp_telegram.daemon._load_own_only_context",
        new=AsyncMock(return_value=OwnOnlyContext(account_id=42)),
    ):
        await _prime_runtime(ctx)

    handler.set_self_id.assert_called_once_with(42)
    assert ctx.own_only_context == OwnOnlyContext(account_id=42)
    assert coordinator.offer.call_args_list == [
        ((DemandKind.SELF_PROFILE_MAINTENANCE,),),
        ((DemandKind.FOLDER_SNAPSHOT,),),
    ]
    assert api._ready is True


@pytest.mark.asyncio
async def test_daemon_lifetime_refreshes_local_dialog_set_and_stops() -> None:
    event = asyncio.Event()
    handler = MagicMock()
    ctx = _ctx(shutdown_event=event, handler_manager=handler, conn=MagicMock(), client=MagicMock())

    async def stop_after_refresh(_awaitable: object, *, timeout: float) -> bool:
        del timeout
        close = getattr(_awaitable, "close", None)
        if callable(close):
            close()
        event.set()
        return True

    with patch("mcp_telegram.daemon.asyncio.wait_for", side_effect=stop_after_refresh):
        await _run_daemon_lifetime(ctx)

    handler.refresh_synced_dialogs.assert_called_once_with()


@pytest.mark.asyncio
async def test_shutdown_requests_coordinator_stop_before_connections_close() -> None:
    event = asyncio.Event()
    coordinator = MagicMock()
    client = MagicMock()
    client.disconnect = AsyncMock()
    client.close_rpc_scheduler = AsyncMock()
    ctx = _ctx(shutdown_event=event, coordinator=coordinator, client=client)
    ctx.api_server.shutdown = AsyncMock()

    await _shutdown_sync_main_context(ctx)

    coordinator.shutdown.assert_called_once_with()
    client.disconnect.assert_awaited_once_with()
    ctx.conn.close.assert_called_once_with()
