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
    generation = _state_int(values.get("canonical_generation"))
    completed_at = _state_int(values.get("completed_at"))
    receipts = (_state_int(values.get("rule_observation_started_at")), _state_int(values.get("canonical_observed_at")))
    current = int(time.time() if now is None else now)
    freshness_receipts = tuple(receipt for receipt in receipts if receipt is not None)
    age = None if len(freshness_receipts) != len(receipts) else max(max(0, current - receipt) for receipt in freshness_receipts)
    complete = generation is not None and completed_at is not None and values.get("coverage_status") == "complete" and age is not None
    return {
        "generation": generation,
        "status": _snapshot_status(complete, age, min(stale_after_seconds, 900)),
        "completed_at": completed_at,
        "age_seconds": age,
        "complete": complete,
    }


def _folder_state_values(conn: FolderReadConnection) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    try:
        rows = cast(
            list[tuple[object, object, object, object, object]],
            conn.execute(
                "SELECT canonical_generation,completed_at,coverage_status,rule_observation_started_at,canonical_observed_at "
                "FROM telegram_folder_projection_state WHERE singleton=1"
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        rows = []
    if rows:
        generation, completed_at, coverage_status, rule_started_at, canonical_observed_at = rows[0]
        values.update(
            {
                "canonical_generation": None if generation is None else str(generation),
                "completed_at": None if completed_at is None else str(completed_at),
                "coverage_status": None if coverage_status is None else str(coverage_status),
                "rule_observation_started_at": None if rule_started_at is None else str(rule_started_at),
                "canonical_observed_at": None if canonical_observed_at is None else str(canonical_observed_at),
            }
        )
    return values


def _snapshot_status(complete: bool, age: int | None, stale_after_seconds: int) -> str:
    if not complete:
        return "unavailable"
    if age is not None and age >= stale_after_seconds:
        return "stale"
    return "fresh"


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
                   FROM telegram_folder_rules AS f
                   LEFT JOIN telegram_folder_local_members AS fm ON fm.namespace=f.namespace AND fm.folder_id=f.folder_id AND fm.state='present'
                   LEFT JOIN dialogs AS d ON d.dialog_id = fm.dialog_id
                   GROUP BY f.namespace, f.folder_id, f.title, f.source_position
                   ORDER BY f.source_position"""
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
                   FROM telegram_folder_rules AS f
                   JOIN telegram_folder_local_members AS m ON m.namespace=f.namespace AND m.folder_id=f.folder_id
                   WHERE m.state='present'
                   ORDER BY f.source_position, m.pin_position IS NULL, m.pin_position, m.dialog_id"""
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
                """SELECT f.folder_id, f.title FROM telegram_folder_rules AS f
               JOIN telegram_folder_local_members AS m ON m.namespace=f.namespace AND m.folder_id=f.folder_id
               WHERE m.dialog_id = ? AND m.state='present' ORDER BY f.source_position""",
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
