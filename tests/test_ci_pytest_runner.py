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


def test_sqlite_shmmap_is_not_classified_as_retryable_runner_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _healthy_capacity(monkeypatch)
    outcome = ci_pytest_runner.classify_failure("sqlite_errorname=SQLITE_IOERR_SHMMAP", tmp_path)
    assert outcome == "sqlite_shared_memory_mapping_failure"
    assert outcome not in ci_pytest_runner._RETRYABLE_OUTCOMES


@pytest.mark.parametrize(
    ("log_text", "expected"),
    [
        ("ImportError: failed to map segment from shared object", "native_extension_load_failure"),
        ("RuntimeError: unexpected test runner failure", "unknown_failure"),
    ],
)
def test_unrecognized_runner_failures_are_not_mislabeled_or_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    log_text: str,
    expected: str,
) -> None:
    _healthy_capacity(monkeypatch)
    outcome = ci_pytest_runner.classify_failure(log_text, tmp_path)
    assert outcome == expected
    assert outcome not in ci_pytest_runner._RETRYABLE_OUTCOMES
    assert {
        "runner_capacity_exhausted",
        "runner_filesystem_io_failure",
    } == ci_pytest_runner._RETRYABLE_OUTCOMES
