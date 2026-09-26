from __future__ import annotations

import sqlite3
from typing import cast

import pytest

from mcp_telegram.sync_db import (
    _DIALOG_DIRECTORY_STATE_DDL,
    _DIALOGS_V74_DDL,
    _apply_migration_78,
    _apply_migrations,
)


def test_fresh_schema_has_separate_identity_fence_and_complete_presence_trigger(request: pytest.FixtureRequest) -> None:
    conn = sqlite3.connect(":memory:")
    request.addfinalizer(conn.close)
    _apply_migrations(conn)
    version_row = cast(tuple[int], conn.execute("SELECT MAX(version) FROM schema_version").fetchone())
    assert version_row[0] == 78
    dialog_columns = cast(list[tuple[object, str]], conn.execute("PRAGMA table_info(dialogs)").fetchall())
    columns = {row[1] for row in dialog_columns}
    assert "identity_revision" in columns
    trigger_row = cast(
        tuple[str],
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='dialogs_revision_after_update'"
        ).fetchone(),
    )
    trigger = trigger_row[0]
    update_columns = trigger.split("UPDATE OF", 1)[1].split("ON dialogs", 1)[0]
    captured = {value.strip() for value in update_columns.replace("\n", "").split(",")}
    identity = {
        "name",
        "type",
        "username",
        "identity_observed_at",
        "identity_complete",
        "identity_source",
        "identity_revision",
    }
    mutable = columns - {"dialog_id", "revision"} - identity
    assert captured == mutable
    baseline_columns = cast(
        list[tuple[object, str]], conn.execute("PRAGMA table_info(dialog_directory_baseline)").fetchall()
    )
    staging_columns = cast(
        list[tuple[object, str]], conn.execute("PRAGMA table_info(dialog_directory_staging)").fetchall()
    )
    assert "baseline_identity_revision" in {row[1] for row in baseline_columns}
    assert "identity_fields_observed" in {row[1] for row in staging_columns}


def test_v77_upgrade_seeds_only_legacy_dialog_facts_and_restarts_incomplete_generation(
    request: pytest.FixtureRequest,
) -> None:
    conn = sqlite3.connect(":memory:")
    request.addfinalizer(conn.close)
    _apply_migrations(conn)
    conn.execute("DROP TRIGGER dialogs_revision_after_update")
    conn.execute(_DIALOGS_V74_DDL)
    conn.execute(
        "INSERT INTO dialogs_v74(dialog_id,name,type,username,identity_complete) VALUES (1,'Old','user','old',0)"
    )
    conn.execute("INSERT INTO dialogs_v74(dialog_id,name,type,username) VALUES (2,NULL,'unknown',NULL)")
    conn.execute("PRAGMA legacy_alter_table=ON")
    conn.execute("DROP TABLE dialogs")
    conn.execute("ALTER TABLE dialogs_v74 RENAME TO dialogs")
    conn.execute("PRAGMA legacy_alter_table=OFF")
    conn.execute("DROP TABLE dialog_directory_baseline")
    conn.execute(
        "CREATE TABLE dialog_directory_baseline(generation INTEGER,dialog_id INTEGER,baseline_revision INTEGER NOT NULL,seen INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(generation,dialog_id)) WITHOUT ROWID"
    )
    conn.execute(_DIALOG_DIRECTORY_STATE_DDL)
    conn.execute(
        "UPDATE dialog_directory_state SET generation=4,status='in_progress',ordinary_status='incomplete',pinned_main_status='complete',pinned_archive_status='incomplete',offset_id=6,observed_count=2 WHERE singleton=1"
    )
    conn.execute("INSERT INTO dialog_directory_baseline VALUES (4,1,2,0)")
    conn.execute("INSERT INTO dialog_directory_pins VALUES (4,0,1,0)")
    conn.execute("DELETE FROM schema_version WHERE version=78")
    conn.commit()
    assert _apply_migration_78(conn, 77) == 78
    rows = cast(
        list[tuple[int, str | None, str | None, str | None, str | None, int, int | None, int]],
        conn.execute(
            "SELECT dialog_id,name,type,username,identity_source,identity_complete,identity_observed_at,identity_revision FROM dialogs ORDER BY dialog_id"
        ).fetchall(),
    )
    assert rows == [
        (1, "Old", "user", "old", "legacy", 0, None, 0),
        (2, None, "unknown", None, None, 0, None, 0),
    ]
    state = cast(
        tuple[str, str | None],
        conn.execute("SELECT status,reason FROM dialog_directory_state WHERE singleton=1").fetchone(),
    )
    assert state == ("pending", "identity_baseline_cutover")
    assert conn.execute("SELECT COUNT(*) FROM dialog_directory_baseline").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dialog_directory_pins").fetchone()[0] == 0
    assert (
        "profile" in conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='dialogs'").fetchone()[0]
    )
