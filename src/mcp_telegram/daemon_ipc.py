"""Daemon IPC path helpers.

Shared helpers for the daemon Unix socket location used by both runtime and
client transports.
"""

from __future__ import annotations

from pathlib import Path

from .state import get_state_dir


def get_daemon_socket_path() -> Path:
    """Return the canonical daemon Unix socket path under configured state."""
    return get_state_dir() / "daemon.sock"
