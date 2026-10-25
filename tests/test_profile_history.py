"""Meaningful observed changes append atomically without inventing a past."""

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from typing import cast

import pytest

from mcp_telegram.entity_profile.contracts import ObservationBoundary, ProfileFieldObservation
from mcp_telegram.entity_profile.profile_history import (
    CollectionHistoryMerge,
    HistoryObservation,
    ProfileHistoryMerge,
    append_collection_history,
    append_profile_history,
)
from mcp_telegram.sync_transactions import write_transaction


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entity_profile_change_events("
        "owner_account_id INTEGER NOT NULL,entity_id INTEGER NOT NULL,observation_order INTEGER NOT NULL,"
        "change_key TEXT NOT NULL,category TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,"
        "previous_provenance_json TEXT NOT NULL,observation_started_at REAL,observation_completed_at REAL,"
        "observed_at INTEGER NOT NULL,source TEXT NOT NULL,server_time REAL,"
        "PRIMARY KEY(owner_account_id,entity_id,observation_order,change_key))"
    )
    conn.execute("CREATE TABLE canonical(value TEXT)")
    conn.commit()
    yield conn
    conn.close()


def observation(order: int = 2) -> HistoryObservation:
    return HistoryObservation(42, ProfileFieldObservation(7, order, 30, "rpc"), ObservationBoundary(20, 25))


def merge(path: str, before: object, after: object) -> ProfileHistoryMerge:
    return ProfileHistoryMerge(
        observation(),
        {path: before},
        {path: after},
        frozenset({path}),
        frozenset({path}),
        {path: {"order": 1, "observed_at": 10, "source": "update"}},
    )


