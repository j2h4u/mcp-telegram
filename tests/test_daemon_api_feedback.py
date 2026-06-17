"""Tests for DaemonAPIServer feedback queue methods."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.feedback_db import VALID_SEVERITIES, ensure_feedback_schema


# ---------------------------------------------------------------------------
@contextmanager
def _make_feedback_server(tmp_path: Path) -> Iterator[tuple[DaemonAPIServer, sqlite3.Connection]]:
    """Return (server, feedback_conn) wired to a real feedback.db + in-memory sync.db."""
    sync_conn = sqlite3.connect(":memory:")
    feedback_conn = ensure_feedback_schema(tmp_path / "feedback.db")
    client = MagicMock()
    shutdown_event = asyncio.Event()
    server = DaemonAPIServer(sync_conn, client, shutdown_event, feedback_conn)
    server._ready = True
    try:
        yield server, feedback_conn
    finally:
        feedback_conn.close()
        sync_conn.close()


def _fetchone_row(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    row = cast(tuple[object, ...] | None, conn.execute(sql, params).fetchone())
    assert row is not None
    return row


def _count_rows(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = _fetchone_row(conn, sql, params)
    return cast(int, row[0])


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_happy_path(tmp_path: Path) -> None:
    """Valid message → ok=True, confirmation text, one DB row inserted."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        before = int(time.time())

        response = await server._submit_feedback({"message": "the search returns stale data"})

        assert response["ok"] is True
        assert "message" in response.get("data", {})
        assert "Feedback recorded" in response["data"]["message"]

        row = _fetchone_row(feedback_conn, "SELECT message, submitted_at FROM feedback")
        assert row[0] == "the search returns stale data"
        assert cast(int, row[1]) >= before


@pytest.mark.asyncio
async def test_submit_feedback_all_optional_fields(tmp_path: Path) -> None:
    """All five fields stored — severity, context, model, harness, message."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback(
            {
                "message": "ListMessages limit=50 returned no rows",
                "severity": "bug",
                "context": "ListMessages limit=50 returned no rows",
                "model": "claude-opus-4-7",
                "harness": "Claude Desktop",
            }
        )

        assert response["ok"] is True
        row = _fetchone_row(feedback_conn, "SELECT message, severity, context, model, harness FROM feedback")
        assert row[1] == "bug"
        assert row[2] == "ListMessages limit=50 returned no rows"
        assert row[3] == "claude-opus-4-7"
        assert row[4] == "Claude Desktop"


# ---------------------------------------------------------------------------
# Input validation — message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_empty_message(tmp_path: Path) -> None:
    """Empty message string → ok=False, invalid_input, zero rows."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": ""})

        assert response["ok"] is False
        assert response.get("error") == "invalid_input"
        assert "message" in response.get("message", "").lower() or "required" in response.get("message", "").lower()
        assert _count_rows(feedback_conn, "SELECT COUNT(*) FROM feedback") == 0


@pytest.mark.asyncio
async def test_submit_feedback_whitespace_only_message(tmp_path: Path) -> None:
    """Whitespace-only message → ok=False, invalid_input, zero rows."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": "   \n\t"})

        assert response["ok"] is False
        assert response.get("error") == "invalid_input"
        assert _count_rows(feedback_conn, "SELECT COUNT(*) FROM feedback") == 0


@pytest.mark.asyncio
async def test_submit_feedback_oversize_message(tmp_path: Path) -> None:
    """Message > 10000 chars → ok=False, invalid_input mentioning 'too long', zero rows."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": "x" * 10_001})

        assert response["ok"] is False
        assert response.get("error") == "invalid_input"
        assert "too long" in response.get("message", "").lower()
        assert _count_rows(feedback_conn, "SELECT COUNT(*) FROM feedback") == 0


# ---------------------------------------------------------------------------
# Input validation — severity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_bad_severity(tmp_path: Path) -> None:
    """Unknown severity → ok=False, invalid_input mentioning 'severity', zero rows."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": "real bug", "severity": "critical"})

        assert response["ok"] is False
        assert response.get("error") == "invalid_input"
        assert "severity" in response.get("message", "").lower()
        assert _count_rows(feedback_conn, "SELECT COUNT(*) FROM feedback") == 0


@pytest.mark.asyncio
async def test_submit_feedback_severity_none_allowed(tmp_path: Path) -> None:
    """Request without severity field → ok=True, row stored with severity NULL."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": "no severity provided"})

        assert response["ok"] is True
        row = _fetchone_row(feedback_conn, "SELECT severity FROM feedback")
        assert row[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", sorted(VALID_SEVERITIES))
async def test_submit_feedback_severity_uses_valid_set(tmp_path: Path, severity: str) -> None:
    """All three values in VALID_SEVERITIES are accepted by the handler."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": "test message", "severity": severity})

        assert response["ok"] is True
        row = _fetchone_row(feedback_conn, "SELECT severity FROM feedback")
        assert row[0] == severity


# ---------------------------------------------------------------------------
# Behaviour — message strip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_strips_message(tmp_path: Path) -> None:
    """Leading/trailing whitespace stripped from message before storage."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": "  real bug  "})

        assert response["ok"] is True
        row = _fetchone_row(feedback_conn, "SELECT message FROM feedback")
        assert row[0] == "real bug"


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_dispatch_route(tmp_path: Path) -> None:
    """method='submit_feedback' is routed to _submit_feedback (not unknown_method)."""
    with _make_feedback_server(tmp_path) as (server, _):
        response = await server._dispatch({"method": "submit_feedback", "message": "route test"})

        # Unknown method returns an error dict with error="unknown_method".
        # If routing works, ok=True or ok=False with a non-unknown_method error.
        assert response.get("error") != "unknown_method"


