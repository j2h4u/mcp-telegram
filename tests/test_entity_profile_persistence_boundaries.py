"""Process and migration crash-boundary checks for Entity Profile storage."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

import mcp_telegram.sync_db as sync_db_module
from mcp_telegram.entity_profile.contracts import (
    FULL_PROFILE_OWNED_FIELDS,
    FULL_USER_ENDPOINT,
    NORMALIZATION_VERSION,
    PERSONAL_CHANNEL_OWNED_FIELDS,
    PROFILE_SECTIONS,
    ProfileAcquisitionEvidence,
)
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.sync_db import _CURRENT_SCHEMA_VERSION, ensure_sync_schema


def _seed_pending_pair(path: Path, *, mode: str = "enabled") -> None:
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO entities(id, type, name, updated_at) VALUES (42, 'user', 'Target', 1)")
    conn.commit()
    EntityProfileRepository(conn, section_ttl_seconds=10).mark_pending(42, now=100, pair_mode_override=mode)
    conn.close()


def _pair_evidence(generation: int, section: str, *, outcome: str = "usable") -> ProfileAcquisitionEvidence:
    fields = FULL_PROFILE_OWNED_FIELDS if section == "full_profile" else PERSONAL_CHANNEL_OWNED_FIELDS
    materialized = ["about"] if section == "full_profile" else list(fields)
    if section == "personal_channel" and outcome == "absent":
        materialized = []
    return ProfileAcquisitionEvidence(
        generation=generation,
        outcome=outcome,
        provenance={
            "endpoint": FULL_USER_ENDPOINT,
            "declared_fields": list(fields),
            "materialized_fields": materialized,
            "authoritative": True,
        },
        normalization_version=NORMALIZATION_VERSION,
        observation_started_at=100,
        observation_completed_at=101,
        identity={"account_generation": 7, "entity_type": "user"},
    )


def _run_child(path: Path, source: str, *, expected_returncode: int) -> None:
    result = subprocess.run(
        [sys.executable, "-c", source, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected_returncode, result.stderr


def test_process_exit_before_pair_commit_leaves_durable_work_required(tmp_path: Path) -> None:
    """A response that dies before the storage transaction remains pending."""
    path = tmp_path / "response-before-commit.sqlite"
    _seed_pending_pair(path)

    _run_child(
        path,
        """
import os
import sqlite3
import sys
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit

path = sys.argv[1]
conn = sqlite3.connect(path)
repo = EntityProfileRepository(conn, section_ttl_seconds=10)
cursor = repo.next_due_refresh(now=101)
assert cursor is not None

def crash_before_detail_write(statement):
    if statement.lstrip().startswith('UPDATE entity_details SET'):
        os._exit(73)

conn.set_trace_callback(crash_before_detail_write)
repo.commit_full_user_pair(
    cursor,
    EntitySectionCommit({'about': 'new'}),
    EntitySectionCommit({'personal_channel': None}, status='unavailable'),
    now=101,
)
raise AssertionError('the pre-commit failpoint did not fire')
""",
        expected_returncode=73,
    )

    conn = sqlite3.connect(path)
    repo = EntityProfileRepository(conn, section_ttl_seconds=10)
    cursor = repo.next_due_refresh(now=101)
    assert cursor is not None
    assert cursor.next_section == "full_profile"
    assert conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=42").fetchone() == (0,)
    assert dict(conn.execute("SELECT section, status FROM entity_detail_sections WHERE entity_id=42")) == dict.fromkeys(
        PROFILE_SECTIONS, "pending"
    )
    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit({"about": "new"}, evidence=_pair_evidence(cursor.generation, "full_profile")),
        EntitySectionCommit(
            {"personal_channel": None},
            status="fresh",
            evidence=_pair_evidence(cursor.generation, "personal_channel", outcome="absent"),
        ),
        now=101,
    )
    conn.close()


def test_pair_commit_survives_exit_before_notification_and_reuses_on_reopen(tmp_path: Path) -> None:
    """A committed pair is durable before notification and can be reused later."""
    path = tmp_path / "commit-before-notification.sqlite"
    _seed_pending_pair(path)

    _run_child(
        path,
        """
