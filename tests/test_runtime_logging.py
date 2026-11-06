from __future__ import annotations

import logging

import pytest

from mcp_telegram.runtime_logging import (
    _TelethonLocalUpdateWaitFilter,
    _TelethonRoutineDifferenceFilter,
    _TelethonStaleSessionFilter,
    install_telethon_log_filter,
)


def _record(
    *, message: str, level: int = logging.INFO, name: str = "telethon.client.updates", args: tuple[object, ...] = ()
) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, args, None)


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
        assert sum(isinstance(filter_, _TelethonLocalUpdateWaitFilter) for filter_ in target_logger.filters) == 1
    finally:
        target_logger.filters[:] = original_filters


@pytest.mark.parametrize("level", (logging.INFO, logging.DEBUG))
def test_stale_session_is_debug_only_and_other_security_warnings_survive(
    caplog: pytest.LogCaptureFixture, level: int
) -> None:
    sender = logging.getLogger("telethon.network.mtprotosender")
    original_filters = list(sender.filters)
    try:
        install_telethon_log_filter()
        install_telethon_log_filter()
        assert sum(isinstance(filter_, _TelethonStaleSessionFilter) for filter_ in sender.filters) == 1
        with caplog.at_level(level, logger=sender.name):
            sender.warning(
                "Security error while unpacking a received message: %s",
                ValueError("Server replied with a wrong session ID (see FAQ for details)"),
            )
            sender.warning("Security error while unpacking a received message: invalid checksum")
            sender.error(
                "Security error while unpacking a received message: "
                "Server replied with a wrong session ID (see FAQ for details)"
            )
        assert [(record.levelno, record.levelname) for record in caplog.records] == (
            [(logging.DEBUG, "DEBUG"), (logging.WARNING, "WARNING"), (logging.ERROR, "ERROR")]
            if level == logging.DEBUG
            else [(logging.WARNING, "WARNING"), (logging.ERROR, "ERROR")]
        )
    finally:
        sender.filters[:] = original_filters


@pytest.mark.parametrize(
    ("level", "message", "args"),
    (
        (
            logging.INFO,
            "Cannot get difference since Telegram is having issues: %s",
            ("TelegramUpdateEligibilityDeferredError",),
        ),
        (
            logging.WARNING,
            "Getting difference for channel updates %s caused %s; ending getting difference prematurely until server issues are resolved",
            (123, "TelegramUpdateEligibilityDeferredError"),
        ),
    ),
)
@pytest.mark.parametrize("debug_enabled", (False, True))
def test_local_update_wait_vendor_messages_are_debug_only(
    level: int, message: str, args: tuple[object, ...], debug_enabled: bool
) -> None:
    record = logging.LogRecord("telethon.client.updates", level, __file__, 1, message, args, None)
    logger = logging.getLogger(record.name)
    original_level = logger.level
    try:
        logger.setLevel(logging.DEBUG if debug_enabled else logging.INFO)
        assert _TelethonLocalUpdateWaitFilter().filter(record) is debug_enabled
        if debug_enabled:
            assert (record.levelno, record.levelname, record.getMessage()) == (
                logging.DEBUG,
                "DEBUG",
                "Update recovery waiting for local RPC eligibility: TelegramUpdateEligibilityDeferredError",
            )
    finally:
        logger.setLevel(original_level)


@pytest.mark.parametrize(
    "record",
    (
        _record(
            message="Cannot get difference since Telegram is having issues: %s",
            level=logging.INFO,
            args=("ValueError",),
        ),
        _record(
            message="Getting difference for channel updates %s caused %s; ending getting difference prematurely until server issues are resolved",
            level=logging.WARNING,
            args=(123, "ServerError"),
        ),
        _record(
            message="Cannot get difference since Telegram is having issues: %s",
            level=logging.INFO,
            name="other.logger",
            args=("TelegramUpdateEligibilityDeferredError",),
        ),
        _record(
            message="Cannot get difference since Telegram is having issues: %s",
            level=logging.WARNING,
            args=("TelegramUpdateEligibilityDeferredError",),
        ),
        _record(
            message=(
                "Getting difference for channel updates %s caused %s;"
                " ending getting difference prematurely until server issues are resolved"
            ),
            level=logging.WARNING,
            args=(123, "TelegramUpdateEligibilityDeferredError", "extra"),
        ),
        _record(
            message="Cannot get difference since Telegram is having issues: %s",
            level=logging.INFO,
            args=("TelegramUpdateEligibilityDeferredError", "extra"),
        ),
        _record(
            message="Cannot get difference since Telegram is having issues: other",
            level=logging.INFO,
            args=("TelegramUpdateEligibilityDeferredError",),
        ),
    ),
)
def test_local_update_wait_filter_preserves_nonmarker_vendor_records(record: logging.LogRecord) -> None:
    assert _TelethonLocalUpdateWaitFilter().filter(record) is True
