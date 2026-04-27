"""CLI tests for the `mcp-telegram feedback` sub-app (Phase 48)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcp_telegram import app
from mcp_telegram.feedback_db import ensure_feedback_schema

runner = CliRunner()


@pytest.fixture
def feedback_db(tmp_path, monkeypatch) -> Path:
    """Redirect get_feedback_db_path() to a tmp dir; return the path (file may or may not exist)."""
    target = tmp_path / "feedback.db"
    monkeypatch.setattr(
        "mcp_telegram.feedback_db.get_feedback_db_path",
        lambda: target,
    )
    # Mirror the patched path for the CLI module's local lookup (it imports the same function).
    return target


def _insert(
    path: Path,
    message: str,
    severity: str | None = None,
    context: str | None = None,
    model: str | None = None,
    harness: str | None = None,
) -> int:
    """Insert one feedback row directly; return the autoincrement id."""
    if not path.exists():
        ensure_feedback_schema(path).close()
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(
            "INSERT INTO feedback (submitted_at, message, severity, context, model, harness) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), message, severity, context, model, harness),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_feedback_list_no_db_file(feedback_db):
    """No file at all — print empty-state, exit 0."""
    result = runner.invoke(app, ["feedback", "list"])
    assert result.exit_code == 0, result.stdout
    assert "No feedback" in result.stdout or "no feedback" in result.stdout.lower()


def test_feedback_list_empty_db(feedback_db):
    """File exists, schema applied, zero rows — exit 0 with empty-state message."""
    ensure_feedback_schema(feedback_db).close()
    result = runner.invoke(app, ["feedback", "list"])
    assert result.exit_code == 0, result.stdout
    assert "no feedback" in result.stdout.lower() or "No feedback" in result.stdout


def test_feedback_list_shows_rows(feedback_db):
    id1 = _insert(
        feedback_db,
        "ListMessages returned stale rows",
        severity="bug",
        context="Saved Messages, limit=50",
        model="claude-opus-4-7",
        harness="Claude Desktop",
    )
    id2 = _insert(feedback_db, "Add a GetReactionAuthors tool", severity="suggestion")
    id3 = _insert(feedback_db, "Should ListDialogs include archived?", severity="question")
    result = runner.invoke(app, ["feedback", "list"])
    assert result.exit_code == 0, result.stdout
    for needle in (
        "ListMessages returned stale rows",
        "Add a GetReactionAuthors tool",
        "Should ListDialogs include archived?",
    ):
        assert needle in result.stdout, f"missing {needle!r} in:\n{result.stdout}"
    # severity tags surfaced
    for tag in ("bug", "suggestion", "question"):
        assert tag in result.stdout


def test_feedback_list_respects_limit(feedback_db):
    ids = [_insert(feedback_db, f"row {i}") for i in range(5)]
    result = runner.invoke(app, ["feedback", "list", "--limit", "2"])
    assert result.exit_code == 0, result.stdout
    # Most recent two IDs (highest) should appear; oldest should not.
    assert f"id={ids[-1]}" in result.stdout
    assert f"id={ids[-2]}" in result.stdout
    assert f"id={ids[0]}" not in result.stdout


def test_feedback_delete_existing_row(feedback_db):
    rid = _insert(feedback_db, "delete me")
    result = runner.invoke(app, ["feedback", "delete", str(rid)])
    assert result.exit_code == 0, result.stdout
    assert "deleted" in result.stdout.lower()
    # Verify row gone
    conn = sqlite3.connect(str(feedback_db))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE id = ?", (rid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_feedback_delete_missing_id(feedback_db):
    ensure_feedback_schema(feedback_db).close()
    result = runner.invoke(app, ["feedback", "delete", "999"])
    assert result.exit_code == 1, f"exit_code={result.exit_code}, stdout={result.stdout}"
    assert "not found" in result.stdout.lower()


def test_feedback_delete_no_db_file(feedback_db):
    """feedback.db never created — delete should fail gracefully, not raise."""
    assert not feedback_db.exists()
    result = runner.invoke(app, ["feedback", "delete", "1"])
    assert result.exit_code == 1
    assert "no feedback" in result.stdout.lower() or "not found" in result.stdout.lower()
