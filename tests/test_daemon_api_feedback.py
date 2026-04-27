"""RED tests for DaemonAPIServer._submit_feedback (Phase 48).

Tests reference:
 - mcp_telegram.feedback_db (new in 48-02) — ImportError until that plan lands
 - DaemonAPIServer(conn, client, event, feedback_conn) — 4-arg constructor
   added in 48-02 — TypeError until that plan lands

Both failures are valid RED signals confirming production wiring is absent.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.feedback_db import VALID_SEVERITIES, ensure_feedback_schema


# ---------------------------------------------------------------------------
# Local test helper — 4-arg constructor intentionally triggers TypeError
# until 48-02 adds the feedback_conn param to DaemonAPIServer.__init__
# ---------------------------------------------------------------------------


def _make_feedback_server(tmp_path):
    """Return (server, feedback_conn) wired to a real feedback.db + in-memory sync.db."""
    sync_conn = sqlite3.connect(":memory:")
    feedback_conn = ensure_feedback_schema(tmp_path / "feedback.db")
    client = MagicMock()
    shutdown_event = asyncio.Event()
    server = DaemonAPIServer(sync_conn, client, shutdown_event, feedback_conn)
    server._ready = True
    return server, feedback_conn


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_happy_path(tmp_path) -> None:
    """Valid message → ok=True, confirmation text, one DB row inserted."""
    server, feedback_conn = _make_feedback_server(tmp_path)
    before = int(time.time())

    response = await server._submit_feedback({"message": "the search returns stale data"})

    assert response["ok"] is True
    assert "message" in response.get("data", {})
    assert "Feedback recorded" in response["data"]["message"]

    row = feedback_conn.execute("SELECT message, submitted_at FROM feedback").fetchone()
    assert row is not None
    assert row[0] == "the search returns stale data"
    assert row[1] >= before


@pytest.mark.asyncio
async def test_submit_feedback_all_optional_fields(tmp_path) -> None:
    """All five fields stored — severity, context, model, harness, message."""
    server, feedback_conn = _make_feedback_server(tmp_path)

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
    row = feedback_conn.execute(
        "SELECT message, severity, context, model, harness FROM feedback"
    ).fetchone()
    assert row is not None
    assert row[1] == "bug"
    assert row[2] == "ListMessages limit=50 returned no rows"
    assert row[3] == "claude-opus-4-7"
    assert row[4] == "Claude Desktop"


# ---------------------------------------------------------------------------
# Input validation — message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_empty_message(tmp_path) -> None:
    """Empty message string → ok=False, invalid_input, zero rows."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": ""})

    assert response["ok"] is False
    assert response.get("error") == "invalid_input"
    assert "message" in response.get("message", "").lower() or "required" in response.get("message", "").lower()
    count = feedback_conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_submit_feedback_whitespace_only_message(tmp_path) -> None:
    """Whitespace-only message → ok=False, invalid_input, zero rows."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": "   \n\t"})

    assert response["ok"] is False
    assert response.get("error") == "invalid_input"
    count = feedback_conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_submit_feedback_oversize_message(tmp_path) -> None:
    """Message > 10000 chars → ok=False, invalid_input mentioning 'too long', zero rows."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": "x" * 10_001})

    assert response["ok"] is False
    assert response.get("error") == "invalid_input"
    assert "too long" in response.get("message", "").lower()
    count = feedback_conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Input validation — severity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_bad_severity(tmp_path) -> None:
    """Unknown severity → ok=False, invalid_input mentioning 'severity', zero rows."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": "real bug", "severity": "critical"})

    assert response["ok"] is False
    assert response.get("error") == "invalid_input"
    assert "severity" in response.get("message", "").lower()
    count = feedback_conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_submit_feedback_severity_none_allowed(tmp_path) -> None:
    """Request without severity field → ok=True, row stored with severity NULL."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": "no severity provided"})

    assert response["ok"] is True
    row = feedback_conn.execute("SELECT severity FROM feedback").fetchone()
    assert row is not None
    assert row[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", sorted(VALID_SEVERITIES))
async def test_submit_feedback_severity_uses_valid_set(tmp_path, severity: str) -> None:
    """All three values in VALID_SEVERITIES are accepted by the handler."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": "test message", "severity": severity})

    assert response["ok"] is True
    row = feedback_conn.execute("SELECT severity FROM feedback").fetchone()
    assert row[0] == severity


# ---------------------------------------------------------------------------
# Behaviour — message strip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_strips_message(tmp_path) -> None:
    """Leading/trailing whitespace stripped from message before storage."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": "  real bug  "})

    assert response["ok"] is True
    row = feedback_conn.execute("SELECT message FROM feedback").fetchone()
    assert row[0] == "real bug"


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_dispatch_route(tmp_path) -> None:
    """method='submit_feedback' is routed to _submit_feedback (not unknown_method)."""
    server, _ = _make_feedback_server(tmp_path)

    response = await server._dispatch({"method": "submit_feedback", "message": "route test"})

    # Unknown method returns an error dict with error="unknown_method".
    # If routing works, ok=True or ok=False with a non-unknown_method error.
    assert response.get("error") != "unknown_method"


# ---------------------------------------------------------------------------
# Input validation — optional field length caps (defense-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_context_too_long(tmp_path) -> None:
    """context > 2000 chars → ok=False, invalid_input mentioning 'context', zero rows."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": "ok", "context": "x" * 2001})

    assert response["ok"] is False
    assert response.get("error") == "invalid_input"
    assert "context" in response.get("message", "").lower()
    count = feedback_conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_submit_feedback_model_too_long(tmp_path) -> None:
    """model > 200 chars → ok=False, invalid_input mentioning 'model', zero rows."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": "ok", "model": "x" * 201})

    assert response["ok"] is False
    assert response.get("error") == "invalid_input"
    assert "model" in response.get("message", "").lower()
    count = feedback_conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_submit_feedback_harness_too_long(tmp_path) -> None:
    """harness > 200 chars → ok=False, invalid_input mentioning 'harness', zero rows."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    response = await server._submit_feedback({"message": "ok", "harness": "x" * 201})

    assert response["ok"] is False
    assert response.get("error") == "invalid_input"
    assert "harness" in response.get("message", "").lower()
    count = feedback_conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# DB error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_db_error_returns_internal(tmp_path) -> None:
    """sqlite3.OperationalError from execute → ok=False, error='internal', no traceback leak."""
    server, feedback_conn = _make_feedback_server(tmp_path)

    original_execute = feedback_conn.execute

    def _raise(*args, **kwargs):
        if args and "INSERT" in str(args[0]):
            raise sqlite3.OperationalError("disk full")
        return original_execute(*args, **kwargs)

    feedback_conn.execute = _raise  # type: ignore[method-assign]

    response = await server._submit_feedback({"message": "trigger db error"})

    assert response["ok"] is False
    assert response.get("error") == "internal"
    assert response.get("message") == "internal error"
    # No traceback details in user-facing message
    assert "Traceback" not in response.get("message", "")
    assert "OperationalError" not in response.get("message", "")
