"""RED tests for tools/feedback.py — SubmitFeedback MCP tool (Phase 48).

These tests import from mcp_telegram.tools.feedback which does NOT yet exist.
Expected outcome: ModuleNotFoundError at collection time — confirming RED state
before 48-03 lands.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from mcp_telegram.tools import TOOL_REGISTRY
from mcp_telegram.tools.feedback import SubmitFeedback, submit_feedback
from mcp_telegram.daemon_client import DaemonNotRunningError


# ---------------------------------------------------------------------------
# Test fixture: mock daemon_connection
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_daemon_connection():
    """Patch daemon_connection to return a scripted mock DaemonConnection."""
    mock_conn = AsyncMock()
    mock_conn.submit_feedback = AsyncMock(
        return_value={"ok": True, "data": {"message": "Feedback recorded. Thank you!"}}
    )

    @asynccontextmanager
    async def fake_daemon_connection():
        yield mock_conn

    with patch("mcp_telegram.tools.feedback.daemon_connection", fake_daemon_connection):
        yield mock_conn


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_tool_happy_path(mock_daemon_connection) -> None:
    """Successful submission → content[0].text equals daemon confirmation message."""
    args = SubmitFeedback(message="the bug")
    result = await submit_feedback(args)

    assert len(result.content) == 1
    assert result.content[0].text == "Feedback recorded. Thank you!"


@pytest.mark.asyncio
async def test_submit_feedback_tool_passes_all_fields(mock_daemon_connection) -> None:
    """All 5 kwargs forwarded to DaemonConnection.submit_feedback exactly."""
    args = SubmitFeedback(
        message="test message",
        severity="bug",
        context="ListMessages limit=50",
        model="claude-opus-4-7",
        harness="Claude Desktop",
    )
    await submit_feedback(args)

    mock_daemon_connection.submit_feedback.assert_called_once_with(
        message="test message",
        severity="bug",
        context="ListMessages limit=50",
        model="claude-opus-4-7",
        harness="Claude Desktop",
    )


@pytest.mark.asyncio
async def test_submit_feedback_tool_omits_unset_optional_fields(mock_daemon_connection) -> None:
    """With only message set, submit_feedback called with all 5 kwargs (None for optionals)."""
    args = SubmitFeedback(message="only message")
    await submit_feedback(args)

    mock_daemon_connection.submit_feedback.assert_called_once_with(
        message="only message",
        severity=None,
        context=None,
        model=None,
        harness=None,
    )


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_tool_daemon_not_running() -> None:
    """DaemonNotRunningError → content[0].text contains 'mcp-telegram sync'."""
    from mcp_telegram.tools._base import _daemon_not_running_text

    @asynccontextmanager
    async def raising_dc():
        raise DaemonNotRunningError("Sync daemon is not running.")
        yield  # pragma: no cover

    with patch("mcp_telegram.tools.feedback.daemon_connection", raising_dc):
        args = SubmitFeedback(message="test")
        result = await submit_feedback(args)

    assert result.is_error is True
    assert "mcp-telegram sync" in result.content[0].text
    assert result.content[0].text == _daemon_not_running_text()


@pytest.mark.asyncio
async def test_submit_feedback_tool_daemon_error_response() -> None:
    """Daemon ok=False → content[0].text starts with 'Error:' and contains detail."""
    mock_conn = AsyncMock()
    mock_conn.submit_feedback = AsyncMock(
        return_value={"ok": False, "message": "internal error"}
    )

    @asynccontextmanager
    async def fake_dc():
        yield mock_conn

    with patch("mcp_telegram.tools.feedback.daemon_connection", fake_dc):
        args = SubmitFeedback(message="test")
        result = await submit_feedback(args)

    assert result.is_error is True
    assert result.content[0].text.startswith("Error:")
    assert "internal error" in result.content[0].text


# ---------------------------------------------------------------------------
# Pydantic schema validation
# ---------------------------------------------------------------------------


def test_submit_feedback_tool_severity_literal_validation() -> None:
    """SubmitFeedback with invalid severity raises pydantic.ValidationError."""
    with pytest.raises(ValidationError):
        SubmitFeedback(message="x", severity="invalid")


def test_submit_feedback_tool_message_max_length() -> None:
    """SubmitFeedback with message > 10000 chars raises pydantic.ValidationError."""
    with pytest.raises(ValidationError):
        SubmitFeedback(message="x" * 10_001)


# ---------------------------------------------------------------------------
# TOOL_REGISTRY registration
# ---------------------------------------------------------------------------


def test_submit_feedback_tool_registered_in_registry() -> None:
    """TOOL_REGISTRY['SubmitFeedback'] exists with posture='primary' and readOnlyHint=False."""
    assert "SubmitFeedback" in TOOL_REGISTRY
    tool_cls, posture, annotations = TOOL_REGISTRY["SubmitFeedback"]
    assert posture == "primary"
    assert annotations is not None
    assert annotations.readOnlyHint is False
