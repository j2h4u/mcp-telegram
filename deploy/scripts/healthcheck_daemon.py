#!/usr/bin/env python3
"""Healthcheck: verify sync daemon is alive and responding on its Unix socket."""

from __future__ import annotations

import json
import os
import socket
import sys
import tomllib
from pathlib import Path
from typing import TypedDict, cast

TIMEOUT_SECONDS = 5.0


class _HealthcheckResponse(TypedDict, total=False):
    ok: bool
    error: str
    detail: str


def _config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config_home / "mcp-telegram" / "config.toml"


def _load_socket_path() -> Path:
    config_path = _config_path()
    if not config_path.exists():
        raise RuntimeError(f"missing config: {config_path}")
    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)
    state_config = config.get("state")
    if not isinstance(state_config, dict):
        raise RuntimeError(f"missing [state] in {config_path}")
    state_dir = state_config.get("dir")
    if not isinstance(state_dir, str) or state_dir.strip() == "":
        raise RuntimeError(f"missing state.dir in {config_path}")
    return Path(state_dir).expanduser() / "daemon.sock"


def main() -> int:
    """Send get_sync_status via newline-delimited JSON (matching daemon_client protocol)."""
    sock: socket.socket | None = None
    socket_path: Path | None = None
    try:
        socket_path = _load_socket_path()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect(str(socket_path))

        request = json.dumps({"method": "get_sync_status", "params": {}}).encode("utf-8") + b"\n"
        sock.sendall(request)

        # Read response until newline (daemon sends JSON + \n)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("daemon closed connection before sending response")
            buf += chunk

        response = cast(_HealthcheckResponse, json.loads(buf.strip()))
        if not response.get("ok"):
            error = response.get("error", "unknown")
            detail = response.get("detail", "")
            msg = f"daemon not ready: {detail}" if error == "daemon_not_ready" else f"daemon error: {error}"
            print(msg, file=sys.stderr)
            return 1

        return 0

    except FileNotFoundError:
        print(f"daemon socket not found: {socket_path}", file=sys.stderr)
        return 1
    except ConnectionRefusedError:
        print("daemon socket exists but connection refused", file=sys.stderr)
        return 1
    except TimeoutError:
        print("daemon did not respond within timeout", file=sys.stderr)
        return 1
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, json.JSONDecodeError) as error:
        print(f"healthcheck failed: {error}", file=sys.stderr)
        return 1
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
