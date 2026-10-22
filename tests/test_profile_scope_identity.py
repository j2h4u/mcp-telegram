"""Authorization scope comparison for durable FullUser pair receipts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
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


def test_reusable_pair_receipt_stops_at_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, repo = _repo(tmp_path / "eligibility.sqlite")
    monkeypatch.setattr(
        repo,
        "_parse_validated_receipt",
        lambda *args, **kwargs: SimpleNamespace(outcome="usable", observation_started_at=100),
    )
    assert repo._reusable_receipt_is_valid(42, "full_profile", identity={"auth_scope": SCOPE}, now=111) is False
    conn.close()


@pytest.mark.asyncio
async def test_service_accepts_same_scope_at_ttl_boundary_without_db_changes(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "service-same-scope.sqlite", migrated=True)
    service = raw_service
    assert isinstance(service, DaemonEntityInfoService)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    service._deps = replace(
        service._deps,
        now_provider=lambda: 400.0,
        full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100),
    )

    try:
        before = tuple(
            tuple(conn.execute(f"SELECT * FROM {table} WHERE entity_id=42").fetchall())
            for table in ("entity_details", "entity_detail_sections", "entity_profile_refresh_state")
        )
        result = await service.get_entity_info({"entity_id": 42})
        after = tuple(
            tuple(conn.execute(f"SELECT * FROM {table} WHERE entity_id=42").fetchall())
            for table in ("entity_details", "entity_detail_sections", "entity_profile_refresh_state")
        )

        assert result["ok"] is True
        data = result["data"]
        assert isinstance(data, dict)
        assert data["id"] == 42
        assert after == before
    finally:
        await service.shutdown()
        conn.close()
