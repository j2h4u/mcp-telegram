"""Database admission must reject incompatible files before modifying them."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from mcp_telegram import feedback_db, sync_db


@pytest.mark.parametrize("kind", ["sync", "feedback"])
@pytest.mark.parametrize(
    "defect",
    [
        "foreign",
        "foreign_view",
        "sqlite_prefix",
        "marker",
        "future",
        "gap",
        "duplicate",
        "empty_ledger",
        "malformed_ledger",
        "missing_table",
    ],
)
def test_rejected_database_is_unchanged(tmp_path: Path, kind: str, defect: str) -> None:  # noqa: PLR0912
    path = tmp_path / f"{kind}.db"
    ensure = sync_db.ensure_sync_schema if kind == "sync" else feedback_db.ensure_feedback_schema
    current = sync_db._CURRENT_SCHEMA_VERSION if kind == "sync" else feedback_db._FEEDBACK_SCHEMA_VERSION
    if defect not in {"foreign", "foreign_view", "sqlite_prefix", "marker"}:
        result = ensure(path)
        if result is not None:
            result.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        if defect == "foreign":
            conn.execute("CREATE TABLE foreign_data (value TEXT)")
            conn.execute("INSERT INTO foreign_data VALUES ('retained')")
        elif defect == "foreign_view":
            conn.execute("CREATE VIEW foreign_view AS SELECT 1")
        elif defect == "sqlite_prefix":
            conn.execute("CREATE TABLE sqliteX (value TEXT)")
        elif defect == "marker":
            conn.execute("PRAGMA application_id=123")
        elif defect == "future":
            conn.execute("INSERT INTO schema_version VALUES (?, 0)", (current + 1,))
        elif defect == "gap":
            conn.execute("DELETE FROM schema_version WHERE version = 1")
        elif defect == "duplicate":
            conn.execute("CREATE TABLE old_versions AS SELECT * FROM schema_version")
            conn.execute("DROP TABLE schema_version")
            conn.execute("ALTER TABLE old_versions RENAME TO schema_version")
            conn.execute("INSERT INTO schema_version VALUES (1, 0)")
        elif defect == "empty_ledger":
            conn.execute("DELETE FROM schema_version")
        elif defect == "malformed_ledger":
            conn.execute("DROP TABLE schema_version")
            conn.execute("CREATE TABLE schema_version (wrong_column INTEGER)")
        else:
            conn.execute("DROP TABLE " + ("synced_dialogs" if kind == "sync" else "feedback"))
    before = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        ensure(path)
    assert path.read_bytes() == before


def test_sync_postcondition_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_db, "_apply_migrations", lambda conn: None)
    with pytest.raises(sqlite3.DatabaseError, match="did not reach"):
        sync_db.ensure_sync_schema(tmp_path / "sync.db")


def test_failed_first_migration_can_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sync.db"
    original = sync_db._SYNCED_DIALOGS_DDL
    monkeypatch.setattr(sync_db, "_SYNCED_DIALOGS_DDL", "INVALID SQL")
    with pytest.raises(sqlite3.OperationalError):
        sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    monkeypatch.setattr(sync_db, "_SYNCED_DIALOGS_DDL", original)
    sync_db.ensure_sync_schema(path)


@pytest.mark.parametrize("kind", ["sync", "feedback"])
def test_lookalike_schema_is_rejected_before_wal(tmp_path: Path, kind: str) -> None:
    path = tmp_path / f"{kind}.db"
    current = sync_db._CURRENT_SCHEMA_VERSION if kind == "sync" else feedback_db._FEEDBACK_SCHEMA_VERSION
    ensure = sync_db.ensure_sync_schema if kind == "sync" else feedback_db.ensure_feedback_schema
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)")
        conn.executemany("INSERT INTO schema_version VALUES (?, 0)", ((v,) for v in range(1, current + 1)))
        for table in ("synced_dialogs", "messages", "message_versions") if kind == "sync" else ("feedback",):
            conn.execute(f"CREATE TABLE {table} (bogus INTEGER)")
    before = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError, match="incomplete"):
        ensure(path)
    assert path.read_bytes() == before


def test_feedback_v2_missing_status_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "feedback.db"
    with closing(feedback_db.ensure_feedback_schema(path)) as conn:
        conn.execute("ALTER TABLE feedback DROP COLUMN status")
    before = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError, match="incomplete"):
        feedback_db.ensure_feedback_schema(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["sync", "feedback"])
@pytest.mark.parametrize("marker", ["application_id", "user_version"])
def test_marked_existing_database_is_unchanged(tmp_path: Path, kind: str, marker: str) -> None:
    path = tmp_path / f"{kind}.db"
    ensure = sync_db.ensure_sync_schema if kind == "sync" else feedback_db.ensure_feedback_schema
    result = ensure(path)
    if result is not None:
        result.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"PRAGMA {marker}=123")
    before = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError, match="markers"):
        ensure(path)
    assert path.read_bytes() == before
