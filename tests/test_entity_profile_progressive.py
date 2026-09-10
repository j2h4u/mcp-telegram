"""Focused contract tests for progressive entity profiles."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from jsonschema import validate
from telethon.tl.types import User  # type: ignore[import-untyped]

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService, EntityInfoDeps
from mcp_telegram.entity_profile.contracts import PROFILE_SECTIONS
from mcp_telegram.entity_profile.refresh import (
    DurableRefreshSliceResult,
    DurableRefreshTerminal,
    EntityProfileDemandAdapter,
    EntityRefreshCoordinator,
    RefreshEnqueueResult,
    RefreshLimits,
)
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sync_db import _apply_migration_57, _apply_migrations, ensure_sync_schema
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, current_rpc_scope
from mcp_telegram.tools.entity_info import GET_ENTITY_INFO_OUTPUT_SCHEMA, GetEntityInfo, _entity_structured_content


class _UnusedClient:
    async def get_entity(self, entity_id: int) -> object:
        raise AssertionError("client should not be called in this test")

    async def get_messages(self, entity: object, ids: list[int]) -> object:
        raise AssertionError("client should not be called in this test")

    async def __call__(self, request: object) -> object:
        raise AssertionError("client should not be called in this test")

    def iter_participants(self, peer: object, limit: int = 0) -> AsyncIterator[object]:
        raise AssertionError("client should not be called in this test")

    def iter_dialogs(self) -> AsyncIterator[object]:
        raise AssertionError("client should not be called in this test")


class _FloodClient(_UnusedClient):
    async def __call__(self, _request: object) -> object:
        raise TelegramRpcThrottled(retry_after_seconds=7)


def _sections_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE entity_detail_sections (
            entity_id INTEGER NOT NULL, section TEXT NOT NULL, status TEXT NOT NULL,
            observed_at INTEGER, reason TEXT, payload_json TEXT, retry_at INTEGER,
            PRIMARY KEY(entity_id, section)
        ) WITHOUT ROWID"""
    )
    conn.execute(
        """CREATE TABLE entity_profile_refresh_state (
            entity_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
            retry_at INTEGER, reason TEXT, updated_at INTEGER NOT NULL,
            next_section TEXT NOT NULL DEFAULT 'full_profile',
            acquisition_cursor INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID"""
    )


def test_local_core_has_pending_sections_without_rpc() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'User', 'Local User', 'local', NULL, 100)")
    profile = EntityProfileRepository(conn, section_ttl_seconds=300).read(42, now=101)
    assert profile is not None
    assert profile.detail == {"id": 42, "type": "user", "name": "Local User", "username": "local"}
    assert {item["status"] for item in profile.sections.values()} == {"pending", "not_applicable"}
    conn.close()


@pytest.mark.asyncio
async def test_refresh_coordinator_coalesces_waiters_until_durable_terminal_signal() -> None:
    coordinator = EntityRefreshCoordinator(limits=RefreshLimits(max_queued_refreshes=2))
    assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
    assert coordinator.enqueue(42) is RefreshEnqueueResult.COALESCED
    assert coordinator.queue_depth == 1

    waiter = asyncio.create_task(coordinator.wait_for_completion(42, 1.0))
    await asyncio.sleep(0)
    assert not waiter.done()

    coordinator.signal_terminal(DurableRefreshSliceResult(42, DurableRefreshTerminal.SUCCESS))
    assert await waiter is True
    assert coordinator.queue_depth == 0
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_refresh_coordinator_waits_for_single_flight_completion() -> None:
    coordinator = EntityRefreshCoordinator()
    assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
    waiter = asyncio.create_task(coordinator.wait_for_completion(42, 1.0))
    await asyncio.sleep(0)
    assert not waiter.done()

    await coordinator.shutdown()
    assert await waiter is True
    assert coordinator.queue_depth == 0
    assert coordinator.enqueue(43) is RefreshEnqueueResult.REJECTED


