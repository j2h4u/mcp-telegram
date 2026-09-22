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
from telethon.errors import PeerIdInvalidError  # type: ignore[import-untyped]
from telethon.tl.types import User  # type: ignore[import-untyped]

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService, EntityInfoDeps
from mcp_telegram.entity_profile.contracts import (
    PROFILE_SECTIONS,
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChatAvatarHistoryObservation,
    CommonChatsObservation,
    GroupReference,
    ObservationBoundary,
    PersonalChannelPost,
    PersonalChannelReference,
    ProjectionStatus,
    TargetKind,
    UserAvatarHistoryObservation,
    UserProfileObservation,
)
from mcp_telegram.entity_profile.full_user_normalization import normalize_full_user_response
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
from mcp_telegram.models import DialogType
from mcp_telegram.sync_db import _CURRENT_SCHEMA_VERSION, _apply_migration_57, _apply_migrations, ensure_sync_schema
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, current_rpc_scope
from mcp_telegram.tools.entity_info import GET_ENTITY_INFO_OUTPUT_SCHEMA, GetEntityInfo, _entity_structured_content
from tests.helpers import (
    ClientCommonChatsPort,
    FakeChannelProfilePort,
    LoudChannelProfilePort,
    LoudChatAvatarHistoryPort,
    LoudCommonChatsPort,
    LoudGroupProfilePort,
    LoudUserAvatarHistoryPort,
)


class _UserProfilePort:
    def __init__(self, client: object) -> None:
        self.client = client

    def get_user_reference(self, user_id: int, *, is_self: bool = False):
        from mcp_telegram.entity_profile.contracts import UserReference

        return UserReference(user_id, 0, is_self=is_self)

    async def fetch_user_profile(self, user_id: int, target_kind: TargetKind) -> UserProfileObservation:
        response = await self.client(("full_user", {"id": user_id}))  # type: ignore[operator]
        if not hasattr(response, "users") or not response.users:
            response = SimpleNamespace(
                full_user=getattr(response, "full_user", SimpleNamespace()),
                users=[SimpleNamespace(id=user_id, bot=target_kind is TargetKind.BOT)],
                chats=getattr(response, "chats", []),
            )
        return normalize_full_user_response(
            response,
            target_id=user_id,
            target_kind=target_kind,
            observation=ObservationBoundary(100.0, 100.0),
        )

    async def fetch_personal_channel_post(
        self, reference: PersonalChannelReference, message_id: int
    ) -> PersonalChannelPost | None:
        raise AssertionError(f"personal channel post fetch is not part of progressive tests: {reference}/{message_id}")


class _UnusedClient:
    async def get_entity(self, entity_id: int) -> object:
        raise AssertionError("client should not be called in this test")

    async def get_messages(self, entity: object, ids: list[int]) -> object:
        raise AssertionError("client should not be called in this test")

    async def __call__(self, request: object) -> object:
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


def _advance_fixture_cursor(repo: EntityProfileRepository, entity_id: int, next_cursor: int, now: int) -> None:
    """Move a hand-built fixture past core acquisition without testing a writer API."""
    with repo._conn:
        repo._conn.execute(
            "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, "
            "reason='refresh_in_progress', updated_at=?, acquisition_cursor=? WHERE entity_id=?",
            (now, next_cursor, entity_id),
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
    assert await waiter is False
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
    assert repo.commit_section(
        cursor,
        EntitySectionCommit({}, status="unavailable", reason="not_an_admin", payload=None),
        now=100,
    )
    repo.mark_refresh_failure(42, now=101, reason="timeout")
    stored = repo.read(42, now=101)
    assert stored is not None
    assert stored.detail["common_chats"] == [{"id": 7}]
    assert stored.observed_at == 100
    assert stored.sections["common_chats"]["status"] == "unavailable"
    assert stored.sections["common_chats"]["reason"] == "timeout"
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
        user_profile_port=_UserProfilePort(_FloodClient()),
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
            user_profile_port=_UserProfilePort(_UnusedClient()),
            group_profile_port=LoudGroupProfilePort(),
            channel_profile_port=LoudChannelProfilePort(),
            channel_reference_provider=LoudChannelProfilePort(),
            common_chats_port=LoudCommonChatsPort(),
            user_avatar_history_port=LoudUserAvatarHistoryPort(),
            chat_avatar_history_port=LoudChatAvatarHistoryPort(),
            refresh_limits=limits,
        )
    )
    service.bind_demand_sink(MagicMock())
    return service


