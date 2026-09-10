"""Authorization-scope fencing for paired Entity Profile receipts."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope, capture_auth_scope
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.telegram_demand import RpcAttemptBudget
from tests.test_entity_profile_full_user_pair import _PairClient, _prepare


def _scope(*, account_id: int = 42, dc_id: int = 2, auth_key_id: int = 99) -> TelegramAuthScope:
    return TelegramAuthScope(AUTH_SCOPE_VERSION, account_id, dc_id, auth_key_id)


def test_capture_scope_requires_positive_account_and_primary_key() -> None:
    client = SimpleNamespace(session=SimpleNamespace(dc_id=2, auth_key=SimpleNamespace(key_id=99)))
    assert capture_auth_scope(SimpleNamespace(id=42), client) == _scope()
    assert capture_auth_scope(SimpleNamespace(id=0), client) is None
    assert capture_auth_scope(SimpleNamespace(id=42), SimpleNamespace(session=SimpleNamespace(dc_id=2))) is None
    assert capture_auth_scope(SimpleNamespace(id=42), SimpleNamespace(session=SimpleNamespace(dc_id=0))) is None


@pytest.mark.asyncio
async def test_scope_is_private_evidence_and_reuse_stops_at_ttl(tmp_path) -> None:
    conn, service = _prepare(tmp_path / "scope.sqlite")
    scope = _scope()
    service._deps = replace(service._deps, full_user_auth_scope=lambda: scope)  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    client = cast(_PairClient, service._deps.client)  # type: ignore[attr-defined]
    assert client.full_user_calls == 1
    evidence = service._profiles.read_section_evidence(42, "full_profile")  # type: ignore[attr-defined]
    assert evidence is not None
    assert evidence["identity"] == {
        "auth_scope": scope.as_private_mapping(),
        "entity_type": "user",
        "endpoint": "users.GetFullUser",
        "normalization_version": "entity-profile-full-user-v1",
        "declared_fields": {
            "full_profile": list(evidence["provenance"]["declared_fields"]),
            "personal_channel": list(
                service._profiles.read_section_evidence(42, "personal_channel")["provenance"]["declared_fields"]  # type: ignore[index,attr-defined]
            ),
        },
    }
    assert "identity" not in service._section_summaries(  # type: ignore[attr-defined]
        {"full_profile": {"status": "fresh", "observed_at": 100, "evidence": evidence}}
    )["full_profile"]

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
    service._deps = replace(service._deps, now_provider=lambda: 400.0)  # type: ignore[attr-defined]
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert client.full_user_calls == 2
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_scope_change_during_rpc_rejects_receipt_and_progress(tmp_path) -> None:
    conn, service = _prepare(tmp_path / "scope-change.sqlite")
    first, second = _scope(), _scope(auth_key_id=100)
    current = [first]

    class ChangingClient(_PairClient):
        async def __call__(self, request: object) -> object:
            result = await super().__call__(request)
            current[0] = second
            return result

    service._deps = replace(  # type: ignore[attr-defined]
        service._deps,
        client=ChangingClient(),
        full_user_auth_scope=lambda: current[0],
    )
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute(
        "SELECT next_section, acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("full_profile", 0)
    assert conn.execute(
        "SELECT acquisition_generation FROM entity_detail_sections WHERE entity_id=42 AND section='full_profile'"
    ).fetchone() == (None,)
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_auth_scope_change_starts_new_generation_and_keeps_old_facts(tmp_path) -> None:
    conn, service = _prepare(tmp_path / "scope-generation.sqlite")
    scope = _scope()
    service._deps = replace(service._deps, full_user_auth_scope=lambda: scope)  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    generation = conn.execute(
        "SELECT generation FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone()[0]
    conn.execute(
        "UPDATE entity_profile_refresh_state SET status='complete', next_section='personal_channel' WHERE entity_id=42"
    )
    conn.commit()
    service.auth_scope_changed()  # type: ignore[attr-defined]
    assert conn.execute(
        "SELECT status, generation, pair_eligible FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("pending", generation + 1, 1)
    assert "about" in cast(str, conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone()[0])
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()
