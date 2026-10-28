"""Journal and maintenance share the canonical writer, ownership and original order."""

import asyncio
import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.entity_profile.collection_repository import store_collection_page, store_story_update
from mcp_telegram.entity_profile.contracts import (
    ObservationBoundary,
    ProfileAcquisitionEvidence,
    ProfileCollectionItem,
    ProfileCollectionPage,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldObservation,
    ProfileFieldState,
    ProfileSemanticSnapshot,
    SuppliedProfileUser,
    TargetKind,
)
from mcp_telegram.entity_profile.full_user_normalization import normalize_full_user_response
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.entity_profile.semantic_normalization import normalize_semantic_profile
from mcp_telegram.entity_profile.updates import project_user_profile
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sync_db import _CURRENT_SCHEMA_VERSION, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from sqlite_writer_helpers import event_manager


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "history.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES(42,'user','Before',1)")
        conn.execute("INSERT INTO dialogs(dialog_id,type,name) VALUES(42,'user','Before')")
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
                        "current_photo": {"photo_id": 8, "date": 1},
                    }
                ),
            ),
        )
    yield conn
    conn.close()


def events(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    return cast(
        list[tuple[object, ...]],
        db.execute(
            "SELECT change_key,before_json,after_json,source,observation_order,event_id FROM entity_profile_change_events ORDER BY event_id"
        ).fetchall(),
    )


def one(db: sqlite3.Connection, query: str) -> tuple[object, ...]:
    value = cast(tuple[object, ...] | None, db.execute(query).fetchone())
    assert value is not None
    return value


def snapshot(fields: Mapping[str, object], order: int) -> ProfileSemanticSnapshot:
    return ProfileSemanticSnapshot(
        42,
        7,
        fields,
        {key: ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True) for key in fields},
        ObservationBoundary(order, order + 1),
    )


def commit_snapshot(db: sqlite3.Connection, fields: Mapping[str, object], order: int) -> bool:
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    with write_transaction(db):
        repo.request_section_refresh(42, ("full_profile",), now=order, pair_mode="disabled")
        cursor = repo.next_due_refresh(now=order)
        assert cursor is not None
        return repo.commit_section(
            cursor,
            EntitySectionCommit(
                {},
                semantic_snapshot=snapshot(fields, order),
                field_observation=ProfileFieldObservation(7, order, order, "rpc"),
                evidence=ProfileAcquisitionEvidence(cursor.generation, "usable"),
            ),
            now=order,
        )


