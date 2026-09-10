"""File-backed persistence tests for fenced Entity Profile refreshes."""

# pyright: reportAny=false

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import mcp_telegram.sync_db as sync_db_module
from mcp_telegram.entity_profile.contracts import ProfileAcquisitionEvidence
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


def test_generation_and_revision_fences_reject_stale_writers(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn, repo = _repository(path)
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
    assert not repo.commit_section(cursor, EntitySectionCommit({"name": "older"}), now=102)
    assert conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone() == (
        '{"schema":1,"id":42,"type":"user","name":"newer"}',
    )
    conn.execute(
        "UPDATE entity_profile_refresh_state SET generation=generation+1, profile_revision=1 WHERE entity_id=42"
    )
    conn.commit()
    assert not repo.commit_section(cursor, EntitySectionCommit({"name": "aba"}), now=103)
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
    assert reopened.execute(
        "SELECT status FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("complete",)
    assert reopened_repo.read(42, now=110).sections["full_profile"]["status"] == "stale"  # type: ignore[union-attr]
    reopened.close()
