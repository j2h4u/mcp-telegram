"""Exact owner selection and persisted progress receipts for profile demand."""

from __future__ import annotations

# pyright: reportAny=false
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import DurableRefreshSliceResult, EntityProfileDemandAdapter
from mcp_telegram.entity_profile.repository import EntityRefreshCursor
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred
from tests.test_dm_profile_repository import _peer, _repository
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient


def _add_pending(conn: sqlite3.Connection, entity_id: int, now: int = 100) -> None:
    with conn:
        _peer(conn, entity_id)
        conn.execute(
            "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,generation) "
            "VALUES(?,'pending',?,'avatar_history',1)",
            (entity_id, now),
        )


def test_five_siblings_and_retry_timestamp_do_not_change_work_revision(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "siblings.sqlite")
    try:
        _peer(conn, 42)
        repo.mark_pending(42, now=100)
        for entity_id in (77, 99, 100, 101):
            _add_pending(conn, entity_id)
        account_id = 99
        selected: list[int] = []
        excluded = frozenset()
        initial_revision = None
        for expected in (42, 77, 99, 100, 101):
            cursor = repo.next_due_refresh(now=120, account_id=account_id, excluded=excluded)
            assert cursor is not None and cursor.entity_id == expected
            revision = repo.refresh_work_revision(cursor, account_id)
            selected.append(cursor.entity_id)
            if expected == 42:
                initial_revision = revision
                assert repo.mark_section_failure(cursor, now=101, reason="flood_wait", retry_at=120)
                changed_deadline = repo.next_due_refresh(now=120, account_id=account_id, entity_id=42)
                assert changed_deadline is not None and changed_deadline.retry_at == 120
                assert repo.refresh_work_revision(changed_deadline, account_id) == initial_revision
            excluded = excluded | {revision}
        assert selected == [42, 77, 99, 100, 101]
        assert repo.next_due_refresh(now=120, account_id=account_id, excluded=excluded) is None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_local_admission_deferral_does_not_write_profile_facts_or_retry(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "deferred.sqlite")
    _peer(conn, 42)
    repo.mark_pending(42, now=100)

    class DeferredClient(_PairClient):
        async def fetch_user_profile(self, user_id: int, target_kind: object, *, owner_account_id: int | None = None):
            del user_id, target_kind, owner_account_id
            raise TelegramRpcAdmissionDeferred(1, eligible_at=999.0, reason="method_hold")

    service = cast(DaemonEntityInfoService, _pair_service(conn, DeferredClient(channel_id=None), enabled=False))
    service._deps = replace(service._deps, now_provider=lambda: 100.0)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    status = service._durable_refresh_status(100)
    assert status is not None and status.selected is not None
    before_state = conn.execute(
        "SELECT status,retry_at,reason,updated_at,next_section,acquisition_cursor,generation "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone()
    before_details = conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=42").fetchone()
    before_sections = conn.execute("SELECT COUNT(*) FROM entity_detail_sections WHERE entity_id=42").fetchone()
    try:
        with pytest.raises(TelegramRpcAdmissionDeferred):
            await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        assert (
            conn.execute(
                "SELECT status,retry_at,reason,updated_at,next_section,acquisition_cursor,generation "
                "FROM entity_profile_refresh_state WHERE entity_id=42"
            ).fetchone()
            == before_state
        )
        assert conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=42").fetchone() == before_details
        assert (
            conn.execute("SELECT COUNT(*) FROM entity_detail_sections WHERE entity_id=42").fetchone() == before_sections
        )
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_rejected_preparation_cas_returns_stale_without_rpc(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "stale.sqlite")
    _peer(conn, 42)
    repo.mark_pending(42, now=100)
    client = _PairClient(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=False))
    status = service._durable_refresh_status(100)
    assert status is not None and status.selected is not None

    prepare = service._prepare_refresh_revision

    def race_cursor(profiles: object, cursor: object, scope: object) -> bool:
        with write_transaction(conn):
            conn.execute(
                "UPDATE entity_profile_refresh_state SET next_section='common_chats',updated_at=101 WHERE entity_id=42"
            )
        return prepare(profiles, cursor, scope)  # type: ignore[arg-type]

    service._prepare_refresh_revision = race_cursor  # type: ignore[method-assign]
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    try:
        outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        assert outcome.kind is SliceOutcomeKind.STALE
        assert client.full_user_calls == 0
        assert conn.execute(
            "SELECT status,retry_at,reason,updated_at,next_section,acquisition_cursor,generation "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone() == ("pending", None, "refresh_queued", 101, "common_chats", 0, 1)
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_accepted_page_commit_returns_progress_receipt_from_sql(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "accepted.sqlite")
    _peer(conn, 42)
    repo.mark_pending(42, now=100)
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='avatar_history' WHERE entity_id=42")
    client = _PairClient(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=False))
    status = service._durable_refresh_status(100)
    assert status is not None and status.selected is not None
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    try:
        outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        assert outcome.kind is SliceOutcomeKind.PROGRESSED
        assert outcome.committed_revision is not None
        current = repo.next_due_refresh(now=100, entity_id=42)
        assert current is not None
        assert (
            repo.refresh_work_revision(current, status.selected.key.account_id).revision == outcome.committed_revision
        )
        assert client.full_user_calls == 0
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_negative_channel_id_remains_a_valid_selected_target(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "negative-channel.sqlite")
    _peer(conn, -42, kind="channel")
    repo.mark_pending(-42, now=100)
    service = cast(DaemonEntityInfoService, _pair_service(conn, _PairClient(channel_id=None), enabled=False))
    status = service._durable_refresh_status(100)
    assert status is not None and status.selected is not None
    prepared: list[int] = []

    def reject_cas(profiles: object, cursor: object, scope: object) -> bool:
        del profiles, scope
        prepared.append(cursor.entity_id)  # type: ignore[attr-defined]
        return False

    service._prepare_refresh_revision = reject_cas  # type: ignore[method-assign]
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    try:
        outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        assert prepared == [-42]
        assert outcome.kind is SliceOutcomeKind.STALE
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_failure_metadata_then_other_writer_advance_is_not_progress(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "failure-race.sqlite")
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,generation) "
            "VALUES(42,'pending',100,'full_profile',1)"
        )
    service = cast(DaemonEntityInfoService, _pair_service(conn, _PairClient(channel_id=None), enabled=False))
    service._deps = replace(service._deps, now_provider=lambda: 100.0)

    async def fail_then_race(cursor: EntityRefreshCursor, *, now: int) -> DurableRefreshSliceResult:
        accepted = await service._write_profiles(
            lambda profiles: profiles.mark_core_failure(
                cursor,
                now=now,
                reason="entity_unavailable",
                retry_at=now,  # type: ignore[arg-type]
            )
        )
        assert accepted is True
        with write_transaction(conn):
            conn.execute(
                "UPDATE entity_profile_refresh_state SET status='pending',retry_at=NULL,reason='other_writer', "
                "updated_at=101,next_section='common_chats' WHERE entity_id=42"
            )
        return DurableRefreshSliceResult(42)

    service._run_unknown_entity_refresh = fail_then_race  # type: ignore[method-assign]
    status = service._durable_refresh_status(100)
    assert status is not None and status.selected is not None
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    try:
        outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        assert outcome.kind is SliceOutcomeKind.STALE
        assert outcome.committed_revision is None
        current = repo.next_due_refresh(now=101, entity_id=42)
        assert current is not None and current.next_section == "common_chats"
    finally:
        await service.shutdown()
        conn.close()
