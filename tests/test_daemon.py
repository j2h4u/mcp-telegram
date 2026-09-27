from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
from collections.abc import Coroutine
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

import mcp_telegram.daemon as daemon
from mcp_telegram.config import AutomaticGroupHistoryConfig, RuntimeObservationConfig, load_config
from mcp_telegram.daemon import (
    _acquire_startup_identity_before_updates,
    _connect_telegram,
    _ensure_demand_runtime,
    _HistorySyncRuntime,
    _message_fact_refresh_policy_from_config,
    _monitor_flood_wait_kill_switch,
    _offer_startup_demands,
    _prime_runtime,
    _run_daemon_lifetime,
    _shutdown_sync_main_context,
    _SyncMainContext,
    _wait_for_startup_identity,
)
from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.dialog_directory import CanonicalDialogDirectory
from mcp_telegram.entity_profile.contracts import (
    ProjectionOutcome,
    ProjectionStatus,
    TargetKind,
    UserProfileObservation,
)
from mcp_telegram.feedback_db import SQLiteFeedbackStore, ensure_feedback_schema
from mcp_telegram.feedback_service import FeedbackApplicationService
from mcp_telegram.flood import FloodWaitKillSwitchStatus, TelegramRpcThrottled
from mcp_telegram.own_only_contracts import OwnOnlyContext
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from mcp_telegram.runtime_observations import RuntimeObservationSink
from mcp_telegram.startup_identity import (
    StartupIdentityResult,
    StartupIdentityState,
    StartupIdentityUnavailableError,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget
from mcp_telegram.telegram_demand_coordinator import CoordinatorState, TelegramDemandCoordinator
from mcp_telegram.telegram_rpc_consumers import DURABLE_DEMAND_ORDER, DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    RPC_SOURCE_SERVICE_CLASS,
    RpcAdmissionEvent,
    RpcAdmissionEventKind,
    TelegramRpcSource,
)
from tests.daemon_api_policy import make_daemon_api_policy
from tests.helpers import (
    LoudChannelProfilePort,
    LoudChatAvatarHistoryPort,
    LoudCommonChatsPort,
    LoudGroupProfilePort,
    LoudUserAvatarHistoryPort,
    LoudUserProfilePort,
)


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
        "user_profile_port": _UserProfilePort(),
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


def test_message_fact_policy_uses_resolved_scheduling_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[state]\ndir = "/state"\n', encoding="utf-8")
    config = load_config(config_path)
    monkeypatch.setenv("REACTION_DETAIL_MAX_PAGES_PER_CYCLE", "2")

    policy = _message_fact_refresh_policy_from_config(config)

    assert policy.reaction_detail_max_pages_per_cycle == 2


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
        self._client = object()

    async def shutdown(self) -> None:
        return

    def _publish_auth_scope(self, _scope: object) -> None:
        return


