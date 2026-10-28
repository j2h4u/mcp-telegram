#!/usr/bin/env python3
"""Healthcheck: verify local serving and absence of a blocking account latch."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import TypedDict, cast

TIMEOUT_SECONDS = 5.0


class _HealthcheckResponse(TypedDict, total=False):
    ok: bool
    error: str
    detail: str
    data: dict[str, object]


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


async def _request(socket_path: Path, method: str) -> _HealthcheckResponse:
    from mcp_telegram.daemon_ipc import REQUEST_LIMIT, read_response

    reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=REQUEST_LIMIT)
    try:
        writer.write(json.dumps({"method": method, "params": {}}).encode() + b"\n")
        await writer.drain()
        data = await read_response(reader)
        if not data:
            raise RuntimeError("daemon closed connection before sending response")
        response = cast(object, json.loads(data))
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ValueError("malformed daemon health response")
        return cast(_HealthcheckResponse, response)
    finally:
        writer.close()
        await writer.wait_closed()


async def _probe(socket_path: Path) -> _HealthcheckResponse:
    response = await _request(socket_path, "get_sync_status")
    if not response.get("ok"):
        return response
    response = await _request(socket_path, "get_account_protection")
    if not response.get("ok"):
        return response
    data = response.get("data")
    if not isinstance(data, dict) or "account_protection" not in data:
        raise ValueError("missing account protection status")
    protection = data["account_protection"]
    if protection is not None:
        if not isinstance(protection, dict) or protection.get("status") != "active":
            raise ValueError("malformed account protection status")
        diagnostic_fields = (
            "reason",
            "unresolved_request_count",
            "unresolved_requests",
            "unresolved_requests_truncated",
        )
        detail = {key: protection[key] for key in diagnostic_fields if key in protection}
        return {"ok": False, "error": "account_protection_active", "detail": json.dumps(detail, ensure_ascii=True)}
    return response


def main() -> int:
    """Check local serving and whether account protection has stopped acquisition."""
    socket_path: Path | None = None
    try:
        socket_path = _load_socket_path()
        response = asyncio.run(asyncio.wait_for(_probe(socket_path), TIMEOUT_SECONDS))
        if not response.get("ok"):
            error = response.get("error", "unknown")
            detail = response.get("detail", "")
            msg = f"daemon not ready: {detail}" if error == "daemon_not_ready" else f"daemon error: {error}"
            if error == "account_protection_active" and detail:
                msg += f": {detail}"
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
    except (
        AttributeError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        asyncio.IncompleteReadError,
    ) as error:
        print(f"healthcheck failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
