"""Durable, single-worker queue for message-fact hydration jobs.

The queue deliberately contains only scheduling state.  Callers own the
SQLite transaction: none of the operations in this module commits or starts a
transaction, so queue changes can be committed together with the corresponding
message-fact write.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import cast

# Keep this name in lock-step with the v37 schema.  The queue is part of the
# durable database contract, so callers must never create an ad-hoc variant.
HYDRATION_QUEUE_TABLE = "hydration_jobs"


@dataclass(frozen=True, slots=True)
class HydrationJob:
    """The complete state of one durable hydration job."""

    kind: str
    dialog_id: int
    message_id: int
    due_at: int
    attempts: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("kind must be nonempty")
        # Telegram dialog identifiers may be negative (channels and groups),
        # but zero is never a valid target. Message IDs are positive.
        if self.dialog_id == 0:
            raise ValueError("dialog_id must be nonzero")
        if self.message_id <= 0:
            raise ValueError("message_id must be positive")
        if self.attempts < 0:
            raise ValueError("attempts must be nonnegative")


_JOB_COLUMNS = "kind, dialog_id, message_id, due_at, attempts"


def _identity(job: HydrationJob) -> tuple[str, int, int]:
    return job.kind, job.dialog_id, job.message_id


def _job_from_row(row: sqlite3.Row | tuple[object, ...]) -> HydrationJob:
    kind, dialog_id, message_id, due_at, attempts = row
    return HydrationJob(
        kind=cast(str, kind),
        dialog_id=int(cast(int | str, dialog_id)),
        message_id=int(cast(int | str, message_id)),
        due_at=int(cast(int | str, due_at)),
        attempts=int(cast(int | str, attempts)),
    )


class HydrationQueueRepository:
    """SQLite adapter for the message hydration queue.

    The connection is supplied by the caller and remains transaction-owned by
    that caller.  In particular, this adapter never uses ``with conn`` and
    never calls ``commit``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def is_available(self) -> bool:
        """Return whether the current database has the durable queue."""
        return (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (HYDRATION_QUEUE_TABLE,),
            ).fetchone()
            is not None
        )

    def enqueue(self, job: HydrationJob) -> None:
        """Insert *job*, or move an existing job's due time earlier.

        A retry count belongs to the queue identity, so re-enqueueing an
        existing job never resets ``attempts``.
        """
        self._conn.execute(
            f"INSERT INTO {HYDRATION_QUEUE_TABLE} ({_JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, dialog_id, message_id) DO UPDATE SET "
            "due_at = CASE WHEN excluded.due_at < due_at THEN excluded.due_at ELSE due_at END",
            (job.kind, job.dialog_id, job.message_id, job.due_at, job.attempts),
        )

    def due_jobs(self, now: int, limit: int, *, kind: str | None = None) -> list[HydrationJob]:
        """Return jobs due at or before *now* in deterministic queue order."""
        if limit <= 0:
            return []
        kind_clause = " AND kind = ?" if kind is not None else ""
        parameters: tuple[object, ...] = (now, kind, limit) if kind is not None else (now, limit)
        rows = cast(
            list[tuple[object, ...]],
            self._conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM {HYDRATION_QUEUE_TABLE} "
                f"WHERE due_at <= ?{kind_clause} ORDER BY due_at, kind, dialog_id, message_id LIMIT ?",
                parameters,
            ).fetchall(),
        )
        return [_job_from_row(row) for row in rows]

    def start(self, job: HydrationJob) -> HydrationJob | None:
        """Atomically increment and return a queued job, or return ``None``.

        ``UPDATE ... RETURNING`` makes the increment and read one SQLite
        statement.  A missing identity therefore cannot manufacture a job.
        """
        row = cast(
            tuple[object, ...] | None,
            self._conn.execute(
                f"UPDATE {HYDRATION_QUEUE_TABLE} SET attempts = attempts + 1 "
                "WHERE kind = ? AND dialog_id = ? AND message_id = ? "
                f"RETURNING {_JOB_COLUMNS}",
                _identity(job),
            ).fetchone(),
        )
        return None if row is None else _job_from_row(row)

    def reschedule(self, job: HydrationJob, due_at: int) -> bool:
        """Set the next due time for *job* without changing its attempts."""
        cursor = self._conn.execute(
            f"UPDATE {HYDRATION_QUEUE_TABLE} SET due_at = ? WHERE kind = ? AND dialog_id = ? AND message_id = ?",
            (due_at, *_identity(job)),
        )
        return cursor.rowcount > 0

    def remove(self, job: HydrationJob) -> bool:
        """Delete *job* and report whether a row was removed."""
        cursor = self._conn.execute(
            f"DELETE FROM {HYDRATION_QUEUE_TABLE} WHERE kind = ? AND dialog_id = ? AND message_id = ?",
            _identity(job),
        )
        return cursor.rowcount > 0

    def remove_for_dialog(self, dialog_id: int, *, kind: str | None = None) -> int:
        """Delete all jobs for one dialog and return the number removed."""
        if not self.is_available():
            return 0
        if kind is None:
            cursor = self._conn.execute(
                f"DELETE FROM {HYDRATION_QUEUE_TABLE} WHERE dialog_id = ?",
                (dialog_id,),
            )
        else:
            cursor = self._conn.execute(
                f"DELETE FROM {HYDRATION_QUEUE_TABLE} WHERE dialog_id = ? AND kind = ?",
                (dialog_id, kind),
            )
        return cursor.rowcount


__all__ = [
    "HYDRATION_QUEUE_TABLE",
    "HydrationJob",
    "HydrationQueueRepository",
]
