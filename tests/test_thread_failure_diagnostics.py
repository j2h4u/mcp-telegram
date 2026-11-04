"""Failure diagnostics remain bounded and never replace the original exception."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scripts import ci_pytest_runner
from tests import conftest
from tests import thread_failure_diagnostics as diag
from tests.test_ci_pytest_runner import _healthy_capacity


def _invoke(error: BaseException) -> None:
    node = cast(pytest.Item, SimpleNamespace(nodeid="tests/test_example.py::test_example"))
    call = cast(pytest.CallInfo[object], SimpleNamespace(excinfo=SimpleNamespace(value=error)))
    report = cast(pytest.TestReport, SimpleNamespace(when="call"))
    conftest.pytest_exception_interact(node, call, report)
    assert call.excinfo is not None and call.excinfo.value is error


@pytest.mark.parametrize("chained", [False, True])
def test_thread_failure_hook_emits_one_structured_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], chained: bool
) -> None:
    monkeypatch.setattr(diag.resource, "getrlimit", lambda _kind: (123, 456))
    records = {
        "/proc/self/status": "Threads:\t3\nVmRSS:\t20 kB\nVmSize:\t40 kB\n",
        "/proc/meminfo": "MemAvailable: 100 kB\nCommitLimit: 200 kB\nCommitted_AS: 50 kB\n",
        "/proc/self/cgroup": "0::/tenant/worker\n",
        "/proc/self/mountinfo": "5 1 0:8 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
        "/sys/fs/cgroup/worker/pids.current": "3\n",
        "/sys/fs/cgroup/worker/pids.max": "100\n",
        "/sys/fs/cgroup/worker/memory.current": "1024\n",
    }
    monkeypatch.setattr(diag, "_read", lambda path: records.get(str(path)))
    error = RuntimeError("can't start new thread")
    if chained:
        outer = ValueError("outer")
        outer.__cause__ = error
        error = outer  # type: ignore[assignment]
    _invoke(error)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("native_resource_diagnostic ")
    body = cast(dict[str, object], json.loads(lines[0].split(" ", 1)[1]))
    limits = cast(dict[str, object], body["limits"])
    process = cast(dict[str, object], body["process"])
    memory = cast(dict[str, object], body["memory"])
    cgroup = cast(dict[str, object], body["cgroup_v2"])
    assert body["snapshot"] == "post_exception_unwind" and body["phase"] == "call"
    assert limits["RLIMIT_AS"] == [123, 456]
    assert process["Threads"] == "3" and process["VmPeak"] == "unavailable"
    assert memory["MemAvailable"] == "100 kB"
    assert cgroup["path"] == "/sys/fs/cgroup/worker"
    assert cgroup["pids.current"] == "3" and cgroup["memory.max"] == "unavailable"
    assert len(cast(list[object], body["threads"])) <= 32


def test_unrelated_failure_and_missing_files_are_handled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _invoke(RuntimeError("some other error"))
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(diag, "_read", lambda _path: None)
    snapshot = diag.collect_thread_resources("node", "setup")
    assert snapshot["process"] == dict.fromkeys(diag._FIELDS, "unavailable")
    assert snapshot["cgroup_v2"]["path"] == "unavailable"  # type: ignore[index]


def test_context_cycle_and_thread_list_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    outer = ValueError("outer")
    failure = RuntimeError("can't start new thread")
    outer.__context__ = failure
    failure.__context__ = outer
    assert diag.is_thread_start_failure(outer)
    monkeypatch.setattr(diag.threading, "enumerate", lambda: [SimpleNamespace(name="worker", native_id=1)] * 40)
    snapshot = diag.collect_thread_resources("node", "call")
    assert snapshot["python_thread_count"] == 40 and snapshot["threads_truncated"] is True
    assert len(cast(list[object], snapshot["threads"])) == 32


@pytest.mark.parametrize("where", ["collector", "emitter"])
def test_diagnostic_failure_never_replaces_original(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise MemoryError("diagnostic failure")

    if where == "collector":
        monkeypatch.setattr(conftest, "collect_thread_resources", fail)
    else:
        monkeypatch.setattr(conftest, "print", fail, raising=False)
    error = RuntimeError("can't start new thread")
    error.__cause__ = sqlite3.OperationalError("fixture")
    _invoke(error)


def test_existing_sqlite_diagnostic_stays_available(capsys: pytest.CaptureFixture[str]) -> None:
    _invoke(sqlite3.OperationalError("fixture"))
    assert "sqlite_diagnostic " in capsys.readouterr().out


def test_native_thread_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _healthy_capacity(monkeypatch)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    calls: list[int] = []

    def fail_once(_command: list[str], _work_dir: Path, attempt: int) -> tuple[int, str]:
        calls.append(attempt)
        return 1, "FAILED tests/test_example.py::test_example - RuntimeError: can't start new thread"

    monkeypatch.setattr(ci_pytest_runner, "_run_once", fail_once)
    monkeypatch.setattr(ci_pytest_runner, "report_filesystem_state", lambda: None)
    assert ci_pytest_runner.run_with_diagnostics(["pytest"]) == 1
    assert calls == [1]
