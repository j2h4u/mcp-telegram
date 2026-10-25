"""Durable profile pages, honest coverage, and canonical semantic merges."""

from __future__ import annotations

# pyright: reportAny=false
import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.collection_repository import store_semantic_snapshot
from mcp_telegram.entity_profile.contracts import (
    ObservationBoundary,
    ProfileCollectionItem,
    ProfileCollectionPage,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldObservation,
    ProfileFieldState,
    ProfileSemanticSnapshot,
    TargetKind,
    UserReference,
)
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway

type Database = tuple[Path, sqlite3.Connection, EntityProfileRepository]


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection, EntityProfileRepository]]:
    path = tmp_path / "profile.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(42,'user',100)")
        conn.execute(
            "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,generation) "
            "VALUES(42,'pending',100,'avatar_history',1)"
        )
    try:
        yield path, conn, EntityProfileRepository(conn, section_ttl_seconds=300)
    finally:
        conn.close()


def _item(key: str, *, identity: str = "remote") -> ProfileCollectionItem:
    return ProfileCollectionItem(key, {"id": key}, {}, identity)


def _page(
    *items: ProfileCollectionItem,
    terminal: bool = False,
    count: int | None = None,
    cursor: dict[str, object] | None = None,
) -> ProfileCollectionPage:
    return ProfileCollectionPage(42, "avatar_history", items, ObservationBoundary(100, 101), count, cursor, terminal)


def _commit(repo: EntityProfileRepository, page: ProfileCollectionPage, now: int = 100) -> bool:
    cursor = repo.next_due_refresh(now=now)
    assert cursor is not None
    return repo.commit_section(
        cursor,
        EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, now, now, "rpc")),
        now=now,
    )


def test_more_than_100_pages_reopen_and_exact_count(database: Database) -> None:
    path, conn, repo = database
    for index in range(103):
        with write_transaction(conn):
            assert _commit(repo, _page(_item(str(index)), count=103, cursor={"offset": index + 1}), 100 + index)
        if index == 50:
            reader = sqlite3.connect(path)
            try:
                assert json.loads(
                    reader.execute("SELECT page_cursor_json FROM entity_profile_refresh_state").fetchone()[0]
                )["request"] == {"offset": 51}
            finally:
                reader.close()
            repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_transaction(conn):
        assert _commit(repo, _page(terminal=True, count=103), 300)
    summary = _summary(repo, "avatar_history")
    assert summary["complete"] is True
    assert summary["reported_count"] == summary["retained_count"] == 103
    assert summary["comparable_membership"] is False


@pytest.mark.parametrize("case", ["same_cursor", "cycle", "changed_count", "count_gap", "malformed"])
def test_partial_never_becomes_complete(database: Database, case: str) -> None:
    _path, conn, repo = database
    with write_transaction(conn):
        assert _commit(repo, _page(_item("1"), count=2, cursor={"offset": 1}))
    page = _page(_item("2"), count=2, terminal=True)
    if case == "same_cursor":
        page = replace(page, terminal=False, next_cursor={"offset": 1})
    elif case == "cycle":
        page = replace(page, terminal=False, next_cursor={})
    elif case == "changed_count":
        page = replace(page, reported_count=3)
    elif case == "count_gap":
        page = replace(page, items=())
    else:
        page = replace(page, diagnostics=("unknown_constructor",))
    with write_transaction(conn):
        assert _commit(repo, page, 101)
    assert _summary(repo, "avatar_history")["complete"] is False
    row = conn.execute("SELECT status,retry_at FROM entity_profile_refresh_state").fetchone()
    assert row == ("failed", 161)
    assert conn.execute("SELECT count(*) FROM entity_profile_collection_facts").fetchone()[0] >= 1


def test_anonymous_gifts_preserve_duplicate_occurrences_and_replay(database: Database) -> None:
    _path, conn, repo = database
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='saved_gifts'")
    page = replace(
        _page(
            _item("template", identity="observation"), _item("template", identity="observation"), count=2, terminal=True
        ),
        section="saved_gifts",
    )
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    commit = EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, 1, 100, "rpc"))
    with write_transaction(conn):
        assert repo.commit_section(cursor, commit, now=100)
        assert not repo.commit_section(cursor, commit, now=100)
    summary = _summary(repo, "saved_gifts")
    assert summary["retained_count"] == 2 and summary["retained_unique_count"] == 0
    assert summary["complete"] and not summary["comparable_membership"]


