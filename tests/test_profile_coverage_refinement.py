"""A complete RPC can clarify unknown absence without replacing retained facts."""

# SQLite rows are explicitly asserted in these persistence scenarios.
# pyright: reportAny=false
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.entity_profile.collection_repository import (
    encode,
    store_collection_page,
    store_semantic_snapshot,
    store_story_update,
)
from mcp_telegram.entity_profile.contracts import (
    ObservationBoundary,
    ProfileCollectionItem,
    ProfileCollectionPage,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldObservation,
    ProfileFieldState,
    ProfileSemanticSnapshot,
)
from mcp_telegram.entity_profile.semantic_normalization import normalize_collection_item
from mcp_telegram.sync_transactions import write_transaction
from tests.test_profile_collection_pages import Database
from tests.test_profile_collection_pages import database as database

_DATE = datetime(2025, 1, 1, tzinfo=UTC)
_END = datetime(2030, 1, 1, tzinfo=UTC)


def story(*, minimal: bool, caption: str | None = None) -> types.StoryItem:
    photo = types.Photo(10, 20, b"private reference", _DATE, [], 2, has_stickers=False)
    return types.StoryItem(
        1, _DATE, _END, types.MessageMediaPhoto(photo=photo), pinned=True, min=minimal, caption=caption
    )


def page(raw: types.StoryItem, at: int) -> ProfileCollectionPage:
    fields, coverage, diagnostics = normalize_collection_item(raw)
    return ProfileCollectionPage(
        42,
        "pinned_stories",
        (ProfileCollectionItem("1", fields, coverage, detail_complete=not diagnostics),),
        ObservationBoundary(at, at + 1),
        1,
        terminal=True,
        diagnostics=diagnostics,
    )


def test_full_nonmin_receipt_refines_prior_unknown_absence(database: Database) -> None:
    _path, conn, _repo = database
    with write_transaction(conn):
        store_collection_page(conn, page(story(minimal=True), 100), owner=77, generation=1, cursor={}, order=1)
        store_collection_page(conn, page(story(minimal=False), 200), owner=77, generation=1, cursor={}, order=2)
    row = conn.execute(
        "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_collection_facts"
    ).fetchone()
    fields, coverage, provenance = (json.loads(value) for value in row)
    assert "caption" not in fields and "item.caption" not in provenance
    assert coverage["item.caption"] == {
        "classification": "profile",
        "state": "api_not_returned",
        "authoritative": False,
        "reason": "endpoint_did_not_return_value",
    }
    assert cast(dict[str, object], provenance["item.id"])["order"] == 2


@pytest.mark.parametrize("value", [None, "", False, 0, [], {}, {"constructor": "Opaque", "retained": "value"}])
@pytest.mark.parametrize("state", [ProfileFieldState.UNKNOWN, ProfileFieldState.PRESENT, ProfileFieldState.CLEARED])
def test_nonreturn_never_relabels_retained_value_or_renews_its_evidence(
    database: Database, value: object, state: ProfileFieldState
) -> None:
    _path, conn, _repo = database
    path = "full_user.about"
    previous = ProfileSemanticSnapshot(
        42,
        77,
        {path: value},
        {path: ProfileFieldCoverage(ProfileFieldClass.PROFILE, state, state is not ProfileFieldState.UNKNOWN)},
        ObservationBoundary(100, 101),
    )
    incoming = ProfileSemanticSnapshot(
        42,
        77,
        {},
        {
            path: ProfileFieldCoverage(
                ProfileFieldClass.PROFILE, ProfileFieldState.API_NOT_RETURNED, False, "endpoint_did_not_return_value"
            )
        },
        ObservationBoundary(200, 201),
    )
    with write_transaction(conn):
        store_semantic_snapshot(conn, previous, 1)
        before = conn.execute(
            "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots"
        ).fetchone()
        store_semantic_snapshot(conn, incoming, 2)
    after = conn.execute(
        "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots"
    ).fetchone()
    assert after[0:2] == before[0:2]
    assert json.loads(after[2])["fields"] == json.loads(before[2])["fields"]


