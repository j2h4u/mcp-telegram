"""Telegram-delivered private profile facts are materialized without RPCs."""

# pyright: reportAny=false

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.entity_profile.contracts import ProfileFieldObservation
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.entity_profile.updates import project_profile_update, project_user_profile, push_marker
from mcp_telegram.event_handlers import EventHandlerManager
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from sqlite_writer_helpers import event_manager


@pytest.fixture
def database(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "profile.sqlite"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (42,'user','Before',1)")
        conn.execute("INSERT INTO dialogs(dialog_id,type,name) VALUES (42,'user','Before')")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (42,1,'explicit',1)"
        )
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (42,?,1,7)",
            (json.dumps({"schema": 1, "id": 42, "type": "user", "name": "Before", "about": "Retained"}),),
        )
    yield conn
    conn.close()


def _manager(conn: sqlite3.Connection) -> tuple[EventHandlerManager, MagicMock]:
    client = MagicMock()
    manager = event_manager(client, conn, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    return manager, client


def _detail(conn: sqlite3.Connection) -> dict[str, object]:
    row = conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone()
    return cast(dict[str, object], json.loads(row[0]))


def _bundle(update: object, user: types.User) -> None:
    cast(types.UpdateUserStatus, update).__dict__["_entities"] = {user.id: user}


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        (types.UpdateUserPhone(42, ""), {"phone": None}),
        (types.UpdateUserPhone(42, "123"), {"phone": "123"}),
        (types.UpdateUserStatus(42, types.UserStatusEmpty()), {"status": None}),
        (types.UpdateUserEmojiStatus(42, types.EmojiStatusEmpty()), {"emoji_status_id": None}),
        (types.UpdatePeerBlocked(types.PeerUser(42), blocked=True), {"blocked": True}),
        (types.UpdatePeerBlocked(types.PeerUser(42)), {"blocked": False}),
        (types.UpdatePeerBlocked(types.PeerUser(42), blocked_my_stories_from=True), {}),
        (types.UpdatePeerHistoryTTL(types.PeerUser(42)), {"ttl_period": None}),
        (types.UpdatePeerHistoryTTL(types.PeerUser(42), ttl_period=60), {"ttl_period": 60}),
    ],
)
def test_sparse_updates_project_only_supplied_facts(update: object, expected: dict[str, object]) -> None:
    projected = project_profile_update(update)
    assert projected is not None
    assert dict(projected.fields) == expected


def test_full_and_min_user_projection_have_distinct_authority() -> None:
    full = project_user_profile(types.User(42, first_name="After", photo=types.UserProfilePhoto(9, 2)))
    assert full is not None
    assert full.fields["current_photo"] == {"photo_id": 9, "date": None}
    assert full.fields["contact"] is False
    assert full.fields["phone"] is None
    minimal = project_user_profile(types.User(42, min=True, first_name="Partial", contact=True))
    assert minimal is not None and minimal.minimal
    assert "phone" not in minimal.fields
    assert "contact" not in minimal.fields
    assert "current_photo" not in minimal.fields


@pytest.mark.asyncio
async def test_status_phone_emoji_and_block_are_canonical_without_rpc(database: sqlite3.Connection) -> None:
    manager, client = _manager(database)
    updates = (
        types.UpdateUserStatus(42, types.UserStatusRecently()),
        types.UpdateUserPhone(42, "123"),
        types.UpdateUserEmojiStatus(42, types.EmojiStatus(99)),
        types.UpdatePeerBlocked(types.PeerUser(42), blocked=True),
        types.UpdatePeerHistoryTTL(types.PeerUser(42), ttl_period=60),
    )
    for update in updates:
        await manager.on_raw_identity_or_notify(update)
    detail = _detail(database)
    assert detail["status"] == {"type": "recently"}
    assert detail["phone"] == "123"
    assert detail["emoji_status_id"] == 99
    assert detail["blocked"] is True
    assert detail["ttl_period"] == 60
    assert detail["about"] == "Retained"
    assert database.execute("SELECT profile_revision FROM entity_details").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM conversation_history_events").fetchone()[0] == 0
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_reused_bundle_cannot_revert_explicit_status(database: sqlite3.Connection) -> None:
    manager, client = _manager(database)
    user = types.User(42, first_name="After", status=types.UserStatusRecently())
    first = types.UpdateUserStatus(42, types.UserStatusOnline(datetime(2030, 1, 1, tzinfo=UTC)))
    second = types.UpdateUserPhone(42, "123")
    _bundle(first, user)
    _bundle(second, user)
    await manager.on_raw_identity_or_notify(first)
    status = _detail(database)["status"]
    await manager.on_raw_identity_or_notify(second)
    assert _detail(database)["status"] == status
    assert _detail(database)["phone"] == "123"
    assert database.execute("SELECT name FROM entities WHERE id=42").fetchone()[0] == "After"
    assert database.execute("SELECT name FROM dialogs WHERE dialog_id=42").fetchone()[0] == "After"
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_new_user_bundle_clears_authorized_photo_and_retains_unreturned_phone(
    database: sqlite3.Connection,
) -> None:
    manager, _ = _manager(database)
    first = types.UpdateUserPhone(42, "123")
    _bundle(first, types.User(42, first_name="Peer", photo=types.UserProfilePhoto(9, 2), premium=True))
    await manager.on_raw_identity_or_notify(first)
    assert _detail(database)["current_photo"] == {"photo_id": 9, "date": None}
    second = types.UpdateUserStatus(42, types.UserStatusEmpty())
    _bundle(second, types.User(42, first_name="Peer", photo=types.UserProfilePhotoEmpty()))
    await manager.on_raw_identity_or_notify(second)
    detail = _detail(database)
    assert detail["current_photo"] is None
    assert detail["phone"] == "123"
    assert detail["premium"] is False
    assert detail.get("status") is None
    assert (
        database.execute(
            "SELECT status FROM entity_detail_sections WHERE entity_id=42 AND section='avatar_history'"
        ).fetchone()[0]
        == "pending"
    )


