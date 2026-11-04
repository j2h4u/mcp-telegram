"""TG-220: collection sidecar authority through the real writer."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram.contacts.state import read_contact_command_state
from mcp_telegram.dialog_fact_observations import claim_dialog_fact
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
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("kind", ["full", "min", "empty"])
async def test_collection_user_sidecar_preserves_authority_and_survives_restart(
    tmp_path: Path, kind: str, stale: bool
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        evidence = ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True)
        fields = {
            "user.first_name": "Public",
            "user.contact": True,
            "owner.saved_contact_name": {"first_name": "Old alias", "last_name": ""},
            "owner.saved_contact_phone_access": True,
            "user.phone": "15550000",
        }
        with write_transaction(conn):
            conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (42,'user','Display',1)")
            conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (88,'user','Old',1)")
            conn.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES (88,'Old','user')")
            if stale:
                assert claim_dialog_fact(conn, 88, "name", 200)
                conn.execute("UPDATE dialogs SET name='Newer' WHERE dialog_id=88")
            conn.execute(
                "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,generation) VALUES(42,'pending',100,'avatar_history',1)"
            )
            assert (
                store_semantic_snapshot(
                    conn,
                    ProfileSemanticSnapshot(42, 77, fields, dict.fromkeys(fields, evidence), ObservationBoundary(1, 1)),
                    1,
                )
                is not None
            )

    await writer.run(seed)
    user = (
        types.UserEmpty(42)
        if kind == "empty"
        else types.User(42, contact=True, min=kind == "min", first_name="New alias")
    )
    sidecar = types.User(88, access_hash=888, first_name="Other")

    class Client:
        async def __call__(self, request: object) -> object:
            assert isinstance(request, functions.photos.GetUserPhotosRequest)
            return types.photos.Photos([], [user, sidecar])

    page = await TelethonUserProfileGateway(Client(), now_provider=lambda: 100).fetch_profile_collection_page(
        UserReference(42, 1), "avatar_history", {}
    )

    def commit(conn: sqlite3.Connection) -> None:
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

    try:
        await writer.run(commit)
    finally:
        await writer.aclose()
    with closing(sqlite3.connect(path)) as conn:
        row = cast(
            tuple[str] | None,
            conn.execute(
                "SELECT fields_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=77 AND entity_id=42"
            ).fetchone(),
        )
        assert row is not None
        fields = cast(dict[str, object], json.loads(row[0]))
        assert fields["user.first_name"] == "Public"
        state = read_contact_command_state(conn, 77, 42)
        assert state["first_name"] == ("New alias" if kind == "full" else "Old alias")
        assert fields["user.phone"] == "15550000"
        assert state["phone_state"] == ("unavailable" if kind == "full" else "available")
        assert state["phone"] == (None if kind == "full" else "15550000")
        assert read_contact_command_state(conn, 78, 42)["membership"] is None
        assert conn.execute("SELECT name FROM dialogs WHERE dialog_id=88").fetchone() == (
            ("Newer" if stale else "Other"),
        )
