"""Only complete, linked canonical receipts may clarify past pinned-story absence."""

# sqlite3 exposes dynamic row tuples; all retained columns are compared below.
# pyright: reportAny=false
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from typing import Literal, cast

import pytest

from mcp_telegram.entity_profile.collection_repository import (
    encode,
    refine_published_story_coverage,
    store_collection_page,
)
from mcp_telegram.entity_profile.contracts import ProfileFieldObservation
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.sync_transactions import write_transaction
from tests.test_profile_collection_pages import Database
from tests.test_profile_collection_pages import database as database
from tests.test_profile_coverage_refinement import page, story

type Repair = tuple[sqlite3.Connection, EntityProfileRepository]


@pytest.fixture
def repair(database: Database) -> Iterator[Repair]:
    _path, conn, repo = database
    repo.apply_profile_update(42, {"phone": "retained"}, observation=ProfileFieldObservation(77, 1, 10, "update"))
    partial = page(story(minimal=True), 100)
    complete = page(story(minimal=False), 200)
    with write_transaction(conn):
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES(42,'user')")
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='pinned_stories'")
        store_collection_page(conn, partial, owner=77, generation=1, cursor={}, order=10)
        cursor = repo.next_due_refresh(now=200)
        assert cursor is not None
        assert repo.commit_section(
            cursor,
            EntitySectionCommit(
                {}, collection_page=complete, field_observation=ProfileFieldObservation(77, 20, 200, "rpc")
            ),
            now=201,
        )
        row = conn.execute("SELECT coverage_json FROM entity_profile_collection_facts").fetchone()
        coverage = json.loads(row[0])
        # Recreate only the historical merge defect; the positive receipt is genuine.
        for path, evidence in partial.items[0].coverage.items():
            if evidence.state == "unknown" and coverage.get(path, {}).get("state") == "api_not_returned":
                coverage[path] = asdict(evidence)
        conn.execute("UPDATE entity_profile_collection_facts SET coverage_json=?", (encode(coverage),))
    yield conn, repo


def fact(conn: sqlite3.Connection) -> tuple[object, ...]:
    return cast(
        tuple[object, ...],
        conn.execute(
            "SELECT fields_json,coverage_json,provenance_json,observation_order,observed_at FROM entity_profile_collection_facts"
        ).fetchone(),
    )


def unaffected(conn: sqlite3.Connection) -> list[list[tuple[object, ...]]]:
    return [
        cast(list[tuple[object, ...]], conn.execute(f"SELECT * FROM {table}").fetchall())
        for table in (
            "entity_details",
            "entity_detail_sections",
            "entity_profile_refresh_state",
            "entity_profile_field_observations",
            "entity_profile_collection_membership",
        )
    ]


def test_proven_canonical_absence_changes_only_coverage(repair: Repair) -> None:
    conn, _repo = repair
    before = fact(conn)
    other = unaffected(conn)
    with write_transaction(conn):
        result = refine_published_story_coverage(conn, 77, 42)
    assert result == (1, 13, 0)
    after = fact(conn)
    assert after[:1] + after[2:] == before[:1] + before[2:]
    assert unaffected(conn) == other
    old, new = json.loads(cast(str, before[1])), json.loads(cast(str, after[1]))
    changed = {path for path in old if old[path] != new[path]}
    assert len(changed) == 13
    assert all(
        new[path]
        == {
            "classification": "profile",
            "state": "api_not_returned",
            "authoritative": False,
            "reason": "endpoint_did_not_return_value",
        }
        for path in changed
    )
    with write_transaction(conn):
        assert refine_published_story_coverage(conn, 77, 42) == (0, 0, 0)


