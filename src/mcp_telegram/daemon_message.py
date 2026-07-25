"""Message serialization helpers for daemon API responses."""

import sqlite3
from typing import cast

from .telegram_message_projection import MessageLike as _MessageLike
from .telegram_message_projection import message_to_dict

__all__ = ["_MessageLike", "fetch_reaction_counts", "message_to_dict"]


def fetch_reaction_counts(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: list[int],
) -> dict[int, list[tuple[str, int]]]:
    """Return `{message_id: [(emoji, count), ...]}` for the given page."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    rows = cast(
        list[tuple[int | float | str, object, int | float | str]],
        conn.execute(
            f"SELECT message_id, emoji, count FROM message_reactions "
            f"WHERE dialog_id = ? AND message_id IN ({placeholders}) "
            f"ORDER BY count DESC, emoji",
            [dialog_id, *message_ids],
        ).fetchall(),
    )
    result: dict[int, list[tuple[str, int]]] = {}
    for msg_id, emoji, count in rows:
        result.setdefault(int(msg_id), []).append((str(emoji), int(count)))
    return result
