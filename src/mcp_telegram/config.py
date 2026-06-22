"""Operator configuration loaded from XDG config home."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import cast

from xdg_base_dirs import xdg_config_home  # type: ignore[import-error]


class ConfigError(RuntimeError):
    """Raised when required operator config is missing or invalid."""


def get_config_path() -> Path:
    """Return the mcp-telegram operator config path."""
    return xdg_config_home() / "mcp-telegram" / "config.toml"


def _read_config() -> dict[str, object]:
    path = get_config_path()
    if not path.exists():
        raise ConfigError(
            f'Missing mcp-telegram config: {path}. Create it with:\n[state]\ndir = "/path/to/mcp-telegram-state"'
        )
    with path.open("rb") as config_file:
        data = tomllib.load(config_file)
    return cast(dict[str, object], data)


def get_configured_state_dir() -> Path:
    """Return required state.dir from config.toml."""
    state_config = _read_config().get("state")
    if not isinstance(state_config, dict):
        raise ConfigError(f"Missing [state] section in {get_config_path()}")
    value = state_config.get("dir")
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError(f"Missing non-empty state.dir in {get_config_path()}")
    return Path(value).expanduser()
