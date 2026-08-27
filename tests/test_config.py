"""Validation tests for typed operator configuration."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mcp_telegram.config import (
    ConfigError,
    EntitiesConfig,
    FloodWaitConfig,
    FolderProjectionConfig,
    FreshnessConfig,
    HttpServerConfig,
    MediaHydrationConfig,
    ReactionsConfig,
    ReadReceiptsConfig,
    SchedulingConfig,
    StateConfig,
    TelegramRpcConfig,
    TelemetryConfig,
    load_config,
    resolve_http_server_config,
    resolve_logging_config,
    resolve_scheduling_config,
)


def _write_config(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(contents, encoding="utf-8")
    return path


def test_load_config_uses_frozen_typed_defaults(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, '[state]\ndir = "/var/lib/mcp-telegram"\n'))

    assert config.state == StateConfig(dir=Path("/var/lib/mcp-telegram"))
    assert config.freshness == FreshnessConfig()
    assert config.telemetry == TelemetryConfig()
    assert config.flood_wait == FloodWaitConfig()
    assert config.telegram_rpc == TelegramRpcConfig()
    assert config.scheduling == SchedulingConfig()
    assert config.scheduling.activity_rpc_timeout_seconds == 120.0
    assert config.scheduling.media_hydration == MediaHydrationConfig()
    assert config.http == HttpServerConfig()
    with pytest.raises(FrozenInstanceError):
        config.freshness.reactions.freshness_ttl_seconds = 1  # type: ignore[misc]


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_load_config_rejects_non_finite_positive_float(tmp_path: Path, value: str) -> None:
    path = _write_config(
        tmp_path, f'[state]\ndir = "/state"\n\n[scheduling]\ndelta_catch_up_interval_seconds = {value}\n'
    )

    with pytest.raises(ConfigError, match="delta_catch_up_interval_seconds"):
        load_config(path)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_resolve_scheduling_rejects_non_finite_positive_float(value: str) -> None:
    with pytest.raises(ConfigError, match="DELTA_CATCH_UP_INTERVAL_SECONDS"):
        resolve_scheduling_config(SchedulingConfig(), {"DELTA_CATCH_UP_INTERVAL_SECONDS": value})


def test_load_config_reads_nested_policy_overrides(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """[state]
dir = "/var/lib/mcp-telegram"

[freshness.reactions]
freshness_ttl_seconds = 40

[freshness.read_receipts]
read_at_ttl_seconds = 41

[freshness.entities]
detail_ttl_seconds = 42
user_directory_ttl_seconds = 43
group_directory_ttl_seconds = 44
resolver_enrichment_ttl_seconds = 45

[scheduling.folder_projection]
refresh_interval_seconds = 900
jitter_ratio = 0.1
retry_delays_seconds = [60, 120, 240, 480]
retry_cap_seconds = 900
warning_failure_threshold = 3
stale_after_seconds = 46

[telemetry]
retention_ttl_seconds = 47

[flood_wait]
kill_switch_enabled = true
kill_switch_window_seconds = 600
kill_switch_max_events = 5
kill_switch_max_wait_seconds = 900
kill_switch_minimum_cooldown_seconds = 1800

[telegram_rpc]
max_calls_per_period = 12
period_seconds = 30

[scheduling]
scheduled_reconciliation_seconds = 48
read_position_reconciliation_seconds = 46.5
read_position_reconciliation_max_dialogs_per_pass = 12
read_position_reconciliation_failure_cooldown_seconds = 47
read_position_reconciliation_batch_size = 16
read_position_reconciliation_batch_pause_seconds = 1.75
scheduled_flood_sleep_threshold_seconds = 0
reconciliation_hourly_seconds = 49
delta_catch_up_interval_seconds = 50
delta_catch_up_max_probes_per_cycle = 7
delta_catch_up_probe_pause_seconds = 3
access_probe_interval_seconds = 86401
access_probe_max_dialogs_per_cycle = 2
access_probe_cooldown_seconds = 604801
access_probe_pause_seconds = 5
message_fact_refresh_seconds = 52
message_fact_refresh_reaction_max_messages_per_cycle = 6
message_fact_refresh_read_at_max_messages_per_cycle = 7
message_fact_refresh_pause_seconds = 4
activity_hot_sweep_seconds = 51
activity_rpc_timeout_seconds = 53

