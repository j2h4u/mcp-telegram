"""SDK adapters preserve profile authority, delivery ordering and provenance."""

from __future__ import annotations

# pyright: reportAny=false
import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl import types

from mcp_telegram.delivered_update import DeliveredUpdate, delivered_update_context
from mcp_telegram.entity_profile.collection_repository import store_semantic_patch
from mcp_telegram.entity_profile.contracts import (
    ObservationBoundary,
    ProfileFieldObservation,
    ProfileSemanticSnapshot,
    SuppliedProfileUser,
)
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.entity_profile.updates import ProfilePush, project_profile_update, project_user_profile
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from sqlite_writer_helpers import event_manager


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "adapters.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES(42,'user','Before','before',1)")
        conn.execute("INSERT INTO dialogs(dialog_id,type,name,username) VALUES(42,'user','Before','before')")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES(42,1,'explicit',1)"
        )
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES(42,?,1,7)",
            (
                json.dumps(
                    {
                        "schema": 1,
                        "id": 42,
                        "type": "user",
                        "name": "Before",
                        "username": "before",
                        "current_photo": {"photo_id": 8, "date": 1},
                    }
                ),
            ),
        )
    try:
        yield conn
    finally:
        conn.close()


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _semantic(db: sqlite3.Connection) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    row = db.execute(
        "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=7 AND entity_id=42"
    ).fetchone()
    assert row is not None
    return _mapping(json.loads(row[0])), _mapping(json.loads(row[1])), _mapping(json.loads(row[2]))


def _seed(db: sqlite3.Connection, push: ProfilePush | None, roots: set[str] | None = None) -> None:
    assert push is not None
    fields = {key: value for key, value in push.semantic_fields.items() if roots is None or key in roots}
    coverage = {
        path: value
        for path, value in push.semantic_coverage.items()
        if any(path == root or path.startswith((root + ".", root + "[")) for root in fields)
    }
    snapshot = ProfileSemanticSnapshot(42, 7, fields, coverage, ObservationBoundary(1, 1), endpoint="supplied.User")
    with write_transaction(db):
        store_semantic_patch(db, snapshot, ProfileFieldObservation(7, 1, 1, "rpc"))


