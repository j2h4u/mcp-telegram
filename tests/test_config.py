"""Validation tests for typed operator configuration."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mcp_telegram.config import ConfigError, ReactionsConfig, StateConfig, load_config


def _write_config(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(contents, encoding="utf-8")
    return path


def test_load_config_uses_frozen_typed_defaults(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[state]\ndir = "/var/lib/mcp-telegram"\n')

    config = load_config(path)

    assert config.state == StateConfig(dir=Path("/var/lib/mcp-telegram"))
    assert config.reactions == ReactionsConfig(freshness_ttl_seconds=600)
    with pytest.raises(FrozenInstanceError):
        config.reactions.freshness_ttl_seconds = 1  # type: ignore[misc]


def test_load_config_reads_reaction_freshness_policy(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        '[state]\ndir = "/var/lib/mcp-telegram"\n\n[reactions]\nfreshness_ttl_seconds = 42\n',
    )

    assert load_config(path).reactions.freshness_ttl_seconds == 42


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ('[state]\ndir = "/state"\n\n[reactions]\nfreshness_ttl_seconds = true\n', "freshness_ttl_seconds"),
        ('[state]\ndir = "/state"\n\n[reactions]\nfreshness_ttl_seconds = 0\n', "freshness_ttl_seconds"),
        ('reactions = "invalid"\n\n[state]\ndir = "/state"\n', "[reactions]"),
    ],
)
def test_load_config_rejects_invalid_reactions_policy(tmp_path: Path, contents: str, expected: str) -> None:
    path = _write_config(tmp_path, contents)

    with pytest.raises(ConfigError, match=expected):
        load_config(path)


def test_load_config_reports_malformed_toml_with_path(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[state\ndir = "/state"\n')

    with pytest.raises(ConfigError, match=f"Could not read config {path}"):
        load_config(path)