def test_completed_scan_then_new_invalidation_isolates_membership_after_reopen(database: Database) -> None:
    path, conn, repo = database
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='pinned_stories'")
        assert _commit(repo, replace(_page(_item("1"), _item("2"), count=2, terminal=True), section="pinned_stories"))
    first = _summary(repo, "pinned_stories")
    assert conn.execute("SELECT status FROM entity_profile_refresh_state").fetchone()[0] == "complete"
    reopened = sqlite3.connect(path)
    try:
        repo = EntityProfileRepository(reopened, section_ttl_seconds=300)
        with write_transaction(reopened):
            assert repo.request_section_refresh(42, ("pinned_stories",), now=200, pair_mode="disabled")
            assert _commit(
                repo, replace(_page(_item("2"), _item("3"), count=2, terminal=True), section="pinned_stories"), 201
            )
        second = _summary(repo, "pinned_stories")
        assert cast(int, second["generation"]) > cast(int, first["generation"])
        assert second["retained_count"] == second["reported_count"] == 2
        assert second["complete"]
        assert second["comparable_membership"]
        assert reopened.execute("SELECT count(*) FROM entity_profile_collection_facts").fetchone()[0] == 3
    finally:
        reopened.close()


def test_gift_terminal_does_not_require_advertised_count_equality(database: Database) -> None:
    _path, conn, repo = database
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='saved_gifts'")
        assert _commit(repo, replace(_page(_item("one"), count=0, cursor={"offset": "opaque"}), section="saved_gifts"))
    with write_transaction(conn):
        assert _commit(repo, replace(_page(count=0, terminal=True), section="saved_gifts"), 101)
    summary = _summary(repo, "saved_gifts")
    assert summary["complete"] and summary["count_discrepancy"]
    assert summary["reported_count"] == 0 and summary["retained_count"] == 1


def test_replaced_object_and_shorter_vector_retire_old_unknown_coverage(database: Database) -> None:
    _path, conn, _repo = database
    first = ProfileSemanticSnapshot(
        42,
        77,
        {
            "full_user.profile_photo": {"constructor": "Photo", "id": 1},
            "full_user.items": [{"constructor": "Photo", "id": 1}, {"constructor": "Photo", "id": 2}],
        },
        {
            "full_user.profile_photo.missing": _coverage(ProfileFieldState.UNKNOWN, False),
            "full_user.items[1].missing": _coverage(ProfileFieldState.UNKNOWN, False),
        },
        ObservationBoundary(100, 101),
    )
    with write_transaction(conn):
        store_semantic_snapshot(conn, first, 1)
        second = replace(
            first,
            fields={
                "full_user.profile_photo": {"constructor": "Photo", "id": 3},
                "full_user.items": [{"constructor": "Photo", "id": 1}],
            },
            coverage={"full_user.profile_photo.id": _coverage(), "full_user.items[0].id": _coverage()},
        )
        store_semantic_snapshot(conn, second, 2)
    coverage, complete = conn.execute("SELECT coverage_json,complete FROM entity_profile_semantic_snapshots").fetchone()
    assert complete == 1
    assert "missing" not in coverage


def test_newer_exact_semantic_push_preserves_complete_value_and_original_age(database: Database) -> None:
    _path, conn, repo = database
    first = ProfileSemanticSnapshot(
        42,
        77,
        {"user.first_name": "old", "full_user.about": "known"},
        {"user.first_name": _coverage(), "full_user.about": _coverage()},
        ObservationBoundary(100, 101),
    )
    with write_transaction(conn):
        store_semantic_snapshot(conn, first, 1)
        repo.apply_profile_update(42, {"first_name": "new"}, observation=ProfileFieldObservation(77, 3, 150, "update"))
        provenance = json.loads(
            conn.execute("SELECT provenance_json FROM entity_profile_semantic_snapshots").fetchone()[0]
        )
        provenance["fields"]["user.first_name"] = {"order": 3, "observed_at": 150, "source": "update"}
        conn.execute(
            "UPDATE entity_profile_semantic_snapshots SET fields_json=?,provenance_json=?",
            (json.dumps({"user.first_name": "  new  ", "full_user.about": "known"}), json.dumps(provenance)),
        )
        merged = store_semantic_snapshot(
            conn,
            replace(
                first,
                fields={"user.first_name": "stale", "full_user.about": "known"},
                observation=ObservationBoundary(200, 201),
            ),
            2,
        )
    assert merged is not None
    assert merged.after["user.first_name"] == "  new  "
    assert conn.execute("SELECT complete FROM entity_profile_semantic_snapshots").fetchone()[0] == 1
    assert _mapping(_mapping(merged.provenance["fields"])["user.first_name"])["observed_at"] == 150