@pytest.mark.asyncio
async def test_min_user_keeps_known_authoritative_values_but_apply_min_photo_updates(
    database: sqlite3.Connection,
) -> None:
    manager, _ = _manager(database)
    await manager.on_raw_identity_or_notify(types.UpdateUserPhone(42, "trusted"))
    update = types.UpdateUserStatus(42, types.UserStatusRecently())
    _bundle(update, types.User(42, min=True, first_name="Wrong", phone="wrong", photo=types.UserProfilePhoto(9, 2)))
    await manager.on_raw_identity_or_notify(update)
    assert _detail(database)["phone"] == "trusted"
    assert _detail(database)["name"] == "Before"
    photo_update = types.UpdateUserStatus(42, types.UserStatusEmpty())
    _bundle(photo_update, types.User(42, min=True, apply_min_photo=True, photo=types.UserProfilePhotoEmpty()))
    await manager.on_raw_identity_or_notify(photo_update)
    assert _detail(database)["current_photo"] is None


@pytest.mark.asyncio
async def test_status_burst_does_not_starve_older_full_profile_result(database: sqlite3.Connection) -> None:
    manager, _ = _manager(database)
    repository = EntityProfileRepository(database, section_ttl_seconds=300)
    repository.mark_pending(42, now=100)
    cursor = repository.next_due_refresh(now=100)
    assert cursor is not None
    with write_transaction(database):
        order = allocate_observation_order(database)
    for _ in range(20):
        await manager.on_raw_identity_or_notify(types.UpdateUserStatus(42, types.UserStatusRecently()))
    assert repository.commit_section(
        cursor,
        EntitySectionCommit(
            {"about": "New full result", "status": {"type": "offline"}},
            field_observation=ProfileFieldObservation(7, order, 100, "rpc"),
            observation_owner_account_id=7,
            ownership_observed=True,
        ),
        now=101,
    )
    assert _detail(database)["about"] == "New full result"
    assert _detail(database)["status"] == {"type": "recently"}


@pytest.mark.asyncio
async def test_rollback_does_not_annotate_bundle_or_lose_retry(database: sqlite3.Connection) -> None:
    manager, _ = _manager(database)
    user = types.User(42, first_name="After")
    update = types.UpdateUserPhone(42, "123")
    _bundle(update, user)
    database.execute(
        "CREATE TRIGGER reject_push BEFORE INSERT ON entity_profile_field_observations "
        "BEGIN SELECT RAISE(ABORT,'push rollback'); END"
    )
    before = _detail(database)
    await manager.on_raw_identity_or_notify(update)
    assert _detail(database) == before
    assert push_marker(user, manager._profile_push_owner) is None
    database.execute("DROP TRIGGER reject_push")
    await manager.on_raw_identity_or_notify(update)
    assert _detail(database)["name"] == "After"
    assert _detail(database)["phone"] == "123"


