"""Profile writer preparation and commits are not bounded by wire response timers."""

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, EntityRefreshCoordinator
from mcp_telegram.entity_profile.repository import EntityProfileRepository, admit_self_profile
from mcp_telegram.models import DialogType
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient, _prepare


def _block_method[**P, T](method: Callable[P, T], entered: threading.Event, release: threading.Event) -> Callable[P, T]:
    def blocked(*args: P.args, **kwargs: P.kwargs) -> T:
        entered.set()
        assert release.wait(5)
        return method(*args, **kwargs)

    return blocked


def _prepare_self(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
        admit_self_profile(conn, 42, kind=DialogType.USER, now=100, pair_mode="enabled")


def _block_writer(
    phase: str, monkeypatch: pytest.MonkeyPatch, entered: threading.Event, release: threading.Event
) -> None:
    if phase in {"commit", "cancel_commit"}:
        monkeypatch.setattr(
            EntityProfileRepository,
            "commit_full_user_pair",
            _block_method(EntityProfileRepository.commit_full_user_pair, entered, release),
        )
    else:
        monkeypatch.setattr(
            EntityProfileRepository,
            "prepare_refresh_revision",
            _block_method(EntityProfileRepository.prepare_refresh_revision, entered, release),
        )


async def _assert_cancelled(
    task: asyncio.Task[SliceOutcome | None],
    phase: str,
    coordinator: EntityRefreshCoordinator,
    service: DaemonEntityInfoService,
) -> None:
    with pytest.raises(asyncio.CancelledError):
        await task
    if phase == "cancel_commit":
        assert coordinator.queue_depth == 0
        profile = service._profiles.read(42, now=100)
        assert profile is not None and profile.detail.get("personal_channel_id") == 123


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preparation", "commit", "cancel", "cancel_commit"])
async def test_deadline_drains_real_writer_and_fences_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    path = tmp_path / "deadline.sqlite"
    conn, prior = _prepare(path, migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    _prepare_self(conn)
    writer = await SQLiteWriter.open(path)
    client = _PairClient()
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    scope = service._deps.full_user_auth_scope
    assert scope is not None
    current = [scope()]
    service._deps = replace(
        service._deps,
        writer=writer,
        self_id=42,
        full_user_auth_scope=lambda: current[0],
        refresh_limits=replace(
            service._deps.refresh_limits,
            foreground_resolve_seconds=0.005,
            per_rpc_seconds=0.01,
            whole_refresh_seconds=0.02,
        ),
    )
    entered = threading.Event()
    release = threading.Event()
    _block_writer(phase, monkeypatch, entered, release)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    coordinator._limits = service._deps.refresh_limits
    if phase in {"commit", "cancel_commit"}:
        assert coordinator.enqueue(42)
    adapter = EntityProfileDemandAdapter(coordinator)
    task = asyncio.create_task(run_ready_slice(adapter, now=100))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if phase in {"cancel", "cancel_commit"}:
            task.cancel()
        await asyncio.sleep(0.04)
        assert not task.done()
        if phase == "preparation":
            assert client.full_user_calls == 0
        release.set()
        if phase in {"cancel", "cancel_commit"}:
            await _assert_cancelled(task, phase, coordinator, service)
            return
        outcome = await task
        assert outcome is not None
        assert outcome.kind in {SliceOutcomeKind.PROGRESSED, SliceOutcomeKind.TERMINAL}
        assert client.full_user_calls == 1
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() != (
            "failed",
        )
        if phase == "commit":
            assert coordinator.queue_depth == 0
    finally:
        release.set()
        await service.shutdown()
        await writer.aclose()
        conn.close()