class _ClientStub:
    def __init__(self) -> None:
        self.disconnect_calls = 0
        self.close_scheduler_calls = 0
        self.observer_detached = False
        self.request_observer_detached = False
        self.flood_event_observer_detached = False
        self.stages: list[str] = []

    def is_connected(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.stages.append("disconnect")

    async def close_rpc_scheduler(self) -> None:
        self.close_scheduler_calls += 1

    def set_rpc_admission_observer(self, observer: object | None) -> None:
        self.observer_detached = observer is None

    def set_rpc_request_observer(self, observer: object | None) -> None:
        self.request_observer_detached = observer is None

    def set_flood_event_observer(self, observer: object | None) -> None:
        self.flood_event_observer_detached = observer is None

    async def get_me(self) -> object:
        return SimpleNamespace(id=1)


@pytest.mark.asyncio
async def test_admission_blocked_startup_stays_not_ready_until_shutdown() -> None:
    class _BlockedClient(_ClientStub):
        def __init__(self) -> None:
            super().__init__()
            self.connect_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            raise TelegramRpcThrottled(retry_after_seconds=None, latched=True, detail="open-for-test")

    shutdown = asyncio.Event()
    api_server = _ApiStub()
    client = _BlockedClient()
    ctx = _typed_ctx(client=client, api_server=api_server, shutdown_event=shutdown)
    pending = asyncio.create_task(_connect_telegram(ctx))
    await asyncio.sleep(0)
    assert not pending.done()
    assert client.connect_calls == 1
    assert api_server._ready is False
    assert "admission unavailable" in api_server.startup_detail
    shutdown.set()
    assert await pending is False


@pytest.mark.asyncio
async def test_admission_blocked_startup_cancellation_propagates() -> None:
    class _BlockedClient(_ClientStub):
        async def connect(self) -> None:
            raise TelegramRpcThrottled(retry_after_seconds=None, latched=True, detail="open-for-test")

    pending = asyncio.create_task(_connect_telegram(_typed_ctx(client=_BlockedClient())))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


class _UserProfilePort:
    def __init__(self) -> None:
        self.calls: list[tuple[int, TargetKind]] = []

    async def fetch_user_profile(self, user_id: int, target_kind: TargetKind) -> UserProfileObservation:
        self.calls.append((user_id, target_kind))
        full = ProjectionOutcome(ProjectionStatus.USABLE, {}, None, None)
        personal = ProjectionOutcome(ProjectionStatus.ABSENT, {"personal_channel_id": None}, None, None)
        return UserProfileObservation(user_id, target_kind, full, personal)

    async def fetch_personal_channel_post(self, _reference: object, _message_id: int) -> None:
        return None


class _ConnectionStub:
    def __init__(self) -> None:
        self.close_calls = 0

    def execute(self, _sql: str, _parameters: tuple[object, ...] = ()) -> object:
        raise sqlite3.DatabaseError("test connection")

    def close(self) -> None:
        self.close_calls += 1


class _StartupIdentityClient(_ClientStub):
    def __init__(self, account_id: int) -> None:
        super().__init__()
        self._account_id = account_id

    async def get_me(self) -> object:
        return SimpleNamespace(id=self._account_id)

    async def get_input_entity(self, _account_id: int) -> object:
        return object()

    async def __call__(self, _request: object) -> object:
        return SimpleNamespace(full_user=SimpleNamespace(personal_channel_id=None))


class _CadenceStub:
    def status(self, _now: float) -> None:
        return None

    def mark_refreshed(self, _completed_at: float) -> None:
        return


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


def test_ensure_demand_runtime_wires_all_demand_sinks_once(monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: PLR0914
    from mcp_telegram import daemon

    events: list[str] = []

    class _Bindable:
        def __init__(self, name: str) -> None:
            self.name = name

        def bind_demand_sink(self, sink: object) -> None:
            events.append(f"{self.name}:{sink!r}")

    class _Coordinator:
        def run(self) -> object:
            async def _run() -> None:
                return None

            return _run()

        def __repr__(self) -> str:
            return "coordinator"

    coordinator = _Coordinator()
    runtime = SimpleNamespace(coordinator=coordinator)
    entity_service = _Bindable("entity")
    api_server = SimpleNamespace(
        _get_entity_info_service=lambda: entity_service,
        bind_demand_sink=lambda sink: events.append(f"api:{sink!r}"),
    )
    handler = _Bindable("handler")
    owner = _Bindable("draft")
    fact_hydration = _Bindable("fact")
    folder_projection = _Bindable("folder")
    ctx = _typed_ctx(
        api_server=api_server,
        handler_manager=handler,
        draft_owner=owner,
        fact_hydration_worker=fact_hydration,
        folder_projection_worker=folder_projection,
    )
    build_calls: list[tuple[object, object, object, object]] = []

    def build_runtime(ctx_arg: object, history: object, directory: object, startup: object) -> object:
        build_calls.append((ctx_arg, history, directory, startup))
        return runtime

    created_tasks: list[dict[str, object]] = []

    def create_task(_ctx: object, coroutine: object, **kwargs: object) -> None:
        cast(Coroutine[object, object, object], coroutine).close()
        created_tasks.append(kwargs)

    monkeypatch.setattr(daemon, "_build_demand_runtime", build_runtime)
    monkeypatch.setattr(daemon, "_create_tracked_task", create_task)
    history = object()
    directory = object()
    startup = object()

    result = _ensure_demand_runtime(
        ctx,
        cast(_HistorySyncRuntime, history),
        cast(CanonicalDialogDirectory, directory),
        cast(StartupIdentityState, startup),
    )

    assert result is runtime
    assert ctx.demand_runtime is runtime
    assert ctx.coordinator is coordinator
    assert len(build_calls) == 1
    assert build_calls[0] == (ctx, history, directory, startup)
    assert events == [
        "api:coordinator",
        "handler:coordinator",
        "draft:coordinator",
        "entity:coordinator",
        "fact:coordinator",
        "folder:coordinator",
    ]
    assert created_tasks == [{"name": "telegram_demand_coordinator", "critical": True}]


def test_ensure_demand_runtime_reuses_existing_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = SimpleNamespace(coordinator=object())
    ctx = _typed_ctx(demand_runtime=existing)

    def fail_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("existing demand runtime must be reused")

    from mcp_telegram import daemon

    monkeypatch.setattr(daemon, "_build_demand_runtime", fail_build)

    assert (
        _ensure_demand_runtime(
            ctx,
            cast(_HistorySyncRuntime, object()),
            cast(CanonicalDialogDirectory, object()),
            cast(StartupIdentityState, object()),
        )
        is existing
    )


@pytest.mark.asyncio
async def test_sync_main_wires_draft_owner_recovery_and_always_cleans_up(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_telegram import daemon

    events: list[str] = []

    class _Barrier:
        def __init__(self, *, closed: bool) -> None:
            events.append(f"barrier:{closed}")

        def open(self) -> None:
            events.append("barrier:open")

    class _Owner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("owner:init")

        def register(self) -> None:
            events.append("owner:register")

        def bind_account(self, account_id: int) -> None:
            events.append(f"owner:bind:{account_id}")

        def request_recovery(self, reason: str) -> None:
            events.append(f"owner:recovery:{reason}")

    class _Handler:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("handler:init")

        def register(self) -> None:
            events.append("handler:register")

        def set_self_id(self, account_id: int) -> None:
            events.append(f"handler:self:{account_id}")

    class _Directory:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("directory:init")

    ctx = SimpleNamespace(
        rpc_observation_sink=None,
        client=object(),
        conn=object(),
        db_path=Path("/tmp/mcp-telegram-sync-main-test.db"),
        shutdown_event=asyncio.Event(),
        scheduling=SimpleNamespace(
            draft_recovery=object(),
            reconnect_catch_up_interval_seconds=5.0,
            automatic_group_history=AutomaticGroupHistoryConfig(),
        ),
        api_server=SimpleNamespace(self_id=314),
        draft_owner=None,
        handler_manager=None,
    )

    async def build_context() -> object:
        events.append("context")
        return ctx

    def create_task(_ctx: object, coroutine: object, **kwargs: object) -> None:
        cast(Coroutine[object, object, object], coroutine).close()
        events.append(f"task:{kwargs['name']}")

    async def connect(_ctx: object) -> bool:
        events.append("connect")
        return True

    async def acquire(_ctx: object, _directory: object) -> object:
        events.append("identity")
        return object()

    async def run_reconnect(*_args: object, **_kwargs: object) -> None:
        return None

    async def run_fts(_ctx: object) -> None:
        events.append("fts")

    async def prime(_ctx: object) -> None:
        events.append("prime")

    async def lifetime(_ctx: object) -> None:
        events.append("lifetime")

    async def shutdown(_ctx: object) -> None:
        events.append("shutdown")

    monkeypatch.setattr(daemon, "_build_sync_main_context", build_context)
    monkeypatch.setattr(daemon, "_create_tracked_task", create_task)
    monkeypatch.setattr(daemon, "_run_fts_backfill", run_fts)
    monkeypatch.setattr(daemon, "UpdateProcessingBarrier", _Barrier)
    monkeypatch.setattr(daemon, "DraftMessageOwner", _Owner)
    monkeypatch.setattr(daemon, "SQLiteDraftProjection", lambda *_args: object())
    monkeypatch.setattr(daemon, "EventHandlerManager", _Handler)
    monkeypatch.setattr(daemon, "_connect_telegram", connect)
    monkeypatch.setattr(daemon, "CanonicalDialogDirectory", _Directory)
    monkeypatch.setattr(daemon, "_acquire_startup_identity_before_updates", acquire)
    monkeypatch.setattr(daemon, "TelethonFullHistoryPageAdapter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon, "TelethonForwardGapPageAdapter", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon, "TelethonHistoryAccessProbe", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon, "DeltaSyncWorker", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon, "FullSyncWorker", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon, "_ensure_demand_runtime", lambda *_args: events.append("ensure"))
    monkeypatch.setattr(daemon, "run_reconnect_catch_up_loop", run_reconnect)
    monkeypatch.setattr(daemon, "_prime_runtime", prime)
    monkeypatch.setattr(daemon, "_offer_startup_demands", lambda _ctx: events.append("offers"))
    monkeypatch.setattr(daemon, "_run_daemon_lifetime", lifetime)
    monkeypatch.setattr(daemon, "_shutdown_sync_main_context", shutdown)

    await daemon.sync_main()

    assert events == [
        "context",
        "task:flood_wait_kill_switch_monitor",
        "fts",
        "barrier:True",
        "owner:init",
        "owner:register",
        "handler:init",
        "handler:register",
        "connect",
        "directory:init",
        "identity",
        "handler:self:314",
        "owner:bind:314",
        "ensure",
        "owner:recovery:startup",
        "barrier:open",
        "task:reconnect_catch_up_loop",
        "prime",
        "offers",
        "lifetime",
        "shutdown",
    ]


def test_startup_demands_are_offered_to_coordinator() -> None:
    coordinator = _CoordinatorStub()
    ctx = _typed_ctx(coordinator=coordinator)

    _offer_startup_demands(ctx)

    assert coordinator.offered == [
        DemandKind.FULL_SYNC_DM_ENROLLMENT,
        DemandKind.DIALOG_BOOTSTRAP,
        DemandKind.FULL_SYNC_PAGE,
        DemandKind.READ_RECEIPT_BATCH,
        DemandKind.DRAFT_SNAPSHOT,
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
    bound_account_ids: list[int] = []
    ctx = _typed_ctx(
        coordinator=coordinator,
        demand_runtime=SimpleNamespace(
            startup_identity=startup_identity,
            dialog_directory=SimpleNamespace(bind_account_id=bound_account_ids.append),
        ),
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
    assert bound_account_ids == [42]
    assert ctx.own_only_context == OwnOnlyContext(account_id=42)
    assert coordinator.offered == [DemandKind.SELF_PROFILE_MAINTENANCE, DemandKind.FOLDER_SNAPSHOT]
    assert api._ready is True


@pytest.mark.asyncio
async def test_classified_startup_identity_mismatch_preserves_directory_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=100")
        conn.execute(
            "INSERT INTO dialogs(dialog_id,name,type,unread_count,snapshot_at,hidden) VALUES (7,'kept','user',4,1,0)"
        )
        conn.commit()
    finally:
        conn.close()
    shutdown = asyncio.Event()
    ctx = _typed_ctx(
        db_path=db_path,
        shutdown_event=shutdown,
        client=_StartupIdentityClient(101),
        api_server=_ApiStub(),
        self_profile_cadence=_CadenceStub(),
    )
    directory = CanonicalDialogDirectory(ctx.client, db_path, shutdown)

    with pytest.raises(StartupIdentityUnavailableError, match="startup identity"):
        await _acquire_startup_identity_before_updates(ctx, directory)

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT account_id FROM dialog_directory_state").fetchone() == (100,)
        assert conn.execute("SELECT unread_count,revision FROM dialogs WHERE dialog_id=7").fetchone() == (4, 0)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_classified_startup_identity_binds_matching_account(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    shutdown = asyncio.Event()
    ctx = _typed_ctx(
        db_path=db_path,
        shutdown_event=shutdown,
        client=_StartupIdentityClient(101),
        api_server=_ApiStub(),
        self_profile_cadence=_CadenceStub(),
    )
    directory = CanonicalDialogDirectory(ctx.client, db_path, shutdown)

    startup = await _acquire_startup_identity_before_updates(ctx, directory)

    assert not startup.pending
    assert cast(_UserProfilePort, ctx.user_profile_port).calls == [(101, TargetKind.USER)]
    assert ctx.api_server.self_id == 101
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT account_id FROM dialog_directory_state").fetchone() == (101,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_startup_identity_wait_has_terminal_deadline() -> None:
    startup_identity = StartupIdentityState(0.0, 1.0)

    with pytest.raises(StartupIdentityUnavailableError, match="deadline expired"):
        await _wait_for_startup_identity(startup_identity, asyncio.Event())


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
async def test_protective_stop_drains_producers_before_flushing_admission_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stages: list[str] = []
    event = asyncio.Event()
    event.set()

    class _StoppingClient(_ClientStub):
        async def disconnect(self) -> None:
            stages.append("disconnect")

    async def producer() -> None:
        try:
            await asyncio.Future()
        finally:
            stages.append("producer_cancelled")

    task = asyncio.create_task(producer())
    await asyncio.sleep(0)
    ctx = _typed_ctx(
        client=_StoppingClient(),
        background_tasks={task},
        flood_wait_kill_switch_event=event,
        rpc_admission_observer=cast(
            RpcAdmissionObservationAggregator,
            SimpleNamespace(flush=lambda: stages.append("flush")),
        ),
    )
    monkeypatch.setattr(
        daemon, "flood_wait_kill_switch_status", lambda: SimpleNamespace(open=True, detail=lambda: "open")
    )

    await _monitor_flood_wait_kill_switch(ctx)

    assert stages == ["producer_cancelled", "disconnect", "flush"]


@pytest.mark.asyncio
async def test_latched_coordinator_stop_keeps_local_daemon_socket_available(  # noqa: PLR0914, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A latched durable acquisition stops its pump, then the monitor tears down Telegram only."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    feedback_conn = ensure_feedback_schema(tmp_path / "feedback.db")
    shutdown = asyncio.Event()
    kill_switch = asyncio.Event()
    client = _ClientStub()
    api = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, client),
        shutdown,
        FeedbackApplicationService(SQLiteFeedbackStore(feedback_conn)),
        channel_profile_port=LoudChannelProfilePort(),
        group_profile_port=LoudGroupProfilePort(),
        user_profile_port=LoudUserProfilePort(),
        common_chats_port=LoudCommonChatsPort(),
        user_avatar_history_port=LoudUserAvatarHistoryPort(),
        chat_avatar_history_port=LoudChatAvatarHistoryPort(),
        policy=make_daemon_api_policy(),
    )
    api._ready = True
    active_status = FloodWaitKillSwitchStatus(
        open=True, reason="test", opened_at=1, events_in_window=1, wait_s_in_window=1, window_seconds=1, source="test"
    )
    api._health_status = lambda: active_status
    monkeypatch.setattr(daemon, "flood_wait_kill_switch_status", lambda: active_status)

    raw_sends: list[str] = []

    class _Adapter:
        def __init__(self, *, ready: bool = False) -> None:
            self.ready = ready

        def status(self, now: float) -> DemandStatus | None:
            del now
            return DemandStatus(0) if self.ready else None

        async def run_slice(self, budget: RpcAttemptBudget) -> None:
            del budget
            raw_sends.append("attempt")
            kill_switch.set()
            raise TelegramRpcThrottled(latched=True)

    target = DemandKind.ENTITY_PROFILE_REFRESH
    adapters = {kind: _Adapter(ready=kind is target) for kind in DURABLE_DEMAND_ORDER}
    coordinator = TelegramDemandCoordinator(adapters, shutdown)
    noncritical_started = asyncio.Event()

    async def noncritical_producer() -> None:
        noncritical_started.set()
        await asyncio.Future()

    ctx = _typed_ctx(
        conn=conn,
        client=client,
        api_server=api,
        shutdown_event=shutdown,
        flood_wait_kill_switch_event=kill_switch,
        background_tasks=set(),
        rpc_admission_observer=None,
    )
    socket_path = tmp_path / "daemon.sock"
    unix_server = await asyncio.start_unix_server(api.handle_client, path=socket_path)
    try:
        coordinator_task = daemon._create_tracked_task(
            ctx,
            coordinator.run(),
            name="telegram_demand_coordinator",
            critical=True,
        )
        noncritical_task = daemon._create_tracked_task(ctx, noncritical_producer(), name="noncritical_producer")
        await noncritical_started.wait()
        await coordinator_task

        assert coordinator.state is CoordinatorState.STOPPED
        assert shutdown.is_set() is False
        assert api._ready is True
        assert raw_sends == ["attempt"]

        await _monitor_flood_wait_kill_switch(ctx)
        assert client.disconnect_calls == 1
        assert noncritical_task.cancelled()
        assert shutdown.is_set() is False
        assert api._ready is True
        assert unix_server.is_serving()
        assert socket_path.exists()
        assert raw_sends == ["attempt"]

        async def request(payload: dict[str, object]) -> dict[str, object]:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
            response = cast(dict[str, object], json.loads((await reader.readline()).decode()))
            writer.close()
            await writer.wait_closed()
            return response

        protection = await request({"method": "get_account_protection"})
        feedback = await request({"method": "submit_feedback", "message": "local after latch"})
        assert protection["ok"] is True
        assert cast(dict[str, object], protection["data"])["account_protection"] is not None
        assert feedback["ok"] is True
        assert feedback_conn.execute("SELECT message FROM feedback").fetchone() == ("local after latch",)
    finally:
        unix_server.close()
        await unix_server.wait_closed()
        await api.shutdown()
        feedback_conn.close()
        conn.close()


@pytest.mark.asyncio
async def test_protective_monitor_cancels_active_coordinator_without_closing_local_socket(  # noqa: PLR0914, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    shutdown = asyncio.Event()
    kill_switch = asyncio.Event()
    client = _ClientStub()
    api = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, client),
        shutdown,
        channel_profile_port=LoudChannelProfilePort(),
        group_profile_port=LoudGroupProfilePort(),
        user_profile_port=LoudUserProfilePort(),
        common_chats_port=LoudCommonChatsPort(),
        user_avatar_history_port=LoudUserAvatarHistoryPort(),
        chat_avatar_history_port=LoudChatAvatarHistoryPort(),
        policy=make_daemon_api_policy(),
    )
    api._ready = True
    active_status = FloodWaitKillSwitchStatus(
        open=True, reason="test", opened_at=1, events_in_window=1, wait_s_in_window=1, window_seconds=1, source="test"
    )
    api._health_status = lambda: active_status
    monkeypatch.setattr(daemon, "flood_wait_kill_switch_status", lambda: active_status)
    started = asyncio.Event()
    raw_sends: list[str] = []

    class _BlockedAdapter:
        def __init__(self, *, ready: bool = False) -> None:
            self.ready = ready

        def status(self, now: float) -> DemandStatus | None:
            del now
            return DemandStatus(0) if self.ready else None

        async def run_slice(self, budget: RpcAttemptBudget) -> None:
            del budget
            started.set()
            await asyncio.Future()
            raw_sends.append("unexpected")

    target = DemandKind.ENTITY_PROFILE_REFRESH
    coordinator = TelegramDemandCoordinator(
        {kind: _BlockedAdapter(ready=kind is target) for kind in DURABLE_DEMAND_ORDER}, shutdown
    )
    noncritical_started = asyncio.Event()

    async def noncritical_producer() -> None:
        noncritical_started.set()
        await asyncio.Future()

    ctx = _typed_ctx(
        conn=conn,
        client=client,
        api_server=api,
        shutdown_event=shutdown,
        flood_wait_kill_switch_event=kill_switch,
        background_tasks=set(),
        rpc_admission_observer=None,
    )
    socket_path = tmp_path / "monitor-first.sock"
    unix_server = await asyncio.start_unix_server(api.handle_client, path=socket_path)
    try:
        coordinator_task = daemon._create_tracked_task(
            ctx,
            coordinator.run(),
            name="telegram_demand_coordinator",
            critical=True,
        )
        noncritical_task = daemon._create_tracked_task(ctx, noncritical_producer(), name="noncritical_producer")
        await asyncio.gather(started.wait(), noncritical_started.wait())
        kill_switch.set()
        await _monitor_flood_wait_kill_switch(ctx)
        with pytest.raises(asyncio.CancelledError):
            await coordinator_task

        assert coordinator.state is CoordinatorState.STOPPED
        assert client.disconnect_calls == 1
        assert noncritical_task.cancelled()
        assert shutdown.is_set() is False
        assert api._ready is True
        assert unix_server.is_serving()
        assert raw_sends == []

        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(b'{"method":"get_account_protection"}\n')
        await writer.drain()
        response = cast(dict[str, object], json.loads((await reader.readline()).decode()))
        writer.close()
        await writer.wait_closed()
        assert response["ok"] is True
    finally:
        unix_server.close()
        await unix_server.wait_closed()
        await api.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_protective_stop_persists_pending_summary_while_daemon_remains_parked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    policy = RuntimeObservationConfig()
    sink = RuntimeObservationSink(db_path, retention_ttl_seconds=3600, policy=policy)
    observer = RpcAdmissionObservationAggregator(sink, policy=policy)
    source = TelegramRpcSource.MCP_INTERACTIVE
    service_class = RPC_SOURCE_SERVICE_CLASS[source]
    observer.observe(
        RpcAdmissionEvent(
            kind=RpcAdmissionEventKind.QUEUED,
            source=source,
            service_class=service_class,
            queue_depth=1,
            total_depth=1,
        )
    )
    observer.observe(
        RpcAdmissionEvent(
            kind=RpcAdmissionEventKind.DISPATCHED,
            source=source,
            service_class=service_class,
            queue_depth=0,
            total_depth=0,
            active_depth=1,
            total_outstanding=1,
            wait_seconds=0.01,
        )
    )

    stages: list[str] = []
    shutdown = asyncio.Event()
    kill_switch = asyncio.Event()
    kill_switch.set()

    class _StoppingClient(_ClientStub):
        async def disconnect(self) -> None:
            stages.append("disconnect")
            await super().disconnect()

    async def producer() -> None:
        try:
            await asyncio.Future()
        finally:
            stages.append("producer_cancelled")

    task = asyncio.create_task(producer())
    await asyncio.sleep(0)
    ctx = _typed_ctx(
        client=_StoppingClient(),
        background_tasks={task},
        shutdown_event=shutdown,
        flood_wait_kill_switch_event=kill_switch,
        rpc_admission_observer=observer,
        rpc_observation_sink=sink,
    )
    monkeypatch.setattr(
        daemon,
        "flood_wait_kill_switch_status",
        lambda: SimpleNamespace(open=True, detail=lambda: "open-for-test"),
    )

    try:
        await _monitor_flood_wait_kill_switch(ctx)
        assert stages == ["producer_cancelled", "disconnect"]
        assert shutdown.is_set() is False
        assert cast(_ClientStub, ctx.client).disconnect_calls == 1

        row: tuple[object, ...] | None = None
        for _ in range(100):
            with closing(sqlite3.connect(db_path)) as conn:
                row = cast(
                    tuple[object, ...] | None,
                    conn.execute(
                        "SELECT outcome, reason_code FROM runtime_observations "
                        "WHERE kind = 'telegram.rpc_admission' ORDER BY observed_at_ms DESC LIMIT 1"
                    ).fetchone(),
                )
            if row is not None:
                break
            await asyncio.sleep(0.01)
        assert row is not None
        assert row[0] == "summary"
        assert stages == ["producer_cancelled", "disconnect"]
    finally:
        await sink.aclose()


@pytest.mark.asyncio
async def test_failed_protective_stop_flushes_without_disconnected_success_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    policy = RuntimeObservationConfig()
    sink = RuntimeObservationSink(db_path, retention_ttl_seconds=3600, policy=policy)
    observer = RpcAdmissionObservationAggregator(sink, policy=policy)
    source = TelegramRpcSource.MCP_INTERACTIVE
    service_class = RPC_SOURCE_SERVICE_CLASS[source]
    observer.observe(
        RpcAdmissionEvent(
            kind=RpcAdmissionEventKind.QUEUED,
            source=source,
            service_class=service_class,
            queue_depth=1,
            total_depth=1,
        )
    )
    observer.observe(
        RpcAdmissionEvent(
            kind=RpcAdmissionEventKind.DISPATCHED,
            source=source,
            service_class=service_class,
            queue_depth=0,
            total_depth=0,
            active_depth=1,
            total_outstanding=1,
            wait_seconds=0.01,
        )
    )

    class _FailingClient(_ClientStub):
        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            raise OSError("transport teardown failed")

    kill_switch = asyncio.Event()
    kill_switch.set()
    ctx = _typed_ctx(
        client=_FailingClient(),
        shutdown_event=asyncio.Event(),
        flood_wait_kill_switch_event=kill_switch,
        rpc_admission_observer=observer,
        rpc_observation_sink=sink,
    )
    monkeypatch.setattr(
        daemon,
        "flood_wait_kill_switch_status",
        lambda: SimpleNamespace(open=True, detail=lambda: "open-for-test"),
    )
    caplog.set_level(logging.ERROR, logger=daemon.__name__)

    try:
        await _monitor_flood_wait_kill_switch(ctx)
        assert cast(_ClientStub, ctx.client).disconnect_calls == 1
        assert not ctx.shutdown_event.is_set()
        assert "flood_wait_kill_switch_telegram_disconnect_failed" in caplog.messages
        assert "flood_wait_kill_switch_telegram_disconnected" not in caplog.messages

        row: tuple[object, ...] | None = None
        for _ in range(100):
            with closing(sqlite3.connect(db_path)) as conn:
                row = cast(
                    tuple[object, ...] | None,
                    conn.execute(
                        "SELECT outcome, reason_code FROM runtime_observations "
                        "WHERE kind = 'telegram.rpc_admission' ORDER BY observed_at_ms DESC LIMIT 1"
                    ).fetchone(),
                )
            if row is not None:
                break
            await asyncio.sleep(0.01)
        assert row is not None
        assert row[0] == "summary"
    finally:
        await sink.aclose()


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
    assert client.request_observer_detached
    assert client.flood_event_observer_detached
    assert cast(_ConnectionStub, ctx.conn).close_calls == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_disconnects_and_detaches_before_flushing_telemetry(tmp_path: Path) -> None:
    stages: list[str] = []

    class _OrderedClient(_ClientStub):
        async def disconnect(self) -> None:
            stages.append("disconnect")

        async def close_rpc_scheduler(self) -> None:
            stages.append("scheduler_close")

        def set_rpc_admission_observer(self, observer: object | None) -> None:
            stages.append("admission_detach")

        def set_rpc_request_observer(self, observer: object | None) -> None:
            stages.append("request_detach")

        def set_flood_event_observer(self, observer: object | None) -> None:
            stages.append("flood_detach")

    class _OrderedConnection(_ConnectionStub):
        def close(self) -> None:
            stages.append("connections_close")

    client = _OrderedClient()
    task_release = asyncio.Event()

    async def background_task() -> None:
        try:
            await task_release.wait()
        finally:
            stages.append("task_cancelled")

    task = asyncio.create_task(background_task())
    await asyncio.sleep(0)
    ctx = _typed_ctx(
        client=client,
        conn=_OrderedConnection(),
        background_tasks={task},
        socket_path=tmp_path / "daemon.sock",
    )
    ctx.rpc_admission_observer = cast(
        RpcAdmissionObservationAggregator,
        SimpleNamespace(flush=lambda: stages.append("flush")),
    )
    ctx.rpc_observation_sink = cast(
        RuntimeObservationSink,
        SimpleNamespace(aclose=lambda: _append_async(stages, "sink_close")),
    )

    await _shutdown_sync_main_context(ctx)

    assert stages.index("task_cancelled") < stages.index("disconnect")
    assert stages.index("disconnect") < stages.index("scheduler_close")
    assert stages.index("scheduler_close") < stages.index("admission_detach")
    assert stages.index("scheduler_close") < stages.index("request_detach")
    assert stages.index("scheduler_close") < stages.index("flood_detach")
    assert stages.index("flood_detach") < stages.index("flush")
    assert stages.index("flush") < stages.index("sink_close")
    assert stages.index("sink_close") < stages.index("connections_close")


async def _append_async(stages: list[str], stage: str) -> None:
    stages.append(stage)
