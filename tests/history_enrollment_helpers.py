"""Explicit test fixtures for the durable full-history intent table."""
# pyright: reportAny=false, reportExplicitAny=false, reportArgumentType=false

from __future__ import annotations

from typing import Any


def seed_full_history_enrollment(
    conn: Any,
    dialog_id: int,
    *,
    enabled: bool,
    updated_at: int = 0,
) -> None:
    """Seed one operator intent; production never derives this from coverage."""
    conn.execute(
        """INSERT INTO full_history_enrollment(dialog_id, enabled, updated_at)
           VALUES (?, ?, ?)""",
        (dialog_id, int(enabled), updated_at),
    )
