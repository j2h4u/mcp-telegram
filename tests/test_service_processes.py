import asyncio
import signal
import sys

import pytest

from mcp_telegram import service_processes


@pytest.mark.parametrize("exiting_worker", ["sync", "http"])
@pytest.mark.asyncio
async def test_worker_exit_stops_sibling(monkeypatch: pytest.MonkeyPatch, exiting_worker: str) -> None:
    spawn = asyncio.create_subprocess_exec
    workers: list[asyncio.subprocess.Process] = []

    async def start(executable: str, *args: str, start_new_session: bool) -> asyncio.subprocess.Process:
        code = "import time; time.sleep(0.05)" if args[0] == exiting_worker else "import time; time.sleep(60)"
        worker = await spawn(sys.executable, "-c", code, start_new_session=start_new_session)
        workers.append(worker)
        return worker

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    assert await service_processes.run_service(host="127.0.0.1", port=3100, shutdown_seconds=0.1) == 1
    assert len(workers) == 2
    assert all(worker.returncode is not None for worker in workers)


@pytest.mark.asyncio
async def test_partial_start_failure_stops_first_worker(monkeypatch: pytest.MonkeyPatch) -> None:
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
        await service_processes.run_service(host="127.0.0.1", port=3100, shutdown_seconds=0.1)
    assert workers[0].returncode is not None


@pytest.mark.asyncio
async def test_operator_stop_drains_http_before_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert await service_processes.run_service(host="127.0.0.1", port=3100, shutdown_seconds=0.2) == 0
    assert signals[:2] == [workers[1].pid, workers[0].pid]
    assert all(worker.returncode is not None for worker in workers)


@pytest.mark.asyncio
async def test_stopped_worker_is_killed_after_grace() -> None:
    worker = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
    worker.send_signal(signal.SIGSTOP)
    try:
        await service_processes._stop_workers([worker], orderly=False, shutdown_seconds=0.03, http_drain_seconds=0.01)
        assert worker.returncode == -signal.SIGKILL
    finally:
        if worker.returncode is None:
            worker.kill()
        await worker.wait()
