"""Delivered profile inserts and durable acquisitions share one revision fence."""

# SQLite result shapes are asserted explicitly.
# pyright: reportAny=false
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import ProfileFieldObservation
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntityRefreshCursor, EntitySectionCommit
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import DemandStatus, DurableDemandAdapter
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_gateway import _supplied_profile_user
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_dm_profile_repository import _peer, _repository
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient
from tests.test_story_profile_diagnostics import _QueuedWriter
from tests.test_telegram_demand_coordinator import _adapters, _Clock


def test_first_delivered_detail_inherits_existing_identity_fence(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "creator.sqlite")
    try:
        _peer(conn, 42)
        repo.mark_pending(42, now=100)
        repo.save_core({"id": 42, "type": "user", "name": "Peer 42"}, now=101)
        assert conn.execute("SELECT profile_revision FROM entity_profile_refresh_state").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM entity_details").fetchone() == (0,)
        repo.apply_profile_update(42, {"phone": "123"}, observation=ProfileFieldObservation(42, 1, 102, "update"))
        assert conn.execute("SELECT profile_revision,fetched_at FROM entity_details").fetchone() == (1, 102)
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        conn.close()


type Legacy = tuple[sqlite3.Connection, EntityProfileRepository, EntityRefreshCursor]


@pytest.fixture
def legacy(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Legacy]:
    conn, repo = _repository(tmp_path / "legacy.sqlite")
    request.addfinalizer(conn.close)
    _peer(conn, 42)
    repo.mark_pending(42, now=100)
    repo.save_core({"id": 42, "type": "user", "name": "Peer 42"}, now=101)
    with write_transaction(conn):
        order = allocate_observation_order(conn)
    repo.apply_supplied_user(
        _supplied_profile_user(types.User(42, first_name="Peer 42", phone="123")),
        observation=ProfileFieldObservation(42, order, 102, "update"),
    )
    assert conn.execute("SELECT profile_revision FROM entity_details").fetchone() == (1,)
    with write_transaction(conn):
        # Authentic persisted pre-fix state: first supplied detail used schema default zero.
        conn.execute("UPDATE entity_details SET profile_revision=0 WHERE entity_id=42")
        conn.execute(
            "UPDATE entity_profile_refresh_state SET status='failed',generation=2,reason='flood_wait',"
            "updated_at=200,retry_at=500,pair_mode='enabled',pair_attempts=4,pair_retries=3 WHERE entity_id=42"
        )
        conn.execute(
            "UPDATE entity_detail_sections SET status='stale',observed_at=102,payload_json='{}' "
            "WHERE entity_id=42 AND section='full_profile'"
        )
    cursor = repo.next_due_refresh(now=500)
    assert cursor is not None and cursor.profile_revision == 1
    try:
        yield conn, repo, cursor
    finally:
        conn.close()


def _record(conn: sqlite3.Connection, table: str) -> dict[str, object]:
    cursor = conn.execute(f"SELECT * FROM {table} WHERE entity_id=42")
    row = cursor.fetchone()
    assert row is not None and cursor.description is not None
    return dict(zip((column[0] for column in cursor.description), row, strict=True))


def _evidence(conn: sqlite3.Connection) -> list[list[tuple[object, ...]]]:
    return [
        cast(
            list[tuple[object, ...]], conn.execute(f"SELECT * FROM {table} WHERE entity_id=42 ORDER BY 1,2").fetchall()
        )
        for table in (
            "entity_detail_sections",
            "entity_profile_field_observations",
            "entity_profile_semantic_snapshots",
        )
    ]


def test_owned_repair_changes_only_both_revision_tokens_and_rejects_old_cursor(legacy: Legacy) -> None:
    conn, repo, old = legacy
    detail = _record(conn, "entity_details")
    state = _record(conn, "entity_profile_refresh_state")
    evidence = _evidence(conn)
    assert not repo.prepare_refresh_revision(old, owner_account_id=42)
    assert _record(conn, "entity_details") == {**detail, "profile_revision": 2}
    assert _record(conn, "entity_profile_refresh_state") == {**state, "profile_revision": 2}
    assert _evidence(conn) == evidence
    assert not repo.commit_section(old, EntitySectionCommit({"about": "late old response"}), now=600)
    fresh = repo.next_due_refresh(now=500)
    assert fresh is not None
    assert fresh == replace(old, profile_revision=2)
    assert repo.prepare_refresh_revision(fresh, owner_account_id=42)
    assert repo.commit_section(fresh, EntitySectionCommit({"about": "fresh response"}), now=600)
    assert repo.next_due_refresh(now=600) != fresh


