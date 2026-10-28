"""Wait for deployment readiness without changing the running container."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

OBSERVATION_SECONDS = 900.0
POLL_SECONDS = 2.0
PROBE_TIMEOUT_SECONDS = 5.0
CLI_ARGUMENT_COUNT = 2


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: str
    terminal: bool = False


def wait_for_healthy(
    probe: Callable[[float], ProbeResult],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    observation_seconds: float = OBSERVATION_SECONDS,
) -> ProbeResult:
    """Observe readiness until healthy, terminal, or the monotonic budget expires."""
    deadline = clock() + observation_seconds
    latest = ProbeResult("inspection not started")
    while (remaining := deadline - clock()) > 0:
        latest = probe(min(PROBE_TIMEOUT_SECONDS, remaining))
        if clock() >= deadline:
            return ProbeResult(f"observation deadline expired; last status: {latest.status}")
        if latest.terminal or latest.status == "healthy":
            return latest
        sleep_remaining = deadline - clock()
        if sleep_remaining <= 0:
            break
        sleep(min(POLL_SECONDS, sleep_remaining))
    return ProbeResult(f"observation deadline expired; last status: {latest.status}")


def inspect_container(container: str, timeout: float) -> ProbeResult:  # noqa: PLR0911, PLR0912 - inspect states stay explicit
    """Read Docker state with a bounded inspect call; never mutate the container."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--type", "container", "--format", "{{json .State}}", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult("inspection timed out")
    except OSError as exc:
        return ProbeResult(f"inspection failed: {exc}")

    if result.returncode != 0:
        if "no such object" in result.stderr.lower() or "no such container" in result.stderr.lower():
            return ProbeResult(f"container {container!r} is missing", terminal=True)
        return ProbeResult(f"inspection failed: {result.stderr.strip() or result.returncode}")

    try:
        records = cast(object, json.loads(result.stdout))
    except json.JSONDecodeError:
        return ProbeResult("inspection returned invalid container state")
    if not isinstance(records, dict):
        return ProbeResult("inspection returned invalid container state")
    container_state = records.get("Status")
    if not isinstance(container_state, str):
        return ProbeResult("inspection returned invalid container state")

    if container_state in {"exited", "dead"}:
        exit_code = records.get("ExitCode")
        detail = f" (exit code {exit_code})" if isinstance(exit_code, int) else ""
        return ProbeResult(f"container is {container_state}{detail}", terminal=True)
    if container_state == "restarting":
        return ProbeResult("restarting")
    if container_state != "running":
        return ProbeResult(container_state)
    health = records.get("Health")
    if not isinstance(health, dict):
        return ProbeResult(f"container state is {container_state}; health status unavailable")
    health_status = health.get("Status")
    if not isinstance(health_status, str):
        return ProbeResult(f"container state is {container_state}; health status unavailable")
    return ProbeResult(health_status)


def main() -> int:
    if len(sys.argv) != CLI_ARGUMENT_COUNT:
        print("Usage: runtime_wait.py CONTAINER", file=sys.stderr)
        return 2
    container = sys.argv[1]
    result = wait_for_healthy(lambda timeout: inspect_container(container, timeout))
    if result.status == "healthy":
        print("healthy")
        return 0
    if result.terminal:
        print(f"Runtime readiness failed: {result.status}", file=sys.stderr)
        return 1
    _, _, last_status = result.status.partition("; last status: ")
    last_status = last_status or result.status
    print(
        "Readiness was not observed within 15 minutes; runtime was left running. "
        f"Last status: {last_status}. Run `just runtime-wait` to continue observing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
