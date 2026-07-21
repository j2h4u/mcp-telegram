"""Typed operator configuration loaded from XDG config home."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from xdg_base_dirs import xdg_config_home  # type: ignore[import-error]


class ConfigError(RuntimeError):
    """Raised when required operator config is missing or invalid."""


@dataclass(frozen=True, slots=True)
class StateConfig:
    """Persistent local-state location."""

    dir: Path


@dataclass(frozen=True, slots=True)
class ReactionsConfig:
    """Freshness policy for locally projected reaction facts."""

    freshness_ttl_seconds: int = 600


@dataclass(frozen=True, slots=True)
class McpTelegramConfig:
    """Complete operator configuration for one mcp-telegram runtime."""

    state: StateConfig
    reactions: ReactionsConfig = field(default_factory=ReactionsConfig)


def get_config_path() -> Path:
    """Return the default mcp-telegram operator config path."""
    return xdg_config_home() / "mcp-telegram" / "config.toml"


def _read_config(path: Path) -> dict[str, object]:
    if not path.exists():
        raise ConfigError(
            f'Missing mcp-telegram config: {path}. Create it with:\n[state]\ndir = "/path/to/mcp-telegram-state"'
        )
    try:
        with path.open("rb") as config_file:
            return cast(dict[str, object], tomllib.load(config_file))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Could not read config {path}: {exc}") from exc


def _table(data: dict[str, object], key: str, path: Path, *, required: bool) -> dict[str, object] | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, dict):
        qualifier = "Missing" if value is None else "Invalid"
        raise ConfigError(f"{qualifier} [{key}] section in {path}")
    return cast(dict[str, object], value)


def load_config(path: Path | None = None) -> McpTelegramConfig:
    """Load and validate typed mcp-telegram operator configuration."""
    config_path = path or get_config_path()
    data = _read_config(config_path)
    state_data = _table(data, "state", config_path, required=True)
    assert state_data is not None
    state_dir = state_data.get("dir")
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise ConfigError(f"Missing non-empty state.dir in {config_path}")

    reactions_data = _table(data, "reactions", config_path, required=False)
    reactions = ReactionsConfig()
    if reactions_data is not None and "freshness_ttl_seconds" in reactions_data:
        ttl = reactions_data["freshness_ttl_seconds"]
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
            raise ConfigError(f"Invalid reactions.freshness_ttl_seconds in {config_path}: expected integer >= 1")
        reactions = ReactionsConfig(freshness_ttl_seconds=ttl)

    return McpTelegramConfig(state=StateConfig(dir=Path(state_dir).expanduser()), reactions=reactions)


def get_configured_state_dir() -> Path:
    """Return required state.dir from config.toml."""
    return load_config().state.dir
