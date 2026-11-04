"""TG-220: actual activity-search sidecars through the durable archive owner."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon import types

from mcp_telegram import activity_sync
from mcp_telegram.activity_sync import ArchiveIncrementalDemandAdapter
from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.entity_profile.collection_repository import store_semantic_snapshot
from mcp_telegram.entity_profile.contracts import (
    ObservationBoundary,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldState,
    ProfileSemanticSnapshot,
)
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction
from tests.test_activity_sync import FakeSearchResult, _FakeClient, _selected_budget


@pytest.mark.parametrize("kind", ["full_contact", "min"])
async def test_activity_search_sidecar_updates_entity_snapshot(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "activity.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    now = int(time.time())

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=99 WHERE singleton=1")
            conn.execute(
                "INSERT INTO entities(id,type,name,username,name_normalized,updated_at) VALUES (55,'user','Public','public','public',1)"
            )
            evidence = ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True)
            assert (
                store_semantic_snapshot(
                    conn,
                    ProfileSemanticSnapshot(
                        55, 99, {"user.first_name": "Public"}, {"user.first_name": evidence}, ObservationBoundary(1, 1)
                    ),
                    1,
                )
                is not None
            )
            conn.execute(
                "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES ('backfill_complete','1'),('last_sync_at',?)",
                (str(now - 7200),),
            )

    await writer.run(seed)
    reader = open_sync_db_reader(path)
    user = types.User(
        55,
        access_hash=555,
        first_name="Contact alias" if kind == "full_contact" else "Partial",
        contact=kind == "full_contact",
        min=kind == "min",
    )
    message = types.Message(
        id=12, peer_id=types.PeerUser(55), date=datetime.fromtimestamp(now - 100, tz=UTC), message="fixture", out=True
    )
    response = types.messages.Messages(messages=[message], topics=[], chats=[], users=[user])
    client = _FakeClient([cast(FakeSearchResult, response)])
    scope = ScopeCell()
    assert scope.publish(TelegramAuthScope(1, 99, 1, 1))
    adapter = ArchiveIncrementalDemandAdapter(
        client,
        reader,
        asyncio.Event(),
        3600.0,
        1.0,
        writer=writer,
        account_id=99,
        capture_scope=scope.capture,
        claim_scope=scope.claim,
    )
    try:
        await adapter.run_slice(_selected_budget(adapter))
        assert reader.execute("SELECT name,username FROM entities WHERE id=55").fetchone() == (
            "Contact alias" if kind == "full_contact" else "Partial",
            "public" if kind == "min" else None,
        )
    finally:
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        row = cast(
            tuple[str],
            reopened.execute(
                "SELECT fields_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=99 AND entity_id=55"
            ).fetchone(),
        )
        assert json.loads(row[0])["user.first_name"] == "Public"
        assert reopened.execute("SELECT name,username FROM entities WHERE id=55").fetchone() == (
            "Contact alias" if kind == "full_contact" else "Partial",
            "public" if kind == "min" else None,
        )


async def test_activity_scope_revoked_during_publication_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "activity-revoked.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    now = int(time.time())

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=99 WHERE singleton=1")
            conn.execute(
                "INSERT INTO entities(id,type,name,username,updated_at) VALUES (55,'user','Before','before',1)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES ('backfill_complete','1'),('last_sync_at',?)",
                (str(now - 7200),),
            )

    await writer.run(seed)
    reader = open_sync_db_reader(path)
    scope = ScopeCell()
    auth = TelegramAuthScope(1, 99, 1, 1)
    assert scope.publish(auth)
    message = types.Message(
        id=12, peer_id=types.PeerUser(55), date=datetime.fromtimestamp(now - 100, tz=UTC), message="fixture", out=True
    )
    response = types.messages.Messages(
        messages=[message], topics=[], chats=[], users=[types.User(55, first_name="After")]
    )
    client = _FakeClient([cast(FakeSearchResult, response)])
    adapter = ArchiveIncrementalDemandAdapter(
        client,
        reader,
        asyncio.Event(),
        3600.0,
        1.0,
        writer=writer,
        account_id=99,
        capture_scope=scope.capture,
        claim_scope=scope.claim,
    )
    original = activity_sync._upsert_entities_from_search

    def revoke_after_write(
        conn: sqlite3.Connection, result: activity_sync._SearchResultLike, *, observation_order: int, observed_at: int
    ) -> None:
        original(conn, result, observation_order=observation_order, observed_at=observed_at)
        change = scope.begin_change()
        scope.end_change(change)
        assert scope.publish(auth)

    monkeypatch.setattr(activity_sync, "_upsert_entities_from_search", revoke_after_write)
    try:
        await adapter.run_slice(_selected_budget(adapter))
        assert reader.execute("SELECT name,username FROM entities WHERE id=55").fetchone() == ("Before", "before")
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    finally:
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT name,username FROM entities WHERE id=55").fetchone() == ("Before", "before")
