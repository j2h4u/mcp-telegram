from __future__ import annotations

import resource
import subprocess
import sys
from types import SimpleNamespace
from typing import cast

import pytest

import conftest


@pytest.mark.parametrize(
    ("cov_source", "no_cov", "hard_limit", "expected_soft"),
    [
        (None, False, resource.RLIM_INFINITY, 512 * 1024 * 1024),
        ("src/mcp_telegram", False, resource.RLIM_INFINITY, 1280 * 1024 * 1024),
        ("src/mcp_telegram", True, resource.RLIM_INFINITY, 512 * 1024 * 1024),
        ("src/mcp_telegram", True, 256 * 1024 * 1024, 256 * 1024 * 1024),
        ("src/mcp_telegram", False, 768 * 1024 * 1024, 768 * 1024 * 1024),
    ],
)
def test_pytest_configure_selects_bounded_address_space_budget(
    monkeypatch: pytest.MonkeyPatch,
    cov_source: str | None,
    no_cov: bool,
    hard_limit: int,
    expected_soft: int,
) -> None:
    captured: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(conftest.sys, "platform", "linux")
    monkeypatch.setattr(
        conftest.resource,
        "getrlimit",
        lambda _resource: (resource.RLIM_INFINITY, hard_limit),
    )
    monkeypatch.setattr(
        conftest.resource,
        "setrlimit",
        lambda resource_id, limits: captured.append((resource_id, limits)),
    )
    options = {"cov_source": cov_source, "no_cov": no_cov}
    config = cast(
        pytest.Config,
        SimpleNamespace(getoption=lambda name, default=None: options.get(name, default)),
    )

    conftest.pytest_configure(config)

    assert captured == [(resource.RLIMIT_AS, (expected_soft, hard_limit))]


def test_live_pytest_process_uses_budget_from_coverage_options(pytestconfig: pytest.Config) -> None:
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_AS)
    cov_source = pytestconfig.getoption("cov_source", default=None)
    no_cov = pytestconfig.getoption("no_cov", default=False)
    expected = 1280 * 1024 * 1024 if cov_source and not no_cov else 512 * 1024 * 1024
    if hard_limit != resource.RLIM_INFINITY:
        expected = min(expected, hard_limit)
    assert soft_limit == expected


@pytest.mark.skipif(sys.platform != "linux", reason="uses Linux process memory accounting")
def test_coverage_budget_allows_thread_at_observed_virtual_peak() -> None:
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY and hard < 1280 * 1024 * 1024:
        pytest.skip("inherited hard limit cannot accommodate coverage budget")
    program = r"""
import mmap, resource, sys, threading
cap = int(sys.argv[1]) * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
threading.stack_size(16 * 1024 * 1024)
def status(name):
    return int(next(line.split()[1] for line in open('/proc/self/status') if line.startswith(name + ':'))) * 1024
target = (1024 - 14) * 1024 * 1024
mapping = mmap.mmap(-1, target - status('VmSize'))
assert status('VmRSS') < 64 * 1024 * 1024
try:
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
except RuntimeError as exc:
    assert str(exc) == "can't start new thread"
    print('refused')
else:
    print('started')
"""
    outcomes = []
    for cap in (1024, 1280):
        result = subprocess.run([sys.executable, "-c", program, str(cap)], capture_output=True, text=True, check=True)
        outcomes.append(result.stdout.strip())
    assert outcomes == ["refused", "started"]