@pytest.mark.asyncio
async def test_get_entity_info_waits_for_fresh_profile_after_cache_miss() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'User', 'Local User', 'local', NULL, 100)")
    limits = RefreshLimits(foreground_refresh_wait_seconds=0.1)
    service = _test_service(conn, limits=limits)
    service._deps = replace(service._deps, get_dialog_placement=lambda _entity_id: {})

    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)

    def status(_now: float) -> DemandStatus:
        return DemandStatus(release_at=0.0)

    async def run_slice(_budget: RpcAttemptBudget) -> DurableRefreshSliceResult:
        service._profiles.save_core({"id": 42, "type": "user", "name": "Fresh User"}, now=100)
        service._profiles.mark_pending(42, now=100)
        while (cursor := service._profiles.next_due_refresh(now=100)) is not None:
            status = "not_applicable" if cursor.next_section == "contact_overlap" else "fresh"
            assert service._profiles.commit_section(
                cursor,
                EntitySectionCommit({"name": "Fresh User"}, status=status),
                now=100,
            )
        return DurableRefreshSliceResult(42, DurableRefreshTerminal.SUCCESS)

    coordinator.bind_durable_executor(status, run_slice)
    request = asyncio.create_task(service.get_entity_info({"entity_id": 42}))
    await asyncio.sleep(0)
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    result = await request

    data = cast(dict[str, object], result["data"])
    sections = cast(dict[str, dict[str, object]], data["sections"])
    assert data["name"] == "Fresh User"
    assert sections["full_profile"]["status"] == "fresh"
    assert sections["common_chats"]["status"] == "fresh"
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_refresh_coordinator_reports_coalescing_and_queue_saturation() -> None:
    coordinator = EntityRefreshCoordinator(
        limits=RefreshLimits(max_concurrent_refreshes=1, max_queued_refreshes=1),
    )
    assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
    assert coordinator.enqueue(42) is RefreshEnqueueResult.COALESCED
    assert coordinator.enqueue(43) is RefreshEnqueueResult.REJECTED
    assert coordinator.queue_depth == 1
    coordinator.signal_terminal(DurableRefreshSliceResult(42, DurableRefreshTerminal.SUCCESS))
    assert coordinator.enqueue(43) is RefreshEnqueueResult.QUEUED
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_rejected_refresh_is_not_reported_as_queued() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    service = _test_service(
        conn,
        limits=RefreshLimits(max_concurrent_refreshes=1, max_queued_refreshes=1),
    )
    service._deps = replace(service._deps, get_dialog_placement=lambda _entity_id: {})
    assert service._refresh is not None
    assert service._refresh.enqueue(99) is RefreshEnqueueResult.QUEUED

    result = service._progressive_result(
        42,
        {"id": 42, "type": "user", "name": "Queued"},
        {section: {"status": "pending", "reason": "refresh_queued"} for section in PROFILE_SECTIONS},
        now=100,
    )

    sections = cast(dict[str, dict[str, object]], cast(dict[str, object], result["data"])["sections"])
    assert all(
        section["status"] == "unavailable" and section["reason"] == "refresh_rejected" for section in sections.values()
    )
    assert service._profiles.refresh_state(42, now=100) == {
        "status": "rejected",
        "retry_at": None,
        "reason": "refresh_rejected",
    }
    rows = cast(
        list[tuple[str | None]],
        conn.execute("SELECT reason FROM entity_detail_sections WHERE entity_id = 42").fetchall(),
    )
    assert {row[0] for row in rows} == {"refresh_rejected"}
    status_rows = cast(
        list[tuple[str | None]],
        conn.execute("SELECT status FROM entity_detail_sections WHERE entity_id = 42").fetchall(),
    )
    assert {row[0] for row in status_rows} == {"unavailable"}
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_progressive_miss_persists_rejected_admission() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    service = _test_service(
        conn,
        limits=RefreshLimits(max_concurrent_refreshes=1, max_queued_refreshes=1),
    )
    service._deps = replace(
        service._deps,
        get_peer_id=lambda value: int(value.id),
        get_dialog_placement=lambda _entity_id: {},
    )
    entity = SimpleNamespace(id=42, first_name="Rejected", username="rejected")

    async def resolve(_entity_id: int) -> tuple[object, None]:
        return entity, None

    service._resolve_entity = resolve  # type: ignore[method-assign]
    assert service._refresh is not None
    assert service._refresh.enqueue(99) is RefreshEnqueueResult.QUEUED

    result = await service._progressive_miss(42, now=100, started_at=100)
    sections = cast(dict[str, dict[str, object]], cast(dict[str, object], result["data"])["sections"])
    assert all(
        section["status"] == "unavailable" and section["reason"] == "refresh_rejected" for section in sections.values()
    )
    assert service._profiles.refresh_state(42, now=100) == {
        "status": "rejected",
        "retry_at": None,
        "reason": "refresh_rejected",
    }
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_self_profile_rejection_persists_unavailable_sections() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    service = _test_service(
        conn,
        limits=RefreshLimits(max_concurrent_refreshes=1, max_queued_refreshes=1),
    )
    service._deps = replace(
        service._deps,
        self_id=42,
        self_profile={"first_name": "Self", "username": "self"},
        get_dialog_placement=lambda _entity_id: {},
    )
    assert service._refresh is not None
    assert service._refresh.enqueue(99) is RefreshEnqueueResult.QUEUED

    result = service._self_snapshot_result(42, now=100, started_at=100)
    sections = cast(dict[str, dict[str, object]], cast(dict[str, object], result["data"])["sections"])
    assert all(
        section["status"] == "unavailable" and section["reason"] == "refresh_rejected" for section in sections.values()
    )
    rows = cast(
        list[tuple[str | None, str | None]],
        conn.execute("SELECT status, reason FROM entity_detail_sections WHERE entity_id = 42").fetchall(),
    )
    assert set(rows) == {("unavailable", "refresh_rejected")}
    await service.shutdown()
    conn.close()