@pytest.mark.parametrize(
    "guard",
    [
        "owner",
        "published",
        "detail",
        "diagnostics",
        "normalization",
        "source",
        "order",
        "bounds",
        "constructor",
        "identity",
        "ancestor",
    ],
)
def test_unproven_receipts_never_relabel_unknown(repair: Repair, guard: str) -> None:
    conn, _repo = repair
    with write_transaction(conn):
        fields, coverage, paths, order, at = fact(conn)
        fields, paths = json.loads(cast(str, fields)), json.loads(cast(str, paths))
        if guard == "owner":
            conn.execute("UPDATE entity_details SET profile_owner_account_id=99")
        elif guard == "published":
            conn.execute("UPDATE entity_profile_collection_membership SET scan=1")
        elif guard == "detail":
            conn.execute("UPDATE entity_profile_collection_membership SET detail_complete=0")
        elif guard == "diagnostics":
            conn.execute(
                "UPDATE entity_detail_sections SET payload_json=json_set(payload_json,'$.diagnostics',json('[\"incomplete\"]'))"
            )
        elif guard == "normalization":
            conn.execute("UPDATE entity_detail_sections SET normalization_version='unknown-future'")
        elif guard == "source":
            paths["item.id"]["source"] = "update"
        elif guard == "order":
            paths["item.id"]["order"] = 19
        elif guard == "bounds":
            paths["item.id"]["observation_completed_at"] = 202
        elif guard == "constructor":
            fields["constructor"] = "StoryItemSkipped"
        elif guard == "identity":
            fields["id"] = 2
        else:
            paths["item"] = {"order": 21, "source": "update", "observed_at": 201}
        conn.execute(
            "UPDATE entity_profile_collection_facts SET fields_json=?,provenance_json=?",
            (encode(fields), encode(paths)),
        )
    before, other = fact(conn), unaffected(conn)
    with write_transaction(conn):
        result = refine_published_story_coverage(conn, 77, 42)
    assert result[0:2] == (0, 0)
    assert fact(conn) == before and unaffected(conn) == other


def test_checkpoint_and_coverage_roll_back_together(repair: Repair) -> None:
    from mcp_telegram.entity_profile.repository import _read_profile_admission_state

    conn, repo = repair
    before, other = fact(conn), unaffected(conn)
    with write_transaction(conn):
        conn.execute(
            "CREATE TRIGGER reject_refinement BEFORE UPDATE OF coverage_json ON entity_profile_collection_facts BEGIN SELECT RAISE(IGNORE); END"
        )
    with pytest.raises(sqlite3.OperationalError, match="refinement changed"):
        repo.reconcile_history_profiles(
            now=300, limit=32, pair_mode="enabled", self_id=77, coverage_owner_account_id=77
        )
    assert fact(conn) == before and unaffected(conn) == other
    assert "story_coverage_refinement_v2" not in _read_profile_admission_state(conn, 77)
    with write_transaction(conn):
        conn.execute("DROP TRIGGER reject_refinement")
    assert (
        repo.reconcile_history_profiles(now=300, limit=1, pair_mode="enabled", self_id=77, coverage_owner_account_id=77)
        == 1
    )
    state = _read_profile_admission_state(conn, 77)
    assert state["story_coverage_refinement_v2"] == {
        "version": 2,
        "last_peer": 42,
        "completed": False,
        "items": 1,
        "slots": 13,
        "unresolved": 0,
    }


def test_checkpoint_is_bounded_restartable_and_idle(repair: Repair) -> None:
    from mcp_telegram.entity_profile.repository import _read_profile_admission_state, _refine_story_coverage_pass

    conn, _repo = repair
    with write_transaction(conn):
        conn.executemany(
            "INSERT INTO entity_profile_collection_facts SELECT owner_account_id,?,section,item_key,fields_json,coverage_json,provenance_json,observation_order,observed_at FROM entity_profile_collection_facts WHERE entity_id=42",
            ((peer,) for peer in range(100, 135)),
        )
        assert _refine_story_coverage_pass(conn, 77, 100) == 32
    state = _read_profile_admission_state(conn, 77)
    checkpoint = cast(dict[str, object], state["story_coverage_refinement_v2"])
    assert checkpoint["last_peer"] == 130 and checkpoint["completed"] is False
    second = sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2])
    try:
        with write_transaction(second):
            assert _refine_story_coverage_pass(second, 77, 32) == 4
        checkpoint = cast(dict[str, object], _read_profile_admission_state(second, 77)["story_coverage_refinement_v2"])
        assert checkpoint["completed"] is True and checkpoint["slots"] == 13
        statements: list[str] = []
        second.set_trace_callback(statements.append)
        with write_transaction(second):
            assert _refine_story_coverage_pass(second, 77, 32) == 0
        assert not any("FROM entity_profile_collection_facts" in sql for sql in statements)
    finally:
        second.close()


@pytest.mark.parametrize("version", [0, 1, 3, True, "2"])
def test_unknown_checkpoint_version_is_not_replayed(repair: Repair, version: object) -> None:
    from mcp_telegram.entity_profile.repository import (
        _read_profile_admission_state,
        _refine_story_coverage_pass,
        _save_profile_admission_state,
    )

    conn, _repo = repair
    before = fact(conn)
    with write_transaction(conn):
        state = _read_profile_admission_state(conn, 77)
        state["story_coverage_refinement_v2"] = {"version": version}
        _save_profile_admission_state(conn, state)
        assert _refine_story_coverage_pass(conn, 77, 32) == 0
    assert fact(conn) == before
    assert _read_profile_admission_state(conn, 77)["story_coverage_refinement_v2"] == {"version": version}


