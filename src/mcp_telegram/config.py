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
class ReadReceiptsConfig:
    """Freshness policy for Telegram read-receipt facts."""

    read_at_ttl_seconds: int = 600


@dataclass(frozen=True, slots=True)
class EntitiesConfig:
    """Freshness policy for cached entity and resolver facts."""

    detail_ttl_seconds: int = 300
    user_directory_ttl_seconds: int = 2_592_000
    group_directory_ttl_seconds: int = 604_800
    resolver_enrichment_ttl_seconds: int = 300


@dataclass(frozen=True, slots=True)
class FreshnessConfig:
    """All Telegram-derived fact freshness policies."""

    reactions: ReactionsConfig = field(default_factory=ReactionsConfig)
    read_receipts: ReadReceiptsConfig = field(default_factory=ReadReceiptsConfig)
    entities: EntitiesConfig = field(default_factory=EntitiesConfig)


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Retention policy for local telemetry."""

    retention_ttl_seconds: int = 2_592_000


@dataclass(frozen=True, slots=True)
class McpTelegramConfig:
    """Complete operator configuration for one mcp-telegram runtime."""

    state: StateConfig
    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)


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


def _reject_unknown_keys(data: dict[str, object], allowed: set[str], section: str, path: Path) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"Unknown key(s) in [{section}] in {path}: {', '.join(unknown)}")


def _positive_int(data: dict[str, object], key: str, section: str, path: Path, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"Invalid {section}.{key} in {path}: expected integer >= 1")
    return value


def _nested_table(
    parent: dict[str, object] | None,
    key: str,
    section: str,
    path: Path,
) -> dict[str, object] | None:
    if parent is None:
        return None
    value = parent.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"Invalid [{section}] section in {path}")
    return cast(dict[str, object], value)


def load_config(path: Path | None = None) -> McpTelegramConfig:
    """Load and validate typed mcp-telegram operator configuration."""
    config_path = path or get_config_path()
    data = _read_config(config_path)
    _reject_unknown_keys(data, {"state", "freshness", "telemetry"}, "root", config_path)

    state_data = _table(data, "state", config_path, required=True)
    assert state_data is not None
    _reject_unknown_keys(state_data, {"dir"}, "state", config_path)
    state_dir = state_data.get("dir")
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise ConfigError(f"Missing non-empty state.dir in {config_path}")

    freshness_data = _table(data, "freshness", config_path, required=False)
    if freshness_data is not None:
        _reject_unknown_keys(freshness_data, {"reactions", "read_receipts", "entities"}, "freshness", config_path)
    reactions_data = _nested_table(freshness_data, "reactions", "freshness.reactions", config_path) or {}
    receipts_data = _nested_table(freshness_data, "read_receipts", "freshness.read_receipts", config_path) or {}
    entities_data = _nested_table(freshness_data, "entities", "freshness.entities", config_path) or {}
    _reject_unknown_keys(reactions_data, {"freshness_ttl_seconds"}, "freshness.reactions", config_path)
    _reject_unknown_keys(receipts_data, {"read_at_ttl_seconds"}, "freshness.read_receipts", config_path)
    _reject_unknown_keys(
        entities_data,
        {
            "detail_ttl_seconds",
            "user_directory_ttl_seconds",
            "group_directory_ttl_seconds",
            "resolver_enrichment_ttl_seconds",
        },
        "freshness.entities",
        config_path,
    )

    telemetry_data = _table(data, "telemetry", config_path, required=False) or {}
    _reject_unknown_keys(telemetry_data, {"retention_ttl_seconds"}, "telemetry", config_path)

    # The dataclass instances are the single owner of parser fallback values.
    # Keep this adjacent to parsing so adding a policy field cannot introduce a
    # second, literal default in the loader.
    defaults = FreshnessConfig()
    telemetry_defaults = TelemetryConfig()
    freshness = FreshnessConfig(
        reactions=ReactionsConfig(
            freshness_ttl_seconds=_positive_int(
                reactions_data,
                "freshness_ttl_seconds",
                "freshness.reactions",
                config_path,
                defaults.reactions.freshness_ttl_seconds,
            )
        ),
        read_receipts=ReadReceiptsConfig(
            read_at_ttl_seconds=_positive_int(
                receipts_data,
                "read_at_ttl_seconds",
                "freshness.read_receipts",
                config_path,
                defaults.read_receipts.read_at_ttl_seconds,
            )
        ),
        entities=EntitiesConfig(
            detail_ttl_seconds=_positive_int(
                entities_data,
                "detail_ttl_seconds",
                "freshness.entities",
                config_path,
                defaults.entities.detail_ttl_seconds,
            ),
            user_directory_ttl_seconds=_positive_int(
                entities_data,
                "user_directory_ttl_seconds",
                "freshness.entities",
                config_path,
                defaults.entities.user_directory_ttl_seconds,
            ),
            group_directory_ttl_seconds=_positive_int(
                entities_data,
                "group_directory_ttl_seconds",
                "freshness.entities",
                config_path,
                defaults.entities.group_directory_ttl_seconds,
            ),
            resolver_enrichment_ttl_seconds=_positive_int(
                entities_data,
                "resolver_enrichment_ttl_seconds",
                "freshness.entities",
                config_path,
                defaults.entities.resolver_enrichment_ttl_seconds,
            ),
        ),
    )
    telemetry = TelemetryConfig(
        retention_ttl_seconds=_positive_int(
            telemetry_data,
            "retention_ttl_seconds",
            "telemetry",
            config_path,
            telemetry_defaults.retention_ttl_seconds,
        )
    )
    return McpTelegramConfig(
        state=StateConfig(dir=Path(state_dir).expanduser()), freshness=freshness, telemetry=telemetry
    )