@pytest.mark.asyncio
async def test_unavailable_common_and_avatar_commits_preserve_existing_projection() -> None:
    class DeniedCommonChatsPort:
        async def fetch_common_chats(self, _reference: object) -> CommonChatsObservation:
            return CommonChatsObservation(42, (), 0, ProjectionStatus.UNAVAILABLE, "not_an_admin", 100, 100)

    class DeniedUserAvatarPort:
        async def fetch_user_avatar_history(self, _reference: object) -> UserAvatarHistoryObservation:
            return UserAvatarHistoryObservation(42, (), 0, ProjectionStatus.UNAVAILABLE, "access_lost", 100, 100)

    class MissingChatAvatarPort:
        def get_chat_avatar_reference(self, _entity_id: int) -> None:
            return None

        async def fetch_chat_avatar_history(self, _reference: object) -> ChatAvatarHistoryObservation:
            raise AssertionError("reference miss must not fetch")

    conn = sqlite3.connect(":memory:")
    _sections_schema(conn)
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(
        service._deps,
        common_chats_port=DeniedCommonChatsPort(),
        user_avatar_history_port=DeniedUserAvatarPort(),
        chat_avatar_history_port=MissingChatAvatarPort(),
    )
    try:
        common = await service._acquire_common_chats(42)
        user_avatar = await service._acquire_user_avatar_history(42)
        chat_avatar = await service._acquire_chat_avatar_history(-123)
        assert common.detail_patch == {} and common.payload is None
        assert common.status == "unavailable" and common.reason == "not_an_admin"
        assert user_avatar.detail_patch == {} and user_avatar.payload is None
        assert user_avatar.status == "unavailable" and user_avatar.reason == "access_lost"
        assert chat_avatar.detail_patch == {} and chat_avatar.payload is None
        assert chat_avatar.status == "unavailable" and chat_avatar.reason == "chat_reference_unavailable"
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_chat_avatar_observation_reference_mismatch_is_rejected() -> None:
    class WrongChatAvatarPort:
        def get_chat_avatar_reference(self, _entity_id: int) -> GroupReference:
            return GroupReference(-123)

        async def fetch_chat_avatar_history(self, _reference: object) -> ChatAvatarHistoryObservation:
            return ChatAvatarHistoryObservation(GroupReference(-456), (), 0, ProjectionStatus.USABLE, None, 100, 100)

    conn = sqlite3.connect(":memory:")
    _sections_schema(conn)
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(service._deps, chat_avatar_history_port=WrongChatAvatarPort())
    try:
        with pytest.raises(ValueError, match="reference"):
            await service._acquire_chat_avatar_history(-123)
    finally:
        await service.shutdown()
        conn.close()


def _channel_profile_service(
    *,
    channel_id: int,
    linked_chat_id: int | None,
    participants_count: int | None = 10,
    overlap: ChannelContactOverlapObservation | None = None,
) -> tuple[DaemonEntityInfoService, sqlite3.Connection, FakeChannelProfilePort]:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE dialogs (dialog_id INTEGER PRIMARY KEY, linked_chat_id INTEGER, linked_chat_resolved_at INTEGER)"
    )
    conn.execute(
        "INSERT INTO dialogs VALUES (?, ?, ?)",
        (channel_id, linked_chat_id, 100 if linked_chat_id is not None else None),
    )
    profile = ChannelProfileObservation(
        channel_id=channel_id,
        about="profile",
        participants_count=participants_count,
        linked_chat_id=-100777,
        pinned_msg_id=None,
        slow_mode_seconds=None,
        available_reactions={"kind": "none", "emojis": []},
        current_photo=None,
        observation_started_at=100,
        observation_completed_at=100,
    )
    overlap_observation = overlap or ChannelContactOverlapObservation(
        channel_id=channel_id,
        contact_ids=(),
        status=ProjectionStatus.PARTIAL,
        reason="bounded_contacts_page",
        observation_started_at=100,
        observation_completed_at=100,
    )
    port = FakeChannelProfilePort(profile, overlap_observation)
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(
        service._deps,
        channel_profile_port=port,
        channel_reference_provider=port,
        get_dialog_placement=lambda _entity_id: {},
    )
    service._profiles.save_core(
        {"id": channel_id, "type": DialogType.CHANNEL.value, "name": "Channel"},
        now=100,
    )
    service._profiles.mark_pending(channel_id, now=100)
    return service, conn, port


