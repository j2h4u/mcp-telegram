"""Small logging filters shared by the active runtime entrypoints."""

from __future__ import annotations

import logging
import re

_TELETHON_UPDATES_LOGGER_NAME = "telethon.client.updates"
_ACCOUNT_DIFFERENCE_MESSAGE = "Got difference for account updates"
_CHANNEL_DIFFERENCE_PATTERN = re.compile(r"Got difference for channel -?\d+ updates\Z")
_TELETHON_SENDER_LOGGER_NAME = "telethon.network.mtprotosender"
_STALE_SESSION_MESSAGE = (
    "Security error while unpacking a received message: Server replied with a wrong session ID (see FAQ for details)"
)
_LOCAL_UPDATE_DEFERRED_TYPE = "TelegramUpdateEligibilityDeferredError"
_CHANNEL_LOCAL_WAIT_ARGUMENT_COUNT = 2
_ACCOUNT_LOCAL_WAIT = "Cannot get difference since Telegram is having issues: %s"
_CHANNEL_LOCAL_WAIT = (
    "Getting difference for channel updates %s caused %s;"
    " ending getting difference prematurely until server issues are resolved"
)


class _TelethonRoutineDifferenceFilter(logging.Filter):
    """Drop only successful INFO difference messages from Telethon."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != _TELETHON_UPDATES_LOGGER_NAME or record.levelno != logging.INFO:
            return True
        message = record.getMessage()
        return message != _ACCOUNT_DIFFERENCE_MESSAGE and not _CHANNEL_DIFFERENCE_PATTERN.fullmatch(message)


class _TelethonStaleSessionFilter(logging.Filter):
    """Keep rejected stale-session frames at DEBUG; preserve other security warnings."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == _TELETHON_SENDER_LOGGER_NAME
            and record.levelno == logging.WARNING
            and record.getMessage() == _STALE_SESSION_MESSAGE
        ):
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
            # Logger levels are checked before filters, so enforce the new level here.
            return logging.getLogger(record.name).isEnabledFor(logging.DEBUG)
        return True


class _TelethonLocalUpdateWaitFilter(logging.Filter):
    """Demote only pinned vendor messages carrying our local deferral type."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != _TELETHON_UPDATES_LOGGER_NAME:
            return True
        message = record.msg
        account_wait = record.levelno == logging.INFO and message == _ACCOUNT_LOCAL_WAIT
        channel_wait = record.levelno == logging.WARNING and message == _CHANNEL_LOCAL_WAIT
        account_marker = account_wait and record.args == (_LOCAL_UPDATE_DEFERRED_TYPE,)
        channel_marker = (
            channel_wait
            and isinstance(record.args, tuple)
            and len(record.args) == _CHANNEL_LOCAL_WAIT_ARGUMENT_COUNT
            and record.args[1] == _LOCAL_UPDATE_DEFERRED_TYPE
        )
        if account_marker or channel_marker:
            record.msg = "Update recovery waiting for local RPC eligibility: %s"
            record.args = (_LOCAL_UPDATE_DEFERRED_TYPE,)
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
            return logging.getLogger(record.name).isEnabledFor(logging.DEBUG)
        return True


def install_telethon_log_filter() -> None:
    """Install narrow Telethon logging filters once per process."""
    target_logger = logging.getLogger(_TELETHON_UPDATES_LOGGER_NAME)
    if not any(isinstance(filter_, _TelethonRoutineDifferenceFilter) for filter_ in target_logger.filters):
        target_logger.addFilter(_TelethonRoutineDifferenceFilter())
    if not any(isinstance(filter_, _TelethonLocalUpdateWaitFilter) for filter_ in target_logger.filters):
        target_logger.addFilter(_TelethonLocalUpdateWaitFilter())
    sender_logger = logging.getLogger(_TELETHON_SENDER_LOGGER_NAME)
    if not any(isinstance(filter_, _TelethonStaleSessionFilter) for filter_ in sender_logger.filters):
        sender_logger.addFilter(_TelethonStaleSessionFilter())
