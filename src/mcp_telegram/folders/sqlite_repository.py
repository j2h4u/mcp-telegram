"""SQLite adapter for the local Telegram folder snapshot."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import cast

from .contracts import FolderSourceSnapshot
from .ports import FolderSnapshotRepository


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def replace_folder_snapshot(
    conn: sqlite3.Connection, folders: Iterable[tuple[int, str]], memberships: Iterable[tuple[int, int]]
) -> None:
    folder_rows = list(folders)
    membership_rows = list(memberships)
    with conn:
        conn.execute("DELETE FROM telegram_folder_members")
        conn.execute("DELETE FROM telegram_folders")
        conn.executemany("INSERT INTO telegram_folders(folder_id, title) VALUES (?, ?)", folder_rows)
        conn.executemany("INSERT INTO telegram_folder_members(folder_id, dialog_id) VALUES (?, ?)", membership_rows)


class SQLiteFolderSnapshotRepository(FolderSnapshotRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def replace_snapshot(self, snapshot: FolderSourceSnapshot, memberships: tuple[tuple[int, int], ...]) -> None:
        replace_folder_snapshot(
            self._conn,
            ((folder.folder_id, folder.title) for folder in snapshot.folders),
            memberships,
        )


def list_folders(conn: sqlite3.Connection) -> list[dict[str, object]]:
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


def list_folder_messages(conn: sqlite3.Connection, folder_id: int, limit: int) -> dict[str, object]:
    rows = cast(
        list[tuple[int, int, int, str | None, str | None]],
        conn.execute(
            """SELECT m.dialog_id, m.message_id, m.sent_at, m.text, d.name
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
           WHERE fm.folder_id = ? AND (sd.dialog_id IS NULL OR sd.status != 'synced'
             OR sd.total_messages IS NULL OR sd.sync_progress IS NULL OR sd.sync_progress < sd.total_messages)
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
                "dialog_name": dialog_name,
            }
            for dialog_id, message_id, sent_at, text, dialog_name in rows
        ],
        "partial": bool(incomplete),
        "incomplete_dialog_ids": incomplete,
        "next_navigation": None,
    }


def folder_ids_by_dialog(conn: sqlite3.Connection) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {}
    try:
        rows = cast(
            list[tuple[int, int]],
            conn.execute("SELECT folder_id, dialog_id FROM telegram_folder_members ORDER BY folder_id").fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        return result
    for folder_id, dialog_id in rows:
        result.setdefault(int(dialog_id), []).append(int(folder_id))
    return result


def dialog_placement(conn: sqlite3.Connection, dialog_id: int) -> dict[str, object]:
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
