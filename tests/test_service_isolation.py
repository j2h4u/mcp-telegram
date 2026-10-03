from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import pytest

_REPO = Path(__file__).resolve().parents[1]
_DAEMON_CODE = """
import asyncio
from mcp_telegram.config import load_config
from test_daemon_api import make_server, _make_db_with_entities

async def main():
    api = make_server(conn=_make_db_with_entities())
    api.self_id = 7
    api.self_profile = {"id": 7, "first_name": "Test", "last_name": None, "username": None}
    socket_path = load_config().state.dir / "daemon.sock"
    async with await asyncio.start_unix_server(api.handle_client, path=str(socket_path)) as server:
        await server.serve_forever()

asyncio.run(main())
"""


@contextmanager
def _worker(command: list[str], env: dict[str, str], log_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, cwd=_REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            yield process
        except BaseException:
            print(log_path.read_text(encoding="utf-8"), file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _health(url: str) -> dict[str, Any]:
    with urlopen(f"{url.removesuffix('/mcp')}/health", timeout=2) as response:
        return json.load(response)


def _wait_for_http(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        assert process.poll() is None, "isolated HTTP process exited during startup"
        try:
            if _health(url).get("ok") is True:
                return
        except URLError, OSError:
            pass
        time.sleep(0.1)
    pytest.fail("isolated HTTP startup exceeded 120s")


def _cli(env: dict[str, str], url: str, command: str) -> Any:
    arguments = [sys.executable, "-m", "devtools.mcp_client.cli", command, "--url", url, "--timeout", "35"]
    if command == "call-tool":
        arguments.extend(["--name", "get_sync_status", "--arguments", '{"dialog_id": 987654321}'])
    result = subprocess.run(arguments, cwd=_REPO, env=env, capture_output=True, text=True, timeout=50, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _exercise_stalled_daemon(
    env: dict[str, str], url: str, http: subprocess.Popen[bytes], daemon: subprocess.Popen[bytes]
) -> None:
    initial = _cli(env, url, "call-tool")
    assert initial["isError"] is False
    assert initial["structuredContent"]["coverage_status"] == "not_synced"
    daemon.send_signal(signal.SIGSTOP)
    assert _health(url)["ok"] is True
    assert any(tool["name"] == "get_sync_status" for tool in _cli(env, url, "list-tools"))
    started = time.monotonic()
    stalled = _cli(env, url, "call-tool")
    elapsed = time.monotonic() - started
    assert stalled["isError"] is True
    text = stalled["content"][0]["text"]
    assert "Action:" in text and ("IPC timeout" in text or "TimeoutError" in text)
    assert stalled["structuredContent"]["error"]["code"] == "tool_error"
    assert stalled["structuredContent"]["error"]["action"]
    assert stalled["structuredContent"]["account_protection"]["status"] == "unavailable"
    assert http.poll() is None
    print(f"SIGSTOP: health/list-tools available, real IPC deadline returned MCP isError; CLI wall time {elapsed:.2f}s")
    daemon.send_signal(signal.SIGCONT)
    recovered = _cli(env, url, "call-tool")
    assert recovered["isError"] is False
    for field in ("dialog_id", "coverage_status", "account_protection"):
        assert recovered["structuredContent"].get(field) == initial["structuredContent"].get(field)
    assert http.poll() is None
    print("SIGCONT: get_sync_status recovered without HTTP restart")


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "linux", reason="requires Unix sockets and SIGSTOP/SIGCONT")
def test_http_survives_absent_and_stopped_daemon_without_restart() -> None:
    # A short path also keeps the Unix socket below its platform length limit.
    with TemporaryDirectory(prefix="mcp-isolation-") as temporary:
        root = Path(temporary)
        state = root / "state"
        state.mkdir()
        config = root / "config" / "mcp-telegram"
        config.mkdir(parents=True)
        (config / "config.toml").write_text(f'[state]\ndir = "{state}"\n', encoding="utf-8")
        env = dict(os.environ, XDG_CONFIG_HOME=str(root / "config"), PYTHONPATH=f"{_REPO}:{_REPO / 'tests'}")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}/mcp"
        console_script = Path(sys.executable).with_name("mcp-telegram")
        http_command = [sys.executable, str(console_script), "http", "--host", "127.0.0.1", "--port", str(port)]
        with _worker(http_command, env, root / "http.log") as http:
            _wait_for_http(url, http)
            assert any(tool["name"] == "get_sync_status" for tool in _cli(env, url, "list-tools"))
            assert _cli(env, url, "call-tool")["isError"] is True
            print("cold startup: health/list-tools available, missing daemon returns MCP isError")
            with _worker([sys.executable, "-c", _DAEMON_CODE], env, root / "daemon.log") as daemon:
                deadline = time.monotonic() + 120
                while not (state / "daemon.sock").exists():
                    assert daemon.poll() is None, "isolated daemon exited during startup"
                    assert time.monotonic() < deadline, "isolated daemon startup exceeded 120s"
                    time.sleep(0.1)
                _exercise_stalled_daemon(env, url, http, daemon)
