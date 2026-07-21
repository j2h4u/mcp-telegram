"""Public, transaction-neutral persistence operations for reaction aggregates."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from .contracts import ReactionAggregate

_DELETE_REACTIONS_SQL = "DELETE FROM message_reactions WHERE dialog_id = ? AND message_id = ?"
_INSERT_REACTION_SQL = (
    "INSERT OR REPLACE INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)"
)


def replace_reaction_aggregates(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    aggregates: Sequence[ReactionAggregate],
) -> None:
    """Replace one message's aggregate rows; the calling use case owns the transaction."""
    conn.execute(_DELETE_REACTIONS_SQL, (dialog_id, message_id))
    if aggregates:
        conn.executemany(
            _INSERT_REACTION_SQL,
            [(dialog_id, message_id, aggregate.emoji, aggregate.count) for aggregate in aggregates],
        )
