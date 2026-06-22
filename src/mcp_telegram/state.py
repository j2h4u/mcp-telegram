"""State directory helpers shared by daemon, CLI, and auth code."""

from __future__ import annotations

from pathlib import Path

from .config import get_configured_state_dir


def get_state_dir(*, mode: int = 0o700) -> Path:
    """Return the canonical mcp-telegram state directory.

    ``~/.config/mcp-telegram/config.toml`` must set ``state.dir``. There is no
    implicit fallback: missing state location is a configuration error.
    """
    db_dir = get_configured_state_dir()
    db_dir.mkdir(parents=True, exist_ok=True, mode=mode)
    return db_dir
