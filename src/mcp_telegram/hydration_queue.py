"""Durable, single-worker queue for message-fact hydration jobs.

The queue deliberately contains only scheduling state.  Callers own the
SQLite transaction: none of the operations in this module commits or starts a
transaction, so queue changes can be committed together with the corresponding
message-fact write.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import IntEnum
from typing import cast

# Keep this name in lock-step with the current schema.  The queue is part of the
# durable database contract, so callers must never create an ad-hoc variant.
HYDRATION_QUEUE_TABLE = "hydration_jobs"
MEDIA_METADATA_KIND = "media_metadata"
TRANSCRIPTION_HYDRATION_KIND = "transcription"
DEFAULT_SUMMARY_MESSAGE_IDS = 32
_REGISTERED_HYDRATION_KINDS = (MEDIA_METADATA_KIND, TRANSCRIPTION_HYDRATION_KIND)


class HydrationPriority(IntEnum):
    """Two intentional service classes for durable hydration work."""

    BACKFILL = 0
    FOREGROUND = 1


@dataclass(frozen=True, slots=True)
class HydrationJob:
    """The complete state of one durable hydration job."""

    kind: str
    dialog_id: int
    message_id: int
    due_at: int
    attempts: int
    message_sent_at: int = 0
    priority: HydrationPriority = HydrationPriority.FOREGROUND
    terminal: bool = False

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
        if self.message_sent_at < 0:
            raise ValueError("message_sent_at must be nonnegative")
        if not isinstance(self.priority, HydrationPriority):
            raise ValueError("priority must be a HydrationPriority")


@dataclass(frozen=True, slots=True)
class HydrationQueueSummary:
    """Bounded, privacy-safe summary of queued jobs for one kind and dialog."""

    kind: str
    dialog_id: int
    job_count: int
    message_ids: tuple[int, ...]
    attempts_min: int
    attempts_max: int


@dataclass(frozen=True, slots=True)
class HydrationQueueKindSnapshot:
    """Privacy-safe operational state for one hydration kind."""

    kind: str
    active: int
    due: int
    foreground: int
    backfill: int
    terminal: int
    oldest_message_sent_at: int | None
    newest_message_sent_at: int | None
    max_attempts: int


_JOB_COLUMNS = "kind, dialog_id, message_id, due_at, attempts, message_sent_at, priority, terminal"
_SELECT_JOB_COLUMNS = ", ".join(f"hj.{column}" for column in _JOB_COLUMNS.split(", "))
_SUMMARY_AGGREGATE_SQL = (
    f"SELECT COUNT(*), MIN(attempts), MAX(attempts) FROM {HYDRATION_QUEUE_TABLE} "
    "WHERE kind = ? AND dialog_id = ? AND terminal = 0"
)
_SUMMARY_MESSAGE_IDS_SQL = (
    f"SELECT message_id FROM {HYDRATION_QUEUE_TABLE} WHERE kind = ? AND dialog_id = ? AND terminal = 0 "
    "ORDER BY message_id LIMIT ?"
)
_QUEUE_SNAPSHOT_SQL = (
    f"SELECT kind, "
    "SUM(CASE WHEN terminal = 0 THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN terminal = 0 AND due_at <= ? THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN terminal = 0 AND priority = 1 THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN terminal = 0 AND priority = 0 THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN terminal = 1 THEN 1 ELSE 0 END), "
    "MIN(CASE WHEN terminal = 0 AND message_sent_at > 0 THEN message_sent_at END), "
    "MAX(CASE WHEN terminal = 0 AND message_sent_at > 0 THEN message_sent_at END), "
    "MAX(CASE WHEN terminal = 0 THEN attempts END) "
    f"FROM {HYDRATION_QUEUE_TABLE} GROUP BY kind ORDER BY kind"
)


def _identity(job: HydrationJob) -> tuple[str, int, int]:
    return job.kind, job.dialog_id, job.message_id


def _job_from_row(row: sqlite3.Row | tuple[object, ...]) -> HydrationJob:
    kind, dialog_id, message_id, due_at, attempts, message_sent_at, priority, terminal = row
    return HydrationJob(
        kind=cast(str, kind),
        dialog_id=int(cast(int | str, dialog_id)),
        message_id=int(cast(int | str, message_id)),
        due_at=int(cast(int | str, due_at)),
        attempts=int(cast(int | str, attempts)),
        message_sent_at=int(cast(int | str, message_sent_at)),
        priority=HydrationPriority(int(cast(int | str, priority))),
        terminal=bool(terminal),
    )


def _snapshot_from_row(row: sqlite3.Row | tuple[object, ...]) -> HydrationQueueKindSnapshot:
    (
        kind,
        active,
        due,
        foreground,
        backfill,
        terminal,
        oldest_message_sent_at,
        newest_message_sent_at,
        max_attempts,
    ) = row
    assert isinstance(kind, str)
    integer_values = (active, due, foreground, backfill, terminal)
    assert all(isinstance(value, int) for value in integer_values)
    assert oldest_message_sent_at is None or isinstance(oldest_message_sent_at, int)
    assert newest_message_sent_at is None or isinstance(newest_message_sent_at, int)
    assert max_attempts is None or isinstance(max_attempts, int)
    return HydrationQueueKindSnapshot(
        kind=kind,
        active=cast(int, active),
        due=cast(int, due),
        foreground=cast(int, foreground),
        backfill=cast(int, backfill),
        terminal=cast(int, terminal),
        oldest_message_sent_at=oldest_message_sent_at,
        newest_message_sent_at=newest_message_sent_at,
        max_attempts=0 if max_attempts is None else max_attempts,
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
            f"INSERT INTO {HYDRATION_QUEUE_TABLE} ({_JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, dialog_id, message_id) DO UPDATE SET "
            "due_at = CASE WHEN excluded.due_at < due_at THEN excluded.due_at ELSE due_at END, "
            "message_sent_at = MAX(message_sent_at, excluded.message_sent_at), "
            "priority = MAX(priority, excluded.priority)",
            (
                job.kind,
                job.dialog_id,
                job.message_id,
                job.due_at,
                job.attempts,
                job.message_sent_at,
                int(job.priority),
                int(job.terminal),
            ),
        )

    def due_jobs(self, now: int, limit: int, *, kind: str | None = None) -> list[HydrationJob]:
        """Return jobs due at or before *now* in deterministic queue order."""
        if limit <= 0:
            return []
        kind_clause = " AND hj.kind = ?" if kind is not None else ""
        parameters: tuple[object, ...] = (now, kind, limit) if kind is not None else (now, limit)
        rows = cast(
            list[tuple[object, ...]],
            self._conn.execute(
                f"SELECT {_SELECT_JOB_COLUMNS} FROM {HYDRATION_QUEUE_TABLE} AS hj "
                f"WHERE hj.due_at <= ? AND hj.terminal = 0{kind_clause} "
                "ORDER BY hj.priority DESC, hj.message_sent_at DESC, "
                "hj.due_at, hj.kind, hj.dialog_id, hj.message_id LIMIT ?",
                parameters,
            ).fetchall(),
        )
        return [_job_from_row(row) for row in rows]

    def snapshot(self, now: int) -> tuple[HydrationQueueKindSnapshot, ...]:
        """Return one compact operational snapshot without changing queue state."""
        rows = cast(list[tuple[object, ...]], self._conn.execute(_QUEUE_SNAPSHOT_SQL, (now,)).fetchall())
        return tuple(_snapshot_from_row(row) for row in rows)

    def start(self, job: HydrationJob) -> HydrationJob | None:
        """Atomically increment and return a queued job, or return ``None``.

        ``UPDATE ... RETURNING`` makes the increment and read one SQLite
        statement.  A missing identity therefore cannot manufacture a job.
        """
        row = cast(
            tuple[object, ...] | None,
            self._conn.execute(
                f"UPDATE {HYDRATION_QUEUE_TABLE} SET attempts = attempts + 1 "
                "WHERE kind = ? AND dialog_id = ? AND message_id = ? AND terminal = 0 "
                f"RETURNING {_JOB_COLUMNS}",
                _identity(job),
            ).fetchone(),
        )
        return None if row is None else _job_from_row(row)

    def reschedule(self, job: HydrationJob, due_at: int) -> bool:
        """Set the next due time for *job* without changing its attempts."""
        cursor = self._conn.execute(
            f"UPDATE {HYDRATION_QUEUE_TABLE} SET due_at = ? WHERE kind = ? AND dialog_id = ? AND message_id = ? AND terminal = 0",
            (due_at, *_identity(job)),
        )
        return cursor.rowcount > 0

    def mark_terminal(self, job: HydrationJob) -> bool:
        """Suppress a job permanently until its message fact is reconciled."""
        cursor = self._conn.execute(
            f"UPDATE {HYDRATION_QUEUE_TABLE} SET terminal = 1 WHERE kind = ? AND dialog_id = ? AND message_id = ?",
            _identity(job),
        )
        return cursor.rowcount > 0

    def remove(self, job: HydrationJob) -> bool:
        """Delete *job* and report whether a row was removed."""
        cursor = self._conn.execute(
            f"DELETE FROM {HYDRATION_QUEUE_TABLE} WHERE kind = ? AND dialog_id = ? AND message_id = ?",
            _identity(job),
        )
        return cursor.rowcount > 0

    def remove_active(self, job: HydrationJob) -> bool:
        """Delete one active job while retaining a terminal suppression."""
        cursor = self._conn.execute(
            f"DELETE FROM {HYDRATION_QUEUE_TABLE} WHERE kind = ? AND dialog_id = ? AND message_id = ? AND terminal = 0",
            _identity(job),
        )
        return cursor.rowcount > 0

    def remove_active_for_dialog(self, dialog_id: int) -> int:
        """Delete active jobs while retaining terminal suppressions."""
        if not self.is_available():
            return 0
        cursor = self._conn.execute(
            f"DELETE FROM {HYDRATION_QUEUE_TABLE} WHERE dialog_id = ? AND terminal = 0",
            (dialog_id,),
        )
        return cursor.rowcount

    def remove_for_message(self, dialog_id: int, message_id: int) -> int:
        """Delete every fact job associated with one message."""
        if not self.is_available():
            return 0
        cursor = self._conn.execute(
            f"DELETE FROM {HYDRATION_QUEUE_TABLE} WHERE dialog_id = ? AND message_id = ?",
            (dialog_id, message_id),
        )
        return cursor.rowcount

    def summarize_for_dialog(
        self, dialog_id: int, *, max_message_ids: int = DEFAULT_SUMMARY_MESSAGE_IDS
    ) -> tuple[HydrationQueueSummary, ...]:
        """Summarize all queued jobs before a dialog-wide purge.

        Aggregates are computed by SQLite so future-due rows are included while
        only queue coordinates (never message payloads) are read into memory.
        """
        if not self.is_available():
            return ()
        message_id_cap = max(0, max_message_ids)
        summaries: list[HydrationQueueSummary] = []
        for kind in _REGISTERED_HYDRATION_KINDS:
            row = cast(
                tuple[object, ...] | None,
                self._conn.execute(_SUMMARY_AGGREGATE_SQL, (kind, dialog_id)).fetchone(),
            )
            if row is None or int(cast(int | str, row[0])) == 0:
                continue
            id_rows = cast(
                list[tuple[object, ...]],
                self._conn.execute(_SUMMARY_MESSAGE_IDS_SQL, (kind, dialog_id, message_id_cap)).fetchall(),
            )
            summaries.append(
                HydrationQueueSummary(
                    kind=kind,
                    dialog_id=dialog_id,
                    job_count=int(cast(int | str, row[0])),
                    message_ids=tuple(int(cast(int | str, id_row[0])) for id_row in id_rows),
                    attempts_min=int(cast(int | str, row[1])),
                    attempts_max=int(cast(int | str, row[2])),
                )
            )
        return tuple(summaries)


__all__ = [
    "HYDRATION_QUEUE_TABLE",
    "HydrationJob",
    "HydrationPriority",
    "HydrationQueueRepository",
    "HydrationQueueSummary",
]
