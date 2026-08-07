"""Validation tests for typed operator configuration."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mcp_telegram.config import (
    ConfigError,
    EntitiesConfig,
    FloodWaitConfig,
    FoldersConfig,
    FreshnessConfig,
    HttpServerConfig,
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
    assert config.http == HttpServerConfig()
    with pytest.raises(FrozenInstanceError):
        config.freshness.reactions.freshness_ttl_seconds = 1  # type: ignore[misc]


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

[freshness.folders]
snapshot_ttl_seconds = 46

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
scheduled_flood_sleep_threshold_seconds = 0
reconciliation_hourly_seconds = 49
delta_catch_up_interval_seconds = 50
delta_catch_up_max_probes_per_cycle = 7
delta_catch_up_probe_pause_seconds = 3
message_fact_refresh_seconds = 52
message_fact_refresh_reaction_max_messages_per_cycle = 6
message_fact_refresh_read_at_max_messages_per_cycle = 7
message_fact_refresh_pause_seconds = 4
activity_hot_sweep_seconds = 51

[http]
host = "localhost"
port = 3200
""",
    )

    config = load_config(path)
    assert config.freshness.reactions == ReactionsConfig(freshness_ttl_seconds=40)
    assert config.freshness.read_receipts == ReadReceiptsConfig(read_at_ttl_seconds=41)
    assert config.freshness.entities == EntitiesConfig(42, 43, 44, 45)
    assert config.freshness.folders == FoldersConfig(snapshot_ttl_seconds=46)
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
        reconciliation_hourly_seconds=49.0,
        delta_catch_up_interval_seconds=50.0,
        delta_catch_up_max_probes_per_cycle=7,
        delta_catch_up_probe_pause_seconds=3.0,
        message_fact_refresh_seconds=52.0,
        message_fact_refresh_reaction_max_messages_per_cycle=6,
        message_fact_refresh_read_at_max_messages_per_cycle=7,
        message_fact_refresh_pause_seconds=4.0,
        activity_hot_sweep_seconds=51.0,
        scheduled_flood_sleep_threshold_seconds=0,
    )
    assert config.http == HttpServerConfig(host="localhost", port=3200)


def test_runtime_environment_overrides_are_parsed_by_config_model() -> None:
    scheduling = resolve_scheduling_config(
        SchedulingConfig(),
        {
            "SCHEDULED_RECONCILIATION_SECONDS": "47.5",
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
            "ACTIVITY_COLD_BACKFILL_SECONDS": "50",
            "ACTIVITY_COLD_BACKFILL_BATCH_PAUSE": "51",
            "ACTIVITY_COLD_ENROLL_SECONDS": "52",
            "ACTIVITY_COLD_ACCESS_RETRY_SECONDS": "53",
            "LOG_LEVEL": "debug",
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
        reconciliation_hourly_seconds=48.0,
        delta_catch_up_interval_seconds=54.0,
        delta_catch_up_max_probes_per_cycle=8,
        delta_catch_up_probe_pause_seconds=9.0,
        message_fact_refresh_seconds=55.0,
        message_fact_refresh_reaction_max_messages_per_cycle=6,
        message_fact_refresh_read_at_max_messages_per_cycle=7,
        message_fact_refresh_pause_seconds=7.0,
        activity_hot_sweep_seconds=49.0,
        activity_cold_backfill_seconds=50.0,
        activity_cold_backfill_batch_pause_seconds=51.0,
        activity_cold_enroll_seconds=52.0,
        activity_cold_access_retry_seconds=53.0,
        scheduled_flood_sleep_threshold_seconds=0,
    )
    assert resolve_logging_config({"LOG_LEVEL": "debug"}).level == "DEBUG"
    assert http == HttpServerConfig(
        host="0.0.0.0",
        port=3200,
        allow_unsafe=True,
        allowed_hosts=("mcp-telegram:3200", "localhost:*"),
        allowed_origins=("http://gateway.local",),
    )


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ('[state]\ndir = "/state"\n\n[freshness.reactions]\nfreshness_ttl_seconds = true\n', "freshness_ttl_seconds"),
        ('[state]\ndir = "/state"\n\n[freshness.read_receipts]\nread_at_ttl_seconds = 0\n', "read_at_ttl_seconds"),
        ('[state]\ndir = "/state"\n\n[freshness.entities]\ndetail_ttl_seconds = "300"\n', "detail_ttl_seconds"),
        ('[state]\ndir = "/state"\n\n[freshness.folders]\nsnapshot_ttl_seconds = 0\n', "snapshot_ttl_seconds"),
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
