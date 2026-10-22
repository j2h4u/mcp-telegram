"""Durable profile knowledge and automatic private-dialog acquisition."""

# SQLite result shapes are asserted by these integration tests.
# pyright: reportAny=false

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

import pytest

from mcp_telegram.entity_profile.contracts import PROFILE_SECTIONS, ProfileFieldObservation, completeness
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_savepoint


def _repository(path: Path) -> tuple[sqlite3.Connection, EntityProfileRepository]:
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn, EntityProfileRepository(conn, section_ttl_seconds=10)


def _peer(conn: sqlite3.Connection, entity_id: int, *, kind: str = "user", enabled: bool = True) -> None:
    with write_savepoint(conn):
        conn.execute(
            "INSERT INTO entities(id,type,name,updated_at) VALUES (?,?,?,1)",
            (entity_id, kind, f"Peer {entity_id}"),
        )
        conn.execute("INSERT INTO dialogs(dialog_id,type,name) VALUES (?,?,?)", (entity_id, kind, f"Peer {entity_id}"))
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (?,?,'explicit',1)",
            (entity_id, int(enabled)),
        )


def _collected(conn: sqlite3.Connection, entity_id: int, *, owner: int = 7) -> None:
    with write_savepoint(conn):
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (?,?,100,?)",
            (entity_id, json.dumps({"schema": 1, "id": entity_id, "type": "user", "about": "Retained"}), owner),
        )
        conn.executemany(
            "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,payload_json) "
            "VALUES (?,?,'fresh',100,'[]')",
            ((entity_id, section) for section in PROFILE_SECTIONS),
        )
        conn.execute(
            "INSERT INTO entity_profile_refresh_state(entity_id,status,reason,updated_at,generation) "
            "VALUES (?,'complete','refresh_complete',100,1)",
            (entity_id,),
        )


@pytest.mark.parametrize("kind", ["user", "bot", "channel", "group"])
def test_elapsed_time_does_not_remove_collected_profile_coverage(tmp_path: Path, kind: str) -> None:
    conn, repo = _repository(tmp_path / "knowledge.sqlite")
    _peer(conn, 42, kind=kind)
    _collected(conn, 42)
    before = conn.execute("SELECT * FROM entity_detail_sections").fetchall()
    profile = repo.read(42, now=10_000_000)
    assert profile is not None
    assert profile.detail["about"] == "Retained"
    assert completeness(profile.sections) == "complete"
    assert all(section["observed_at"] == 100 for section in profile.sections.values())
    assert conn.execute("SELECT * FROM entity_detail_sections").fetchall() == before
    assert "about" not in repo.read_identity(42)
    conn.close()


def test_legacy_core_blob_is_not_a_collected_full_profile(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "core.sqlite")
    _peer(conn, 42)
    with write_savepoint(conn):
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at) VALUES (42,?,100)",
            ('{"schema":1,"id":42,"type":"user","name":"Only identity"}',),
        )
    profile = repo.read(42, now=101)
    assert profile is not None
    assert profile.sections["full_profile"]["status"] == "pending"
    assert completeness(profile.sections) == "partial"
    conn.close()


def test_reconcile_backfills_large_cohort_without_waiters_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "backfill.sqlite"
    conn, repo = _repository(path)
    for entity_id in range(100, 240):
        _peer(conn, entity_id)
    _peer(conn, 240, kind="bot")
    _peer(conn, 7)
    _peer(conn, 300, enabled=False)
    _peer(conn, 301, kind="service")
    _peer(conn, 302, kind="group")
    with write_savepoint(conn):
        conn.execute("UPDATE dialogs SET hidden=1,archived=1 WHERE dialog_id=240")
    assert repo.reconcile_history_profiles(now=200, limit=32, pair_mode="enabled", self_id=None) == 0
    admitted = 0
    while count := repo.reconcile_history_profiles(now=200, limit=32, pair_mode="enabled", self_id=7):
        assert count <= 32
        admitted += count
    assert admitted == 141
    assert repo.is_history_profile_managed(240)
    assert not repo.is_history_profile_managed(300)
    assert not repo.is_history_profile_managed(301)
    assert conn.execute("SELECT COUNT(*) FROM entity_details").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_refresh_state WHERE status='pending'").fetchone() == (141,)
    conn.close()
    reopened = sqlite3.connect(path)
    restarted = EntityProfileRepository(reopened, section_ttl_seconds=10)
    assert restarted.reconcile_history_profiles(now=100_000, limit=32, pair_mode="enabled", self_id=7) == 0
    assert restarted.next_due_refresh(now=100_000) is not None
    reopened.close()


