"""Profile queries project durable facts without acquiring or queueing work."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.types import EmojiStatus, User  # type: ignore[import-untyped]

import mcp_telegram.daemon as daemon
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import (
    PROFILE_SECTIONS,
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChatEntityProfileFacts,
    GroupProfileObservation,
    ObservationBoundary,
    ProfileFieldObservation,
    ProjectionStatus,
    TargetKind,
    UserAvatarHistoryObservation,
    UserProfileObservation,
    UserReference,
)
from mcp_telegram.entity_profile.full_user_normalization import normalize_full_user_response
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, RefreshLimits
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_savepoint
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from tests.helpers import FakeChannelProfilePort
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient
from tests.test_entity_profile_progressive import _test_service

type LocalProfile = tuple[sqlite3.Connection, DaemonEntityInfoService]


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@pytest.fixture
def local_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalProfile]:
    path = tmp_path / "profiles.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    service = _test_service(conn, limits=RefreshLimits())
    service._deps = replace(
        service._deps,
        self_id=42,
        full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99),
        now_provider=lambda: 100_000.0,
    )
    monkeypatch.setattr(service, "_write_profiles", AsyncMock(side_effect=AssertionError("query wrote profiles")))
    service.bind_demand_sink(MagicMock(offer=MagicMock(side_effect=AssertionError("query offered demand"))))
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    monkeypatch.setattr(coordinator, "enqueue", MagicMock(side_effect=AssertionError("query admitted work")))
    try:
        yield conn, service
    finally:
        conn.close()


def _seed_profile(conn: sqlite3.Connection, *, owner: int | None = 42, entity_type: str = "user") -> None:
    with write_savepoint(conn):
        conn.execute(
            "INSERT INTO entities(id,type,name,username,updated_at) VALUES (7,?,'Known','known',100)", (entity_type,)
        )
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (7,?,100,?)",
            (json.dumps({"schema": 1, "id": 7, "type": entity_type, "about": "Stored bio", "phone": "123"}), owner),
        )
        conn.executemany(
            "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,payload_json) "
            "VALUES (7,?,'fresh',100,'{}')",
            ((section,) for section in PROFILE_SECTIONS),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", ("user", "bot", "service", "channel", "supergroup", "group"))
async def test_old_profile_read_is_local_for_every_type(local_profile: LocalProfile, entity_type: str) -> None:
    conn, service = local_profile
    _seed_profile(conn, entity_type=entity_type)
    before = conn.total_changes
    result = await service.get_entity_info({"entity_id": 7})
    assert result["ok"] is True
    data = _mapping(result["data"])
    assert data["about"] == "Stored bio"
    assert data["completeness"] == "complete"
    assert _mapping(_mapping(data["sections"])["full_profile"])["observed_at"] == 100
    assert conn.total_changes == before


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", (None, TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 3, 123)))
async def test_same_account_reads_survive_missing_or_changed_session_key(
    local_profile: LocalProfile, scope: TelegramAuthScope | None
) -> None:
    conn, service = local_profile
    _seed_profile(conn)
    service._deps = replace(service._deps, full_user_auth_scope=lambda: scope)
    result = await service.get_entity_info({"entity_id": 7})
    assert result["ok"] is True
    assert _mapping(result["data"])["phone"] == "123"


@pytest.mark.asyncio
async def test_wrong_account_profile_is_not_exposed(local_profile: LocalProfile) -> None:
    conn, service = local_profile
    _seed_profile(conn, owner=99)
    result = await service.get_entity_info({"entity_id": 7})
    assert result["ok"] is False
    assert result["error"] == "entity_profile_account_unavailable"
    assert "Stored bio" not in json.dumps(result)


@pytest.mark.asyncio
async def test_unowned_legacy_profile_exposes_only_canonical_identity(local_profile: LocalProfile) -> None:
    conn, service = local_profile
    _seed_profile(conn, owner=None)
    result = await service.get_entity_info({"entity_id": 7})
    assert result["ok"] is True
    data = _mapping(result["data"])
    assert data["name"] == "Known"
    assert data["completeness"] == "partial"
    assert _mapping(_mapping(data["sections"])["full_profile"])["reason"] == "not_observed"
    assert "phone" not in data
    assert "about" not in data


@pytest.mark.asyncio
async def test_terminal_acquisition_failure_does_not_hide_stored_profile(local_profile: LocalProfile) -> None:
    conn, service = local_profile
    _seed_profile(conn)
    with write_savepoint(conn):
        conn.execute(
            "INSERT INTO entity_profile_refresh_state(entity_id,status,reason,updated_at) "
            "VALUES (7,'rejected','entity_not_found',200)"
        )
    result = await service.get_entity_info({"entity_id": 7})
    assert result["ok"] is True
    assert _mapping(result["data"])["about"] == "Stored bio"


@pytest.mark.asyncio
async def test_missing_entity_is_local_absence_without_writes(local_profile: LocalProfile) -> None:
    conn, service = local_profile
    before = conn.total_changes
    result = await service.get_entity_info({"entity_id": 999})
    assert result["ok"] is False
    assert result["error"] == "entity_not_observed"
    assert conn.total_changes == before


@pytest.mark.asyncio
@pytest.mark.parametrize("name", (None, "Named local stub"))
async def test_unknown_directory_identity_is_not_reported_as_complete(
    local_profile: LocalProfile, name: str | None
) -> None:
    conn, service = local_profile
    with write_savepoint(conn):
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (7,'unknown',?,100)", (name,))
    before = conn.total_changes
    result = await service.get_entity_info({"entity_id": 7})
    if name is None:
        assert result["error"] == "entity_not_observed"
    else:
        assert result["ok"] is True
        data = _mapping(result["data"])
        assert data["name"] == name
        assert data["completeness"] == "partial"
        sections = _mapping(data["sections"])
        assert all(_mapping(value)["reason"] == "not_observed" for value in sections.values())
    assert conn.total_changes == before


@pytest.mark.asyncio
async def test_self_published_identity_is_read_without_persisting_or_acquiring(local_profile: LocalProfile) -> None:
    conn, service = local_profile
    service._deps = replace(service._deps, self_profile={"first_name": "Self", "username": "self"})
    before = conn.total_changes
    result = await service.get_entity_info({"entity_id": 42})
    assert result["ok"] is True
    assert _mapping(result["data"])["name"] == "Self"
    assert _mapping(result["data"])["completeness"] == "partial"
    assert conn.total_changes == before


@pytest.mark.asyncio
async def test_background_reconciliation_survives_restart_beyond_waiter_capacity(tmp_path: Path) -> None:
    path = tmp_path / "backfill.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    service = _test_service(conn, limits=RefreshLimits(max_queued_refreshes=1))
    service._deps = replace(service._deps, self_id=42)
    with write_savepoint(conn):
        conn.executemany(
            "INSERT INTO dialogs(dialog_id,name,type,archived,hidden) VALUES (?,'DM','user',1,1)",
            ((entity_id,) for entity_id in range(1000, 1129)),
        )
        conn.executemany(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (?,1,'automatic',100)",
            ((entity_id,) for entity_id in range(1000, 1129)),
        )

    offered_counts: list[int] = []

    def after_commit(kind: DemandKind) -> bool:
        assert kind is DemandKind.ENTITY_PROFILE_REFRESH
        assert not conn.in_transaction
        count = cast(
            int,
            conn.execute("SELECT count(*) FROM entity_profile_refresh_state WHERE status='pending'").fetchone()[0],
        )
        offered_counts.append(count)
        return True

    service.bind_demand_sink(MagicMock(offer=after_commit))
    assert [await service.reconcile_history_profiles() for _ in range(6)] == [32, 32, 32, 32, 1, 0]
    assert offered_counts == [32, 64, 96, 128, 129]
    assert service.refresh_coordinator is not None
    assert service.refresh_coordinator.queue_depth == 0
    await service.shutdown()
    conn.close()

    reopened = sqlite3.connect(path)
    resumed = _test_service(reopened, limits=RefreshLimits())
    resumed._deps = replace(resumed._deps, self_id=42)
    try:
        assert await resumed.reconcile_history_profiles() == 0
        assert resumed._profiles.next_due_refresh(now=100) is not None
        assert reopened.execute("SELECT count(*) FROM entity_profile_refresh_state").fetchone()[0] == 129
    finally:
        await resumed.shutdown()
        reopened.close()


@pytest.mark.asyncio
async def test_lifetime_drains_startup_backlog_then_runs_one_local_heartbeat_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = asyncio.Event()
    batches = iter((32, 7, 0, 0))
    seen: list[int] = []

    async def reconcile() -> int:
        count = next(batches)
        seen.append(count)
        if len(seen) == 4:
            shutdown.set()
        return count

    service = SimpleNamespace(reconcile_history_profiles=reconcile)
    ctx = SimpleNamespace(
        shutdown_event=shutdown,
        coordinator=object(),
        api_server=SimpleNamespace(_get_entity_info_service=lambda: service),
        conn=None,
        client=None,
        handler_manager=None,
    )
    monkeypatch.setattr(daemon, "_log_heartbeat", lambda *_args: None)
    await daemon._run_daemon_lifetime(cast(daemon._SyncMainContext, ctx))
    assert seen == [32, 7, 0, 0]


@pytest.mark.asyncio
async def test_rpc_reserves_order_before_acquisition_and_newer_push_wins(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class DelayedProfile(_PairClient):
        async def fetch_user_profile(
            self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
        ) -> UserProfileObservation:
            entered.set()
            await release.wait()
            return await super().fetch_user_profile(user_id, target_kind, owner_account_id=owner_account_id)

    path = tmp_path / "ordered-profile.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    client = DelayedProfile(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    service._profiles.save_core({"id": 42, "type": "user", "name": "Known"}, now=100)
    service._profiles.mark_pending(42, now=100)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    acquisition = asyncio.create_task(EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert conn.execute("SELECT value FROM daemon_state WHERE key='canonical_observation_sequence'").fetchone() == (
            "1",
        )
        pushed = ProfileFieldObservation(42, allocate_observation_order(conn), 100, "update")
        service._profiles.apply_profile_update(
            42, {"phone": "new phone", "current_photo": {"photo_id": 99, "date": None}}, observation=pushed
        )
        release.set()
        await acquisition
        result = await service.get_entity_info({"entity_id": 42})
        assert result["ok"] is True
        data = _mapping(result["data"])
        assert data["phone"] == "new phone"
        assert data["about"] == "about"
        assert data["avatar_history"] == [{"photo_id": 99, "date": None}]
        assert client.full_user_calls == 1
    finally:
        release.set()
        await acquisition
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_photo", (None, {"photo_id": 99, "date": None}))
async def test_avatar_history_uses_canonical_push_photo_not_stale_full_payload(
    tmp_path: Path, current_photo: dict[str, object] | None
) -> None:
    class EmptyHistory:
        async def fetch_user_avatar_history(self, reference: UserReference) -> UserAvatarHistoryObservation:
            return UserAvatarHistoryObservation(reference.user_id, (), 0, ProjectionStatus.USABLE, None, 100, 100)

    path = tmp_path / "photo-history.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    service = cast(DaemonEntityInfoService, _pair_service(conn, _PairClient(channel_id=None), enabled=True))
    service._deps = replace(service._deps, user_avatar_history_port=EmptyHistory())
    _seed_profile(conn)
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_detail_sections SET payload_json=? WHERE entity_id=7 AND section='full_profile'",
            (json.dumps({"current_photo": {"photo_id": 1, "date": None}}),),
        )
        service._profiles.apply_profile_update(
            7,
            {"current_photo": current_photo},
            observation=ProfileFieldObservation(42, allocate_observation_order(conn), 100, "update"),
        )
    try:
        acquired = await service._acquire_user_avatar_history(7)
        assert acquired.detail_patch["avatar_history"] == ([] if current_photo is None else [current_photo])
        assert acquired.detail_patch["avatar_count"] == (0 if current_photo is None else 1)
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_durable_user_profile_preserves_dynamic_relationship_and_emoji_fields(tmp_path: Path) -> None:
    class RelationshipProfile(_PairClient):
        async def fetch_user_profile(
            self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
        ) -> UserProfileObservation:
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            user = User(
                id=user_id,
                first_name="Known",
                contact=True,
                mutual_contact=True,
                close_friend=True,
                emoji_status=EmojiStatus(document_id=555),
            )
            return normalize_full_user_response(
                SimpleNamespace(
                    full_user=SimpleNamespace(about="bio", blocked=False, personal_channel_id=None),
                    users=[user],
                    chats=[],
                ),
                target_id=user_id,
                target_kind=target_kind,
                owner_account_id=owner_account_id,
                observation=ObservationBoundary(100, 100),
            )

    path = tmp_path / "relationship-profile.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    service = cast(DaemonEntityInfoService, _pair_service(conn, RelationshipProfile(), enabled=True))
    service._profiles.save_core({"id": 42, "type": "user", "name": "Known"}, now=100)
    service._profiles.mark_pending(42, now=100)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    try:
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
        result = await service.get_entity_info({"entity_id": 42})
        assert result["ok"] is True
        data = _mapping(result["data"])
        assert data["emoji_status_id"] == 555
        assert data["mutual_contact"] is True
        assert data["close_friend"] is True
        membership = _mapping(data["my_membership"])
        assert membership["is_member"] is True
        assert membership["admin_rights"] is None
        assert _mapping(membership["relationship"]) == {
            "contact": True,
            "mutual_contact": True,
            "close_friend": True,
            "blocked": False,
        }
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", ("channel", "supergroup", "group"))
async def test_durable_chat_profile_preserves_observed_membership_and_type_facts(
    tmp_path: Path, entity_type: str
) -> None:
    entity_id = -7 if entity_type == "group" else -1_000_000_000_007
    facts: ChatEntityProfileFacts = {
        "my_membership": {"is_member": True, "is_admin": True, "admin_rights": {"delete_messages": True}},
        "restrictions": [{"platform": "all", "reason": "test", "text": "restriction"}],
    }
    if entity_type == "group":
        facts["migrated_to"] = -1_000_000_000_009
    else:
        facts["has_topics"] = entity_type == "supergroup"

    class GroupProfile:
        async def fetch_group_profile(self, group_id: int) -> GroupProfileObservation:
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            return GroupProfileObservation(
                group_id,
                "about",
                None,
                None,
                "participants_forbidden",
                None,
                100,
                100,
                chat_entity_fields=facts,
                participants_count=23,
            )

    path = tmp_path / "chat-facts.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    service = _test_service(conn, limits=RefreshLimits())
    profile = ChannelProfileObservation(
        -1_000_000_000_007, "about", 1, None, None, None, {}, None, 100, 100, chat_entity_fields=facts
    )
    overlap = ChannelContactOverlapObservation(
        -1_000_000_000_007, (), ProjectionStatus.PARTIAL, "bounded_contacts_page", 100, 100
    )
    port = FakeChannelProfilePort(profile, overlap)
    service._deps = replace(
        service._deps,
        self_id=42,
        full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99),
        group_profile_port=GroupProfile(),
        channel_profile_port=port,
        channel_reference_provider=port,
    )
    service._profiles.save_core({"id": entity_id, "type": entity_type, "name": "Known"}, now=100)
    service._profiles.mark_pending(entity_id, now=100)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    try:
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
        result = await service.get_entity_info({"entity_id": entity_id})
        assert result["ok"] is True
        data = _mapping(result["data"])
        assert data["my_membership"] == facts["my_membership"]
        assert data["restrictions"] == facts["restrictions"]
        if entity_type == "group":
            assert data["migrated_to"] == facts.get("migrated_to")
            assert data["members_count"] == 23
        else:
            assert data["has_topics"] == facts.get("has_topics")
            assert port.profile_calls == [entity_id]
    finally:
        await service.shutdown()
        conn.close()