def test_last_good_survives_refresh_failure() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'user', 'Good', 'good', NULL, 100)")
    conn.execute(
        "INSERT INTO entity_details VALUES (42, ?, 100)",
        (json.dumps({"schema": 1, "id": 42, "type": "user", "name": "Good", "common_chats": [{"id": 7}]}),),
    )
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None and cursor.next_section == "full_profile"
    assert repo.commit_section(cursor, EntitySectionCommit({}, status="fresh"), now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None and cursor.next_section == "common_chats"
    assert repo.commit_section(cursor, EntitySectionCommit({}, status="fresh"), now=100)
    repo.mark_refresh_failure(42, now=101, reason="timeout")
    stored = repo.read(42, now=101)
    assert stored is not None
    assert stored.detail["common_chats"] == [{"id": 7}]
    assert stored.observed_at == 100
    assert stored.sections["common_chats"]["status"] == "stale"
    assert stored.sections["common_chats"]["observed_at"] == 100
    conn.close()


def test_durable_section_failure_preserves_completed_sections_and_cursor() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'user', 'Known', 'known', NULL, 1)")
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    assert repo.commit_section(cursor, EntitySectionCommit({"about": "complete"}), now=101)
    failed_cursor = repo.next_due_refresh(now=101)
    assert failed_cursor is not None and failed_cursor.next_section == "common_chats"

    assert repo.mark_section_failure(failed_cursor, now=102, reason="timeout", retry_at=162)

    rows = dict(conn.execute("SELECT section, status FROM entity_detail_sections WHERE entity_id=42").fetchall())
    assert rows["full_profile"] == "fresh"
    assert rows["common_chats"] == "unavailable"
    assert rows["avatar_history"] == "pending"
    assert repo.next_due_refresh(now=161) is None
    resumed = repo.next_due_refresh(now=162)
    assert resumed is not None and resumed.next_section == "common_chats"
    conn.close()


