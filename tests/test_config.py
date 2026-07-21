"""Validation tests for typed operator configuration."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mcp_telegram.config import (
    ConfigError,
    EntitiesConfig,
    FreshnessConfig,
    ReactionsConfig,
    ReadReceiptsConfig,
    StateConfig,
    TelemetryConfig,
    load_config,
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
""",
    )

    config = load_config(path)
    assert config.freshness.reactions == ReactionsConfig(freshness_ttl_seconds=40)
    assert config.freshness.read_receipts == ReadReceiptsConfig(read_at_ttl_seconds=41)
    assert config.freshness.entities == EntitiesConfig(42, 43, 44, 45)
    assert config.telemetry == TelemetryConfig(retention_ttl_seconds=46)


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
