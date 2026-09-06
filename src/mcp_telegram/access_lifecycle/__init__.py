"""Application-owned access-loss and revalidation lifecycle."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import count
from typing import cast

from ..history_enrollment import reset_read_position_retry, restore_access_status
from ..hydration_queue import HydrationPriority, HydrationQueueRepository
from ..messages.sqlite_hydration_jobs import reconcile_fact_hydration_jobs_for_dialog

_SAVEPOINTS = count()
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AccessLossEvidence:
    reason_code: str
    access_change_cause: str
    actor_id: int


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


def set_access_lost(
    conn: sqlite3.Connection,
    dialog_id: int,
    now: int,
    *,
    reason: str | None = None,
    evidence: AccessLossEvidence | None = None,
) -> bool:
    """Atomically mark a peer inaccessible and hide its local snapshot."""
    changed = False
    reason_code = evidence.reason_code if evidence is not None else reason
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
            conn.execute(
                """INSERT INTO conversation_history_events(
                       kind, occurred_at, time_basis, dialog_id, reason_code, previous_status,
                       access_change_cause, actor_id
                   ) VALUES ('access_lost', ?, 'observed', ?, ?, ?, ?, ?)""",
                (
                    now,
                    dialog_id,
                    reason_code,
                    previous_status,
                    evidence.access_change_cause if evidence is not None else None,
                    evidence.actor_id if evidence is not None else None,
                ),
            )
            changed = True
    if changed:
        logger.warning("access_lost dialog_id=%d reason_code=%s", dialog_id, reason_code)
    return changed


def restore_access_after_revalidation(
    conn: sqlite3.Connection, dialog_id: int, now: int, *, total_messages: int | None = None
) -> bool:
    """Restore access while preserving snapshot metadata and requesting refresh."""
    changed = False
    with _lifecycle_savepoint(conn):
        row = cast(
            tuple[str | None] | None,
            conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
        )
        was_access_lost = row is not None and row[0] == "access_lost"
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
        if was_access_lost:
            conn.execute(
                """INSERT INTO conversation_history_events(
                       kind, occurred_at, time_basis, dialog_id, previous_status
                   ) VALUES ('access_restored', ?, 'observed', ?, 'access_lost')""",
                (now, dialog_id),
            )
            changed = True
    if changed:
        logger.info("access_restored dialog_id=%d total_messages=%s", dialog_id, total_messages)
    return changed


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
    "AccessLossEvidence",
    "due_access_revalidations",
    "restore_access_after_revalidation",
    "set_access_lost",
    "stamp_access_revalidation",
]