@pytest.mark.parametrize("prior", ["retained", "missing", "newer_exact"])
def test_legacy_alias_fences_keep_public_photo_and_composite_identity(database: Database, prior: str) -> None:
    _path, conn, repo = database
    fields: dict[str, object] = {
        "full_user.profile_photo": {"constructor": "Photo", "id": 1},
        "user.photo": {"constructor": "UserProfilePhoto", "photo_id": 2},
        "user.usernames": [{"constructor": "Username", "username": "old", "active": True}],
        "user.emoji_status": {"constructor": "EmojiStatus", "document_id": 7},
    }
    coverage = {path: _coverage() for path in fields}
    first = ProfileSemanticSnapshot(42, 77, fields, coverage, ObservationBoundary(100, 101))
    composites = {"user.usernames", "user.emoji_status"}
    with write_transaction(conn):
        initial = replace(
            first, fields={key: value for key, value in fields.items() if prior != "missing" or key not in composites}
        )
        store_semantic_snapshot(conn, initial, 1)
        repo.apply_profile_update(
            42,
            {"current_photo": {"photo_id": 9}, "extra_usernames": ["new"], "emoji_status_id": 8},
            observation=ProfileFieldObservation(77, 3, 150, "update"),
        )
        if prior == "newer_exact":
            row = conn.execute("SELECT fields_json,provenance_json FROM entity_profile_semantic_snapshots").fetchone()
            exact, provenance = json.loads(row[0]), json.loads(row[1])
            exact["user.usernames"][0]["username"] = "new"
            exact["user.emoji_status"]["document_id"] = 8
            for root in composites:
                provenance["fields"][root] = {"order": 3, "observed_at": 150, "source": "update"}
            conn.execute(
                "UPDATE entity_profile_semantic_snapshots SET fields_json=?,provenance_json=?",
                (json.dumps(exact), json.dumps(provenance)),
            )
        merged = store_semantic_snapshot(conn, replace(first, observation=ObservationBoundary(200, 201)), 2)
    assert merged is not None
    assert _mapping(merged.after["full_user.profile_photo"])["id"] == 1
    assert _mapping(merged.after["user.photo"])["photo_id"] == 9
    canonical = json.loads(conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone()[0])
    assert canonical["extra_usernames"] == ["new"] and canonical["emoji_status_id"] == 8
    persisted_coverage = json.loads(
        conn.execute("SELECT coverage_json FROM entity_profile_semantic_snapshots").fetchone()[0]
    )
    for root in composites:
        assert not any(path == root or path.startswith(root + ".") for path in merged.accepted_paths)
        if prior == "missing":
            assert root not in merged.after
        else:
            assert _mapping(_mapping(merged.provenance["fields"])[root])["observed_at"] == (
                150 if prior == "newer_exact" else 100
            )
            assert _mapping(_mapping(merged.provenance["fields"])[root])["source"] == (
                "update" if prior == "newer_exact" else "rpc"
            )
        assert persisted_coverage[root]["state"] == ("present" if prior == "newer_exact" else "unknown")
    if prior != "missing":
        usernames = cast(list[dict[str, object]], merged.after["user.usernames"])
        assert usernames[0]["username"] == ("new" if prior == "newer_exact" else "old")
        assert _mapping(merged.after["user.emoji_status"])["document_id"] == (8 if prior == "newer_exact" else 7)


