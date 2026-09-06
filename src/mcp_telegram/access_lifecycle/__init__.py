"""Application-owned access-loss and revalidation lifecycle."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from types import MappingProxyType
from typing import cast

from ..history_enrollment import reset_read_position_retry, restore_access_status
from ..hydration_queue import HydrationPriority, HydrationQueueRepository
from ..messages.sqlite_hydration_jobs import reconcile_fact_hydration_jobs_for_dialog
from ..runtime_events import record_runtime_event

_SAVEPOINTS = count()
logger = logging.getLogger(__name__)
_DATABASE_LIST_PATH_INDEX = 2


@dataclass(frozen=True, slots=True)
class AccessLifecycleEvent:
    """Immutable runtime event captured before the caller commits its work."""

    db_path: Path | None
    kind: str
    dialog_id: int
    outcome: str
    reason_code: str | None
    payload: Mapping[str, object]
    observed_at_ms: int


def _sync_db_path_from_connection(conn: sqlite3.Connection) -> Path | None:
    rows = cast(list[tuple[object, ...]], conn.execute("PRAGMA database_list").fetchall())
    for row in rows:
        if len(row) > _DATABASE_LIST_PATH_INDEX and row[1] == "main" and row[_DATABASE_LIST_PATH_INDEX]:
            return Path(str(row[_DATABASE_LIST_PATH_INDEX]))
    return None


def _try_record_runtime_event(event: AccessLifecycleEvent | None) -> None:
    if event is None:
        return
    if event.db_path is None:
        logger.warning("runtime_event_record_failed kind=%s reason=database_path_unavailable", event.kind)
        return
    try:
        conn = sqlite3.connect(event.db_path)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                record_runtime_event(
                    conn,
                    kind=event.kind,
                    dialog_id=event.dialog_id,
                    outcome=event.outcome,
                    reason_code=event.reason_code,
                    payload=event.payload,
                    observed_at_ms=event.observed_at_ms,
                )
        finally:
            conn.close()
    except Exception:
        logger.exception("runtime_event_record_failed kind=%s", event.kind)


def record_access_lifecycle_event(event: AccessLifecycleEvent | None) -> None:
    """Persist a lifecycle diagnostic after the caller's transaction commits."""
    _try_record_runtime_event(event)


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
    conn: sqlite3.Connection, dialog_id: int, now: int, *, reason: str | None = None
) -> AccessLifecycleEvent | None:
    """Atomically mark a peer inaccessible and hide its local snapshot."""
    db_path = _sync_db_path_from_connection(conn)
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
            enrolled = cast(
                tuple[int] | None,
                conn.execute(
                    "SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1", (dialog_id,)
                ).fetchone(),
            )
            if enrolled is not None:
                conn.execute(
                    "INSERT INTO sync_alert_events(kind, occurred_at, dialog_id) VALUES ('access_lost', ?, ?)",
                    (now, dialog_id),
                )
            return AccessLifecycleEvent(
                db_path=db_path,
                kind="sync.access_lost",
                dialog_id=dialog_id,
                outcome="applied",
                reason_code=reason,
                payload=MappingProxyType(payload),
                observed_at_ms=now * 1000,
            )
    return None


def restore_access_after_revalidation(
    conn: sqlite3.Connection, dialog_id: int, now: int, *, total_messages: int | None = None
) -> AccessLifecycleEvent | None:
    """Restore access while preserving snapshot metadata and requesting refresh."""
    db_path = _sync_db_path_from_connection(conn)
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
            return AccessLifecycleEvent(
                db_path=db_path,
                kind="sync.access_restored",
                dialog_id=dialog_id,
                outcome="applied",
                reason_code=None,
                payload=MappingProxyType({}),
                observed_at_ms=now * 1000,
            )
    return None


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
    "AccessLifecycleEvent",
    "due_access_revalidations",
    "record_access_lifecycle_event",
    "restore_access_after_revalidation",
    "set_access_lost",
    "stamp_access_revalidation",
]
