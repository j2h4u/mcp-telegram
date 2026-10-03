from pathlib import Path

import pytest

import mcp_telegram
from mcp_telegram.config import HttpServerConfig, McpTelegramConfig, StateConfig


def _patch_serve_tasks(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    async def fake_run_service(*, host: str, port: int) -> int:
        captured["host"] = host
        captured["port"] = port
        return 0

    monkeypatch.setattr("mcp_telegram.service_processes.run_service", fake_run_service)
    monkeypatch.setattr(
        mcp_telegram,
        "load_config",
        lambda: McpTelegramConfig(state=StateConfig(dir=Path("/tmp/mcp-telegram-test"))),
    )


def test_serve_reads_http_bind_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _patch_serve_tasks(monkeypatch, captured)
    monkeypatch.setenv("MCP_TELEGRAM_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_TELEGRAM_HTTP_PORT", "3101")

    with pytest.raises(SystemExit) as exc:
        mcp_telegram.serve()
    assert exc.value.code == 0

    assert captured == {"host": "0.0.0.0", "port": 3101}


def test_serve_options_override_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _patch_serve_tasks(monkeypatch, captured)
    monkeypatch.setenv("MCP_TELEGRAM_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_TELEGRAM_HTTP_PORT", "3101")

    with pytest.raises(SystemExit) as exc:
        mcp_telegram.serve(host="127.0.0.1", port=3200)
    assert exc.value.code == 0

    assert captured == {"host": "127.0.0.1", "port": 3200}


def test_serve_uses_operator_http_config_when_no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _patch_serve_tasks(monkeypatch, captured)
    monkeypatch.delenv("MCP_TELEGRAM_HTTP_HOST", raising=False)
    monkeypatch.delenv("MCP_TELEGRAM_HTTP_PORT", raising=False)
    monkeypatch.setattr(
        mcp_telegram,
        "load_config",
        lambda: McpTelegramConfig(
            state=StateConfig(dir=Path("/tmp/mcp-telegram-test")),
            http=HttpServerConfig(host="localhost", port=3201),
        ),
    )

    with pytest.raises(SystemExit) as exc:
        mcp_telegram.serve()
    assert exc.value.code == 0

    assert captured == {"host": "localhost", "port": 3201}


def test_http_runs_without_starting_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _patch_serve_tasks(monkeypatch, captured)

    async def fake_http(*, host: str, port: int) -> None:
        captured.update(host=host, port=port)

    monkeypatch.setattr("mcp_telegram.server.run_mcp_http_server", fake_http)
    monkeypatch.delenv("MCP_TELEGRAM_HTTP_HOST", raising=False)
    monkeypatch.delenv("MCP_TELEGRAM_HTTP_PORT", raising=False)
    mcp_telegram.http()
    assert captured == {"host": "127.0.0.1", "port": 3100}