def test_min_changed_name_cannot_replace_known_full_fact_or_renew_its_age(database: Database) -> None:
    from types import SimpleNamespace

    from mcp_telegram.entity_profile.full_user_normalization import normalize_full_user_response

    _path, conn, repo = database

    def snapshot(name: str, *, minimal: bool, about: str):
        return normalize_full_user_response(
            SimpleNamespace(
                users=[types.User(id=42, first_name=name, min=minimal)],
                chats=[],
                full_user=types.UserFull(
                    id=42,
                    settings=types.PeerSettings(),
                    notify_settings=types.PeerNotifySettings(),
                    common_chats_count=0,
                    about=about,
                ),
            ),
            target_id=42,
            target_kind="user",
            owner_account_id=77,
            observation=ObservationBoundary(200 if minimal else 100, 201 if minimal else 101),
        )

    first = snapshot("Ada", minimal=False, about="before")
    second = snapshot("Augusta", minimal=True, about="after")
    assert first.semantic_snapshot is not None and second.semantic_snapshot is not None
    assert first.full_profile.payload is not None and second.full_profile.payload is not None
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='full_profile'")
        cursor = repo.next_due_refresh(now=100)
        assert cursor is not None
        assert repo.commit_section(
            cursor,
            EntitySectionCommit(
                first.full_profile.payload,
                semantic_snapshot=first.semantic_snapshot,
                field_observation=ProfileFieldObservation(77, 1, 100, "rpc"),
            ),
            now=100,
        )
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='full_profile',generation=generation+1")
        cursor = repo.next_due_refresh(now=200)
        assert cursor is not None
        assert repo.commit_section(
            cursor,
            EntitySectionCommit(
                second.full_profile.payload,
                semantic_snapshot=second.semantic_snapshot,
                field_observation=ProfileFieldObservation(77, 2, 200, "rpc"),
            ),
            now=200,
        )
    fields, coverage, provenance = conn.execute(
        "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots"
    ).fetchone()
    assert json.loads(fields)["user.first_name"] == "Ada"
    assert json.loads(fields)["full_user.about"] == "after"
    assert json.loads(coverage)["user.first_name"]["authoritative"] is True
    assert json.loads(provenance)["fields"]["user.first_name"]["observed_at"] == 100
    assert json.loads(provenance)["fields"]["full_user.about"]["observed_at"] == 200


def test_partial_container_updates_authoritative_sibling_and_preserves_known_clear(database: Database) -> None:
    _path, conn, _repo = database
    first = ProfileSemanticSnapshot(
        42,
        77,
        {"full_user.business_intro": {"constructor": "BusinessIntro", "title": "old", "description": None}},
        {
            "full_user.business_intro": _coverage(),
            "full_user.business_intro.title": _coverage(),
            "full_user.business_intro.description": _coverage(ProfileFieldState.CLEARED),
        },
        ObservationBoundary(100, 101),
    )
    with write_transaction(conn):
        store_semantic_snapshot(conn, first, 1)
        second = replace(
            first,
            fields={
                "full_user.business_intro": {"constructor": "BusinessIntro", "title": "new", "description": "untrusted"}
            },
            coverage={
                "full_user.business_intro": _coverage(authoritative=False),
                "full_user.business_intro.title": _coverage(),
                "full_user.business_intro.description": _coverage(authoritative=False),
            },
            observation=ObservationBoundary(200, 201),
        )
        merged = store_semantic_snapshot(conn, second, 2)
    assert merged is not None
    assert _mapping(merged.after["full_user.business_intro"]) == {
        "constructor": "BusinessIntro",
        "title": "new",
        "description": None,
    }
    assert "full_user.business_intro.title" in merged.accepted_paths
    assert "full_user.business_intro.description" not in merged.accepted_paths
    assert _mapping(_mapping(merged.provenance["fields"])["full_user.business_intro.description"])["observed_at"] == 100


def test_transaction_rollback_and_invalidated_cursor_reject_page(database: Database) -> None:
    _path, conn, repo = database
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    commit = EntitySectionCommit(
        {},
        collection_page=_page(_item("1"), cursor={"offset": 1}),
        field_observation=ProfileFieldObservation(77, 1, 100, "rpc"),
    )
    with pytest.raises(RuntimeError), write_transaction(conn):
        assert repo.commit_section(cursor, commit, now=100)
        raise RuntimeError("body failed")
    assert conn.execute("SELECT count(*) FROM entity_profile_collection_facts").fetchone()[0] == 0
    assert repo.next_due_refresh(now=100) == cursor
    with write_transaction(conn):
        assert repo.request_section_refresh(42, ("avatar_history",), now=101, pair_mode="disabled")
        assert not repo.commit_section(cursor, commit, now=102)
    assert conn.execute("SELECT count(*) FROM entity_profile_collection_facts").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["group", "channel", "supergroup"])
