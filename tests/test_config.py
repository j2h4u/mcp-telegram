"""Validation tests for typed operator configuration."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mcp_telegram.config import (
    ConfigError,
    EntitiesConfig,
    FreshnessConfig,
    HttpServerConfig,
    ReactionsConfig,
    ReadReceiptsConfig,
    SchedulingConfig,
    StateConfig,
    TelemetryConfig,
    load_config,
    resolve_http_auth_token,
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

[telemetry]
retention_ttl_seconds = 46

[scheduling]
scheduled_reconciliation_seconds = 47
reconciliation_hourly_seconds = 48
activity_hot_sweep_seconds = 49

[http]
host = "localhost"
port = 3200
""",
    )

    config = load_config(path)
    assert config.freshness.reactions == ReactionsConfig(freshness_ttl_seconds=40)
    assert config.freshness.read_receipts == ReadReceiptsConfig(read_at_ttl_seconds=41)
    assert config.freshness.entities == EntitiesConfig(42, 43, 44, 45)
    assert config.telemetry == TelemetryConfig(retention_ttl_seconds=46)
    assert config.scheduling == SchedulingConfig(47.0, 48.0, 49.0)
    assert config.http == HttpServerConfig(host="localhost", port=3200)


def test_runtime_environment_overrides_are_parsed_by_config_model() -> None:
    scheduling = resolve_scheduling_config(
        SchedulingConfig(),
        {
            "SCHEDULED_RECONCILIATION_SECONDS": "47.5",
            "RECON_HOURLY_SECONDS": "48",
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

    assert scheduling == SchedulingConfig(47.5, 48.0, 49.0, 50.0, 51.0, 52.0, 53.0)
    assert resolve_logging_config({"LOG_LEVEL": "debug"}).level == "DEBUG"
    assert http == HttpServerConfig(
        host="0.0.0.0",
        port=3200,
        allow_unsafe=True,
        allowed_hosts=("mcp-telegram:3200", "localhost:*"),
        allowed_origins=("http://gateway.local",),
    )


def test_http_auth_token_is_required_for_streamable_http() -> None:
    with pytest.raises(ConfigError, match="MCP_TELEGRAM_HTTP_AUTH_TOKEN"):
        resolve_http_auth_token({})

    with pytest.raises(ConfigError, match="MCP_TELEGRAM_HTTP_AUTH_TOKEN"):
        resolve_http_auth_token({"MCP_TELEGRAM_HTTP_AUTH_TOKEN": "   "})


def test_http_auth_token_is_stripped_from_environment() -> None:
    assert resolve_http_auth_token({"MCP_TELEGRAM_HTTP_AUTH_TOKEN": "  local-secret  "}) == "local-secret"


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ('[state]\ndir = "/state"\n\n[freshness.reactions]\nfreshness_ttl_seconds = true\n', "freshness_ttl_seconds"),
        ('[state]\ndir = "/state"\n\n[freshness.read_receipts]\nread_at_ttl_seconds = 0\n', "read_at_ttl_seconds"),
        ('[state]\ndir = "/state"\n\n[freshness.entities]\ndetail_ttl_seconds = "300"\n', "detail_ttl_seconds"),
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
