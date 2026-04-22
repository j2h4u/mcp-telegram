from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# MCP server startup log — daemon API routing
# ---------------------------------------------------------------------------


def test_run_mcp_server_logs_daemon_api_message(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run_mcp_server() emits an INFO log with 'daemon API' at startup."""
    import asyncio

    # Patch stdio_server to raise immediately after logging — avoids real I/O
    @asynccontextmanager
    async def fake_stdio_server():
        raise RuntimeError("stop after logging")
        yield  # noqa: unreachable

    with (
        patch("mcp.server.stdio.stdio_server", fake_stdio_server),
        # Suppress basicConfig(force=True) which would replace caplog's handler
        patch("logging.basicConfig"),
        caplog.at_level(logging.INFO, logger="mcp_telegram.server"),
    ):
        try:
            asyncio.run(_run_mcp_server_until_guard_log())
        except RuntimeError as exc:
            if "stop after logging" not in str(exc):
                raise

    daemon_logs = [r.message for r in caplog.records if "daemon api" in r.message.lower()]
    assert daemon_logs, (
        f"Expected daemon API INFO log at MCP server startup. Got logs: {[r.message for r in caplog.records]}"
    )


async def _run_mcp_server_until_guard_log() -> None:
    from mcp_telegram.server import run_mcp_server

    await run_mcp_server()


# ---------------------------------------------------------------------------
# _base.py imports — verify daemon client is available, legacy code removed
# ---------------------------------------------------------------------------


def test_base_exports_daemon_connection() -> None:
    """daemon_connection and DaemonNotRunningError must be importable from _base."""
    from mcp_telegram.tools._base import DaemonNotRunningError, daemon_connection

    assert daemon_connection is not None
    assert DaemonNotRunningError is not None


def test_base_has_no_connected_client() -> None:
    """connected_client must not exist in _base after migration."""
    import mcp_telegram.tools._base as _base_mod

    assert not hasattr(_base_mod, "connected_client"), "connected_client was removed — it should not exist in _base"


def test_base_has_no_disable_telegram_session() -> None:
    """disable_telegram_session must not exist in _base after migration."""
    import mcp_telegram.tools._base as _base_mod

    assert not hasattr(_base_mod, "disable_telegram_session"), (
        "disable_telegram_session was removed — it should not exist in _base"
    )


def test_base_has_no_session_disabled_flag() -> None:
    """_session_disabled module-level flag must not exist in _base after migration."""
    import mcp_telegram.tools._base as _base_mod

    assert not hasattr(_base_mod, "_session_disabled"), "_session_disabled was removed — it should not exist in _base"