# ---------------------------------------------------------------------------
# Input validation — optional field length caps (defense-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_context_too_long(tmp_path: Path) -> None:
    """context > 2000 chars → ok=False, invalid_input mentioning 'context', zero rows."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": "ok", "context": "x" * 2001})

        assert response["ok"] is False
        assert response.get("error") == "invalid_input"
        assert "context" in response.get("message", "").lower()
        assert _count_rows(feedback_conn, "SELECT COUNT(*) FROM feedback") == 0


@pytest.mark.asyncio
async def test_submit_feedback_model_too_long(tmp_path: Path) -> None:
    """model > 200 chars → ok=False, invalid_input mentioning 'model', zero rows."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": "ok", "model": "x" * 201})

        assert response["ok"] is False
        assert response.get("error") == "invalid_input"
        assert "model" in response.get("message", "").lower()
        assert _count_rows(feedback_conn, "SELECT COUNT(*) FROM feedback") == 0


@pytest.mark.asyncio
async def test_submit_feedback_harness_too_long(tmp_path: Path) -> None:
    """harness > 200 chars → ok=False, invalid_input mentioning 'harness', zero rows."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._submit_feedback({"message": "ok", "harness": "x" * 201})

        assert response["ok"] is False
        assert response.get("error") == "invalid_input"
        assert "harness" in response.get("message", "").lower()
        assert _count_rows(feedback_conn, "SELECT COUNT(*) FROM feedback") == 0


# ---------------------------------------------------------------------------
# DB error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_db_error_returns_internal(tmp_path: Path) -> None:
    """sqlite3.OperationalError from execute → ok=False, error='internal', no traceback leak."""
    # Python 3.14 made sqlite3.Connection.execute a read-only C attribute so
    # direct assignment and patch.object both fail.  Inject a MagicMock that
    # raises on INSERT — this is the correct approach for C-extension objects.
    sync_conn = sqlite3.connect(":memory:")
    mock_feedback_conn = MagicMock(spec=sqlite3.Connection)
    try:

        def _raise(*args: object, **kwargs: object) -> MagicMock:
            if args and "INSERT" in str(args[0]):
                raise sqlite3.OperationalError("disk full")
            # Pass through non-INSERT calls (none expected in this test path)
            return MagicMock()

        cast(MagicMock, mock_feedback_conn.execute).side_effect = _raise

        client = MagicMock()
        shutdown_event = asyncio.Event()
        server = DaemonAPIServer(sync_conn, client, shutdown_event, mock_feedback_conn)
        server._ready = True

        response = await server._submit_feedback({"message": "trigger db error"})

        assert response["ok"] is False
        assert response.get("error") == "internal"
        assert response.get("message") == "internal error"
        # No traceback details in user-facing message
        assert "Traceback" not in response.get("message", "")
        assert "OperationalError" not in response.get("message", "")
    finally:
        sync_conn.close()


# ---------------------------------------------------------------------------
# _update_feedback_status — happy path, validation, comment lifecycle, dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_feedback_status_happy_path(tmp_path: Path) -> None:
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        feedback_conn.execute(
            "INSERT INTO feedback (submitted_at, message, status) VALUES (?, ?, 'open')",
            (int(time.time()), "needs review"),
        )
        feedback_conn.commit()
        rid = _count_rows(feedback_conn, "SELECT id FROM feedback")

        response = await server._update_feedback_status({"id": rid, "status": "done"})

        assert response["ok"] is True
        assert "set to 'done'" in response["data"]["message"]
        row = _fetchone_row(feedback_conn, "SELECT status, status_changed_at FROM feedback WHERE id=?", (rid,))
        assert row[0] == "done"
        assert isinstance(row[1], int) and row[1] > 0


@pytest.mark.asyncio
async def test_update_feedback_status_not_found(tmp_path: Path) -> None:
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        response = await server._update_feedback_status({"id": 9999, "status": "done"})
        assert response["ok"] is False
        assert response.get("error") == "not_found"
        assert "9999" in response.get("message", "")
        # No row was inserted — handler returned BEFORE commit on no-op.
        assert _count_rows(feedback_conn, "SELECT COUNT(*) FROM feedback") == 0


@pytest.mark.asyncio
async def test_update_feedback_status_invalid_status(tmp_path: Path) -> None:
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        feedback_conn.execute(
            "INSERT INTO feedback (submitted_at, message, status) VALUES (?, ?, 'open')",
            (int(time.time()), "row"),
        )
        feedback_conn.commit()
        rid = _count_rows(feedback_conn, "SELECT id FROM feedback")

        response = await server._update_feedback_status({"id": rid, "status": "wontfix"})
        assert response["ok"] is False
        assert response.get("error") == "invalid_input"
        assert "status" in response.get("message", "").lower()
        # Row was NOT updated
        row = _fetchone_row(feedback_conn, "SELECT status FROM feedback WHERE id=?", (rid,))
        assert row[0] == "open"


@pytest.mark.asyncio
async def test_update_feedback_status_invalid_id(tmp_path: Path) -> None:
    with _make_feedback_server(tmp_path) as (server, _):
        for bad_id in (0, -1, "abc", None):
            response = await server._update_feedback_status({"id": bad_id, "status": "done"})
            assert response["ok"] is False, f"bad_id={bad_id!r} should fail"
            assert response.get("error") == "invalid_input"
            assert "id" in response.get("message", "").lower()


@pytest.mark.asyncio
async def test_update_feedback_status_invalid_reason_type(tmp_path: Path) -> None:
    """Non-string, non-None reason is rejected as invalid_input (not internal)."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        feedback_conn.execute(
            "INSERT INTO feedback (submitted_at, message, status) VALUES (?, ?, 'open')",
            (int(time.time()), "row"),
        )
        feedback_conn.commit()
        rid = _count_rows(feedback_conn, "SELECT id FROM feedback")

        for bad_reason in ([1, 2, 3], {"x": 1}, 42):
            response = await server._update_feedback_status({"id": rid, "status": "done", "reason": bad_reason})
            assert response["ok"] is False, f"bad_reason={bad_reason!r} should fail"
            assert response.get("error") == "invalid_input"
            assert "reason" in response.get("message", "").lower()
        # Row status was not changed by any of the rejected calls
        row = _fetchone_row(feedback_conn, "SELECT status FROM feedback WHERE id=?", (rid,))
        assert row[0] == "open"


