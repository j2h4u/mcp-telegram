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
class FolderProjectionConfig:
    """Daemon-owned schedule and health policy for folder projections."""

    refresh_interval_seconds: float = 900.0
    jitter_ratio: float = 0.10
    retry_delays_seconds: tuple[int, ...] = (60, 120, 240, 480)
    retry_cap_seconds: int = 900
    warning_failure_threshold: int = 3
    stale_after_seconds: int | None = None

    @property
    def stale_threshold_seconds(self) -> int:
        """Derive the stale boundary unless the operator configured one."""
        if self.stale_after_seconds is not None:
            return self.stale_after_seconds
        return int(self.refresh_interval_seconds + self.retry_cap_seconds)


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
class FloodWaitConfig:
    """Account-level FloodWait storm kill-switch policy."""

    kill_switch_enabled: bool = True
    kill_switch_window_seconds: int = 600
    kill_switch_max_events: int = 5
    kill_switch_max_wait_seconds: int = 900
    kill_switch_minimum_cooldown_seconds: int = 1_800


@dataclass(frozen=True, slots=True)
class TelegramRpcConfig:
    """Account-level Telegram RPC budget."""

    max_calls_per_period: int = 30
    period_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class SchedulingConfig:
    """Intervals for local daemon maintenance loops."""

    scheduled_reconciliation_seconds: float = 900.0
    reconciliation_hourly_seconds: float = 3_600.0
    delta_catch_up_interval_seconds: float = 300.0
    delta_catch_up_max_probes_per_cycle: int = 10
    delta_catch_up_probe_pause_seconds: float = 1.0
    access_probe_interval_seconds: float = 86_400.0
    access_probe_max_dialogs_per_cycle: int = 3
    access_probe_cooldown_seconds: int = 604_800
    access_probe_pause_seconds: float = 1.0
    message_fact_refresh_seconds: float = 600.0
    message_fact_refresh_reaction_max_messages_per_cycle: int = 5
    message_fact_refresh_read_at_max_messages_per_cycle: int = 5
    message_fact_refresh_pause_seconds: float = 1.0
    activity_hot_sweep_seconds: float = 3_600.0
    activity_cold_backfill_seconds: float = 300.0
    activity_cold_backfill_batch_pause_seconds: float = 5.0
    activity_cold_enroll_seconds: float = 1_800.0
    activity_cold_access_retry_seconds: float = 3_600.0
    scheduled_flood_sleep_threshold_seconds: int = 0
    folder_projection: FolderProjectionConfig = field(default_factory=FolderProjectionConfig)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Process logging policy."""

    level: str = "INFO"
    daemon_api_slow_request_seconds: float = 5.0


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
    flood_wait: FloodWaitConfig = field(default_factory=FloodWaitConfig)
    telegram_rpc: TelegramRpcConfig = field(default_factory=TelegramRpcConfig)
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


def _non_negative_int(data: dict[str, object], key: str, section: str, path: Path, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"Invalid {section}.{key} in {path}: expected integer >= 0")
    return value


def _bool(data: dict[str, object], key: str, section: str, path: Path, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"Invalid {section}.{key} in {path}: expected boolean")
    return value


def _non_empty_str(data: dict[str, object], key: str, section: str, path: Path, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Invalid {section}.{key} in {path}: expected non-empty string")
    return value


def _retry_schedule(
    data: dict[str, object], key: str, section: str, path: Path, default: tuple[int, ...]
) -> tuple[int, ...]:
    value = data.get(key, default)
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value)
    ):
        raise ConfigError(f"Invalid {section}.{key} in {path}: expected a non-empty array of integers >= 1")
    return tuple(value)


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


def _env_non_negative_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer >= 0") from exc
    if value < 0:
        raise ConfigError(f"{name} must be an integer >= 0")
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
        scheduled_flood_sleep_threshold_seconds=_env_non_negative_int(
            env,
            "SCHEDULED_FLOOD_SLEEP_THRESHOLD_SECONDS",
            config.scheduled_flood_sleep_threshold_seconds,
        ),
        reconciliation_hourly_seconds=_env_positive_float(
            env, "RECON_HOURLY_SECONDS", config.reconciliation_hourly_seconds
        ),
        delta_catch_up_interval_seconds=_env_positive_float(
            env, "DELTA_CATCH_UP_INTERVAL_SECONDS", config.delta_catch_up_interval_seconds
        ),
        delta_catch_up_max_probes_per_cycle=_env_non_negative_int(
            env, "DELTA_CATCH_UP_MAX_PROBES_PER_CYCLE", config.delta_catch_up_max_probes_per_cycle
        ),
        delta_catch_up_probe_pause_seconds=_env_positive_float(
            env, "DELTA_CATCH_UP_PROBE_PAUSE_SECONDS", config.delta_catch_up_probe_pause_seconds
        ),
        access_probe_interval_seconds=_env_positive_float(
            env, "ACCESS_PROBE_INTERVAL_SECONDS", config.access_probe_interval_seconds
        ),
        access_probe_max_dialogs_per_cycle=_env_non_negative_int(
            env, "ACCESS_PROBE_MAX_DIALOGS_PER_CYCLE", config.access_probe_max_dialogs_per_cycle
        ),
        access_probe_cooldown_seconds=_env_non_negative_int(
            env, "ACCESS_PROBE_COOLDOWN_SECONDS", config.access_probe_cooldown_seconds
        ),
        access_probe_pause_seconds=_env_positive_float(
            env, "ACCESS_PROBE_PAUSE_SECONDS", config.access_probe_pause_seconds
        ),
        message_fact_refresh_seconds=_env_positive_float(
            env, "MESSAGE_FACT_REFRESH_SECONDS", config.message_fact_refresh_seconds
        ),
        message_fact_refresh_reaction_max_messages_per_cycle=_env_non_negative_int(
            env,
            "MESSAGE_FACT_REFRESH_REACTION_MAX_MESSAGES_PER_CYCLE",
            config.message_fact_refresh_reaction_max_messages_per_cycle,
        ),
        message_fact_refresh_read_at_max_messages_per_cycle=_env_non_negative_int(
            env,
            "MESSAGE_FACT_REFRESH_READ_AT_MAX_MESSAGES_PER_CYCLE",
            config.message_fact_refresh_read_at_max_messages_per_cycle,
        ),
        message_fact_refresh_pause_seconds=_env_positive_float(
            env,
            "MESSAGE_FACT_REFRESH_PAUSE_SECONDS",
            config.message_fact_refresh_pause_seconds,
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
    defaults = LoggingConfig()
    return LoggingConfig(
        level=env.get("LOG_LEVEL", defaults.level).upper(),
        daemon_api_slow_request_seconds=_env_positive_float(
            env,
            "DAEMON_API_SLOW_REQUEST_SECONDS",
            defaults.daemon_api_slow_request_seconds,
        ),
    )


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


def _parse_flood_wait(data: dict[str, object], path: Path) -> FloodWaitConfig:
    flood_data = _optional_section(
        data,
        "flood_wait",
        {
            "kill_switch_enabled",
            "kill_switch_window_seconds",
            "kill_switch_max_events",
            "kill_switch_max_wait_seconds",
            "kill_switch_minimum_cooldown_seconds",
        },
        path,
    )
    defaults = FloodWaitConfig()
    return FloodWaitConfig(
        kill_switch_enabled=_bool(flood_data, "kill_switch_enabled", "flood_wait", path, defaults.kill_switch_enabled),
        kill_switch_window_seconds=_positive_int(
            flood_data, "kill_switch_window_seconds", "flood_wait", path, defaults.kill_switch_window_seconds
        ),
        kill_switch_max_events=_positive_int(
            flood_data, "kill_switch_max_events", "flood_wait", path, defaults.kill_switch_max_events
        ),
        kill_switch_max_wait_seconds=_positive_int(
            flood_data, "kill_switch_max_wait_seconds", "flood_wait", path, defaults.kill_switch_max_wait_seconds
        ),
        kill_switch_minimum_cooldown_seconds=_positive_int(
            flood_data,
            "kill_switch_minimum_cooldown_seconds",
            "flood_wait",
            path,
            defaults.kill_switch_minimum_cooldown_seconds,
        ),
    )


def _parse_telegram_rpc(data: dict[str, object], path: Path) -> TelegramRpcConfig:
    rpc_data = _optional_section(data, "telegram_rpc", {"max_calls_per_period", "period_seconds"}, path)
    defaults = TelegramRpcConfig()
    return TelegramRpcConfig(
        max_calls_per_period=_non_negative_int(
            rpc_data,
            "max_calls_per_period",
            "telegram_rpc",
            path,
            defaults.max_calls_per_period,
        ),
        period_seconds=_positive_float(rpc_data, "period_seconds", "telegram_rpc", path, defaults.period_seconds),
    )


def _parse_scheduling(data: dict[str, object], path: Path) -> SchedulingConfig:
    defaults = SchedulingConfig()
    allowed = {
        "scheduled_reconciliation_seconds",
        "scheduled_flood_sleep_threshold_seconds",
        "reconciliation_hourly_seconds",
        "delta_catch_up_interval_seconds",
        "delta_catch_up_max_probes_per_cycle",
        "delta_catch_up_probe_pause_seconds",
        "access_probe_interval_seconds",
        "access_probe_max_dialogs_per_cycle",
        "access_probe_cooldown_seconds",
        "access_probe_pause_seconds",
        "message_fact_refresh_seconds",
        "message_fact_refresh_reaction_max_messages_per_cycle",
        "message_fact_refresh_read_at_max_messages_per_cycle",
        "message_fact_refresh_pause_seconds",
        "activity_hot_sweep_seconds",
        "activity_cold_backfill_seconds",
        "activity_cold_backfill_batch_pause_seconds",
        "activity_cold_enroll_seconds",
        "activity_cold_access_retry_seconds",
        "folder_projection",
    }
    scheduling_data = _optional_section(data, "scheduling", allowed, path)
    folder_data = _nested_table(scheduling_data, "folder_projection", "scheduling.folder_projection", path) or {}
    _reject_unknown_keys(
        folder_data,
        {
            "refresh_interval_seconds",
            "jitter_ratio",
            "retry_delays_seconds",
            "retry_cap_seconds",
            "warning_failure_threshold",
            "stale_after_seconds",
        },
        "scheduling.folder_projection",
        path,
    )
    folder_defaults = defaults.folder_projection
    stale_value = folder_data.get("stale_after_seconds", folder_defaults.stale_after_seconds)
    if stale_value is not None and (
        isinstance(stale_value, bool) or not isinstance(stale_value, int) or stale_value < 1
    ):
        raise ConfigError(f"Invalid scheduling.folder_projection.stale_after_seconds in {path}: expected integer >= 1")
    jitter = folder_data.get("jitter_ratio", folder_defaults.jitter_ratio)
    if isinstance(jitter, bool) or not isinstance(jitter, (int, float)) or not 0 <= float(jitter) <= 1:
        raise ConfigError(
            f"Invalid scheduling.folder_projection.jitter_ratio in {path}: expected number between 0 and 1"
        )
    refresh_interval_seconds = _positive_float(
        folder_data,
        "refresh_interval_seconds",
        "scheduling.folder_projection",
        path,
        folder_defaults.refresh_interval_seconds,
    )
    if refresh_interval_seconds < 1:
        raise ConfigError(
            f"Invalid scheduling.folder_projection.refresh_interval_seconds in {path}: expected number >= 1"
        )
    folder_projection = FolderProjectionConfig(
        refresh_interval_seconds=refresh_interval_seconds,
        jitter_ratio=float(jitter),
        retry_delays_seconds=_retry_schedule(
            folder_data,
            "retry_delays_seconds",
            "scheduling.folder_projection",
            path,
            folder_defaults.retry_delays_seconds,
        ),
        retry_cap_seconds=_positive_int(
            folder_data, "retry_cap_seconds", "scheduling.folder_projection", path, folder_defaults.retry_cap_seconds
        ),
        warning_failure_threshold=_positive_int(
            folder_data,
            "warning_failure_threshold",
            "scheduling.folder_projection",
            path,
            folder_defaults.warning_failure_threshold,
        ),
        stale_after_seconds=stale_value,
    )
    return SchedulingConfig(
        scheduled_reconciliation_seconds=_positive_float(
            scheduling_data,
            "scheduled_reconciliation_seconds",
            "scheduling",
            path,
            defaults.scheduled_reconciliation_seconds,
        ),
        scheduled_flood_sleep_threshold_seconds=_non_negative_int(
            scheduling_data,
            "scheduled_flood_sleep_threshold_seconds",
            "scheduling",
            path,
            defaults.scheduled_flood_sleep_threshold_seconds,
        ),
        reconciliation_hourly_seconds=_positive_float(
            scheduling_data,
            "reconciliation_hourly_seconds",
            "scheduling",
            path,
            defaults.reconciliation_hourly_seconds,
        ),
        delta_catch_up_interval_seconds=_positive_float(
            scheduling_data,
            "delta_catch_up_interval_seconds",
            "scheduling",
            path,
            defaults.delta_catch_up_interval_seconds,
        ),
        delta_catch_up_max_probes_per_cycle=_non_negative_int(
            scheduling_data,
            "delta_catch_up_max_probes_per_cycle",
            "scheduling",
            path,
            defaults.delta_catch_up_max_probes_per_cycle,
        ),
        delta_catch_up_probe_pause_seconds=_positive_float(
            scheduling_data,
            "delta_catch_up_probe_pause_seconds",
            "scheduling",
            path,
            defaults.delta_catch_up_probe_pause_seconds,
        ),
        access_probe_interval_seconds=_positive_float(
            scheduling_data,
            "access_probe_interval_seconds",
            "scheduling",
            path,
            defaults.access_probe_interval_seconds,
        ),
        access_probe_max_dialogs_per_cycle=_non_negative_int(
            scheduling_data,
            "access_probe_max_dialogs_per_cycle",
            "scheduling",
            path,
            defaults.access_probe_max_dialogs_per_cycle,
        ),
        access_probe_cooldown_seconds=_non_negative_int(
            scheduling_data,
            "access_probe_cooldown_seconds",
            "scheduling",
            path,
            defaults.access_probe_cooldown_seconds,
        ),
        access_probe_pause_seconds=_positive_float(
            scheduling_data,
            "access_probe_pause_seconds",
            "scheduling",
            path,
            defaults.access_probe_pause_seconds,
        ),
        message_fact_refresh_seconds=_positive_float(
            scheduling_data,
            "message_fact_refresh_seconds",
            "scheduling",
            path,
            defaults.message_fact_refresh_seconds,
        ),
        message_fact_refresh_reaction_max_messages_per_cycle=_non_negative_int(
            scheduling_data,
            "message_fact_refresh_reaction_max_messages_per_cycle",
            "scheduling",
            path,
            defaults.message_fact_refresh_reaction_max_messages_per_cycle,
        ),
        message_fact_refresh_read_at_max_messages_per_cycle=_non_negative_int(
            scheduling_data,
            "message_fact_refresh_read_at_max_messages_per_cycle",
            "scheduling",
            path,
            defaults.message_fact_refresh_read_at_max_messages_per_cycle,
        ),
        message_fact_refresh_pause_seconds=_positive_float(
            scheduling_data,
            "message_fact_refresh_pause_seconds",
            "scheduling",
            path,
            defaults.message_fact_refresh_pause_seconds,
        ),
        activity_hot_sweep_seconds=_positive_float(
            scheduling_data,
            "activity_hot_sweep_seconds",
            "scheduling",
            path,
            defaults.activity_hot_sweep_seconds,
        ),
        activity_cold_backfill_seconds=_positive_float(
            scheduling_data,
            "activity_cold_backfill_seconds",
            "scheduling",
            path,
            defaults.activity_cold_backfill_seconds,
        ),
        activity_cold_backfill_batch_pause_seconds=_positive_float(
            scheduling_data,
            "activity_cold_backfill_batch_pause_seconds",
            "scheduling",
            path,
            defaults.activity_cold_backfill_batch_pause_seconds,
        ),
        activity_cold_enroll_seconds=_positive_float(
            scheduling_data,
            "activity_cold_enroll_seconds",
            "scheduling",
            path,
            defaults.activity_cold_enroll_seconds,
        ),
        activity_cold_access_retry_seconds=_positive_float(
            scheduling_data,
            "activity_cold_access_retry_seconds",
            "scheduling",
            path,
            defaults.activity_cold_access_retry_seconds,
        ),
        folder_projection=folder_projection,
    )


def _parse_http(data: dict[str, object], path: Path) -> HttpServerConfig:
    http_data = _optional_section(data, "http", {"host", "port"}, path)
    defaults = HttpServerConfig()
    return HttpServerConfig(
        host=_non_empty_str(http_data, "host", "http", path, defaults.host),
        port=_http_port(http_data.get("port", defaults.port)),
    )


def _parse_logging(data: dict[str, object], path: Path) -> LoggingConfig:
    logging_data = _optional_section(data, "logging", {"level", "daemon_api_slow_request_seconds"}, path)
    defaults = LoggingConfig()
    return LoggingConfig(
        level=_non_empty_str(logging_data, "level", "logging", path, defaults.level).upper(),
        daemon_api_slow_request_seconds=_positive_float(
            logging_data,
            "daemon_api_slow_request_seconds",
            "logging",
            path,
            defaults.daemon_api_slow_request_seconds,
        ),
    )


def load_config(path: Path | None = None) -> McpTelegramConfig:
    """Load and validate typed mcp-telegram operator configuration."""
    config_path = path or get_config_path()
    data = _read_config(config_path)
    _reject_unknown_keys(
        data,
        {"state", "freshness", "telemetry", "flood_wait", "telegram_rpc", "scheduling", "http", "logging"},
        "root",
        config_path,
    )
    return McpTelegramConfig(
        state=_parse_state(data, config_path),
        freshness=_parse_freshness(data, config_path),
        telemetry=_parse_telemetry(data, config_path),
        flood_wait=_parse_flood_wait(data, config_path),
        telegram_rpc=_parse_telegram_rpc(data, config_path),
        scheduling=_parse_scheduling(data, config_path),
        http=_parse_http(data, config_path),
        logging=_parse_logging(data, config_path),
    )