@pytest.mark.asyncio
async def test_known_legacy_name_and_photo_changes_before_backfill_are_not_lost(db: sqlite3.Connection) -> None:
    client = MagicMock()
    manager = event_manager(client, db, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    update = types.UpdateUserName(42, "After", "", [])
    await manager.on_raw_identity_or_notify(update)
    await manager.on_raw_identity_or_notify(update)
    user = types.User(42, first_name="After", photo=types.UserProfilePhoto(9, 2))
    await manager._persist_profile_pushes(sender=user)
    selected = [row for row in events(db) if row[0] in {"legacy.name", "photo.current"}]
    assert [(row[0], json.loads(cast(str, row[1]))) for row in selected] == [
        ("legacy.name", "Before"),
        ("photo.current", {"photo_id": 8, "date": 1}),
    ]
    assert all(row[3] == "update" for row in selected)
    client.assert_not_called()


def test_first_exact_note_baseline_then_formatted_date_change_and_rollback(db: sqlite3.Connection) -> None:
    before = {
        "constructor": "TextWithEntities",
        "text": " date😀 ",
        "entities": [{"constructor": "MessageEntityFormattedDate", "offset": 1, "length": 6, "date": 100}],
    }
    after = {
        **before,
        "entities": [{"constructor": "MessageEntityFormattedDate", "offset": 1, "length": 6, "date": 200}],
    }
    assert commit_snapshot(db, {"full_user.note": before}, 10)
    assert events(db) == []
    assert commit_snapshot(db, {"full_user.note": after}, 20)
    row = events(db)[0]
    assert row[0] == "full_user.note"
    assert json.loads(cast(str, row[1])) == before
    assert json.loads(cast(str, row[2])) == after
    db.execute(
        "CREATE TRIGGER reject_history BEFORE INSERT ON entity_profile_change_events BEGIN SELECT RAISE(ABORT,'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        commit_snapshot(db, {"full_user.note": before}, 30)
    stored = one(db, "SELECT fields_json FROM entity_profile_semantic_snapshots")
    assert json.loads(cast(str, stored[0]))["full_user.note"] == after
    assert len(events(db)) == 1


def test_supplied_user_patch_retains_fulluser_acquisition_and_old_field_ages(db: sqlite3.Connection) -> None:
    assert commit_snapshot(db, {"full_user.about": "retained", "user.first_name": "Before", "user.last_name": None}, 10)
    before = one(
        db,
        "SELECT observation_started_at,observation_completed_at,source,observation_order FROM entity_profile_semantic_snapshots",
    )
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    push = project_user_profile(types.User(42, first_name="After", phone="123"))
    assert push is not None
    user = SuppliedProfileUser(42, push.semantic_fields, push.semantic_coverage, push.fields, False, False)
    page = ProfileCollectionPage(
        42, "avatar_history", (), ObservationBoundary(20, 21), terminal=True, supplied_user=user
    )
    with write_transaction(db):
        repo.request_section_refresh(42, ("avatar_history",), now=20, pair_mode="disabled")
        db.execute("UPDATE entity_profile_refresh_state SET next_section='avatar_history'")
        cursor = repo.next_due_refresh(now=20)
        assert cursor is not None
        assert repo.commit_section(
            cursor,
            EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(7, 20, 20, "rpc")),
            now=20,
        )
    after = one(
        db,
        "SELECT observation_started_at,observation_completed_at,source,observation_order FROM entity_profile_semantic_snapshots",
    )
    assert before == after
    facts, evidence = one(db, "SELECT fields_json,provenance_json FROM entity_profile_semantic_snapshots")
    assert json.loads(cast(str, facts))["full_user.about"] == "retained"
    assert json.loads(cast(str, evidence))["fields"]["full_user.about"]["order"] == 10
    assert json.loads(cast(str, facts))["user.phone"] == "123"
    assert any(row[0] == "user.first_name" for row in events(db))


@pytest.mark.asyncio
async def test_min_user_cannot_replace_full_name_or_emit_history(db: sqlite3.Connection) -> None:
    client = MagicMock()
    manager = event_manager(client, db, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    await manager._persist_profile_pushes(sender=types.User(42, first_name="Ada", last_name="Lovelace"))
    count = len(events(db))
    await manager._persist_profile_pushes(sender=types.User(42, min=True, first_name="Augusta"))
    assert len(events(db)) == count
    fields = one(db, "SELECT fields_json FROM entity_profile_semantic_snapshots")[0]
    assert json.loads(cast(str, fields))["user.first_name"] == "Ada"
    assert db.execute("SELECT name FROM entities WHERE id=42").fetchone()[0] == "Ada Lovelace"


def test_generic_invalidation_replay_cannot_revive_finished_work(db: sqlite3.Connection) -> None:
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    observation = ProfileFieldObservation(7, 50, 50, "update")
    with write_transaction(db):
        assert repo.invalidate_profile_sections(
            42, ("saved_music", "saved_gifts"), observation=observation, reason="user", pair_mode="disabled"
        )
        db.execute("UPDATE entity_profile_refresh_state SET status='complete'")
        assert not repo.invalidate_profile_sections(
            42, ("saved_music", "saved_gifts"), observation=observation, reason="user", pair_mode="disabled"
        )
    assert db.execute("SELECT status FROM entity_profile_refresh_state").fetchone()[0] == "complete"


@pytest.mark.asyncio
async def test_specific_settings_and_story_updates_store_facts_without_whole_peer_fanout(
    db: sqlite3.Connection,
) -> None:
    client = MagicMock()
    manager = event_manager(client, db, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    await manager.on_raw_identity_or_notify(
        types.UpdatePeerSettings(types.PeerUser(42), types.PeerSettings(report_spam=True))
    )
    await manager.on_raw_identity_or_notify(types.UpdateReadStories(types.PeerUser(42), 8))
    assert db.execute("SELECT COUNT(*) FROM entity_profile_refresh_state").fetchone()[0] == 0
    await manager.on_raw_identity_or_notify(types.UpdateStory(types.PeerUser(42), types.StoryItemDeleted(8)))
    facts = one(db, "SELECT fields_json FROM entity_profile_collection_facts WHERE section='current_stories'")[0]
    assert json.loads(cast(str, facts))["constructor"] == "StoryItemDeleted"
    sections = {
        row[0]
        for row in cast(list[tuple[object, ...]], db.execute("SELECT section FROM entity_detail_sections").fetchall())
    }
    assert sections == {"current_stories", "pinned_stories"}
    assert not events(db)
    client.assert_not_called()


def test_event_identity_is_insertion_order_not_acquisition_order(db: sqlite3.Connection) -> None:
    assert commit_snapshot(db, {"full_user.about": "one"}, 100)
    assert commit_snapshot(db, {"full_user.about": "two"}, 200)
    row = events(db)[0]
    assert row[4] == 200
    assert type(row[5]) is int
    assert db.execute("PRAGMA user_version").fetchone() is not None
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == _CURRENT_SCHEMA_VERSION


def test_fulluser_effective_summary_changes_dirty_only_dependents(db: sqlite3.Connection) -> None:
    assert commit_snapshot(db, {"full_user.saved_music": {"id": 1}, "full_user.stargifts_count": 2}, 10)
    assert {
        row[0]
        for row in cast(
            list[tuple[object, ...]],
            db.execute("SELECT section FROM entity_detail_sections WHERE reason='profile_summary_changed'").fetchall(),
        )
    } == {"saved_music", "saved_gifts"}
    with write_transaction(db):
        db.execute(
            "UPDATE entity_detail_sections SET status='fresh',observed_at=5 WHERE section IN ('saved_music','saved_gifts')"
        )
    assert commit_snapshot(db, {"full_user.saved_music": {"id": 1}, "full_user.stargifts_count": 3}, 20)
    assert db.execute(
        "SELECT status,observed_at FROM entity_detail_sections WHERE section='saved_music'"
    ).fetchone() == ("fresh", 5)
    assert db.execute(
        "SELECT status,observed_at FROM entity_detail_sections WHERE section='saved_gifts'"
    ).fetchone() == ("pending", 5)


def test_legacy_whitespace_repair_is_not_a_remote_rename(db: sqlite3.Connection) -> None:
    assert commit_snapshot(db, {"user.first_name": " Before ", "user.last_name": None}, 10)
    assert not events(db)


@pytest.mark.parametrize("skipped", [False, True])
def test_fulluser_supplied_stories_fill_or_queue_existing_detail_owner(db: sqlite3.Connection, skipped: bool) -> None:
    date = datetime(2026, 1, 1, tzinfo=UTC)
    stories: list[types.TypeStoryItem] = [types.StoryItemSkipped(9, date, date)] if skipped else []
    full = types.UserFull(
        42, types.PeerSettings(), types.PeerNotifySettings(), 0, stories=types.PeerStories(types.PeerUser(42), stories)
    )
    normalized = normalize_semantic_profile(
        types.User(42, first_name="Before"),
        full,
        target_id=42,
        owner_account_id=7,
        observation=ObservationBoundary(10, 11),
    )
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    with write_transaction(db):
        repo.request_section_refresh(42, ("full_profile",), now=10, pair_mode="disabled")
        cursor = repo.next_due_refresh(now=10)
        assert cursor is not None
        assert repo.commit_section(
            cursor,
            EntitySectionCommit(
                {},
                semantic_snapshot=normalized,
                field_observation=ProfileFieldObservation(7, 10, 10, "rpc"),
                evidence=ProfileAcquisitionEvidence(cursor.generation, "usable"),
            ),
            now=10,
        )
    payload = repo.read_section_payload(42, "current_stories")
    assert isinstance(payload, dict)
    if skipped:
        assert payload["supplied_cursor"]["request"] == {
            "phase": "details",
            "ids": [9],
            "after": None,
            "reported_count": None,
            "listing_terminal": True,
        }
    else:
        assert payload["complete"] is True
        assert payload["retained_count"] == 0


@pytest.mark.asyncio
async def test_first_authoritative_value_after_min_fragment_is_silent_without_known_legacy(
    db: sqlite3.Connection,
) -> None:
    with write_transaction(db):
        db.execute("UPDATE entity_details SET detail_json=?", (json.dumps({"schema": 1, "id": 42, "type": "user"}),))
        db.execute("UPDATE entities SET name=NULL WHERE id=42")
    client = MagicMock()
    manager = event_manager(client, db, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    await manager._persist_profile_pushes(sender=types.User(42, min=True, first_name="Partial"))
    assert not events(db)
    assert commit_snapshot(db, {"user.first_name": "Authoritative", "user.last_name": None}, 100)
    assert not events(db)


def test_unknown_child_enrichment_is_not_an_edit_but_known_sibling_change_is(db: sqlite3.Connection) -> None:
    root = "full_user.business_intro"
    coverage = {
        root: {"classification": "profile", "state": "present", "authoritative": False},
        root + ".title": {"classification": "profile", "state": "present", "authoritative": True},
        root + ".description": {"classification": "profile", "state": "unknown", "authoritative": False},
    }
    with write_transaction(db):
        db.execute(
            "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,fields_json,coverage_json,provenance_json,source) "
            "VALUES(7,42,'entity-profile-full-user-v2',?,?,?,'rpc')",
            (
                json.dumps({root: {"title": "known"}}),
                json.dumps(coverage),
                json.dumps({"fields": {root + ".title": {"order": 1}}}),
            ),
        )

    # Real canonical snapshots carry leaf authority, not only this container's coverage.
    def acquire(title: str, order: int) -> None:
        repo = EntityProfileRepository(db, section_ttl_seconds=300)
        value = snapshot({root: {"title": title, "description": "newly available"}}, order)
        value = replace(
            value,
            coverage={
                **value.coverage,
                root + ".title": ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True),
                root + ".description": ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True),
            },
        )
        with write_transaction(db):
            repo.request_section_refresh(42, ("full_profile",), now=order, pair_mode="disabled")
            cursor = repo.next_due_refresh(now=order)
            assert cursor is not None
            assert repo.commit_section(
                cursor,
                EntitySectionCommit(
                    {},
                    semantic_snapshot=value,
                    field_observation=ProfileFieldObservation(7, order, order, "rpc"),
                    evidence=ProfileAcquisitionEvidence(cursor.generation, "usable"),
                ),
                now=order,
            )

    acquire("known", 10)
    assert not events(db)
    acquire("changed", 20)
    assert [row[0] for row in events(db)] == [root]


def test_newer_reaction_preserves_original_evidence_without_starving_story_content(db: sqlite3.Connection) -> None:
    known = ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True)
    coverage: dict[str, ProfileFieldCoverage] = dict.fromkeys(
        ("item", "item.id", "item.caption", "item.sent_reaction"), known
    )

    def page(caption: str) -> ProfileCollectionPage:
        item = ProfileCollectionItem(
            "8", {"constructor": "StoryItem", "id": 8, "caption": caption, "sent_reaction": "old"}, coverage
        )
        return ProfileCollectionPage(42, "current_stories", (item,), ObservationBoundary(10, 11), terminal=True)

    with write_transaction(db):
        store_collection_page(db, page("old content"), owner=7, generation=1, cursor={}, order=10)
        reaction = ProfileCollectionItem(
            "8", {"constructor": "StoryItem", "id": 8, "sent_reaction": "new"}, {"item.sent_reaction": known}
        )
        store_story_update(db, 42, reaction, ProfileFieldObservation(7, 30, 30, "update"))
        store_collection_page(db, page("new content"), owner=7, generation=1, cursor={}, order=20)
    fields, evidence = one(
        db, "SELECT fields_json,provenance_json FROM entity_profile_collection_facts WHERE section='current_stories'"
    )
    assert json.loads(cast(str, fields))["caption"] == "new content"
    assert json.loads(cast(str, fields))["sent_reaction"] == "new"
    assert json.loads(cast(str, evidence))["item.sent_reaction"]["order"] == 30
    assert json.loads(cast(str, evidence))["item.caption"]["order"] == 20
    with write_transaction(db):
        tombstone = ProfileCollectionItem(
            "8", {"constructor": "StoryItemDeleted", "id": 8}, {"item": known, "item.id": known}
        )
        store_story_update(db, 42, tombstone, ProfileFieldObservation(7, 40, 40, "update"))
        store_collection_page(db, page("stale"), owner=7, generation=1, cursor={}, order=35)
    facts, evidence = one(
        db, "SELECT fields_json,provenance_json FROM entity_profile_collection_facts WHERE section='current_stories'"
    )
    assert json.loads(cast(str, facts)) == {"constructor": "StoryItemDeleted", "id": 8}
    assert "item.caption" not in json.loads(cast(str, evidence))
    assert not events(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("pair", [False, True])
async def test_fulluser_nonreturn_preserves_known_phone_value_and_age_until_explicit_clear(
    db: sqlite3.Connection,
    pair: bool,
) -> None:
    manager = event_manager(MagicMock(), db, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    await manager.on_raw_identity_or_notify(types.UpdateUserPhone(42, "123"))
    age_before = one(
        db, "SELECT observation_order,observed_at FROM entity_profile_field_observations WHERE field='phone'"
    )
    full = types.UserFull(42, types.PeerSettings(), types.PeerNotifySettings(), 0)
    response = types.users.UserFull(full, [], [types.User(42, first_name="Peer", phone=None)])
    normalized = normalize_full_user_response(
        response,
        target_id=42,
        target_kind=TargetKind.USER,
        owner_account_id=7,
        observation=ObservationBoundary(100, 101),
    )
    assert normalized.full_profile.payload is not None and "phone" not in normalized.full_profile.payload
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    with write_transaction(db):
        repo.request_section_refresh(42, ("full_profile",), now=100, pair_mode="enabled" if pair else "disabled")
        cursor = repo.next_due_refresh(now=100)
        assert cursor is not None
        commit = EntitySectionCommit(
            normalized.full_profile.payload,
            observation_owner_account_id=7,
            ownership_observed=True,
            semantic_snapshot=normalized.semantic_snapshot,
            field_observation=ProfileFieldObservation(7, 100, 100, "rpc"),
            evidence=ProfileAcquisitionEvidence(cursor.generation, "usable"),
        )
        if pair:
            assert repo.commit_full_user_pair(
                cursor,
                commit,
                EntitySectionCommit(
                    {},
                    status="not_applicable",
                    evidence=ProfileAcquisitionEvidence(cursor.generation, "absent"),
                ),
                now=101,
            )
        else:
            assert repo.commit_section(cursor, commit, now=101)
    legacy, semantic, provenance = one(
        db,
        "SELECT d.detail_json,s.fields_json,s.provenance_json FROM entity_details d JOIN entity_profile_semantic_snapshots s ON s.entity_id=d.entity_id",
    )
    assert json.loads(cast(str, legacy))["phone"] == "123"
    assert json.loads(cast(str, semantic))["user.phone"] == "123"
    assert (
        one(db, "SELECT observation_order,observed_at FROM entity_profile_field_observations WHERE field='phone'")
        == age_before
    )
    assert json.loads(cast(str, provenance))["fields"]["user.phone"]["order"] == age_before[0]
    await manager.on_raw_identity_or_notify(types.UpdateUserPhone(42, ""))
    legacy, semantic = one(
        db,
        "SELECT d.detail_json,s.fields_json FROM entity_details d JOIN entity_profile_semantic_snapshots s ON s.entity_id=d.entity_id",
    )
    assert json.loads(cast(str, legacy))["phone"] is None
    assert json.loads(cast(str, semantic))["user.phone"] is None


def publish_story_sections(
    db: sqlite3.Connection, repo: EntityProfileRepository, items: tuple[ProfileCollectionItem, ...], now: int
) -> None:
    for section in ("current_stories", "pinned_stories"):
        cursor = repo.next_due_refresh(now=now)
        assert cursor is not None and cursor.next_section == section
        page = ProfileCollectionPage(42, section, items, ObservationBoundary(now, now + 1), terminal=True)
        assert repo.commit_section(
            cursor,
            EntitySectionCommit(
                {},
                collection_page=page,
                field_observation=ProfileFieldObservation(7, allocate_observation_order(db), now, "rpc"),
            ),
            now=now,
        )


@pytest.mark.asyncio
async def test_known_delivered_story_delete_keeps_traversal_receipt_and_fences_old_page(db: sqlite3.Connection) -> None:
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    known = ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True)
    item = ProfileCollectionItem("8", {"constructor": "StoryItem", "id": 8}, {"item": known, "item.id": known})
    with write_transaction(db):
        repo.request_section_refresh(42, ("current_stories", "pinned_stories"), now=10, pair_mode="disabled")
        db.execute("UPDATE entity_profile_refresh_state SET next_section='current_stories'")
        publish_story_sections(db, repo, (item,), 10)
        before = repo.read_section_payload(42, "current_stories")
        assert isinstance(before, dict)
        repo.request_section_refresh(42, ("current_stories",), now=20, pair_mode="disabled")
        older = repo.next_due_refresh(now=20)
        assert older is not None
        older_order = allocate_observation_order(db)
    manager = event_manager(MagicMock(), db, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    update = types.UpdateStory(types.PeerUser(42), types.StoryItemDeleted(8))
    await manager.on_raw_identity_or_notify(update)
    after = repo.read_section_payload(42, "current_stories")
    assert isinstance(after, dict)
    assert after["published"] == before["published"]
    assert after["retained_count"] == before["retained_count"]
    assert after["observation_started_at"] == before["observation_started_at"]
    assert len(events(db)) == 2
    assert all(row[3] == "update" for row in events(db))
    generation = one(db, "SELECT generation FROM entity_profile_refresh_state")
    await manager.on_raw_identity_or_notify(update)
    assert one(db, "SELECT generation FROM entity_profile_refresh_state") == generation
    with write_transaction(db):
        old_page = ProfileCollectionPage(42, "current_stories", (item,), ObservationBoundary(20, 21), terminal=True)
        assert not repo.commit_section(
            older,
            EntitySectionCommit(
                {}, collection_page=old_page, field_observation=ProfileFieldObservation(7, older_order, 20, "rpc")
            ),
            now=30,
        )
    fields = one(db, "SELECT fields_json FROM entity_profile_collection_facts WHERE section='current_stories'")[0]
    assert json.loads(cast(str, fields))["constructor"] == "StoryItemDeleted"
    # A later real complete traversal sees the already-delivered removal, not a second edit.
    with write_transaction(db):
        publish_story_sections(db, repo, (), 30)
    assert len(events(db)) == 2
    # A genuinely observed re-add establishes a new baseline; a later delete is distinct.
    with write_transaction(db):
        repo.request_section_refresh(42, ("current_stories",), now=40, pair_mode="disabled")
        fresh = repo.next_due_refresh(now=40)
        assert fresh is not None
        added = ProfileCollectionPage(42, "current_stories", (item,), ObservationBoundary(40, 41), terminal=True)
        assert repo.commit_section(
            fresh,
            EntitySectionCommit(
                {},
                collection_page=added,
                field_observation=ProfileFieldObservation(7, allocate_observation_order(db), 40, "rpc"),
            ),
            now=40,
        )
    assert len(events(db)) == 3
    await manager.on_raw_identity_or_notify(types.UpdateStory(types.PeerUser(42), types.StoryItemDeleted(8)))
    assert len(events(db)) == 4


def test_reserved_invalidation_fence_is_account_bound_and_never_a_projected_fact(db: sqlite3.Connection) -> None:
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    with write_transaction(db):
        assert repo.invalidate_profile_sections(
            42,
            ("saved_music",),
            observation=ProfileFieldObservation(7, 10, 10, "update"),
            reason="user",
            pair_mode="disabled",
        )
    before = one(db, "SELECT detail_json FROM entity_details")[0]
    assert "__invalidation__" not in cast(str, before)
    with pytest.raises(ValueError):
        with write_transaction(db):
            repo.invalidate_profile_sections(
                42,
                ("saved_music",),
                observation=ProfileFieldObservation(8, 11, 11, "update"),
                reason="user",
                pair_mode="disabled",
            )
    assert one(
        db,
        "SELECT owner_account_id,observation_order FROM entity_profile_field_observations WHERE field='__invalidation__:user'",
    ) == (7, 10)
    assert one(db, "SELECT detail_json FROM entity_details")[0] == before
    assert not events(db)


def test_supplied_user_does_not_satisfy_fulluser_acquisition_or_hide_unknown_mapping(db: sqlite3.Connection) -> None:
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    push = project_user_profile(types.User(42, first_name="Before"))
    assert push is not None
    user = SuppliedProfileUser(42, push.semantic_fields, push.semantic_coverage, push.fields, False, False)
    with write_transaction(db):
        repo.apply_supplied_user(
            user, observation=ProfileFieldObservation(7, 1, 20, "rpc"), boundary=ObservationBoundary(20, 21)
        )
        assert not repo._has_semantic_snapshot(42)
    assert commit_snapshot(db, {"full_user.about": "known"}, 10)
    prior = one(
        db,
        "SELECT observation_started_at,observation_completed_at,source,observation_order FROM entity_profile_semantic_snapshots",
    )
    unknown = replace(
        user,
        coverage={
            **user.coverage,
            "user.new_layer_field": ProfileFieldCoverage(
                ProfileFieldClass.PROFILE, ProfileFieldState.UNKNOWN, reason="unknown_field"
            ),
        },
    )
    with write_transaction(db):
        repo.apply_supplied_user(unknown, observation=ProfileFieldObservation(7, 20, 20, "update"))
    assert (
        one(
            db,
            "SELECT observation_started_at,observation_completed_at,source,observation_order FROM entity_profile_semantic_snapshots",
        )
        == prior
    )
    complete, provenance = one(db, "SELECT complete,provenance_json FROM entity_profile_semantic_snapshots")
    assert complete == 0
    assert (
        json.loads(cast(str, provenance))["supplied_observation"]["coverage"]["user.new_layer_field"]["state"]
        == "unknown"
    )


def test_publication_history_and_watermark_rollback_with_section_failure(db: sqlite3.Connection) -> None:
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    known = ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True)
    item = ProfileCollectionItem("8", {"constructor": "StoryItem", "id": 8}, {"item": known, "item.id": known})
    with write_transaction(db):
        repo.request_section_refresh(42, ("current_stories", "pinned_stories"), now=10, pair_mode="disabled")
        db.execute("UPDATE entity_profile_refresh_state SET next_section='current_stories'")
        publish_story_sections(db, repo, (item,), 10)
        repo.request_section_refresh(42, ("current_stories",), now=20, pair_mode="disabled")
    original = repo.read_section_payload(42, "current_stories")
    cursor = repo.next_due_refresh(now=20)
    assert cursor is not None
    db.execute(
        "CREATE TRIGGER reject_publication BEFORE UPDATE OF payload_json ON entity_detail_sections "
        "WHEN NEW.section='current_stories' BEGIN SELECT RAISE(ABORT,'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        with write_transaction(db):
            page = ProfileCollectionPage(42, "current_stories", (), ObservationBoundary(20, 21), terminal=True)
            repo.commit_section(
                cursor,
                EntitySectionCommit(
                    {},
                    collection_page=page,
                    field_observation=ProfileFieldObservation(7, allocate_observation_order(db), 20, "rpc"),
                ),
                now=20,
            )
    assert not events(db)
    assert repo.read_section_payload(42, "current_stories") == original


def test_min_photo_hint_is_not_a_known_current_role_history_baseline(db: sqlite3.Connection) -> None:
    with write_transaction(db):
        db.execute("UPDATE entity_details SET detail_json=?", (json.dumps({"schema": 1, "name": "Before"}),))
    repo = EntityProfileRepository(db, section_ttl_seconds=300)
    hint = project_user_profile(types.User(42, min=True, photo=types.UserProfilePhoto(9, 2)))
    assert hint is not None
    with write_transaction(db):
        repo.apply_supplied_user(
            SuppliedProfileUser(42, hint.semantic_fields, hint.semantic_coverage, hint.fields, True, False),
            observation=ProfileFieldObservation(7, 1, 1, "min"),
        )
    assert commit_snapshot(db, {"user.photo": {"constructor": "UserProfilePhoto", "photo_id": 10}}, 10)
    assert not events(db)
