"""Application-owned access-loss and revalidation lifecycle."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import count
from typing import cast

from ..history_enrollment import reset_read_position_retry, restore_access_status
from ..hydration_queue import HydrationPriority, HydrationQueueRepository
from ..messages.sqlite_repository import reconcile_fact_hydration_jobs_for_dialog

_SAVEPOINTS = count()


def _purge_hydration_jobs(conn: sqlite3.Connection, dialog_id: int) -> None:
    HydrationQueueRepository(conn).remove_active_for_dialog(dialog_id)


@contextmanager
def _lifecycle_savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    """Isolate one lifecycle operation without consuming an outer transaction."""
    name = f"access_lifecycle_{next(_SAVEPOINTS)}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


def _record_event(
    conn: sqlite3.Connection, *, kind: str, dialog_id: int, occurred_at: int, payload: dict[str, object]
) -> None:
    conn.execute(
        "INSERT INTO daemon_events (kind, dialog_id, occurred_at, payload_json) VALUES (?, ?, ?, ?)",
        (kind, dialog_id, occurred_at, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
    )


def set_access_lost(conn: sqlite3.Connection, dialog_id: int, now: int, *, reason: str | None = None) -> None:
    """Atomically mark a peer inaccessible and hide its local snapshot."""
    with _lifecycle_savepoint(conn):
        row = cast(
            tuple[str | None] | None,
            conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
        )
        previous_status = str(row[0]) if row is not None and row[0] is not None else None
        if row is None:
            conn.execute(
                "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', ?)",
                (dialog_id, now),
            )
        else:
            conn.execute(
                "UPDATE synced_dialogs SET status = 'access_lost', access_lost_at = ?, delta_refresh_requested_at = NULL WHERE dialog_id = ?",
                (now, dialog_id),
            )
        reset_read_position_retry(conn, dialog_id)
        _purge_hydration_jobs(conn, dialog_id)
        conn.execute("UPDATE dialogs SET hidden = 1, snapshot_at = ? WHERE dialog_id = ?", (now, dialog_id))
        if previous_status != "access_lost":
            payload: dict[str, object] = {}
            if previous_status is not None:
                payload["previous_status"] = previous_status
            if reason is not None:
                payload["reason"] = reason
            _record_event(conn, kind="access_lost", dialog_id=dialog_id, occurred_at=now, payload=payload)


def restore_access_after_revalidation(
    conn: sqlite3.Connection, dialog_id: int, now: int, *, total_messages: int | None = None
) -> None:
    """Restore access while preserving snapshot metadata and requesting refresh."""
    with _lifecycle_savepoint(conn):
        conn.execute(
            "UPDATE synced_dialogs SET access_lost_at = NULL, access_last_revalidated_at = ?, access_next_revalidate_at = NULL WHERE dialog_id = ?",
            (now, dialog_id),
        )
        restore_access_status(conn, dialog_id)
        conn.execute(
            """INSERT INTO dialogs (dialog_id, hidden, needs_refresh, snapshot_at, archived, pinned,
               unread_mentions_count, unread_reactions_count)
               VALUES (?, 0, 1, ?, 0, 0, 0, 0)
               ON CONFLICT(dialog_id) DO UPDATE SET hidden = 0, needs_refresh = 1, snapshot_at = excluded.snapshot_at""",
            (dialog_id, now),
        )
        if total_messages is not None:
            conn.execute(
                "UPDATE synced_dialogs SET total_messages = ? WHERE dialog_id = ?", (total_messages, dialog_id)
            )
        reconcile_fact_hydration_jobs_for_dialog(
            conn,
            dialog_id,
            due_at=now,
            priority=HydrationPriority.BACKFILL,
        )
        _record_event(conn, kind="access_restored", dialog_id=dialog_id, occurred_at=now, payload={})


def due_access_revalidations(conn: sqlite3.Connection, *, now: int, cooldown_seconds: int, limit: int) -> list[int]:
    rows = cast(
        list[tuple[int]],
        conn.execute(
            """SELECT dialog_id FROM synced_dialogs
           WHERE status = 'access_lost'
             AND COALESCE(access_next_revalidate_at, COALESCE(access_lost_at, 0) + ?) <= ?
           ORDER BY COALESCE(access_next_revalidate_at, COALESCE(access_lost_at, 0) + ?), dialog_id LIMIT ?""",
            (cooldown_seconds, now, cooldown_seconds, limit),
        ).fetchall(),
    )
    return [int(row[0]) for row in rows]


def stamp_access_revalidation(conn: sqlite3.Connection, dialog_id: int, checked_at: int, cooldown_seconds: int) -> None:
    with _lifecycle_savepoint(conn):
        conn.execute(
            "UPDATE synced_dialogs SET access_last_revalidated_at = ?, access_next_revalidate_at = ? WHERE dialog_id = ?",
            (checked_at, checked_at + cooldown_seconds, dialog_id),
        )


__all__ = [
    "due_access_revalidations",
    "restore_access_after_revalidation",
    "set_access_lost",
    "stamp_access_revalidation",
]