def test_reconcile_preserves_completed_unavailable_failed_and_not_found(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "states.sqlite")
    for entity_id in range(40, 45):
        _peer(conn, entity_id)
        _collected(conn, entity_id)
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_detail_sections SET status='unavailable',observed_at=NULL,reason='private' WHERE entity_id=40"
        )
        conn.execute(
            "UPDATE entity_profile_refresh_state SET status='failed',retry_at=9999,next_section='avatar_history',"
            "acquisition_cursor=3 WHERE entity_id=41"
        )
        conn.execute(
            "UPDATE entity_profile_refresh_state SET status='rejected',reason='entity_not_found' WHERE entity_id=42"
        )
        conn.execute(
            "UPDATE entity_profile_refresh_state SET status='rejected',reason='refresh_rejected' WHERE entity_id=43"
        )
        conn.execute(
            "UPDATE entity_detail_sections SET status='unavailable',reason='refresh_rejected' WHERE entity_id=43"
        )
    failed = conn.execute("SELECT * FROM entity_profile_refresh_state WHERE entity_id=41").fetchone()
    assert repo.reconcile_history_profiles(now=100_000, limit=32, pair_mode="enabled", self_id=7) == 1
    assert conn.execute("SELECT * FROM entity_profile_refresh_state WHERE entity_id=41").fetchone() == failed
    assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=43").fetchone() == ("pending",)
    assert repo.reconcile_history_profiles(now=200_000, limit=32, pair_mode="enabled", self_id=7) == 0
    conn.close()


def test_targeted_refresh_preserves_untouched_facts_and_fences_older_cursor(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "targeted.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    untouched = conn.execute(
        "SELECT * FROM entity_detail_sections WHERE section<>'avatar_history' ORDER BY section"
    ).fetchall()
    assert repo.request_section_refresh(42, ("avatar_history",), now=200, pair_mode="enabled")
    cursor = repo.next_due_refresh(now=200)
    assert cursor is not None and cursor.next_section == "avatar_history"
    assert not repo.complete_unchanged_section(cursor, now=200)
    assert repo.request_section_refresh(42, ("avatar_history",), now=201, pair_mode="enabled")
    assert not repo.commit_section(cursor, EntitySectionCommit({"about": "Wrong old result"}), now=202)
    assert (
        conn.execute("SELECT * FROM entity_detail_sections WHERE section<>'avatar_history' ORDER BY section").fetchall()
        == untouched
    )
    assert repo.read(42, now=202).detail["about"] == "Retained"  # type: ignore[union-attr]
    conn.close()