[scheduling.media_hydration]
interval_seconds = 48
max_requests_per_cycle = 4
max_jobs_per_cycle = 301
batch_size = 99
pause_between_requests_seconds = 5.5
retry_delay_seconds = 21601
circuit_retry_seconds = 1801
max_attempts = 4

[http]
host = "localhost"
port = 3200

[logging]
level = "warning"
daemon_api_slow_request_seconds = 2.5
""",
    )

    config = load_config(path)
    assert config.freshness.reactions == ReactionsConfig(freshness_ttl_seconds=40)
    assert config.freshness.read_receipts == ReadReceiptsConfig(read_at_ttl_seconds=41)
    assert config.freshness.entities == EntitiesConfig(42, 43, 44, 45)
    assert config.scheduling.folder_projection == FolderProjectionConfig(stale_after_seconds=46)
    assert config.telemetry == TelemetryConfig(retention_ttl_seconds=47)
    assert config.flood_wait == FloodWaitConfig(
        kill_switch_enabled=True,
        kill_switch_window_seconds=600,
        kill_switch_max_events=5,
        kill_switch_max_wait_seconds=900,
        kill_switch_minimum_cooldown_seconds=1800,
    )
    assert config.telegram_rpc == TelegramRpcConfig(max_calls_per_period=12, period_seconds=30.0)
    assert config.scheduling == SchedulingConfig(
        scheduled_reconciliation_seconds=48.0,
        read_position_reconciliation_seconds=46.5,
        read_position_reconciliation_max_dialogs_per_pass=12,
        read_position_reconciliation_failure_cooldown_seconds=47.0,
        read_position_reconciliation_batch_size=16,
        read_position_reconciliation_batch_pause_seconds=1.75,
        reconciliation_hourly_seconds=49.0,
        delta_catch_up_interval_seconds=50.0,
        delta_catch_up_max_probes_per_cycle=7,
        delta_catch_up_probe_pause_seconds=3.0,
        access_probe_interval_seconds=86401.0,
        access_probe_max_dialogs_per_cycle=2,
        access_probe_cooldown_seconds=604801,
        access_probe_pause_seconds=5.0,
        message_fact_refresh_seconds=52.0,
        message_fact_refresh_reaction_max_messages_per_cycle=6,
        message_fact_refresh_read_at_max_messages_per_cycle=7,
        message_fact_refresh_pause_seconds=4.0,
        activity_hot_sweep_seconds=51.0,
        activity_rpc_timeout_seconds=53.0,
        media_hydration=MediaHydrationConfig(
            interval_seconds=48.0,
            max_requests_per_cycle=4,
            max_jobs_per_cycle=301,
            batch_size=99,
            pause_between_requests_seconds=5.5,
            retry_delay_seconds=21601,
            circuit_retry_seconds=1801,
            max_attempts=4,
        ),
        scheduled_flood_sleep_threshold_seconds=0,
        folder_projection=FolderProjectionConfig(stale_after_seconds=46),
    )
    assert config.http == HttpServerConfig(host="localhost", port=3200)
    assert config.logging.level == "WARNING"
    assert config.logging.daemon_api_slow_request_seconds == 2.5


def test_runtime_environment_overrides_are_parsed_by_config_model() -> None:
    scheduling = resolve_scheduling_config(
        SchedulingConfig(),
        {
            "SCHEDULED_RECONCILIATION_SECONDS": "47.5",
            "READ_POSITION_RECONCILIATION_SECONDS": "45.5",
            "READ_POSITION_RECONCILIATION_MAX_DIALOGS_PER_PASS": "11",
            "READ_POSITION_RECONCILIATION_FAILURE_COOLDOWN_SECONDS": "46",
            "READ_POSITION_RECONCILIATION_BATCH_SIZE": "16",
            "READ_POSITION_RECONCILIATION_BATCH_PAUSE_SECONDS": "1.75",
            "SCHEDULED_FLOOD_SLEEP_THRESHOLD_SECONDS": "0",
            "RECON_HOURLY_SECONDS": "48",
            "DELTA_CATCH_UP_INTERVAL_SECONDS": "54",
            "DELTA_CATCH_UP_MAX_PROBES_PER_CYCLE": "8",
            "DELTA_CATCH_UP_PROBE_PAUSE_SECONDS": "9",
            "MESSAGE_FACT_REFRESH_SECONDS": "55",
            "MESSAGE_FACT_REFRESH_REACTION_MAX_MESSAGES_PER_CYCLE": "6",
            "MESSAGE_FACT_REFRESH_READ_AT_MAX_MESSAGES_PER_CYCLE": "7",
            "MESSAGE_FACT_REFRESH_PAUSE_SECONDS": "7",
            "ACTIVITY_HOT_SWEEP_SECONDS": "49",
            "ACTIVITY_RPC_TIMEOUT_SECONDS": "54",
            "ACTIVITY_COLD_BACKFILL_SECONDS": "50",
            "ACTIVITY_COLD_BACKFILL_BATCH_PAUSE": "51",
            "ACTIVITY_COLD_ENROLL_SECONDS": "52",
            "ACTIVITY_COLD_ACCESS_RETRY_SECONDS": "53",
            "ACCESS_PROBE_INTERVAL_SECONDS": "86402",
            "ACCESS_PROBE_MAX_DIALOGS_PER_CYCLE": "4",
            "ACCESS_PROBE_COOLDOWN_SECONDS": "604802",
            "ACCESS_PROBE_PAUSE_SECONDS": "6",
            "MEDIA_HYDRATION_INTERVAL_SECONDS": "47",
            "MEDIA_HYDRATION_MAX_REQUESTS_PER_CYCLE": "4",
            "MEDIA_HYDRATION_MAX_JOBS_PER_CYCLE": "301",
            "MEDIA_HYDRATION_BATCH_SIZE": "99",
            "MEDIA_HYDRATION_PAUSE_BETWEEN_REQUESTS_SECONDS": "5.5",
            "MEDIA_HYDRATION_RETRY_DELAY_SECONDS": "21601",
            "MEDIA_HYDRATION_CIRCUIT_RETRY_SECONDS": "1801",
            "MEDIA_HYDRATION_MAX_ATTEMPTS": "4",
        },
    )
    http = resolve_http_server_config(
        environ={
            "MCP_TELEGRAM_HTTP_HOST": "0.0.0.0",
            "MCP_TELEGRAM_HTTP_PORT": "3200",
            "MCP_TELEGRAM_HTTP_ALLOW_UNSAFE": "yes",
            "MCP_TELEGRAM_HTTP_ALLOWED_HOSTS": "mcp-telegram:3200, localhost:*",
            "MCP_TELEGRAM_HTTP_ALLOWED_ORIGINS": "http://gateway.local",
        }
    )

    assert scheduling == SchedulingConfig(
        scheduled_reconciliation_seconds=47.5,
        read_position_reconciliation_seconds=45.5,
        read_position_reconciliation_max_dialogs_per_pass=11,
        read_position_reconciliation_failure_cooldown_seconds=46.0,
        read_position_reconciliation_batch_size=16,
        read_position_reconciliation_batch_pause_seconds=1.75,
        reconciliation_hourly_seconds=48.0,
        delta_catch_up_interval_seconds=54.0,
        delta_catch_up_max_probes_per_cycle=8,
        delta_catch_up_probe_pause_seconds=9.0,
        access_probe_interval_seconds=86402.0,
        access_probe_max_dialogs_per_cycle=4,
        access_probe_cooldown_seconds=604802,
        access_probe_pause_seconds=6.0,
        message_fact_refresh_seconds=55.0,
        message_fact_refresh_reaction_max_messages_per_cycle=6,
        message_fact_refresh_read_at_max_messages_per_cycle=7,
        message_fact_refresh_pause_seconds=7.0,
        activity_hot_sweep_seconds=49.0,
        activity_rpc_timeout_seconds=54.0,
        activity_cold_backfill_seconds=50.0,
        activity_cold_backfill_batch_pause_seconds=51.0,
        activity_cold_enroll_seconds=52.0,
        activity_cold_access_retry_seconds=53.0,
        media_hydration=MediaHydrationConfig(
            interval_seconds=47.0,
            max_requests_per_cycle=4,
            max_jobs_per_cycle=301,
            batch_size=99,
            pause_between_requests_seconds=5.5,
            retry_delay_seconds=21601,
            circuit_retry_seconds=1801,
            max_attempts=4,
        ),
        scheduled_flood_sleep_threshold_seconds=0,
    )

    logging = resolve_logging_config({"LOG_LEVEL": "debug", "DAEMON_API_SLOW_REQUEST_SECONDS": "1.5"})
    assert logging.level == "DEBUG"
    assert logging.daemon_api_slow_request_seconds == 1.5
    assert http == HttpServerConfig(
        host="0.0.0.0",
        port=3200,
        allow_unsafe=True,
        allowed_hosts=("mcp-telegram:3200", "localhost:*"),
        allowed_origins=("http://gateway.local",),
    )


def test_runtime_environment_rejects_subsecond_read_position_failure_cooldown() -> None:
    with pytest.raises(ConfigError, match="READ_POSITION_RECONCILIATION_FAILURE_COOLDOWN_SECONDS"):
        resolve_scheduling_config(SchedulingConfig(), {"READ_POSITION_RECONCILIATION_FAILURE_COOLDOWN_SECONDS": "0.5"})


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ('[state]\ndir = "/state"\n\n[freshness.reactions]\nfreshness_ttl_seconds = true\n', "freshness_ttl_seconds"),
        ('[state]\ndir = "/state"\n\n[freshness.read_receipts]\nread_at_ttl_seconds = 0\n', "read_at_ttl_seconds"),
        ('[state]\ndir = "/state"\n\n[freshness.entities]\ndetail_ttl_seconds = "300"\n', "detail_ttl_seconds"),
        (
            '[state]\ndir = "/state"\n\n[scheduling.folder_projection]\nstale_after_seconds = 0\n',
            "stale_after_seconds",
        ),
        (
            '[state]\ndir = "/state"\n\n[scheduling.folder_projection]\nrefresh_interval_seconds = 0.5\n',
            "refresh_interval_seconds",
        ),
        (
            '[state]\ndir = "/state"\n\n[scheduling]\nread_position_reconciliation_failure_cooldown_seconds = 0.5\n',
            "read_position_reconciliation_failure_cooldown_seconds",
        ),
        (
            '[state]\ndir = "/state"\n\n[scheduling]\nscheduled_flood_sleep_threshold_seconds = -1\n',
            "scheduled_flood_sleep_threshold_seconds",
        ),
        (
            '[state]\ndir = "/state"\n\n[scheduling]\ndelta_catch_up_max_probes_per_cycle = true\n',
            "delta_catch_up_max_probes_per_cycle",
        ),
        (
            '[state]\ndir = "/state"\n\n[telegram_rpc]\nmax_calls_per_period = -1\n',
            "max_calls_per_period",
        ),
        (
            '[state]\ndir = "/state"\n\n[logging]\ndaemon_api_slow_request_seconds = 0\n',
            "daemon_api_slow_request_seconds",
        ),
        ('[state]\ndir = "/state"\n\n[freshness]\nunknown = 1\n', "freshness"),
        ('[state]\ndir = "/state"\n\nfreshness = "invalid"\n', "[freshness]"),
        ('[state]\ndir = "/state"\n\n[reactions]\nfreshness_ttl_seconds = 42\n', "root"),
    ],
)
def test_load_config_rejects_invalid_policy(tmp_path: Path, contents: str, expected: str) -> None:
    with pytest.raises(ConfigError, match=expected):
        load_config(_write_config(tmp_path, contents))


def test_load_config_reports_malformed_toml_with_path(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[state\ndir = "/state"\n')

    with pytest.raises(ConfigError, match=f"Could not read config {path}"):
        load_config(path)
