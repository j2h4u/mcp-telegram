"""Authorization-scope fencing for paired Entity Profile receipts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon import capture_auth_scope
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.test_entity_profile_full_user_pair import _PairClient, _prepare


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


@pytest.mark.parametrize(
    "case",
    [
        (None, None, {"type": "user"}, False, False),
        (lambda: _scope(), _scope().as_private_mapping(), {"type": "user"}, False, False),
        (lambda: _scope(dc_id=3), _scope().as_private_mapping(), {"type": "user"}, False, True),
        (lambda: _scope(account_id=7), _scope().as_private_mapping(), {"type": "user"}, False, True),
        (lambda: None, _scope().as_private_mapping(), {"type": "user"}, False, True),
        (lambda: _scope(), None, {"type": "user"}, False, True),
        (lambda: _scope(), _scope().as_private_mapping(), [], False, False),
        (lambda: _scope(), _scope().as_private_mapping(), {"type": "channel"}, False, False),
        (lambda: _scope(), _scope().as_private_mapping(), {"type": "user"}, True, True),
    ],
)
def test_profile_scope_changed_fences_only_stale_or_unavailable_pairs(
    tmp_path: Path,
    case: tuple[object, dict[str, object] | None, object, bool, bool],
) -> None:
    provider, stored_scope, detail, receipts_changed, expected = case
    conn, raw_service = _prepare(tmp_path / f"scope-{expected}-{hash(str(detail))}.sqlite")
    service = cast(DaemonEntityInfoService, raw_service)
    service._deps = replace(service._deps, full_user_auth_scope=provider)  # type: ignore[arg-type]
    service._profiles.pair_receipts_require_new_scope = MagicMock(return_value=receipts_changed)  # type: ignore[method-assign]
    cached = SimpleNamespace(detail=detail, profile_observation_scope=stored_scope)

    assert service._profile_scope_changed(cached, 42, now=100) is expected  # type: ignore[attr-defined]
    conn.close()


def test_scope_change_preserves_cached_detail_and_hides_private_scope(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "scope-preserve.sqlite")
    service = cast(DaemonEntityInfoService, raw_service)
    service._deps = replace(service._deps, full_user_auth_scope=lambda: _scope())
    cached = SimpleNamespace(detail={"id": 42, "type": "user", "name": "Target"}, profile_observation_scope=None)

    assert service._profile_scope_changed(cached, 42, now=100) is True  # type: ignore[attr-defined]
    result = cast(dict[str, object], service._progressive_result(42, cached.detail, {}, now=100))  # type: ignore[attr-defined]
    data = cast(dict[str, object], result["data"])

    assert data["name"] == "Target"
    assert "auth_scope" not in data
    conn.close()


@pytest.mark.asyncio
async def test_scope_is_private_evidence_and_reuse_stops_at_ttl(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "scope.sqlite")
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

    conn.execute(
        "UPDATE entity_profile_refresh_state SET status='pending', next_section='full_profile', acquisition_cursor=0 "
        "WHERE entity_id=42"
    )
    conn.commit()
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert client.full_user_calls == 1

    conn.execute("UPDATE entity_profile_refresh_state SET status='pending' WHERE entity_id=42")
    conn.execute("UPDATE entity_profile_refresh_state SET next_section='full_profile' WHERE entity_id=42")
    conn.commit()
    service._deps = replace(service._deps, now_provider=lambda: 400.0)
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert client.full_user_calls == 2
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_ownership_invalid_cached_profile_admits_refresh_and_offers_demand(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "ownership-admission.sqlite", migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    sink = _DemandSink()
    service.bind_demand_sink(sink)

    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    conn.execute(
        "UPDATE entity_details SET profile_owner_account_id=NULL, profile_observation_scope_json=NULL "
        "WHERE entity_id=42"
    )
    conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id=42")
    conn.commit()

    result = service._progressive_cached_result(42, now=100)  # type: ignore[attr-defined]

    assert result is not None and result["error"] == "entity_info_pending"
    assert coordinator.queue_depth == 1
    assert sink.kinds == [DemandKind.ENTITY_PROFILE_REFRESH]
    assert conn.execute(
        "SELECT status, generation, next_section, acquisition_cursor FROM entity_profile_refresh_state "
        "WHERE entity_id=42"
    ).fetchone() == ("pending", 1, "full_profile", 0)

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
    conn.execute(
        "UPDATE entity_details SET profile_owner_account_id=NULL, profile_observation_scope_json=NULL "
        "WHERE entity_id=42"
    )
    conn.execute(
        "UPDATE entity_profile_refresh_state SET status='failed', retry_at=777, reason='flood_wait' "
        "WHERE entity_id=42"
    )
    conn.commit()
    before = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT status, retry_at, reason, generation, started_at, next_section, acquisition_cursor "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone(),
    )

    result = service._progressive_cached_result(42, now=100)  # type: ignore[attr-defined]

    assert result is not None and result["error"] == "entity_info_pending"
    assert coordinator.queue_depth == 1
    assert sink.kinds == [DemandKind.ENTITY_PROFILE_REFRESH]
    assert conn.execute(
        "SELECT status, retry_at, reason, generation, started_at, next_section, acquisition_cursor "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == before

    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_scope_change_during_rpc_rejects_receipt_and_progress(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "scope-change.sqlite")
    service = cast(DaemonEntityInfoService, raw_service)
    first, second = _scope(), _scope(auth_key_id=100)
    current = [first]

    class ChangingClient(_PairClient):
        async def __call__(self, request: object) -> object:
            result = await super().__call__(request)
            current[0] = second
            return result

    service._deps = replace(
        service._deps,
        client=ChangingClient(),
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
async def test_auth_scope_change_starts_new_generation_and_keeps_old_facts(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "scope-generation.sqlite")
    service = cast(DaemonEntityInfoService, raw_service)
    scope = _scope()
    service._deps = replace(service._deps, full_user_auth_scope=lambda: scope)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    generation_row = cast(
        tuple[int],
        conn.execute("SELECT generation FROM entity_profile_refresh_state WHERE entity_id=42").fetchone(),
    )
    generation = generation_row[0]
    conn.execute(
        "UPDATE entity_profile_refresh_state SET status='complete', next_section='personal_channel' WHERE entity_id=42"
    )
    conn.commit()
    service.auth_scope_changed()  # type: ignore[attr-defined]
    assert conn.execute(
        "SELECT status, generation, pair_eligible FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("pending", generation + 1, 1)
    assert "about" in cast(str, conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone()[0])
    await service.shutdown()
    conn.close()
