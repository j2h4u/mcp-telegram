"""Durable per-section retry pacing survives failures and restart."""

# SQLite result shapes are asserted below.
# pyright: reportAny=false
from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import TargetKind, UserProfileObservation
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, RefreshLimits
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sync_db import load_account_cooldown_until_utc
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from tests.test_dm_profile_repository import _peer, _repository
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient


def test_backoff_survives_ordinary_failure_coalescing_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "retry.sqlite"
    conn, repo = _repository(path)
    _peer(conn, 42)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    assert repo.mark_section_failure(cursor, now=108, reason="flood_wait", retry_at=116)
    assert repo.next_due_refresh(now=407) is None
    assert repo.next_due_refresh(now=408) == replace(cursor, retry_at=408)
    repo.mark_pending(42, now=200)
    assert conn.execute("SELECT updated_at,retry_at FROM entity_profile_refresh_state").fetchone() == (108, 408)
    conn.close()
    conn = sqlite3.connect(path)
    repo = EntityProfileRepository(conn, section_ttl_seconds=10)
    try:
        cursor = repo.next_due_refresh(now=408)
        assert cursor is not None
        assert repo.mark_section_failure(cursor, now=410, reason="timeouterror", retry_at=470)
        assert conn.execute("SELECT reason,retry_at FROM entity_profile_refresh_state").fetchone() == (
            "timeouterror",
            710,
        )
        cursor = repo.next_due_refresh(now=710)
        assert cursor is not None
        assert repo.mark_section_failure(cursor, now=712, reason="flood_wait", retry_at=720)
        assert conn.execute("SELECT retry_at FROM entity_profile_refresh_state").fetchone() == (1312,)
        assert not repo.mark_section_failure(
            replace(cursor, generation=cursor.generation + 1), now=713, reason="flood_wait", retry_at=9999
        )
        assert conn.execute("SELECT retry_at FROM entity_profile_refresh_state").fetchone() == (1312,)
        cursor = repo.next_due_refresh(now=1312)
        assert cursor is not None
        assert repo.commit_section(cursor, EntitySectionCommit({}), now=1313)
        fresh = repo.next_due_refresh(now=1313)
        assert fresh is not None and fresh.next_section != cursor.next_section
        assert repo.mark_section_failure(fresh, now=1314, reason="flood_wait", retry_at=1320)
        assert conn.execute("SELECT retry_at FROM entity_profile_refresh_state").fetchone() == (1614,)
    finally:
        conn.close()


