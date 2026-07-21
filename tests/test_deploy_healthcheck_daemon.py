from __future__ import annotations

from pathlib import Path

import pytest
from deploy.scripts import healthcheck_daemon


def test_daemon_healthcheck_loads_socket_path_from_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "xdg-config" / "mcp-telegram"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(f'[state]\ndir = "{state_dir}"\n', encoding="utf-8")

    assert healthcheck_daemon._load_socket_path() == state_dir / "daemon.sock"