@pytest.mark.asyncio
async def test_non_enrolled_user_and_other_owner_are_not_materialized(database: sqlite3.Connection) -> None:
    manager, client = _manager(database)
    with write_transaction(database):
        database.execute("UPDATE full_history_enrollment SET enabled=0 WHERE dialog_id=42")
    await manager.on_raw_identity_or_notify(types.UpdateUserPhone(42, "ignored"))
    assert "phone" not in _detail(database)
    with write_transaction(database):
        database.execute("UPDATE full_history_enrollment SET enabled=1 WHERE dialog_id=42")
        database.execute("UPDATE entity_details SET profile_owner_account_id=8 WHERE entity_id=42")
    await manager.on_raw_identity_or_notify(types.UpdateUserPhone(42, "ignored"))
    assert "phone" not in _detail(database)
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_first_owned_push_discards_unowned_private_blob_but_survives_older_rpc(
    database: sqlite3.Connection,
) -> None:
    manager, _ = _manager(database)
    with write_transaction(database):
        database.execute("UPDATE entity_details SET profile_owner_account_id=NULL WHERE entity_id=42")
        order = allocate_observation_order(database)
    repository = EntityProfileRepository(database, section_ttl_seconds=300)
    repository.mark_pending(42, now=100)
    cursor = repository.next_due_refresh(now=100)
    assert cursor is not None
    await manager.on_raw_identity_or_notify(types.UpdateUserPhone(42, "new owned phone"))
    assert "about" not in _detail(database)
    assert _detail(database)["phone"] == "new owned phone"
    assert database.execute("SELECT profile_owner_account_id FROM entity_details").fetchone()[0] == 7
    assert repository.commit_section(
        cursor,
        EntitySectionCommit(
            {"about": "Owned result", "phone": "older phone"},
            field_observation=ProfileFieldObservation(7, order, 100, "rpc"),
            observation_owner_account_id=7,
            ownership_observed=True,
        ),
        now=101,
    )
    assert _detail(database)["about"] == "Owned result"
    assert _detail(database)["phone"] == "new owned phone"


@pytest.mark.asyncio
async def test_generic_user_change_requests_targeted_acquisition_without_rpc(database: sqlite3.Connection) -> None:
    manager, client = _manager(database)
    update = types.UpdateUser(42)
    await manager.on_raw_identity_or_notify(update)
    sections = database.execute("SELECT section,status FROM entity_detail_sections ORDER BY section").fetchall()
    assert [tuple(row) for row in sections] == [
        (section, "pending")
        for section in sorted(
            (
                "full_profile",
                "personal_channel",
                "avatar_history",
                "common_chats",
                "saved_music",
                "saved_gifts",
                "current_stories",
                "pinned_stories",
            )
        )
    ]
    state = database.execute("SELECT generation FROM entity_profile_refresh_state").fetchone()[0]
    await manager.on_raw_identity_or_notify(update)
    assert database.execute("SELECT generation FROM entity_profile_refresh_state").fetchone()[0] == state
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_repeated_unchanged_photo_does_not_restart_pending_acquisition(database: sqlite3.Connection) -> None:
    manager, _ = _manager(database)
    update = types.UpdateUserStatus(42, types.UserStatusRecently())
    _bundle(update, types.User(42, first_name="Peer", photo=types.UserProfilePhoto(9, 2)))
    await manager.on_raw_identity_or_notify(update)
    generation = database.execute("SELECT generation FROM entity_profile_refresh_state").fetchone()[0]
    for _ in range(3):
        repeated = types.UpdateUserStatus(42, types.UserStatusRecently())
        _bundle(repeated, types.User(42, first_name="Peer", photo=types.UserProfilePhoto(9, 2)))
        await manager.on_raw_identity_or_notify(repeated)
    assert database.execute("SELECT generation FROM entity_profile_refresh_state").fetchone()[0] == generation


def test_profile_handler_precedes_message_handler(database: sqlite3.Connection) -> None:
    manager, client = _manager(database)
    manager.register()
    handlers = [call.args[0] for call in client.add_event_handler.call_args_list]
    assert handlers.index(manager.on_raw_identity_or_notify) < handlers.index(manager.on_new_message)


@pytest.mark.asyncio
async def test_first_message_user_is_materialized_after_new_dm_admission(database: sqlite3.Connection) -> None:
    manager, client = _manager(database)
    user = types.User(43, first_name="New peer", phone="123", premium=True)
    assert await manager._auto_enroll_dm(43, user)
    row = database.execute(
        "SELECT detail_json,profile_owner_account_id FROM entity_details WHERE entity_id=43"
    ).fetchone()
    assert row is not None and row[1] == 7
    detail = json.loads(row[0])
    assert detail["phone"] == "123"
    assert detail["premium"] is True
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_failed_optional_profile_does_not_block_new_dm_admission(database: sqlite3.Connection) -> None:
    manager, _ = _manager(database)
    database.execute(
        "CREATE TRIGGER reject_push BEFORE INSERT ON entity_profile_field_observations "
        "BEGIN SELECT RAISE(ABORT,'push rollback'); END"
    )
    assert await manager._auto_enroll_dm(43, types.User(43, first_name="New peer"))
    assert 43 in manager._synced_dialog_ids
    assert database.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id=43").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_explicit_peer_invalidation_coalesces_bundled_photo_trigger(database: sqlite3.Connection) -> None:
    manager, client = _manager(database)
    update = types.UpdateUser(42)
    _bundle(update, types.User(42, first_name="Peer", photo=types.UserProfilePhoto(9, 2)))
    await manager.on_raw_identity_or_notify(update)
    assert database.execute("SELECT generation FROM entity_profile_refresh_state").fetchone()[0] == 1
    fences = database.execute(
        "SELECT field FROM entity_profile_field_observations WHERE field LIKE '__invalidation__:%'"
    ).fetchall()
    assert fences == [("__invalidation__:user",)]
    client.assert_not_called()
