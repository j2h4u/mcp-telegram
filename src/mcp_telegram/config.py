"""Typed operator configuration loaded from XDG config home."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from xdg_base_dirs import xdg_config_home  # type: ignore[import-error]

_VALID_HTTP_PORTS = range(1, 65_536)


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
class SchedulingConfig:
    """Intervals for local daemon maintenance loops."""

    scheduled_reconciliation_seconds: float = 900.0
    reconciliation_hourly_seconds: float = 3_600.0
    activity_hot_sweep_seconds: float = 3_600.0
    activity_cold_backfill_seconds: float = 300.0
    activity_cold_backfill_batch_pause_seconds: float = 5.0
    activity_cold_enroll_seconds: float = 1_800.0
    activity_cold_access_retry_seconds: float = 3_600.0


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Process logging policy."""

    level: str = "INFO"


@dataclass(frozen=True, slots=True)
class HttpServerConfig:
    """HTTP transport settings, including safe local-only defaults."""

    host: str = "127.0.0.1"
    port: int = 3100
    allow_unsafe: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()


HTTP_LOOPBACK_ALLOWED_HOSTS: tuple[str, ...] = (
    "127.0.0.1",
    "127.0.0.1:*",
    "localhost",
    "localhost:*",
    "::1",
    "[::1]",
    "[::1]:*",
)
HTTP_LOOPBACK_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1",
    "http://127.0.0.1:*",
    "https://127.0.0.1",
    "https://127.0.0.1:*",
    "http://localhost",
    "http://localhost:*",
    "https://localhost",
    "https://localhost:*",
    "http://[::1]",
    "http://[::1]:*",
    "https://[::1]",
    "https://[::1]:*",
)