import os
import sqlite3
import sys
from mcp_telegram.entity_profile.contracts import FULL_PROFILE_OWNED_FIELDS, FULL_USER_ENDPOINT, NORMALIZATION_VERSION, PERSONAL_CHANNEL_OWNED_FIELDS, ProfileAcquisitionEvidence
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit

path = sys.argv[1]
conn = sqlite3.connect(path)
repo = EntityProfileRepository(conn, section_ttl_seconds=10)
cursor = repo.next_due_refresh(now=100)
assert cursor is not None
identity = {'account_generation': 7, 'entity_type': 'user'}
def evidence(section, outcome='usable'):
    fields = FULL_PROFILE_OWNED_FIELDS if section == 'full_profile' else PERSONAL_CHANNEL_OWNED_FIELDS
    materialized = ['about'] if section == 'full_profile' else list(fields)
    if section == 'personal_channel' and outcome == 'absent':
        materialized = []
    return ProfileAcquisitionEvidence(
        generation=cursor.generation, outcome=outcome,
        provenance={'endpoint': FULL_USER_ENDPOINT, 'declared_fields': list(fields), 'materialized_fields': materialized, 'authoritative': True},
        normalization_version=NORMALIZATION_VERSION, observation_started_at=100,
        observation_completed_at=101, identity=identity,
    )
assert repo.commit_full_user_pair(
    cursor,
    EntitySectionCommit({'about': 'new'}, evidence=evidence('full_profile')),
    EntitySectionCommit({'personal_channel': None}, status='fresh', evidence=evidence('personal_channel', 'absent')),
    now=101,
)
os._exit(74)
""",
        expected_returncode=74,
    )

    conn = sqlite3.connect(path)
    repo = EntityProfileRepository(conn, section_ttl_seconds=10)
    cursor = repo.next_due_refresh(now=101)
    assert cursor is not None
    assert cursor.next_section == "common_chats"
    assert conn.execute(
        "SELECT pair_mode, pair_measurement_complete, pair_summary_watermark FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("enabled", 1, 101)
    for section in PROFILE_SECTIONS[1:-1]:
        cursor = repo.next_due_refresh(now=101)
        assert cursor is not None and cursor.next_section == section
        assert repo.commit_section(cursor, EntitySectionCommit({}, status="not_applicable"), now=101)
    cursor = repo.next_due_refresh(now=101)
    assert cursor is not None and cursor.next_section == "personal_channel"
    assert repo.complete_same_generation_section(cursor, {"account_generation": 7, "entity_type": "user"}, now=101)
    assert repo.next_due_refresh(now=105) is None
    assert repo.read_section_evidence(42, "full_profile") is not None
    assert repo.read(42, now=105) is not None
    conn.close()


@pytest.mark.parametrize(
    ("version", "fail_marker", "expected_before"),
    ((61, "VALUES (61", 60), (62, "VALUES (62", 61)),
)
def test_interrupted_profile_migration_records_no_success_and_reopens_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    fail_marker: str,
    expected_before: int,
) -> None:
    path = tmp_path / f"migration-{version}.sqlite"
    with monkeypatch.context() as schema:
        schema.setattr(sync_db_module, "_CURRENT_SCHEMA_VERSION", expected_before)
        ensure_sync_schema(path)

    _run_child(
        path,
        f"""
import os
import sqlite3
import sys
import mcp_telegram.sync_db as sync_db

path = sys.argv[1]
original_open = sync_db._open_sync_db
def crash_before_ledger(statement):
    if {fail_marker!r} in statement:
        os._exit(75)

def open_with_failpoint(db_path, *, read_only=False):
    opened = original_open(db_path, read_only=read_only)
    opened.set_trace_callback(crash_before_ledger)
    return opened

