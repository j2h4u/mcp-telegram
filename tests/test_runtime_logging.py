from __future__ import annotations

import logging

import pytest

from mcp_telegram.runtime_logging import _TelethonRoutineDifferenceFilter, install_telethon_log_filter


def _record(*, message: str, level: int = logging.INFO, name: str = "telethon.client.updates") -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, (), None)


@pytest.mark.parametrize(
    "message",
    (
        "Got difference for account updates",
        "Got difference for channel 123 updates",
        "Got difference for channel -100123 updates",
    ),
)
def test_telethon_routine_difference_filter_suppresses_exact_success(message: str) -> None:
    assert _TelethonRoutineDifferenceFilter().filter(_record(message=message)) is False


@pytest.mark.parametrize(
    "record",
    (
        _record(message="Got difference for account updates: network problem"),
        _record(message="Got difference for channel 123 updates (retrying)"),
        _record(message="Got difference for account updates", level=logging.WARNING),
        _record(message="Got difference for channel 123 updates", level=logging.ERROR),
        _record(message="Got difference for account updates", name="other.logger"),
    ),
)
def test_telethon_routine_difference_filter_preserves_other_signals(record: logging.LogRecord) -> None:
    assert _TelethonRoutineDifferenceFilter().filter(record) is True


def test_telethon_log_filter_installation_is_idempotent() -> None:
    target_logger = logging.getLogger("telethon.client.updates")
    original_filters = list(target_logger.filters)
    try:
        target_logger.filters[:] = [
            filter_ for filter_ in original_filters if not isinstance(filter_, _TelethonRoutineDifferenceFilter)
        ]
        install_telethon_log_filter()
        install_telethon_log_filter()
        assert sum(isinstance(filter_, _TelethonRoutineDifferenceFilter) for filter_ in target_logger.filters) == 1
    finally:
        target_logger.filters[:] = original_filters
