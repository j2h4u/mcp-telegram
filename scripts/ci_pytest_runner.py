"""Run CI pytest gates with filesystem diagnostics and bounded retries."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO, cast

_CAPACITY_MARKERS = ("no space left on device", "disk quota exceeded", "errno 28")
_SQLITE_IO_MARKERS = ("sqlite_errorname=SQLITE_IOERR", "sqlite3.operationalerror: disk i/o error")
_SQLITE_APPLICATION_MARKERS = (
    "sqlite_error",
    "sqlite3.",
    "database is locked",
    "cannot start a transaction",
    "cannot commit",
)
_MIN_FREE_BYTES = 512 * 1024 * 1024
_MIN_FREE_INODES = 4_096
_MAX_ATTEMPTS = 2


def _runner_temp() -> Path:
    return Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()


def _run_diagnostic_command(args: list[str]) -> None:
    print(f"$ {' '.join(args)}", flush=True)
    subprocess.run(args, check=False)


def report_filesystem_state() -> None:
    """Print capacity, inode, mount, filesystem, and temporary-directory evidence."""
    runner_temp = _runner_temp()
    print(f"runner_temp={runner_temp}", flush=True)
    _run_diagnostic_command(["df", "-hT", str(runner_temp), "/tmp", "."])
    _run_diagnostic_command(["df", "-iT", str(runner_temp), "/tmp", "."])
    _run_diagnostic_command(["findmnt", "-T", str(runner_temp), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    _run_diagnostic_command(["stat", "-f", str(runner_temp), "/tmp", "."])
    _run_diagnostic_command(["du", "-sh", str(runner_temp), "/tmp"])


def classify_failure(log_text: str, runner_temp: Path) -> str:
    """Classify SQLite-related CI failures using log and live capacity evidence."""
    lowered = log_text.lower()
    usage = shutil.disk_usage(runner_temp)
    stat = os.statvfs(runner_temp)
    if (
        any(marker in lowered for marker in _CAPACITY_MARKERS)
        or usage.free < _MIN_FREE_BYTES
        or stat.f_favail < _MIN_FREE_INODES
    ):
        return "runner_capacity_exhausted"
    if any(marker.lower() in lowered for marker in _SQLITE_IO_MARKERS):
        return "runner_filesystem_io_failure"
    if any(marker in lowered for marker in _SQLITE_APPLICATION_MARKERS):
        return "sqlite_application_failure"
    return "sqlite_application_failure"


def _run_once(command: list[str], work_dir: Path, attempt: int) -> tuple[int, str]:
    attempt_dir = work_dir / f"a{attempt}"
    pytest_temp = attempt_dir / "p"
    process_temp = attempt_dir / "t"
    pytest_temp.mkdir(parents=True)
    process_temp.mkdir()
    log_path = attempt_dir / "pytest.log"
    env = os.environ.copy()
    env["TMPDIR"] = str(process_temp)
    env["PYTEST_ADDOPTS"] = f"{env.get('PYTEST_ADDOPTS', '')} --basetemp={pytest_temp}".strip()
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        stdout = cast(TextIO, process.stdout)
        for line in stdout:
            sys.stdout.write(line)
            log_file.write(line)
        return_code = process.wait()
    return return_code, log_path.read_text(encoding="utf-8", errors="replace")


def run_with_diagnostics(command: list[str]) -> int:
    """Run a pytest command and retry once only for a diagnosed runner failure."""
    runner_temp = _runner_temp()
    job_name = os.environ.get("GITHUB_JOB", "local")
    job_key = "u" if job_name == "unit" else "c" if job_name == "crap" else "l"
    work_dir = runner_temp / "pt" / job_key
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        return_code, log_text = _run_once(command, work_dir, attempt)
        if return_code == 0:
            return 0
        print(f"pytest_attempt={attempt} exit_code={return_code}", flush=True)
        report_filesystem_state()
        outcome = classify_failure(log_text, runner_temp)
        print(f"ci_failure_outcome={outcome}", flush=True)
        if attempt == _MAX_ATTEMPTS or outcome == "sqlite_application_failure":
            return return_code
        print(f"retrying_after={outcome}", flush=True)
        shutil.rmtree(work_dir / f"a{attempt}")
    return 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("diagnose")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    action = cast(str, args.action)
    if action == "diagnose":
        report_filesystem_state()
        return 0
    command = list(cast(list[str], args.command))
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("run requires a command after --")
    return run_with_diagnostics(command)


if __name__ == "__main__":
    raise SystemExit(main())
