"""File-backed persistence tests for fenced Entity Profile refreshes."""

# pyright: reportAny=false

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import mcp_telegram.entity_profile.repository as repository_module
import mcp_telegram.sync_db as sync_db_module
from mcp_telegram.entity_profile.contracts import FULL_PROFILE_OWNED_FIELDS, ProfileAcquisitionEvidence
from mcp_telegram.entity_profile.refresh import failure_retry_at, scope_changed_retry_at, throttled_retry_at
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.sync_db import ensure_sync_schema


def _repository(path: Path) -> tuple[sqlite3.Connection, EntityProfileRepository]:
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO entities(id, type, name, updated_at) VALUES (42, 'user', 'User', 1)")
    conn.commit()
    return conn, EntityProfileRepository(conn, section_ttl_seconds=10)


def _evidence(generation: int, *, outcome: str = "usable") -> ProfileAcquisitionEvidence:
    return ProfileAcquisitionEvidence(
        generation=generation,
        outcome=outcome,
        provenance={"endpoint": "users.GetFullUser"},
        normalization_version="entity-profile-v1",
        observation_started_at=100,
        observation_completed_at=101,
        identity={"account_generation": 7, "entity_type": "user"},
    )


def test_migration_is_additive_and_invents_no_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as old_schema:
        old_schema.setattr(sync_db_module, "_CURRENT_SCHEMA_VERSION", 60)
        old_schema.setattr(sync_db_module, "_ENTITY_PROFILE_ACQUISITION_MIGRATION_61", 61)
        ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO entities(id, type, name, updated_at) VALUES (42, 'user', 'Old', 1)")
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at) VALUES (42, ?, 90)",
        ('{"schema":1,"id":42,"type":"user","name":"Old"}',),
    )
    conn.execute(
        "INSERT INTO entity_profile_refresh_state(entity_id,status,retry_at,reason,updated_at,next_section,acquisition_cursor) "
        "VALUES (42,'failed',123,'timeout',90,'common_chats',3)"
    )
    conn.commit()
    ensure_sync_schema(path)
    row = conn.execute(
        "SELECT detail_json, fetched_at, profile_revision FROM entity_details WHERE entity_id=42"
    ).fetchone()
    assert row == ('{"schema":1,"id":42,"type":"user","name":"Old"}', 90, 0)
    state = conn.execute(
        "SELECT status,retry_at,reason,next_section,acquisition_cursor,generation,started_at,pair_eligible "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone()
    assert state == ("failed", 123, "timeout", "common_chats", 3, 0, None, 0)
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_detail_sections WHERE entity_id=42 AND acquisition_generation IS NOT NULL"
    ).fetchone() == (0,)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(entity_details)")}
    assert {"profile_owner_account_id", "profile_observation_scope_json"} <= columns
    assert conn.execute(
        "SELECT profile_owner_account_id, profile_observation_scope_json FROM entity_details WHERE entity_id=42"
    ).fetchone() == (None, None)
    conn.close()