def rows(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    return cast(
        list[tuple[object, ...]],
        db.execute("SELECT * FROM entity_profile_change_events ORDER BY change_key").fetchall(),
    )


def test_first_observation_and_unknown_legacy_note_are_silent(db: sqlite3.Connection) -> None:
    legacy = merge("full_user.note", {"text": "old"}, {"text": " old "})
    with write_transaction(db):
        assert append_profile_history(db, replace(legacy, comparable_paths=frozenset())) == 0
        assert append_profile_history(db, replace(merge("user.first_name", None, "Ada"), before={})) == 0
    assert rows(db) == []


def test_known_legacy_composite_compares_without_splitting_names(db: sqlite3.Connection) -> None:
    with write_transaction(db):
        value = merge("legacy.name", "Ada Lovelace", "Ada Byron")
        assert (
            append_profile_history(db, replace(value, comparison_values={"legacy.name": ("Ada Lovelace", "Ada Byron")}))
            == 1
        )
    row = rows(db)[0]
    assert row[3] == "legacy.name"
    assert json.loads(cast(str, row[5])) == "Ada Lovelace"
    assert row[8:13] == (20, 25, 30, "rpc", None)
    assert json.loads(cast(str, row[7])) == {
        "legacy.name": {"order": 1, "observed_at": 10, "source": "update"},
        "before_precision": "legacy_normalized",
    }


def test_exact_note_entities_and_private_role_are_preserved(db: sqlite3.Connection) -> None:
    before = {"text": " \tA😀e\u0301\n", "entities": [{"offset": 2, "length": 3, "constructor": "MessageEntityBold"}]}
    after = {"text": " \tA😀e\u0301\n\n", "entities": [{"offset": 2, "length": 3, "constructor": "MessageEntityBold"}]}
    with write_transaction(db):
        assert append_profile_history(db, merge("full_user.note", before, after)) == 1
    row = rows(db)[0]
    assert row[4] == "private_owner"
    assert json.loads(cast(str, row[5])) == before
    assert json.loads(cast(str, row[6])) == after


@pytest.mark.parametrize(
    "path",
    [
        "user.status",
        "user.bot_active_users",
        "user.contact",
        "full_user.notify_settings",
        "full_user.common_chats_count",
        "full_user.stories.views",
        "coverage",
        "user.access_hash",
    ],
)
def test_volatile_unknown_and_internal_paths_are_current_only(db: sqlite3.Connection, path: str) -> None:
    with write_transaction(db):
        assert append_profile_history(db, merge(path, 1, 2)) == 0
    assert not rows(db)


def test_emoji_expiry_is_current_only_but_substantive_price_is_an_edit(db: sqlite3.Connection) -> None:
    value = merge("user.emoji_status", {"document_id": 9, "until": 10}, {"document_id": 9, "until": 20})
    with write_transaction(db):
        assert append_profile_history(db, replace(value, accepted_paths=frozenset({"user.emoji_status.until"}))) == 0
        assert append_profile_history(db, merge("user.send_paid_messages_stars", 10, 20)) == 1
    assert [row[3] for row in rows(db)] == ["user.send_paid_messages_stars"]


@pytest.mark.parametrize(
    "path,role,category",
    [
        ("user.photo", "current", "profile"),
        ("full_user.profile_photo", "public", "profile"),
        ("full_user.personal_photo", "personal", "private_owner"),
        ("full_user.fallback_photo", "fallback", "profile"),
    ],
)
def test_photo_roles_transition_clear_and_reappearance_without_upload_dates(
    db: sqlite3.Connection,
    path: str,
    role: str,
    category: str,
) -> None:
    first = merge(path, {"id": 8, "date": 1}, {"id": 8, "date": 2, "sizes": [1, 2]})
    with write_transaction(db):
        assert append_profile_history(db, first) == 0
        assert append_profile_history(db, replace(first, after={path: None})) == 1
        returned = merge(path, None, {"id": 8, "date": 1})
        assert append_profile_history(db, replace(returned, observation=observation(3))) == 1
    result = rows(db)
    assert [(row[3], row[4]) for row in result] == [(f"photo.{role}", category)] * 2
    assert [(json.loads(cast(str, row[5])), json.loads(cast(str, row[6]))) for row in result] == [(8, None), (None, 8)]


def test_nested_accepted_paths_group_once_and_ignore_unaccepted_roots(db: sqlite3.Connection) -> None:
    value = merge(
        "full_user.business_intro", {"title": "Before", "description": "old"}, {"title": "After", "description": "new"}
    )
    value = replace(
        value, accepted_paths=frozenset({"full_user.business_intro.title", "full_user.business_intro.description"})
    )
    with write_transaction(db):
        assert append_profile_history(db, value) == 1
        assert append_profile_history(db, replace(value, accepted_paths=frozenset())) == 0
        assert append_profile_history(db, value) == 0
    assert len(rows(db)) == 1


def test_min_noop_rejected_and_account_namespaces(db: sqlite3.Connection) -> None:
    value = merge("user.first_name", "Before", "After")
    minimal = replace(value.observation, field=replace(value.observation.field, source="min"))
    with write_transaction(db):
        assert append_profile_history(db, replace(value, observation=minimal)) == 0
        assert append_profile_history(db, replace(value, accepted_paths=frozenset())) == 0
        assert append_profile_history(db, replace(value, after=value.before)) == 0
        assert append_profile_history(db, value) == 1
        other_owner = replace(value.observation, field=replace(value.observation.field, owner_account_id=8))
        assert append_profile_history(db, replace(value, observation=other_owner)) == 1
    assert {row[0] for row in rows(db)} == {7, 8}


def test_no_fabricated_bounds_or_server_time(db: sqlite3.Connection) -> None:
    value = merge("user.first_name", "Before", "After")
    with write_transaction(db):
        assert (
            append_profile_history(
                db, replace(value, observation=replace(value.observation, boundary=ObservationBoundary()))
            )
            == 1
        )
    assert rows(db)[0][8:13] == (None, None, 30, "rpc", None)


def test_requires_canonical_writer_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        append_profile_history(db, merge("user.first_name", "Before", "After"))


def test_actual_sqlite_failure_rolls_back_history_and_canonical_write(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TRIGGER reject_second BEFORE INSERT ON entity_profile_change_events "
        "WHEN NEW.change_key='user.last_name' BEGIN SELECT RAISE(ABORT,'injected'); END"
    )
    value = ProfileHistoryMerge(
        observation(),
        {"user.first_name": "A", "user.last_name": "B"},
        {"user.first_name": "C", "user.last_name": "D"},
        frozenset({"user.first_name", "user.last_name"}),
        frozenset({"user.first_name", "user.last_name"}),
        {},
    )
    with pytest.raises(sqlite3.IntegrityError):
        with write_transaction(db):
            db.execute("INSERT INTO canonical VALUES ('new')")
            append_profile_history(db, value)
    assert rows(db) == []
    assert db.execute("SELECT COUNT(*) FROM canonical").fetchone() == (0,)


def test_collection_terminal_stable_membership_only(db: sqlite3.Connection) -> None:
    value = CollectionHistoryMerge(
        observation(), "saved_gifts", frozenset({"remote:1"}), frozenset({"remote:2"}), False, {}
    )
    with write_transaction(db):
        # Partial/error/first/anonymous publications are explicitly incomparable.
        assert append_collection_history(db, value) == 0
        assert append_collection_history(db, replace(value, comparable=True)) == 2
        assert append_collection_history(db, replace(value, comparable=True)) == 0
    assert [(json.loads(cast(str, row[5])), json.loads(cast(str, row[6]))) for row in rows(db)] == [
        ("remote:1", None),
        (None, "remote:2"),
    ]


def test_explicit_personal_current_photo_is_owner_category(db: sqlite3.Connection) -> None:
    value = merge("user.photo", {"photo_id": 8, "personal": True}, {"photo_id": 9, "personal": True})
    with write_transaction(db):
        assert append_profile_history(db, value) == 1
    assert rows(db)[0][3:5] == ("photo.current", "private_owner")


def test_comparable_legacy_photo_requires_explicit_role_and_identity(db: sqlite3.Connection) -> None:
    with write_transaction(db):
        assert append_profile_history(db, merge("legacy.current_photo", {"photo_id": 8}, {"id": 9})) == 1
        assert append_profile_history(db, merge("legacy.photos", [8], [9])) == 0
        assert append_profile_history(db, merge("full_user.fallback_photo", {"date": 5}, {"id": 9})) == 0
    assert len(rows(db)) == 1


def test_explicit_clear_preserves_meaningful_nested_metadata(db: sqlite3.Connection) -> None:
    value = merge("full_user.business_intro", {"title": "old", "sticker": {"id": 9, "date": 5}}, None)
    with write_transaction(db):
        assert append_profile_history(db, value) == 1
    assert json.loads(cast(str, rows(db)[0][5])) == {"title": "old", "sticker": {"id": 9, "date": 5}}
    assert json.loads(cast(str, rows(db)[0][6])) is None


def test_missing_after_never_infers_a_clear(db: sqlite3.Connection) -> None:
    value = merge("full_user.about", "known", None)
    with write_transaction(db):
        assert append_profile_history(db, replace(value, after={})) == 0


def test_confirmed_server_time_stays_separate_from_observation(db: sqlite3.Connection) -> None:
    value = merge("user.first_name", "Before", "After")
    with write_transaction(db):
        assert append_profile_history(db, replace(value, observation=replace(value.observation, server_time=5))) == 1
    assert rows(db)[0][8:13] == (20, 25, 30, "rpc", 5)
    with pytest.raises(ValueError):
        replace(value.observation, server_time=float("nan"))


def test_outer_rejection_after_history_append_rolls_back_both(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.OperationalError):
        with write_transaction(db):
            db.execute("INSERT INTO canonical VALUES ('new')")
            assert append_profile_history(db, merge("user.first_name", "Before", "After")) == 1
            raise sqlite3.OperationalError("cursor CAS rejected")
    assert not rows(db)
    assert db.execute("SELECT COUNT(*) FROM canonical").fetchone() == (0,)


def test_collection_current_counters_and_incomparable_items_never_imply_changes(db: sqlite3.Connection) -> None:
    value = CollectionHistoryMerge(
        observation(), "saved_gifts", frozenset({"anonymous:old"}), frozenset({"anonymous:new"}), False, {}
    )
    with write_transaction(db):
        assert append_collection_history(db, value) == 0
        assert append_collection_history(db, replace(value, section="count", comparable=True)) == 0
        assert (
            append_collection_history(db, replace(value, before=frozenset(), after=frozenset(), comparable=True)) == 0
        )
        minimal = replace(value.observation, field=replace(value.observation.field, source="min"))
        assert append_collection_history(db, replace(value, observation=minimal, comparable=True)) == 0
    assert not rows(db)


def test_legacy_comparison_suppresses_whitespace_restoration_but_keeps_raw_change(db: sqlite3.Connection) -> None:
    baseline = merge("legacy.name", "Ada Lovelace", "Ada  Lovelace ")
    with write_transaction(db):
        assert append_profile_history(db, baseline) == 0
        assert (
            append_profile_history(
                db, replace(baseline, comparison_values={"legacy.name": ("Ada Lovelace", "Ada Lovelace")})
            )
            == 0
        )
        changed = replace(
            baseline,
            after={"legacy.name": "Ada  Byron "},
            comparison_values={"legacy.name": ("Ada Lovelace", "Ada Byron")},
        )
        assert append_profile_history(db, changed) == 1
    assert json.loads(cast(str, rows(db)[0][6])) == "Ada  Byron "
    assert json.loads(cast(str, rows(db)[0][7]))["before_precision"] == "legacy_normalized"


def test_photo_empty_placeholder_requires_explicit_clear_authority(db: sqlite3.Connection) -> None:
    value = merge("full_user.profile_photo", {"constructor": "Photo", "id": 8}, {"constructor": "PhotoEmpty", "id": 8})
    with write_transaction(db):
        assert append_profile_history(db, value) == 0
        assert append_profile_history(db, replace(value, cleared_paths=frozenset({"full_user.profile_photo"}))) == 1
        unknown_before = replace(
            value, before=value.after, after={"full_user.profile_photo": {"id": 9}}, observation=observation(3)
        )
        assert append_profile_history(db, unknown_before) == 0
        assert (
            append_profile_history(
                db, replace(unknown_before, previous_cleared_paths=frozenset({"full_user.profile_photo"}))
            )
            == 1
        )
    assert [(json.loads(cast(str, row[5])), json.loads(cast(str, row[6]))) for row in rows(db)] == [
        (8, None),
        (None, 9),
    ]


def test_note_formatted_date_only_change_preserves_all_entity_fields(db: sqlite3.Connection) -> None:
    before = {
        "constructor": "TextWithEntities",
        "text": " \tdate😀\n",
        "entities": [
            {
                "constructor": "MessageEntityFormattedDate",
                "offset": 2,
                "length": 6,
                "date": 1767225600,
                "relative": True,
                "short_date": True,
                "long_time": False,
            },
        ],
    }
    after = {
        "constructor": "TextWithEntities",
        "text": " \tdate😀\n",
        "entities": [
            {
                "constructor": "MessageEntityFormattedDate",
                "offset": 2,
                "length": 6,
                "date": 1798761600,
                "relative": True,
                "short_date": True,
                "long_time": False,
            },
        ],
    }
    value = replace(
        merge("full_user.note", before, after), accepted_paths=frozenset({"full_user.note.entities[0].date"})
    )
    with write_transaction(db):
        assert append_profile_history(db, value) == 1
    row = rows(db)[0]
    assert row[3:5] == ("full_user.note", "private_owner")
    assert json.loads(cast(str, row[5])) == before
    assert json.loads(cast(str, row[6])) == after


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        ("user.phone", "123", "456"),
        (
            "full_user.business_greeting_message",
            {"shortcut_id": 1, "recipients": {"users": [3]}},
            {"shortcut_id": 2, "recipients": {"users": [3]}},
        ),
        (
            "full_user.business_away_message",
            {"schedule": {"constructor": "BusinessAwayMessageScheduleCustom", "start_date": 10, "end_date": 20}},
            {"schedule": {"constructor": "BusinessAwayMessageScheduleCustom", "start_date": 30, "end_date": 40}},
        ),
    ],
)
def test_stable_contact_and_business_facts_have_observed_history(
    db: sqlite3.Connection,
    path: str,
    before: object,
    after: object,
) -> None:
    with write_transaction(db):
        assert append_profile_history(db, merge(path, before, after)) == 1
    row = rows(db)[0]
    # Profile category is not an assertion about who acted or phonebook acquisition.
    assert row[3:5] == (path, "profile")
    assert json.loads(cast(str, row[5])) == before
    assert json.loads(cast(str, row[6])) == after
