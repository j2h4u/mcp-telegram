"""SQLite write adapter for the local Telegram folder snapshot."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import cast

from .contracts import FolderSourceSnapshot
from .ports import FolderSnapshotRepository


def _state_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def replace_folder_snapshot(
    conn: sqlite3.Connection,
    folders: Iterable[tuple[int, str]],
    memberships: Iterable[tuple[int, int]],
) -> None:
    """Replace folder rows for low-level test and maintenance fixtures."""
    with conn:
        conn.execute("DELETE FROM telegram_folder_members")
        conn.execute("DELETE FROM telegram_folders")
        conn.executemany("INSERT INTO telegram_folders(folder_id, title) VALUES (?, ?)", list(folders))
        conn.executemany("INSERT INTO telegram_folder_members(folder_id, dialog_id) VALUES (?, ?)", list(memberships))


class SQLiteFolderSnapshotRepository(FolderSnapshotRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def replace_snapshot(
        self,
        snapshot: FolderSourceSnapshot,
        memberships: tuple[tuple[int, int], ...],
        *,
        completed_at: int,
    ) -> int:
        """Replace tables and success metadata in one SQLite transaction."""
        with self._conn:
            previous = cast(  # pyright: ignore[reportAny]
                tuple[object] | None,
                self._conn.execute(
                    "SELECT value FROM daemon_state WHERE key = 'folder_snapshot_generation'"
                ).fetchone(),
            )
            try:
                generation = int(cast(int | str, previous[0])) + 1 if previous is not None else 1
            except TypeError, ValueError:
                generation = 1
            self._conn.execute("DELETE FROM telegram_folder_members")
            self._conn.execute("DELETE FROM telegram_folders")
            self._conn.executemany(
                "INSERT INTO telegram_folders(folder_id, title) VALUES (?, ?)",
                ((folder.folder_id, folder.title) for folder in snapshot.folders),
            )
            self._conn.executemany(
                "INSERT INTO telegram_folder_members(folder_id, dialog_id) VALUES (?, ?)", memberships
            )
            _set_state_values(
                self._conn,
                {
                    "folder_snapshot_generation": generation,
                    "folder_snapshot_last_attempt_at": completed_at,
                    "folder_snapshot_last_success_at": completed_at,
                    "folder_snapshot_last_outcome": "success",
                    "folder_snapshot_next_retry_at": None,
                    "folder_snapshot_consecutive_failures": 0,
                },
            )
        return generation

    def record_attempt(
        self,
        *,
        attempted_at: int,
        outcome: str,
        next_retry_at: int | None,
        consecutive_failures: int,
    ) -> None:
        """Persist failure metadata without touching the last complete snapshot."""
        with self._conn:
            _set_state_values(
                self._conn,
                {
                    "folder_snapshot_last_attempt_at": attempted_at,
                    "folder_snapshot_last_outcome": outcome,
                    "folder_snapshot_next_retry_at": next_retry_at,
                    "folder_snapshot_consecutive_failures": consecutive_failures,
                },
            )

    def read_consecutive_failures(self) -> int:
        return _state_int(self._read_state_value("folder_snapshot_consecutive_failures")) or 0

    def read_last_outcome(self) -> str | None:
        return self._read_state_value("folder_snapshot_last_outcome")

    def read_last_success_at(self) -> int | None:
        return _state_int(self._read_state_value("folder_snapshot_last_success_at"))

    def read_next_retry_at(self) -> int | None:
        return _state_int(self._read_state_value("folder_snapshot_next_retry_at"))

    def _read_state_value(self, key: str) -> str | None:
        row = cast(
            tuple[object] | None,
            self._conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone(),
        )
        return None if row is None or row[0] is None else str(row[0])


def _set_state_values(conn: sqlite3.Connection, values: dict[str, object | None]) -> None:
    conn.executemany(
        "INSERT INTO daemon_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ((key, None if value is None else str(value)) for key, value in values.items()),
    )