def test_pair_commit_rolls_back_all_projections_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    conn.execute(
        """CREATE TRIGGER reject_profile_pair
        BEFORE INSERT ON entity_detail_sections
        WHEN NEW.section='personal_channel'
        BEGIN SELECT RAISE(ABORT, 'test rollback'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        repo.commit_full_user_pair(
            cursor,
            EntitySectionCommit({"about": "new"}, evidence=_evidence(cursor.generation)),
            EntitySectionCommit(
                {"personal_channel": None},
                status="unavailable",
                reason="absent",
                evidence=_evidence(cursor.generation, outcome="absent"),
            ),
            now=101,
        )
    assert conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=42").fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_detail_sections WHERE entity_id=42 AND acquisition_generation IS NOT NULL"
    ).fetchone() == (0,)
    assert repo.next_due_refresh(now=101) == cursor
    conn.close()


def test_pair_commit_rolls_back_when_final_cursor_cas_returns_false(tmp_path: Path) -> None:
    path = tmp_path / "pair-cursor-reject.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    original_advance = repo._advance_full_pair_cursor
    repo._advance_full_pair_cursor = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError, match="full user pair cursor advance"):
        repo.commit_full_user_pair(
            cursor,
            EntitySectionCommit({"about": "new"}, evidence=_evidence(cursor.generation)),
            EntitySectionCommit(
                {"personal_channel": None},
                status="unavailable",
                reason="absent",
                evidence=_evidence(cursor.generation, outcome="absent"),
            ),
            now=101,
        )
    repo._advance_full_pair_cursor = original_advance  # type: ignore[method-assign]
    assert conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=42").fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_detail_sections WHERE entity_id=42 AND acquisition_generation IS NOT NULL"
    ).fetchone() == (0,)
    assert repo.next_due_refresh(now=101) == cursor
    conn.close()


def test_pair_measurement_survives_reopen_after_projection_commit(tmp_path: Path) -> None:
    path = tmp_path / "pair-measurement.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100, pair_mode_override="enabled")
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit({"about": "new"}, evidence=_evidence(cursor.generation)),
        EntitySectionCommit(
            {"personal_channel": None},
            status="unavailable",
            reason="absent",
            evidence=_evidence(cursor.generation, outcome="absent"),
        ),
        now=101,
    )
    conn.close()

    reopened = sqlite3.connect(path)
    assert reopened.execute(
        "SELECT pair_mode, pair_full_profile_outcome, pair_personal_channel_outcome, "
        "pair_measurement_complete, pair_ready_at, pair_summary_watermark "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("enabled", "usable", "absent", 1, 101, 101)
    reopened.close()


def test_pair_mode_is_generation_durable_across_restart_and_flip(tmp_path: Path) -> None:
    path = tmp_path / "pair-mode.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100, pair_mode_override="disabled")
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None and cursor.pair_mode == "disabled"
    conn.close()

    reopened = sqlite3.connect(path)
    repo = EntityProfileRepository(reopened, section_ttl_seconds=10)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None and cursor.pair_mode == "disabled"
    repo.mark_pending(42, now=101, pair_mode_override="enabled")
    cursor = repo.next_due_refresh(now=101)
    assert cursor is not None and cursor.pair_mode == "disabled"
    while cursor is not None:
        assert repo.commit_section(cursor, EntitySectionCommit({}, status="not_applicable"), now=101)
        cursor = repo.next_due_refresh(now=101)
    repo.mark_pending(42, now=102, pair_mode_override="enabled")
    cursor = repo.next_due_refresh(now=102)
    assert cursor is not None and cursor.pair_mode == "enabled"
    reopened.close()


def test_active_auth_scope_readmission_preserves_completed_pair_sections(tmp_path: Path) -> None:
    path = tmp_path / "active-readmission.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100, pair_mode_override="enabled")
    conn.execute(
        "UPDATE entity_profile_refresh_state SET status='failed', retry_at=777, reason='flood_wait', "
        "next_section='personal_channel', acquisition_cursor=3 WHERE entity_id=42"
    )
    conn.execute(
        "UPDATE entity_detail_sections SET status='fresh', observed_at=90, reason=NULL, payload_json='{}' "
        "WHERE entity_id=42 AND section IN ('full_profile', 'personal_channel')"
    )
    conn.commit()
    repo.mark_pending(42, now=200, reason="auth_scope_changed", pair_mode_override="disabled")
    assert conn.execute(
        "SELECT status, retry_at, next_section, acquisition_cursor, generation, pair_mode, follow_up_required "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("failed", 777, "personal_channel", 3, 1, "enabled", 1)
    assert conn.execute(
        "SELECT section, status, observed_at, reason, payload_json FROM entity_detail_sections "
        "WHERE entity_id=42 AND section IN ('full_profile', 'personal_channel') ORDER BY section"
    ).fetchall() == [("full_profile", "fresh", 90, None, "{}"), ("personal_channel", "fresh", 90, None, "{}")]
    conn.close()


def test_generation_and_revision_fences_reject_stale_writers(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    conn.execute("UPDATE entities SET username='canonical' WHERE id=42")
    conn.commit()
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    # A newer canonical writer changes the revision without changing the
    # durable cursor.  The old response must not overwrite it.
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at, profile_revision) VALUES (42, ?, 101, 1)",
        ('{"schema":1,"id":42,"type":"user","name":"newer"}',),
    )
    conn.commit()
    assert not repo.commit_section(
        cursor, EntitySectionCommit({"name": "older"}, identity_patch={"username": None}), now=102
    )
    assert conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone() == (
        '{"schema":1,"id":42,"type":"user","name":"newer"}',
    )
    assert conn.execute("SELECT type, name, username FROM entities WHERE id=42").fetchone() == (
        "user",
        "User",
        "canonical",
    )
    conn.execute(
        "UPDATE entity_profile_refresh_state SET generation=generation+1, profile_revision=1 WHERE entity_id=42"
    )
    conn.commit()
    assert not repo.commit_section(cursor, EntitySectionCommit({"name": "aba"}), now=103)
    conn.close()


def test_canonical_identity_merge_preserves_richer_placeholders_and_materializes_fresh_patch(tmp_path: Path) -> None:
    path = tmp_path / "identity-merge.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO entities(id, type, name, username, updated_at) VALUES (42, 'unknown', NULL, NULL, 1)")
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at) VALUES (42, ?, 90)",
        ('{"schema":1,"id":999,"type":"user","name":"Blob User","username":"blob"}',),
    )
    conn.commit()
    repo = EntityProfileRepository(conn, section_ttl_seconds=10)

    stored = repo.read(42, now=90)
    assert stored is not None
    assert stored.detail["id"] == 42
    assert stored.detail["type"] == "user"
    assert stored.detail["name"] == "Blob User"
    assert stored.detail["username"] == "blob"

    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    assert repo.commit_section(
        cursor,
        EntitySectionCommit(
            {"about": "enrichment"},
            identity_patch={"id": 999, "type": "User", "name": "Fresh User", "username": "fresh"},
        ),
        now=101,
    )
    assert conn.execute("SELECT id, type, name, username FROM entities WHERE id=42").fetchone() == (
        42,
        "user",
        "Fresh User",
        "fresh",
    )
    stored = repo.read(42, now=101)
    assert stored is not None
    assert stored.detail["id"] == 42
    assert stored.detail["type"] == "user"
    assert stored.detail["name"] == "Fresh User"
    assert stored.detail["username"] == "fresh"
    conn.close()


def test_core_acquisition_is_atomic_and_reopens_at_next_section(tmp_path: Path) -> None:
    path = tmp_path / "core-acquisition.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at, profile_revision) VALUES (42, ?, 90, 0)",
        ('{"schema":1,"about":"old"}',),
    )
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    assert repo.commit_core_acquisition(
        cursor,
        {"id": 42, "type": "user", "name": "Fresh", "username": "fresh"},
        next_acquisition_cursor=1,
        now=101,
    )
    assert conn.execute("SELECT type, name, username FROM entities WHERE id=42").fetchone() == (
        "user",
        "Fresh",
        "fresh",
    )
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (1,)
    assert conn.execute(
        "SELECT acquisition_cursor, profile_revision FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == (1, 1)
    conn.close()

    reopened = sqlite3.connect(path)
    reopened_repo = EntityProfileRepository(reopened, section_ttl_seconds=10)
    resumed = reopened_repo.next_due_refresh(now=102)
    assert resumed is not None
    assert resumed.acquisition_cursor == 1
    assert resumed.profile_revision == 1
    assert reopened_repo.commit_section(resumed, EntitySectionCommit({"about": "new"}), now=102)
    assert reopened.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (2,)
    assert reopened.execute(
        "SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == (2,)
    reopened.close()


def test_core_acquisition_stale_cursor_and_exception_leave_all_rows_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "core-rollback.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at, profile_revision) VALUES (42, ?, 90, 0)",
        ('{"schema":1,"about":"old"}',),
    )
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    conn.execute("UPDATE entity_profile_refresh_state SET acquisition_cursor=1 WHERE entity_id=42")
    conn.commit()
    before = (
        conn.execute("SELECT type, name, username FROM entities WHERE id=42").fetchone(),
        conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone(),
    )
    assert not repo.commit_core_acquisition(
        cursor,
        {"id": 42, "type": "user", "name": "stale", "username": "stale"},
        next_acquisition_cursor=1,
        now=101,
    )
    assert before == (
        conn.execute("SELECT type, name, username FROM entities WHERE id=42").fetchone(),
        conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone(),
    )
    conn.execute("UPDATE entity_profile_refresh_state SET acquisition_cursor=0 WHERE entity_id=42")
    conn.commit()
    original_matches = repo._cursor_matches

    def stale_after_match(candidate: object) -> bool:
        matched = original_matches(candidate)  # type: ignore[arg-type]
        conn.execute("UPDATE entity_profile_refresh_state SET acquisition_cursor=1 WHERE entity_id=42")
        return matched

    monkeypatch.setattr(repo, "_cursor_matches", stale_after_match)
    assert not repo.commit_core_acquisition(
        cursor,
        {"id": 42, "type": "user", "name": "rowcount", "username": "rowcount"},
        next_acquisition_cursor=1,
        now=101,
    )
    assert conn.execute(
        "SELECT acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == (0,)
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (0,)
    monkeypatch.setattr(repo, "_cursor_matches", original_matches)
    original = repository_module.upsert_entity_snapshots

    def fail_after_write(connection: sqlite3.Connection, snapshots: object) -> None:
        original(connection, snapshots)  # type: ignore[arg-type]
        raise RuntimeError("injected core failure")

    monkeypatch.setattr(repository_module, "upsert_entity_snapshots", fail_after_write)
    with pytest.raises(RuntimeError, match="injected core failure"):
        repo.commit_core_acquisition(
            cursor,
            {"id": 42, "type": "user", "name": "new", "username": "new"},
            next_acquisition_cursor=1,
            now=101,
        )
    assert conn.execute("SELECT type, name, username FROM entities WHERE id=42").fetchone() == (
        "user",
        "User",
        None,
    )
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (0,)
    assert conn.execute(
        "SELECT acquisition_cursor FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == (0,)
    conn.close()


def test_save_core_carries_active_refresh_revision_before_section_progress(tmp_path: Path) -> None:
    path = tmp_path / "save-core-active.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at, profile_revision) VALUES (42, ?, 90, 0)",
        ('{"schema":1,"about":"old"}',),
    )
    repo.mark_pending(42, now=100)
    repo.save_core({"id": 42, "type": "user", "name": "Fresh Name", "username": "fresh"}, now=101)
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (1,)
    assert conn.execute("SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        1,
    )
    assert conn.execute("SELECT name_normalized FROM entities WHERE id=42").fetchone() == ("fresh name",)
    cursor = repo.next_due_refresh(now=101)
    assert cursor is not None and cursor.profile_revision == 1
    assert repo.commit_section(cursor, EntitySectionCommit({"about": "new"}), now=102)
    conn.close()


def test_refresh_readmission_paths_preserve_detail_revision_fence(tmp_path: Path) -> None:
    path = tmp_path / "readmission-fence.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at, profile_revision) VALUES (42, ?, 90, 0)",
        ('{"schema":1,"about":"old"}',),
    )
    repo.mark_pending(42, now=100)
    conn.execute(
        "UPDATE entity_profile_refresh_state SET status='complete', retry_at=NULL, reason='refresh_complete' "
        "WHERE entity_id=42"
    )
    conn.commit()
    # A realtime identity write can arrive after completion. It advances the
    # detail fence while no refresh generation is active.
    repo.save_core({"id": 42, "type": "user", "name": "Fresh"}, now=101)
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (1,)
    assert conn.execute("SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        0,
    )
    repo.mark_pending(42, now=102)
    assert (
        conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone()
        == conn.execute("SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42").fetchone()
        == (1,)
    )
    cursor = repo.next_due_refresh(now=102)
    assert cursor is not None
    assert repo.commit_section(cursor, EntitySectionCommit({"about": "readmitted"}), now=103)

    conn.execute(
        "UPDATE entity_profile_refresh_state SET status='complete', retry_at=NULL, reason='refresh_complete' "
        "WHERE entity_id=42"
    )
    conn.commit()
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (2,)
    assert conn.execute("SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        2,
    )
    # Repeat the same drift against a completed generation and enter through
    # request_follow_up, which must carry the detail fence into generation 3.
    repo.save_core({"id": 42, "type": "user", "name": "Fresher"}, now=104)
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (3,)
    assert conn.execute("SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        2,
    )
    assert repo.request_follow_up(42, now=105)
    assert (
        conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone()
        == conn.execute("SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42").fetchone()
        == (3,)
    )
    cursor = repo.next_due_refresh(now=105)
    assert cursor is not None
    assert repo.commit_section(cursor, EntitySectionCommit({"about": "follow-up"}), now=106)

    conn.execute(
        "UPDATE entity_profile_refresh_state SET status='failed', retry_at=777, reason='flood_wait' WHERE entity_id=42"
    )
    conn.commit()
    # Active failed generations already carry the detail fence; re-admission
    # must preserve it without advancing generation or resetting the cursor.
    repo.save_core({"id": 42, "type": "user", "name": "Newest"}, now=107)
    repo.mark_pending(42, now=108)
    assert (
        conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone()
        == conn.execute("SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42").fetchone()
        == (5,)
    )
    conn.close()


def test_pre_detail_identity_writes_advance_only_the_active_refresh_fence(tmp_path: Path) -> None:
    path = tmp_path / "pre-detail-fence.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    before = conn.execute(
        "SELECT status, retry_at, reason, next_section, acquisition_cursor, generation, started_at "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone()

    repo.save_core({"id": 42, "type": "user", "name": "First", "username": "first"}, now=101)
    repo.save_core({"id": 42, "type": "user", "name": "Second", "username": "second"}, now=102)

    assert conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=42").fetchone() == (0,)
    state = conn.execute(
        "SELECT status, retry_at, reason, next_section, acquisition_cursor, generation, started_at, profile_revision "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone()
    assert state == (*before, 2)
    assert conn.execute("SELECT name, username FROM entities WHERE id=42").fetchone() == ("Second", "second")
    cursor = repo.next_due_refresh(now=102)
    assert cursor is not None and cursor.profile_revision == 2
    assert repo.commit_section(cursor, EntitySectionCommit({"about": "first detail"}), now=103)
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (3,)
    assert conn.execute("SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        3,
    )
    conn.close()


def test_pre_detail_core_acquisition_advances_cursor_fence_then_first_section_inserts(tmp_path: Path) -> None:
    path = tmp_path / "pre-detail-core.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None and cursor.profile_revision == 0

    assert repo.commit_core_acquisition(
        cursor,
        {"id": 42, "type": "user", "name": "Core", "username": "core"},
        next_acquisition_cursor=1,
        now=101,
    )
    assert conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=42").fetchone() == (0,)
    assert conn.execute(
        "SELECT acquisition_cursor, profile_revision FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == (1, 1)
    next_cursor = repo.next_due_refresh(now=101)
    assert next_cursor is not None and next_cursor.profile_revision == 1
    assert repo.commit_section(next_cursor, EntitySectionCommit({"about": "section"}), now=102)
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (2,)
    assert conn.execute("SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        2,
    )
    conn.close()


def test_pre_detail_pair_first_insert_uses_retained_refresh_revision(tmp_path: Path) -> None:
    path = tmp_path / "pre-detail-pair.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100, pair_mode_override="enabled")
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    assert repo.save_core({"id": 42, "type": "user", "name": "Core"}, now=101) is None
    cursor = repo.next_due_refresh(now=101)
    assert cursor is not None and cursor.profile_revision == 1

    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit({"about": "full"}, evidence=_evidence(cursor.generation)),
        EntitySectionCommit(
            {"personal_channel": None},
            status="unavailable",
            reason="absent",
            evidence=_evidence(cursor.generation, outcome="absent"),
        ),
        now=102,
    )
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone() == (2,)
    assert conn.execute(
        "SELECT next_section, profile_revision FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("common_chats", 2)
    conn.close()


@pytest.mark.parametrize(
    ("identity_patch", "expected"),
    [
        ({"username": None}, None),
        ({}, "known"),
        ({"username": ""}, "known"),
    ],
)
def test_identity_patch_deletion_absence_and_empty_semantics(
    tmp_path: Path, identity_patch: dict[str, object], expected: str | None
) -> None:
    path = tmp_path / "identity-patch.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    conn.execute("UPDATE entities SET username='known' WHERE id=42")
    conn.commit()
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    assert repo.commit_section(
        cursor, EntitySectionCommit({"about": "observed"}, identity_patch=identity_patch), now=101
    )
    assert conn.execute("SELECT username FROM entities WHERE id=42").fetchone() == (expected,)
    conn.close()
    reopened = sqlite3.connect(path)
    stored = EntityProfileRepository(reopened, section_ttl_seconds=10).read(42, now=102)
    assert stored is not None
    assert stored.detail["username"] == expected
    reopened.close()


def test_known_canonical_null_identity_overrides_legacy_blob_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "canonical-null.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO entities(id, type, name, username, updated_at) VALUES (42, 'user', 'Known', NULL, 1)")
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at) VALUES (42, ?, 90)",
        ('{"schema":1,"id":999,"type":"user","name":"Old","username":"old"}',),
    )
    conn.commit()
    repo = EntityProfileRepository(conn, section_ttl_seconds=10)
    stored = repo.read(42, now=100)
    assert stored is not None and stored.detail["id"] == 42
    assert stored.detail["name"] == "Known"
    assert stored.detail["username"] is None
    conn.close()
    reopened = sqlite3.connect(path)
    stored = EntityProfileRepository(reopened, section_ttl_seconds=10).read(42, now=101)
    assert stored is not None and stored.detail["username"] is None
    reopened.close()


def test_known_canonical_identity_is_not_erased_by_placeholder_section_values(tmp_path: Path) -> None:
    path = tmp_path / "identity-placeholder.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO entities(id, type, name, username, updated_at) VALUES (42, 'user', 'Known', 'known', 1)")
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at) VALUES (42, ?, 90)",
        ('{"schema":1,"id":42,"type":"unknown","name":"Old","username":"old"}',),
    )
    conn.commit()
    repo = EntityProfileRepository(conn, section_ttl_seconds=10)
    repo.save_core({"id": 42, "type": "unknown", "name": None, "username": None}, now=95)
    assert conn.execute("SELECT type, name, username FROM entities WHERE id=42").fetchone() == (
        "user",
        "Known",
        "known",
    )
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    assert repo.commit_section(
        cursor,
        EntitySectionCommit({"type": "unknown", "name": None, "username": ""}),
        now=101,
    )
    assert conn.execute("SELECT type, name, username FROM entities WHERE id=42").fetchone() == (
        "user",
        "Known",
        "known",
    )
    stored = repo.read(42, now=101)
    assert stored is not None
    assert stored.detail["type"] == "user"
    assert stored.detail["name"] == "Known"
    assert stored.detail["username"] == "known"
    conn.close()


def test_restart_keeps_original_observation_time_and_same_generation_channel_completion(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit({"about": "usable"}, evidence=_evidence(cursor.generation)),
        EntitySectionCommit(
            {"personal_channel": None},
            status="unavailable",
            reason="channel_metadata_unavailable",
            evidence=_evidence(cursor.generation, outcome="unavailable"),
        ),
        now=101,
    )
    conn.close()
    reopened = sqlite3.connect(path)
    reopened_repo = EntityProfileRepository(reopened, section_ttl_seconds=10)
    profile = reopened_repo.read(42, now=109)
    assert profile is not None
    assert profile.sections["full_profile"]["status"] == "fresh"
    assert profile.sections["full_profile"]["observed_at"] == 100
    assert profile.sections["personal_channel"]["status"] == "unavailable"
    cursor = reopened_repo.next_due_refresh(now=109)
    assert cursor is not None and cursor.next_section == "common_chats"
    while cursor is not None:
        status = "not_applicable" if cursor.next_section == "contact_overlap" else "fresh"
        assert reopened_repo.commit_section(cursor, EntitySectionCommit({}, status=status), now=109)
        cursor = reopened_repo.next_due_refresh(now=109)
    assert reopened.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        "complete",
    )
    assert reopened_repo.read(42, now=110).sections["full_profile"]["status"] == "stale"  # type: ignore[union-attr]
    reopened.close()


def test_same_generation_channel_completion_ignores_ttl(tmp_path: Path) -> None:
    path = tmp_path / "same-generation-ttl.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    identity = {"account_generation": 7, "entity_type": "user"}
    evidence = ProfileAcquisitionEvidence(
        generation=cursor.generation,
        outcome="absent",
        provenance={
            "endpoint": "users.GetFullUser",
            "declared_fields": [
                "personal_channel_id",
                "personal_channel_message",
                "title",
                "username",
            ],
            "materialized_fields": ["personal_channel_id"],
            "authoritative": True,
        },
        normalization_version="entity-profile-full-user-v1",
        observation_started_at=100,
        observation_completed_at=101,
        identity=identity,
    )
    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit({"about": "old"}, evidence=_evidence(cursor.generation)),
        EntitySectionCommit(
            {"personal_channel": None},
            status="unavailable",
            evidence=evidence,
        ),
        now=101,
    )
    conn.execute(
        "UPDATE entity_profile_refresh_state SET next_section='personal_channel', acquisition_cursor=0 "
        "WHERE entity_id=42"
    )
    conn.commit()
    channel_cursor = repo.next_due_refresh(now=10_000)
    assert channel_cursor is not None
    assert not repo.complete_same_generation_section(channel_cursor, None, now=10_000)
    assert repo.complete_same_generation_section(channel_cursor, identity, now=10_000)
    assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        "complete",
    )
    conn.close()


@pytest.mark.parametrize(
    ("generation_delta", "identity"),
    ((1, {"account_generation": 7, "entity_type": "user"}), (0, {"account_generation": 8, "entity_type": "user"})),
)
def test_same_generation_channel_completion_rejects_changed_generation_or_scope(
    tmp_path: Path,
    generation_delta: int,
    identity: dict[str, object],
) -> None:
    path = tmp_path / "same-generation-fence.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    stored_identity = {"account_generation": 7, "entity_type": "user"}
    evidence = ProfileAcquisitionEvidence(
        generation=cursor.generation,
        outcome="absent",
        provenance={
            "endpoint": "users.GetFullUser",
            "declared_fields": ["personal_channel_id", "personal_channel_message", "title", "username"],
            "materialized_fields": ["personal_channel_id"],
            "authoritative": True,
        },
        normalization_version="entity-profile-full-user-v1",
        observation_started_at=100,
        observation_completed_at=101,
        identity=stored_identity,
    )
    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit({"about": "old"}, evidence=_evidence(cursor.generation)),
        EntitySectionCommit({"personal_channel": None}, evidence=evidence),
        now=101,
    )
    conn.execute(
        "UPDATE entity_profile_refresh_state SET next_section='personal_channel', acquisition_cursor=0, generation=generation+? "
        "WHERE entity_id=42",
        (generation_delta,),
    )
    conn.commit()
    fenced_cursor = repo.next_due_refresh(now=10_000)
    assert fenced_cursor is not None
    assert not repo.complete_same_generation_section(fenced_cursor, identity, now=10_000)
    assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() != (
        "complete",
    )
    refreshed_cursor = repo.next_due_refresh(now=10_000)
    assert refreshed_cursor is not None and refreshed_cursor.next_section == "full_profile"
    assert refreshed_cursor.generation > fenced_cursor.generation
    conn.close()


def test_same_generation_completion_rejects_corrupt_provenance_or_payload(tmp_path: Path) -> None:
    path = tmp_path / "same-generation-corrupt.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    identity = {"account_generation": 7, "entity_type": "user"}
    evidence = ProfileAcquisitionEvidence(
        generation=cursor.generation,
        outcome="usable",
        provenance={
            "endpoint": "users.GetFullUser",
            "declared_fields": ["personal_channel_id", "personal_channel_message", "title", "username"],
            "materialized_fields": ["personal_channel_id"],
            "authoritative": True,
        },
        normalization_version="entity-profile-full-user-v1",
        observation_started_at=100,
        observation_completed_at=101,
        identity=identity,
    )
    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit({"about": "old"}, evidence=_evidence(cursor.generation)),
        EntitySectionCommit({"personal_channel": {"channel_id": 1}}, payload={"channel_id": 1}, evidence=evidence),
        now=101,
    )
    conn.execute(
        "UPDATE entity_profile_refresh_state SET next_section='personal_channel', acquisition_cursor=0 WHERE entity_id=42"
    )
    conn.execute(
        "UPDATE entity_detail_sections SET provenance_json='{}', payload_json='not-json' "
        "WHERE entity_id=42 AND section='personal_channel'"
    )
    conn.commit()
    fenced_cursor = repo.next_due_refresh(now=10_000)
    assert fenced_cursor is not None
    assert not repo.complete_same_generation_section(fenced_cursor, identity, now=10_000)
    conn.close()


@pytest.mark.parametrize(
    ("started_at", "completed_at"),
    ((101, 100), (100, 10_001)),
)
def test_receipts_with_invalid_observation_bounds_are_rejected(
    tmp_path: Path, started_at: int, completed_at: int
) -> None:
    path = tmp_path / "invalid-bounds.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    identity = {"account_generation": 7, "entity_type": "user"}
    evidence = ProfileAcquisitionEvidence(
        generation=cursor.generation,
        outcome="usable",
        provenance={
            "endpoint": "users.GetFullUser",
            "declared_fields": list(FULL_PROFILE_OWNED_FIELDS),
            "materialized_fields": ["about"],
            "authoritative": True,
        },
        normalization_version="entity-profile-full-user-v1",
        observation_started_at=100,
        observation_completed_at=101,
        identity=identity,
    )
    assert repo.commit_section(cursor, EntitySectionCommit({"about": "old"}, evidence=evidence), now=101)
    conn.execute(
        "UPDATE entity_detail_sections SET observation_started_at=?, observation_completed_at=? "
        "WHERE entity_id=42 AND section='full_profile'",
        (started_at, completed_at),
    )
    conn.commit()
    assert not repo.section_is_reusable(42, "full_profile", identity=identity, now=100)
    conn.close()


def test_profile_retry_boundaries_are_pure_and_bounded() -> None:
    assert throttled_retry_at(100, None) == 101
    assert throttled_retry_at(100, 0) == 101
    assert throttled_retry_at(100, -5) == 101
    assert throttled_retry_at(100, 7) == 107
    assert failure_retry_at(100) == 160
    assert scope_changed_retry_at(100) == 101


def test_observation_owner_and_auth_scope_are_persisted_separately(tmp_path: Path) -> None:
    path = tmp_path / "ownership.sqlite"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
    repo.mark_pending(42, now=100)
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    scope = {"version": 1, "account_id": 7, "dc_id": 2, "auth_key_id": 9}
    commit = EntitySectionCommit(
        {"about": "owned"},
        evidence=_evidence(cursor.generation),
        observation_owner_account_id=7,
        observation_auth_scope=scope,
        ownership_observed=True,
    )
    assert repo.commit_section(cursor, commit, now=101)
    assert conn.execute(
        "SELECT profile_owner_account_id, profile_observation_scope_json FROM entity_details WHERE entity_id=42"
    ).fetchone() == (7, '{"account_id":7,"auth_key_id":9,"dc_id":2,"version":1}')
    stored = repo.read(42, now=101)
    assert stored is not None
    assert stored.profile_owner_account_id == 7
    assert stored.profile_observation_scope == scope
    conn.close()
