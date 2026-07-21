"""SQLite persistence for reaction aggregates and freshness snapshots."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import cast

from .contracts import ReactionSnapshot
from .persistence import replace_reaction_aggregates


class SQLiteReactionSnapshotRepository:
    """Reaction persistence adapter over the daemon-owned SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Isolate one refresh without committing a surrounding caller transaction."""
        self._conn.execute("SAVEPOINT reaction_refresh")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK TO SAVEPOINT reaction_refresh")
            self._conn.execute("RELEASE SAVEPOINT reaction_refresh")
            raise
        else:
            self._conn.execute("RELEASE SAVEPOINT reaction_refresh")

    def stale_reaction_ids(
        self, dialog_id: int, message_ids: Sequence[int], threshold: int
    ) -> tuple[str, set[int], list[int]]:
        if not message_ids:
            return "active", set(), []
        row = cast(
            tuple[object, ...] | None,
            self._conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
        )
        if row is None:
            return "not_synced", set(), list(message_ids)
        if row[0] == "access_lost":
            return "access_lost", set(), list(message_ids)
        placeholders = ",".join("?" * len(message_ids))
        rows = cast(
            list[tuple[object, ...]],
            self._conn.execute(
                "SELECT message_id FROM message_reactions_freshness "
                f"WHERE dialog_id = ? AND message_id IN ({placeholders}) AND checked_at > ?",
                [dialog_id, *message_ids, threshold],
            ).fetchall(),
        )
        fresh_ids = {value if isinstance(value := row[0], int) else int(str(value)) for row in rows}
        return "active", fresh_ids, [message_id for message_id in message_ids if message_id not in fresh_ids]

    def persist_reaction_snapshots(
        self, dialog_id: int, snapshots: Sequence[ReactionSnapshot | None], checked_at: int
    ) -> int:
        """Persist snapshots in the caller's transaction; never commit here."""
        refreshed = 0
        for snapshot in snapshots:
            if snapshot is None:
                continue
            replace_reaction_aggregates(self._conn, dialog_id, snapshot.message_id, snapshot.aggregates)
            self._replace_event_snapshot(dialog_id, snapshot, checked_at)
            self._conn.execute(
                "INSERT OR REPLACE INTO message_reactions_freshness (dialog_id, message_id, checked_at) VALUES (?, ?, ?)",
                (dialog_id, snapshot.message_id, checked_at),
            )
            refreshed += 1
        return refreshed

    def _replace_event_snapshot(self, dialog_id: int, snapshot: ReactionSnapshot, checked_at: int) -> None:
        """Persist best-effort individual details without weakening aggregates."""
        try:
            self._conn.execute(
                "DELETE FROM message_reaction_events WHERE dialog_id = ? AND message_id = ?",
                (dialog_id, snapshot.message_id),
            )
            if snapshot.events_status != "unavailable":
                self._conn.executemany(
                    "INSERT INTO message_reaction_events "
                    "(dialog_id, message_id, reactor_id, emoji, reacted_at, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            dialog_id,
                            snapshot.message_id,
                            event.reactor_id,
                            event.emoji,
                            event.reacted_at,
                            checked_at,
                        )
                        for event in snapshot.events
                    ],
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO message_reaction_event_status "
                "(dialog_id, message_id, checked_at, status, returned_count) VALUES (?, ?, ?, ?, ?)",
                (dialog_id, snapshot.message_id, checked_at, snapshot.events_status, len(snapshot.events)),
            )
        except sqlite3.OperationalError:
            # Pre-v28 databases lack detail tables; aggregate freshness remains usable.
            pass
