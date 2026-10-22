"""Authorization scope comparison for durable FullUser pair receipts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget
from tests.test_entity_profile_full_user_pair import _prepare

SCOPE = {"version": 1, "account_id": 42, "dc_id": 2, "auth_key_id": 99}
SECTIONS = ("full_profile", "personal_channel")


def _repo(path: Path, *, receipt_scope: object = SCOPE) -> tuple[sqlite3.Connection, EntityProfileRepository]:
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO entities(id, type, name, updated_at) VALUES (42, 'user', 'User', 1)")
    identity = json.dumps({"auth_scope": receipt_scope, "endpoint": "users.GetFullUser"})
    for section in SECTIONS:
        conn.execute(
            "INSERT INTO entity_detail_sections "
            "(entity_id, section, status, observed_at, acquisition_identity_json) VALUES (42, ?, 'fresh', 100, ?)",
            (section, identity),
        )
    conn.commit()
    return conn, EntityProfileRepository(conn, section_ttl_seconds=10)


def test_old_fresh_pair_with_same_auth_scope_does_not_request_new_scope(tmp_path: Path) -> None:
    conn, repo = _repo(tmp_path / "same-scope.sqlite")
    changed_identity = json.dumps(
        {"auth_scope": SCOPE, "endpoint": "different", "normalization_version": "new", "declared_fields": ["other"]}
    )
    conn.execute(
        "UPDATE entity_detail_sections SET acquisition_identity_json=?, acquisition_generation=999, observed_at=1",
        (changed_identity,),
    )
    conn.commit()
    assert repo.pair_receipts_require_new_scope(42, {"auth_scope": SCOPE, "endpoint": "changed"}, now=111) is False
    conn.close()


@pytest.mark.parametrize(
    "changed",
    [
        {"version": 2, "account_id": 42, "dc_id": 2, "auth_key_id": 99},
        {"version": 1, "account_id": 7, "dc_id": 2, "auth_key_id": 99},
        {"version": 1, "account_id": 42, "dc_id": 3, "auth_key_id": 99},
        {"version": 1, "account_id": 42, "dc_id": 2, "auth_key_id": 100},
    ],
)
def test_actual_auth_scope_change_requests_new_scope(tmp_path: Path, changed: dict[str, int]) -> None:
    conn, repo = _repo(tmp_path / f"changed-{changed['account_id']}-{changed['dc_id']}-{changed['auth_key_id']}.sqlite")
    assert repo.pair_receipts_require_new_scope(42, {"auth_scope": changed}, now=101) is True
    conn.close()


@pytest.mark.parametrize("malformed", [None, {}, {"version": 1}, "not-a-scope"])
def test_missing_or_malformed_scope_fails_closed(tmp_path: Path, malformed: object) -> None:
    conn, repo = _repo(tmp_path / f"malformed-{malformed!s}.sqlite", receipt_scope=malformed)
    assert repo.pair_receipts_require_new_scope(42, {"auth_scope": SCOPE}, now=101) is True
    assert repo.pair_receipts_require_new_scope(42, {"auth_scope": None}, now=101) is True
    conn.close()


def test_pair_eligibility_guard_and_reuse_ttl_remain_separate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, repo = _repo(tmp_path / "eligibility.sqlite")
    conn.execute("UPDATE entity_detail_sections SET status='stale' WHERE section='personal_channel'")
    conn.commit()
    assert repo.pair_receipts_require_new_scope(42, {"auth_scope": SCOPE}, now=111) is False

    conn.execute("UPDATE entity_detail_sections SET status='fresh' WHERE section='personal_channel'")
    conn.commit()
    monkeypatch.setattr(
        repo,
        "_parse_validated_receipt",
        lambda *args, **kwargs: SimpleNamespace(outcome="usable", observation_started_at=100),
    )
    assert repo._reusable_receipt_is_valid(42, "full_profile", identity={"auth_scope": SCOPE}, now=111) is False
    conn.close()


def test_eligible_pair_with_missing_identity_column_fails_closed(tmp_path: Path) -> None:
    conn, repo = _repo(tmp_path / "missing-column.sqlite")
    conn.execute("ALTER TABLE entity_detail_sections DROP COLUMN acquisition_identity_json")
    conn.commit()
    assert repo.pair_receipts_require_new_scope(42, {"auth_scope": SCOPE}, now=101) is True
    conn.close()


@pytest.mark.asyncio
async def test_service_accepts_same_scope_at_ttl_boundary_without_db_changes(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "service-same-scope.sqlite", migrated=True)
    service = raw_service
    assert isinstance(service, DaemonEntityInfoService)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    cached = service._profiles.read(42, now=100)
    assert cached is not None

    before = tuple(
        tuple(conn.execute(f"SELECT * FROM {table} WHERE entity_id=42").fetchall())
        for table in ("entity_details", "entity_detail_sections", "entity_profile_refresh_state")
    )
    assert service._profile_scope_changed(cached, 42, now=400) is False
    after = tuple(
        tuple(conn.execute(f"SELECT * FROM {table} WHERE entity_id=42").fetchall())
        for table in ("entity_details", "entity_detail_sections", "entity_profile_refresh_state")
    )

    assert after == before
    await service.shutdown()
    conn.close()