def test_uncapped_server_floor_and_future_deadline_are_preserved(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "floor.sqlite")
    _peer(conn, 42)
    repo.mark_pending(42, now=100)
    try:
        cursor = repo.next_due_refresh(now=100)
        assert cursor is not None
        assert repo.mark_section_failure(cursor, now=110, reason="flood_wait", retry_at=10_110)
        assert repo.mark_section_failure(cursor, now=111, reason="flood_wait", retry_at=112)
        assert conn.execute("SELECT retry_at FROM entity_profile_refresh_state").fetchone() == (10_110,)
        for now in (10_110, 13_710):
            cursor = repo.next_due_refresh(now=now)
            assert cursor is not None
            assert repo.mark_section_failure(cursor, now=now, reason="flood_wait", retry_at=now + 8)
            assert conn.execute("SELECT retry_at FROM entity_profile_refresh_state").fetchone() == (now + 3600,)
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("paired", [False, True])
async def test_scheduled_flood_uses_actual_failure_clock_and_does_not_repeat(
    tmp_path: Path, paired: bool, request: pytest.FixtureRequest
) -> None:
    clock = [100.0]

    class FloodClient(_PairClient):
        async def fetch_user_profile(
            self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
        ) -> UserProfileObservation:
            del user_id, target_kind, owner_account_id
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            self.full_user_calls += 1
            clock[0] += 7
            raise TelegramRpcThrottled(retry_after_seconds=8)

    conn, repo = _repository(tmp_path / "scheduled.sqlite")
    request.addfinalizer(conn.close)
    _peer(conn, 42)
    client = FloodClient(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=paired))
    service._deps = replace(service._deps, now_provider=lambda: clock[0])
    repo.mark_pending(42, now=100)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    try:
        await adapter.run_slice(RpcAttemptBudget(limit=1))
        assert conn.execute("SELECT updated_at,retry_at,reason FROM entity_profile_refresh_state").fetchone() == (
            107,
            407,
            "flood_wait",
        )
        assert client.full_user_calls == 1
        clock[0] = 406
        await adapter.run_slice(RpcAttemptBudget(limit=1))
        assert client.full_user_calls == 1
        clock[0] = 407
        await adapter.run_slice(RpcAttemptBudget(limit=1))
        assert client.full_user_calls == 2
        assert conn.execute("SELECT updated_at,retry_at FROM entity_profile_refresh_state").fetchone() == (414, 1014)
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.parametrize(
    "values",
    [
        {"flood_retry_base_seconds": 0},
        {"flood_retry_cap_seconds": True},
        {"flood_retry_base_seconds": 500, "flood_retry_cap_seconds": 300},
    ],
)
def test_flood_retry_policy_validates(values: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        RefreshLimits(**values)


def test_backoff_is_per_peer_and_section_not_an_account_guard_replacement(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "peers.sqlite")
    try:
        for entity_id in (42, 43):
            _peer(conn, entity_id)
            repo.mark_pending(entity_id, now=100)
        first = repo.next_due_refresh(now=100)
        assert first is not None and first.entity_id == 42
        assert repo.mark_section_failure(first, now=100, reason="flood_wait", retry_at=108)
        second = repo.next_due_refresh(now=100)
        assert second is not None and second.entity_id == 43
        assert repo.mark_section_failure(second, now=100, reason="flood_wait", retry_at=110)
        first = repo.next_due_refresh(now=400)
        assert first is not None and first.entity_id == 42
        assert repo.mark_section_failure(first, now=400, reason="flood_wait", retry_at=408)
        assert conn.execute(
            "SELECT entity_id,retry_at FROM entity_profile_refresh_state ORDER BY entity_id"
        ).fetchall() == [(42, 1000), (43, 400)]
    finally:
        conn.close()


def test_scope_change_keeps_control_plane_short_retry(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "scope.sqlite")
    try:
        conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES (?,?)", ("telegram_account_cooldown_until_utc", "20000.0")
        )
        conn.commit()
        _peer(conn, 42)
        repo.mark_pending(42, now=100)
        cursor = repo.next_due_refresh(now=100)
        assert cursor is not None
        assert repo.mark_section_failure(cursor, now=100, reason="flood_wait", retry_at=10_000)
        assert repo.mark_section_failure(cursor, now=110, reason="auth_scope_changed", retry_at=111)
        assert conn.execute("SELECT reason,retry_at FROM entity_profile_refresh_state").fetchone() == (
            "auth_scope_changed",
            111,
        )
        assert load_account_cooldown_until_utc(conn) == 20_000.0
    finally:
        conn.close()


def test_unknown_core_failure_is_cursor_fenced_with_foreign_keys(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "unknown.sqlite")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        conn.execute("INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at) VALUES (42,'pending',100)")
        conn.commit()
        cursor = repo.next_due_refresh(now=100)
        assert cursor is not None
        assert repo.mark_core_failure(cursor, now=107, reason="flood_wait", retry_at=115)
        assert repo.mark_core_failure(cursor, now=407, reason="entity_unavailable", retry_at=467)
        assert conn.execute("SELECT reason,retry_at FROM entity_profile_refresh_state").fetchone() == (
            "entity_unavailable",
            707,
        )
        assert not repo.mark_core_failure(
            replace(cursor, generation=cursor.generation + 1), now=408, reason="flood_wait", retry_at=9000
        )
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM entity_detail_sections").fetchone() == (0,)
    finally:
        conn.close()
