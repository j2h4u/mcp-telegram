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
        (
            (
                "    db: sqlite3.Connection\n    with pytest.raises(sqlite3.OperationalError):\n"
                "E   AttributeError: 'Fake' object has no attribute 'observe'\n"
                "FAILED tests/test_worker.py::test_close - AttributeError: missing observe\n"
            ),
            "unknown_failure",
        ),
        ("sqlite_errorname=SQLITE_IOERR\nFAILED tests/test_x.py::test_x - AttributeError: missing", "unknown_failure"),
        ("sqlite_errorname=None\nE   ValueError: sqlite3.OperationalError", "unknown_failure"),
        ("E   AttributeError: sqlite3.connect\n", "unknown_failure"),
        ("sqlite_errorname=\n", "unknown_failure"),
        ("sqlite_errorname=NOT_SQLITE\n", "unknown_failure"),
        ("E   sqlite3.OperationalError: database is locked\n", "sqlite_application_failure"),
        ("SQLITE_BUSY", "sqlite_application_failure"),
        ("SQLITE_IOERR_SHMMAP", "sqlite_shared_memory_mapping_failure"),
        ("SQLITE_IOERR", "runner_filesystem_io_failure"),
        (
            (
                "E   sqlite3.OperationalError: disk I/O error\n"
                "WARNING sync_transactions error_type=OperationalError sqlite_errorcode=5386 "
                "sqlite_errorname=SQLITE_IOERR_SHMMAP\n"
                "FAILED tests/test_x.py::test_x - sqlite3.OperationalError: disk I/O error"
            ),
            "sqlite_shared_memory_mapping_failure",
        ),
        (
            (
                "WARNING sync_transactions sqlite_errorname=SQLITE_IOERR_SHMMAP\n"
                "FAILED tests/test_x.py::test_x - AttributeError: missing"
            ),
            "unknown_failure",
        ),
        (
            "FAILED tests/test_x.py::test_x - ImportError: failed to map segment from shared object",
            "native_extension_load_failure",
        ),
        ("sqlite3.OperationalError: disk I/O error", "runner_filesystem_io_failure"),
        ("E   AttributeError: missing\nE   sqlite3.OperationalError: database is locked", "unknown_failure"),
        (
            (
                "FAILED tests/test_x.py::test_a - AttributeError: missing\n"
                "FAILED tests/test_x.py::test_b - sqlite3.OperationalError: database is locked"
            ),
            "unknown_failure",
        ),
        ("sqlite3.Connection\n    sqlite3.connect(path)\n", "unknown_failure"),
    ],
)
def test_sqlite_classification_uses_failure_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log_text: str, expected: str
) -> None:
    _healthy_capacity(monkeypatch)
    assert ci_pytest_runner.classify_failure(log_text, tmp_path) == expected


@pytest.mark.parametrize(
    ("log_text", "expected"),
    [
        ("sqlite3.Connection\nFAILED tests/test_x.py::test_x - AttributeError: missing", "unknown_failure"),
        ("E   sqlite3.OperationalError: database is locked", "sqlite_application_failure"),
        (
            (
                "E   sqlite3.OperationalError: disk I/O error\n"
                "WARNING sync_transactions error_type=OperationalError sqlite_errorcode=5386 "
                "sqlite_errorname=SQLITE_IOERR_SHMMAP\n"
                "FAILED tests/test_x.py::test_x - sqlite3.OperationalError: disk I/O error"
            ),
            "sqlite_shared_memory_mapping_failure",
        ),
    ],
)
def test_runner_preserves_nonretryable_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], log_text: str, expected: str
) -> None:
    _healthy_capacity(monkeypatch)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    calls: list[int] = []

    def run_once(_command: list[str], _work_dir: Path, attempt: int) -> tuple[int, str]:
        calls.append(attempt)
        return 7, log_text

    monkeypatch.setattr(ci_pytest_runner, "_run_once", run_once)
    monkeypatch.setattr(ci_pytest_runner, "report_filesystem_state", lambda: None)
    assert ci_pytest_runner.run_with_diagnostics(["pytest"]) == 7
    assert calls == [1]
    assert f"ci_failure_outcome={expected}" in capsys.readouterr().out


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
