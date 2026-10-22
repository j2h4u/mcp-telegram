"""Authorization-scope fencing for paired Entity Profile receipts."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon import capture_auth_scope
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import UserProfileObservation
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.sync_transactions import write_savepoint
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient, _prepare


def _scope(*, account_id: int = 42, dc_id: int = 2, auth_key_id: int = 99) -> TelegramAuthScope:
    return TelegramAuthScope(AUTH_SCOPE_VERSION, account_id, dc_id, auth_key_id)


class _DemandSink:
    def __init__(self) -> None:
        self.kinds: list[DemandKind] = []

    def offer(self, kind: DemandKind) -> bool:
        self.kinds.append(kind)
        return True


def test_capture_scope_requires_positive_account_and_primary_key() -> None:
    client = SimpleNamespace(session=SimpleNamespace(dc_id=2, auth_key=SimpleNamespace(key_id=99)))
    assert capture_auth_scope(SimpleNamespace(id=42), client) == _scope()
    assert capture_auth_scope(SimpleNamespace(id=0), client) is None
    assert capture_auth_scope(SimpleNamespace(id=42), SimpleNamespace(session=SimpleNamespace(dc_id=2))) is None
    assert capture_auth_scope(SimpleNamespace(id=42), SimpleNamespace(session=SimpleNamespace(dc_id=0))) is None


@pytest.mark.asyncio
async def test_scope_is_private_evidence_and_invalidated_pair_is_reacquired(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "scope.sqlite", migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    scope = _scope()
    service._deps = replace(service._deps, full_user_auth_scope=lambda: scope)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    client = cast(_PairClient, service._deps.client)
    assert client.full_user_calls == 1
    evidence = service._profiles.read_section_evidence(42, "full_profile")
    assert evidence is not None
    full_provenance = cast(dict[str, object], evidence["provenance"])
    channel_evidence = service._profiles.read_section_evidence(42, "personal_channel")
    assert channel_evidence is not None
    channel_provenance = cast(dict[str, object], channel_evidence["provenance"])
    assert evidence["identity"] == {
        "auth_scope": scope.as_private_mapping(),
        "entity_type": "user",
        "endpoint": "users.GetFullUser",
        "normalization_version": "entity-profile-full-user-v1",
        "declared_fields": {
            "full_profile": list(cast(list[str], full_provenance["declared_fields"])),
            "personal_channel": list(cast(list[str], channel_provenance["declared_fields"])),
        },
    }
    assert (
        "identity"
        not in service._section_summaries(
            {"full_profile": {"status": "fresh", "observed_at": 100, "evidence": evidence}}
        )["full_profile"]
    )

    service._profiles.request_section_refresh(42, ("full_profile", "personal_channel"), now=100, pair_mode="enabled")
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert client.full_user_calls == 2

    service._profiles.request_section_refresh(42, ("full_profile", "personal_channel"), now=400, pair_mode="enabled")
    service._deps = replace(service._deps, now_provider=lambda: 400.0)
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert client.full_user_calls == 3
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_disabled_baseline_scope_reuse_survives_restart_without_pair_receipts(tmp_path: Path) -> None:
    path = tmp_path / "disabled-scope.sqlite"
    conn, raw_service = _prepare(path, migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    service._deps = replace(service._deps, enable_full_user_pair=False)
    coordinator = service.refresh_coordinator
    assert coordinator is not None

    service._profiles.mark_pending(42, now=100, pair_mode_override="disabled")
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_profile_refresh_state SET next_section='personal_channel', acquisition_cursor=0 "
            "WHERE entity_id=42"
        )
    conn.commit()
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_detail_sections SET status='fresh', observed_at=100, reason=NULL WHERE entity_id=42"
        )
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_profile_refresh_state SET status='complete', retry_at=NULL, reason=NULL WHERE entity_id=42"
        )
    conn.commit()
    before = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT status, retry_at, generation, pair_attempts, pair_measurement_complete "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone(),
    )
    client = cast(_PairClient, service._deps.client)
    assert service._profiles.read_section_evidence(42, "full_profile") is None
    assert service._profiles.read_section_evidence(42, "personal_channel") is None

    first = await service.get_entity_info({"entity_id": 42})

    assert first["ok"] is True
    assert coordinator.queue_depth == 0
    assert client.full_user_calls == 2
    assert (
        conn.execute(
            "SELECT status, retry_at, generation, pair_attempts, pair_measurement_complete "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone()
        == before
    )
    await service.shutdown()
    conn.close()

    reopened = sqlite3.connect(path)
    restarted = cast(DaemonEntityInfoService, _pair_service(reopened, _PairClient(), enabled=False))
    restarted_coordinator = restarted.refresh_coordinator
    assert restarted_coordinator is not None
    restarted_client = cast(_PairClient, restarted._deps.client)

    second = await restarted.get_entity_info({"entity_id": 42})

    assert second["ok"] is True
    assert restarted_coordinator.queue_depth == 0
    assert restarted_client.full_user_calls == 0
    assert (
        reopened.execute(
            "SELECT status, retry_at, generation, pair_attempts, pair_measurement_complete "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone()
        == before
    )
    await restarted.shutdown()
    reopened.close()


@pytest.mark.asyncio
async def test_ownership_invalid_cached_profile_admits_refresh_and_offers_demand(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "ownership-admission.sqlite", migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    sink = _DemandSink()
    service.bind_demand_sink(sink)

    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_details SET profile_owner_account_id=NULL, profile_observation_scope_json=NULL "
            "WHERE entity_id=42"
        )
    before = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT * FROM entity_profile_refresh_state WHERE entity_id=42").fetchone(),
    )

    result = await service.get_entity_info({"entity_id": 42})

    assert result["ok"] is True
    data = cast(dict[str, object], result["data"])
    assert data["name"] == "Target"
    assert data["completeness"] == "partial"
    assert "about" not in data and "phone" not in data
    assert coordinator.queue_depth == 0
    assert sink.kinds == []
    assert conn.execute("SELECT * FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == before

    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_ownership_invalid_cached_profile_preserves_failed_retry_state(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "ownership-failed.sqlite", migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    sink = _DemandSink()
    service.bind_demand_sink(sink)

    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_details SET profile_owner_account_id=NULL, profile_observation_scope_json=NULL "
            "WHERE entity_id=42"
        )
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_profile_refresh_state SET status='failed', retry_at=777, reason='flood_wait' WHERE entity_id=42"
        )
    conn.commit()
    before = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT status, retry_at, reason, generation, started_at, next_section, acquisition_cursor "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone(),
    )

    result = await service.get_entity_info({"entity_id": 42})

    assert result["ok"] is True
    data = cast(dict[str, object], result["data"])
    assert data["name"] == "Target"
    assert data["completeness"] == "partial"
    assert "about" not in data and "phone" not in data
    assert coordinator.queue_depth == 0
    assert sink.kinds == []
    assert (
        conn.execute(
            "SELECT status, retry_at, reason, generation, started_at, next_section, acquisition_cursor "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone()
        == before
    )

    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_public_local_read_does_not_requeue_failed_refresh(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "ownership-reread.sqlite", migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    coordinator = service.refresh_coordinator
    assert coordinator is not None

    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    with write_savepoint(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET status='failed', retry_at=877, reason='flood_wait'")
    before = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT status, retry_at, generation, next_section, acquisition_cursor "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone(),
    )
    client = cast(_PairClient, service._deps.client)
    calls_before = client.full_user_calls

    result = await service.get_entity_info({"entity_id": 42})

    assert result["ok"] is True
    assert cast(dict[str, object], result["data"])["about"] == "about"
    assert coordinator.queue_depth == 0
    assert client.full_user_calls == calls_before
    assert (
        conn.execute(
            "SELECT status, retry_at, generation, next_section, acquisition_cursor "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone()
        == before
    )
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_scope_change_during_rpc_rejects_receipt_and_progress(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "scope-change.sqlite", migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    first, second = _scope(), _scope(auth_key_id=100)
    current = [first]

    class ChangingClient(_PairClient):
        async def fetch_user_profile(self, user_id: int, target_kind: object) -> UserProfileObservation:
            result = await super().fetch_user_profile(user_id, target_kind)  # type: ignore[arg-type]
            current[0] = second
            return result

    client = ChangingClient()
    service._deps = replace(
        service._deps,
        client=client,
        user_profile_port=client,
        full_user_auth_scope=lambda: current[0],
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute(
        "SELECT next_section, acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("full_profile", 0)
    assert conn.execute(
        "SELECT acquisition_generation FROM entity_detail_sections WHERE entity_id=42 AND section='full_profile'"
    ).fetchone() == (None,)
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_same_account_scope_rotation_keeps_local_facts_without_mutation(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "scope-generation.sqlite", migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    current_scope = [_scope()]
    service._deps = replace(service._deps, full_user_auth_scope=lambda: current_scope[0])
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    before = {
        table: conn.execute(f"SELECT * FROM {table} WHERE entity_id=42").fetchall()
        for table in ("entity_details", "entity_detail_sections", "entity_profile_refresh_state")
    }
    current_scope[0] = _scope(auth_key_id=100)

    result = await service.get_entity_info({"entity_id": 42})

    assert result["ok"] is True
    assert cast(dict[str, object], result["data"])["about"] == "about"
    assert coordinator.queue_depth == 0
    assert {table: conn.execute(f"SELECT * FROM {table} WHERE entity_id=42").fetchall() for table in before} == before
    assert "auth_scope" not in str(result)
    await service.shutdown()
    conn.close()