def _events(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return cast(
        list[tuple[str, str]],
        db.execute("SELECT change_key,source FROM entity_profile_change_events ORDER BY event_id").fetchall(),
    )


async def test_story_blocklist_selector_preserves_independent_block_and_rejects_replay(db: sqlite3.Connection) -> None:
    _seed(db, project_profile_update(types.UpdatePeerBlocked(types.PeerUser(42), blocked=True)))
    _seed(
        db,
        project_profile_update(
            types.UpdatePeerBlocked(types.PeerUser(42), blocked=False, blocked_my_stories_from=True)
        ),
    )
    client = MagicMock()
    manager = event_manager(client, db, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    for order, blocked, expected in ((2, True, True), (3, False, False), (2, True, False), (3, False, False)):
        update = types.UpdatePeerBlocked(types.PeerUser(42), blocked=blocked, blocked_my_stories_from=True)
        with delivered_update_context(DeliveredUpdate(order, float(order), order)):
            await manager.on_raw_identity_or_notify(update)
        fields, coverage, provenance = _semantic(db)
        assert fields["full_user.blocked"] is True
        assert fields["full_user.blocked_my_stories_from"] is expected
        assert _mapping(coverage["full_user.blocked_my_stories_from"])["classification"] == "private_fact"
    assert _mapping(_mapping(provenance["fields"])["full_user.blocked_my_stories_from"])["order"] == 3
    assert _events(db) == []
    assert db.execute("SELECT count(*) FROM entity_profile_refresh_state").fetchone() == (0,)
    client.assert_not_called()


@pytest.mark.parametrize("parent_source,photo_source", [("rpc", "rpc"), ("min", "update")])
async def test_min_authoritative_photo_keeps_actual_parent_source(
    db: sqlite3.Connection, parent_source: str, photo_source: str
) -> None:
    _seed(db, project_user_profile(types.User(42, photo=types.UserProfilePhoto(8, 2))), {"user.photo"})
    user = types.User(42, min=True, apply_min_photo=True, first_name="Partial", photo=types.UserProfilePhoto(9, 2))
    push = project_user_profile(user)
    assert push is not None
    supplied = SuppliedProfileUser(
        42, push.semantic_fields, push.semantic_coverage, push.fields, push.minimal, push.authoritative_photo
    )
    assert supplied.minimal and supplied.authoritative_photo
    client = MagicMock()
    if parent_source == "rpc":
        profiles = EntityProfileRepository(db, section_ttl_seconds=300)
        with write_transaction(db):
            profiles.apply_supplied_user(
                supplied, observation=ProfileFieldObservation(7, 20, 20, "rpc"), boundary=ObservationBoundary(20, 21)
            )
    else:
        manager = event_manager(client, db, asyncio.Event())
        manager.set_self_id(7)
        manager.bind_demand_sink(MagicMock())
        with delivered_update_context(DeliveredUpdate(20, 20.0, 20)):
            await manager._persist_profile_pushes(sender=user)
    fields, _coverage, provenance = _semantic(db)
    assert _mapping(fields["user.photo"])["photo_id"] == 9
    evidence = _mapping(provenance["fields"])
    assert _mapping(evidence["user.photo.photo_id"])["source"] == photo_source
    assert _mapping(evidence["user.first_name"])["source"] == "min"
    assert db.execute(
        "SELECT source FROM entity_profile_field_observations WHERE entity_id=42 AND field='current_photo'"
    ).fetchone() == (photo_source,)
    assert [(key, source) for key, source in _events(db) if key == "photo.current"] == [("photo.current", photo_source)]
    client.assert_not_called()


async def test_retained_delivery_identity_fence_masks_semantic_roots_and_coverage(db: sqlite3.Connection) -> None:
    identity_roots = {"user.first_name", "user.last_name", "user.username", "user.usernames"}
    _seed(
        db,
        project_user_profile(
            types.User(
                42,
                first_name="New",
                last_name="",
                username="new",
                usernames=[types.Username("new", active=True)],
                premium=False,
            )
        ),
        identity_roots | {"user.premium"},
    )
    with write_transaction(db):
        db.execute("UPDATE entities SET name='New',username='new' WHERE id=42")
        db.execute("UPDATE dialogs SET name='New',username='new' WHERE dialog_id=42")
        db.execute(
            "UPDATE entity_details SET detail_json=? WHERE entity_id=42",
            (
                json.dumps(
                    {
                        "schema": 1,
                        "id": 42,
                        "type": "user",
                        "name": "New",
                        "first_name": "New",
                        "last_name": "",
                        "username": "new",
                        "extra_usernames": [],
                    }
                ),
            ),
        )
        db.executemany(
            "INSERT INTO dialog_fact_observations(dialog_id,field,observation_order) VALUES(42,?,3)",
            [("name",), ("username",)],
        )
    before_fields, before_coverage, before_provenance = _semantic(db)
    client = MagicMock()
    manager = event_manager(client, db, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    user = types.User(
        42, first_name="Old", last_name="", username="old", usernames=[types.Username("old", active=True)], premium=True
    )
    with delivered_update_context(DeliveredUpdate(2, 2.0, 2)):
        await manager._persist_profile_pushes(sender=user)
    fields, coverage, provenance = _semantic(db)
    delivered_coverage = _mapping(_mapping(provenance["supplied_observation"])["coverage"])
    assert not any(
        path == root or path.startswith((root + ".", root + "["))
        for path in delivered_coverage
        for root in identity_roots
    )
    for root in identity_roots:
        assert fields[root] == before_fields[root]
        paths = {path for path in before_coverage if path == root or path.startswith((root + ".", root + "["))}
        assert {path: coverage[path] for path in paths} == {path: before_coverage[path] for path in paths}
        for path in paths:
            if path in _mapping(before_provenance["fields"]):
                assert _mapping(provenance["fields"])[path] == _mapping(before_provenance["fields"])[path]
    assert fields["user.premium"] is True
    assert db.execute("SELECT name,username FROM entities WHERE id=42").fetchone() == ("New", "new")
    assert db.execute("SELECT name,username FROM dialogs WHERE dialog_id=42").fetchone() == ("New", "new")
    for key, _source in _events(db):
        assert key not in {"legacy.name", "legacy.username"}
        assert not any(key == root or key.startswith((root + ".", root + "[")) for root in identity_roots)
    assert db.execute(
        "SELECT observation_order FROM dialog_fact_observations WHERE dialog_id=42 AND field='name'"
    ).fetchone() == (3,)
    client.assert_not_called()
