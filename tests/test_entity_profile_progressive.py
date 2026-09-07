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

import pytest
from jsonschema import validate
from telethon.tl.types import User  # type: ignore[import-untyped]

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService, EntityInfoDeps
from mcp_telegram.entity_profile.refresh import EntityRefreshCoordinator, RefreshLimits
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.entity_profile.telegram_gateway import BoundedTelegramGateway
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sync_db import _apply_migration_57, _apply_migrations, ensure_sync_schema
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
            retry_at INTEGER, reason TEXT, updated_at INTEGER NOT NULL
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
async def test_refresh_coordinator_single_flight_and_shutdown() -> None:
    calls = 0
    release = asyncio.Event()

    async def refresh(_entity_id: int) -> None:
        nonlocal calls
        calls += 1
        await release.wait()

    coordinator = EntityRefreshCoordinator(refresh)
    assert coordinator.enqueue(42)
    assert coordinator.enqueue(43)
    assert all(not coordinator.enqueue(42) for _ in range(9))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
        if calls == 2:
            break
    assert calls == 2
    await coordinator.shutdown()
    assert coordinator.queue_depth == 0


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
    repo.save_detail(42, {"id": 42, "type": "user", "name": "Good", "common_chats": [{"id": 7}]}, now=100)
    repo.mark_refresh_failure(42, now=101, reason="timeout")
    stored = repo.read(42, now=101)
    assert stored is not None
    assert stored.detail["common_chats"] == [{"id": 7}]
    assert stored.observed_at == 100
    assert stored.sections["common_chats"]["status"] == "stale"
    assert stored.sections["common_chats"]["observed_at"] == 100
    conn.close()


def test_independent_section_failure_preserves_last_good_payload() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    first = {
        "id": 42,
        "type": "user",
        "name": "Good",
        "common_chats": [{"id": 7}],
        "avatar_history": [{"photo_id": 8}],
        "avatar_count": 1,
        "personal_channel": {"id": 9},
    }
    repo.save_detail(42, first, now=100)
    repo.save_detail(
        42,
        {**first, "common_chats": [], "avatar_history": [], "avatar_count": 0, "personal_channel": {"id": 10}},
        now=101,
        section_outcomes={"common_chats": "timeout", "avatar_history": "rpc_error"},
    )
    stored = repo.read(42, now=101)
    assert stored is not None
    assert stored.detail["common_chats"] == [{"id": 7}]
    assert stored.detail["avatar_history"] == [{"photo_id": 8}]
    assert stored.detail["personal_channel"] == {"id": 10}
    assert stored.sections["common_chats"]["status"] == "stale"
    assert stored.sections["avatar_history"]["status"] == "stale"
    assert stored.sections["personal_channel"]["status"] == "fresh"
    assert stored.sections["common_chats"]["observed_at"] == 100
    conn.close()


