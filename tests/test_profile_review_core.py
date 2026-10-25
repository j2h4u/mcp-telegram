"""Real TL regressions for canonical story ordering and equivalent clears."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.delivered_update import DeliveredUpdate, delivered_update_context
from mcp_telegram.entity_profile.collection_repository import store_semantic_snapshot
from mcp_telegram.entity_profile.contracts import (
    ObservationBoundary,
    ProfileAcquisitionEvidence,
    ProfileFieldObservation,
    ProfileSemanticSnapshot,
    SuppliedProfileUser,
)
from mcp_telegram.entity_profile.profile_history_store import append_effective_history
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.entity_profile.semantic_normalization import normalize_semantic_profile
from mcp_telegram.entity_profile.updates import project_profile_update, project_user_profile
from mcp_telegram.sync_transactions import write_transaction
from sqlite_writer_helpers import event_manager
from test_profile_history_integration import db as db
from test_profile_history_integration import events, one

DATE = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2027, 1, 1, tzinfo=UTC)


def full(
    order: int,
    *,
    stories: list[types.TypeStoryItem] | None = None,
    max_read_id: int = 1,
    emoji: types.TypeEmojiStatus | None = None,
    user: types.User | None = None,
) -> ProfileSemanticSnapshot:
    return normalize_semantic_profile(
        user if user is not None else types.User(42, first_name="Before", emoji_status=emoji),
        types.UserFull(
            42,
            types.PeerSettings(),
            types.PeerNotifySettings(),
            0,
            stories=types.PeerStories(types.PeerUser(42), stories, max_read_id) if stories is not None else None,
        ),
        target_id=42,
        owner_account_id=7,
        observation=ObservationBoundary(order, order + 1),
    )


def persist_full(db: sqlite3.Connection, value: ProfileSemanticSnapshot, order: int) -> None:
    with write_transaction(db):
        merge = store_semantic_snapshot(db, value, order)
        assert merge is not None
        append_effective_history(db, merge, ProfileFieldObservation(7, order, order, "rpc"), {})


def supplied(db: sqlite3.Connection, update: object, order: int) -> None:
    push = project_profile_update(update)
    assert push is not None
    repository = EntityProfileRepository(db, section_ttl_seconds=300)
    observation = ProfileFieldObservation(7, order, order, "update")
    with write_transaction(db):
        repository.apply_supplied_user(
            SuppliedProfileUser(
                42, push.semantic_fields, push.semantic_coverage, push.fields, push.minimal, push.authoritative_photo
            ),
            observation=observation,
        )
        if push.story is not None:
            repository.apply_story_update(42, push.story, observation=observation)


def story(identifier: int, caption: str, reaction: str) -> types.StoryItem:
    return types.StoryItem(
        identifier, DATE, END, types.MessageMediaEmpty(), caption=caption, sent_reaction=types.ReactionEmoji(reaction)
    )


def semantic(db: sqlite3.Connection) -> tuple[dict[str, object], dict[str, object]]:
    fields, evidence = one(db, "SELECT fields_json,provenance_json FROM entity_profile_semantic_snapshots")
    return cast(dict[str, object], json.loads(cast(str, fields))), cast(
        dict[str, object], json.loads(cast(str, evidence))
    )


def test_real_read_stories_leaf_survives_older_fulluser_and_accepts_unrelated_profile(db: sqlite3.Connection) -> None:
    persist_full(db, full(10, stories=[], max_read_id=1), 10)
    supplied(db, types.UpdateReadStories(types.PeerUser(42), 8), 30)
    incoming = full(20, stories=[], max_read_id=1)
    persist_full(db, incoming, 20)
    fields, evidence = semantic(db)
    peer = cast(dict[str, object], fields["full_user.stories"])
    assert peer["max_read_id"] == 8
    paths = cast(dict[str, dict[str, object]], evidence["fields"])
    assert paths["full_user.stories.max_read_id"]["source"] == "update"
    assert paths["full_user.stories.max_read_id"]["order"] == 30
    assert one(db, "SELECT observation_started_at,observation_completed_at FROM entity_profile_semantic_snapshots") == (
        20,
        21,
    )


def test_real_reaction_stays_with_story_identity_when_older_fulluser_reorders_vector(db: sqlite3.Connection) -> None:
    persist_full(db, full(10, stories=[story(8, "old8", "before8"), story(9, "old9", "before9")]), 10)
    supplied(db, types.UpdateSentStoryReaction(types.PeerUser(42), 8, types.ReactionEmoji("new8")), 30)
    persist_full(db, full(20, stories=[story(9, "changed9", "before9"), story(8, "changed8", "stale8")]), 20)
    fields, evidence = semantic(db)
    peer = cast(dict[str, object], fields["full_user.stories"])
    stories = cast(list[dict[str, object]], peer["stories"])
    assert [value["id"] for value in stories] == [9, 8]
    assert [value["caption"] for value in stories] == ["changed9", "changed8"]
    assert cast(dict[str, object], stories[0]["sent_reaction"])["emoticon"] == "before9"
    assert cast(dict[str, object], stories[1]["sent_reaction"])["emoticon"] == "new8"
    paths = cast(dict[str, dict[str, object]], evidence["fields"])
    assert paths["full_user.stories.stories[1].sent_reaction"]["order"] == 30
    assert paths["full_user.stories.stories[1].sent_reaction"]["source"] == "update"
    assert paths["full_user.stories.stories[0].sent_reaction"]["order"] == 20


def test_tombstone_survives_real_reaction_and_late_full_and_embedded_unknown_is_not_appended(
    db: sqlite3.Connection,
) -> None:
    persist_full(db, full(10, stories=[story(8, "old8", "before8"), story(9, "old9", "before9")]), 10)
    supplied(db, types.UpdateStory(types.PeerUser(42), types.StoryItemDeleted(8)), 30)
    _assert_tombstone_metadata(db, "full_user.stories.stories[0]")
    supplied(db, types.UpdateSentStoryReaction(types.PeerUser(42), 8, types.ReactionEmoji("late")), 40)
    supplied(db, types.UpdateSentStoryReaction(types.PeerUser(42), 99, types.ReactionEmoji("unknown")), 50)
    for (encoded,) in cast(
        list[tuple[str]],
        db.execute("SELECT fields_json FROM entity_profile_collection_facts WHERE item_key='8'").fetchall(),
    ):
        assert json.loads(encoded) == {"constructor": "StoryItemDeleted", "id": 8}
    fields, _evidence = semantic(db)
    embedded = cast(list[dict[str, object]], cast(dict[str, object], fields["full_user.stories"])["stories"])
    assert embedded[0] == {"constructor": "StoryItemDeleted", "id": 8}
    assert embedded[1]["id"] == 9 and embedded[1]["caption"] == "old9"
    assert len(embedded) == 2
    assert one(db, "SELECT observation_started_at,observation_completed_at FROM entity_profile_semantic_snapshots") == (
        10,
        11,
    )
    persist_full(db, full(20, stories=[story(9, "new9", "before9"), story(8, "stale", "before8")]), 20)
    fields, evidence = semantic(db)
    embedded = cast(list[dict[str, object]], cast(dict[str, object], fields["full_user.stories"])["stories"])
    assert embedded[1] == {"constructor": "StoryItemDeleted", "id": 8}
    assert embedded[0]["caption"] == "new9"
    paths = cast(dict[str, dict[str, object]], evidence["fields"])
    assert paths["full_user.stories.stories[1]"]["order"] == 30
    assert paths["full_user.stories.stories[1]"]["source"] == "update"
    _assert_tombstone_metadata(db, "full_user.stories.stories[1]")


def _assert_tombstone_metadata(db: sqlite3.Connection, prefix: str) -> None:
    coverage, provenance = one(db, "SELECT coverage_json,provenance_json FROM entity_profile_semantic_snapshots")
    maps = (
        cast(dict[str, object], json.loads(cast(str, coverage))),
        cast(dict[str, dict[str, object]], json.loads(cast(str, provenance)))["fields"],
    )
    for evidence in maps:
        assert {path for path in evidence if path == prefix or path.startswith(prefix + ".")} == {
            prefix,
            prefix + ".id",
        }


def test_known_empty_emoji_forms_are_equivalent_but_real_clear_is_recorded_once(db: sqlite3.Connection) -> None:
    persist_full(db, full(10), 10)
    supplied(db, types.UpdateUserEmojiStatus(42, types.EmojiStatusEmpty()), 20)
    persist_full(db, full(30), 30)
    assert not [row for row in events(db) if row[0] == "user.emoji_status"]
    persist_full(db, full(40, emoji=types.EmojiStatus(99, END)), 40)
    supplied(db, types.UpdateUserEmojiStatus(42, types.EmojiStatusEmpty()), 50)
    persist_full(db, full(60), 60)
    selected = [row for row in events(db) if row[0] == "user.emoji_status"]
    assert len(selected) == 2
    assert json.loads(cast(str, selected[-1][2])) == {"constructor": "EmojiStatusEmpty"}


@pytest.mark.asyncio
async def test_known_name_and_alias_empty_forms_do_not_fake_edits_and_preserve_whitespace(
    db: sqlite3.Connection,
) -> None:
    persist_full(db, full(100), 100)
    manager = event_manager(MagicMock(), db, asyncio.Event())
    manager.set_self_id(7)
    manager.bind_demand_sink(MagicMock())
    with delivered_update_context(DeliveredUpdate(200, 200.0, 200)):
        await manager.on_raw_identity_or_notify(types.UpdateUserName(42, "Before", "", []))
    persist_full(db, full(300), 300)
    selected = {"user.last_name", "user.usernames"}
    assert not [row for row in events(db) if row[0] in selected]
    user = types.User(
        42, first_name="Before", last_name="Lovelace", usernames=[types.Username("alias", active=True, editable=False)]
    )
    persist_full(db, full(400, user=user), 400)
    initial = len([row for row in events(db) if row[0] in selected])
    with delivered_update_context(DeliveredUpdate(500, 500.0, 500)):
        await manager.on_raw_identity_or_notify(types.UpdateUserName(42, "Before", "", []))
    persist_full(db, full(600), 600)
    assert len([row for row in events(db) if row[0] in selected]) == initial + 2
    with delivered_update_context(DeliveredUpdate(700, 700.0, 700)):
        await manager.on_raw_identity_or_notify(types.UpdateUserName(42, "Before", " ", []))
    last = [row for row in events(db) if row[0] == "user.last_name"][-1]
    assert json.loads(cast(str, last[2])) == " "


@pytest.mark.parametrize("mode", ["supplied", "scalar", "pair"])
def test_legacy_baseline_captures_matching_prior_evidence_before_any_write(db: sqlite3.Connection, mode: str) -> None:
    with write_transaction(db):
        db.executemany(
            "INSERT INTO entity_profile_field_observations VALUES (42,?,7,5,123,'update')",
            [("name",), ("current_photo",)],
        )
    user = types.User(42, first_name="After", photo=types.UserProfilePhoto(9, 2))
    repository = EntityProfileRepository(db, section_ttl_seconds=300)
    observation = ProfileFieldObservation(7, 10, 200, "update" if mode == "supplied" else "rpc")
    with write_transaction(db):
        if mode == "supplied":
            push = project_user_profile(user)
            assert push is not None
            repository.apply_supplied_user(
                SuppliedProfileUser(42, push.semantic_fields, push.semantic_coverage, push.fields, False, False),
                observation=observation,
            )
        else:
            repository.request_section_refresh(
                42, ("full_profile",), now=200, pair_mode="enabled" if mode == "pair" else "disabled"
            )
            cursor = repository.next_due_refresh(now=200)
            assert cursor is not None
            commit = EntitySectionCommit(
                {
                    "name": "After",
                    "first_name": "After",
                    "last_name": None,
                    "current_photo": {"photo_id": 9, "date": None},
                },
                semantic_snapshot=full(200, user=user),
                field_observation=observation,
                observation_owner_account_id=7,
                ownership_observed=True,
                evidence=ProfileAcquisitionEvidence(cursor.generation, "usable"),
            )
            if mode == "pair":
                assert repository.commit_full_user_pair(
                    cursor,
                    commit,
                    EntitySectionCommit(
                        {}, status="not_applicable", evidence=ProfileAcquisitionEvidence(cursor.generation, "absent")
                    ),
                    now=201,
                )
            else:
                assert repository.commit_section(cursor, commit, now=201)
    rows = cast(
        list[tuple[str, str]],
        db.execute(
            "SELECT change_key,previous_provenance_json FROM entity_profile_change_events WHERE change_key IN ('legacy.name','photo.current')"
        ).fetchall(),
    )
    proof = {key: cast(dict[str, object], json.loads(value)) for key, value in rows}
    expected = {"order": 5, "observed_at": 123, "source": "update"}
    assert proof["legacy.name"]["legacy.name"] == expected
    assert proof["legacy.name"]["before_precision"] == "legacy_normalized"
    assert proof["photo.current"]["legacy.current_photo"] == expected


def test_min_legacy_evidence_does_not_fabricate_a_known_previous_observation(db: sqlite3.Connection) -> None:
    with write_transaction(db):
        db.executemany(
            "INSERT INTO entity_profile_field_observations VALUES (42,?,7,5,123,'min')", [("name",), ("current_photo",)]
        )
    push = project_user_profile(types.User(42, first_name="After", photo=types.UserProfilePhoto(9, 2)))
    assert push is not None
    with write_transaction(db):
        EntityProfileRepository(db, section_ttl_seconds=300).apply_supplied_user(
            SuppliedProfileUser(42, push.semantic_fields, push.semantic_coverage, push.fields, False, False),
            observation=ProfileFieldObservation(7, 10, 200, "update"),
        )
    assert not [row for row in events(db) if row[0] in {"legacy.name", "photo.current"}]


def test_late_fulluser_confirmed_member_keeps_newer_canonical_reaction_and_exact_evidence(
    db: sqlite3.Connection,
) -> None:
    repository = EntityProfileRepository(db, section_ttl_seconds=300)
    with write_transaction(db):
        repository.request_section_refresh(42, ("full_profile",), now=10, pair_mode="disabled")
        initial = repository.next_due_refresh(now=10)
        assert initial is not None
        assert repository.commit_section(
            initial,
            EntitySectionCommit(
                {},
                semantic_snapshot=full(10, stories=[]),
                field_observation=ProfileFieldObservation(7, 10, 10, "rpc"),
                evidence=ProfileAcquisitionEvidence(initial.generation, "usable"),
            ),
            now=11,
        )
        repository.request_section_refresh(42, ("full_profile",), now=20, pair_mode="disabled")
        reserved = repository.next_due_refresh(now=20)
        assert reserved is not None
    supplied(db, types.UpdateSentStoryReaction(types.PeerUser(42), 8, types.ReactionEmoji("new")), 30)
    before, _ = semantic(db)
    assert cast(dict[str, object], before["full_user.stories"])["stories"] == []
    with write_transaction(db):
        assert repository.commit_section(
            reserved,
            EntitySectionCommit(
                {},
                semantic_snapshot=full(20, stories=[story(8, "new caption", "old")]),
                field_observation=ProfileFieldObservation(7, 20, 20, "rpc"),
                evidence=ProfileAcquisitionEvidence(reserved.generation, "usable"),
            ),
            now=21,
        )
    fields, provenance = semantic(db)
    embedded = cast(list[dict[str, object]], cast(dict[str, object], fields["full_user.stories"])["stories"])
    assert [item["id"] for item in embedded] == [8]
    assert embedded[0]["caption"] == "new caption"
    assert cast(dict[str, object], embedded[0]["sent_reaction"])["emoticon"] == "new"
    current, canonical_provenance = one(
        db,
        "SELECT fields_json,provenance_json FROM entity_profile_collection_facts WHERE section='current_stories' AND item_key='8'",
    )
    assert embedded[0] == json.loads(cast(str, current))
    paths = cast(dict[str, dict[str, object]], provenance["fields"])
    canonical = cast(dict[str, dict[str, object]], json.loads(cast(str, canonical_provenance)))
    assert paths["full_user.stories.stories[0].sent_reaction"] == canonical["item.sent_reaction"]
    assert paths["full_user.stories.stories[0].sent_reaction"]["order"] == 30
    assert paths["full_user.stories.stories[0].caption"]["order"] == 20
    assert one(
        db,
        "SELECT observation_started_at,observation_completed_at,observation_order FROM entity_profile_semantic_snapshots",
    ) == (20, 21, 20)
