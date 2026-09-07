"""Read-only SQLite queries for the Telegram folder projection."""

from __future__ import annotations

import sqlite3
import time
from typing import Protocol, cast, overload


class FolderReadCursor(Protocol):
    def fetchall(self) -> object: ...

    def fetchone(self) -> object | None: ...


class FolderReadConnection(Protocol):
    @overload
    def execute(self, sql: str, /) -> FolderReadCursor: ...

    @overload
    def execute(self, sql: str, _: tuple[int, ...], /) -> FolderReadCursor: ...


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def _state_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def folder_snapshot(
    conn: FolderReadConnection,
    *,
    stale_after_seconds: int,
    now: int | None = None,
) -> dict[str, object]:
    """Read compact folder projection health metadata without local writes."""
    values = _folder_state_values(conn)
    generation = _state_int(values.get("folder_snapshot_generation"))
    completed_at = _state_int(values.get("folder_snapshot_last_success_at"))
    age = None if completed_at is None else max(0, int(time.time() if now is None else now) - completed_at)
    complete = generation is not None and completed_at is not None
    return {
        "generation": generation,
        "status": _snapshot_status(complete, age, stale_after_seconds),
        "completed_at": completed_at,
        "age_seconds": age,
        "complete": complete,
    }


def _folder_state_values(conn: FolderReadConnection) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    try:
        rows = cast(
            list[tuple[str, str | None]],
            conn.execute(
                "SELECT key, value FROM daemon_state WHERE key IN "
                "('folder_snapshot_generation', 'folder_snapshot_last_success_at', "
                "'folder_snapshot_last_outcome', 'folder_snapshot_consecutive_failures')"
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        rows = []
    values.update({str(key): None if value is None else str(value) for key, value in rows})
    return values


def _snapshot_status(complete: bool, age: int | None, stale_after_seconds: int) -> str:
    if not complete:
        return "unavailable"
    if age is not None and age >= stale_after_seconds:
        return "stale"
    return "fresh"


def list_folders(conn: FolderReadConnection) -> list[dict[str, object]]:
    try:
        rows = cast(
            list[tuple[int, str]],
            conn.execute("SELECT folder_id, title FROM telegram_folders ORDER BY folder_id").fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        return []
    return [{"id": int(row[0]), "title": str(row[1])} for row in rows]


def folder_summaries(conn: FolderReadConnection) -> list[dict[str, object]]:
    """Return one compact structural summary per Telegram folder."""
    try:
        rows = cast(
            list[tuple[int, str, int, int, int, int | None]],
            conn.execute(
                """SELECT f.folder_id,
                          f.title,
                          COUNT(fm.dialog_id),
                          COALESCE(SUM(CASE WHEN COALESCE(d.unread_count, 0) > 0 THEN 1 ELSE 0 END), 0),
                          COALESCE(SUM(COALESCE(d.unread_count, 0)), 0),
                          MAX(d.last_message_at)
                   FROM telegram_folders AS f
                   LEFT JOIN telegram_folder_members AS fm USING(folder_id)
                   LEFT JOIN dialogs AS d ON d.dialog_id = fm.dialog_id
                   GROUP BY f.folder_id, f.title
                   ORDER BY f.folder_id"""
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        return []
    return [
        {
            "id": int(folder_id),
            "title": str(title),
            "dialog_count": int(dialog_count),
            "unread_dialog_count": int(unread_dialog_count),
            "unread_count": int(unread_count),
            "last_message_at": None if last_message_at is None else int(last_message_at),
        }
        for folder_id, title, dialog_count, unread_dialog_count, unread_count, last_message_at in rows
    ]


def folders_by_dialog(conn: FolderReadConnection) -> dict[int, list[dict[str, object]]]:
    result: dict[int, list[dict[str, object]]] = {}
    try:
        rows = cast(
            list[tuple[int, str, int]],
            conn.execute(
                """SELECT f.folder_id, f.title, m.dialog_id
                   FROM telegram_folders AS f
                   JOIN telegram_folder_members AS m USING(folder_id)
                   ORDER BY f.folder_id"""
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        return result
    for folder_id, title, dialog_id in rows:
        result.setdefault(int(dialog_id), []).append({"id": int(folder_id), "title": str(title)})
    return result


def dialog_placement(conn: FolderReadConnection, dialog_id: int) -> dict[str, object]:
    try:
        archived_row = cast(
            tuple[int] | None, conn.execute("SELECT archived FROM dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone()
        )
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        archived_row = None
    try:
        rows = cast(
            list[tuple[int, str]],
            conn.execute(
                """SELECT f.folder_id, f.title FROM telegram_folders AS f
               JOIN telegram_folder_members AS m USING(folder_id)
               WHERE m.dialog_id = ? ORDER BY f.folder_id""",
                (dialog_id,),
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        rows = []
    return {
        "archived": bool(archived_row[0]) if archived_row is not None else False,
        "folders": [{"id": int(row[0]), "title": str(row[1])} for row in rows],
    }
