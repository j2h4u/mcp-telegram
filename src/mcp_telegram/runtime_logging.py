"""Small logging filters shared by the active runtime entrypoints."""

from __future__ import annotations

import logging
import re

_TELETHON_UPDATES_LOGGER_NAME = "telethon.client.updates"
_ACCOUNT_DIFFERENCE_MESSAGE = "Got difference for account updates"
_CHANNEL_DIFFERENCE_PATTERN = re.compile(r"Got difference for channel -?\d+ updates\Z")


class _TelethonRoutineDifferenceFilter(logging.Filter):
    """Drop only successful INFO difference messages from Telethon."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != _TELETHON_UPDATES_LOGGER_NAME or record.levelno != logging.INFO:
            return True
        message = record.getMessage()
        return message != _ACCOUNT_DIFFERENCE_MESSAGE and not _CHANNEL_DIFFERENCE_PATTERN.fullmatch(message)


def install_telethon_log_filter() -> None:
    """Install the narrow Telethon success-message filter once per process."""
    target_logger = logging.getLogger(_TELETHON_UPDATES_LOGGER_NAME)
    if any(isinstance(filter_, _TelethonRoutineDifferenceFilter) for filter_ in target_logger.filters):
        return
    target_logger.addFilter(_TelethonRoutineDifferenceFilter())