@pytest.mark.parametrize("value", [None, "", False, 0, [], {}, {"constructor": "Unsupported"}])
def test_retained_optional_value_is_never_reclassified(repair: Repair, value: object) -> None:
    conn, _repo = repair
    with write_transaction(conn):
        row = fact(conn)
        fields = json.loads(cast(str, row[0]))
        fields["caption"] = value
        conn.execute("UPDATE entity_profile_collection_facts SET fields_json=?", (encode(fields),))
    before = fact(conn)
    with write_transaction(conn):
        assert refine_published_story_coverage(conn, 77, 42) == (1, 12, 1)
    after = fact(conn)
    old, new = json.loads(cast(str, before[1])), json.loads(cast(str, after[1]))
    assert new["item.caption"] == old["item.caption"]
    assert after[:1] + after[2:] == before[:1] + before[2:]


def test_absent_slot_with_existing_provenance_is_not_reclassified(repair: Repair) -> None:
    conn, _repo = repair
    with write_transaction(conn):
        paths = json.loads(cast(str, fact(conn)[2]))
        paths["item.caption"] = {"source": "update", "order": 19, "observed_at": 150}
        conn.execute("UPDATE entity_profile_collection_facts SET provenance_json=?", (encode(paths),))
    before = fact(conn)
    with write_transaction(conn):
        assert refine_published_story_coverage(conn, 77, 42) == (1, 12, 1)
    after = fact(conn)
    assert json.loads(cast(str, after[1]))["item.caption"] == json.loads(cast(str, before[1]))["item.caption"]
    assert after[:1] + after[2:] == before[:1] + before[2:]


@pytest.mark.parametrize("guard", ["nested_lookalike", "malformed_ancestor"])
def test_proof_is_limited_to_exact_chain_and_valid_ancestors(repair: Repair, guard: str) -> None:
    conn, _repo = repair
    with write_transaction(conn):
        fields_json, coverage_json, paths_json, *_ = fact(conn)
        fields, coverage, paths = (json.loads(cast(str, value)) for value in (fields_json, coverage_json, paths_json))
        if guard == "nested_lookalike":
            fields["opaque"] = {"constructor": "Photo"}
            coverage["item.opaque.video_sizes"] = dict(coverage["item.media.photo.video_sizes"])
        else:
            paths["item"] = "malformed"
        conn.execute(
            "UPDATE entity_profile_collection_facts SET fields_json=?,coverage_json=?,provenance_json=?",
            (encode(fields), encode(coverage), encode(paths)),
        )
    with write_transaction(conn):
        result = refine_published_story_coverage(conn, 77, 42)
    coverage = json.loads(cast(str, fact(conn)[1]))
    if guard == "nested_lookalike":
        assert result == (1, 13, 1)
        assert coverage["item.opaque.video_sizes"]["state"] == "unknown"
    else:
        assert result == (0, 0, 1)


@pytest.mark.parametrize("field,value", [("owner_account_id", 99), ("generation", 2), ("scan", 1), ("scan", False)])
def test_current_receipt_must_match_published_identity(repair: Repair, field: str, value: object) -> None:
    conn, _repo = repair
    with write_transaction(conn):
        conn.execute(
            "UPDATE entity_detail_sections SET payload_json=json_set(payload_json,?,json(?)) WHERE section='pinned_stories'",
            ("$." + field, json.dumps(value)),
        )
    before, other = fact(conn), unaffected(conn)
    with write_transaction(conn):
        assert refine_published_story_coverage(conn, 77, 42) == (0, 0, 0)
    assert fact(conn) == before and unaffected(conn) == other


