from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon import types

import tests.test_entity_profile_full_user_pair as pair_fixtures
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon import _start_self_profile_after_replay, _SyncMainContext
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import TargetKind, UserProfileObservation
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.entity_profile.repository import EntityProfileRepository, admit_self_profile
from mcp_telegram.event_handlers import EventHandlerManager
from mcp_telegram.models import DialogType
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.startup_identity import StartupIdentityResult
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.demand_adapter_helpers import run_ready_slice
from tests.sqlite_writer_helpers import event_manager, inline_writer
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient, _prepare


@pytest.mark.asyncio
async def test_startup_admits_self_once_after_identity_binding(tmp_path: Path) -> None:
    conn, prior = _prepare(tmp_path / "self-startup.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
    identity = StartupIdentityResult(types.User(42, first_name="Self"), 42)

    class Cadence:
        calls = 0

        async def mark_refreshed(self, _now: float) -> None:
            self.calls += 1

    class Coordinator:
        def __init__(self) -> None:
            self.offered: list[DemandKind] = []

        def offer(self, kind: DemandKind) -> None:
            self.offered.append(kind)

    cadence = Cadence()
    coordinator = Coordinator()
    ctx = cast(
        _SyncMainContext,
        SimpleNamespace(
            demand_runtime=SimpleNamespace(startup_identity=SimpleNamespace(result=lambda: identity)),
            coordinator=coordinator,
            api_server=SimpleNamespace(
                self_id=42,
                _auth_scope=TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99),
                _policy=SimpleNamespace(full_user_pair_enabled=True),
            ),
            writer=inline_writer(conn),
            self_profile_cadence=cadence,
        ),
    )
    try:
        await _start_self_profile_after_replay(ctx)
        await _start_self_profile_after_replay(ctx)
        assert coordinator.offered == [DemandKind.ENTITY_PROFILE_REFRESH, DemandKind.ENTITY_PROFILE_REFRESH]
        assert conn.execute(
            "SELECT generation,follow_up_required FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone() == (1, 0)
        assert cadence.calls == 2
        with write_transaction(conn):
            conn.execute(
                "UPDATE entity_profile_refresh_state SET status='failed',retry_at=500,next_section='common_chats' "
                "WHERE entity_id=42"
            )
        await _start_self_profile_after_replay(ctx)
        assert conn.execute(
            "SELECT generation,retry_at,next_section,pair_eligible FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone() == (1, 500, "full_profile", 1)
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_self_profile_worker_drains_only_owned_pair(tmp_path: Path, enabled: bool) -> None:
    conn, prior = _prepare(tmp_path / "self.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
        assert admit_self_profile(
            conn, 42, kind=DialogType.USER, now=100, pair_mode="enabled" if enabled else "disabled"
        )
    client = _PairClient(channel_id=123)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=enabled))
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_profile_semantic_snapshots WHERE owner_account_id=42 AND entity_id=42"
        ).fetchone() == (0,)
        for _ in range(4):
            result = await run_ready_slice(adapter, now=100)
            if result is None:
                break
        assert client.full_user_calls == (1 if enabled else 2)
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
            "complete",
        )
        rows = conn.execute(
            "SELECT section,status FROM entity_detail_sections WHERE entity_id=42 AND status='fresh' ORDER BY section"
        ).fetchall()
        assert rows == [("full_profile", "fresh"), ("personal_channel", "fresh")]
        profile = service._profiles.read(42, now=100)
        assert profile is not None and profile.detail["personal_channel_id"] == 123
        before_read = client.full_user_calls
        local = await service.get_entity_info({"entity_id": 42})
        assert local["ok"] is True
        semantic = cast(dict[str, object], cast(dict[str, object], local["data"])["semantic"])
        assert semantic["availability"] == "retained"
        assert cast(dict[str, object], semantic["fields"])["full_user.personal_channel_id"] == 123
        assert client.full_user_calls == before_read
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_scope",
    [TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100), TelegramAuthScope(AUTH_SCOPE_VERSION, 43, 2, 99)],
)
async def test_self_worker_rejects_old_scope_result(tmp_path: Path, changed_scope: TelegramAuthScope) -> None:
    conn, prior = _prepare(tmp_path / "self-scope.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
        assert admit_self_profile(conn, 42, kind=DialogType.USER, now=100, pair_mode="enabled")
    current = [TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)]

    class ScopeChangingClient(_PairClient):
        async def fetch_user_profile(
            self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
        ) -> UserProfileObservation:
            result = await super().fetch_user_profile(user_id, target_kind, owner_account_id=owner_account_id)
            current[0] = changed_scope
            return result

    client = ScopeChangingClient()
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    service._deps = replace(service._deps, full_user_auth_scope=lambda: current[0])
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    try:
        await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100)
        assert client.full_user_calls == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_profile_semantic_snapshots WHERE owner_account_id=42 AND entity_id=42"
        ).fetchone() == (0,)
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
            "failed",
        )
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_self_worker_keeps_value_on_failure_then_accepts_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, prior = _prepare(tmp_path / "self-clear.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    clock = [100.0]
    boundary = pair_fixtures.ObservationBoundary
    monkeypatch.setattr(pair_fixtures, "ObservationBoundary", lambda _start, _end: boundary(clock[0], clock[0]))
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
        assert admit_self_profile(conn, 42, kind=DialogType.USER, now=100, pair_mode="enabled")
    client = _PairClient(channel_id=123)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    service._deps = replace(service._deps, now_provider=lambda: clock[0])
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    try:
        await run_ready_slice(adapter, now=clock[0])
        profile = service._profiles.read(42, now=100)
        assert profile is not None and profile.detail["personal_channel_id"] == 123
        clock[0] = 500.0
        with write_transaction(conn):
            assert service._profiles.request_section_refresh(
                42, ("full_profile", "personal_channel"), now=500, pair_mode="enabled"
            )
        original = client.fetch_user_profile

        async def fail(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("profile unavailable")

        monkeypatch.setattr(client, "fetch_user_profile", fail)
        await run_ready_slice(adapter, now=clock[0])
        retained = service._profiles.read(42, now=500)
        assert retained is not None and retained.detail["personal_channel_id"] == 123
        monkeypatch.setattr(client, "fetch_user_profile", original)
        client.channel_id = None
        clock[0] = 5000.0
        for _ in range(4):
            await run_ready_slice(adapter, now=clock[0])
        cleared = service._profiles.read(42, now=5000)
        assert cleared is not None and cleared.detail["personal_channel_id"] is None, (
            client.full_user_calls,
            conn.execute("SELECT status,retry_at,next_section,reason FROM entity_profile_refresh_state").fetchall(),
        )
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_self_update_during_pair_creates_one_followup(tmp_path: Path) -> None:
    conn, prior = _prepare(tmp_path / "self-race.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
        assert admit_self_profile(conn, 42, kind=DialogType.USER, now=100, pair_mode="enabled")
    started = asyncio.Event()
    resume = asyncio.Event()

    class BlockedClient(_PairClient):
        async def fetch_user_profile(self, user_id: int, target_kind: object, *, owner_account_id: int | None = None):
            if self.full_user_calls == 0:
                started.set()
                await resume.wait()
            return await super().fetch_user_profile(user_id, target_kind, owner_account_id=owner_account_id)  # type: ignore[arg-type]

    client = BlockedClient()
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    manager = cast(EventHandlerManager, event_manager(MagicMock(), conn, asyncio.Event()))
    manager.set_self_id(42)
    manager.bind_demand_sink(MagicMock())
    task = asyncio.create_task(run_ready_slice(adapter, now=100))
    try:
        await started.wait()
        await manager._persist_profile_pushes(sender=types.User(42, first_name="Newer self"))
        await manager.on_raw_identity_or_notify(types.UpdateUser(42))
        await manager.on_raw_identity_or_notify(types.UpdateUser(42))
        resume.set()
        await task
        assert conn.execute("SELECT name FROM entities WHERE id=42").fetchone() == ("Newer self",)
        trace = [
            (
                client.full_user_calls,
                conn.execute(
                    "SELECT status,generation,next_section,pair_eligible,pair_mode FROM entity_profile_refresh_state WHERE entity_id=42"
                ).fetchone(),
            )
        ]
        for _ in range(3):
            await run_ready_slice(adapter, now=100)
            trace.append(
                (
                    client.full_user_calls,
                    conn.execute(
                        "SELECT status,generation,next_section,pair_eligible,pair_mode FROM entity_profile_refresh_state WHERE entity_id=42"
                    ).fetchone(),
                )
            )
        assert client.full_user_calls == 2, trace
        assert conn.execute(
            "SELECT status,follow_up_required FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone() == ("complete", 0)
    finally:
        resume.set()
        await task
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_self_scope_change_while_writer_queued_rejects_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    conn, prior = _prepare(tmp_path / "queued-scope.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
        admit_self_profile(conn, 42, kind=DialogType.USER, now=100, pair_mode="enabled" if enabled else "disabled")
    current = [TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)]
    client = _PairClient(channel_id=123)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=enabled))
    service._deps = replace(service._deps, full_user_auth_scope=lambda: current[0])
    original_write = service._write_profiles
    switched = False

    async def queued_write[T](operation: Callable[[EntityProfileRepository], T], *, track_rejection: bool = True) -> T:
        nonlocal switched
        if client.full_user_calls and not switched:
            switched = True
            current[0] = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100)
        return await original_write(operation, track_rejection=track_rejection)

    monkeypatch.setattr(service, "_write_profiles", queued_write)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    try:
        await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100)
        assert switched
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_profile_semantic_snapshots WHERE owner_account_id=42 AND entity_id=42"
        ).fetchone() == (0,)
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
            "failed",
        )
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_self_followup_changes_coordinator_revision_when_pair_becomes_eligible(tmp_path: Path) -> None:
    conn, prior = _prepare(tmp_path / "followup-revision.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
        admit_self_profile(conn, 42, kind=DialogType.USER, now=100, pair_mode="enabled")
    client = _PairClient(channel_id=123)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    manager = cast(EventHandlerManager, event_manager(MagicMock(), conn, asyncio.Event()))
    manager.set_self_id(42)
    manager.bind_demand_sink(MagicMock())
    try:
        await run_ready_slice(adapter, now=100)
        with write_transaction(conn):
            service._profiles.request_section_refresh(42, ("full_profile",), now=100, pair_mode="enabled")
        await manager.on_raw_identity_or_notify(types.UpdateUser(42))
        selected = adapter.status(100)
        assert selected is not None and selected.selected is not None
        await run_ready_slice(adapter, now=100)
        next_status = adapter.status(100, excluded=frozenset({selected.selected}))
        assert next_status is not None and next_status.selected != selected.selected
        await run_ready_slice(adapter, now=100)
        assert client.full_user_calls == 2
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
            "complete",
        )
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rotate_key", [False, True])
async def test_self_pair_commits_on_real_sqlite_writer_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rotate_key: bool
) -> None:
    path = tmp_path / "writer-thread.sqlite"
    conn, prior = _prepare(path, migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
        admit_self_profile(conn, 42, kind=DialogType.USER, now=100, pair_mode="enabled")
    writer = await SQLiteWriter.open(path)
    client = _PairClient(channel_id=123)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    current = [TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)]
    service._deps = replace(service._deps, writer=writer, full_user_auth_scope=lambda: current[0])
    original_write = service._write_profiles

    async def queued_write[T](operation: Callable[[EntityProfileRepository], T], *, track_rejection: bool = True) -> T:
        if rotate_key and client.full_user_calls:
            current[0] = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100)
        return await original_write(operation, track_rejection=track_rejection)

    monkeypatch.setattr(service, "_write_profiles", queued_write)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    try:
        await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100)
        assert client.full_user_calls == 1
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
            "failed" if rotate_key else "complete",
        )
        profile = service._profiles.read(42, now=100)
        assert profile is not None
        assert profile.detail.get("personal_channel_id") == (None if rotate_key else 123)
    finally:
        await service.shutdown()
        await writer.aclose()
        conn.close()