@pytest.mark.asyncio
async def test_channel_profile_commit_advances_once_and_overlays_canonical_link_without_persisting_it() -> None:
    service, conn, port = _channel_profile_service(channel_id=-10042, linked_chat_id=-10099)
    try:
        cursor = service._profiles.next_due_refresh(now=100)
        assert cursor is not None
        _advance_fixture_cursor(service._profiles, -10042, 1, 100)
        cursor = service._profiles.next_due_refresh(now=100)
        assert cursor is not None and cursor.next_section == "full_profile"

        commit = await service._acquire_channel_full_profile(-10042, DialogType.CHANNEL)
        assert service._profiles.commit_section(cursor, commit, now=100)
        assert port.profile_calls == [-1000000010042]
        stored = service._profiles.read(-10042, now=100)
        assert stored is not None
        assert "linked_chat_id" not in stored.detail
        result = service._progressive_result(-10042, stored.detail, stored.sections, now=100, admit_refresh=False)
        data = cast(dict[str, object], result["data"])
        assert data["linked_chat_id"] == -10099
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_channel_progressive_link_overlay_replaces_stale_and_unresolved_with_none() -> None:
    service, conn, _port = _channel_profile_service(channel_id=-10043, linked_chat_id=None)
    try:
        stale = {"id": -10043, "type": DialogType.CHANNEL.value, "linked_chat_id": -10088}
        sections: dict[str, dict[str, object]] = {section: {"status": "fresh"} for section in PROFILE_SECTIONS}
        result = service._progressive_result(-10043, stale, sections, now=100, admit_refresh=False)
        assert cast(dict[str, object], result["data"])["linked_chat_id"] is None
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_channel_profile_unavailable_commits_adapter_reason() -> None:
    service, conn, _port = _channel_profile_service(
        channel_id=-10045,
        linked_chat_id=None,
    )
    try:
        _port.profile = replace(_port.profile, status=ProjectionStatus.UNAVAILABLE, reason="access_lost")
        cursor = service._profiles.next_due_refresh(now=100)
        assert cursor is not None
        _advance_fixture_cursor(service._profiles, -10045, 1, 100)
        cursor = service._profiles.next_due_refresh(now=100)
        assert cursor is not None
        commit = await service._acquire_channel_full_profile(-10045, DialogType.CHANNEL)
        assert commit.status == "unavailable"
        assert commit.reason == "access_lost"
        assert service._profiles.commit_section(cursor, commit, now=100)
        section = service._profiles.read(-10045, now=100)
        assert section is not None
        assert section.sections["full_profile"]["status"] == "unavailable"
        assert section.sections["full_profile"]["reason"] == "access_lost"
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_type", "expected_reason"),
    (
        (DialogType.SUPERGROUP, "hidden_by_admin"),
        (DialogType.FORUM, "hidden_by_admin"),
        (DialogType.CHANNEL, "not_an_admin"),
    ),
)
async def test_progressive_contact_permission_reason_depends_on_entity_type(
    entity_type: DialogType,
    expected_reason: str,
) -> None:
    service, conn, _port = _channel_profile_service(
        channel_id=-10046,
        linked_chat_id=None,
        overlap=ChannelContactOverlapObservation(
            channel_id=-10046,
            contact_ids=None,
            status=ProjectionStatus.UNAVAILABLE,
            reason="not_an_admin",
            observation_started_at=100,
            observation_completed_at=100,
        ),
    )
    try:
        commit = await service._acquire_channel_contact_overlap(-10046, entity_type)
        assert commit.status == "unavailable"
        assert commit.reason == expected_reason
        assert commit.detail_patch["contacts_reason"] == expected_reason
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_supergroup_profile_persists_reverse_link_fact_only() -> None:
    service, conn, port = _channel_profile_service(channel_id=-10044, linked_chat_id=-10055)
    try:
        cursor = service._profiles.next_due_refresh(now=100)
        assert cursor is not None
        _advance_fixture_cursor(service._profiles, -10044, 1, 100)
        cursor = service._profiles.next_due_refresh(now=100)
        assert cursor is not None
        commit = await service._acquire_channel_full_profile(-10044, DialogType.SUPERGROUP)
        assert port.profile_calls == [-1000000010044]
        assert commit.detail_patch["linked_broadcast_id"] == -100777
        assert "linked_chat_id" not in commit.detail_patch
        assert service._profiles.commit_section(cursor, commit, now=100)
        stored = service._profiles.read(-10044, now=100)
        assert stored is not None
        assert stored.detail["linked_broadcast_id"] == -100777
        assert "linked_chat_id" not in stored.detail
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_durable_channel_reference_miss_commits_unavailable_without_rpc_attempt() -> None:
    class MissingReferenceProvider:
        def __init__(self) -> None:
            self.calls = 0

        def get_channel_reference(self, _channel_id: int) -> None:
            self.calls += 1
            return

    channel_id = -1000000000047
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
        "INSERT INTO entities VALUES (?, 'channel', 'Channel', NULL, NULL, 100)",
        (channel_id,),
    )
    conn.execute(
        "INSERT INTO entity_profile_refresh_state(entity_id,status,retry_at,reason,updated_at,next_section,acquisition_cursor) "
        "VALUES (?, 'pending', NULL, 'refresh_queued', 100, 'full_profile', 0)",
        (channel_id,),
    )
    provider = MissingReferenceProvider()
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(
        service._deps,
        channel_reference_provider=provider,
        channel_profile_port=LoudChannelProfilePort(),
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    budget = RpcAttemptBudget(limit=1)

    await EntityProfileDemandAdapter(coordinator).run_slice(budget)

    assert provider.calls == 1
    assert budget.attempts == 0
    assert conn.execute(
        "SELECT status, reason, next_section FROM entity_profile_refresh_state WHERE entity_id=?",
        (channel_id,),
    ).fetchone() == ("pending", "refresh_queued", "common_chats")
    assert conn.execute(
        "SELECT status, reason FROM entity_detail_sections WHERE entity_id=? AND section='full_profile'",
        (channel_id,),
    ).fetchone() == ("unavailable", "channel_reference_unavailable")
    await service.shutdown()
    conn.close()


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
        user_profile_port=_UserProfilePort(client),
        common_chats_port=ClientCommonChatsPort(client),
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
        user_profile_port=_UserProfilePort(client),
        common_chats_port=ClientCommonChatsPort(client),
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
    assert await waiter is False
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
        user_profile_port=_UserProfilePort(ScopedClient()),
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
    service._deps = replace(
        service._deps,
        client=TimeoutClient(),
        common_chats_port=ClientCommonChatsPort(TimeoutClient()),
    )

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
async def test_partial_detail_uses_canonical_identity_and_skips_core_resolution() -> None:  # noqa: PLR0915
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'user', 'Canonical', 'canonical', NULL, 100)")
    conn.execute(
        "INSERT INTO entity_details VALUES (42, ?, 90)",
        (json.dumps({"schema": 1, "id": 999, "type": "unknown", "name": "Enrichment", "about": "legacy"}),),
    )
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    _advance_fixture_cursor(repo, 42, 7, 100)
    service = _test_service(conn, limits=RefreshLimits())
    resolved_calls: list[int] = []

    async def resolve_entity(entity_id: int) -> tuple[object | None, dict[str, object] | None]:
        resolved_calls.append(entity_id)
        raise AssertionError("known canonical entities must not resolve their core again")

    async def acquire_section(_cursor: object, _entity_type: DialogType) -> EntitySectionCommit:
        return EntitySectionCommit({"about": "fresh"}, payload={"about": "fresh"})

    service._resolve_entity = resolve_entity  # type: ignore[method-assign]
    service._acquire_profile_section = acquire_section  # type: ignore[method-assign]
    try:
        stored = repo.read(42, now=100)
        assert stored is not None
        assert stored.detail["id"] == 42
        assert stored.detail["type"] == "user"
        assert stored.detail["name"] == "Canonical"
        assert stored.detail["username"] == "canonical"

        coordinator = service.refresh_coordinator
        assert coordinator is not None
        assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

        assert resolved_calls == []
        cursor = repo.next_due_refresh(now=100)
        assert cursor is not None
        assert cursor.next_section == "common_chats"
        assert cursor.acquisition_cursor == 0
        stored = repo.read(42, now=100)
        assert stored is not None
        assert stored.detail["type"] == "user"
        assert stored.detail["name"] == "Canonical"
        assert stored.detail["about"] == "fresh"
        written_value = cast(
            object,
            json.loads(
                cast(str, conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone()[0])
            ),
        )
        assert isinstance(written_value, dict)
        written = cast(dict[str, object], written_value)
        assert written["type"] == "user"
        assert written["name"] == "Canonical"
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_core_acquisition_becomes_known_across_reopen_without_cursor_retry(tmp_path: Path) -> None:
    path = tmp_path / "partial-core.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    conn.execute("INSERT INTO entities VALUES (42, 'unknown', 'Legacy', NULL, NULL, 90)")
    conn.execute(
        "INSERT INTO entity_details VALUES (42, ?, 90)",
        (json.dumps({"schema": 1, "about": "legacy"}),),
    )
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    _advance_fixture_cursor(repo, 42, 7, 100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None and cursor.acquisition_cursor == 7

    service = _test_service(conn, limits=RefreshLimits())

    async def resolve_entity(_entity_id: int) -> tuple[object, None]:
        return SimpleNamespace(id=42), None

    service._resolve_entity = resolve_entity  # type: ignore[method-assign]
    service._core_from_entity = lambda _entity: {
        "id": 42,
        "type": "user",
        "name": "Resolved",
        "username": "resolved",
    }  # type: ignore[method-assign]
    try:
        assert await service._acquire_durable_refresh_core(cursor, now=100) is None
        assert conn.execute(
            "SELECT acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone() == (8,)
    finally:
        await service.shutdown()
        conn.close()

    reopened = sqlite3.connect(path)
    try:
        reopened_repo = EntityProfileRepository(reopened, section_ttl_seconds=300)
        stored = reopened_repo.read(42, now=100)
        assert stored is not None
        assert stored.detail["type"] == "user"
        assert stored.detail["name"] == "Resolved"
        cursor = reopened_repo.next_due_refresh(now=100)
        assert cursor is not None
        assert cursor.acquisition_cursor == 8
        assert cursor.next_section == "full_profile"
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_production_schema_core_write_syncs_revision_before_next_section_and_reopen(tmp_path: Path) -> None:  # noqa: PLR0915
    path = tmp_path / "production-core.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO entities(id, type, name, updated_at) VALUES (42, 'unknown', 'Legacy', 90)")
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at) VALUES (42, ?, 90)",
        (json.dumps({"schema": 1, "about": "legacy"}),),
    )
    conn.commit()
    service = _test_service(conn, limits=RefreshLimits())
    service._profiles.mark_pending(42, now=100)
    core_calls = 0

    async def resolve_entity(_entity_id: int) -> tuple[object, None]:
        nonlocal core_calls
        core_calls += 1
        scope = current_rpc_scope()
        assert scope.attempt_budget is not None
        scope.attempt_budget.debit()
        return SimpleNamespace(id=42), None

    async def acquire_section(_cursor: object, _entity_type: DialogType) -> EntitySectionCommit:
        return EntitySectionCommit({"about": "fresh"}, payload={"about": "fresh"})

    service._resolve_entity = resolve_entity  # type: ignore[method-assign]
    service._core_from_entity = lambda _entity: {
        "id": 42,
        "type": "user",
        "name": "Resolved",
        "username": "resolved",
    }  # type: ignore[method-assign]
    service._acquire_profile_section = acquire_section  # type: ignore[method-assign]
    try:
        coordinator = service.refresh_coordinator
        assert coordinator is not None
        assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
        assert core_calls == 1
        assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (1,)
        assert conn.execute(
            "SELECT acquisition_cursor, profile_revision FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone() == (1, 1)
        coordinator.signal_terminal(DurableRefreshSliceResult(42, DurableRefreshTerminal.SUCCESS))

        assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
        assert core_calls == 1
        cursor = service._profiles.next_due_refresh(now=100)
        assert cursor is not None
        assert cursor.next_section == "common_chats"
        assert cursor.profile_revision == 2
        conn.close()
    finally:
        await service.shutdown()

    reopened = sqlite3.connect(path)
    reopened_service = _test_service(reopened, limits=RefreshLimits())

    async def forbidden_resolve(_entity_id: int) -> tuple[object, None]:
        raise AssertionError("reopened known entity must not resolve core again")

    reopened_service._resolve_entity = forbidden_resolve  # type: ignore[method-assign]
    reopened_service._acquire_profile_section = acquire_section  # type: ignore[method-assign]
    try:
        cursor = reopened_service._profiles.next_due_refresh(now=100)
        assert cursor is not None
        assert cursor.next_section == "common_chats"
        assert cursor.profile_revision == 2
        coordinator = reopened_service.refresh_coordinator
        assert coordinator is not None
        assert coordinator.enqueue(42) is RefreshEnqueueResult.QUEUED
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
        cursor = reopened_service._profiles.next_due_refresh(now=100)
        assert cursor is not None
        assert cursor.next_section == "contact_overlap"
        assert cursor.profile_revision == 3
    finally:
        await reopened_service.shutdown()
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", ("user", "bot"))
@pytest.mark.parametrize("avatar_status", ("pending", "unavailable"))
async def test_progressive_cached_avatar_projects_persisted_current_photo_without_full_profile_rpc(
    entity_type: str, avatar_status: str
) -> None:
    class CountingClient(_UnusedClient):
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, _request: object) -> object:
            self.calls += 1
            raise AssertionError("cached projection must not fetch full profile")

    entity_id = 42 if entity_type == "user" else 43
    client = CountingClient()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    detail = {
        "schema": 1,
        "id": entity_id,
        "type": entity_type,
        "name": "Cached profile",
        "avatar_history": [{"photo_id": 7, "date": None}],
        "avatar_count": 1,
    }
    conn.execute("INSERT INTO entities VALUES (?, ?, ?, NULL, NULL, 100)", (entity_id, entity_type, "Cached profile"))
    conn.execute("INSERT INTO entity_details VALUES (?, ?, 100)", (entity_id, json.dumps(detail)))
    section_rows = {
        "full_profile": ("fresh", 100, None, {"current_photo": {"photo_id": 99, "date": None}}),
        "common_chats": ("fresh", 100, None, []),
        "contact_overlap": ("not_applicable", None, None, None),
        "avatar_history": (
            avatar_status,
            100,
            "history_pending" if avatar_status == "pending" else "access_lost",
            detail["avatar_history"],
        ),
        "personal_channel": ("fresh", 100, None, None),
    }
    conn.executemany(
        "INSERT INTO entity_detail_sections(entity_id, section, status, observed_at, reason, payload_json, retry_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (
            (entity_id, section, status, observed_at, reason, json.dumps(payload) if payload is not None else None)
            for section, (status, observed_at, reason, payload) in section_rows.items()
        ),
    )
    service = _test_service(conn, limits=RefreshLimits(foreground_refresh_wait_seconds=0.01))
    service._deps = replace(
        service._deps,
        client=client,
        user_profile_port=_UserProfilePort(client),
        get_dialog_placement=lambda _entity_id: {},
    )
    try:
        result = await service.get_entity_info({"entity_id": entity_id})
        data = cast(dict[str, object], result["data"])
        assert data["avatar_history"] == [
            {"photo_id": 99, "date": None},
            {"photo_id": 7, "date": None},
        ]
        assert data["avatar_count"] == 2
        assert cast(dict[str, dict[str, object]], data["sections"])["avatar_history"]["status"] == avatar_status
        assert client.calls == 0
    finally:
        await service.shutdown()
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