@pytest.mark.parametrize(
    "guard",
    [
        "path",
        "ancestor",
        "missing_bounds",
        "reversed_bounds",
        "nan_bounds",
        "infinite_bounds",
        "stale_order",
        "unknown_incoming",
    ],
)
def test_unknown_absence_needs_attributed_nonstale_evidence(database: Database, guard: str) -> None:
    _path, conn, _repo = database
    path = "full_user.about"
    unknown = ProfileFieldCoverage(
        ProfileFieldClass.PROFILE, ProfileFieldState.UNKNOWN, False, "partial_value_unobserved"
    )
    before = ProfileSemanticSnapshot(42, 77, {}, {path: unknown}, ObservationBoundary(100, 101))
    incoming = replace(
        before,
        coverage={
            path: ProfileFieldCoverage(
                ProfileFieldClass.PROFILE, ProfileFieldState.API_NOT_RETURNED, False, "endpoint_did_not_return_value"
            )
        },
        observation=ObservationBoundary(200, 201),
    )
    with write_transaction(conn):
        store_semantic_snapshot(conn, before, 10)
        if guard in {"path", "ancestor"}:
            row = conn.execute("SELECT provenance_json FROM entity_profile_semantic_snapshots").fetchone()
            provenance = json.loads(row[0])
            key = path if guard == "path" else "full_user"
            provenance["fields"][key] = {"order": 30, "source": "update", "observed_at": 150}
            conn.execute("UPDATE entity_profile_semantic_snapshots SET provenance_json=?", (encode(provenance),))
        if guard == "missing_bounds":
            incoming = replace(incoming, observation=ObservationBoundary())
        invalid_bounds = {
            "reversed_bounds": ObservationBoundary(201, 200),
            "nan_bounds": ObservationBoundary(float("nan"), 201),
            "infinite_bounds": ObservationBoundary(200, float("inf")),
        }
        if guard in invalid_bounds:
            incoming = replace(incoming, observation=invalid_bounds[guard])
        if guard == "unknown_incoming":
            incoming = replace(incoming, coverage={path: unknown})
        store_semantic_snapshot(conn, incoming, 9 if guard == "stale_order" else 20)
    coverage = json.loads(conn.execute("SELECT coverage_json FROM entity_profile_semantic_snapshots").fetchone()[0])
    assert coverage[path]["state"] == "unknown"


def test_complete_snapshot_refines_unknown_without_a_value_or_observation(database: Database) -> None:
    _path, conn, _repo = database
    path = "full_user.about"
    old = ProfileSemanticSnapshot(
        42,
        77,
        {},
        {
            path: ProfileFieldCoverage(
                ProfileFieldClass.PROFILE, ProfileFieldState.UNKNOWN, False, "partial_value_unobserved"
            )
        },
        ObservationBoundary(100, 101),
    )
    new = replace(
        old,
        coverage={
            path: ProfileFieldCoverage(
                ProfileFieldClass.PROFILE, ProfileFieldState.API_NOT_RETURNED, False, "endpoint_did_not_return_value"
            )
        },
        observation=ObservationBoundary(200, 201),
    )
    with write_transaction(conn):
        store_semantic_snapshot(conn, old, 1)
        store_semantic_snapshot(conn, new, 2)
    fields, coverage, provenance = (
        json.loads(value)
        for value in conn.execute(
            "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots"
        ).fetchone()
    )
    assert path not in fields and path not in provenance["fields"]
    assert coverage[path]["state"] == "api_not_returned"


def test_delivered_story_nonreturn_does_not_masquerade_as_rpc_refinement(database: Database) -> None:
    _path, conn, _repo = database
    with write_transaction(conn):
        store_collection_page(conn, page(story(minimal=True), 100), owner=77, generation=1, cursor={}, order=1)
        full = page(story(minimal=False), 200).items[0]
        store_story_update(conn, 42, full, ProfileFieldObservation(77, 2, 200, "update"))
    coverage = json.loads(
        conn.execute(
            "SELECT coverage_json FROM entity_profile_collection_facts WHERE section='pinned_stories'"
        ).fetchone()[0]
    )
    assert coverage["item.caption"]["state"] == "unknown"
