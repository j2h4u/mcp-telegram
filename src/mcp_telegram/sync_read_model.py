"""Neutral read model for sync lifecycle and coverage facts."""

from __future__ import annotations

import time
from typing import cast

_HISTORY_SCOPE_BY_STATUS = {
    "synced": "full",
    "syncing": "full",
    "own_only": "own_only",
    "fragment": "fragment",
    "access_lost": "access_lost",
}
_HISTORY_DEPTH_BY_STATUS = {
    "synced": "complete",
    "syncing": "partial",
    "own_only": "partial",
    "fragment": "partial",
    "access_lost": "partial",
    "not_synced": "none",
}
_HISTORY_SYNC_BY_STATUS = {
    "synced": "complete_as_of_last_sync",
    "syncing": "syncing",
    "own_only": "own_messages_only",
    "fragment": "fragment_only",
    "access_lost": "access_lost_archive",
    "not_synced": "not_synced",
}


def compute_sync_coverage(total_messages: int | None, local_count: int) -> int | None:
    if total_messages is None or total_messages < 0 or local_count > total_messages:
        return None
    if total_messages == 0:
        return 100
    return round(local_count / cast(int, total_messages) * 100)


def build_sync_read_model(
    *, status: str, timestamps: tuple[int | None, int | None, int | None], local_count: int, total_messages: int | None
) -> dict[str, object]:
    local_knowledge_at = max((ts for ts in timestamps if ts is not None), default=None)
    age = None if local_knowledge_at is None else max(0, int(time.time()) - local_knowledge_at)
    coverage_state = (
        "telegram_total_unknown"
        if total_messages is None
        else "telegram_total_invalid"
        if total_messages < 0
        else "telegram_total_not_comparable"
        if local_count > total_messages
        else "telegram_total_comparable"
    )
    return {
        "history_scope": _HISTORY_SCOPE_BY_STATUS.get(status, "none"),
        "history_depth_state": _HISTORY_DEPTH_BY_STATUS.get(status, "unknown"),
        "history_sync_state": _HISTORY_SYNC_BY_STATUS.get(status, "unknown"),
        "history_complete_at": timestamps[0] if status == "synced" else None,
        "saved_message_count": local_count,
        "coverage_state": coverage_state,
        "local_knowledge_at": local_knowledge_at,
        "local_knowledge_age_seconds": age,
    }
