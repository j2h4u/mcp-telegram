"""Daemon IPC path helpers.

Shared helpers for the daemon Unix socket location used by both runtime and
client transports.
"""

from __future__ import annotations

from pathlib import Path

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]


def get_daemon_socket_path() -> Path:
    """Return the canonical daemon Unix socket path under XDG state home."""
    return xdg_state_home() / "mcp-telegram" / "daemon.sock"
