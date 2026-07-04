from __future__ import annotations

import stat
from pathlib import Path

import pytest

from deploy.telegram_qr_login import _ensure_private_state_dir, _protect_session_file


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_qr_login_tightens_existing_state_directory(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o755)

    _ensure_private_state_dir(state_dir)

    assert _mode(state_dir) == 0o700


def test_qr_login_protects_existing_session_file(tmp_path: Path) -> None:
    session_base = tmp_path / "mcp_telegram_session"
    session_file = session_base.with_suffix(".session")
    session_file.write_text("sqlite placeholder", encoding="utf-8")
    session_file.chmod(0o644)

    _protect_session_file(session_base)

    assert _mode(session_file) == 0o600


def test_qr_login_rejects_state_file(tmp_path: Path) -> None:
    state_path = tmp_path / "state"
    state_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _ensure_private_state_dir(state_path)