@pytest.mark.asyncio
async def test_queued_scope_change_cannot_repair_or_checkpoint(repair: Repair) -> None:
    import asyncio
    from dataclasses import replace

    from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
    from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
    from mcp_telegram.entity_profile.repository import _read_profile_admission_state
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient
    from tests.test_story_profile_diagnostics import _QueuedWriter

    conn, _repo = repair
    service = cast(DaemonEntityInfoService, _pair_service(conn, _PairClient(channel_id=None), enabled=True))
    writer = _QueuedWriter(conn)
    writer.pause = True
    service._deps = replace(
        service._deps,
        writer=cast(SQLiteWriter, writer),
        self_id=77,
        full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 77, 2, 99),
    )
    before = fact(conn)
    task = asyncio.create_task(service.reconcile_history_profiles())
    try:
        await asyncio.wait_for(writer.waiting.wait(), timeout=2)
        service._deps = replace(
            service._deps, full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 99, 2, 100)
        )
        writer.release.set()
        await task
    finally:
        writer.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert fact(conn) == before
    assert "story_coverage_refinement_v2" not in _read_profile_admission_state(conn, 77)


def _copy_current_story(conn: sqlite3.Connection) -> None:
    for table in ("entity_profile_collection_facts", "entity_profile_collection_membership", "entity_detail_sections"):
        columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
        projection = ",".join("'current_stories'" if column == "section" else column for column in columns)
        conn.execute(f"INSERT INTO {table} SELECT {projection} FROM {table} WHERE section='pinned_stories'")


@pytest.mark.parametrize("section", ["current_stories", "pinned_stories"])
def test_repair_isolates_identical_item_keys_in_each_story_section(repair: Repair, section: str) -> None:
    conn, _repo = repair
    with write_transaction(conn):
        _copy_current_story(conn)
    other = "pinned_stories" if section == "current_stories" else "current_stories"
    before = conn.execute("SELECT * FROM entity_profile_collection_facts WHERE section=?", (other,)).fetchall()
    with write_transaction(conn):
        assert refine_published_story_coverage(
            conn, 77, 42, section=cast(Literal["current_stories", "pinned_stories"], section)
        ) == (1, 13, 0)
    assert conn.execute("SELECT * FROM entity_profile_collection_facts WHERE section=?", (other,)).fetchall() == before


def test_v2_revisits_current_once_and_preserves_completed_v1_receipt(repair: Repair) -> None:
    from mcp_telegram.entity_profile.repository import (
        _read_profile_admission_state,
        _refine_story_coverage_pass,
        _save_profile_admission_state,
    )

    conn, _repo = repair
    old = {"version": 1, "last_peer": 42, "completed": True, "items": 1, "slots": 13, "unresolved": 0}
    with write_transaction(conn):
        _copy_current_story(conn)
        assert refine_published_story_coverage(conn, 77, 42) == (1, 13, 0)
        state = _read_profile_admission_state(conn, 77)
        state["story_coverage_refinement"] = old
        _save_profile_admission_state(conn, state)
        assert _refine_story_coverage_pass(conn, 77, 1) == 1
    state = _read_profile_admission_state(conn, 77)
    assert state["story_coverage_refinement"] == old
    assert state["story_coverage_refinement_v2"] == {
        "version": 2,
        "last_peer": 42,
        "completed": False,
        "items": 1,
        "slots": 13,
        "unresolved": 0,
    }
    reopened = sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2])
    try:
        with write_transaction(reopened):
            assert _refine_story_coverage_pass(reopened, 77, 32) == 0
        assert _read_profile_admission_state(reopened, 77)["story_coverage_refinement"] == old
        statements: list[str] = []
        reopened.set_trace_callback(statements.append)
        with write_transaction(reopened):
            assert _refine_story_coverage_pass(reopened, 77, 32) == 0
        assert not any("FROM entity_profile_collection_facts" in statement for statement in statements)
    finally:
        reopened.close()


def test_v2_second_section_failure_rolls_back_first_section_and_checkpoint(repair: Repair) -> None:
    from mcp_telegram.entity_profile.repository import _read_profile_admission_state, _refine_story_coverage_pass

    conn, _repo = repair
    with write_transaction(conn):
        _copy_current_story(conn)
        conn.execute(
            "CREATE TRIGGER reject_second_section BEFORE UPDATE OF coverage_json ON entity_profile_collection_facts WHEN OLD.section='pinned_stories' BEGIN SELECT RAISE(IGNORE); END"
        )
    before = conn.execute("SELECT * FROM entity_profile_collection_facts ORDER BY section").fetchall()
    with pytest.raises(sqlite3.OperationalError, match="refinement changed"):
        with write_transaction(conn):
            _refine_story_coverage_pass(conn, 77, 32)
    assert conn.execute("SELECT * FROM entity_profile_collection_facts ORDER BY section").fetchall() == before
    assert "story_coverage_refinement_v2" not in _read_profile_admission_state(conn, 77)
