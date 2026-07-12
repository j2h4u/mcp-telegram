"""SQLite queries for reaction freshness snapshots."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from .sync_worker import apply_reactions_delta
from .telegram_reading import ReactionMessage


def stale_reaction_ids(
    conn: sqlite3.Connection, dialog_id: int, message_ids: Sequence[int], threshold: int
) -> tuple[str, set[int], list[int]]:
    row = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    if row is None:
        return "not_synced", set(), list(message_ids)
    if row[0] == "access_lost":
        return "access_lost", set(), list(message_ids)
    placeholders = ",".join("?" * len(message_ids))
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(
            f"SELECT message_id FROM message_reactions_freshness WHERE dialog_id = ? AND message_id IN ({placeholders}) AND checked_at > ?",
            [dialog_id, *message_ids, threshold],
        ).fetchall(),
    )
    fresh_ids = {value if isinstance(value := row[0], int) else int(str(value)) for row in rows}
    return "active", fresh_ids, [message_id for message_id in message_ids if message_id not in fresh_ids]


def persist_reaction_snapshots(
    conn: sqlite3.Connection, dialog_id: int, messages: Sequence[ReactionMessage | None], checked_at: int
) -> int:
    refreshed = 0
    with conn:
        for item in messages:
            if item is None:
                continue
            apply_reactions_delta(
                conn,
                dialog_id,
                item.message_id,
                [replace(row, dialog_id=dialog_id, message_id=item.message_id) for row in item.rows],
            )
            conn.execute(
                "INSERT OR REPLACE INTO message_reactions_freshness (dialog_id, message_id, checked_at) VALUES (?, ?, ?)",
                (dialog_id, item.message_id, checked_at),
            )
            refreshed += 1
    return refreshed