sync_db._open_sync_db = open_with_failpoint
sync_db.ensure_sync_schema(__import__('pathlib').Path(path))
raise AssertionError('migration failpoint did not fire')
""",
        expected_returncode=75,
    )

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (expected_before,)
    conn.close()
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (_CURRENT_SCHEMA_VERSION,)
    assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version=?", (version,)).fetchone() == (1,)
    conn.close()


def test_migrated_profile_pair_mode_sequence_preserves_receipts_and_progress(tmp_path: Path) -> None:
    """Enabled, disabled, and re-enabled generations retain honest storage facts."""
    path = tmp_path / "mode-sequence.sqlite"
    _seed_pending_pair(path, mode="enabled")
    conn = sqlite3.connect(path)
    repo = EntityProfileRepository(conn, section_ttl_seconds=10)
    identity = {"account_generation": 7, "entity_type": "user"}

    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None and cursor.pair_mode == "enabled"
    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit({"about": "enabled"}, evidence=_pair_evidence(cursor.generation, "full_profile")),
        EntitySectionCommit(
            {"personal_channel": None},
            status="fresh",
            evidence=_pair_evidence(cursor.generation, "personal_channel", outcome="absent"),
        ),
        now=101,
    )
    repo.record_pair_section_outcome(cursor, "full_profile", outcome="usable", actual_attempts=1, ready_at=101)
    repo.record_pair_section_outcome(cursor, "personal_channel", outcome="absent", actual_attempts=0, ready_at=101)
    for section in PROFILE_SECTIONS[1:-1]:
        cursor = repo.next_due_refresh(now=101)
        assert cursor is not None and cursor.next_section == section
        assert repo.commit_section(cursor, EntitySectionCommit({}, status="not_applicable"), now=101)
    cursor = repo.next_due_refresh(now=101)
    assert cursor is not None and cursor.next_section == "personal_channel"
    assert repo.complete_same_generation_section(cursor, identity, now=101)

    repo.mark_pending(42, now=200, pair_mode_override="disabled")
    cursor = repo.next_due_refresh(now=200)
    assert cursor is not None and cursor.pair_mode == "disabled"
    assert repo.commit_section_with_pair_measurement(
        cursor,
        EntitySectionCommit({"about": "disabled"}, evidence=_pair_evidence(cursor.generation, "full_profile")),
        now=201,
        outcome="usable",
        actual_attempts=2,
    )[0]
    for section in PROFILE_SECTIONS[1:-1]:
        cursor = repo.next_due_refresh(now=201)
        assert cursor is not None and cursor.next_section == section
        assert repo.commit_section_with_pair_measurement(
            cursor, EntitySectionCommit({}, status="not_applicable"), now=202, outcome="unavailable", actual_attempts=0
        )[0]
    cursor = repo.next_due_refresh(now=201)
    assert cursor is not None and cursor.next_section == "personal_channel"
    assert repo.commit_section_with_pair_measurement(
        cursor,
        EntitySectionCommit(
            {"personal_channel": None},
            status="fresh",
            evidence=_pair_evidence(cursor.generation, "personal_channel", outcome="absent"),
        ),
        now=202,
        outcome="absent",
        actual_attempts=2,
    )[0]

    disabled = cast(
        tuple[object, ...],
        conn.execute(
            "SELECT generation, pair_mode, pair_full_profile_outcome, pair_personal_channel_outcome, pair_attempts, pair_measurement_complete "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone(),
    )
    assert disabled[1:] == ("disabled", "usable", "absent", 4, 1)

    repo.mark_pending(42, now=300, pair_mode_override="enabled")
    cursor = repo.next_due_refresh(now=300)
    assert cursor is not None and cursor.pair_mode == "enabled"
    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit({"about": "re-enabled"}, evidence=_pair_evidence(cursor.generation, "full_profile")),
        EntitySectionCommit(
            {"personal_channel": None},
            status="fresh",
            evidence=_pair_evidence(cursor.generation, "personal_channel", outcome="absent"),
        ),
        now=301,
    )
    repo.record_pair_section_outcome(cursor, "full_profile", outcome="usable", actual_attempts=1, ready_at=301)
    repo.record_pair_section_outcome(cursor, "personal_channel", outcome="absent", actual_attempts=0, ready_at=301)
    state = cast(
        tuple[object, ...],
        conn.execute(
            "SELECT pair_mode, pair_full_profile_outcome, pair_personal_channel_outcome, pair_attempts, pair_measurement_complete, next_section "
            "FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone(),
    )
    assert state == ("enabled", "usable", "absent", 1, 1, "common_chats")
    evidence = repo.read_section_evidence(42, "full_profile")
    assert evidence is not None and evidence["identity"] == identity
    profile = repo.read(42, now=301)
    assert profile is not None and profile.detail["about"] == "re-enabled"
    conn.close()
