"""Small operator-facing dialog context helpers for daemon logs.

These helpers read only local Telegram snapshots. They never contact Telegram
and must not be used as a source of truth for user-facing event timestamps.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class DialogLogContext:
    dialog_id: int
    name: str | None = None
    type: str | None = None
    archived: bool | None = None
    hidden: bool | None = None


def dialog_log_context(conn: sqlite3.Connection, dialog_id: int) -> DialogLogContext:
    """Return compact local context for operator logs."""
    try:
        row = cast(
            tuple[object, object, object, object] | None,
            conn.execute(
                "SELECT name, type, archived, hidden FROM dialogs WHERE dialog_id = ?",
                (int(dialog_id),),
            ).fetchone(),
        )
    except sqlite3.Error:
        row = None
    if row is None:
        return DialogLogContext(dialog_id=int(dialog_id))
    return DialogLogContext(
        dialog_id=int(dialog_id),
        name=str(row[0]) if row[0] is not None else None,
        type=str(row[1]) if row[1] is not None else None,
        archived=bool(row[2]) if row[2] is not None else None,
        hidden=bool(row[3]) if row[3] is not None else None,
    )