@pytest.mark.parametrize("fence", ["owner", "scope", "missing_owner", "generation", "page", "revision"])
def test_repair_rejects_unowned_or_changed_captured_state(legacy: Legacy, fence: str) -> None:
    conn, repo, cursor = legacy
    owner: int | None = 42
    if fence == "owner":
        owner = 99
    elif fence == "scope":
        owner = None
    elif fence == "missing_owner":
        with write_transaction(conn):
            conn.execute("UPDATE entity_details SET profile_owner_account_id=NULL WHERE entity_id=42")
    elif fence == "generation":
        cursor = replace(cursor, generation=cursor.generation + 1)
    elif fence == "page":
        cursor = replace(cursor, page_cursor_json='{"offset":1}')
    else:
        cursor = replace(cursor, profile_revision=0)
    before = (_record(conn, "entity_details"), _record(conn, "entity_profile_refresh_state"), _evidence(conn))
    if fence in {"owner", "scope", "missing_owner"}:
        with pytest.raises(RuntimeError, match="revision ownership unavailable"):
            repo.prepare_refresh_revision(cursor, owner_account_id=owner)
    else:
        assert not repo.prepare_refresh_revision(cursor, owner_account_id=owner)
    assert (_record(conn, "entity_details"), _record(conn, "entity_profile_refresh_state"), _evidence(conn)) == before


