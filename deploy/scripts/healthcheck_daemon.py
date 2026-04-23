#!/usr/bin/env python3
"""Healthcheck: verify sync daemon is alive and responding on its Unix socket."""
from __future__ import annotations

import json
import socket
import sys

SOCKET_PATH = "/root/.local/state/mcp-telegram/daemon.sock"
TIMEOUT_SECONDS = 5.0


def main() -> int:
    """Send get_sync_status via newline-delimited JSON (matching daemon_client protocol)."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect(SOCKET_PATH)

        request = json.dumps({"method": "get_sync_status", "params": {}}).encode("utf-8") + b"\n"
        sock.sendall(request)

        # Read response until newline (daemon sends JSON + \n)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("daemon closed connection before sending response")
            buf += chunk

        response = json.loads(buf.strip())
        if not response.get("ok"):
            print(f"daemon returned error: {response.get('error', 'unknown')}", file=sys.stderr)
            return 1

        return 0

    except FileNotFoundError:
        print(f"daemon socket not found: {SOCKET_PATH}", file=sys.stderr)
        return 1
    except ConnectionRefusedError:
        print("daemon socket exists but connection refused", file=sys.stderr)
        return 1
    except TimeoutError:
        print("daemon did not respond within timeout", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"healthcheck failed: {error}", file=sys.stderr)
        return 1
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
