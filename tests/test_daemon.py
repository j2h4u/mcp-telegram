from __future__ import annotations

import asyncio
import inspect
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from mcp_telegram.daemon import (
    _ensure_demand_runtime,
    _offer_startup_demands,
    _prime_runtime,
    _run_daemon_lifetime,
    _shutdown_sync_main_context,
    _SyncMainContext,
    _wait_for_startup_identity,
)
from mcp_telegram.own_only import OwnOnlyContext
from mcp_telegram.self_profile_maintenance import (
    StartupIdentityResult,
    StartupIdentityState,
    StartupIdentityUnavailableError,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind


def _ctx(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "background_tasks": set(),
        "shutdown_event": asyncio.Event(),
        "coordinator": None,
        "demand_runtime": None,
        "handler_manager": _HandlerStub(),
        "api_server": _ApiStub(),
        "conn": MagicMock(),
        "client": _ClientStub(),
        "folder_projection_worker": SimpleNamespace(),
        "fact_hydration_worker": SimpleNamespace(),
        "socket_path": Path("/tmp/mcp-telegram-test.sock"),
        "unix_server": None,
        "feedback_conn": MagicMock(),
        "rpc_admission_observer": None,
        "rpc_observation_sink": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _typed_ctx(**overrides: object) -> _SyncMainContext:
    return cast(_SyncMainContext, _ctx(**overrides))


class _CoordinatorStub:
    def __init__(self) -> None:
        self.offered: list[DemandKind] = []
        self.shutdown_calls = 0

    def offer(self, kind: DemandKind) -> bool:
        self.offered.append(kind)
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _HandlerStub:
    def __init__(self) -> None:
        self.self_ids: list[int] = []
        self.refresh_calls = 0

    def set_self_id(self, value: int) -> None:
        self.self_ids.append(value)

    def refresh_synced_dialogs(self) -> None:
        self.refresh_calls += 1

    def unregister(self) -> None:
        return


class _ApiStub:
    def __init__(self) -> None:
        self.self_id = 1
        self.self_profile: dict[str, object] | None = None
        self.startup_detail = ""
        self._ready = False

    async def shutdown(self) -> None:
        return


class _ClientStub:
    def __init__(self) -> None:
        self.disconnect_calls = 0
        self.close_scheduler_calls = 0
        self.observer_detached = False

    def is_connected(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def close_rpc_scheduler(self) -> None:
        self.close_scheduler_calls += 1

    def set_rpc_admission_observer(self, observer: object | None) -> None:
        self.observer_detached = observer is None


class _ConnectionStub:
    def __init__(self) -> None:
        self.close_calls = 0

    def execute(self, _sql: str, _parameters: tuple[object, ...] = ()) -> object:
        raise sqlite3.DatabaseError("test connection")

    def close(self) -> None:
        self.close_calls += 1


def test_sync_main_has_one_coordinator_task_and_no_retired_launcher() -> None:
    from mcp_telegram import daemon

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
    coordinator = _CoordinatorStub()
    ctx = _typed_ctx(coordinator=coordinator)

    _offer_startup_demands(ctx)

    assert coordinator.offered == [
        DemandKind.FULL_SYNC_DM_ENROLLMENT,
        DemandKind.DIALOG_BOOTSTRAP,
        DemandKind.FULL_SYNC_PAGE,
        DemandKind.READ_RECEIPT_BATCH,
    ]


@pytest.mark.asyncio
async def test_prime_runtime_waits_for_coordinator_profile_and_offers_folder() -> None:
    coordinator = _CoordinatorStub()
    handler = _HandlerStub()
    api = _ApiStub()
    api.self_id = 42
    api.self_profile = {"id": 42}
    conn = sqlite3.connect(":memory:")
    own_only_context = OwnOnlyContext(account_id=42)
    startup_identity = StartupIdentityState(100.0, 200.0)
    startup_identity.advance_profile(SimpleNamespace(id=42))
    startup_identity.advance_input_user(object())
    startup_identity.complete(StartupIdentityResult(SimpleNamespace(id=42), own_only_context))
    ctx = _typed_ctx(
        coordinator=coordinator,
        demand_runtime=SimpleNamespace(startup_identity=startup_identity),
        handler_manager=handler,
        api_server=api,
        conn=conn,
        client=_ClientStub(),
        socket_path=Path("/tmp/mcp-telegram-test.sock"),
        own_only_context=own_only_context,
        self_profile_cadence=None,
    )

    try:
        await _prime_runtime(ctx)
    finally:
        conn.close()

    assert handler.self_ids == [42]
    assert ctx.own_only_context == OwnOnlyContext(account_id=42)
    assert coordinator.offered == [DemandKind.SELF_PROFILE_MAINTENANCE, DemandKind.FOLDER_SNAPSHOT]
    assert api._ready is True


@pytest.mark.asyncio
async def test_startup_identity_wait_has_terminal_deadline() -> None:
    startup_identity = StartupIdentityState(0.0, 1.0)

    with pytest.raises(StartupIdentityUnavailableError, match="deadline expired"):
        await _wait_for_startup_identity(startup_identity, asyncio.Event())

    assert startup_identity.failure_reason == "startup identity deadline expired"


@pytest.mark.asyncio
async def test_daemon_lifetime_refreshes_local_dialog_set_and_stops() -> None:
    event = asyncio.Event()
    handler = _HandlerStub()
    ctx = _typed_ctx(shutdown_event=event, handler_manager=handler, conn=_ConnectionStub(), client=_ClientStub())

    def refresh_and_stop() -> None:
        handler.refresh_calls += 1
        event.set()

    handler.refresh_synced_dialogs = refresh_and_stop
    await _run_daemon_lifetime(ctx)

    assert handler.refresh_calls == 1


@pytest.mark.asyncio
async def test_shutdown_requests_coordinator_stop_before_connections_close() -> None:
    event = asyncio.Event()
    coordinator = _CoordinatorStub()
    client = _ClientStub()
    ctx = _typed_ctx(shutdown_event=event, coordinator=coordinator, client=client)
    ctx.conn = cast(sqlite3.Connection, _ConnectionStub())

    await _shutdown_sync_main_context(ctx)

    assert coordinator.shutdown_calls == 1
    assert client.disconnect_calls == 1
    assert client.close_scheduler_calls == 1
    assert client.observer_detached
    assert cast(_ConnectionStub, ctx.conn).close_calls == 1
