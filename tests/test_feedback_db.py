"""Tests for feedback_db.py schema lifecycle and constants."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

from mcp_telegram.feedback_db import (
    _FEEDBACK_SCHEMA_VERSION,
    VALID_SEVERITIES,
    ensure_feedback_schema,
    get_feedback_db_path,
)


def test_ensure_feedback_schema_creates_table(
    make_feedback_db: Callable[[], tuple[sqlite3.Connection, Path]],
) -> None:
    """ensure_feedback_schema creates the feedback table with zero rows."""
    conn, _ = make_feedback_db()
    row = cast(tuple[int] | None, conn.execute("SELECT 1 FROM feedback").fetchone())
    # Table exists; no rows yet
    assert row is None


def test_ensure_feedback_schema_records_version(
    make_feedback_db: Callable[[], tuple[sqlite3.Connection, Path]],
) -> None:
    """After ensure_feedback_schema, schema_version table has MAX(version) == 1."""
    conn, _ = make_feedback_db()
    row = cast(tuple[int] | None, conn.execute("SELECT MAX(version) FROM schema_version").fetchone())
    assert row is not None
    typed_row = cast(tuple[int], row)
    assert typed_row[0] == _FEEDBACK_SCHEMA_VERSION


def test_ensure_feedback_schema_idempotent(tmp_path: Path) -> None:
    """Calling ensure_feedback_schema twice does NOT raise; second call adds no new version rows."""
    db_path = tmp_path / "feedback_idem.db"
    conn1 = ensure_feedback_schema(db_path)
    conn2 = ensure_feedback_schema(db_path)
    try:
        # After two calls, schema_version contains exactly one row per applied version.
        # Idempotency: the second call adds no new rows.
        row = cast(tuple[int] | None, conn2.execute("SELECT COUNT(*) FROM schema_version").fetchone())
        assert row is not None
        count = cast(tuple[int], row)[0]
        assert count == _FEEDBACK_SCHEMA_VERSION
    finally:
        conn2.close()
        conn1.close()


def test_ensure_feedback_schema_wal_mode(
    make_feedback_db: Callable[[], tuple[sqlite3.Connection, Path]],
) -> None:
    """After ensure_feedback_schema, PRAGMA journal_mode returns 'wal'."""
    conn, _ = make_feedback_db()
    row = cast(tuple[str] | None, conn.execute("PRAGMA journal_mode").fetchone())
    assert row is not None
    assert str(cast(tuple[str], row)[0]).lower() == "wal"


def test_get_feedback_db_path_uses_explicit_state_dir(tmp_path: Path) -> None:
    """The path helper is pure; composition roots provide the state directory."""
    state_dir = tmp_path / "deployed-state"

    assert get_feedback_db_path(state_dir) == state_dir / "feedback.db"


def test_valid_severities_constant() -> None:
    """VALID_SEVERITIES is a frozenset equal to {bug, suggestion, question}."""
    assert frozenset({"bug", "suggestion", "question"}) == VALID_SEVERITIES
    assert isinstance(VALID_SEVERITIES, frozenset)


def test_feedback_table_columns_match_spec(
    make_feedback_db: Callable[[], tuple[sqlite3.Connection, Path]],
) -> None:
    """PRAGMA table_info(feedback) returns exactly the specified columns."""
    conn, _ = make_feedback_db()
    rows = cast(list[tuple[int, str, str, int, object, int]], conn.execute("PRAGMA table_info(feedback)").fetchall())
    # columns: cid, name, type, notnull, dflt_value, pk
    col_map: dict[str, tuple[int, str, str, int, object, int]] = {row[1]: row for row in rows}

    expected_columns = {
        "id",
        "submitted_at",
        "message",
        "severity",
        "context",
        "model",
        "harness",
        "status",
        "status_changed_at",
        "status_comment",
    }
    assert set(col_map.keys()) == expected_columns

    # id: INTEGER, PK=1
    assert col_map["id"][2].upper() == "INTEGER"
    assert col_map["id"][5] == 1  # pk

    # submitted_at: INTEGER, NOT NULL
    assert col_map["submitted_at"][2].upper() == "INTEGER"
    assert col_map["submitted_at"][3] == 1  # notnull

    # message: TEXT, NOT NULL
    assert col_map["message"][2].upper() == "TEXT"
    assert col_map["message"][3] == 1  # notnull

    # severity: TEXT, nullable
    assert col_map["severity"][2].upper() == "TEXT"
    assert col_map["severity"][3] == 0  # nullable

    # context: TEXT, nullable
    assert col_map["context"][2].upper() == "TEXT"
    assert col_map["context"][3] == 0  # nullable

    # model: TEXT, nullable
    assert col_map["model"][2].upper() == "TEXT"
    assert col_map["model"][3] == 0  # nullable

    # harness: TEXT, nullable
    assert col_map["harness"][2].upper() == "TEXT"
    assert col_map["harness"][3] == 0  # nullable

    # status: TEXT NOT NULL DEFAULT 'open'
    assert col_map["status"][2].upper() == "TEXT"
    assert col_map["status"][3] == 1  # notnull
    assert col_map["status"][4] == "'open'"  # dflt_value as quoted string in PRAGMA output

    # status_changed_at: INTEGER, nullable, no default
    assert col_map["status_changed_at"][2].upper() == "INTEGER"
    assert col_map["status_changed_at"][3] == 0

    # status_comment: TEXT, nullable, no default
    assert col_map["status_comment"][2].upper() == "TEXT"
    assert col_map["status_comment"][3] == 0


def test_feedback_schema_v2_migration(tmp_path: Path) -> None:
    """Fresh DB reaches schema_version 2 with all v2 columns present."""
    db_path = tmp_path / "feedback_v2.db"
    conn = ensure_feedback_schema(db_path)
    try:
        row = cast(tuple[int] | None, conn.execute("SELECT MAX(version) FROM schema_version").fetchone())
        assert row is not None
        assert cast(tuple[int], row)[0] == 2

        table_info = cast(
            list[tuple[int, str, str, int, object, int]], conn.execute("PRAGMA table_info(feedback)").fetchall()
        )
        cols = {row[1] for row in table_info}
        assert "status" in cols
        assert "status_changed_at" in cols
        assert "status_comment" in cols
    finally:
        conn.close()


def test_feedback_schema_v1_to_v2_preserves_rows(tmp_path: Path) -> None:
    """A pre-existing v1 schema with rows is upgraded to v2 with status='open' applied."""
    import sqlite3

    db_path = tmp_path / "feedback_legacy.db"

    # Build a v1-only schema by hand (mirror the v1 DDL in feedback_db.py)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)")
    conn.execute(
        "CREATE TABLE feedback ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "submitted_at INTEGER NOT NULL, "
        "message TEXT NOT NULL, "
        "severity TEXT, context TEXT, model TEXT, harness TEXT)"
    )
    conn.execute("INSERT INTO schema_version VALUES (1, strftime('%s','now'))")
    conn.execute(
        "INSERT INTO feedback (submitted_at, message, severity) VALUES (?, ?, ?)",
        (1700000000, "legacy row", "bug"),
    )
    conn.commit()
    conn.close()

    # Now run the real migration
    conn = ensure_feedback_schema(db_path)
    try:
        row = cast(
            tuple[int, str, str, str, int | None, str | None] | None,
            conn.execute(
                "SELECT submitted_at, message, severity, status, status_changed_at, status_comment "
                "FROM feedback WHERE message='legacy row'"
            ).fetchone(),
        )
        assert row is not None
        assert row[0] == 1700000000
        assert row[1] == "legacy row"
        assert row[2] == "bug"
        assert row[3] == "open"  # default applied by ALTER TABLE
        assert row[4] is None
        assert row[5] is None

        # schema_version reflects v2
        max_v_row = cast(tuple[int] | None, conn.execute("SELECT MAX(version) FROM schema_version").fetchone())
        assert max_v_row is not None
        assert max_v_row[0] == 2
    finally:
        conn.close()
