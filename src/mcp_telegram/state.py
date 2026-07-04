"""State directory helpers shared by daemon, CLI, and auth code."""

from __future__ import annotations

from pathlib import Path

from .config import get_configured_state_dir

PRIVATE_STATE_DIR_MODE = 0o700


def ensure_private_state_dir(path: Path, *, mode: int = PRIVATE_STATE_DIR_MODE) -> Path:
    """Create or tighten a state directory before storing Telegram state."""
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    path.chmod(mode)
    if not path.is_dir():
        raise NotADirectoryError(path)
    return path


def get_state_dir(*, mode: int = PRIVATE_STATE_DIR_MODE) -> Path:
    """Return the canonical mcp-telegram state directory.

    ``~/.config/mcp-telegram/config.toml`` must set ``state.dir``. There is no
    implicit fallback: missing state location is a configuration error.
    """
    db_dir = get_configured_state_dir()
    return ensure_private_state_dir(db_dir, mode=mode)