def test_second_cas_failure_rolls_back_detail_fence(legacy: Legacy) -> None:
    conn, repo, cursor = legacy
    before = (_record(conn, "entity_details"), _record(conn, "entity_profile_refresh_state"), _evidence(conn))
    with write_transaction(conn):
        conn.execute(
            "CREATE TRIGGER reject_revision BEFORE UPDATE OF profile_revision ON entity_profile_refresh_state "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
    with pytest.raises(sqlite3.OperationalError, match="reconciliation cursor changed"):
        repo.prepare_refresh_revision(cursor, owner_account_id=42)
    assert (_record(conn, "entity_details"), _record(conn, "entity_profile_refresh_state"), _evidence(conn)) == before


@pytest.mark.asyncio
async def test_scheduled_owner_repairs_without_rpc_then_refetches_and_progresses(legacy: Legacy) -> None:
    conn, repo, old = legacy
    client = _PairClient(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    service._deps = replace(service._deps, now_provider=lambda: 1000.0)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    before = _evidence(conn)
    try:
        await run_ready_slice(adapter, now=1000.0)
        assert client.full_user_calls == 0
        assert _evidence(conn) == before
        assert repo.next_due_refresh(now=1000) == replace(old, profile_revision=2)
        await run_ready_slice(adapter, now=1000.0)
        assert client.full_user_calls == 1
        fresh = repo.next_due_refresh(now=1000)
        assert fresh is not None and fresh.next_section == "common_chats"
        assert fresh.profile_revision == 3 and fresh.generation == old.generation
    finally:
        await service.shutdown()


def test_existing_delivered_detail_never_lowers_or_resets_revision(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "existing.sqlite")
    try:
        _peer(conn, 42)
        repo.mark_pending(42, now=100)
        repo.save_core({"id": 42, "type": "user", "name": "Peer 42"}, now=101)
        repo.apply_profile_update(42, {"phone": "123"}, observation=ProfileFieldObservation(42, 1, 102, "update"))
        repo.apply_profile_update(42, {"phone": "456"}, observation=ProfileFieldObservation(42, 2, 103, "update"))
        assert conn.execute("SELECT profile_revision,fetched_at FROM entity_details").fetchone() == (1, 102)
        repo.apply_profile_update(42, {"name": "Changed"}, observation=ProfileFieldObservation(42, 3, 104, "update"))
        assert conn.execute("SELECT profile_revision FROM entity_details").fetchone() == (2,)
        assert conn.execute("SELECT profile_revision FROM entity_profile_refresh_state").fetchone() == (2,)
    finally:
        conn.close()


def test_missing_detail_allows_normal_core_acquisition_without_repair(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "missing.sqlite")
    try:
        _peer(conn, 42)
        repo.mark_pending(42, now=100)
        cursor = repo.next_due_refresh(now=100)
        assert cursor is not None
        assert repo.prepare_refresh_revision(cursor, owner_account_id=None)
        assert conn.execute("SELECT COUNT(*) FROM entity_details").fetchone() == (0,)
        assert repo.next_due_refresh(now=100) == cursor
    finally:
        conn.close()


def test_repair_uses_maximum_when_detail_fence_is_ahead(legacy: Legacy) -> None:
    conn, repo, cursor = legacy
    with write_transaction(conn):
        conn.execute("UPDATE entity_details SET profile_revision=3 WHERE entity_id=42")
    assert not repo.prepare_refresh_revision(cursor, owner_account_id=42)
    assert conn.execute("SELECT profile_revision FROM entity_details").fetchone() == (4,)
    assert repo.next_due_refresh(now=500) == replace(cursor, profile_revision=4)


@pytest.mark.asyncio
async def test_queued_repair_rechecks_authenticated_account_before_writing(legacy: Legacy) -> None:
    conn, _repo, _cursor = legacy
    client = _PairClient(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    writer = _QueuedWriter(conn)

    def captured_scope() -> TelegramAuthScope:
        writer.pause = True
        return TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)

    service._deps = replace(
        service._deps,
        writer=cast(SQLiteWriter, writer),
        now_provider=lambda: 1000.0,
        full_user_auth_scope=captured_scope,
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    before = (_record(conn, "entity_details"), _record(conn, "entity_profile_refresh_state"), _evidence(conn))
    task = asyncio.create_task(run_ready_slice(EntityProfileDemandAdapter(coordinator), now=1000.0))
    try:
        await asyncio.wait_for(writer.waiting.wait(), timeout=2)
        service._deps = replace(
            service._deps, full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 99, 2, 100)
        )
        writer.release.set()
        with pytest.raises(RuntimeError, match="revision scope changed"):
            await task
        assert client.full_user_calls == 0
        assert (
            _record(conn, "entity_details"),
            _record(conn, "entity_profile_refresh_state"),
            _evidence(conn),
        ) == before
    finally:
        writer.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.shutdown()


@pytest.mark.asyncio
async def test_unowned_mismatch_excludes_selected_revision_and_leaves_siblings_ready(legacy: Legacy) -> None:
    conn = legacy[0]
    client = _PairClient(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    service._deps = replace(
        service._deps,
        now_provider=lambda: 1000.0,
        full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 99, 2, 100),
    )
    refresh = service.refresh_coordinator
    assert refresh is not None
    kind = DemandKind.ENTITY_PROFILE_REFRESH
    sibling = DemandKind.DELTA_GAP_FILL
    supplied = _adapters({sibling: DemandStatus(0)})
    adapters: dict[DemandKind, DurableDemandAdapter] = dict(supplied)
    adapters[kind] = EntityProfileDemandAdapter(refresh)
    shutdown = asyncio.Event()
    clock = _Clock(1000)
    observations: list[dict[str, object]] = []

    def observe(**values: object) -> None:
        observations.append(values)

    coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=clock, safety_scan_seconds=10, observer=observe)

    async def sibling_runs() -> None:
        assert kind not in coordinator.scan(now=1000)
        assert kind not in coordinator.scan(now=1010)
        shutdown.set()

    supplied[sibling].on_run = sibling_runs
    before = (_record(conn, "entity_details"), _record(conn, "entity_profile_refresh_state"), _evidence(conn))
    try:
        await asyncio.wait_for(coordinator.run(), timeout=2)
        assert client.full_user_calls == 0
        assert (
            _record(conn, "entity_details"),
            _record(conn, "entity_profile_refresh_state"),
            _evidence(conn),
        ) == before
        assert len(supplied[sibling].run_calls) == 1
        failed = [row for row in observations if row["demand_kind"] == kind and row["outcome"] == "failed"]
        assert len(failed) == 1 and failed[0]["actual_attempts"] == 0
        assert failed[0]["reason"] == "RuntimeError"
    finally:
        await service.shutdown()