@pytest.mark.asyncio
async def test_flood_wait_refresh_failure_signals_terminal_waiter_and_persists_retry() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'user', 'Good', 'good', NULL, 100)")
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    repo.mark_pending(42, now=100)
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(
        service._deps,
        client=cast(object, _FloodClient()),
        get_full_user_request=lambda **_kwargs: object(),
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
    waiter = asyncio.create_task(coordinator.wait_for_completion(42, 1.0))

    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    assert await waiter is True
    retry_at = cast(
        tuple[int | None] | None,
        conn.execute("SELECT retry_at FROM entity_profile_refresh_state WHERE entity_id = 42").fetchone(),
    )
    assert retry_at == (107,)
    stored = repo.read(42, now=100)
    assert stored is not None
    assert stored.detail["name"] == "Good"
    await service.shutdown()
    conn.close()


def _test_service(conn: sqlite3.Connection, *, limits: RefreshLimits) -> DaemonEntityInfoService:
    service = DaemonEntityInfoService(
        EntityInfoDeps(
            conn=conn,
            client=_UnusedClient(),
            dm_peer_ids=lambda: set(),
            self_id=None,
            self_profile=None,
            get_peer_id=lambda _value: 0,
            rid=lambda: "",
            logger=logging.getLogger(__name__),
            now_provider=lambda: 100.0,
            detail_ttl_seconds=300,
            slow_stage_seconds=1.0,
            get_common_chats_request=lambda **_kwargs: object(),
            get_full_user_request=lambda **_kwargs: object(),
            get_user_photos_request=lambda **_kwargs: object(),
            get_messages_search_request=lambda **_kwargs: object(),
            get_full_channel_request=lambda **_kwargs: object(),
            get_participants_request=lambda **_kwargs: object(),
            channel_participants_contacts_request=lambda **_kwargs: object(),
            get_full_chat_request=lambda **_kwargs: object(),
            input_messages_filter_chat_photos=object,
            message_action_chat_edit_photo=object,
            chat_reactions_all=object,
            chat_reactions_some=object,
            chat_reactions_none=object,
            channel_type=object,
            chat_type=object,
            refresh_limits=limits,
        )
    )
    service.bind_demand_sink(MagicMock())
    return service


@pytest.mark.asyncio
async def test_durable_profile_adapter_resumes_one_section_per_actual_attempt() -> None:
    class BudgetedClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.scopes: list[tuple[DemandKind | None, AcquisitionKind | None]] = []

        async def __call__(self, request: object) -> object:
            scope = current_rpc_scope()
            assert scope.attempt_budget is not None
            scope.attempt_budget.debit()
            self.scopes.append((scope.demand_kind, scope.acquisition_kind))
            kind = cast(tuple[str, object], request)[0]
            self.calls.append(kind)
            if kind == "full_user":
                return SimpleNamespace(
                    full_user=SimpleNamespace(about="fresh", blocked=False, folder_id=None),
                    users=[],
                    chats=[],
                )
            if kind == "common_chats":
                return SimpleNamespace(chats=[SimpleNamespace(id=7, title="Shared")])
            raise AssertionError(f"unexpected request: {kind}")

        async def get_entity(self, _entity_id: int) -> object:
            raise AssertionError("stored core must avoid a resolve acquisition")

        async def get_messages(self, entity: object, ids: list[int]) -> object:
            raise AssertionError((entity, ids))

        def iter_participants(self, peer: object, limit: int = 0) -> AsyncIterator[object]:
            raise AssertionError((peer, limit))

        def iter_dialogs(self) -> AsyncIterator[object]:
            raise AssertionError("dialog traversal is not part of a profile section")

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'user', 'Known', 'known', NULL, 1)")
    repository = EntityProfileRepository(conn, section_ttl_seconds=300)
    repository.mark_pending(42, now=100)
    client = BudgetedClient()

    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(
        service._deps,
        client=cast(object, client),
        get_peer_id=lambda value: int(value.id),
        get_full_user_request=lambda **_kwargs: ("full_user", _kwargs),
        get_common_chats_request=lambda **_kwargs: ("common_chats", _kwargs),
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)

    first_budget = RpcAttemptBudget(limit=1)
    await adapter.run_slice(first_budget)

    assert first_budget.attempts == 1
    assert client.calls == ["full_user"]
    assert conn.execute(
        "SELECT next_section, acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("common_chats", 0)
    assert conn.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=42 AND section='full_profile'"
    ).fetchone() == ("fresh",)
    await service.shutdown()

    restarted = _test_service(conn, limits=RefreshLimits())
    restarted._deps = replace(
        restarted._deps,
        client=cast(object, client),
        get_peer_id=lambda value: int(value.id),
        get_full_user_request=lambda **_kwargs: ("full_user", _kwargs),
        get_common_chats_request=lambda **_kwargs: ("common_chats", _kwargs),
    )
    restarted_coordinator = restarted.refresh_coordinator
    assert restarted_coordinator is not None
    restarted_adapter = EntityProfileDemandAdapter(restarted_coordinator)
    second_budget = RpcAttemptBudget(limit=1)
    await restarted_adapter.run_slice(second_budget)

    assert second_budget.attempts == 1
    assert client.calls == ["full_user", "common_chats"]
    assert conn.execute(
        "SELECT next_section, acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("contact_overlap", 0)
    local_budget = RpcAttemptBudget(limit=1)
    await restarted_adapter.run_slice(local_budget)
    assert local_budget.attempts == 0
    assert conn.execute("SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        "avatar_history",
    )
    assert client.scopes == [
        (DemandKind.ENTITY_PROFILE_REFRESH, AcquisitionKind.ENTITY_LOOKUP),
        (DemandKind.ENTITY_PROFILE_REFRESH, AcquisitionKind.ENTITY_LOOKUP),
    ]
    await restarted.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_durable_profile_adapter_checkpoints_core_resolution_before_section() -> None:
    class ResolvingClient(_UnusedClient):
        async def get_entity(self, entity_id: int) -> object:
            scope = current_rpc_scope()
            assert scope.attempt_budget is not None
            scope.attempt_budget.debit()
            return User(id=entity_id, first_name="Resolved", username="resolved")

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute(
        "INSERT INTO entity_profile_refresh_state("
        "entity_id,status,retry_at,reason,updated_at,next_section,acquisition_cursor) "
        "VALUES (42,'pending',NULL,'refresh_queued',100,'full_profile',0)"
    )
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(
        service._deps,
        client=ResolvingClient(),
        get_peer_id=lambda value: int(value.id),
        get_dialog_placement=lambda _entity_id: {},
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    budget = RpcAttemptBudget(limit=1)

    await EntityProfileDemandAdapter(coordinator).run_slice(budget)

    assert budget.attempts == 1
    assert conn.execute("SELECT type, name FROM entities WHERE id=42").fetchone() == ("user", "Resolved")
    assert conn.execute(
        "SELECT next_section, acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("full_profile", 1)
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_durable_profile_budget_exhaustion_leaves_core_cursor_ready() -> None:
    class ExhaustedClient(_UnusedClient):
        async def get_entity(self, entity_id: int) -> object:
            del entity_id
            raise RpcAttemptBudgetExhaustedError("slice complete")

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute(
        "INSERT INTO entity_profile_refresh_state("
        "entity_id,status,retry_at,reason,updated_at,next_section,acquisition_cursor) "
        "VALUES (42,'pending',NULL,'refresh_queued',100,'full_profile',0)"
    )
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(service._deps, client=ExhaustedClient())
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
    waiter = asyncio.create_task(coordinator.wait_for_completion(42, 1.0))

    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    assert not waiter.done()
    assert conn.execute(
        "SELECT status, retry_at, next_section, acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("pending", None, "full_profile", 0)
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone() == (0,)
    await service.shutdown()
    assert await waiter is True
    conn.close()


@pytest.mark.asyncio
async def test_entity_info_foreground_entrypoint_sets_rpc_source() -> None:
    conn = sqlite3.connect(":memory:")
    service = _test_service(conn, limits=RefreshLimits())
    observed: list[tuple[TelegramRpcSource, DemandKind, AcquisitionKind | None]] = []

    async def implementation(_req: object) -> dict[str, object]:
        scope = current_rpc_scope()
        assert scope.demand_kind is not None
        observed.append((scope.source, scope.demand_kind, scope.acquisition_kind))
        return {"ok": True}

    service._get_entity_info = implementation  # type: ignore[method-assign]
    assert await service.get_entity_info({"entity_id": 42}) == {"ok": True}
    assert observed == [
        (
            TelegramRpcSource.ENTITY_INFO_FOREGROUND,
            DemandKind.FOREGROUND_ENTITY_FACTS,
            AcquisitionKind.ENTITY_LOOKUP,
        )
    ]
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_entity_profile_adapter_sets_bounded_rpc_scope() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'user', 'Known', 'known', NULL, 1)")
    EntityProfileRepository(conn, section_ttl_seconds=300).mark_pending(42, now=100)
    limits = RefreshLimits(foreground_resolve_seconds=0.01, per_rpc_seconds=0.02, whole_refresh_seconds=0.05)
    service = _test_service(conn, limits=limits)
    observed: list[
        tuple[TelegramRpcSource, float | None, asyncio.Task[object] | None, DemandKind, AcquisitionKind]
    ] = []

    class ScopedClient(_UnusedClient):
        async def __call__(self, _request: object) -> object:
            scope = current_rpc_scope()
            assert scope.attempt_budget is not None
            scope.attempt_budget.debit()
            assert scope.demand_kind is not None
            assert scope.acquisition_kind is not None
            observed.append((scope.source, scope.deadline, scope.owner_task, scope.demand_kind, scope.acquisition_kind))
            return SimpleNamespace(
                full_user=SimpleNamespace(about="fresh", blocked=False, folder_id=None), users=[], chats=[]
            )

    service._deps = replace(
        service._deps,
        client=ScopedClient(),
        get_full_user_request=lambda **_kwargs: object(),
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    source, deadline, owner_task, demand_kind, acquisition_kind = observed[0]
    assert source is TelegramRpcSource.ENTITY_INFO_REFRESH
    assert deadline is not None
    assert owner_task is asyncio.current_task()
    assert demand_kind is DemandKind.ENTITY_PROFILE_REFRESH
    assert acquisition_kind is AcquisitionKind.ENTITY_LOOKUP
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_refresh_resolution_preserves_success_and_failure_semantics() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute(
        "INSERT INTO entity_profile_refresh_state(entity_id,status,retry_at,reason,updated_at) "
        "VALUES (42,'pending',NULL,'refresh_queued',100)"
    )
    service = _test_service(conn, limits=RefreshLimits())
    entity = SimpleNamespace(id=42)

    async def resolved(_entity_id: int) -> tuple[object, None]:
        return entity, None

    service._resolve_entity = resolved  # type: ignore[method-assign]
    cursor = service._profiles.next_due_refresh(now=100)
    assert cursor is not None
    assert await service._acquire_durable_refresh_core(cursor, now=100) is None
    assert conn.execute(
        "SELECT acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == (1,)

    async def throttled(_entity_id: int) -> tuple[None, dict[str, object]]:
        return None, {"_retry_after_seconds": 7}

    service._resolve_entity = throttled  # type: ignore[method-assign]
    cursor = service._profiles.next_due_refresh(now=100)
    assert cursor is not None
    assert await service._acquire_durable_refresh_core(cursor, now=100) is DurableRefreshTerminal.FAILURE
    assert service._profiles.refresh_state(42, now=100) == {
        "status": "failed",
        "retry_at": 107,
        "reason": "flood_wait",
    }
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_core_projection_uses_full_user_name_and_local_placement() -> None:
    conn = sqlite3.connect(":memory:")
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(
        service._deps,
        get_peer_id=lambda value: int(value.id),
        get_dialog_placement=lambda entity_id: {"entity_id": entity_id, "folders": []},
    )
    entity = User(id=42, first_name="First", last_name="Last", username="person")

    assert service._core_from_entity(entity) == {
        "id": 42,
        "type": "user",
        "name": "First Last",
        "username": "person",
        "dialog_placement": {"entity_id": 42, "folders": []},
    }
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_expected_enrichment_timeout_does_not_emit_traceback(caplog: pytest.LogCaptureFixture) -> None:
    class TimeoutClient:
        async def __call__(self, _request: object) -> object:
            raise TimeoutError

    conn = sqlite3.connect(":memory:")
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(service._deps, client=TimeoutClient())

    with caplog.at_level(logging.WARNING):
        assert await service._collect_common_chats(42) == []
    assert caplog.records[-1].exc_info is False
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_cached_core_path_is_local_and_fast() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'user', 'Cached', 'cached', NULL, 100)")
    service = _test_service(conn, limits=RefreshLimits())
    result = await service.get_entity_info({"entity_id": 42})
    await service.shutdown()
    assert result["ok"] is True
    data = cast(dict[str, object], result["data"])
    assert data["name"] == "Cached"
    assert data["completeness"] == "partial"
    conn.close()


@pytest.mark.asyncio
async def test_unknown_core_timeout_enqueues_background_resolution() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    calls = 0
    entity = SimpleNamespace(id=42, first_name="Known", username="known")

    async def resolve(_entity_id: int) -> tuple[object, None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)
        return entity, None

    service = _test_service(conn, limits=RefreshLimits(0.01, 0.02, 0.05, 1))
    service._deps = replace(service._deps, get_peer_id=lambda value: int(value.id))
    service._resolve_entity = resolve  # type: ignore[method-assign]
    pending = await service._progressive_miss(42, now=100, started_at=100)
    assert pending["error"] == "entity_info_pending"
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    await service.shutdown()
    assert conn.execute("SELECT type, name FROM entities WHERE id=42").fetchone() == ("user", "Known")
    conn.close()


@pytest.mark.asyncio
async def test_unknown_core_flood_wait_is_durable_without_fake_entity_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    service = _test_service(conn, limits=RefreshLimits(0.01, 0.02, 0.05, 1))
    calls = 0

    async def resolve(_entity_id: int) -> tuple[object, None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)
        raise TelegramRpcThrottled(retry_after_seconds=7)

    service._resolve_entity = resolve  # type: ignore[method-assign]
    pending = await service._progressive_miss(42, now=100, started_at=100)
    assert pending["error"] == "entity_info_pending"
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    await service.shutdown()
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone() == (0,)
    state = service._profiles.refresh_state(42, now=100)
    assert state == {"status": "failed", "retry_at": 107, "reason": "flood_wait"}
    conn.close()


def test_progressive_projection_schema_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)")
    _apply_migration_57(conn, 56)
    _apply_migration_57(conn, 57)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_detail_sections'"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_profile_refresh_state'"
    ).fetchone() == (1,)
    conn.close()


def test_progressive_projection_schema_is_present_on_fresh_database(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_detail_sections'"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_profile_refresh_state'"
    ).fetchone() == (1,)
    conn.close()


def test_progressive_projection_schema_upgrades_from_v56(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE entity_detail_sections")
    conn.execute("DELETE FROM schema_version WHERE version = 57")
    conn.commit()
    _apply_migrations(conn)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_detail_sections'"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_profile_refresh_state'"
    ).fetchone() == (1,)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (61,)
    conn.close()


def test_progressive_payload_validates_against_real_mcp_schema() -> None:
    payload = _entity_structured_content(
        args=GetEntityInfo(exact_entity_id=42),
        data={
            "type": "user",
            "name": "Known",
            "completeness": "partial",
            "sections": {
                "full_profile": {"status": "stale", "observed_at": 100},
                "common_chats": {"status": "pending", "reason": "refresh_queued"},
                "avatar_history": {"status": "unavailable", "reason": "timeout"},
            },
            "dialog_placement": {"archived": False, "folders": []},
        },
        entity_id=42,
        display_name="Known",
        resolution="exact_id",
    )
    validate(instance=payload, schema=GET_ENTITY_INFO_OUTPUT_SCHEMA)
