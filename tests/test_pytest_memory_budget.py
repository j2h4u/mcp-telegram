from __future__ import annotations

import resource
from types import SimpleNamespace
from typing import cast

import pytest

import conftest


@pytest.mark.parametrize(
    ("cov_source", "no_cov", "hard_limit", "expected_soft"),
    [
        (None, False, resource.RLIM_INFINITY, 512 * 1024 * 1024),
        ("src/mcp_telegram", False, resource.RLIM_INFINITY, 1024 * 1024 * 1024),
        ("src/mcp_telegram", True, resource.RLIM_INFINITY, 512 * 1024 * 1024),
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
    expected = 1024 * 1024 * 1024 if cov_source and not no_cov else 512 * 1024 * 1024
    if hard_limit != resource.RLIM_INFINITY:
        expected = min(expected, hard_limit)
    assert soft_limit == expected
