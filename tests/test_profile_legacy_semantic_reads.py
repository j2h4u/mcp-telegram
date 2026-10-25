"""Actual v86 migration markers cannot block retained User/Bot profile reads."""

import json
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from jsonschema import Draft202012Validator

import mcp_telegram.sync_db as sync_db
from mcp_telegram import server
from mcp_telegram.entity_profile.refresh import RefreshLimits
from mcp_telegram.tools import entity_info
from tests.test_entity_profile_progressive import _test_service
from tests.test_profile_read_integration import registered_validator as registered_validator


@pytest.mark.parametrize("kind", ["user", "bot"])
async def test_migrated_legacy_profile_default_read_preserves_facts_without_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registered_validator: Draft202012Validator,
    kind: Literal["user", "bot"],
) -> None:
    path = tmp_path / "legacy.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 86)
        sync_db.ensure_sync_schema(path)
    retained = {
        "schema": 1,
        "id": 42,
        "type": kind,
        "name": "Retained",
        "about": "Retained biography",
        "note": " 💬 retained note ",
        "bot": kind == "bot",
    }
    with closing(sqlite3.connect(path)) as seed:
        seed.execute("INSERT INTO entities(id,type,name,updated_at) VALUES(42,?,'Retained',10)", (kind,))
        seed.execute("INSERT INTO dialogs(dialog_id,type,name,archived,hidden) VALUES(42,?,'Retained',1,1)", (kind,))
        seed.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES(42,?,100,77)",
            (json.dumps(retained),),
        )
        seed.execute(
            "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,normalization_version) VALUES(42,'full_profile','fresh',100,'entity-profile-full-user-v1')"
        )
        seed.commit()
    sync_db.ensure_sync_schema(path)
    reader = sync_db.open_sync_db_reader(path)
    service = _test_service(reader, limits=RefreshLimits(), account_id=77)
    monkeypatch.setattr(service, "_write_profiles", AsyncMock(side_effect=AssertionError("read queued writer")))
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    monkeypatch.setattr(coordinator, "enqueue", MagicMock(side_effect=AssertionError("read enrolled profile")))
    service.bind_demand_sink(MagicMock(offer=MagicMock(side_effect=AssertionError("read offered acquisition"))))

    @asynccontextmanager
    async def connection():
        async def get_entity_info(**kwargs: object):
            return await service.get_entity_info(kwargs)

        yield SimpleNamespace(get_entity_info=get_entity_info)

    monkeypatch.setattr(entity_info, "_bounded_daemon_connection", connection)
    try:
        assert reader.execute(
            "SELECT fields_json,coverage_json,provenance_json,source FROM entity_profile_semantic_snapshots WHERE owner_account_id=77 AND entity_id=42"
        ).fetchone() == ("{}", '{"reason":"legacy_normalization_incomplete"}', "{}", "legacy")
        tool = await entity_info.get_entity_info(entity_info.GetEntityInfo(exact_entity_id=42))
        assert not tool.is_error
        result = server._project_tool_result("get_entity_info", tool, server._CallTelemetry())
        wire = cast(dict, result.structured_content)
        registered_validator.validate(wire)
        assert result.content == [] and not result.is_error
        assert wire["type"] == kind and wire["display_name"] == "Retained"
        assert wire["common"]["about"]["content"]["text"] == "Retained biography"
        assert wire["completeness"] == "partial"
        assert wire["semantic"]["fields"] == {} and wire["semantic"]["field_evidence"] == []
        assert wire["semantic"]["complete"] is False
        assert wire["semantic"]["availability"] == "not_observed"
        assert wire["semantic"]["acquisition"] == {"availability": "not_observed"}
        assert reader.total_changes == 0
        invalid = await service.get_entity_info({"entity_id": 42, "section": "history", "cursor": "tampered"})
        assert invalid["ok"] is False and invalid["error"] == "invalid_profile_navigation"
    finally:
        await service.shutdown()
        reader.close()
