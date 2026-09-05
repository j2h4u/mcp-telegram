"""Consistent log-level policy for periodic maintenance summaries."""

import logging


def log_maintenance_cycle(
    logger: logging.Logger,
    did_work: bool,
    message: str,
    *args: object,
) -> None:
    """Log productive cycles at INFO and routine no-op cycles at DEBUG."""
    logger.log(logging.INFO if did_work else logging.DEBUG, message, *args)


__all__ = ["log_maintenance_cycle"]
