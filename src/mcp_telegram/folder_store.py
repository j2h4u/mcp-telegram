"""Local snapshot of Telegram custom dialog folders.

This module is deliberately free of Telethon dependencies.  The daemon owns
snapshot replacement; readers only consume the two relational tables.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import cast


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def replace_folder_snapshot(
    conn: sqlite3.Connection,
    folders: Iterable[tuple[int, str]],
    memberships: Iterable[tuple[int, int]],
) -> None:
    """Atomically replace the complete custom-folder snapshot."""
    folder_rows = list(folders)
    membership_rows = list(memberships)
    with conn:
        conn.execute("DELETE FROM telegram_folder_members")
        conn.execute("DELETE FROM telegram_folders")
        conn.executemany(
            "INSERT INTO telegram_folders(folder_id, title) VALUES (?, ?)",
            folder_rows,
        )
        conn.executemany(
            "INSERT INTO telegram_folder_members(folder_id, dialog_id) VALUES (?, ?)",
            membership_rows,
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
            tuple[int] | None,
            conn.execute("SELECT archived FROM dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
        )
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        archived_row = None
    try:
        rows = cast(
            list[tuple[int, str]],
            conn.execute(
                """SELECT f.folder_id, f.title
                   FROM telegram_folders AS f
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
