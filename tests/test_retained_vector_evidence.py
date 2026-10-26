"""Retained unchanged vectors keep evidence; real reobservations renew it."""

import json
from dataclasses import replace
from typing import cast

from telethon.tl import types

from mcp_telegram.entity_profile.collection_repository import store_semantic_snapshot
from mcp_telegram.entity_profile.contracts import (
    ObservationBoundary,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldState,
)
from mcp_telegram.entity_profile.semantic_normalization import normalize_semantic_profile
from mcp_telegram.sync_transactions import write_transaction
from tests.test_channel_contact_failure_identity import _run
from tests.test_profile_collection_pages import Database
from tests.test_profile_collection_pages import database as database
from tests.test_story_profile_diagnostics import _IDS
from tests.test_story_profile_recovery import Recovery, _fact, _story
from tests.test_story_profile_recovery import recovery as recovery


async def test_skipped_retains_all_vector_evidence_and_equal_full_response_renews_it(recovery: Recovery) -> None:
    _conn, service, client, _clock = recovery
    row = _fact(_conn, _IDS[1])
    previous_value = cast(object, json.loads(row[0])["media_areas"])
    coverage = cast(dict[str, dict[str, object]], json.loads(row[1]))
    provenance = cast(dict[str, dict[str, object]], json.loads(row[2]))
    present = {
        key: value
        for key, value in coverage.items()
        if key.startswith("item.media_areas") and value["state"] == "present"
    }
    observations = {key: value for key, value in provenance.items() if key.startswith("item.media_areas")}
    assert len(present) == len(observations) == 9
    assert set(present) == set(observations)
    assert all(value["observed_at"] == 100 for value in observations.values())

    local = await service.get_entity_info({"entity_id": 42, "section": "current_stories"})
    local = cast(dict[str, object], cast(dict[str, object], local["data"])["page"])
    item = next(value for value in cast(list[dict[str, object]], local["items"]) if value["item_key"] == str(_IDS[1]))
    dated = [
        cast(dict[str, object], value["value"])["observed_at"]
        for value in cast(list[dict[str, object]], item["field_evidence"])
        if str(value["path"]).startswith("item.media_areas")
        and cast(dict[str, object], value["value"])["state"] == "present"
    ]
    assert len(dated) == 9 and all(value == 100 for value in dated)

    client.result = types.stories.Stories(2, [_story(value, "Observed again") for value in _IDS], [], [])
    await _run(service)
    row = _fact(_conn, _IDS[1])
    assert json.loads(row[0])["media_areas"] == previous_value
    observations = {
        key: value
        for key, value in cast(dict[str, dict[str, object]], json.loads(row[2])).items()
        if key.startswith("item.media_areas")
    }
    assert len(observations) == 9
    assert all(value["observed_at"] == 300 for value in observations.values())


def test_semantic_snapshot_retains_omitted_unchanged_vector_evidence(database: Database) -> None:
    _path, conn, _repo = database
    snapshot = normalize_semantic_profile(
        types.User(42, first_name="Retained"),
        types.UserFull(
            42,
            types.PeerSettings(),
            types.PeerNotifySettings(),
            0,
            business_work_hours=types.BusinessWorkHours("UTC", [types.BusinessWeeklyOpen(60, 120)]),
        ),
        target_id=42,
        owner_account_id=77,
        observation=ObservationBoundary(100, 101),
    )
    root = "full_user.business_work_hours"
    vector = root + ".weekly_open"
    with write_transaction(conn):
        assert store_semantic_snapshot(conn, snapshot, 1) is not None
    before = cast(
        tuple[str, str, str],
        conn.execute(
            "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots"
        ).fetchone(),
    )
    partial = replace(
        snapshot,
        fields={root: {"constructor": "BusinessWorkHours", "timezone_id": "Europe/Paris"}},
        coverage={
            root: ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True),
            root + ".timezone_id": ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True),
            vector: ProfileFieldCoverage(
                ProfileFieldClass.PROFILE, ProfileFieldState.UNKNOWN, False, "partial_value_unobserved"
            ),
        },
        observation=ObservationBoundary(200, 201),
        diagnostics=("partial_business_hours",),
    )
    with write_transaction(conn):
        assert store_semantic_snapshot(conn, partial, 2) is not None
    after = cast(
        tuple[str, str, str],
        conn.execute(
            "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots"
        ).fetchone(),
    )
    assert json.loads(before[0])[root]["weekly_open"] == json.loads(after[0])[root]["weekly_open"]
    assert json.loads(after[0])[root]["timezone_id"] == "Europe/Paris"
    old_coverage, new_coverage = (
        cast(dict[str, object], json.loads(before[1])),
        cast(dict[str, object], json.loads(after[1])),
    )
    old_paths, new_paths = (
        cast(dict[str, object], json.loads(before[2])["fields"]),
        cast(dict[str, object], json.loads(after[2])["fields"]),
    )
    retained = {key for key in old_paths if key.startswith(vector)}
    assert retained == {vector, vector + "[0].start_minute", vector + "[0].end_minute"}
    assert all(old_paths[key] == new_paths.get(key) and old_coverage[key] == new_coverage.get(key) for key in retained)