def test_nonpersonal_refresh_keeps_original_sections(database: Database, kind: str) -> None:
    _path, conn, repo = database
    with write_transaction(conn):
        conn.execute("UPDATE entities SET type=?", (kind,))
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='personal_channel'")
        cursor = repo.next_due_refresh(now=100)
        assert cursor is not None
        assert repo.commit_section(cursor, EntitySectionCommit({}), now=100)
    assert conn.execute("SELECT status FROM entity_profile_refresh_state").fetchone()[0] == "complete"
    stored = repo.read(42, now=100)
    assert stored is not None
    assert not set(stored.sections) & {
        "saved_music",
        "saved_gifts",
        "current_stories",
        "pinned_stories",
    }


def _coverage(state: ProfileFieldState = ProfileFieldState.PRESENT, authoritative: bool = True):
    return ProfileFieldCoverage(ProfileFieldClass.PROFILE, state, authoritative)


def test_semantic_nonreturn_keeps_value_age_and_push_fence(database: Database) -> None:
    _path, conn, repo = database
    first = ProfileSemanticSnapshot(
        42,
        77,
        {"user.first_name": "old", "full_user.about": "retained"},
        {"user.first_name": _coverage(), "full_user.about": _coverage()},
        ObservationBoundary(100, 101),
    )
    with write_transaction(conn):
        store_semantic_snapshot(conn, first, 1)
        repo.apply_profile_update(42, {"first_name": "new"}, observation=ProfileFieldObservation(77, 3, 150, "update"))
        newer = replace(
            first,
            fields={"user.first_name": "stale", "full_user.about": None},
            coverage={
                "user.first_name": _coverage(),
                "full_user.about": _coverage(ProfileFieldState.API_NOT_RETURNED, False),
            },
            observation=ObservationBoundary(200, 201),
        )
        merged = store_semantic_snapshot(conn, newer, 2)
    assert merged is not None
    assert merged.after == {"user.first_name": "new", "full_user.about": "retained"}
    provenance = json.loads(conn.execute("SELECT provenance_json FROM entity_profile_semantic_snapshots").fetchone()[0])
    assert provenance["fields"]["full_user.about"]["observed_at"] == 100
    assert "user.first_name" not in merged.accepted_paths


def test_semantic_new_object_does_not_inherit_children_or_zip_vectors(database: Database) -> None:
    _path, conn, _repo = database
    fields = {
        "full_user.profile_photo": {"constructor": "Photo", "id": 1, "sizes": [1]},
        "full_user.items": [{"constructor": "Photo", "id": 1, "date": "old"}],
    }
    first = ProfileSemanticSnapshot(42, 77, fields, {}, ObservationBoundary(100, 101))
    with write_transaction(conn):
        store_semantic_snapshot(conn, first, 1)
        merged = store_semantic_snapshot(
            conn,
            replace(
                first,
                fields={
                    "full_user.profile_photo": {"constructor": "Photo", "id": 2},
                    "full_user.items": [{"constructor": "Photo", "id": 2}],
                },
                observation=ObservationBoundary(200, 201),
            ),
            2,
        )
    assert merged is not None
    assert "sizes" not in _mapping(merged.after["full_user.profile_photo"])
    assert "date" not in _mapping(cast(list[object], merged.after["full_user.items"])[0])


class _Client:
    def __init__(self, *results: object):
        self.results = iter(results)
        self.requests: list[object] = []

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        return next(self.results)


async def test_gateway_one_request_per_page_and_story_details_next_slice() -> None:
    client = _Client(
        types.stories.PeerStories(
            types.PeerStories(
                types.PeerUser(42),
                [types.StoryItemSkipped(7, datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 1, 2, tzinfo=UTC))],
            ),
            [],
            [],
        ),
        types.stories.Stories(1, [types.StoryItemDeleted(7)], [], []),
    )
    gateway = TelethonUserProfileGateway(client, now_provider=lambda: 100)
    page = await gateway.fetch_profile_collection_page(UserReference(42, 1), "current_stories", {})
    assert page.next_cursor is not None
    assert len(client.requests) == 1 and page.next_cursor["phase"] == "details" and not page.terminal
    detail = await gateway.fetch_profile_collection_page(UserReference(42, 1), "current_stories", page.next_cursor)
    assert len(client.requests) == 2 and detail.terminal and detail.items[0].detail_complete


