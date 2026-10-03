"""Lifecycle of the two fixed workers in the single-container service."""

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

logger = logging.getLogger(__name__)
SHUTDOWN_SECONDS = 40.0
HTTP_DRAIN_SECONDS = 30.0
KILL_REAP_SECONDS = 1.0
WORKER_COUNT = 2


def _signal_worker(worker: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if worker.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            worker.send_signal(sig)


async def _wait_workers(workers: list[asyncio.subprocess.Process], timeout: float) -> None:
    tasks = [asyncio.create_task(worker.wait()) for worker in workers]
    try:
        if tasks:
            await asyncio.wait(tasks, timeout=max(0.0, timeout))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _stop_workers(
    workers: list[asyncio.subprocess.Process], *, orderly: bool, shutdown_seconds: float, http_drain_seconds: float
) -> None:
    deadline = asyncio.get_running_loop().time() + shutdown_seconds
    if orderly and len(workers) == WORKER_COUNT:
        _signal_worker(workers[1], signal.SIGTERM)
        await _wait_workers([workers[1]], min(http_drain_seconds, shutdown_seconds))
    for worker in workers:
        _signal_worker(worker, signal.SIGTERM)
    await _wait_workers(workers, deadline - asyncio.get_running_loop().time())
    for worker in workers:
        _signal_worker(worker, signal.SIGKILL)
    # A worker in kernel disk sleep may not exit even after SIGKILL. Never
    # respawn it here or wait indefinitely; Docker owns the whole-container restart.
    await _wait_workers(workers, KILL_REAP_SECONDS)


async def run_service(
    *, host: str, port: int, shutdown_seconds: float = SHUTDOWN_SECONDS, http_drain_seconds: float = HTTP_DRAIN_SECONDS
) -> int:
    """Start once, stop both on a worker exit, and leave restart to Docker."""
    executable = Path(sys.executable).with_name("mcp-telegram")
    workers: list[asyncio.subprocess.Process] = []
    waits: list[asyncio.Task[int]] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    stop_task = asyncio.create_task(stop.wait())
    orderly = False
    try:
        for args in (("sync",), ("http", "--host", host, "--port", str(port))):
            worker = await asyncio.create_subprocess_exec(str(executable), *args, start_new_session=True)
            workers.append(worker)
            waits.append(asyncio.create_task(worker.wait()))
        done, _ = await asyncio.wait([*waits, stop_task], return_when=asyncio.FIRST_COMPLETED)
        orderly = stop_task in done
        if orderly:
            return 0
        for index, task in enumerate(waits):
            if task in done:
                logger.error("Service worker %s exited with status %s; stopping service", index, task.result())
        return 1
    finally:
        await _stop_workers(
            workers, orderly=orderly, shutdown_seconds=shutdown_seconds, http_drain_seconds=http_drain_seconds
        )
        for task in [*waits, stop_task]:
            task.cancel()
        await asyncio.gather(*waits, stop_task, return_exceptions=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
