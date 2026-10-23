"""Collection-specific ordering for dialog pins; fence rows contain no payload."""

from __future__ import annotations

import sqlite3
from typing import cast

from .sync_transactions import require_write_transaction


def claim_pin_delta(conn: sqlite3.Connection, folder_id: int, dialog_id: int, pinned: bool, order: int) -> bool:
    require_write_transaction(conn)
    floor = cast(
        tuple[int] | None,
        conn.execute(
            "SELECT observation_order FROM dialog_pin_snapshot_observations WHERE folder_id=?", (folder_id,)
        ).fetchone(),
    )
    if floor is not None and order <= floor[0]:
        return False
    return (
        conn.execute(
            "INSERT INTO dialog_pin_observations(folder_id,dialog_id,observation_order,pinned) VALUES (?,?,?,?) "
            "ON CONFLICT(folder_id,dialog_id) DO UPDATE SET observation_order=excluded.observation_order,pinned=excluded.pinned "
            "WHERE excluded.observation_order>dialog_pin_observations.observation_order RETURNING 1",
            (folder_id, dialog_id, order, int(pinned)),
        ).fetchone()
        is not None
    )


def ordered_pin_overlay(
    conn: sqlite3.Connection, folder_id: int, base: list[int], floor: int | None = None
) -> list[int]:
    """Prepend newer pins in retained order and remove newer unpins."""
    if floor is None:
        row = cast(
            tuple[int] | None,
            conn.execute(
                "SELECT observation_order FROM dialog_pin_snapshot_observations WHERE folder_id=?", (folder_id,)
            ).fetchone(),
        )
        floor = int(row[0]) if row is not None else 0
    rows = cast(
        list[tuple[int, int]],
        conn.execute(
            "SELECT dialog_id,pinned FROM dialog_pin_observations WHERE folder_id=? AND observation_order>? "
            "ORDER BY observation_order DESC,dialog_id",
            (folder_id, floor),
        ).fetchall(),
    )
    changed = {dialog_id for dialog_id, _ in rows}
    return [dialog_id for dialog_id, pinned in rows if pinned] + [
        dialog_id for dialog_id in base if dialog_id not in changed
    ]


def claim_pin_snapshot(conn: sqlite3.Connection, folder_id: int, order: int, base: list[int]) -> list[int] | None:
    require_write_transaction(conn)
    accepted = cast(
        tuple[int] | None,
        conn.execute(
            "INSERT INTO dialog_pin_snapshot_observations(folder_id,observation_order) VALUES (?,?) "
            "ON CONFLICT(folder_id) DO UPDATE SET observation_order=excluded.observation_order "
            "WHERE excluded.observation_order>dialog_pin_snapshot_observations.observation_order RETURNING 1",
            (folder_id, order),
        ).fetchone(),
    )
    if accepted is None:
        return None
    conn.execute("DELETE FROM dialog_pin_observations WHERE folder_id=? AND observation_order<=?", (folder_id, order))
    return ordered_pin_overlay(conn, folder_id, base, order)