async def test_target_user_sidecar_is_typed_and_keeps_private_capabilities_out() -> None:
    client = _Client(types.photos.Photos([], [types.User(id=42, first_name="exact  name", access_hash=123)]))
    page = await TelethonUserProfileGateway(client).fetch_profile_collection_page(
        UserReference(42, 1), "avatar_history", {}
    )
    assert page.supplied_user is not None
    assert page.supplied_user.fields["user.first_name"] == "exact  name"
    assert "user.access_hash" not in page.supplied_user.fields
    assert page.sidecar_identities[0].dialog_id == 42


@pytest.mark.parametrize(
    ("minimal", "apply_photo", "has_photo", "expected"),
    [
        (True, False, True, {}),
        (True, False, False, {}),
        (True, True, True, {"current_photo": {"photo_id": 2, "date": None}}),
        (False, False, False, {"current_photo": None}),
    ],
)
async def test_current_photo_requires_authoritative_id_or_actual_clear(
    minimal: bool, apply_photo: bool, has_photo: bool, expected: dict[str, object]
) -> None:
    from types import SimpleNamespace

    from mcp_telegram.daemon_entity_info import _profile_photo_patch

    user = types.User(
        id=42,
        min=minimal,
        apply_min_photo=apply_photo,
        photo=types.UserProfilePhoto(photo_id=2, dc_id=1) if has_photo else None,
    )
    response = SimpleNamespace(
        users=[user],
        chats=[],
        full_user=types.UserFull(
            id=42, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(), common_chats_count=0
        ),
    )
    observation = await TelethonUserProfileGateway(_Client(response)).fetch_user_profile(
        42, TargetKind.USER, owner_account_id=77
    )
    assert _profile_photo_patch(observation.current_photo, observation.semantic_snapshot) == expected


def test_reordered_identifiable_vector_keeps_rejected_leaf_original_age(database: Database) -> None:
    _path, conn, _repo = database
    root = "full_user.items"
    first = ProfileSemanticSnapshot(
        42,
        77,
        {root: [{"constructor": "Photo", "id": 1, "date": "one"}, {"constructor": "Photo", "id": 2, "date": "two"}]},
        {
            root: _coverage(),
            root + "[0].id": _coverage(),
            root + "[0].date": _coverage(),
            root + "[1].id": _coverage(),
            root + "[1].date": _coverage(),
        },
        ObservationBoundary(100, 101),
    )
    with write_transaction(conn):
        store_semantic_snapshot(conn, first, 1)
        provenance = json.loads(
            conn.execute("SELECT provenance_json FROM entity_profile_semantic_snapshots").fetchone()[0]
        )
        provenance["fields"][root + "[1].date"]["observed_at"] = 130
        conn.execute("UPDATE entity_profile_semantic_snapshots SET provenance_json=?", (json.dumps(provenance),))
        second = replace(
            first,
            fields={root: [{"constructor": "Photo", "id": 2, "date": "untrusted"}]},
            coverage={
                root: _coverage(authoritative=False),
                root + "[0].id": _coverage(),
                root + "[0].date": _coverage(authoritative=False),
            },
            observation=ObservationBoundary(200, 201),
        )
        merged = store_semantic_snapshot(conn, second, 2)
    assert merged is not None
    values = cast(list[object], merged.after[root])
    assert _mapping(values[0])["date"] == "two"
    assert _mapping(values[1])["id"] == 1
    assert _mapping(_mapping(merged.provenance["fields"])[root + "[0].date"])["observed_at"] == 130


