"""Collection User sidecars keep owner aliases separate from public names."""

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.contacts.state import read_contact_command_state
from mcp_telegram.entity_profile.collection_repository import store_semantic_snapshot
from mcp_telegram.entity_profile.contracts import (
    ObservationBoundary,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldObservation,
    ProfileFieldState,
    ProfileSemanticSnapshot,
    UserReference,
)
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "min", "min_none", "min_false", "empty"])
@pytest.mark.parametrize("public_present", [True, False])
async def test_collection_user_keeps_public_name_and_correct_owner_alias(
    tmp_path: Path, kind: str, public_present: bool
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    evidence = ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True)
    fields = {
        "user.first_name": "Public",
        "user.contact": True,
        "owner.saved_contact_name": {"first_name": "Old alias", "last_name": ""},
    }
    if not public_present:
        fields.pop("user.first_name")
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (42,'user','Display',1)")
        conn.execute(
            "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,generation) VALUES(42,'pending',100,'avatar_history',1)"
        )
        snapshot = ProfileSemanticSnapshot(42, 77, fields, dict.fromkeys(fields, evidence), ObservationBoundary(1, 1))
        assert store_semantic_snapshot(conn, snapshot, 1) is not None
    user: types.User | types.UserEmpty
    flags: dict[str, bool | None] = {"full": True, "min": True, "min_none": None, "min_false": False}
    user = (
        types.UserEmpty(42)
        if kind == "empty"
        else types.User(42, contact=flags[kind], min=kind != "full", first_name="New alias")
    )

    class Client:
        async def __call__(self, request: object) -> object:
            assert type(request).__name__ == "GetUserPhotosRequest"
            return types.photos.Photos([], [user])

    page = await TelethonUserProfileGateway(Client(), now_provider=lambda: 100).fetch_profile_collection_page(
        UserReference(42, 1), "avatar_history", {}
    )
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_transaction(conn):
        cursor = repo.next_due_refresh(now=100)
        assert cursor is not None
        assert repo.commit_section(
            cursor,
            EntitySectionCommit(
                {}, collection_page=page, field_observation=ProfileFieldObservation(77, 100, 100, "rpc")
            ),
            now=100,
        )
    row = cast(
        tuple[str],
        conn.execute(
            "SELECT fields_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=77 AND entity_id=42"
        ).fetchone(),
    )
    retained = cast(dict[str, object], json.loads(row[0]))
    if public_present:
        assert retained["user.first_name"] == "Public"
    else:
        assert "user.first_name" not in retained
    state = read_contact_command_state(conn, 77, 42)
    assert state["first_name"] == ("New alias" if kind == "full" else "Old alias")
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_profile_change_events WHERE change_key='user.first_name'"
    ).fetchone() == (0,)
    conn.close()
