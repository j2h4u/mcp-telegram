from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import cast
from unittest.mock import patch

import pytest
from devtools.runtime_wait import ProbeResult, inspect_container, main, wait_for_healthy


@dataclass
class _Clock:
    now: float = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


def test_wait_continues_past_ten_minutes_until_healthy() -> None:
    clock = _Clock()
    calls: list[float] = []

    def probe(timeout: float) -> ProbeResult:
        calls.append(timeout)
        states = ("starting", "unhealthy", "restarting")
        return ProbeResult("healthy" if clock.now >= 602 else states[len(calls) % len(states)])

    assert wait_for_healthy(probe, clock=clock, sleep=clock.sleep) == ProbeResult("healthy")
    assert clock.now == 602
    assert all(timeout <= 5 for timeout in calls)


def test_expiry_does_not_start_probe_after_deadline_or_mutate_runtime() -> None:
    clock = _Clock()
    calls: list[float] = []

    def probe(timeout: float) -> ProbeResult:
        calls.append(timeout)
        return ProbeResult("unhealthy")

    result = wait_for_healthy(probe, clock=clock, sleep=clock.sleep, observation_seconds=10)

    assert result.status == "observation deadline expired; last status: unhealthy"
    assert clock.now == 10
    assert calls == [5, 5, 5, 4, 2]


def test_clock_crossing_deadline_before_sleep_returns_timeout_without_sleeping() -> None:
    clock_values = iter((0, 0, 899, 901))
    probes: list[float] = []
    sleeps: list[float] = []

    def probe(timeout: float) -> ProbeResult:
        probes.append(timeout)
        return ProbeResult("unhealthy")

    result = wait_for_healthy(
        probe,
        clock=lambda: next(clock_values),
        sleep=sleeps.append,
    )

    assert result == ProbeResult("observation deadline expired; last status: unhealthy")
    assert len(probes) == 1
    assert sleeps == []


def test_probe_budget_uses_remaining_deadline_and_late_health_is_not_success() -> None:
    clock = _Clock()
    calls: list[float] = []

    def late_probe(timeout: float) -> ProbeResult:
        calls.append(timeout)
        clock.now += timeout + 0.01
        return ProbeResult("healthy")

    result = wait_for_healthy(late_probe, clock=clock, observation_seconds=3)

    assert calls == [3]
    assert result.status == "observation deadline expired; last status: healthy"


@pytest.mark.parametrize("status", ["missing", "exited", "dead"])
def test_terminal_container_states_stop_observation(status: str) -> None:
    calls = 0

    def probe(_timeout: float) -> ProbeResult:
        nonlocal calls
        calls += 1
        return ProbeResult(status, terminal=True)

    result = wait_for_healthy(probe, clock=lambda: 0, sleep=lambda _seconds: None)

    assert result == ProbeResult(status, terminal=True)
    assert calls == 1


def test_probe_timeout_and_inspection_failure_never_become_healthy() -> None:
    clock = _Clock()
    results = iter((ProbeResult("inspection timed out"), ProbeResult("inspection failed")))

    result = wait_for_healthy(lambda _timeout: next(results), clock=clock, sleep=clock.sleep, observation_seconds=4)

    assert result.status == "observation deadline expired; last status: inspection failed"


def test_docker_inspection_timeout_is_transient_and_bounded() -> None:
    with patch("devtools.runtime_wait.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 2)) as run:
        assert inspect_container("mcp-telegram", 2) == ProbeResult("inspection timed out")
    assert cast(object, run.call_args.kwargs["timeout"]) == 2


@pytest.mark.parametrize(
    ("state", "health", "expected"),
    [
        ("running", "healthy", ProbeResult("healthy")),
        ("running", "unhealthy", ProbeResult("unhealthy")),
        ("restarting", "unhealthy", ProbeResult("restarting")),
        ("exited", "unhealthy", ProbeResult("container is exited (exit code 1)", terminal=True)),
        ("dead", "unhealthy", ProbeResult("container is dead (exit code 1)", terminal=True)),
    ],
)
def test_docker_inspection_projects_only_readiness_state(state: str, health: str, expected: ProbeResult) -> None:
    payload = json.dumps({"Status": state, "ExitCode": 1, "Health": {"Status": health}})
    completed = subprocess.CompletedProcess(["docker", "inspect"], 0, stdout=payload, stderr="")
    with patch("devtools.runtime_wait.subprocess.run", return_value=completed) as run:
        assert inspect_container("mcp-telegram", 5) == expected
    assert run.call_args.args[0] == [
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        "{{json .State}}",
        "mcp-telegram",
    ]


def test_missing_container_is_a_terminal_diagnostic() -> None:
    completed = subprocess.CompletedProcess(
        ["docker", "inspect"], 1, stdout="", stderr="Error: No such object: mcp-telegram"
    )
    with patch("devtools.runtime_wait.subprocess.run", return_value=completed):
        assert inspect_container("mcp-telegram", 5) == ProbeResult("container 'mcp-telegram' is missing", terminal=True)


@pytest.mark.parametrize("stdout", ["[]", "{}", '{"Status": 42}'])
def test_inspector_rejects_malformed_state(stdout: str) -> None:
    completed = subprocess.CompletedProcess(["docker", "inspect"], 0, stdout=stdout, stderr="")
    with patch("devtools.runtime_wait.subprocess.run", return_value=completed):
        assert inspect_container("mcp-telegram", 5) == ProbeResult("inspection returned invalid container state")


def test_expiry_report_says_runtime_was_left_running_and_can_be_observed_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["runtime_wait.py", "mcp-telegram"])
    monkeypatch.setattr(
        "devtools.runtime_wait.wait_for_healthy", lambda _probe: ProbeResult("observation deadline expired")
    )

    assert main() == 1
    output = capsys.readouterr().err
    assert "not observed within 15 minutes" in output
    assert "runtime was left running" in output
    assert "just runtime-wait" in output