async def test_full_user_semantic_fields_use_captured_owner_and_same_commit(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
    from mcp_telegram.sync_db import open_sync_db_reader
    from mcp_telegram.telegram_demand import RpcAttemptBudget
    from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
    from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient

    note = " \n💬 note\n "
    response = SimpleNamespace(
        users=[types.User(id=7, first_name="  exact  ")],
        chats=[],
        full_user=types.UserFull(
            id=7,
            settings=types.PeerSettings(),
            notify_settings=types.PeerNotifySettings(),
            common_chats_count=0,
            note=types.TextWithEntities(note, [types.MessageEntityBold(5, 4)]),
        ),
    )

    class Client(_Client):
        async def __call__(self, request: object) -> object:
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            return await super().__call__(request)

    path = tmp_path / "owner.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = Client(response)
    service = cast(DaemonEntityInfoService, _pair_service(reader, _PairClient(), enabled=True))
    service._deps = replace(
        service._deps, writer=writer, user_profile_port=TelethonUserProfileGateway(client, now_provider=lambda: 100)
    )

    def prepare(connection: sqlite3.Connection) -> None:
        repo = EntityProfileRepository(connection, section_ttl_seconds=300)
        repo.save_core({"id": 7, "type": "user", "name": "old"}, now=100)
        repo.mark_pending(7, now=100, pair_eligible_override=True)

    try:
        await writer.run(prepare)
        coordinator = service.refresh_coordinator
        assert coordinator is not None
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
        owner, target, fields = reader.execute(
            "SELECT owner_account_id,entity_id,fields_json FROM entity_profile_semantic_snapshots"
        ).fetchone()
        assert (owner, target) == (42, 7)
        decoded = json.loads(fields)
        assert decoded["full_user.note"]["text"] == note
        assert decoded["full_user.note"]["entities"][0]["offset"] == 5
        assert decoded["user.first_name"] == "  exact  "
        assert len(client.requests) == 1
        assert (
            reader.execute("SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=7").fetchone()[0]
            == "common_chats"
        )
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()


@pytest.mark.parametrize(
    "section,result",
    [
        ("avatar_history", types.photos.PhotosSlice(1000, [], [])),
        ("common_chats", types.messages.ChatsSlice(1000, [])),
        ("saved_music", types.users.SavedMusic(1000, [])),
        ("saved_gifts", types.payments.SavedStarGifts(1000, [], [], [], next_offset="opaque-next")),
        ("pinned_stories", types.stories.Stories(1000, [], [], [])),
    ],
)
async def test_gateway_counts_and_opaque_cursor(section: str, result: object) -> None:
    client = _Client(result)
    page = await TelethonUserProfileGateway(client).fetch_profile_collection_page(UserReference(42, 1), section, {})
    assert len(client.requests) == 1 and page.reported_count == 1000
    if section == "saved_gifts":
        assert page.next_cursor == {"offset": "opaque-next"} and not page.terminal


async def test_real_writer_atomic_page_visible_after_commit(database: Database) -> None:
    path, conn, _repo = database
    writer = await SQLiteWriter.open(path)
    trace: list[str] = []

    def persist(connection: sqlite3.Connection):
        connection.set_trace_callback(trace.append)
        return _commit(
            EntityProfileRepository(connection, section_ttl_seconds=300), _page(_item("one"), cursor={"offset": 1})
        )

    try:
        assert await writer.run(persist)
        assert conn.execute("SELECT count(*) FROM entity_profile_collection_facts").fetchone()[0] == 1
        assert json.loads(conn.execute("SELECT page_cursor_json FROM entity_profile_refresh_state").fetchone()[0])[
            "request"
        ] == {"offset": 1}
        assert sum(sql == "COMMIT" for sql in trace) == 1
    finally:
        await writer.aclose()


async def test_commit_failure_rolls_back_both_page_and_cursor(database: Database) -> None:
    path, conn, _repo = database
    writer = await SQLiteWriter.open(path)

    def persist(connection: sqlite3.Connection):
        connection.set_authorizer(
            lambda action, name, _a, _b, _c: (
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and name == "COMMIT" else sqlite3.SQLITE_OK
            )
        )
        try:
            return _commit(
                EntityProfileRepository(connection, section_ttl_seconds=300), _page(_item("one"), cursor={"offset": 1})
            )
        finally:
            connection.set_authorizer(None)

    try:
        with pytest.raises(sqlite3.DatabaseError):
            await writer.run(persist)
        assert conn.execute("SELECT count(*) FROM entity_profile_collection_facts").fetchone()[0] == 0
        assert conn.execute("SELECT page_cursor_json FROM entity_profile_refresh_state").fetchone()[0] is None
    finally:
        await writer.aclose()


@pytest.mark.parametrize("after_admission", [False, True])
async def test_page_writer_cancellation_settles_truthfully_and_never_starts_next_page(
    database: Database, after_admission: bool
) -> None:
    path, conn, _repo = database
    writer = await SQLiteWriter.open(path, capacity=1)
    entered, release = threading.Event(), threading.Event()
    started_waiting = asyncio.Event()
    next_requests: list[str] = []

    def block() -> None:
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test barrier timed out")

    def persist(connection: sqlite3.Connection):
        if after_admission:
            block()
        return _commit(
            EntityProfileRepository(connection, section_ttl_seconds=300), _page(_item("one"), cursor={"offset": 1})
        )

    async def operation() -> None:
        started_waiting.set()
        await writer.run(persist)
        next_requests.append("next page")

    blocker = None if after_admission else asyncio.create_task(writer.run(lambda _connection: block()))
    task = None
    try:
        if blocker is not None:
            assert await asyncio.to_thread(entered.wait, 10)
        task = asyncio.create_task(operation())
        await started_waiting.wait()
        if after_admission:
            assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        if after_admission:
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if blocker is not None:
            await blocker
        assert conn.execute("SELECT count(*) FROM entity_profile_collection_facts").fetchone()[0] == int(
            after_admission
        )
        assert next_requests == []
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if blocker is not None:
            await asyncio.gather(blocker, return_exceptions=True)
        await writer.aclose()


def _summary(repo: EntityProfileRepository, section: str) -> dict[str, object]:
    return _mapping(repo.read_section_payload(42, section))


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@pytest.mark.parametrize(
    "entity_id,kind,section",
    [(7, "user", "pinned_stories"), (-7, "group", "personal_channel"), (-1000000000007, "channel", "personal_channel")],
)
async def test_terminal_owner_releases_completion_admission(
    tmp_path: Path, entity_id: int, kind: str, section: str
) -> None:
    from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, RefreshEnqueueResult
    from mcp_telegram.telegram_demand import RpcAttemptBudget
    from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient

    path = tmp_path / "terminal.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    writer = await SQLiteWriter.open(path)
    service = cast(DaemonEntityInfoService, _pair_service(conn, _PairClient(), enabled=False))
    service._deps = replace(service._deps, writer=writer)

    def prepare(connection: sqlite3.Connection) -> None:
        profiles = EntityProfileRepository(connection, section_ttl_seconds=300)
        profiles.save_core({"id": entity_id, "type": kind}, now=100)
        profiles.mark_pending(entity_id, now=100)
        with write_transaction(connection):
            connection.execute(
                "UPDATE entity_profile_refresh_state SET next_section=? WHERE entity_id=?", (section, entity_id)
            )

    try:
        await writer.run(prepare)
        coordinator = service.refresh_coordinator
        assert coordinator is not None
        assert coordinator.enqueue(entity_id) is RefreshEnqueueResult.QUEUED
        event = coordinator._completion_events[entity_id]
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
        assert conn.execute(
            "SELECT status FROM entity_profile_refresh_state WHERE entity_id=?", (entity_id,)
        ).fetchone() == ("complete",)
        assert coordinator.queue_depth == 0 and event.is_set()
        assert service._profiles.refresh_state(entity_id, now=100) is None
    finally:
        await service.shutdown()
        conn.close()
        await writer.aclose()


def test_post_write_collection_cursor_rejection_rolls_back_facts(database: Database) -> None:
    _path, conn, repo = database
    with write_transaction(conn):
        conn.execute(
            "CREATE TRIGGER reject_collection_advance BEFORE UPDATE OF page_cursor_json ON entity_profile_refresh_state BEGIN SELECT RAISE(IGNORE); END"
        )
    with pytest.raises(sqlite3.OperationalError, match="collection cursor advance was rejected"):
        with write_transaction(conn):
            _commit(repo, _page(_item("one"), cursor={"offset": 1}))
    assert conn.execute("SELECT count(*) FROM entity_profile_collection_facts").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM entity_profile_collection_membership").fetchone() == (0,)
    assert repo.read_section_payload(42, "avatar_history") is None
