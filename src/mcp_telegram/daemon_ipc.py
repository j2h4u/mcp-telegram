"""Daemon IPC path helpers.

Shared helpers for the daemon Unix socket location used by both runtime and
client transports.
"""

from __future__ import annotations

from pathlib import Path


def get_daemon_socket_path(state_dir: Path) -> Path:
    """Return the daemon Unix socket path below an explicit state directory."""
    return state_dir / "daemon.sock"