@pytest.mark.asyncio
async def test_update_feedback_status_with_reason(tmp_path: Path) -> None:
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        feedback_conn.execute(
            "INSERT INTO feedback (submitted_at, message, status) VALUES (?, ?, 'open')",
            (int(time.time()), "noisy bug"),
        )
        feedback_conn.commit()
        rid = _count_rows(feedback_conn, "SELECT id FROM feedback")

        response = await server._update_feedback_status({"id": rid, "status": "dismissed", "reason": "noise"})
        assert response["ok"] is True
        row = _fetchone_row(feedback_conn, "SELECT status, status_comment FROM feedback WHERE id=?", (rid,))
        assert row[0] == "dismissed"
        assert row[1] == "noise"


@pytest.mark.asyncio
async def test_update_feedback_status_omitting_reason_clears_it(tmp_path: Path) -> None:
    """Each status transition writes status_comment fresh; omitted → NULL."""
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        feedback_conn.execute(
            "INSERT INTO feedback (submitted_at, message, status) VALUES (?, ?, 'open')",
            (int(time.time()), "row"),
        )
        feedback_conn.commit()
        rid = _count_rows(feedback_conn, "SELECT id FROM feedback")

        # First transition with reason
        await server._update_feedback_status({"id": rid, "status": "in_progress", "reason": "starting"})
        # Second transition without reason
        await server._update_feedback_status({"id": rid, "status": "done"})

        row = _fetchone_row(feedback_conn, "SELECT status, status_comment FROM feedback WHERE id=?", (rid,))
        assert row[0] == "done"
        assert row[1] is None  # reason omitted → NULL


@pytest.mark.asyncio
async def test_update_feedback_status_dispatch_route(tmp_path: Path) -> None:
    with _make_feedback_server(tmp_path) as (server, feedback_conn):
        feedback_conn.execute(
            "INSERT INTO feedback (submitted_at, message, status) VALUES (?, ?, 'open')",
            (int(time.time()), "route test"),
        )
        feedback_conn.commit()
        rid = _count_rows(feedback_conn, "SELECT id FROM feedback")

        response = await server._dispatch({"method": "update_feedback_status", "id": rid, "status": "done"})
        assert response.get("error") != "unknown_method"
        assert response["ok"] is True
