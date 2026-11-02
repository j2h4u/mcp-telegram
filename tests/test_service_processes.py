import asyncio
import signal
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from mcp_telegram import service_processes


async def _check_worker_exit_stops_sibling(monkeypatch: pytest.MonkeyPatch, exiting_worker: str) -> None:
    spawn = asyncio.create_subprocess_exec
    workers: list[asyncio.subprocess.Process] = []

    async def start(executable: str, *args: str, start_new_session: bool) -> asyncio.subprocess.Process:
        code = "import time; time.sleep(0.05)" if args[0] == exiting_worker else "import time; time.sleep(60)"
        worker = await spawn(sys.executable, "-c", code, start_new_session=start_new_session)
        workers.append(worker)
        return worker

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    assert await service_processes.run_service(host="127.0.0.1", port=3100) == 1
    assert len(workers) == 2
    assert all(worker.returncode is not None for worker in workers)


async def _check_partial_start_failure_stops_first_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = asyncio.create_subprocess_exec
    workers: list[asyncio.subprocess.Process] = []

    async def start(executable: str, *args: str, start_new_session: bool) -> asyncio.subprocess.Process:
        if workers:
            raise OSError("second worker cannot start")
        worker = await spawn(sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True)
        workers.append(worker)
        return worker

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    with pytest.raises(OSError, match="second worker"):
        await service_processes.run_service(host="127.0.0.1", port=3100)
    assert workers[0].returncode is not None


async def _check_operator_stop_drains_http_before_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = asyncio.create_subprocess_exec
    workers: list[asyncio.subprocess.Process] = []
    signals: list[int] = []
    loop = asyncio.get_running_loop()
    callbacks: dict[signal.Signals, object] = {}
    send_signal = service_processes._signal_worker

    def observe_signal(worker: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        if worker.returncode is None:
            signals.append(worker.pid)
        send_signal(worker, sig)

    async def start(executable: str, *args: str, start_new_session: bool) -> asyncio.subprocess.Process:
        worker = await spawn(sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True)
        workers.append(worker)
        if len(workers) == 2:
            callback = callbacks[signal.SIGTERM]
            assert callable(callback)
            loop.call_soon(callback)
        return worker

    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: callbacks.update({sig: callback}))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(service_processes, "_signal_worker", observe_signal)
    assert await service_processes.run_service(host="127.0.0.1", port=3100) == 0
    assert signals[:2] == [workers[1].pid, workers[0].pid]
    assert all(worker.returncode is not None for worker in workers)


async def _check_slow_worker_is_allowed_to_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    async def expired_wait(_workers: object, _timeout: float) -> None:
        return None

    monkeypatch.setattr(service_processes, "_wait_workers", expired_wait)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "result.sqlite"
        code = (
            "import signal,sqlite3,sys,time\n"
            "def stop(*_):\n"
            " time.sleep(.15)\n"
            " conn=sqlite3.connect(sys.argv[1]); conn.execute('create table receipt (value text)'); "
            "conn.execute(\"insert into receipt values ('committed')\"); conn.commit(); conn.close(); exit(0)\n"
            "signal.signal(signal.SIGTERM, stop); print('ready', flush=True); time.sleep(60)"
        )
        worker = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, str(path), stdout=asyncio.subprocess.PIPE
        )
        assert worker.stdout is not None
        await worker.stdout.readline()
        stop = asyncio.create_task(service_processes._stop_workers([worker], orderly=False, http_drain_seconds=0.01))
        await asyncio.sleep(0.05)
        assert not stop.done()
        await stop
        assert worker.returncode == 0
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("select value from receipt").fetchone() == ("committed",)


async def _run_scenario(scenario: str) -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        if scenario in ("sync", "http"):
            await _check_worker_exit_stops_sibling(monkeypatch, scenario)
        elif scenario == "partial":
            await _check_partial_start_failure_stops_first_worker(monkeypatch)
        elif scenario == "stop":
            await _check_operator_stop_drains_http_before_daemon(monkeypatch)
        else:
            await _check_slow_worker_is_allowed_to_finish(monkeypatch)


@pytest.mark.parametrize("scenario", ["sync", "http", "partial", "stop", "kill"])
def test_coordinator_lifecycle_in_fresh_process(scenario: str) -> None:
    # The full suite has a 512 MiB address-space ceiling. A fresh interpreter
    # leaves room for asyncio's child-watcher threads without raising that limit.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import asyncio, sys; from test_service_processes import _run_scenario; "
                "asyncio.run(_run_scenario(sys.argv[1]))"
            ),
            scenario,
        ],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