@pytest.mark.asyncio
async def test_resolve_entity_requires_canonical_id_match() -> None:
    class MismatchClient(_UnusedClient):
        async def get_entity(self, entity_id: int) -> object:
            del entity_id
            return SimpleNamespace(id=-42)

    conn = sqlite3.connect(":memory:")
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(service._deps, client=MismatchClient(), get_peer_id=lambda value: int(value.id))

    entity, error = await service._resolve_entity(42)

    assert entity is None
    assert error is not None and error["error"] == "entity_not_found"
    assert "Action:" in str(error["message"])
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_resolve_entity_not_found_is_terminal_and_positive_id_is_valid() -> None:
    class NotFoundClient(_UnusedClient):
        async def get_entity(self, entity_id: int) -> object:
            del entity_id
            raise PeerIdInvalidError(request=None)

    class ValidClient(_UnusedClient):
        async def get_entity(self, entity_id: int) -> object:
            del entity_id
            return SimpleNamespace(id=42)

    conn = sqlite3.connect(":memory:")
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(service._deps, client=NotFoundClient(), get_peer_id=lambda value: int(value.id))

    entity, error = await service._resolve_entity(42)
    assert entity is None
    assert error is not None and error["error"] == "entity_not_found"
    assert "Action:" in str(error["message"])

    service._deps = replace(service._deps, client=ValidClient())
    entity, error = await service._resolve_entity(42)
    assert entity is not None and getattr(entity, "id", None) == 42
    assert error is None
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_unknown_timeout_creates_fk_parent_before_refresh_rows(tmp_path: Path) -> None:
    class TimeoutClient(_UnusedClient):
        async def get_entity(self, entity_id: int) -> object:
            del entity_id
            await asyncio.sleep(1)
            raise AssertionError("foreground timeout should cancel resolution")

    path = tmp_path / "unknown-fk.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    service = _test_service(conn, limits=RefreshLimits(0.01, 0.02, 0.05, 1))
    service._deps = replace(service._deps, client=TimeoutClient())

    result = await service._progressive_miss(42, now=100, started_at=100)

    assert result["error"] == "entity_info_pending"
    assert conn.execute("SELECT type FROM entities WHERE id=42").fetchone() == ("unknown",)
    assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == ("pending",)
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_detail_sections WHERE entity_id=42 AND status='pending'"
    ).fetchone() == (5,)
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_unknown_timeout_then_terminal_not_found_stays_terminal_on_reread(tmp_path: Path) -> None:
    class TimeoutThenNotFoundClient(_UnusedClient):
        calls = 0

        async def get_entity(self, entity_id: int) -> object:
            del entity_id
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(1)
            raise ValueError("entity no longer exists")

    path = tmp_path / "unknown-terminal.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    client = TimeoutThenNotFoundClient()
    service = _test_service(conn, limits=RefreshLimits(0.01, 0.02, 0.05, 1))
    service._deps = replace(service._deps, client=client)
    coordinator = service.refresh_coordinator
    assert coordinator is not None

    pending = await service._progressive_miss(42, now=100, started_at=100)
    assert pending["error"] == "entity_info_pending"
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute("SELECT status, reason FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        "rejected",
        "entity_not_found",
    )

    reread = await service.get_entity_info({"entity_id": 42})

    assert reread["error"] == "entity_not_found"
    assert "Action:" in str(reread["message"])
    assert coordinator.queue_depth == 0
    assert client.calls == 2
    await service.shutdown()
    conn.close()


def test_refresh_rejection_is_fk_safe_without_unknown_parent(tmp_path: Path) -> None:
    path = tmp_path / "rejected-fk.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)

    repo.mark_refresh_rejected(42, now=100)

    assert conn.execute("SELECT 1 FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() is None
    assert conn.execute("SELECT 1 FROM entity_detail_sections WHERE entity_id=42").fetchone() is None
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
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (_CURRENT_SCHEMA_VERSION,)
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
