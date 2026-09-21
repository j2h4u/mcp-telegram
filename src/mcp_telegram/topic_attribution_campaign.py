"""Application facade for the temporary topic-attribution repair.

Persistence, including every ``messages`` mutation, belongs to the reviewed
SQLite bundle repository. This facade owns enrollment intent and lifecycle.
PR2 removes the facade and the persisted daemon-state campaign receipt.
"""

from __future__ import annotations

from collections.abc import Sequence

from .history_enrollment import full_history_enabled
from .message_contracts import ExtractedMessage
from .messages.sqlite_bundle import (
    CAMPAIGN_DIALOG_COUNT,
    CAMPAIGN_STATE_KEY,
    SQLiteConnection,
    TopicAttributionCampaignError,
)
from .messages.sqlite_bundle import (
    abort_campaign as _abort_campaign,
)
from .messages.sqlite_bundle import (
    advance_campaign as _advance_campaign,
)
from .messages.sqlite_bundle import (
    campaign_dialog_visible as _campaign_dialog_visible,
)
from .messages.sqlite_bundle import (
    campaign_release_at as _campaign_release_at,
)
from .messages.sqlite_bundle import (
    campaign_status as _campaign_status,
)
from .messages.sqlite_bundle import (
    enroll_campaign as _enroll_campaign,
)
from .messages.sqlite_bundle import (
    record_access_lost as _record_access_lost,
)
from .messages.sqlite_bundle import (
    record_deferred as _record_deferred,
)
from .messages.sqlite_bundle import (
    record_failed_attempt as _record_failed_attempt,
)
from .messages.sqlite_bundle import (
    record_page as _record_page,
)
from .messages.sqlite_bundle import (
    reset_campaign as _reset_campaign,
)


def enroll_campaign(conn: SQLiteConnection, dialog_ids: Sequence[int], *, now: int | None = None) -> dict[str, object]:
    """Enroll only dialogs with explicit enabled full-history intent."""
    if len(dialog_ids) != CAMPAIGN_DIALOG_COUNT or any(isinstance(dialog_id, bool) for dialog_id in dialog_ids):
        raise TopicAttributionCampaignError("exactly two dialog ids are required")
    if len(set(dialog_ids)) != CAMPAIGN_DIALOG_COUNT:
        raise TopicAttributionCampaignError("the two dialog ids must be distinct")
    if any(not full_history_enabled(conn, dialog_id) for dialog_id in dialog_ids):
        raise TopicAttributionCampaignError("each enrolled dialog requires enabled full-history enrollment")
    return _enroll_campaign(conn, dialog_ids, now=now)


def advance_campaign(conn: SQLiteConnection, *, now: int) -> tuple[int, int] | None:
    """Select a ready enrolled dialog, abandoning one disabled since enrollment."""
    while (candidate := _advance_campaign(conn, now=now)) is not None:
        if full_history_enabled(conn, candidate[0]):
            return candidate
        _record_access_lost(conn, candidate[0], candidate[1], observed_at=now, reason="history_disabled")
    return None


def campaign_execution_allowed(conn: SQLiteConnection, dialog_id: int) -> bool:
    """Require current enabled intent and canonical visible synced eligibility."""
    return full_history_enabled(conn, dialog_id) and _campaign_dialog_visible(conn, dialog_id)


def campaign_release_at(conn: SQLiteConnection, *, now: int) -> float | None:
    return _campaign_release_at(conn, now=now)


def campaign_status(conn: SQLiteConnection) -> dict[str, object]:
    return _campaign_status(conn)


def reset_campaign(conn: SQLiteConnection) -> dict[str, object]:
    return _reset_campaign(conn)


def abort_campaign(conn: SQLiteConnection) -> dict[str, object]:
    """Terminalize only an active manifest before reset and re-enrollment."""
    return _abort_campaign(conn)


def record_page(
    conn: SQLiteConnection, dialog_id: int, checkpoint: int, messages: Sequence[ExtractedMessage], *, observed_at: int
) -> dict[str, object]:
    return _record_page(conn, dialog_id, checkpoint, messages, observed_at=observed_at)


def record_deferred(conn: SQLiteConnection, dialog_id: int, checkpoint: int, *, reason: str, observed_at: int) -> None:
    _record_deferred(conn, dialog_id, checkpoint, reason=reason, observed_at=observed_at)


def record_failed_attempt(
    conn: SQLiteConnection, dialog_id: int, checkpoint: int, *, reason: str, observed_at: int
) -> None:
    _record_failed_attempt(conn, dialog_id, checkpoint, reason=reason, observed_at=observed_at)


def record_access_lost(
    conn: SQLiteConnection, dialog_id: int, checkpoint: int, *, observed_at: int, reason: str = "access_lost"
) -> None:
    _record_access_lost(conn, dialog_id, checkpoint, observed_at=observed_at, reason=reason)


__all__ = [
    "CAMPAIGN_DIALOG_COUNT",
    "CAMPAIGN_STATE_KEY",
    "TopicAttributionCampaignError",
    "abort_campaign",
    "advance_campaign",
    "campaign_execution_allowed",
    "campaign_release_at",
    "campaign_status",
    "enroll_campaign",
    "record_access_lost",
    "record_deferred",
    "record_failed_attempt",
    "record_page",
    "reset_campaign",
]
