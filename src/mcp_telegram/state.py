"""State directory helpers shared by daemon, CLI, and auth code."""

from __future__ import annotations

import os
from pathlib import Path

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

_STATE_DIR_ENV = "MCP_TELEGRAM_STATE_DIR"


def get_state_dir(*, mode: int = 0o700) -> Path:
    """Return the canonical mcp-telegram state directory.

    ``MCP_TELEGRAM_STATE_DIR`` lets host-side operator commands point at the
    deployed bind mount, while the default stays XDG-compliant for local use.
    """
    override = os.environ.get(_STATE_DIR_ENV)
    db_dir = Path(override).expanduser() if override else xdg_state_home() / "mcp-telegram"
    db_dir.mkdir(parents=True, exist_ok=True, mode=mode)
    return db_dir
