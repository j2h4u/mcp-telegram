"""CLI tests for the `mcp-telegram feedback` sub-app (Phase 48/49)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def test_feedback_delete_removed(feedback_db):
    """`feedback delete` no longer exists — Typer exits 2 (no such command)."""
    result = runner.invoke(app, ["feedback", "delete", "1"])
    assert result.exit_code == 2


def _patched_daemon(mock_response: dict):
    """Build a patch context for daemon_connection used by feedback_status.

    Returns (patcher, mock_conn). Caller starts patcher in a `with` block.
    """
    mock_conn = AsyncMock()
    mock_conn.update_feedback_status.return_value = mock_response

    async_cm = MagicMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    async_cm.__aexit__ = AsyncMock(return_value=False)

    patcher = patch(
        "mcp_telegram.daemon_client.daemon_connection", return_value=async_cm
    )
    return patcher, mock_conn


def test_feedback_status_sets_status(feedback_db):
    """Valid id+status -> daemon client called, exit 0."""
    ok_response = {"ok": True, "data": {"message": "Feedback 1 status set to 'done'."}}
    patcher, mock_conn = _patched_daemon(ok_response)
    with patcher:
        result = runner.invoke(app, ["feedback", "status", "1", "done"])
    assert result.exit_code == 0, result.stdout
    mock_conn.update_feedback_status.assert_called_once_with(
        feedback_id=1, status="done", comment=None
    )


def test_feedback_status_with_comment(feedback_db):
    """--comment is forwarded to the daemon client."""
    ok_response = {"ok": True, "data": {"message": "Feedback 2 status set to 'dismissed'."}}
    patcher, mock_conn = _patched_daemon(ok_response)
    with patcher:
        result = runner.invoke(
            app, ["feedback", "status", "2", "dismissed", "--comment", "noise"]
        )
    assert result.exit_code == 0, result.stdout
    mock_conn.update_feedback_status.assert_called_once_with(
        feedback_id=2, status="dismissed", comment="noise"
    )


def test_feedback_status_invalid_enum(feedback_db):
    """Invalid status string -> exit 1 BEFORE any socket call."""
    patcher, mock_conn = _patched_daemon({"ok": True})
    with patcher:
        result = runner.invoke(app, ["feedback", "status", "1", "wontfix"])
    assert result.exit_code == 1
    # Critical: the socket was never opened
    mock_conn.update_feedback_status.assert_not_called()
    assert "Invalid status" in result.stdout


def test_feedback_status_daemon_error(feedback_db):
    """Daemon returns ok=False -> CLI exits 1 with the error message."""
    err_response = {"ok": False, "error": "not_found", "message": "Feedback id 99 not found."}
    patcher, mock_conn = _patched_daemon(err_response)
    with patcher:
        result = runner.invoke(app, ["feedback", "status", "99", "done"])
    assert result.exit_code == 1
    assert "not found" in result.stdout.lower()


def _set_status_direct(db_path, rid: int, status: str) -> None:
    """Test helper — directly UPDATE feedback.db (test-only; bypasses daemon)."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE feedback SET status=? WHERE id=?", (status, rid))
        conn.commit()
    finally:
        conn.close()


def test_feedback_list_default_hides_done(feedback_db):
    rid = _insert(feedback_db, "completed work")
    _set_status_direct(feedback_db, rid, "done")
    result = runner.invoke(app, ["feedback", "list"])
    assert result.exit_code == 0, result.stdout
    assert "completed work" not in result.stdout
    # Distinguishing message — history exists but is hidden by default filter
    assert "No open or in-progress feedback" in result.stdout
    assert "--all" in result.stdout


def test_feedback_list_all_shows_done(feedback_db):
    rid = _insert(feedback_db, "completed work")
    _set_status_direct(feedback_db, rid, "done")
    result = runner.invoke(app, ["feedback", "list", "--all"])
    assert result.exit_code == 0, result.stdout
    assert "completed work" in result.stdout
    assert "[done]" in result.stdout


def test_feedback_list_shows_status_column(feedback_db):
    _insert(feedback_db, "open bug")
    result = runner.invoke(app, ["feedback", "list"])
    assert result.exit_code == 0, result.stdout
    assert "[open]" in result.stdout


def test_feedback_list_truly_empty(feedback_db):
    """Table with zero rows -> 'No feedback recorded yet.', NOT the filter-hint message."""
    ensure_feedback_schema(feedback_db).close()
    result = runner.invoke(app, ["feedback", "list"])
    assert result.exit_code == 0, result.stdout
    assert "No feedback recorded yet." in result.stdout
    # The filter-hint message must NOT appear when the table is genuinely empty.
    assert "No open or in-progress feedback" not in result.stdout
