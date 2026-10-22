"""Schema migration coverage for ordered profile field observations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.sync_db import ensure_sync_schema


def test_fresh_schema_creates_empty_field_observation_table_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    first_schema = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='entity_profile_field_observations'"
        ).fetchone(),
    )
    assert first_schema is not None
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_field_observations").fetchone() == (0,)
    column_rows = cast(
        list[tuple[object, ...]], conn.execute("PRAGMA table_info(entity_profile_field_observations)").fetchall()
    )
    assert [row[1] for row in column_rows] == [
        "entity_id",
        "field",
        "owner_account_id",
        "observation_order",
        "observed_at",
        "source",
    ]
    assert "WITHOUT ROWID" in str(first_schema[0]).upper()
    assert conn.execute("SELECT 1 FROM schema_version WHERE version=83").fetchone() == (1,)

    ensure_sync_schema(path)
    assert (
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='entity_profile_field_observations'"
        ).fetchone()
        == first_schema
    )
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_field_observations").fetchone() == (0,)
    conn.close()


def test_field_observation_constraints_and_entity_cascade(tmp_path: Path) -> None:
    path = tmp_path / "constraints.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO entities(id, type, updated_at) VALUES (7, 'user', 1)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO entity_profile_field_observations VALUES (99, 'name', 7, 1, 100, 'rpc')")
    for order, source in ((0, "rpc"), (1, "guess")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO entity_profile_field_observations VALUES (7, 'name', 7, ?, 100, ?)",
                (order, source),
            )
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_field_observations").fetchone() == (0,)

    conn.execute("INSERT INTO entity_profile_field_observations VALUES (7, 'name', 7, 1, 100, 'rpc')")
    conn.execute("DELETE FROM entities WHERE id=7")
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_field_observations").fetchone() == (0,)
    conn.close()


def test_upgrade_from_v82_preserves_profile_facts_and_creates_no_field_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "upgrade-v82.sqlite"
    with monkeypatch.context() as legacy:
        legacy.setattr("mcp_telegram.sync_db._apply_migration_83", lambda _conn, current: current)
        legacy.setattr("mcp_telegram.sync_db._apply_migration_84", lambda _conn, current: current)
        ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO entities(id, type, name, updated_at) VALUES (42, 'user', 'Target', 10)")
    detail_json = json.dumps({"schema": 1, "id": 42, "type": "user", "about": "retained"})
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at, profile_revision) VALUES (42, ?, 1234, 5)",
        (detail_json,),
    )
    conn.execute(
        "INSERT INTO entity_detail_sections "
        "(entity_id, section, status, observed_at, payload_json, acquisition_generation, acquisition_outcome, "
        "provenance_json, normalization_version, observation_started_at, observation_completed_at, "
        "acquisition_identity_json) VALUES (42, 'full_profile', 'fresh', 1234, '{\"about\":\"retained\"}', "
        "12, 'usable', '{\"endpoint\":\"users.GetFullUser\"}', 'v1', 1220, 1234, '{\"account\":42}')"
    )
    conn.execute(
        "INSERT INTO entity_profile_refresh_state "
        "(entity_id, status, retry_at, reason, updated_at, next_section, acquisition_cursor, generation, started_at, "
        "pair_eligible, follow_up_required, profile_revision) "
        "VALUES (42, 'failed', 9999, 'flooded', 1234, 'personal_channel', 1, 12, 1200, 1, 0, 5)"
    )
    conn.commit()
    before = tuple(
        conn.execute(f"SELECT * FROM {table} WHERE entity_id=42").fetchall()
        for table in ("entity_details", "entity_detail_sections", "entity_profile_refresh_state")
    )

    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (82,)
    conn.close()

    ensure_sync_schema(path)
    upgraded = sqlite3.connect(path)
    after = tuple(
        upgraded.execute(f"SELECT * FROM {table} WHERE entity_id=42").fetchall()
        for table in ("entity_details", "entity_detail_sections", "entity_profile_refresh_state")
    )
    assert after == before
    assert upgraded.execute("SELECT COUNT(*) FROM entity_profile_field_observations").fetchone() == (0,)
    assert upgraded.execute("SELECT 1 FROM schema_version WHERE version=83").fetchone() == (1,)
    upgraded.close()
