"""Tests for the shared maintenance-cycle log-level policy."""

import logging

import pytest

from mcp_telegram.maintenance_logging import log_maintenance_cycle


def test_productive_cycle_is_operator_visible(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("tests.maintenance")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        log_maintenance_cycle(logger, True, "cycle complete count=%d", 1)

    assert caplog.records[-1].levelno == logging.INFO
    assert caplog.records[-1].message == "cycle complete count=1"


def test_noop_cycle_is_debug_only(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("tests.maintenance")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        log_maintenance_cycle(logger, False, "cycle complete count=%d", 0)

    assert caplog.records[-1].levelno == logging.DEBUG
    assert caplog.records[-1].message == "cycle complete count=0"