def test_rejected_foreground_waiter_never_cancels_durable_retry(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "waiter.sqlite")
    _peer(conn, 42)
    repo.mark_pending(42, now=100)
    with write_savepoint(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET status='failed',retry_at=9999")
    before_state = conn.execute("SELECT * FROM entity_profile_refresh_state").fetchall()
    before_sections = conn.execute("SELECT * FROM entity_detail_sections").fetchall()
    repo.mark_refresh_rejected(42, now=200)
    assert conn.execute("SELECT * FROM entity_profile_refresh_state").fetchall() == before_state
    assert conn.execute("SELECT * FROM entity_detail_sections").fetchall() == before_sections
    repo.mark_refresh_not_found(42, now=201)
    assert conn.execute("SELECT status FROM entity_profile_refresh_state").fetchone() == ("rejected",)
    conn.close()


def test_unchanged_section_advances_without_renewing_observation(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "advance.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    repo.request_section_refresh(42, ("full_profile",), now=200, pair_mode="enabled")
    cursor = repo.next_due_refresh(now=200)
    assert cursor is not None
    assert repo.commit_section(cursor, EntitySectionCommit({"about": "Updated"}), now=201)
    untouched = conn.execute(
        "SELECT * FROM entity_detail_sections WHERE section<>'full_profile' ORDER BY section"
    ).fetchall()
    for _ in range(4):
        cursor = repo.next_due_refresh(now=100_000)
        assert cursor is not None
        assert repo.complete_unchanged_section(cursor, now=100_000)
    assert repo.next_due_refresh(now=100_000) is None
    assert (
        conn.execute("SELECT * FROM entity_detail_sections WHERE section<>'full_profile' ORDER BY section").fetchall()
        == untouched
    )
    conn.close()


def test_minimal_fixture_has_no_automatic_enrollment(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "minimal.sqlite")
    repo = EntityProfileRepository(conn, section_ttl_seconds=10)
    assert not repo.is_history_profile_managed(42)
    assert repo.reconcile_history_profiles(now=100, limit=32, pair_mode="enabled", self_id=7) == 0
    conn.close()


def _observed(
    order: int, *, source: Literal["rpc", "update", "min"] = "update", owner: int = 7
) -> ProfileFieldObservation:
    return ProfileFieldObservation(owner, order, 200 + order, source)


def _detail(conn: sqlite3.Connection) -> dict[str, object]:
    row = conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone()
    return json.loads(row[0])  # type: ignore[no-any-return]


@pytest.mark.parametrize("paired", [False, True])
def test_push_is_canonical_and_older_rpc_merges_only_untouched_fields(tmp_path: Path, paired: bool) -> None:
    conn, repo = _repository(tmp_path / "order.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    repo.request_section_refresh(42, ("full_profile",), now=200, pair_mode="enabled")
    cursor = repo.next_due_refresh(now=200)
    assert cursor is not None
    for order in range(2, 12):
        repo.apply_profile_update(42, {"status": {"type": "online", "expires": order}}, observation=_observed(order))
    full = EntitySectionCommit(
        {"status": {"type": "offline"}, "about": "Acquired bio"},
        field_observation=_observed(1, source="rpc"),
        ownership_observed=True,
        observation_owner_account_id=7,
    )
    if paired:
        assert repo.commit_full_user_pair(cursor, full, EntitySectionCommit({}), now=220)
    else:
        assert repo.commit_section(cursor, full, now=220)
    assert _detail(conn)["status"] == {"type": "online", "expires": 11}
    assert _detail(conn)["about"] == "Acquired bio"
    assert conn.execute(
        "SELECT field,observation_order,source FROM entity_profile_field_observations ORDER BY field"
    ).fetchall() == [("about", 1, "rpc"), ("status", 11, "update")]
    assert repo.read(42, now=99999).sections["full_profile"]["status"] == "fresh"  # type: ignore[union-attr]
    conn.close()


def test_same_value_advances_order_and_min_cannot_replace_authoritative_none(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "min.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    assert repo.apply_profile_update(42, {"phone": None}, observation=_observed(1)) == {"phone"}
    assert repo.apply_profile_update(42, {"phone": None}, observation=_observed(3)) == set()
    assert repo.apply_profile_update(42, {"phone": "older"}, observation=_observed(2)) == set()
    assert repo.apply_profile_update(42, {"phone": "minimal"}, observation=_observed(4, source="min")) == set()
    assert repo.apply_profile_update(42, {"about": "minimal"}, observation=_observed(5, source="min")) == set()
    assert _detail(conn)["phone"] is None
    assert _detail(conn)["about"] == "Retained"
    assert conn.execute(
        "SELECT observation_order FROM entity_profile_field_observations WHERE field='phone'"
    ).fetchone() == (3,)
    conn.close()


def test_photo_same_id_preserves_acquired_date_but_new_id_does_not(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "photo.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    repo.apply_profile_update(
        42, {"current_photo": {"photo_id": 100, "date": 80}}, observation=_observed(1, source="rpc")
    )
    assert (
        repo.apply_profile_update(42, {"current_photo": {"photo_id": 100, "date": None}}, observation=_observed(2))
        == set()
    )
    assert _detail(conn)["current_photo"] == {"photo_id": 100, "date": 80}
    repo.apply_profile_update(42, {"current_photo": {"photo_id": 101, "date": None}}, observation=_observed(3))
    assert _detail(conn)["current_photo"] == {"photo_id": 101, "date": None}
    conn.close()


def test_null_owner_first_push_drops_unverifiable_private_facts_and_old_rpc_cannot_replace_push(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "unowned.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    with write_savepoint(conn):
        conn.execute("UPDATE entity_details SET profile_owner_account_id=NULL")
    repo.request_section_refresh(42, ("full_profile",), now=200, pair_mode="enabled")
    cursor = repo.next_due_refresh(now=200)
    assert cursor is not None
    repo.apply_profile_update(42, {"phone": "latest"}, observation=_observed(2))
    assert "about" not in _detail(conn)
    assert _detail(conn)["phone"] == "latest"
    assert conn.execute("SELECT profile_owner_account_id FROM entity_details").fetchone() == (7,)
    assert conn.execute("SELECT DISTINCT status,observed_at,payload_json FROM entity_detail_sections").fetchall() == [
        ("pending", None, None)
    ]
    assert repo.commit_section(
        cursor,
        EntitySectionCommit(
            {"phone": "older", "about": "Owned bio"},
            field_observation=_observed(1, source="rpc"),
            ownership_observed=True,
            observation_owner_account_id=7,
        ),
        now=205,
    )
    assert _detail(conn)["phone"] == "latest"
    assert _detail(conn)["about"] == "Owned bio"
    conn.close()


def test_other_owner_is_rejected_without_changing_canonical_values(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "owner.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    before = _detail(conn)
    with pytest.raises(ValueError, match="does not match stored owner"):
        repo.apply_profile_update(42, {"phone": "other account"}, observation=_observed(2, owner=8))
    assert _detail(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_field_observations").fetchone() == (0,)
    conn.close()


def test_rpc_field_metadata_rolls_back_with_section_failure(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "atomic.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    repo.request_section_refresh(42, ("full_profile",), now=200, pair_mode="enabled")
    cursor = repo.next_due_refresh(now=200)
    assert cursor is not None
    before = _detail(conn)
    conn.execute(
        "CREATE TRIGGER reject_section BEFORE UPDATE ON entity_detail_sections "
        "BEGIN SELECT RAISE(ABORT,'section rejected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="section rejected"):
        repo.commit_section(
            cursor,
            EntitySectionCommit({"about": "Must rollback"}, field_observation=_observed(1, source="rpc")),
            now=201,
        )
    assert _detail(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_field_observations").fetchone() == (0,)
    assert repo.next_due_refresh(now=202) == cursor
    conn.close()


def test_rejected_stale_cursor_publishes_neither_field_metadata_nor_owner_reset(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "stale-observation.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    with write_savepoint(conn):
        conn.execute("UPDATE entity_details SET profile_owner_account_id=NULL")
    repo.request_section_refresh(42, ("full_profile",), now=200, pair_mode="enabled")
    cursor = repo.next_due_refresh(now=200)
    assert cursor is not None
    repo.request_section_refresh(42, ("full_profile",), now=201, pair_mode="enabled")
    before = _detail(conn)
    assert not repo.commit_section(
        cursor,
        EntitySectionCommit(
            {"about": "Rejected"},
            field_observation=_observed(1, source="rpc"),
            ownership_observed=True,
            observation_owner_account_id=7,
        ),
        now=202,
    )
    assert _detail(conn) == before
    assert conn.execute("SELECT profile_owner_account_id FROM entity_details").fetchone() == (None,)
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_field_observations").fetchone() == (0,)
    conn.close()


def test_due_retry_of_uncollected_section_is_not_terminal_unavailability(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "retry.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    with write_savepoint(conn):
        conn.execute("DELETE FROM entity_detail_sections WHERE section='common_chats'")
    repo.request_section_refresh(42, ("common_chats",), now=200, pair_mode="enabled")
    cursor = repo.next_due_refresh(now=200)
    assert cursor is not None
    assert repo.mark_section_failure(cursor, now=200, reason="timeouterror", retry_at=300)
    retry = repo.next_due_refresh(now=300)
    assert retry is not None
    assert not repo.complete_unchanged_section(retry, now=300)
    assert repo.next_due_refresh(now=300) == retry
    conn.close()


def test_personal_only_backfill_never_claims_pair_due_to_full_profile_age(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "personal-only.sqlite")
    _peer(conn, 42)
    _collected(conn, 42)
    with write_savepoint(conn):
        conn.execute("DELETE FROM entity_detail_sections WHERE section='personal_channel'")
    assert repo.reconcile_history_profiles(now=100_000, limit=32, pair_mode="enabled", self_id=7) == 1
    cursor = repo.next_due_refresh(now=100_000)
    assert cursor is not None and cursor.next_section == "personal_channel"
    assert not cursor.pair_eligible
    conn.close()
