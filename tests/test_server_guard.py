from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

import mcp_telegram.tools._base as _base_mod


# ---------------------------------------------------------------------------
# Guard flag and disable function
# ---------------------------------------------------------------------------


def test_disable_telegram_session_sets_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling disable_telegram_session() sets _session_disabled to True."""
    from mcp_telegram.tools._base import disable_telegram_session

    monkeypatch.setattr(_base_mod, "_session_disabled", False)
    disable_telegram_session()
    assert _base_mod._session_disabled is True
    # Cleanup — reset flag so other tests are unaffected
    monkeypatch.setattr(_base_mod, "_session_disabled", False)


def test_connected_client_raises_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """connected_client() raises RuntimeError when _session_disabled is True."""
    from mcp_telegram.tools._base import connected_client

    monkeypatch.setattr(_base_mod, "_session_disabled", True)
    try:
        import asyncio

        async def _try_enter() -> None:
            async with connected_client():
                pass

        with pytest.raises(RuntimeError, match="(?i)session disabled"):
            asyncio.run(_try_enter())
    finally:
        monkeypatch.setattr(_base_mod, "_session_disabled", False)


def test_connected_client_works_when_not_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With _session_disabled=False (default), connected_client() proceeds normally."""
    import asyncio
    from mcp_telegram.tools._base import connected_client

    monkeypatch.setattr(_base_mod, "_session_disabled", False)

    mock_client = AsyncMock()
    mock_client.is_connected.return_value = False
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()

    with patch("mcp_telegram.tools._base._telegram_mod") as mock_tg:
        mock_tg.create_client.return_value = mock_client

        async def _enter_and_exit() -> None:
            async with connected_client() as client:
                assert client is mock_client

        asyncio.run(_enter_and_exit())

    mock_client.connect.assert_called_once()
    mock_client.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# MCP server startup log
# ---------------------------------------------------------------------------


def test_run_mcp_server_logs_guard_message(caplog: pytest.LogCaptureFixture) -> None:
    """run_mcp_server() emits an INFO log with 'read-only mode' or 'no Telegram session' at startup."""
    import asyncio

    # Patch stdio_server to raise immediately after logging — avoids real I/O
    @asynccontextmanager
    async def fake_stdio_server():
        raise RuntimeError("stop after logging")
        yield  # noqa: unreachable

    with (
        patch("mcp_telegram.server.stdio_server", fake_stdio_server),
        caplog.at_level(logging.INFO, logger="mcp_telegram.server"),
    ):
        try:
            asyncio.run(_run_mcp_server_until_guard_log())
        except RuntimeError as exc:
            if "stop after logging" not in str(exc):
                raise

    guard_logs = [
        r.message
        for r in caplog.records
        if "read-only" in r.message.lower() or "no telegram session" in r.message.lower()
    ]
    assert guard_logs, (
        f"Expected guard INFO log at MCP server startup. Got logs: {[r.message for r in caplog.records]}"
    )


async def _run_mcp_server_until_guard_log() -> None:
    from mcp_telegram.server import run_mcp_server

    await run_mcp_server()
