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


def list_folder_messages(conn: FolderReadConnection, folder_id: int, limit: int) -> dict[str, object]:
    rows = cast(
        list[tuple[int, int, int, str | None, str | None, str | None, str | None]],
        conn.execute(
            """SELECT m.dialog_id, m.message_id, m.sent_at, m.text, m.media_description, m.media_kind, d.name
           FROM telegram_folder_members fm JOIN messages m ON m.dialog_id = fm.dialog_id
           LEFT JOIN dialogs d ON d.dialog_id = m.dialog_id
           WHERE fm.folder_id = ? AND m.is_deleted = 0
           ORDER BY m.sent_at DESC, m.message_id DESC LIMIT ?""",
            (folder_id, limit),
        ).fetchall(),
    )
    incomplete_rows = cast(
        list[tuple[int]],
        conn.execute(
            """SELECT fm.dialog_id FROM telegram_folder_members fm
           LEFT JOIN synced_dialogs sd ON sd.dialog_id = fm.dialog_id
           WHERE fm.folder_id = ? AND (sd.dialog_id IS NULL OR sd.status != 'synced')
           ORDER BY fm.dialog_id""",
            (folder_id,),
        ).fetchall(),
    )
    incomplete = [int(row[0]) for row in incomplete_rows]
    return {
        "folder_id": folder_id,
        "messages": [
            {
                "dialog_id": int(dialog_id),
                "message_id": int(message_id),
                "sent_at": int(sent_at),
                "text": text,
                "media_description": media_description,
                "media_kind": media_kind,
                "dialog_name": dialog_name,
            }
            for dialog_id, message_id, sent_at, text, media_description, media_kind, dialog_name in rows
        ],
        "partial": bool(incomplete),
        "incomplete_dialog_ids": incomplete,
        "next_navigation": None,
    }


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
