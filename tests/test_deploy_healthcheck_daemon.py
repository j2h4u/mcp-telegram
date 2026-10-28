from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from deploy.scripts import healthcheck_daemon


def test_daemon_healthcheck_loads_socket_path_from_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "xdg-config" / "mcp-telegram"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(f'[state]\ndir = "{state_dir}"\n', encoding="utf-8")

    assert healthcheck_daemon._load_socket_path() == state_dir / "daemon.sock"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protection", "expected"),
    [(None, True), ({"status": "active", "outbound_acquisition": "blocked", "recovery": "manual"}, False)],
)
async def test_daemon_health_requires_acquisition_not_protected(
    tmp_path: Path, protection: dict[str, str] | None, expected: bool
) -> None:
    methods: list[str] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = cast(dict[str, object], json.loads(await reader.readline()))
        method = request["method"]
        assert isinstance(method, str)
        methods.append(method)
        data = {} if request["method"] == "get_sync_status" else {"account_protection": protection}
        writer.write(json.dumps({"ok": True, "data": data}).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    socket = tmp_path / "daemon.sock"
    async with await asyncio.start_unix_server(respond, path=socket):
        result = await healthcheck_daemon._probe(socket)
    assert result.get("ok") is expected
    assert methods == ["get_sync_status", "get_account_protection"]
    if not expected:
        assert result.get("error") == "account_protection_active"


def test_daemon_health_prints_specific_protection_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def request(_socket: Path, method: str) -> healthcheck_daemon._HealthcheckResponse:
        if method == "get_sync_status":
            return {"ok": True}
        return {
            "ok": True,
            "data": {
                "account_protection": {
                    "status": "active",
                    "reason": "unresolved_outbound_outcome",
                    "unresolved_request_count": 1,
                    "unresolved_requests": [{"method": "GetHistoryRequest", "phase": "unknown"}],
                    "generation": "private-generation",
                    "key_id": "private-key",
                }
            },
        }

    monkeypatch.setattr(healthcheck_daemon, "_load_socket_path", lambda: Path("/unused.sock"))
    monkeypatch.setattr(healthcheck_daemon, "_request", request)
    assert healthcheck_daemon.main() == 1
    diagnostic = capsys.readouterr().err
    assert "unresolved_outbound_outcome" in diagnostic and "GetHistoryRequest" in diagnostic
    assert '"unresolved_request_count": 1' in diagnostic
    assert "private-" not in diagnostic


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [b"{}\n", b"[]\n", b'{"ok":true}\n', b'{"ok":true,"data":{}}\n', b"bad json\n"])
async def test_daemon_health_rejects_malformed_protection(tmp_path: Path, response: bytes) -> None:
    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = cast(dict[str, object], json.loads(await reader.readline()))
        writer.write(b'{"ok":true}\n' if request["method"] == "get_sync_status" else response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    socket = tmp_path / "daemon.sock"
    async with await asyncio.start_unix_server(respond, path=socket):
        with pytest.raises(ValueError):
            await healthcheck_daemon._probe(socket)


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), ConnectionRefusedError(), TimeoutError(), ValueError("invalid")]
)
def test_daemon_health_main_fails_on_transport_or_malformed_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    async def fail(_socket: Path) -> healthcheck_daemon._HealthcheckResponse:
        raise error

    monkeypatch.setattr(healthcheck_daemon, "_load_socket_path", lambda: Path("/unused.sock"))
    monkeypatch.setattr(healthcheck_daemon, "_probe", fail)
    assert healthcheck_daemon.main() == 1
    assert capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_method", ["get_sync_status", "get_account_protection"])
async def test_daemon_health_keeps_daemon_error_failure(tmp_path: Path, failed_method: str) -> None:
    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = cast(dict[str, object], json.loads(await reader.readline()))
        response = {"ok": False, "error": "daemon_not_ready"} if request["method"] == failed_method else {"ok": True}
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    socket = tmp_path / "daemon.sock"
    async with await asyncio.start_unix_server(respond, path=socket):
        assert await healthcheck_daemon._probe(socket) == {"ok": False, "error": "daemon_not_ready"}
