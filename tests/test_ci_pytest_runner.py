from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_pytest_runner


def _healthy_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ci_pytest_runner.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10**12, used=10**9, free=10**12 - 10**9),
    )
    monkeypatch.setattr(ci_pytest_runner.os, "statvfs", lambda _path: SimpleNamespace(f_favail=1_000_000))


def test_capacity_failure_takes_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _healthy_capacity(monkeypatch)
    assert ci_pytest_runner.classify_failure("OSError: [Errno 28] No space left on device", tmp_path) == (
        "runner_capacity_exhausted"
    )


def test_sqlite_ioerr_on_healthy_filesystem_is_runner_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _healthy_capacity(monkeypatch)
    assert ci_pytest_runner.classify_failure("sqlite_errorname=SQLITE_IOERR", tmp_path) == (
        "runner_filesystem_io_failure"
    )


def test_sqlite_lock_is_application_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _healthy_capacity(monkeypatch)
    assert ci_pytest_runner.classify_failure("sqlite3.OperationalError: database is locked", tmp_path) == (
        "sqlite_application_failure"
    )
