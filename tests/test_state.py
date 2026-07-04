from __future__ import annotations

import stat
from pathlib import Path

import pytest

from mcp_telegram.state import ensure_private_state_dir


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_ensure_private_state_dir_tightens_existing_directory(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o755)

    ensure_private_state_dir(state_dir)

    assert _mode(state_dir) == 0o700


def test_ensure_private_state_dir_rejects_file(tmp_path: Path) -> None:
    state_path = tmp_path / "state"
    state_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        ensure_private_state_dir(state_path)
