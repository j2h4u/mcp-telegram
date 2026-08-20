"""Shared writer for exact Telegram unread facts on the dialog snapshot.

Unread values are observations, not projections from the local message mirror:
``None`` means that Telegram did not provide the fact, while ``0``/``False``
are exact observations.  Callers own the transaction boundary.
"""

from __future__ import annotations

import sqlite3
from typing import Final, cast

_UNSET: Final = object()


def apply_unread_facts(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    observed_at: int,
    unread_count: int | object | None = _UNSET,
    unread_mark: bool | object | None = _UNSET,
    mark_needs_refresh: bool = False,
    create_missing: bool = False,
) -> int:
    """Write one or both exact Telegram unread facts.

    ``unread_count`` and ``unread_mark`` use an omitted sentinel so a caller
    can update one fact without touching the other.  A supplied ``None`` is
    treated as unknown and therefore leaves the previous observation intact.
    When ``create_missing`` is true, a missing snapshot row is created as a
    thin ``needs_refresh=1`` row; ``INSERT OR IGNORE`` never unhides an existing
    row or overwrites its body metadata.

    Returns the number of existing snapshot rows updated (0 or 1).
    """
    assignments: list[str] = []
    values: list[object] = []
    if unread_count is not _UNSET and unread_count is not None:
        assignments.extend(
            (
                (
                    "unread_count = CASE WHEN unread_count_observed_at IS NULL "
                    "OR unread_count_observed_at <= ? THEN ? ELSE unread_count END"
                ),
                (
                    "unread_count_observed_at = CASE WHEN unread_count_observed_at IS NULL "
                    "OR unread_count_observed_at <= ? THEN ? ELSE unread_count_observed_at END"
                ),
            )
        )
        values.extend((observed_at, int(cast(int, unread_count)), observed_at, observed_at))
    if unread_mark is not _UNSET and unread_mark is not None:
        assignments.extend(
            (
                (
                    "unread_mark = CASE WHEN unread_mark_observed_at IS NULL "
                    "OR unread_mark_observed_at <= ? THEN ? ELSE unread_mark END"
                ),
                (
                    "unread_mark_observed_at = CASE WHEN unread_mark_observed_at IS NULL "
                    "OR unread_mark_observed_at <= ? THEN ? ELSE unread_mark_observed_at END"
                ),
            )
        )
        values.extend((observed_at, int(bool(unread_mark)), observed_at, observed_at))
    if mark_needs_refresh:
        assignments.append("needs_refresh = 1")
    if create_missing:
        conn.execute(
            "INSERT OR IGNORE INTO dialogs (dialog_id, snapshot_at, needs_refresh) VALUES (?, NULL, 1)",
            (dialog_id,),
        )
    if not assignments:
        return 0
    values.append(dialog_id)
    cursor = conn.execute(
        "UPDATE dialogs SET " + ", ".join(assignments) + " WHERE dialog_id = ?",
        values,
    )
    return cursor.rowcount
