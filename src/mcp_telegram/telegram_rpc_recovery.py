"""Shared backoff policy for the durable Telegram safety owner."""

from enum import StrEnum


class RecoveryCause(StrEnum):
    FLOOD_WAIT = "flood_wait"
    TRANSIENT = "transient"
    READ_UNCERTAINTY = "read_uncertainty"


_BACKOFF = {
    RecoveryCause.FLOOD_WAIT: (3600, 86400),
    RecoveryCause.TRANSIENT: (30, 1800),
    RecoveryCause.READ_UNCERTAINTY: (300, 3600),
}


def backoff_seconds(cause: RecoveryCause, failure_stage: int) -> int:
    """Calculate a capped exponential delay without unbounded integer growth."""
    if isinstance(failure_stage, bool) or not isinstance(failure_stage, int) or failure_stage < 0:
        raise ValueError("failure_stage must be a nonnegative integer")
    base, cap = _BACKOFF[cause]
    return min(base << min(failure_stage, cap.bit_length()), cap)
