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
            # Individual reaction details are a separate, best-effort fact
            # stream.  Aggregate counters remain authoritative even when the
            # details endpoint is private/too old/unavailable.
            try:
                # Replace the detail snapshot even for an unavailable probe:
                # stale rows must not be presented as current Telegram facts.
                conn.execute(
                    "DELETE FROM message_reaction_events WHERE dialog_id = ? AND message_id = ?",
                    (dialog_id, item.message_id),
                )
                if item.events_status != "unavailable":
                    conn.executemany(
                        "INSERT INTO message_reaction_events "
                        "(dialog_id, message_id, reactor_id, emoji, reacted_at, fetched_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (
                                dialog_id,
                                item.message_id,
                                event.reactor_id,
                                event.emoji,
                                event.reacted_at,
                                checked_at,
                            )
                            for event in item.events
                        ],
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO message_reaction_event_status "
                    "(dialog_id, message_id, checked_at, status, returned_count) VALUES (?, ?, ?, ?, ?)",
                    (dialog_id, item.message_id, checked_at, item.events_status, len(item.events)),
                )
            except sqlite3.OperationalError:
                # Aggregate reaction freshness remains usable for lightweight
                # pre-v28 databases that do not have the detail tables yet.
                pass
            conn.execute(
                "INSERT OR REPLACE INTO message_reactions_freshness (dialog_id, message_id, checked_at) VALUES (?, ?, ?)",
                (dialog_id, item.message_id, checked_at),
            )
            refreshed += 1
    return refreshed