def test_section_write_failure_rolls_back_complete_profile_update() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    repo.save_detail(42, {"id": 42, "type": "user", "name": "Before", "common_chats": []}, now=100)
    before = cast(
        tuple[str, int] | None,
        conn.execute("SELECT detail_json, fetched_at FROM entity_details WHERE entity_id = 42").fetchone(),
    )
    conn.execute(
        "CREATE TRIGGER reject_profile_sections BEFORE UPDATE ON entity_detail_sections "
        "BEGIN SELECT RAISE(ABORT, 'section write failed'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="section write failed"):
        repo.save_detail(42, {"id": 42, "type": "user", "name": "After", "common_chats": []}, now=101)

    assert conn.execute("SELECT detail_json, fetched_at FROM entity_details WHERE entity_id = 42").fetchone() == before
    conn.close()


def test_legacy_blob_partial_failure_keeps_original_observed_at() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    legacy = {"schema": 1, "id": 42, "type": "user", "name": "Good", "common_chats": [{"id": 7}]}
    conn.execute("INSERT INTO entities VALUES (42, 'user', 'Good', 'good', NULL, 100)")
    conn.execute("INSERT INTO entity_details VALUES (42, ?, 100)", (json.dumps(legacy),))
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    repo.save_detail(
        42,
        {"id": 42, "type": "user", "name": "Good", "common_chats": []},
        now=101,
        section_outcomes={"common_chats": "timeout"},
    )
    stored = repo.read(42, now=101)
    assert stored is not None
    assert stored.detail["common_chats"] == [{"id": 7}]
    assert stored.sections["common_chats"]["status"] == "stale"
    assert stored.sections["common_chats"]["observed_at"] == 100
    conn.close()


def test_full_profile_failure_does_not_discard_successful_sibling_sections() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    repo.save_detail(
        42,
        {"id": 42, "type": "user", "name": "Good", "about": "old", "common_chats": [{"id": 7}]},
        now=100,
    )
    repo.save_detail(
        42,
        {"id": 42, "type": "user", "name": "Good", "about": None, "common_chats": [{"id": 8}]},
        now=101,
        section_outcomes={"full_profile": "timeout"},
    )
    stored = repo.read(42, now=101)
    assert stored is not None
    assert stored.detail["about"] == "old"
    assert stored.detail["common_chats"] == [{"id": 8}]
    assert stored.sections["full_profile"]["status"] == "stale"
    assert stored.sections["common_chats"]["status"] == "fresh"
    conn.close()


@pytest.mark.asyncio
async def test_bounded_gateway_applies_deadline_to_each_rpc_shape() -> None:
    class SlowClient:
        async def __call__(self, _request: object) -> object:
            await asyncio.sleep(1)
            return None

        async def get_entity(self, _entity_id: int) -> object:
            await asyncio.sleep(1)
            return None

        async def get_messages(self, _entity: object, *, ids: list[int]) -> object:
            del ids
            await asyncio.sleep(1)
            return None

        async def _participants(self) -> object:
            await asyncio.sleep(1)
            return
            yield  # pragma: no cover

        def iter_participants(self, _peer: object, *, limit: int) -> object:
            del limit
            return self._participants()

        def iter_dialogs(self) -> object:
            return self._participants()

    gateway = BoundedTelegramGateway(SlowClient(), timeout_seconds=0.01)
    with pytest.raises(TimeoutError):
        await gateway(object())
    with pytest.raises(TimeoutError):
        await gateway.get_entity(42)
    with pytest.raises(TimeoutError):
        await gateway.get_messages(object(), ids=[1])
    with pytest.raises(TimeoutError):
        async for _item in gateway.iter_participants(object(), limit=1):
            pass
    with pytest.raises(TimeoutError):
        async for _item in gateway.iter_dialogs():
            pass


@pytest.mark.asyncio
async def test_refresh_coordinator_records_flood_wait_without_sleeping() -> None:
    failures: list[BaseException] = []

    async def refresh(_entity_id: int) -> None:
        raise TelegramRpcThrottled(retry_after_seconds=7)

    coordinator = EntityRefreshCoordinator(refresh, on_failure=lambda _id, exc: failures.append(exc))
    assert coordinator.enqueue(42)
    for _ in range(5):
        await asyncio.sleep(0)
        if failures:
            break
    await coordinator.shutdown()
    assert len(failures) == 1
    assert isinstance(failures[0], TelegramRpcThrottled)
    assert failures[0].retry_after_seconds == 7


def test_flood_wait_refresh_failure_persists_retry_and_keeps_last_good() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT, "
        "name_normalized TEXT, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE entity_details (entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    _sections_schema(conn)
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    repo.save_detail(42, {"id": 42, "type": "user", "name": "Good", "common_chats": [{"id": 7}]}, now=100)
    service = _test_service(conn, limits=RefreshLimits())
    service._refresh_failed(42, TelegramRpcThrottled(retry_after_seconds=7))
    stored = repo.read(42, now=100)
    assert stored is not None
    assert stored.detail["common_chats"] == [{"id": 7}]
    retry_at = cast(
        tuple[int | None] | None,
        conn.execute(
            "SELECT retry_at FROM entity_detail_sections WHERE entity_id = 42 AND section = 'common_chats'"
        ).fetchone(),
    )
    assert retry_at == (107,)
    conn.close()


def _test_service(conn: sqlite3.Connection, *, limits: RefreshLimits) -> DaemonEntityInfoService:
    return DaemonEntityInfoService(
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


@pytest.mark.asyncio
async def test_refresh_resolution_preserves_success_and_failure_semantics() -> None:
    conn = sqlite3.connect(":memory:")
    service = _test_service(conn, limits=RefreshLimits())
    worker = _test_service(conn, limits=RefreshLimits())
    entity = SimpleNamespace(id=42)

    async def resolved(_entity_id: int) -> tuple[object, None]:
        return entity, None

    worker._resolve_entity = resolved  # type: ignore[method-assign]
    assert await service._refresh_resolved_entity(worker, 42) is entity

    async def throttled(_entity_id: int) -> tuple[None, dict[str, object]]:
        return None, {"_retry_after_seconds": 7}

    worker._resolve_entity = throttled  # type: ignore[method-assign]
    with pytest.raises(TelegramRpcThrottled) as throttled_error:
        await service._refresh_resolved_entity(worker, 42)
    assert throttled_error.value.retry_after_seconds == 7

    async def unavailable(_entity_id: int) -> tuple[None, dict[str, object]]:
        return None, {"message": "unavailable"}

    worker._resolve_entity = unavailable  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="unavailable"):
        await service._refresh_resolved_entity(worker, 42)
    await service.shutdown()
    await worker.shutdown()
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
    resolved: list[object] = []
    entity = SimpleNamespace(id=42, first_name="Known", username="known")

    async def resolve(_entity_id: int) -> tuple[object, None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)
        return entity, None

    async def background(entity_id: int) -> None:
        result, error = await service._resolve_entity(entity_id)
        assert error is None
        resolved.append(result)

    service = _test_service(conn, limits=RefreshLimits(0.01, 0.02, 0.05, 1))
    service._resolve_entity = resolve  # type: ignore[method-assign]
    service._refresh = EntityRefreshCoordinator(
        background,
        limits=service._deps.refresh_limits,
        on_failure=service._refresh_failed,
    )
    pending = await service._progressive_miss(42, now=100, started_at=100)
    assert pending["error"] == "entity_info_pending"
    await asyncio.sleep(0.01)
    await service.shutdown()
    assert resolved == [entity]
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

    async def background(entity_id: int) -> None:
        await service._resolve_entity(entity_id)

    service._resolve_entity = resolve  # type: ignore[method-assign]
    service._refresh = EntityRefreshCoordinator(
        background,
        limits=service._deps.refresh_limits,
        on_failure=service._refresh_failed,
    )
    pending = await service._progressive_miss(42, now=100, started_at=100)
    assert pending["error"] == "entity_info_pending"
    await asyncio.sleep(0.01)
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
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (57,)
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