@dataclass(frozen=True, slots=True)
class McpTelegramConfig:
    """Complete operator configuration for one mcp-telegram runtime."""

    state: StateConfig
    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    http: HttpServerConfig = field(default_factory=HttpServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


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


def _positive_float(data: dict[str, object], key: str, section: str, path: Path, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"Invalid {section}.{key} in {path}: expected number > 0")
    return float(value)


def _non_empty_str(data: dict[str, object], key: str, section: str, path: Path, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Invalid {section}.{key} in {path}: expected non-empty string")
    return value


def _http_port(value: object, *, error_type: type[Exception] = ConfigError) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in _VALID_HTTP_PORTS:
        raise error_type("HTTP port must be between 1 and 65535")
    return value


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _env_positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number > 0") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a number > 0")
    return value


def resolve_scheduling_config(
    config: SchedulingConfig,
    environ: Mapping[str, str] | None = None,
) -> SchedulingConfig:
    """Apply documented environment overrides to daemon scheduling policy."""
    env = os.environ if environ is None else environ
    return replace(
        config,
        scheduled_reconciliation_seconds=_env_positive_float(
            env, "SCHEDULED_RECONCILIATION_SECONDS", config.scheduled_reconciliation_seconds
        ),
        reconciliation_hourly_seconds=_env_positive_float(
            env, "RECON_HOURLY_SECONDS", config.reconciliation_hourly_seconds
        ),
        activity_hot_sweep_seconds=_env_positive_float(
            env, "ACTIVITY_HOT_SWEEP_SECONDS", config.activity_hot_sweep_seconds
        ),
        activity_cold_backfill_seconds=_env_positive_float(
            env, "ACTIVITY_COLD_BACKFILL_SECONDS", config.activity_cold_backfill_seconds
        ),
        activity_cold_backfill_batch_pause_seconds=_env_positive_float(
            env, "ACTIVITY_COLD_BACKFILL_BATCH_PAUSE", config.activity_cold_backfill_batch_pause_seconds
        ),
        activity_cold_enroll_seconds=_env_positive_float(
            env, "ACTIVITY_COLD_ENROLL_SECONDS", config.activity_cold_enroll_seconds
        ),
        activity_cold_access_retry_seconds=_env_positive_float(
            env, "ACTIVITY_COLD_ACCESS_RETRY_SECONDS", config.activity_cold_access_retry_seconds
        ),
    )


def resolve_logging_config(environ: Mapping[str, str] | None = None) -> LoggingConfig:
    """Return the normalized process log level from its environment override."""
    env = os.environ if environ is None else environ
    return LoggingConfig(level=env.get("LOG_LEVEL", LoggingConfig().level).upper())


def resolve_http_server_config(
    *,
    host: str | None = None,
    port: int | None = None,
    environ: Mapping[str, str] | None = None,
    base: HttpServerConfig | None = None,
) -> HttpServerConfig:
    """Resolve HTTP settings from CLI values, environment, and operator defaults."""
    env = os.environ if environ is None else environ
    defaults = HttpServerConfig() if base is None else base
    resolved_host = host if host is not None else env.get("MCP_TELEGRAM_HTTP_HOST") or defaults.host
    if port is not None:
        resolved_port = _http_port(port)
    else:
        raw_port = env.get("MCP_TELEGRAM_HTTP_PORT")
        if raw_port is None or not raw_port.strip():
            resolved_port = defaults.port
        elif raw_port.isdecimal():
            resolved_port = _http_port(int(raw_port))
        else:
            raise ConfigError("MCP_TELEGRAM_HTTP_PORT must be an integer")
    return HttpServerConfig(
        host=resolved_host,
        port=resolved_port,
        allow_unsafe=env.get("MCP_TELEGRAM_HTTP_ALLOW_UNSAFE", "").strip().lower() in {"1", "true", "yes", "on"},
        allowed_hosts=_csv(env.get("MCP_TELEGRAM_HTTP_ALLOWED_HOSTS")),
        allowed_origins=_csv(env.get("MCP_TELEGRAM_HTTP_ALLOWED_ORIGINS")),
    )


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


def _parse_state(data: dict[str, object], path: Path) -> StateConfig:
    state_data = _table(data, "state", path, required=True)
    assert state_data is not None
    _reject_unknown_keys(state_data, {"dir"}, "state", path)
    state_dir = state_data.get("dir")
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise ConfigError(f"Missing non-empty state.dir in {path}")
    return StateConfig(dir=Path(state_dir).expanduser())


def _parse_freshness(data: dict[str, object], path: Path) -> FreshnessConfig:
    freshness_data = _table(data, "freshness", path, required=False)
    if freshness_data is not None:
        _reject_unknown_keys(freshness_data, {"reactions", "read_receipts", "entities"}, "freshness", path)
    reactions_data = _nested_table(freshness_data, "reactions", "freshness.reactions", path) or {}
    receipts_data = _nested_table(freshness_data, "read_receipts", "freshness.read_receipts", path) or {}
    entities_data = _nested_table(freshness_data, "entities", "freshness.entities", path) or {}
    _reject_unknown_keys(reactions_data, {"freshness_ttl_seconds"}, "freshness.reactions", path)
    _reject_unknown_keys(receipts_data, {"read_at_ttl_seconds"}, "freshness.read_receipts", path)
    _reject_unknown_keys(
        entities_data,
        {
            "detail_ttl_seconds",
            "user_directory_ttl_seconds",
            "group_directory_ttl_seconds",
            "resolver_enrichment_ttl_seconds",
        },
        "freshness.entities",
        path,
    )
    defaults = FreshnessConfig()
    return FreshnessConfig(
        reactions=ReactionsConfig(
            freshness_ttl_seconds=_positive_int(
                reactions_data,
                "freshness_ttl_seconds",
                "freshness.reactions",
                path,
                defaults.reactions.freshness_ttl_seconds,
            )
        ),
        read_receipts=ReadReceiptsConfig(
            read_at_ttl_seconds=_positive_int(
                receipts_data,
                "read_at_ttl_seconds",
                "freshness.read_receipts",
                path,
                defaults.read_receipts.read_at_ttl_seconds,
            )
        ),
        entities=EntitiesConfig(
            detail_ttl_seconds=_positive_int(
                entities_data, "detail_ttl_seconds", "freshness.entities", path, defaults.entities.detail_ttl_seconds
            ),
            user_directory_ttl_seconds=_positive_int(
                entities_data,
                "user_directory_ttl_seconds",
                "freshness.entities",
                path,
                defaults.entities.user_directory_ttl_seconds,
            ),
            group_directory_ttl_seconds=_positive_int(
                entities_data,
                "group_directory_ttl_seconds",
                "freshness.entities",
                path,
                defaults.entities.group_directory_ttl_seconds,
            ),
            resolver_enrichment_ttl_seconds=_positive_int(
                entities_data,
                "resolver_enrichment_ttl_seconds",
                "freshness.entities",
                path,
                defaults.entities.resolver_enrichment_ttl_seconds,
            ),
        ),
    )


def _optional_section(data: dict[str, object], name: str, allowed: set[str], path: Path) -> dict[str, object]:
    section = _table(data, name, path, required=False) or {}
    _reject_unknown_keys(section, allowed, name, path)
    return section


def _parse_telemetry(data: dict[str, object], path: Path) -> TelemetryConfig:
    telemetry_data = _optional_section(data, "telemetry", {"retention_ttl_seconds"}, path)
    defaults = TelemetryConfig()
    return TelemetryConfig(
        retention_ttl_seconds=_positive_int(
            telemetry_data, "retention_ttl_seconds", "telemetry", path, defaults.retention_ttl_seconds
        )
    )


def _parse_scheduling(data: dict[str, object], path: Path) -> SchedulingConfig:
    defaults = SchedulingConfig()
    default_values = {
        "scheduled_reconciliation_seconds": defaults.scheduled_reconciliation_seconds,
        "reconciliation_hourly_seconds": defaults.reconciliation_hourly_seconds,
        "activity_hot_sweep_seconds": defaults.activity_hot_sweep_seconds,
        "activity_cold_backfill_seconds": defaults.activity_cold_backfill_seconds,
        "activity_cold_backfill_batch_pause_seconds": defaults.activity_cold_backfill_batch_pause_seconds,
        "activity_cold_enroll_seconds": defaults.activity_cold_enroll_seconds,
        "activity_cold_access_retry_seconds": defaults.activity_cold_access_retry_seconds,
    }
    scheduling_data = _optional_section(data, "scheduling", set(default_values), path)
    return SchedulingConfig(
        **{
            name: _positive_float(scheduling_data, name, "scheduling", path, default)
            for name, default in default_values.items()
        }
    )


def _parse_http(data: dict[str, object], path: Path) -> HttpServerConfig:
    http_data = _optional_section(data, "http", {"host", "port"}, path)
    defaults = HttpServerConfig()
    return HttpServerConfig(
        host=_non_empty_str(http_data, "host", "http", path, defaults.host),
        port=_http_port(http_data.get("port", defaults.port)),
    )


def _parse_logging(data: dict[str, object], path: Path) -> LoggingConfig:
    logging_data = _optional_section(data, "logging", {"level"}, path)
    defaults = LoggingConfig()
    return LoggingConfig(level=_non_empty_str(logging_data, "level", "logging", path, defaults.level).upper())


def load_config(path: Path | None = None) -> McpTelegramConfig:
    """Load and validate typed mcp-telegram operator configuration."""
    config_path = path or get_config_path()
    data = _read_config(config_path)
    _reject_unknown_keys(
        data, {"state", "freshness", "telemetry", "scheduling", "http", "logging"}, "root", config_path
    )
    return McpTelegramConfig(
        state=_parse_state(data, config_path),
        freshness=_parse_freshness(data, config_path),
        telemetry=_parse_telemetry(data, config_path),
        scheduling=_parse_scheduling(data, config_path),
        http=_parse_http(data, config_path),
        logging=_parse_logging(data, config_path),
    )
