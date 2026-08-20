"""Pure policy for realtime message-history coverage.

Sync status is a domain fact, not permission to write arbitrary event data.
This module keeps the status vocabulary and the outgoing-message exception
independent from Telethon and SQLite orchestration.
"""

from __future__ import annotations

from enum import StrEnum


class RealtimeHistoryCoverage(StrEnum):
    """Realtime body coverage granted by a persisted sync status."""

    FULL_HISTORY = "full_history"
    OWN_OUTGOING = "own_outgoing"
    NO_REALTIME_HISTORY = "no_realtime_history"


class RealtimeBodyEvent(StrEnum):
    """Message-body event families governed by this policy."""

    EDIT = "edit"
    TRANSCRIPTION = "transcription"
    REACTION = "reaction"
    DELETE = "delete"


_FULL_HISTORY_STATUSES = frozenset({"synced", "syncing"})


def realtime_history_coverage(status: str | None) -> RealtimeHistoryCoverage:
    """Map persisted sync status to realtime body coverage.

    Unknown and missing statuses deliberately fail closed.  Status values are
    compared exactly so a future status cannot accidentally enable writes.
    """

    if status in _FULL_HISTORY_STATUSES:
        return RealtimeHistoryCoverage.FULL_HISTORY
    if status == "own_only":
        return RealtimeHistoryCoverage.OWN_OUTGOING
    return RealtimeHistoryCoverage.NO_REALTIME_HISTORY


def allows_new_message(coverage: RealtimeHistoryCoverage, *, outgoing: bool) -> bool:
    """Return whether a NewMessage body may be inserted."""

    if coverage is RealtimeHistoryCoverage.FULL_HISTORY:
        return True
    return coverage is RealtimeHistoryCoverage.OWN_OUTGOING and outgoing


def allows_missing_body_insert(
    coverage: RealtimeHistoryCoverage,
    event: RealtimeBodyEvent,
    *,
    outgoing: bool,
) -> bool:
    """Return whether an event may create a message row absent from history."""

    if event in (RealtimeBodyEvent.REACTION, RealtimeBodyEvent.DELETE):
        return False
    if coverage is RealtimeHistoryCoverage.FULL_HISTORY:
        return True
    return coverage is RealtimeHistoryCoverage.OWN_OUTGOING and outgoing


def allows_existing_body_update(
    coverage: RealtimeHistoryCoverage,
    event: RealtimeBodyEvent,
    *,
    outgoing: bool,
) -> bool:
    """Return whether an event may mutate an existing message or projection."""

    if coverage is RealtimeHistoryCoverage.FULL_HISTORY:
        return True
    if coverage is not RealtimeHistoryCoverage.OWN_OUTGOING:
        return False
    # Existing own-only rows are canonical: the event payload's ``out`` flag
    # is not trusted to reclassify a row that was already stored as outgoing.
    return outgoing


__all__ = [
    "RealtimeBodyEvent",
    "RealtimeHistoryCoverage",
    "allows_existing_body_update",
    "allows_missing_body_insert",
    "allows_new_message",
    "realtime_history_coverage",
]
