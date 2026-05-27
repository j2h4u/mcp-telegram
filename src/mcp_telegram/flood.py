"""Shared FloodWait helpers.

Telethon raises ``FloodWaitError`` (carrying a ``.seconds`` field) from any
request that trips Telegram's per-account rate limiter. Short floods
(``seconds <= flood_sleep_threshold``, Telethon's default 60) are absorbed
inside Telethon itself; these helpers cover only the long-flood path that our
own loops must handle — extracting the wait duration and sleeping through it
without losing shutdown responsiveness.

The *recovery policy* — commit partial progress, stamp a checkpoint, return a
neutral result, retry the same batch — is intentionally NOT captured here. It
differs per call site and stays explicit in each handler. Only the two
genuinely-duplicated mechanics live in this module.
"""

import asyncio

DEFAULT_FLOOD_WAIT_SECONDS = 60
"""Fallback when an exception carries no usable ``.seconds``.

Defensive only — a real Telethon ``FloodWaitError`` always sets ``.seconds``.
Matches Telethon's own default ``flood_sleep_threshold`` so the long/short
boundary stays consistent.
"""


def flood_seconds(
    exc: BaseException,
    *,
    default: int = DEFAULT_FLOOD_WAIT_SECONDS,
) -> int:
    """Return a FloodWait's wait duration in whole seconds.

    Reads ``exc.seconds`` defensively: a missing, ``None``, or zero value
    falls back to ``default`` so callers never sleep for 0s or crash on a
    malformed exception.
    """
    seconds = getattr(exc, "seconds", None)
    return int(seconds or default)


async def sleep_through_flood(shutdown_event: asyncio.Event, seconds: float) -> bool:
    """Sleep ``seconds``, waking early if ``shutdown_event`` is set.

    Returns ``True`` if shutdown was signalled during the wait — the caller
    should bail out of its current pass. Returns ``False`` if the full
    duration elapsed normally — the caller may retry or continue.
    """
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=float(seconds))
        return True
    except TimeoutError:
        return False
