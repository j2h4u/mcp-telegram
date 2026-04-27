"""RED tests for feedback_db.py — schema init, DDL contract, constants.

These tests deliberately import symbols from mcp_telegram.feedback_db which
does NOT yet exist.  The expected outcome is ImportError/ModuleNotFoundError
at collection time — confirming the RED state before 48-02 lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp_telegram.feedback_db import (
    _FEEDBACK_SCHEMA_VERSION,
    VALID_SEVERITIES,
    ensure_feedback_schema,
    get_feedback_db_path,
)


def test_ensure_feedback_schema_creates_table(make_feedback_db) -> None:
    """ensure_feedback_schema creates the feedback table with zero rows."""
    conn, _ = make_feedback_db()
    row = conn.execute("SELECT 1 FROM feedback").fetchone()
    # Table exists; no rows yet
    assert row is None


def test_ensure_feedback_schema_records_version(make_feedback_db) -> None:
    """After ensure_feedback_schema, schema_version table has MAX(version) == 1."""
    conn, _ = make_feedback_db()
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert row is not None
    assert row[0] == _FEEDBACK_SCHEMA_VERSION


def test_ensure_feedback_schema_idempotent(tmp_path: Path) -> None:
    """Calling ensure_feedback_schema twice does NOT raise and keeps version count at 1."""
    db_path = tmp_path / "feedback_idem.db"
    conn1 = ensure_feedback_schema(db_path)
    conn2 = ensure_feedback_schema(db_path)
    count = conn2.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 1


def test_ensure_feedback_schema_wal_mode(make_feedback_db) -> None:
    """After ensure_feedback_schema, PRAGMA journal_mode returns 'wal'."""
    conn, _ = make_feedback_db()
    row = conn.execute("PRAGMA journal_mode").fetchone()
    assert row is not None
    assert str(row[0]).lower() == "wal"


def test_get_feedback_db_path_under_xdg_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_feedback_db_path() returns a path ending in mcp-telegram/feedback.db."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = get_feedback_db_path()
    assert path.name == "feedback.db"
    assert path.parent.name == "mcp-telegram"
    assert path.parent.exists()


def test_valid_severities_constant() -> None:
    """VALID_SEVERITIES is a frozenset equal to {bug, suggestion, question}."""
    assert VALID_SEVERITIES == frozenset({"bug", "suggestion", "question"})
    assert isinstance(VALID_SEVERITIES, frozenset)


def test_feedback_table_columns_match_spec(make_feedback_db) -> None:
    """PRAGMA table_info(feedback) returns exactly the specified columns."""
    conn, _ = make_feedback_db()
    rows = conn.execute("PRAGMA table_info(feedback)").fetchall()
    # columns: cid, name, type, notnull, dflt_value, pk
    col_map = {row[1]: row for row in rows}

    expected_columns = {"id", "submitted_at", "message", "severity", "context", "model", "harness"}
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
